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

    outstanding = (
        db.query(ReceiverEnrollmentCode)
        .filter(
            ReceiverEnrollmentCode.store_id == store_id,
            ReceiverEnrollmentCode.redeemed_at_epoch.is_(None),
        )
        .count()
    )
    if outstanding >= MAX_OUTSTANDING_CODES_PER_STORE:
        raise TooManyOutstandingCodes(
            f"this Store already has {outstanding} unused enrolment codes; "
            "use or let them expire before creating another"
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
        return enroll_receiver_device(
            engine,
            store_id=redeemed.store_id,
            display_name=device_name,
            actor_user_id=actor,
            hash_key=signing_key,
            hash_key_version=signing_version,
        )
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
    "enrolled_at", "disabled_at", "created_at", "updated_at",
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


__all__ = [
    "MAX_OUTSTANDING_CODES_PER_STORE",
    "DeviceNotFound",
    "EnrollmentApiError",
    "EnrollmentRefused",
    "EnrollmentUnavailable",
    "TooManyOutstandingCodes",
    "create_enrollment_code",
    "disable_device",
    "list_devices",
    "read_device",
    "redeem_and_enroll",
    "revoke_device",
]
