"""Replacing one Device's credential without taking its Store off the air.

Rotation is what you reach for when something has gone wrong: a laptop was
stolen, a technician left, a credential was pasted into a chat window. That
shapes every decision in this module.

**There is no overlap.** The old credential stops working at the instant the new
one is issued. ``plan_rotation`` supports a bounded grace window and it is used
here with a grace of zero, deliberately: the reason you are rotating is usually
that somebody else may hold a copy, and a fifteen-minute window is fifteen more
minutes of that copy working. The Device is then offline until an operator
carries the new credential to it. That is honest; the alternative is a system
that reports "rotated" while the old secret still opens the door.

**The blast radius is one Device.** The Store stays active, its other Device
keeps working, and the shared legacy Store token is not touched. If rotating one
till took a Store off the air, nobody would ever rotate anything.

**Failure must not destroy the working credential.** A rotation that dies halfway
and leaves a Device with nothing usable is worse than never rotating: that
computer can neither authenticate nor re-enrol, and recovering it means somebody
drives to the Store. Everything below happens inside one ``BEGIN IMMEDIATE``
transaction, and the tests kill it at each step to prove the old credential still
authenticates afterwards.

Like ``receiver_device_service``, this takes an explicit Engine and has no
FastAPI, no ORM session and no application state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine

from receiver_credentials import (
    MIN_HASH_KEY_BYTES,
    generate_receiver_credential,
    hash_receiver_token,
    plan_rotation,
    sanitize_audit_payload,
)
from migrations import PHASE_ONE_NAME, PHASE_ONE_VERSION
from receiver_device_service import (
    CredentialAlreadyDeliveredError,
    InactiveActorError,
    InactiveStoreError,
    MigrationNotReadyError,
    ReceiverDeviceServiceError,
    StoreNotFoundError,
    _require_utc,
    _validate_phase_one,
    _validate_positive_identifier,
)


ROTATION_TOKEN_FORMAT = "speaklink_rcv"
ROTATION_REASON = "credential_rotation"

#: The states in which a rotated credential can actually be used.
#:
#: ``receiver_device_service._validate_phase_one`` pins enrolment to
#: ``legacy_only`` because it was written for the migration rehearsal. Rotation
#: cannot inherit that rule: ``receiver_auth_service`` only verifies hashed Device
#: credentials in ``dual_verify``, ``hash_only`` and ``raw_neutralized``, so a
#: service that rotated only in ``legacy_only`` would issue credentials in exactly
#: the state where they do not work, and refuse in every state where they do.
#:
#: ``legacy_only`` is kept so a rotation performed during the rehearsal window is
#: still valid once the state advances. ``backfilled`` is excluded deliberately:
#: hashed credentials are not verified there either, so rotating into it would
#: hand an operator a credential that silently cannot authenticate.
ROTATABLE_STATES = frozenset({"legacy_only", "dual_verify", "hash_only", "raw_neutralized"})

#: Zero, and not configurable. See the module docstring: an overlap window during
#: a rotation is a window in which a suspected-compromised credential still works.
ROTATION_GRACE = timedelta(0)


class DeviceNotRotatableError(ReceiverDeviceServiceError):
    """No such Device, or it is not in a state where a new credential is sane.

    One class for "unknown", "disabled" and "retired" on purpose. Handing a fresh
    credential to a Device an administrator just switched off would quietly undo
    the switching off, and that is the same mistake in all three cases.
    """


class CredentialNotFoundError(ReceiverDeviceServiceError):
    """The Device has no active credential to replace."""


class RotationPersistenceError(ReceiverDeviceServiceError):
    """The rotation was rolled back. The previous credential is untouched."""


class ReceiverRotationResult:
    """Rotation identifiers and one-time credential delivery.

    Same shape as ``ReceiverEnrollmentResult``: the raw value can be taken once,
    and ``__repr__`` never renders it, because this object ends up in whatever
    traceback or log line a support ticket gets pasted into.
    """

    __slots__ = (
        "device_public_id",
        "credential_public_id",
        "credential_version",
        "previous_credential_public_id",
        "_raw_credential",
    )

    def __init__(
        self,
        *,
        device_public_id: str,
        credential_public_id: str,
        credential_version: int,
        previous_credential_public_id: str,
        raw_credential: str,
    ) -> None:
        self.device_public_id = device_public_id
        self.credential_public_id = credential_public_id
        self.credential_version = credential_version
        self.previous_credential_public_id = previous_credential_public_id
        self._raw_credential = raw_credential

    def take_raw_credential(self) -> str:
        raw_credential = self._raw_credential
        if raw_credential is None:
            raise CredentialAlreadyDeliveredError(
                "receiver credential has already been delivered"
            )
        self._raw_credential = None
        return raw_credential

    def __repr__(self) -> str:
        return (
            "ReceiverRotationResult("
            f"device_public_id={self.device_public_id!r}, "
            f"credential_public_id={self.credential_public_id!r}, "
            f"credential_version={self.credential_version}, "
            f"previous_credential_public_id={self.previous_credential_public_id!r}, "
            "raw_credential=<redacted>)"
        )

    __str__ = __repr__


def _notify(step_hook: Callable[[str], None] | None, step: str) -> None:
    if step_hook is not None:
        step_hook(step)


def _require_rotatable_state(connection) -> str:
    """Same schema checks as enrolment, but its own state rule.

    ``_validate_phase_one`` is reused for everything structural - tables, columns,
    indexes, constraints, the migration ledger - so the two services cannot drift
    on what "phase one is applied" means. Only the *state* rule differs, and it
    differs for the reason set out at ROTATABLE_STATES.
    """
    try:
        _validate_phase_one(connection)
    except MigrationNotReadyError:
        # Either the schema is genuinely missing, or the state is simply not
        # legacy_only - which is fine for a rotation. Tell them apart.
        ledger = connection.execute(
            text("SELECT name FROM schema_migrations WHERE version = :version"),
            {"version": PHASE_ONE_VERSION},
        ).scalar_one_or_none()
        if ledger != PHASE_ONE_NAME:
            raise
        state = connection.execute(
            text("SELECT state FROM receiver_credential_migration_state WHERE id = 1")
        ).scalar_one_or_none()
        if state not in ROTATABLE_STATES:
            raise MigrationNotReadyError(
                f"Receiver credentials cannot be rotated while the migration state "
                f"is {state!r}; a credential issued now could not be verified"
            ) from None
        return str(state)
    return "legacy_only"


def _audit_metadata(
    *,
    store_id: int,
    actor_user_id: int,
    device_public_id: str,
    credential_public_id: str,
    credential_version: int,
    replacement_credential_public_id: str,
) -> str:
    return json.dumps(
        sanitize_audit_payload(
            {
                "store_id": store_id,
                "actor_user_id": actor_user_id,
                "device_public_id": device_public_id,
                "credential_public_id": credential_public_id,
                "credential_version": credential_version,
                "replacement_credential_public_id": replacement_credential_public_id,
                "reason": ROTATION_REASON,
                "outcome": "success",
            }
        ),
        sort_keys=True,
        separators=(",", ":"),
    )


def rotate_receiver_device_credential(
    engine: Engine,
    *,
    device_public_id: str,
    actor_user_id: int,
    hash_key: bytes,
    hash_key_version: int,
    now: datetime | None = None,
    step_hook: Callable[[str], None] | None = None,
) -> ReceiverRotationResult:
    """Issue one new credential for one Device and kill the old one immediately."""
    if engine.dialect.name != "sqlite":
        raise ReceiverDeviceServiceError("receiver rotation currently supports SQLite only")
    if not isinstance(device_public_id, str) or not device_public_id.strip():
        raise DeviceNotRotatableError("a Receiver Device identifier is required")
    validated_actor_id = _validate_positive_identifier(actor_user_id, "actor_user_id")
    validated_key_version = _validate_positive_identifier(hash_key_version, "hash_key_version")
    if not isinstance(hash_key, bytes) or len(hash_key) < MIN_HASH_KEY_BYTES:
        raise ReceiverDeviceServiceError(
            f"receiver hash key must contain at least {MIN_HASH_KEY_BYTES} bytes"
        )
    from datetime import timezone

    validated_now = _require_utc(now or datetime.now(timezone.utc), "now")
    timestamp = validated_now.isoformat()
    public_id = device_public_id.strip()

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise ReceiverDeviceServiceError("SQLite foreign key enforcement is unavailable")
        connection.commit()
        # IMMEDIATE, so two administrators clicking Rotate in the same second are
        # serialised by SQLite rather than racing to write two live credentials.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _require_rotatable_state(connection)

            device = connection.execute(
                text(
                    "SELECT d.id, d.store_id, d.status, s.is_active "
                    "FROM receiver_devices d JOIN stores s ON s.id = d.store_id "
                    "WHERE d.public_id = :public_id"
                ),
                {"public_id": public_id},
            ).one_or_none()
            if device is None:
                raise DeviceNotRotatableError("no Receiver Device with that identifier")
            if device.status != "active":
                raise DeviceNotRotatableError(
                    "only an active Receiver Device can have its credential rotated"
                )
            if not bool(device.is_active):
                raise InactiveStoreError("Store is inactive")

            actor = connection.execute(
                text("SELECT is_active FROM hq_users WHERE id = :actor_user_id"),
                {"actor_user_id": validated_actor_id},
            ).one_or_none()
            if actor is None:
                raise StoreNotFoundError("HQ actor was not found")
            if not bool(actor.is_active):
                raise InactiveActorError("HQ actor is inactive")

            current = connection.execute(
                text(
                    "SELECT id, public_id, credential_version FROM receiver_credentials "
                    "WHERE device_id = :device_id AND status = 'active' "
                    "ORDER BY credential_version DESC"
                ),
                {"device_id": device.id},
            ).all()
            if not current:
                raise CredentialNotFoundError(
                    "this Receiver Device has no active credential to replace"
                )
            previous = current[0]

            plan = plan_rotation(previous.credential_version, validated_now, ROTATION_GRACE)
            issued = generate_receiver_credential(plan.new_version)
            token_hash = hash_receiver_token(
                issued.raw_token, hash_key, key_version=validated_key_version
            )

            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credentials (
                        public_id, device_id, credential_version, token_format,
                        token_hash, hash_key_version, status, expiry_policy,
                        issued_at, expires_at, revoked_at, replaced_at,
                        accept_until, last_used_at, created_by,
                        replaces_credential_id, created_at
                    ) VALUES (
                        :public_id, :device_id, :credential_version, :token_format,
                        :token_hash, :hash_key_version, 'active', 'non_expiring',
                        :issued_at, NULL, NULL, NULL, NULL, NULL,
                        :actor_user_id, :replaces_credential_id, :created_at
                    )
                    """
                ),
                {
                    "public_id": issued.public_id,
                    "device_id": device.id,
                    "credential_version": plan.new_version,
                    "token_format": ROTATION_TOKEN_FORMAT,
                    "token_hash": token_hash,
                    "hash_key_version": validated_key_version,
                    "issued_at": timestamp,
                    "actor_user_id": validated_actor_id,
                    "replaces_credential_id": previous.id,
                    "created_at": timestamp,
                },
            )
            _notify(step_hook, "after_credential_insert")

            # accept_until equals the rotation moment, and usability is
            # `now >= accept_until`, so the old credential is dead at that instant
            # rather than one tick later.
            superseded = connection.execute(
                text(
                    "UPDATE receiver_credentials SET status = 'superseded', "
                    "replaced_at = :now, accept_until = :accept_until "
                    "WHERE device_id = :device_id AND status = 'active' "
                    "AND id != (SELECT id FROM receiver_credentials "
                    "           WHERE public_id = :new_public_id)"
                ),
                {
                    "now": timestamp,
                    "accept_until": plan.previous_accept_until.isoformat(),
                    "device_id": device.id,
                    "new_public_id": issued.public_id,
                },
            ).rowcount
            if superseded < 1:
                raise RotationPersistenceError(
                    "the previous credential could not be retired, so the rotation "
                    "was abandoned rather than leaving two live credentials"
                )
            _notify(step_hook, "after_supersede")

            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credential_events (
                        public_id, event_type, outcome, store_id, device_id,
                        credential_id, actor_user_id, event_at, reason_code,
                        correlation_id, metadata_json
                    ) VALUES (
                        :public_id, 'credential_rotated', 'success', :store_id, :device_id,
                        :credential_id, :actor_user_id, :event_at, :reason_code,
                        NULL, :metadata_json
                    )
                    """
                ),
                {
                    "public_id": str(uuid.uuid4()),
                    "store_id": device.store_id,
                    "device_id": device.id,
                    "credential_id": previous.id,
                    "actor_user_id": validated_actor_id,
                    "event_at": timestamp,
                    "reason_code": ROTATION_REASON,
                    "metadata_json": _audit_metadata(
                        store_id=device.store_id,
                        actor_user_id=validated_actor_id,
                        device_public_id=public_id,
                        credential_public_id=previous.public_id,
                        credential_version=plan.new_version,
                        replacement_credential_public_id=issued.public_id,
                    ),
                },
            )
            _notify(step_hook, "after_audit_insert")
            connection.commit()
        except ReceiverDeviceServiceError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise RotationPersistenceError(
                "the Receiver credential rotation was rolled back; the previous "
                "credential is unchanged and still works"
            ) from None

    return ReceiverRotationResult(
        device_public_id=public_id,
        credential_public_id=issued.public_id,
        credential_version=plan.new_version,
        previous_credential_public_id=previous.public_id,
        raw_credential=issued.raw_token,
    )


__all__ = [
    "ROTATION_GRACE",
    "CredentialAlreadyDeliveredError",
    "CredentialNotFoundError",
    "DeviceNotRotatableError",
    "ReceiverRotationResult",
    "RotationPersistenceError",
    "rotate_receiver_device_credential",
]
