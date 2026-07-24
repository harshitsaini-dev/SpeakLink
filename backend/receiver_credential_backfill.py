"""Isolated legacy Receiver Credential fleet backfill rehearsal.

This module is not integrated with application startup, FastAPI, WebSockets,
or the default database engine. Callers must inject a SQLite Engine.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from migrations import (
    MIGRATION_STATE_LEGACY_ONLY,
    PHASE_ONE_NAME,
    PHASE_ONE_VERSION,
    PROTECTED_DATABASE_PATH,
)
from receiver_credentials import (
    MIN_HASH_KEY_BYTES,
    InvalidCredentialError,
    hash_legacy_receiver_token,
    sanitize_audit_payload,
    verify_legacy_receiver_token,
)


BACKFILLED_STATE = "backfilled"
LEGACY_TOKEN_FORMAT = "legacy_uuid_hex"
BACKFILL_REASON_CODE = "legacy_backfill_rehearsal"
BACKFILL_MIGRATION_PHASE = "backfilled"

_REQUIRED_COLUMNS = {
    "stores": {
        "id", "receiver_token", "is_active", "status", "last_seen", "is_online_store",
    },
    "receiver_devices": {
        "id", "public_id", "store_id", "display_name", "status", "enrolled_at",
        "disabled_at", "created_by", "created_at", "updated_at",
    },
    "receiver_credentials": {
        "id", "public_id", "device_id", "credential_version", "token_format",
        "token_hash", "hash_key_version", "status", "expiry_policy", "expires_at",
        "issued_at", "created_by", "created_at",
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
    "ix_receiver_credential_events_store_time",
}


class ReceiverCredentialBackfillError(RuntimeError):
    """Base class for secret-free rehearsal failures."""


class ProtectedDatabaseError(ReceiverCredentialBackfillError):
    """Raised before connection when the protected live database is supplied."""


class BackfillMigrationNotReadyError(ReceiverCredentialBackfillError):
    """Raised when the Phase 1 schema or state cannot authorize rehearsal."""


class InvalidLegacyCredentialError(ReceiverCredentialBackfillError):
    """Raised without identifying which Store credential was invalid."""


class BackfillConflictError(ReceiverCredentialBackfillError):
    """Raised when existing rows make a complete fleet rehearsal ambiguous."""


class BackfillAlreadyAppliedError(ReceiverCredentialBackfillError):
    """Raised on a validated no-write replay after successful rehearsal."""


class BackfillValidationError(ReceiverCredentialBackfillError):
    """Raised when fleet counts, relationships, or foreign keys are invalid."""


class BackfillPersistenceError(ReceiverCredentialBackfillError):
    """Raised after a persistence failure rolls back the whole rehearsal."""


class BackfillResult:
    """Non-secret summary of a completed isolated rehearsal."""

    __slots__ = (
        "total_store_count",
        "active_store_count",
        "inactive_store_count",
        "device_count",
        "credential_count",
        "audit_event_count",
        "final_state",
        "store_ids",
    )

    def __init__(
        self,
        *,
        total_store_count: int,
        active_store_count: int,
        inactive_store_count: int,
        device_count: int,
        credential_count: int,
        audit_event_count: int,
        final_state: str,
        store_ids: tuple[int, ...],
    ) -> None:
        self.total_store_count = total_store_count
        self.active_store_count = active_store_count
        self.inactive_store_count = inactive_store_count
        self.device_count = device_count
        self.credential_count = credential_count
        self.audit_event_count = audit_event_count
        self.final_state = final_state
        self.store_ids = store_ids

    def __repr__(self) -> str:
        return (
            "BackfillResult("
            f"total_store_count={self.total_store_count}, "
            f"active_store_count={self.active_store_count}, "
            f"inactive_store_count={self.inactive_store_count}, "
            f"device_count={self.device_count}, "
            f"credential_count={self.credential_count}, "
            f"audit_event_count={self.audit_event_count}, "
            f"final_state={self.final_state!r}, store_ids=<redacted>)"
        )

    __str__ = __repr__


@dataclass(slots=True, repr=False)
class _StorePlan:
    store_id: int
    raw_token: str
    is_active: bool
    device_public_id: str
    credential_public_id: str
    token_hash: str
    device_id: int | None = None
    credential_id: int | None = None


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _reject_protected_database(engine: Engine) -> None:
    if _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
        raise ProtectedDatabaseError("the protected EchoCast database cannot be rehearsed")


def _validate_inputs(
    engine: Engine,
    hash_key: object,
    hash_key_version: object,
    now: datetime | None,
) -> tuple[bytes, int, datetime]:
    if engine.dialect.name != "sqlite":
        raise BackfillValidationError("legacy receiver backfill rehearsal requires SQLite")
    _reject_protected_database(engine)
    if not isinstance(hash_key, bytes) or len(hash_key) < MIN_HASH_KEY_BYTES:
        raise BackfillValidationError("hash key does not meet strength requirements")
    if (
        isinstance(hash_key_version, bool)
        or not isinstance(hash_key_version, int)
        or hash_key_version <= 0
    ):
        raise BackfillValidationError("hash key version must be a positive integer")
    validated_now = now or datetime.now(timezone.utc)
    if (
        not isinstance(validated_now, datetime)
        or validated_now.tzinfo is None
        or validated_now.utcoffset() != timedelta(0)
    ):
        raise BackfillValidationError("now must be timezone-aware UTC")
    return hash_key, hash_key_version, validated_now


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.exec_driver_sql(f'PRAGMA table_info("{table_name}")')
    }


def _validate_schema(connection: Connection) -> None:
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not set(_REQUIRED_COLUMNS) <= tables:
        raise BackfillMigrationNotReadyError("receiver credential Phase 1 schema is missing")
    for table_name, columns in _REQUIRED_COLUMNS.items():
        if not columns <= _table_columns(connection, table_name):
            raise BackfillMigrationNotReadyError(
                "receiver credential Phase 1 schema is inconsistent"
            )
    indexes = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    if not _REQUIRED_INDEXES <= indexes:
        raise BackfillMigrationNotReadyError(
            "receiver credential Phase 1 indexes are inconsistent"
        )
    migration_name = connection.execute(
        text("SELECT name FROM schema_migrations WHERE version = :version"),
        {"version": PHASE_ONE_VERSION},
    ).scalar_one_or_none()
    if migration_name != PHASE_ONE_NAME:
        raise BackfillMigrationNotReadyError("receiver credential Phase 1 ledger is inconsistent")


def _current_state(connection: Connection) -> tuple[int, str, int]:
    state = connection.execute(
        text(
            "SELECT schema_version, state, legacy_verification_enabled "
            "FROM receiver_credential_migration_state WHERE id = 1"
        )
    ).one_or_none()
    if state is None or state.schema_version != PHASE_ONE_VERSION:
        raise BackfillMigrationNotReadyError("receiver credential migration state is missing")
    if state.legacy_verification_enabled != 1:
        raise BackfillMigrationNotReadyError("legacy receiver verification is not enabled")
    return state


def _foreign_key_errors(connection: Connection) -> list:
    return connection.exec_driver_sql("PRAGMA foreign_key_check").all()


def _validate_existing_backfill(
    connection: Connection,
    hash_key: bytes,
    hash_key_version: int,
) -> None:
    stores = connection.execute(
        text("SELECT id, receiver_token, is_active FROM stores ORDER BY id")
    ).all()
    if not stores:
        raise BackfillConflictError("backfilled state has no Store fleet")
    devices = connection.execute(
        text(
            """
            SELECT d.id, d.store_id, d.status, d.disabled_at
            FROM receiver_devices d ORDER BY d.store_id
            """
        )
    ).all()
    credentials = connection.execute(
        text(
            """
            SELECT d.store_id, c.credential_version, c.token_format, c.token_hash,
                   c.hash_key_version, c.status, c.expiry_policy, c.expires_at
            FROM receiver_credentials c
            JOIN receiver_devices d ON d.id = c.device_id
            ORDER BY d.store_id
            """
        )
    ).all()
    if len(devices) != len(stores) or len(credentials) != len(stores):
        raise BackfillConflictError("backfilled state has inconsistent fleet counts")
    stores_by_id = {row.id: row for row in stores}
    if {row.store_id for row in devices} != set(stores_by_id):
        raise BackfillConflictError("backfilled devices do not cover the Store fleet")
    if {row.store_id for row in credentials} != set(stores_by_id):
        raise BackfillConflictError("backfilled credentials do not cover the Store fleet")
    for device in devices:
        store = stores_by_id[device.store_id]
        expected_status = "active" if bool(store.is_active) else "disabled"
        if device.status != expected_status:
            raise BackfillConflictError("backfilled device status is inconsistent")
        if expected_status == "active" and device.disabled_at is not None:
            raise BackfillConflictError("active backfilled device has a disabled timestamp")
        if expected_status == "disabled" and device.disabled_at is None:
            raise BackfillConflictError("disabled backfilled device lacks a timestamp")
    for credential in credentials:
        store = stores_by_id[credential.store_id]
        if (
            credential.credential_version != 1
            or credential.token_format != LEGACY_TOKEN_FORMAT
            or credential.hash_key_version != hash_key_version
            or credential.status != "active"
            or credential.expiry_policy != "non_expiring"
            or credential.expires_at is not None
        ):
            raise BackfillConflictError("backfilled credential format is inconsistent")
        if not verify_legacy_receiver_token(
            store.receiver_token, credential.token_hash, hash_key
        ):
            raise BackfillConflictError("backfilled credential hash is inconsistent")
    if _foreign_key_errors(connection):
        raise BackfillValidationError("backfilled foreign-key validation failed")
    expected_audits = len(stores) * 2 + 1
    audit_count = connection.execute(
        text("SELECT COUNT(*) FROM receiver_credential_events")
    ).scalar_one()
    state_event_count = connection.execute(
        text(
            "SELECT COUNT(*) FROM receiver_credential_events "
            "WHERE event_type = 'migration_state_changed'"
        )
    ).scalar_one()
    if audit_count != expected_audits or state_event_count != 1:
        raise BackfillConflictError("backfilled audit history is incomplete")


def _ensure_no_existing_backfill_rows(connection: Connection) -> None:
    counts = tuple(
        connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
        for table_name in (
            "receiver_devices",
            "receiver_credentials",
            "receiver_credential_events",
        )
    )
    if counts != (0, 0, 0):
        formats = {
            row[0]
            for row in connection.execute(
                text("SELECT DISTINCT token_format FROM receiver_credentials")
            )
        }
        if formats - {LEGACY_TOKEN_FORMAT}:
            raise BackfillConflictError("an unsupported existing credential format was found")
        raise BackfillConflictError("existing Receiver Credential rows conflict with rehearsal")


def _audit_metadata(
    *,
    store_id: int | None = None,
    device_public_id: str | None = None,
    credential_public_id: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "outcome": "success",
        "migration_phase": BACKFILL_MIGRATION_PHASE,
    }
    if store_id is not None:
        payload["store_id"] = store_id
    if device_public_id is not None:
        payload["device_public_id"] = device_public_id
    if credential_public_id is not None:
        payload["credential_public_id"] = credential_public_id
        payload["credential_version"] = 1
    sanitized = sanitize_audit_payload(payload)
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _notify(step_hook: Callable[[str], None] | None, step: str) -> None:
    if step_hook is not None:
        step_hook(step)


def _validate_inserted_fleet(
    connection: Connection,
    plans: list[_StorePlan],
    store_snapshot: list,
    ledger_snapshot: list,
    hash_key: bytes,
) -> None:
    expected = len(plans)
    device_count = connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar_one()
    credential_count = connection.execute(
        text("SELECT COUNT(*) FROM receiver_credentials")
    ).scalar_one()
    if device_count != expected or credential_count != expected:
        raise BackfillValidationError("backfill row counts do not match the Store fleet")

    duplicate_devices = connection.execute(
        text(
            """
            SELECT store_id FROM receiver_devices
            GROUP BY store_id HAVING COUNT(*) != 1
            """
        )
    ).all()
    broken_relationships = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM receiver_credentials c
            LEFT JOIN receiver_devices d ON d.id = c.device_id
            LEFT JOIN stores s ON s.id = d.store_id
            WHERE d.id IS NULL OR s.id IS NULL
            """
        )
    ).scalar_one()
    if duplicate_devices or broken_relationships:
        raise BackfillValidationError("backfill relationships are inconsistent")

    credentials = {
        row.store_id: row
        for row in connection.execute(
            text(
                """
                SELECT d.store_id, c.token_format, c.token_hash
                FROM receiver_credentials c
                JOIN receiver_devices d ON d.id = c.device_id
                """
            )
        )
    }
    for plan in plans:
        credential = credentials.get(plan.store_id)
        if credential is None or credential.token_format != LEGACY_TOKEN_FORMAT:
            raise BackfillValidationError("a Store lacks its legacy credential mapping")
        if not verify_legacy_receiver_token(plan.raw_token, credential.token_hash, hash_key):
            raise BackfillValidationError("a stored legacy credential hash did not verify")

    current_stores = connection.execute(
        text(
            """
            SELECT id, receiver_token, is_active, status, last_seen, is_online_store
            FROM stores ORDER BY id
            """
        )
    ).all()
    if current_stores != store_snapshot:
        raise BackfillValidationError("the Store fleet changed during backfill rehearsal")
    current_ledger = connection.execute(
        text("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    ).all()
    if current_ledger != ledger_snapshot:
        raise BackfillValidationError("schema migration ledger changed during backfill rehearsal")
    if _foreign_key_errors(connection):
        raise BackfillValidationError("foreign-key validation failed during backfill rehearsal")

    persisted_values: list[str] = []
    for table_name in (
        "receiver_devices",
        "receiver_credentials",
        "receiver_credential_events",
    ):
        for row in connection.exec_driver_sql(f'SELECT * FROM "{table_name}"'):
            persisted_values.extend("" if value is None else str(value) for value in row)
    for plan in plans:
        if any(plan.raw_token in value for value in persisted_values):
            raise BackfillValidationError("raw legacy credential material was persisted")


def rehearse_legacy_receiver_backfill(
    engine: Engine,
    *,
    hash_key: bytes,
    hash_key_version: int,
    now: datetime | None = None,
    step_hook: Callable[[str], None] | None = None,
) -> BackfillResult:
    """Rehearse a complete legacy Store credential backfill transaction."""
    validated_key, validated_key_version, validated_now = _validate_inputs(
        engine, hash_key, hash_key_version, now
    )
    timestamp = validated_now.isoformat()

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise BackfillMigrationNotReadyError(
                "SQLite foreign-key enforcement is unavailable"
            )
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _validate_schema(connection)
            state = _current_state(connection)
            if state.state == BACKFILLED_STATE:
                _validate_existing_backfill(
                    connection,
                    validated_key,
                    validated_key_version,
                )
                raise BackfillAlreadyAppliedError(
                    "legacy receiver backfill rehearsal is already applied"
                )
            if state.state != MIGRATION_STATE_LEGACY_ONLY:
                raise BackfillMigrationNotReadyError(
                    "receiver credential migration state is not legacy_only"
                )
            if _foreign_key_errors(connection):
                raise BackfillValidationError("foreign-key validation failed before rehearsal")
            _ensure_no_existing_backfill_rows(connection)

            stores = connection.execute(
                text(
                    """
                    SELECT id, receiver_token, is_active, status, last_seen, is_online_store
                    FROM stores ORDER BY id
                    """
                )
            ).all()
            if not stores:
                raise BackfillValidationError("Store fleet is empty")
            ledger_snapshot = connection.execute(
                text("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
            ).all()

            plans: list[_StorePlan] = []
            for store in stores:
                try:
                    token_hash = hash_legacy_receiver_token(
                        store.receiver_token,
                        validated_key,
                        key_version=validated_key_version,
                    )
                except InvalidCredentialError:
                    raise InvalidLegacyCredentialError(
                        "a Store has an invalid legacy receiver credential"
                    ) from None
                plans.append(
                    _StorePlan(
                        store_id=store.id,
                        raw_token=store.receiver_token,
                        is_active=bool(store.is_active),
                        device_public_id=str(uuid.uuid4()),
                        credential_public_id=str(uuid.uuid4()),
                        token_hash=token_hash,
                    )
                )

            for index, plan in enumerate(plans):
                device_status = "active" if plan.is_active else "disabled"
                disabled_at = None if plan.is_active else timestamp
                result = connection.execute(
                    text(
                        """
                        INSERT INTO receiver_devices (
                            public_id, store_id, display_name, status, enrolled_at,
                            disabled_at, created_by, created_at, updated_at
                        ) VALUES (
                            :public_id, :store_id, :display_name, :status, :now,
                            :disabled_at, NULL, :now, :now
                        )
                        """
                    ),
                    {
                        "public_id": plan.device_public_id,
                        "store_id": plan.store_id,
                        "display_name": f"Legacy Receiver {plan.store_id}",
                        "status": device_status,
                        "disabled_at": disabled_at,
                        "now": timestamp,
                    },
                )
                plan.device_id = result.lastrowid
                if index == 0:
                    _notify(step_hook, "after_first_device_insert")

            for index, plan in enumerate(plans):
                result = connection.execute(
                    text(
                        """
                        INSERT INTO receiver_credentials (
                            public_id, device_id, credential_version, token_format,
                            token_hash, hash_key_version, status, expiry_policy,
                            issued_at, expires_at, revoked_at, replaced_at,
                            accept_until, last_used_at, created_by,
                            replaces_credential_id, created_at
                        ) VALUES (
                            :public_id, :device_id, 1, :token_format,
                            :token_hash, :hash_key_version, 'active', 'non_expiring',
                            :now, NULL, NULL, NULL, NULL, NULL, NULL, NULL, :now
                        )
                        """
                    ),
                    {
                        "public_id": plan.credential_public_id,
                        "device_id": plan.device_id,
                        "token_format": LEGACY_TOKEN_FORMAT,
                        "token_hash": plan.token_hash,
                        "hash_key_version": validated_key_version,
                        "now": timestamp,
                    },
                )
                plan.credential_id = result.lastrowid
                if index == 0:
                    _notify(step_hook, "after_first_credential_insert")

            _notify(step_hook, "before_first_audit_insert")
            for plan in plans:
                for event_type, credential_id, metadata in (
                    (
                        "device_enrolled",
                        None,
                        _audit_metadata(
                            store_id=plan.store_id,
                            device_public_id=plan.device_public_id,
                        ),
                    ),
                    (
                        "credential_issued",
                        plan.credential_id,
                        _audit_metadata(
                            store_id=plan.store_id,
                            device_public_id=plan.device_public_id,
                            credential_public_id=plan.credential_public_id,
                        ),
                    ),
                ):
                    connection.execute(
                        text(
                            """
                            INSERT INTO receiver_credential_events (
                                public_id, event_type, outcome, store_id, device_id,
                                credential_id, actor_user_id, event_at, reason_code,
                                correlation_id, metadata_json
                            ) VALUES (
                                :public_id, :event_type, 'success', :store_id, :device_id,
                                :credential_id, NULL, :now, :reason_code, NULL, :metadata_json
                            )
                            """
                        ),
                        {
                            "public_id": str(uuid.uuid4()),
                            "event_type": event_type,
                            "store_id": plan.store_id,
                            "device_id": plan.device_id,
                            "credential_id": credential_id,
                            "now": timestamp,
                            "reason_code": BACKFILL_REASON_CODE,
                            "metadata_json": metadata,
                        },
                    )

            _validate_inserted_fleet(
                connection,
                plans,
                stores,
                ledger_snapshot,
                validated_key,
            )
            connection.execute(
                text(
                    """
                    UPDATE receiver_credential_migration_state
                    SET state = :state, updated_at = :now
                    WHERE id = 1 AND state = :expected_state
                      AND legacy_verification_enabled = 1
                    """
                ),
                {
                    "state": BACKFILLED_STATE,
                    "now": timestamp,
                    "expected_state": MIGRATION_STATE_LEGACY_ONLY,
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credential_events (
                        public_id, event_type, outcome, store_id, device_id,
                        credential_id, actor_user_id, event_at, reason_code,
                        correlation_id, metadata_json
                    ) VALUES (
                        :public_id, 'migration_state_changed', 'success', NULL, NULL,
                        NULL, NULL, :now, :reason_code, NULL, :metadata_json
                    )
                    """
                ),
                {
                    "public_id": str(uuid.uuid4()),
                    "now": timestamp,
                    "reason_code": BACKFILL_REASON_CODE,
                    "metadata_json": _audit_metadata(),
                },
            )
            _notify(step_hook, "after_state_update_before_commit")

            final_state = _current_state(connection)
            expected_audit_count = len(plans) * 2 + 1
            audit_count = connection.execute(
                text("SELECT COUNT(*) FROM receiver_credential_events")
            ).scalar_one()
            if final_state.state != BACKFILLED_STATE or audit_count != expected_audit_count:
                raise BackfillValidationError("final backfill state validation failed")
            if _foreign_key_errors(connection):
                raise BackfillValidationError("final foreign-key validation failed")
            connection.commit()
        except ReceiverCredentialBackfillError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise BackfillPersistenceError(
                "legacy receiver backfill rehearsal could not be persisted"
            ) from None

    active_count = sum(plan.is_active for plan in plans)
    return BackfillResult(
        total_store_count=len(plans),
        active_store_count=active_count,
        inactive_store_count=len(plans) - active_count,
        device_count=len(plans),
        credential_count=len(plans),
        audit_event_count=len(plans) * 2 + 1,
        final_state=BACKFILLED_STATE,
        store_ids=tuple(plan.store_id for plan in plans),
    )
