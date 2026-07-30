"""Fail-closed administrator bootstrap.

Creating the first administrator is a one-time act that needs explicit
credentials. It used to be neither: ``seed_admin`` read ADMIN_USERNAME and
ADMIN_PASSWORD with hard-coded fallbacks, so an unconfigured machine got a
known password; and it re-aligned the stored hash whenever the environment
disagreed with the database, so an *unset* ADMIN_PASSWORD silently reset the
administrator's password back to that known value on every single restart.

Two rules replace that:

* No credential, no bootstrap. Missing or blank configuration is refused with a
  controlled error that names the variable and never its value.
* Startup never modifies an administrator that already exists - not the
  username, not the hash, not anything. Rotating a password is an
  administrative action someone performs deliberately, not something a reboot
  does on their behalf.

Nothing here prints, logs or formats a password or a hash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from auth import hash_password
from models import HQUser


ADMIN_USERNAME_ENV = "ADMIN_USERNAME"
ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"

CREATED = "created"
ALREADY_PRESENT = "already_present"
#: Rows exist, but none of them is an enabled administrator. Startup continues
#: and says so; see ``seed.seed_admin`` for why it neither creates nor refuses.
NO_ENABLED_ADMINISTRATOR = "no_enabled_administrator"

#: What counts as an administrator, copied deliberately from the check an
#: operator has already been shown - 'it holds at least one enabled
#: administrator' in Test-SpeakLinkPersistentLanServer.ps1.
#:
#: Both spellings of the legacy role are here on purpose: a database that has not
#: been through ``migrate_legacy_roles`` still holds lowercase 'admin', and that
#: account can sign in and administer. Treating it as unprivileged would make
#: startup demand bootstrap credentials for an HQ that already has a working
#: administrator - which is the defect this constant exists to fix, in a new form.
PRIVILEGED_ROLES = frozenset({"OWNER", "ADMIN", "SUPER_ADMIN", "admin"})


class AdminBootstrapError(RuntimeError):
    """The administrator bootstrap configuration was missing or unusable.

    Raised before anything is read from or written to the database, so a
    refusal can never leave a half-created administrator behind.
    """


class AdminStateUnavailable(AdminBootstrapError):
    """Whether an administrator exists could not be established.

    A subclass, so an existing caller handling ``AdminBootstrapError`` still
    fails closed rather than letting an unreadable database through a narrower
    ``except``.

    This is never "there are none". The convenient answer to an unreadable
    database is zero administrators, and zero is the answer that creates one.
    """


def count_enabled_administrators(db: Session) -> int:
    """How many accounts can actually administer this HQ right now.

    Roles are compared in Python rather than in SQL for the same reason the
    PowerShell verifier does it: the role column holds legacy and normalised
    spellings, and an ``IN (...)`` list is one quoting accident away from
    reporting a healthy database as having no administrator.

    ``is_active`` is the flag rather than ``lifecycle_state`` because
    ``user_lifecycle`` writes both in one statement and ``is_active`` is the one
    that exists on every schema version, including databases that predate the
    lifecycle columns entirely.

    Any failure raises ``AdminStateUnavailable``. There is deliberately no
    ``except`` that returns 0.
    """
    try:
        rows = db.query(HQUser.role, HQUser.is_active).all()
    except Exception as failure:
        raise AdminStateUnavailable(
            "the administrator state could not be read from the database "
            f"({failure.__class__.__name__}). Refusing to continue rather than "
            "assume there is no administrator: assuming none is what would "
            "create one."
        ) from None
    return sum(1 for role, active in rows if active and str(role) in PRIVILEGED_ROLES)


@dataclass(frozen=True)
class BootstrapCredentials:
    """Explicit bootstrap credentials.

    ``__repr__`` is written by hand because the default one would print the
    password into any traceback that happens to include this object.
    """

    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - exercised through tests
        return f"BootstrapCredentials(username={self.username!r}, password=<hidden>)"

    __str__ = __repr__


def _required(environment, name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise AdminBootstrapError(
            f"{name} is not set, or is blank. SpeakLink refuses to start without "
            "explicit administrator bootstrap credentials, because the "
            "alternative is a password everybody already knows. Set "
            f"{ADMIN_USERNAME_ENV} and {ADMIN_PASSWORD_ENV} in the environment "
            "or in backend/.env, then start again."
        )
    return value


def resolve_bootstrap_credentials(environment=None) -> BootstrapCredentials:
    """Read and validate the bootstrap credentials, or refuse.

    The username is stripped because a trailing space in a config file is a
    typo, not a different account. The password is used exactly as supplied -
    trimming it would silently change the operator's secret.
    """
    source = os.environ if environment is None else environment
    username = _required(source, ADMIN_USERNAME_ENV).strip()
    password = _required(source, ADMIN_PASSWORD_ENV)
    return BootstrapCredentials(username=username, password=password)


def bootstrap_administrator(db: Session, credentials: BootstrapCredentials) -> str:
    """Create the first administrator, or leave the existing one alone.

    Returns ``CREATED`` or ``ALREADY_PRESENT``. The check is "does any HQ user
    exist", not "does *this* username exist": a changed ADMIN_USERNAME used to
    add a second row, which then looked exactly like a broken password because
    the operator was signing in as an account that had just been created empty.
    """
    if db.query(HQUser).first() is not None:
        return ALREADY_PRESENT

    db.add(
        HQUser(
            username=credentials.username,
            password_hash=hash_password(credentials.password),
            role="admin",
        )
    )
    db.commit()
    return CREATED
