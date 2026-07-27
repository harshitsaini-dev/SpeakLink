"""One-time enrolment codes: how a Receiver computer earns its own credential.

Every Receiver for a Store currently shares the single raw token in
``stores.receiver_token``. Two computers in the same shop present the same
secret, so the backend cannot tell them apart, and revoking one revokes both.
There is also no way for a Receiver to *obtain* a credential - somebody copies
the Store token out of the UI and pastes it into an environment variable, which
puts a long-lived shared secret on a clipboard.

An enrolment code is the first half of the fix. An administrator creates one for
one Store, reads it out exactly once, and types it into one computer. It is
opaque, stored only as a verifier, expires in minutes, and can be redeemed
exactly once - so overhearing it later is worth nothing.

The second half is the device credential itself, which
``receiver_device_service.enroll_receiver_device`` already issues. This module
deliberately does not duplicate that work: it establishes *who may ask*, and
hands over.

Nothing here logs, prints or formats a code.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import ReceiverEnrollmentCode, Store


# Long enough that guessing is hopeless, short enough to read aloud in chunks.
CODE_BYTES = 24
# It is handed to one computer during setup, not mailed out. Minutes, not days.
CODE_TTL_SECONDS = 900


class EnrollmentCodeError(Exception):
    """Base class, so a handler cannot forget one of the failure modes."""


class EnrollmentCodeInvalid(EnrollmentCodeError):
    """Unknown, malformed or empty."""


class EnrollmentCodeExpired(EnrollmentCodeError):
    """Past its window."""


class EnrollmentCodeUsed(EnrollmentCodeError):
    """Already redeemed by some computer."""


class EnrollmentStoreUnavailable(EnrollmentCodeError):
    """The Store does not exist, or is no longer active."""


@dataclass(frozen=True)
class IssuedEnrollmentCode:
    """The one and only time the raw code exists outside the operator's hands.

    ``__repr__`` is written by hand: the default one would print the code into
    any traceback that happened to include this object.
    """

    code: str
    store_id: int
    expires_at_epoch: float

    def __repr__(self) -> str:  # pragma: no cover - exercised through tests
        return (
            f"IssuedEnrollmentCode(store_id={self.store_id}, "
            f"expires_at_epoch={self.expires_at_epoch}, code=<hidden>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class RedeemedEnrollmentCode:
    """What redemption proves: this caller may enrol one Device for this Store."""

    store_id: int
    code_id: int


def _verifier(code: str) -> str:
    """SHA-256 of the code.

    A plain digest is right here, unlike a password: the code is 24 bytes of
    urandom, so there is nothing to brute-force and no need to slow an
    attacker down. What matters is that the stored value cannot be replayed.
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _active_store(db: Session, store_id: int) -> Store:
    store = db.query(Store).filter(Store.id == store_id).first()
    if store is None:
        raise EnrollmentStoreUnavailable("that Store was not found")
    if not store.is_active:
        raise EnrollmentStoreUnavailable("that Store is not active")
    return store


def issue_enrollment_code(
    db: Session,
    *,
    store_id: int,
    actor_user_id: int,
    now: float | None = None,
    ttl_seconds: int = CODE_TTL_SECONDS,
) -> IssuedEnrollmentCode:
    """Mint one code for one Store. The raw value is returned exactly once."""
    moment = time.time() if now is None else _as_epoch(now)
    _active_store(db, store_id)

    code = secrets.token_urlsafe(CODE_BYTES)
    row = ReceiverEnrollmentCode(
        code_hash=_verifier(code),
        store_id=store_id,
        created_by=actor_user_id,
        expires_at_epoch=moment + ttl_seconds,
    )
    db.add(row)
    db.commit()
    return IssuedEnrollmentCode(
        code=code, store_id=store_id, expires_at_epoch=row.expires_at_epoch
    )


def redeem_enrollment_code(
    db: Session, code: str | None, *, now: float | None = None
) -> RedeemedEnrollmentCode:
    """Spend a code, or refuse. The supplied value never appears in the error.

    Redemption is a conditional UPDATE rather than a read-then-write, so two
    computers racing with the same code cannot both win: the database decides,
    and exactly one row transitions from unredeemed to redeemed.
    """
    moment = time.time() if now is None else _as_epoch(now)
    if not isinstance(code, str) or not code.strip():
        raise EnrollmentCodeInvalid("no enrolment code was presented")

    digest = _verifier(code.strip())
    row = (
        db.query(ReceiverEnrollmentCode)
        .filter(ReceiverEnrollmentCode.code_hash == digest)
        .first()
    )
    if row is None:
        raise EnrollmentCodeInvalid("that enrolment code is not recognised")
    if row.redeemed_at_epoch is not None:
        raise EnrollmentCodeUsed("that enrolment code has already been used")
    if moment >= row.expires_at_epoch:
        raise EnrollmentCodeExpired("that enrolment code has expired")

    _active_store(db, row.store_id)

    # The WHERE clause carries the condition, so the winner is whoever the
    # database lets through first. compare_and_swap, expressed in SQL.
    claimed = (
        db.query(ReceiverEnrollmentCode)
        .filter(
            ReceiverEnrollmentCode.id == row.id,
            ReceiverEnrollmentCode.redeemed_at_epoch.is_(None),
        )
        .update({ReceiverEnrollmentCode.redeemed_at_epoch: moment}, synchronize_session=False)
    )
    try:
        db.commit()
    except IntegrityError:  # pragma: no cover - defensive
        db.rollback()
        raise EnrollmentCodeUsed("that enrolment code has already been used") from None

    if claimed != 1:
        raise EnrollmentCodeUsed("that enrolment code has already been used")

    return RedeemedEnrollmentCode(store_id=row.store_id, code_id=row.id)


def _as_epoch(value) -> float:
    """Accept epoch seconds or an aware datetime, so tests can inject a clock."""
    if hasattr(value, "timestamp"):
        return value.timestamp()
    return float(value)


__all__ = [
    "CODE_TTL_SECONDS",
    "EnrollmentCodeError",
    "EnrollmentCodeExpired",
    "EnrollmentCodeInvalid",
    "EnrollmentCodeUsed",
    "EnrollmentStoreUnavailable",
    "IssuedEnrollmentCode",
    "RedeemedEnrollmentCode",
    "issue_enrollment_code",
    "redeem_enrollment_code",
]
