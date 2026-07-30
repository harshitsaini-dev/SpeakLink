"""Mint the first Receiver HMAC key container, once, and only into emptiness.

WHY THIS MODULE EXISTS

Three places in this repository stated that the backend creates this container on
first start - ``tools/hq_runtime.py``, ``scripts/Test-SpeakLinkHQAutoStart.ps1``,
and the tests written to match them. ``backend/server.py`` did not: its
``receiver_key_ring()`` calls ``load_key_ring`` and says in its own docstring
that the container is never created there. Three documented owners, no
implementation. The first installed HQ start produced a server that ran and
failed ``the Receiver key container is present``.

WHY IT LIVES IN THE BACKEND RATHER THAN THE SUPERVISOR

DPAPI ``CURRENT_USER`` binds the sealed blob to the identity that sealed it. The
backend is the process that must *open* it, so the backend is the only process
that can guarantee it will be openable. A supervisor that sealed it would work
today, because the child runs as the same user, and would fail silently the day
HQ moves to a service account - as `KeyCustodyUnavailable`, which
``receiver_key_ring()`` turns into ``None``, which turns into the legacy
authenticator, which looks like a working server.

Two supporting reasons. ``hq_runtime.spec`` excludes SQLAlchemy and starts the
backend as a child under the machine's own Python, so the supervisor counts
Devices with raw ``sqlite3`` out of necessity; putting creation there would mean
a second Device count and a second container-path resolution, and duplicated
*policy* is what this repository has been bitten by before. And the ordering
matters: ``build_receiver_runtime_authenticator()`` runs at import and returns
the legacy authenticator alone when there is no ring, for the life of the
process, so a container minted anywhere later is not used until a restart.

The division of labour is deliberate and neither half creates what the other
should: **the supervisor refuses early** when a container is missing and Devices
are enrolled, so no child is even started; **the backend creates** when, and only
when, creating harms nobody.

WHAT MAKES CREATION SAFE

Exactly one state: the container is missing and the database holds zero enrolled
Devices. One enrolled Device plus a missing container is not a first start, it is
an emergency - a new key would leave every Device credential unverifiable while
every Store still looked enrolled, and 44 Stores would have to re-enrol.

An unestablished count is never zero. The convenient wrong answer here is the
dangerous one.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from enum import Enum
from pathlib import Path

from key_custody import (
    KeyCustodyError,
    Protector,
    create_key_container,
    load_key_ring,
)

logger = logging.getLogger("speaklink.keys")

#: The table the supervisor counts too. Named once here so the two agree.
DEVICE_TABLE = "receiver_devices"


class BootstrapOutcome(Enum):
    """What the startup actually did. Returned rather than logged as prose so a
    caller can assert on it."""

    CREATED = "created"
    REUSED = "reused"


class KeyBootstrapRefused(Exception):
    """Startup must stop. Nothing was created.

    One exception type for every refusal, so a caller cannot handle the
    enrolled-Devices case and accidentally let a corrupt database through.
    """


def count_enrolled_devices(database: Path) -> int:
    """How many Receiver Devices exist. Read-only, and never a guess.

    ``mode=ro`` AND ``immutable=1``. ``mode=ro`` alone still builds the
    shared-memory index a WAL database wants, and creating it is a file creation
    beside a database this module has no business writing to.

    TWO ABSENCES ARE SAFE AND EVERY OTHER FAILURE IS NOT. The rule being enforced
    is "do not mint a key while credentials exist", so what matters is whether
    credentials can exist:

    * **No database file at all** - nothing can be enrolled in a file that is not
      there, so this is 0 with certainty rather than a guess. The production
      supervisor refuses on a missing persistent database before the backend is
      ever started (``resolve_runtime_profile``), so on a managed HQ this branch
      is reached only by a backend started directly against a database it is about
      to create.
    * **No ``receiver_devices`` table** - a database that predates Device
      enrolment genuinely has no Devices.

    A file that exists but cannot be READ - corrupt, locked, permission denied -
    raises. "I could not count them" must never become "there are none", because
    zero is the answer that mints a key over credentials still in use.
    """
    database = Path(database)
    if not database.exists():
        return 0

    uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        present = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (DEVICE_TABLE,),
        ).fetchall()
        if not present:
            return 0
        return int(connection.execute(f"SELECT COUNT(*) FROM {DEVICE_TABLE}").fetchone()[0])
    finally:
        connection.close()


def bootstrap_receiver_key_container(
    *,
    container_path,
    database_path,
    protector: Protector,
    count_devices=None,
    create=None,
) -> BootstrapOutcome:
    """Ensure a usable key container exists, or refuse and create nothing.

    ``count_devices`` and ``create`` are injectable so the refusal paths can be
    driven in tests without a damaged disk. Production passes neither.
    """
    container = Path(container_path)
    counter = count_devices or count_enrolled_devices
    creator = create or create_key_container

    # ---------------------------------------------------------------- reuse
    # Checked first and returned early, so nothing below can run against an
    # existing container. An existing container is reused whatever the Device
    # count says: the enrolled-Device rule guards CREATION, and applying it here
    # would refuse to start a perfectly healthy HQ.
    if container.exists():
        try:
            load_key_ring(container, protector=protector)
        except KeyCustodyError as failure:
            # Deliberately not replaced. A corrupt or foreign-sealed container may
            # be recoverable from a backup; overwriting it destroys the only copy
            # of the keys that verify existing credentials.
            raise KeyBootstrapRefused(
                f"the Receiver key container at {container} exists but could not be "
                f"opened ({failure.__class__.__name__}). It has NOT been replaced: "
                "the keys inside it are the only ones that verify the Receiver "
                "credentials already in the database. Restore it from a backup, or "
                "confirm this host is the one that sealed it - DPAPI CURRENT_USER "
                "binds a container to the identity that created it."
            ) from None
        logger.info("Receiver key container present at %s; reused unchanged", container)
        return BootstrapOutcome.REUSED

    # ------------------------------------------------------------- count first
    # Before creating anything, and fail closed on every error including ones
    # this module did not anticipate.
    try:
        enrolled = counter(Path(database_path))
    except Exception as failure:
        raise KeyBootstrapRefused(
            f"the Receiver key container is missing from {container} and the number "
            f"of enrolled Devices could not be established from {database_path} "
            f"({failure.__class__.__name__}). Refusing to start rather than assume "
            "the database is empty: assuming zero is what would mint a new key over "
            "credentials that are still in use. Nothing has been created."
        ) from None

    if enrolled != 0:
        raise KeyBootstrapRefused(
            f"the Receiver key container is missing from {container}, and {enrolled} "
            "Device(s) are enrolled against it. Creating one now would make every "
            "one of those Device credentials unverifiable while every Store still "
            "looked enrolled, and all of them would have to re-enrol. Restore the "
            "container from a backup first. If those Devices really are gone, remove "
            "them deliberately through HQ rather than by starting the server. "
            "Nothing has been created."
        )

    # ------------------------------------------------------------------ create
    # create_key_container writes through a sibling temporary file and os.replace,
    # and removes the temporary on any failure, so a half-written container cannot
    # be left behind. The belt-and-braces cleanup below covers a creator that got
    # further than sealing - including the injected one the tests use.
    try:
        creator(container, protector=protector)
    except Exception as failure:
        _remove_partial(container)
        raise KeyBootstrapRefused(
            f"the Receiver key container at {container} could not be created "
            f"({failure.__class__.__name__}). Nothing has been left behind. If this "
            "is a DPAPI failure, check that HQ is running as the account that owns "
            "this profile."
        ) from None

    # Creation is only complete when the ring opens. A container that seals but
    # does not load would let the backend start and answer 503 to every enrolment,
    # which is a fault reported far from its cause.
    try:
        load_key_ring(container, protector=protector)
    except Exception as failure:
        _remove_partial(container)
        raise KeyBootstrapRefused(
            f"a Receiver key container was written to {container} but could not be "
            f"read back ({failure.__class__.__name__}). It has been removed rather "
            "than left in place, because an unreadable container looks present to "
            "every check that only tests for the file."
        ) from None

    logger.warning(
        "Created the first Receiver key container at %s (0 Devices enrolled). "
        "Back this file up: without it, every Device enrolled from now on must "
        "re-enrol.",
        container,
    )
    return BootstrapOutcome.CREATED


def _remove_partial(container: Path) -> None:
    """Leave nothing behind. A file that exists is a file every check believes."""
    try:
        if container.exists():
            container.unlink()
    except OSError:
        logger.error(
            "Could not remove the partial key container at %s. Delete it before "
            "starting HQ again.",
            container,
        )


def bootstrap_from_environment(*, container_path, protector) -> "BootstrapOutcome | None":
    """The production entry point, gated on being a managed HQ start.

    Returns ``None`` when this process is not a managed start, having attempted
    nothing.

    BOTH variables must be set explicitly, and the pair is the gate rather than
    either one alone:

    * ``SPEAKLINK_KEY_CONTAINER`` - because the fallback is
      ``SERVICE_CONTAINER_PATH`` under ``C:\\ProgramData``, which is the machine's
      real service custody path. Gating on ``SPEAKLINK_DB_PATH`` alone was the
      first version of this, and it would have had the **test suite** mint a live
      key container there: conftest always sets ``SPEAKLINK_DB_PATH``, the
      temporary database has zero Devices, so every run would have created a real
      key that a later service-account HQ would find and reuse. A key nobody
      decided to make is exactly what this module exists to prevent.
    * ``SPEAKLINK_DB_PATH`` - because without it there is no database to count
      honestly, and creating against a guess is what every refusal above is for.

    ``tools/hq_runtime.py`` and ``Start-SpeakLinkPersistentLanServer.ps1`` both set
    the pair. A developer running the backend by hand sets neither and gets
    today's behaviour unchanged.
    """
    container = os.environ.get("SPEAKLINK_KEY_CONTAINER")
    database = os.environ.get("SPEAKLINK_DB_PATH")
    if not container or not database:
        return None
    return bootstrap_receiver_key_container(
        container_path=container_path,
        database_path=Path(database),
        protector=protector,
    )
