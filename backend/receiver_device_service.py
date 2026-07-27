"""Isolated Receiver Device enrollment service for credential migration Phase 2.

The service uses only an explicitly supplied SQLite engine. It is deliberately
not integrated with FastAPI, application startup, WebSockets, or Base metadata.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import json
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from migrations import MIGRATION_STATE_LEGACY_ONLY, PHASE_ONE_NAME, PHASE_ONE_VERSION
from receiver_credentials import (
    MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE,
    MIN_HASH_KEY_BYTES,
    generate_receiver_credential,
    hash_receiver_token,
    sanitize_audit_payload,
)


DEVICE_DISPLAY_NAME_MAX_LENGTH = 200

#: The migration states in which enrolling a Device is safe.
#:
#: This used to be ``legacy_only`` alone, which was right for a rehearsal and
#: wrong for production. ``receiver_auth_service`` verifies hashed Device
#: credentials only in ``dual_verify``, ``hash_only`` and ``raw_neutralized``, so
#: the two sets were disjoint: a server that had cut over could never enrol a new
#: till, because the Device would be created and could never connect.
#:
#: ``legacy_only`` is kept so a Device enrolled during the rehearsal window is
#: still usable after cutover rather than being born dead.
#:
#: ``backfilled`` is excluded deliberately. Hashed credentials are not verified
#: there either, so enrolling into it would hand an operator a credential that
#: silently cannot authenticate - the same trap, one state along.
#:
#: Kept in sync with ``receiver_auth_service._HASH_STATES`` by a test rather than
#: by hand, so a new state cannot be added without a decision about this one.
ENROLLABLE_STATES = frozenset(
    {MIGRATION_STATE_LEGACY_ONLY, "dual_verify", "hash_only", "raw_neutralized"}
)

INITIAL_CREDENTIAL_VERSION = 1
INITIAL_TOKEN_FORMAT = "echocast_rcv"
INITIAL_ENROLLMENT_REASON = "initial_enrollment"
_FORBIDDEN_DISPLAY_FRAGMENTS = (
    "authorization",
    "bearer ",
    "echocast_rcv_",
    "password",
)

_REQUIRED_COLUMNS = {
    "stores": {"id", "receiver_token", "is_active", "status", "last_seen"},
    "hq_users": {"id", "is_active"},
    "receiver_devices": {
        "id", "public_id", "store_id", "display_name", "status",
        "enrolled_at", "disabled_at", "created_by", "created_at", "updated_at",
    },
    "receiver_credentials": {
        "id", "public_id", "device_id", "credential_version", "token_format",
        "token_hash", "hash_key_version", "status", "expiry_policy", "issued_at",
        "expires_at", "revoked_at", "replaced_at", "accept_until", "last_used_at",
        "created_by", "replaces_credential_id", "created_at",
    },
    "receiver_credential_events": {
        "id", "public_id", "event_type", "outcome", "store_id", "device_id",
        "credential_id", "actor_user_id", "event_at", "reason_code",
        "correlation_id", "metadata_json",
    },
    "receiver_credential_migration_state": {
        "id", "schema_version", "state", "legacy_verification_enabled", "updated_at",
    },
    "schema_migrations": {"version", "name", "applied_at"},
}
_REQUIRED_INDEXES = {
    "ix_receiver_devices_store_status",
    "ix_receiver_credentials_auth_lookup",
    "ix_receiver_credentials_device_status",
    "ix_receiver_credential_events_device_time",
    "ix_receiver_credential_events_credential_time",
    "ix_receiver_credential_events_store_time",
}
_REQUIRED_CONSTRAINT_MARKERS = {
    "receiver_devices": (
        "fk_receiver_devices_store",
        "ck_receiver_devices_disabled_state",
        "ck_receiver_devices_utc",
    ),
    "receiver_credentials": (
        "uq_receiver_credentials_device_version",
        "fk_receiver_credentials_device",
        "ck_receiver_credentials_expiry",
        "ck_receiver_credentials_utc",
    ),
    "receiver_credential_events": (
        "fk_receiver_credential_events_store",
        "fk_receiver_credential_events_device",
        "fk_receiver_credential_events_credential",
        "ck_receiver_credential_events_utc",
    ),
}


class ReceiverDeviceServiceError(RuntimeError):
    """Base class for secret-free enrollment failures."""


class MigrationNotReadyError(ReceiverDeviceServiceError):
    """Raised when Phase 1 schema or legacy-only state is not trustworthy."""


class StoreNotFoundError(ReceiverDeviceServiceError):
    """Raised when the requested Store does not exist."""


class InactiveStoreError(ReceiverDeviceServiceError):
    """Raised when the requested Store is inactive."""


class ActorNotFoundError(ReceiverDeviceServiceError):
    """Raised when the recorded HQ actor does not exist."""


class InactiveActorError(ReceiverDeviceServiceError):
    """Raised when the recorded HQ actor is inactive."""


class DeviceLimitExceededError(ReceiverDeviceServiceError):
    """Raised when enrollment would exceed two active devices for a Store."""


class InvalidEnrollmentRequestError(ReceiverDeviceServiceError):
    """Raised when bounded, typed enrollment input validation fails."""


class EnrollmentPersistenceError(ReceiverDeviceServiceError):
    """Raised after an enrollment transaction is rolled back."""


class CredentialAlreadyDeliveredError(ReceiverDeviceServiceError):
    """Raised when a caller tries to retrieve one-time credential material again."""


class ReceiverEnrollmentResult:
    """Enrollment identifiers and one-time credential delivery container."""

    __slots__ = (
        "device_public_id",
        "credential_public_id",
        "credential_version",
        "_raw_credential",
    )

    def __init__(
        self,
        *,
        device_public_id: str,
        credential_public_id: str,
        credential_version: int,
        raw_credential: str,
    ) -> None:
        self.device_public_id = device_public_id
        self.credential_public_id = credential_public_id
        self.credential_version = credential_version
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
            "ReceiverEnrollmentResult("
            f"device_public_id={self.device_public_id!r}, "
            f"credential_public_id={self.credential_public_id!r}, "
            f"credential_version={self.credential_version}, "
            "raw_credential=<redacted>)"
        )

    __str__ = __repr__


def _validate_positive_identifier(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InvalidEnrollmentRequestError(f"{field_name} must be a positive integer")
    return value


def _require_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise InvalidEnrollmentRequestError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise InvalidEnrollmentRequestError(f"{field_name} must be timezone-aware UTC")
    return value


def _validate_request(
    *,
    store_id: object,
    display_name: object,
    actor_user_id: object,
    hash_key: object,
    hash_key_version: object,
    expires_at: datetime | None,
    now: datetime | None,
) -> tuple[int, str, int, bytes, int, datetime | None, datetime]:
    validated_store_id = _validate_positive_identifier(store_id, "store_id")
    validated_actor_id = _validate_positive_identifier(actor_user_id, "actor_user_id")
    validated_key_version = _validate_positive_identifier(
        hash_key_version, "hash_key_version"
    )
    if not isinstance(display_name, str):
        raise InvalidEnrollmentRequestError("display_name must be text")
    normalized_name = display_name.strip()
    if (
        not normalized_name
        or len(normalized_name) > DEVICE_DISPLAY_NAME_MAX_LENGTH
        or not normalized_name.isprintable()
    ):
        raise InvalidEnrollmentRequestError("display_name is invalid")
    if not isinstance(hash_key, bytes) or len(hash_key) < MIN_HASH_KEY_BYTES:
        raise InvalidEnrollmentRequestError("hash_key does not meet strength requirements")
    lowered_name = normalized_name.lower()
    if any(fragment in lowered_name for fragment in _FORBIDDEN_DISPLAY_FRAGMENTS):
        raise InvalidEnrollmentRequestError("display_name is invalid")
    try:
        decoded_hash_key = hash_key.decode("utf-8")
    except UnicodeDecodeError:
        decoded_hash_key = ""
    if decoded_hash_key and decoded_hash_key in normalized_name:
        raise InvalidEnrollmentRequestError("display_name is invalid")

    validated_now = _require_utc(now or datetime.now(timezone.utc), "now")
    validated_expiry = None
    if expires_at is not None:
        validated_expiry = _require_utc(expires_at, "expires_at")
        if validated_expiry <= validated_now:
            raise InvalidEnrollmentRequestError("expires_at must be later than now")
    return (
        validated_store_id,
        normalized_name,
        validated_actor_id,
        hash_key,
        validated_key_version,
        validated_expiry,
        validated_now,
    )


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
    }


def _validate_phase_one(connection: Connection) -> None:
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not set(_REQUIRED_COLUMNS) <= tables:
        raise MigrationNotReadyError("receiver credential Phase 1 schema is not ready")
    for table_name, required_columns in _REQUIRED_COLUMNS.items():
        if not required_columns <= _table_columns(connection, table_name):
            raise MigrationNotReadyError("receiver credential Phase 1 schema is inconsistent")

    indexes = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    if not _REQUIRED_INDEXES <= indexes:
        raise MigrationNotReadyError("receiver credential Phase 1 indexes are inconsistent")

    for table_name, markers in _REQUIRED_CONSTRAINT_MARKERS.items():
        table_sql = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": table_name},
        ).scalar_one_or_none()
        if not table_sql or any(marker not in table_sql for marker in markers):
            raise MigrationNotReadyError("receiver credential Phase 1 constraints are inconsistent")

    migration = connection.execute(
        text("SELECT name FROM schema_migrations WHERE version = :version"),
        {"version": PHASE_ONE_VERSION},
    ).scalar_one_or_none()
    state = connection.execute(
        text(
            "SELECT schema_version, state FROM receiver_credential_migration_state "
            "WHERE id = 1"
        )
    ).one_or_none()
    if migration != PHASE_ONE_NAME or state is None:
        raise MigrationNotReadyError("receiver credential Phase 1 migration is not recorded")
    schema_version, migration_state = state
    if schema_version != PHASE_ONE_VERSION:
        raise MigrationNotReadyError("receiver credential Phase 1 schema version is unexpected")
    if migration_state not in ENROLLABLE_STATES:
        raise MigrationNotReadyError(
            f"Receiver Devices cannot be enrolled while the migration state is "
            f"{migration_state!r}; a credential issued now could not be verified"
        )


def _audit_metadata(
    *,
    store_id: int,
    actor_user_id: int,
    device_public_id: str,
    credential_public_id: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "store_id": store_id,
        "actor_user_id": actor_user_id,
        "device_public_id": device_public_id,
        "outcome": "success",
    }
    if credential_public_id is not None:
        payload["credential_public_id"] = credential_public_id
        payload["credential_version"] = INITIAL_CREDENTIAL_VERSION
    sanitized = sanitize_audit_payload(payload)
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _notify(step_hook: Callable[[str], None] | None, step: str) -> None:
    if step_hook is not None:
        step_hook(step)


def enroll_receiver_device(
    engine: Engine,
    *,
    store_id: int,
    display_name: str,
    actor_user_id: int,
    hash_key: bytes,
    hash_key_version: int,
    expires_at: datetime | None = None,
    now: datetime | None = None,
    step_hook: Callable[[str], None] | None = None,
) -> ReceiverEnrollmentResult:
    """Enroll one active Receiver Device and return its credential once."""
    if engine.dialect.name != "sqlite":
        raise InvalidEnrollmentRequestError("receiver enrollment currently supports SQLite only")
    (
        validated_store_id,
        normalized_name,
        validated_actor_id,
        validated_hash_key,
        validated_key_version,
        validated_expiry,
        validated_now,
    ) = _validate_request(
        store_id=store_id,
        display_name=display_name,
        actor_user_id=actor_user_id,
        hash_key=hash_key,
        hash_key_version=hash_key_version,
        expires_at=expires_at,
        now=now,
    )

    timestamp = validated_now.isoformat()
    expiry_policy = "expires_at" if validated_expiry is not None else "non_expiring"
    expiry_text = validated_expiry.isoformat() if validated_expiry is not None else None

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise MigrationNotReadyError("SQLite foreign key enforcement is unavailable")
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _validate_phase_one(connection)

            store = connection.execute(
                text("SELECT is_active FROM stores WHERE id = :store_id"),
                {"store_id": validated_store_id},
            ).one_or_none()
            if store is None:
                raise StoreNotFoundError("Store was not found")
            if not bool(store.is_active):
                raise InactiveStoreError("Store is inactive")

            actor = connection.execute(
                text("SELECT is_active FROM hq_users WHERE id = :actor_user_id"),
                {"actor_user_id": validated_actor_id},
            ).one_or_none()
            if actor is None:
                raise ActorNotFoundError("HQ actor was not found")
            if not bool(actor.is_active):
                raise InactiveActorError("HQ actor is inactive")

            active_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM receiver_devices "
                    "WHERE store_id = :store_id AND status = 'active'"
                ),
                {"store_id": validated_store_id},
            ).scalar_one()
            if active_count >= MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE:
                raise DeviceLimitExceededError("Store active Receiver Device limit reached")

            device_public_id = str(uuid.uuid4())
            issued = generate_receiver_credential(INITIAL_CREDENTIAL_VERSION)
            token_hash = hash_receiver_token(
                issued.raw_token,
                validated_hash_key,
                key_version=validated_key_version,
            )

            device_result = connection.execute(
                text(
                    """
                    INSERT INTO receiver_devices (
                        public_id, store_id, display_name, status, enrolled_at,
                        disabled_at, created_by, created_at, updated_at
                    ) VALUES (
                        :public_id, :store_id, :display_name, 'active', :now,
                        NULL, :actor_user_id, :now, :now
                    )
                    """
                ),
                {
                    "public_id": device_public_id,
                    "store_id": validated_store_id,
                    "display_name": normalized_name,
                    "actor_user_id": validated_actor_id,
                    "now": timestamp,
                },
            )
            device_id = device_result.lastrowid
            _notify(step_hook, "after_device_insert")

            credential_result = connection.execute(
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
                        :token_hash, :hash_key_version, 'active', :expiry_policy,
                        :issued_at, :expires_at, NULL, NULL, NULL, NULL,
                        :actor_user_id, NULL, :created_at
                    )
                    """
                ),
                {
                    "public_id": issued.public_id,
                    "device_id": device_id,
                    "credential_version": INITIAL_CREDENTIAL_VERSION,
                    "token_format": INITIAL_TOKEN_FORMAT,
                    "token_hash": token_hash,
                    "hash_key_version": validated_key_version,
                    "expiry_policy": expiry_policy,
                    "issued_at": timestamp,
                    "expires_at": expiry_text,
                    "actor_user_id": validated_actor_id,
                    "created_at": timestamp,
                },
            )
            credential_id = credential_result.lastrowid
            _notify(step_hook, "after_credential_insert")

            audit_parameters = (
                {
                    "public_id": str(uuid.uuid4()),
                    "event_type": "device_enrolled",
                    "credential_id": None,
                    "metadata_json": _audit_metadata(
                        store_id=validated_store_id,
                        actor_user_id=validated_actor_id,
                        device_public_id=device_public_id,
                    ),
                },
                {
                    "public_id": str(uuid.uuid4()),
                    "event_type": "credential_issued",
                    "credential_id": credential_id,
                    "metadata_json": _audit_metadata(
                        store_id=validated_store_id,
                        actor_user_id=validated_actor_id,
                        device_public_id=device_public_id,
                        credential_public_id=issued.public_id,
                    ),
                },
            )
            for parameters in audit_parameters:
                connection.execute(
                    text(
                        """
                        INSERT INTO receiver_credential_events (
                            public_id, event_type, outcome, store_id, device_id,
                            credential_id, actor_user_id, event_at, reason_code,
                            correlation_id, metadata_json
                        ) VALUES (
                            :public_id, :event_type, 'success', :store_id, :device_id,
                            :credential_id, :actor_user_id, :event_at,
                            :reason_code, NULL, :metadata_json
                        )
                        """
                    ),
                    {
                        **parameters,
                        "store_id": validated_store_id,
                        "device_id": device_id,
                        "actor_user_id": validated_actor_id,
                        "event_at": timestamp,
                        "reason_code": INITIAL_ENROLLMENT_REASON,
                    },
                )
            _notify(step_hook, "after_audit_inserts")
            connection.commit()
        except ReceiverDeviceServiceError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise EnrollmentPersistenceError(
                "receiver enrollment could not be persisted"
            ) from None

    return ReceiverEnrollmentResult(
        device_public_id=device_public_id,
        credential_public_id=issued.public_id,
        credential_version=INITIAL_CREDENTIAL_VERSION,
        raw_credential=issued.raw_token,
    )
