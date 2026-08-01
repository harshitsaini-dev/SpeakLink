"""Joining a one-time enrolment code to a Receiver Device credential.

Two halves already existed and had never been introduced to each other.
``receiver_enrollment_codes`` decides *who may ask*;
``receiver_device_service.enroll_receiver_device`` issues the credential, hashes
it under a versioned HMAC key and returns the raw value exactly once. Nothing
here re-implements either.

The interesting problem is that they do not share a transaction. The code lives
in the ORM session; the device enrolment opens its own connection and runs its
own ``BEGIN IMMEDIATE``. So the order is chosen deliberately:

1. **Claim the code first.** If the device were created first, two computers
   racing with the same code would both enrol, and the whole point of a
   single-use code would be gone.
2. **Release the claim if enrolment fails.** Claiming first means a server-side
   fault would otherwise burn a perfectly good code, and the operator would
   issue a replacement for no reason.

That leaves a narrow window: a process killed between the claim and the release
leaves a spent code. That is the safe direction to fail - a wasted code costs a
minute, a double enrolment costs an identity - and it is written down rather
than hidden.

Nothing in this module logs, formats or returns a credential except the single
one-time delivery the caller asks for.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from key_custody import KeyRing
from receiver_device_service import (
    ReceiverDeviceServiceError,
    ReceiverEnrollmentResult,
    enroll_receiver_device,
)
from receiver_enrollment_codes import (
    EnrollmentCodeError,
    IssuedEnrollmentCode,
    ReceiverEnrollmentCode,
    issue_enrollment_code,
    record_enrolled_device,
    redeem_enrollment_code,
)
from models import Store


#: Enough for a primary, a standby and one spare in flight. A stuck script that
#: mints codes forever turns the table into a haystack of live credentials.
MAX_OUTSTANDING_CODES_PER_STORE = 3

MAX_DEVICE_NAME = 200
MAX_HOSTNAME = 253          # the longest a DNS name can be
MAX_SOFTWARE_VERSION = 64

PHASE_ONE_TABLES = ("receiver_devices", "receiver_credentials")


class EnrollmentApiError(Exception):
    """Base class, so no caller handles one failure and misses another."""


class EnrollmentRefused(EnrollmentApiError):
    """The request was wrong: bad code, inactive Store, invalid metadata.

    One class for all of them on purpose. Telling an unauthenticated caller
    *which* of "unknown", "expired" and "already used" applied would let them
    map out valid codes.
    """


class TooManyOutstandingCodes(EnrollmentApiError):
    """This Store already has as many live codes as it is allowed."""


class EnrollmentUnavailable(EnrollmentApiError):
    """The platform is not ready: no phase-one schema, or no key ring.

    Separate from EnrollmentRefused because this is the operator's problem, not
    the caller's, and it must never look like a bad code.
    """


class DeviceNotFound(EnrollmentApiError):
    """No Device with that public id."""


def _bounded(value, *, field: str, limit: int, required: bool = True) -> str:
    if value is None:
        if required:
            raise EnrollmentRefused(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise EnrollmentRefused(f"{field} must be text")
    cleaned = value.strip()
    if required and not cleaned:
        raise EnrollmentRefused(f"{field} is required")
    if len(cleaned) > limit:
        raise EnrollmentRefused(f"{field} must be at most {limit} characters")
    return cleaned


def _require_phase_one(engine: Engine) -> None:
    with engine.connect() as connection:
        present = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    missing = [table for table in PHASE_ONE_TABLES if table not in present]
    if missing:
        raise EnrollmentUnavailable(
            "Receiver device enrolment is not available on this server: the "
            f"phase-one schema is missing ({', '.join(missing)}). Apply it with "
            "tools/receiver_migration.py during a maintenance window."
        )
    # Self-healing rather than startup-only: any caller that ran Phase One
    # directly (a test fixture, a maintenance script) and never went through
    # server.py's startup_event still gets the archive column the moment it
    # reads or writes a Device - the same "add it wherever it might be
    # missing" rule ensure_rbac_schema's session_version column follows.
    ensure_device_archive_schema(engine)


def _active_store(db: Session, store_id: int) -> Store:
    store = db.query(Store).filter(Store.id == store_id).first()
    if store is None or not store.is_active:
        raise EnrollmentRefused("that Store is not available for enrolment")
    return store


def create_enrollment_code(
    db: Session, *, store_id: int, actor_user_id: int
) -> IssuedEnrollmentCode:
    """Mint one code for one Store. The raw value is returned exactly once."""
    _active_store(db, store_id)

    # LIVE codes, which is what "outstanding" has always been documented to
    # mean - see MAX_OUTSTANDING_CODES_PER_STORE's own comment about codes "in
    # flight" and a "haystack of live credentials".
    #
    # The expiry term used to be missing, and nothing in this codebase ever
    # prunes, deletes or marks an expired code: redeemed_at_epoch stays NULL for
    # ever. So three abandoned codes locked a Store out of enrolment
    # PERMANENTLY - an ordinary sequence across 44 Stores, where an admin clicks
    # Generate during a failed setup visit and again a week later. The refusal
    # even advised waiting for them to expire, which could never help.
    outstanding = (
        db.query(ReceiverEnrollmentCode)
        .filter(
            ReceiverEnrollmentCode.store_id == store_id,
            ReceiverEnrollmentCode.redeemed_at_epoch.is_(None),
            ReceiverEnrollmentCode.expires_at_epoch > time.time(),
        )
        .count()
    )
    if outstanding >= MAX_OUTSTANDING_CODES_PER_STORE:
        raise TooManyOutstandingCodes(
            f"this Store already has {outstanding} live enrolment codes; "
            "use one, or wait for them to expire, before creating another"
        )

    try:
        return issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_user_id)
    except EnrollmentCodeError as refusal:
        raise EnrollmentRefused(str(refusal)) from None


def _release_claim(db: Session, code_id: int) -> None:
    """Give a claimed code back after a failed enrolment."""
    db.query(ReceiverEnrollmentCode).filter(
        ReceiverEnrollmentCode.id == code_id
    ).update({ReceiverEnrollmentCode.redeemed_at_epoch: None}, synchronize_session=False)
    db.commit()


def _require_device_auth_capable_state(engine: Engine) -> None:
    """Refuse enrolment when the migration state cannot verify what we would issue.

    Reads the state directly rather than trusting a cached authenticator: the
    question is what the DATABASE will do at verification time, and that is the
    only place it is recorded.
    """
    from receiver_auth_reasons import (
        AuthReason,
        DeviceEnrolmentBlocked,
        state_can_verify_device_credentials,
    )

    try:
        with engine.connect() as connection:
            row = connection.exec_driver_sql(
                "SELECT state FROM receiver_credential_migration_state"
            ).fetchone()
    except Exception:
        # Unreadable state is not permission to proceed. Issuing a credential we
        # cannot confirm is verifiable is exactly how this fleet got stuck.
        raise DeviceEnrolmentBlocked(AuthReason.MIGRATION_STATE_BLOCKS_DEVICE_AUTH) from None

    state = row[0] if row else None
    if not state_can_verify_device_credentials(state):
        raise DeviceEnrolmentBlocked(
            AuthReason.BACKFILL_REQUIRED if state == "legacy_only"
            else AuthReason.MIGRATION_STATE_BLOCKS_DEVICE_AUTH
        )


def redeem_and_enroll(
    db: Session,
    engine: Engine,
    *,
    code: str,
    device_name: str,
    hostname: str,
    software_version: str,
    key_ring: KeyRing | None,
    actor_user_id: int | None = None,
) -> ReceiverEnrollmentResult:
    """Spend a code and enrol one Device. The credential is returned once."""
    # Validate before claiming, so bad metadata never costs a code.
    device_name = _bounded(device_name, field="device_name", limit=MAX_DEVICE_NAME)
    _bounded(hostname, field="hostname", limit=MAX_HOSTNAME, required=False)
    _bounded(software_version, field="software_version", limit=MAX_SOFTWARE_VERSION, required=False)

    if key_ring is None:
        raise EnrollmentUnavailable(
            "Receiver device enrolment is not available on this server: the HMAC "
            "key container could not be opened."
        )
    _require_phase_one(engine)
    # BEFORE the code is claimed. HQ must not issue a credential it cannot
    # verify: in legacy_only the runtime never computes a hashed identity at all,
    # so every Device enrolled there was unusable from the moment it was created.
    # Four such Devices exist on the live HQ and not one ever authenticated - and
    # their rows then BLOCKED the supported backfill that would have opened the
    # gate. A refusal here costs nothing; issuing costs a one-time code, a dead
    # Device row, and a migration that can no longer run.
    _require_device_auth_capable_state(engine)

    try:
        redeemed = redeem_enrollment_code(db, code)
    except EnrollmentCodeError as refusal:
        raise EnrollmentRefused(str(refusal)) from None

    signing_version, signing_key = key_ring.signing_key()
    # The Receiver computer redeeming this code is unauthenticated - it has no
    # credential yet. The administrator who issued the code authorised this
    # Device, so the Device is attributed to them.
    actor = actor_user_id if actor_user_id is not None else redeemed.created_by
    try:
        _active_store(db, redeemed.store_id)
        result = enroll_receiver_device(
            engine,
            store_id=redeemed.store_id,
            display_name=device_name,
            actor_user_id=actor,
            hash_key=signing_key,
            hash_key_version=signing_version,
        )
        # Which Device this code produced, recorded rather than inferred later
        # from a store_id and a timestamp. Best-effort on purpose: the
        # credential has already been issued, so failing here would strand a
        # Device that HQ has already created. A missing link degrades reported
        # progress; it must never undo a successful enrolment.
        try:
            record_enrolled_device(db, code_id=redeemed.code_id,
                                   device_public_id=result.device_public_id)
        except Exception:  # noqa: BLE001 - never fail an issued enrolment
            db.rollback()
        return result
    except ReceiverDeviceServiceError as failure:
        _release_claim(db, redeemed.code_id)
        raise EnrollmentRefused(str(failure)) from None
    except Exception:
        # Any other fault - a disk error, a bug - must not silently spend the
        # operator's code. Give it back, then let the error surface unchanged.
        _release_claim(db, redeemed.code_id)
        raise


# ---------------------------------------------------------------------------
# Device administration. No response here ever carries credential material.
# ---------------------------------------------------------------------------
_DEVICE_COLUMNS = (
    "public_id", "store_id", "display_name", "status",
    "enrolled_at", "disabled_at", "created_at", "updated_at", "archived_at",
)


def ensure_device_archive_schema(engine: Engine) -> None:
    """Add ``receiver_devices.archived_at`` if missing. Additive and idempotent.

    ``status`` already has a fixed CHECK constraint of ``active`` /
    ``disabled`` / ``retired`` (SQLite cannot ALTER a CHECK constraint without
    rebuilding the table), so "archived" is not a fourth status value - it is
    this separate nullable timestamp, the same shape Store and HQUser already
    use for their own archive/restore lifecycle. A Device is archived by
    setting this column (and disabling it, if it was still active); restoring
    clears the column and leaves it disabled, never straight back to active -
    exactly the rule Store/User restore already follow.
    """
    with engine.begin() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(receiver_devices)")
        }
        if "archived_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE receiver_devices ADD COLUMN archived_at TEXT"
            )


def _device_row(row) -> dict:
    return {column: getattr(row, column) for column in _DEVICE_COLUMNS}


def list_devices(engine: Engine, *, store_id: int) -> list[dict]:
    _require_phase_one(engine)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                f"SELECT {', '.join(_DEVICE_COLUMNS)} FROM receiver_devices "
                "WHERE store_id = :store_id ORDER BY id"
            ),
            {"store_id": store_id},
        ).all()
    return [_device_row(row) for row in rows]


def read_device(engine: Engine, *, public_id: str) -> dict:
    _require_phase_one(engine)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT {', '.join(_DEVICE_COLUMNS)} FROM receiver_devices "
                "WHERE public_id = :public_id"
            ),
            {"public_id": public_id},
        ).one_or_none()
    if row is None:
        raise DeviceNotFound("no Receiver Device with that identifier")
    return _device_row(row)


def _set_status(engine: Engine, *, public_id: str, status: str) -> dict:
    from datetime import datetime, timezone

    _require_phase_one(engine)
    now = datetime.now(timezone.utc).isoformat()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        updated = connection.execute(
            text(
                "UPDATE receiver_devices SET status = :status, disabled_at = :now, "
                "updated_at = :now WHERE public_id = :public_id"
            ),
            {"status": status, "now": now, "public_id": public_id},
        ).rowcount

        # A Device that is switched off or retired must not stay its Store's
        # primary. This lives here rather than in the two endpoints so it cannot
        # be forgotten by a third caller. Nothing is promoted in its place: the
        # Store is left without a primary until an administrator chooses one,
        # because a silent failover puts the announcement on a computer nobody
        # has confirmed is plugged into the amplifier.
        has_primary_table = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='receiver_store_primary_device'"
        ).first()
        if has_primary_table:
            connection.execute(
                text(
                    "DELETE FROM receiver_store_primary_device WHERE device_id = "
                    "(SELECT id FROM receiver_devices WHERE public_id = :public_id)"
                ),
                {"public_id": public_id},
            )
        connection.commit()
    if not updated:
        raise DeviceNotFound("no Receiver Device with that identifier")
    return read_device(engine, public_id=public_id)


def disable_device(engine: Engine, *, public_id: str) -> dict:
    """Temporarily stop this Device. Reversible; the Store is untouched."""
    return _set_status(engine, public_id=public_id, status="disabled")


def revoke_device(engine: Engine, *, public_id: str) -> dict:
    """Retire this Device permanently. Its Store and every other Device of that
    Store keep working - that separation is the reason Devices exist."""
    return _set_status(engine, public_id=public_id, status="retired")


def archive_device(engine: Engine, *, public_id: str) -> dict:
    """Retire this Device from the active list. Reversible - its credential
    history and every enrolment/audit event stay exactly as they were.

    Mirrors Store/User archive: the Device is disabled (if it was still
    active) and ``archived_at`` is stamped, but nothing is deleted and its
    Store keeps every other Device working.
    """
    from datetime import datetime, timezone

    _require_phase_one(engine)
    now = datetime.now(timezone.utc).isoformat()
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        row = connection.execute(
            text("SELECT status FROM receiver_devices WHERE public_id = :public_id"),
            {"public_id": public_id},
        ).one_or_none()
        if row is None:
            raise DeviceNotFound("no Receiver Device with that identifier")
        if row.status == "active":
            connection.execute(
                text(
                    "UPDATE receiver_devices SET status = 'disabled', disabled_at = :now, "
                    "archived_at = :now, updated_at = :now WHERE public_id = :public_id"
                ),
                {"now": now, "public_id": public_id},
            )
        else:
            connection.execute(
                text(
                    "UPDATE receiver_devices SET archived_at = :now, updated_at = :now "
                    "WHERE public_id = :public_id"
                ),
                {"now": now, "public_id": public_id},
            )
        has_primary_table = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='receiver_store_primary_device'"
        ).first()
        if has_primary_table:
            connection.execute(
                text(
                    "DELETE FROM receiver_store_primary_device WHERE device_id = "
                    "(SELECT id FROM receiver_devices WHERE public_id = :public_id)"
                ),
                {"public_id": public_id},
            )
        connection.commit()
    return read_device(engine, public_id=public_id)


def restore_device(engine: Engine, *, public_id: str) -> dict:
    """Bring an archived Device back to DISABLED, never straight to active -
    the same rule Store/User restore already follow. An operator re-enables it
    deliberately, the same way they would any other disabled Device."""
    from datetime import datetime, timezone

    _require_phase_one(engine)
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        updated = connection.execute(
            text(
                "UPDATE receiver_devices SET archived_at = NULL, updated_at = :now "
                "WHERE public_id = :public_id"
            ),
            {"now": now, "public_id": public_id},
        ).rowcount
    if not updated:
        raise DeviceNotFound("no Receiver Device with that identifier")
    return read_device(engine, public_id=public_id)


__all__ = [
    "MAX_OUTSTANDING_CODES_PER_STORE",
    "DeviceNotFound",
    "EnrollmentApiError",
    "EnrollmentRefused",
    "EnrollmentUnavailable",
    "TooManyOutstandingCodes",
    "archive_device",
    "create_enrollment_code",
    "disable_device",
    "ensure_device_archive_schema",
    "list_devices",
    "read_device",
    "redeem_and_enroll",
    "restore_device",
    "revoke_device",
]
