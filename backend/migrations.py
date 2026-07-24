"""Explicit, additive SQLite migrations for SpeakLink.

This module intentionally does not import the application's default engine.
Callers must provide an Engine, and the protected ``speaklink_live.db`` path is
refused unless a future maintenance-mode caller explicitly opts in.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import Engine


PHASE_ONE_VERSION = 1
PHASE_ONE_NAME = "receiver_credential_lifecycle_phase_one"
MIGRATION_STATE_LEGACY_ONLY = "legacy_only"
PROTECTED_DATABASE_PATH = Path(__file__).resolve().parent / "speaklink_live.db"

_PHASE_ONE_TABLES = {
    "receiver_devices",
    "receiver_credentials",
    "receiver_credential_events",
    "receiver_credential_migration_state",
    "schema_migrations",
}


class ReceiverCredentialMigrationError(RuntimeError):
    """Base error for an explicit credential migration failure."""


class ProtectedDatabaseError(ReceiverCredentialMigrationError):
    """Raised before connecting when the protected live database was supplied."""


class MigrationIntegrityError(ReceiverCredentialMigrationError):
    """Raised when an applied migration ledger does not match the schema."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    version: int
    state: str
    applied: bool


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _reject_protected_database(engine: Engine, allow_protected_database: bool) -> None:
    if _database_path(engine) == PROTECTED_DATABASE_PATH.resolve() and not allow_protected_database:
        raise ProtectedDatabaseError(
            "the protected SpeakLink database requires an explicit maintenance-mode opt-in"
        )


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notify(step_hook: Callable[[str], None] | None, step: str) -> None:
    if step_hook is not None:
        step_hook(step)


def _assert_applied_schema(connection) -> str:
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = _PHASE_ONE_TABLES - tables
    if missing:
        raise MigrationIntegrityError("an applied credential migration has missing schema objects")
    state = connection.execute(
        text(
            "SELECT state FROM receiver_credential_migration_state "
            "WHERE id = 1 AND schema_version = :version"
        ),
        {"version": PHASE_ONE_VERSION},
    ).scalar_one_or_none()
    if state is None:
        raise MigrationIntegrityError("credential migration state is missing or inconsistent")
    return state


def run_receiver_credential_phase_one(
    engine: Engine,
    *,
    allow_protected_database: bool = False,
    step_hook: Callable[[str], None] | None = None,
) -> MigrationResult:
    """Apply Receiver Credential Lifecycle Phase 1 in one SQLite transaction.

    ``step_hook`` supports progress reporting and deterministic rollback tests;
    it must not receive or emit credential material.
    """
    if engine.dialect.name != "sqlite":
        raise ReceiverCredentialMigrationError("Phase 1 currently supports SQLite only")
    _reject_protected_database(engine, allow_protected_database)

    if not event.contains(engine, "connect", _enable_sqlite_foreign_keys):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise ReceiverCredentialMigrationError("SQLite foreign key enforcement is unavailable")
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            connection.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name VARCHAR(200) NOT NULL UNIQUE,
                    applied_at VARCHAR(40) NOT NULL
                        CHECK (
                            length(applied_at) BETWEEN 20 AND 40
                            AND (
                                substr(applied_at, -6) = '+00:00'
                                OR substr(applied_at, -1) = 'Z'
                            )
                        )
                )
                """
            )
            _notify(step_hook, "schema_migrations")

            already_applied = connection.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :version"),
                {"version": PHASE_ONE_VERSION},
            ).scalar_one_or_none()
            if already_applied:
                state = _assert_applied_schema(connection)
                connection.commit()
                return MigrationResult(PHASE_ONE_VERSION, state, False)

            connection.exec_driver_sql(
                """
                CREATE TABLE receiver_devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id VARCHAR(36) NOT NULL UNIQUE
                        CHECK (
                            length(public_id) = 36
                            AND substr(public_id, 9, 1) = '-'
                            AND substr(public_id, 14, 1) = '-'
                            AND substr(public_id, 19, 1) = '-'
                            AND substr(public_id, 24, 1) = '-'
                            AND length(replace(public_id, '-', '')) = 32
                            AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                        ),
                    store_id INTEGER NOT NULL,
                    display_name VARCHAR(200) NOT NULL
                        CHECK (length(display_name) BETWEEN 1 AND 200),
                    status VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled', 'retired')),
                    enrolled_at VARCHAR(40) NOT NULL,
                    disabled_at VARCHAR(40),
                    created_by INTEGER,
                    created_at VARCHAR(40) NOT NULL,
                    updated_at VARCHAR(40) NOT NULL,
                    CONSTRAINT fk_receiver_devices_store
                        FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE RESTRICT,
                    CONSTRAINT fk_receiver_devices_creator
                        FOREIGN KEY (created_by) REFERENCES hq_users(id) ON DELETE SET NULL,
                    CONSTRAINT ck_receiver_devices_disabled_state CHECK (
                        (status = 'active' AND disabled_at IS NULL)
                        OR (status IN ('disabled', 'retired') AND disabled_at IS NOT NULL)
                    ),
                    CONSTRAINT ck_receiver_devices_utc CHECK (
                        (substr(enrolled_at, -6) = '+00:00' OR substr(enrolled_at, -1) = 'Z')
                        AND (substr(created_at, -6) = '+00:00' OR substr(created_at, -1) = 'Z')
                        AND (substr(updated_at, -6) = '+00:00' OR substr(updated_at, -1) = 'Z')
                        AND (
                            disabled_at IS NULL
                            OR substr(disabled_at, -6) = '+00:00'
                            OR substr(disabled_at, -1) = 'Z'
                        )
                    )
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX ix_receiver_devices_public_id "
                "ON receiver_devices(public_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_devices_store_id ON receiver_devices(store_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_devices_store_status "
                "ON receiver_devices(store_id, status)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_devices_status ON receiver_devices(status)"
            )
            _notify(step_hook, "receiver_devices")

            connection.exec_driver_sql(
                """
                CREATE TABLE receiver_credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id VARCHAR(36) NOT NULL UNIQUE
                        CHECK (
                            length(public_id) = 36
                            AND substr(public_id, 9, 1) = '-'
                            AND substr(public_id, 14, 1) = '-'
                            AND substr(public_id, 19, 1) = '-'
                            AND substr(public_id, 24, 1) = '-'
                            AND length(replace(public_id, '-', '')) = 32
                            AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                        ),
                    device_id INTEGER NOT NULL,
                    credential_version INTEGER NOT NULL
                        CHECK (credential_version > 0),
                    token_format VARCHAR(24) NOT NULL
                        CHECK (token_format IN ('legacy_uuid_hex', 'speaklink_rcv')),
                    token_hash VARCHAR(128) NOT NULL UNIQUE
                        CHECK (length(token_hash) BETWEEN 64 AND 128),
                    hash_key_version INTEGER NOT NULL
                        CHECK (hash_key_version > 0),
                    status VARCHAR(20) NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'superseded', 'revoked', 'expired')),
                    expiry_policy VARCHAR(20) NOT NULL DEFAULT 'non_expiring'
                        CHECK (expiry_policy IN ('non_expiring', 'expires_at')),
                    issued_at VARCHAR(40) NOT NULL,
                    expires_at VARCHAR(40),
                    revoked_at VARCHAR(40),
                    replaced_at VARCHAR(40),
                    accept_until VARCHAR(40),
                    last_used_at VARCHAR(40),
                    created_by INTEGER,
                    replaces_credential_id INTEGER,
                    created_at VARCHAR(40) NOT NULL,
                    CONSTRAINT uq_receiver_credentials_device_version
                        UNIQUE (device_id, credential_version),
                    CONSTRAINT fk_receiver_credentials_device
                        FOREIGN KEY (device_id) REFERENCES receiver_devices(id) ON DELETE RESTRICT,
                    CONSTRAINT fk_receiver_credentials_creator
                        FOREIGN KEY (created_by) REFERENCES hq_users(id) ON DELETE SET NULL,
                    CONSTRAINT fk_receiver_credentials_replaces
                        FOREIGN KEY (replaces_credential_id)
                        REFERENCES receiver_credentials(id) ON DELETE SET NULL,
                    CONSTRAINT ck_receiver_credentials_expiry CHECK (
                        (expiry_policy = 'non_expiring' AND expires_at IS NULL)
                        OR (expiry_policy = 'expires_at' AND expires_at IS NOT NULL)
                    ),
                    CONSTRAINT ck_receiver_credentials_revocation CHECK (
                        status != 'revoked' OR revoked_at IS NOT NULL
                    ),
                    CONSTRAINT ck_receiver_credentials_superseded CHECK (
                        status != 'superseded'
                        OR (replaced_at IS NOT NULL AND accept_until IS NOT NULL)
                    ),
                    CONSTRAINT ck_receiver_credentials_utc CHECK (
                        (substr(issued_at, -6) = '+00:00' OR substr(issued_at, -1) = 'Z')
                        AND (substr(created_at, -6) = '+00:00' OR substr(created_at, -1) = 'Z')
                        AND (expires_at IS NULL OR substr(expires_at, -6) = '+00:00' OR substr(expires_at, -1) = 'Z')
                        AND (revoked_at IS NULL OR substr(revoked_at, -6) = '+00:00' OR substr(revoked_at, -1) = 'Z')
                        AND (replaced_at IS NULL OR substr(replaced_at, -6) = '+00:00' OR substr(replaced_at, -1) = 'Z')
                        AND (accept_until IS NULL OR substr(accept_until, -6) = '+00:00' OR substr(accept_until, -1) = 'Z')
                        AND (last_used_at IS NULL OR substr(last_used_at, -6) = '+00:00' OR substr(last_used_at, -1) = 'Z')
                    )
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX ix_receiver_credentials_public_id "
                "ON receiver_credentials(public_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credentials_auth_lookup "
                "ON receiver_credentials(hash_key_version, token_hash)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credentials_device_status "
                "ON receiver_credentials(device_id, status)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credentials_expires_at "
                "ON receiver_credentials(expires_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credentials_revoked_at "
                "ON receiver_credentials(revoked_at)"
            )
            _notify(step_hook, "receiver_credentials")

            connection.exec_driver_sql(
                """
                CREATE TABLE receiver_credential_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id VARCHAR(36) NOT NULL UNIQUE
                        CHECK (
                            length(public_id) = 36
                            AND substr(public_id, 9, 1) = '-'
                            AND substr(public_id, 14, 1) = '-'
                            AND substr(public_id, 19, 1) = '-'
                            AND substr(public_id, 24, 1) = '-'
                            AND length(replace(public_id, '-', '')) = 32
                            AND replace(public_id, '-', '') NOT GLOB '*[^0-9a-f]*'
                        ),
                    event_type VARCHAR(40) NOT NULL
                        CHECK (event_type IN (
                            'device_enrolled', 'device_enabled', 'device_disabled',
                            'credential_issued', 'credential_rotated',
                            'credential_revoked', 'authentication_succeeded',
                            'authentication_failed', 'migration_state_changed'
                        )),
                    outcome VARCHAR(20) NOT NULL
                        CHECK (outcome IN ('success', 'rejected', 'failed')),
                    store_id INTEGER,
                    device_id INTEGER,
                    credential_id INTEGER,
                    actor_user_id INTEGER,
                    event_at VARCHAR(40) NOT NULL,
                    reason_code VARCHAR(64),
                    correlation_id VARCHAR(64),
                    metadata_json TEXT,
                    CONSTRAINT fk_receiver_credential_events_store
                        FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE SET NULL,
                    CONSTRAINT fk_receiver_credential_events_device
                        FOREIGN KEY (device_id) REFERENCES receiver_devices(id) ON DELETE SET NULL,
                    CONSTRAINT fk_receiver_credential_events_credential
                        FOREIGN KEY (credential_id)
                        REFERENCES receiver_credentials(id) ON DELETE SET NULL,
                    CONSTRAINT fk_receiver_credential_events_actor
                        FOREIGN KEY (actor_user_id) REFERENCES hq_users(id) ON DELETE SET NULL,
                    CONSTRAINT ck_receiver_credential_events_reason CHECK (
                        reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64
                    ),
                    CONSTRAINT ck_receiver_credential_events_correlation CHECK (
                        correlation_id IS NULL OR length(correlation_id) BETWEEN 1 AND 64
                    ),
                    CONSTRAINT ck_receiver_credential_events_utc CHECK (
                        substr(event_at, -6) = '+00:00' OR substr(event_at, -1) = 'Z'
                    )
                )
                """
            )
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX ix_receiver_credential_events_public_id "
                "ON receiver_credential_events(public_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credential_events_device_time "
                "ON receiver_credential_events(device_id, event_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credential_events_credential_time "
                "ON receiver_credential_events(credential_id, event_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credential_events_store_time "
                "ON receiver_credential_events(store_id, event_at)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX ix_receiver_credential_events_type_time "
                "ON receiver_credential_events(event_type, event_at)"
            )
            _notify(step_hook, "receiver_credential_events")

            connection.exec_driver_sql(
                """
                CREATE TABLE receiver_credential_migration_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                    state VARCHAR(24) NOT NULL
                        CHECK (state IN (
                            'legacy_only', 'backfilled', 'dual_verify',
                            'hash_only', 'raw_neutralized'
                        )),
                    legacy_verification_enabled INTEGER NOT NULL DEFAULT 1
                        CHECK (legacy_verification_enabled IN (0, 1)),
                    updated_at VARCHAR(40) NOT NULL
                        CHECK (substr(updated_at, -6) = '+00:00' OR substr(updated_at, -1) = 'Z')
                )
                """
            )
            now = _utc_now_text()
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credential_migration_state (
                        id, schema_version, state, legacy_verification_enabled, updated_at
                    ) VALUES (1, :version, :state, 1, :now)
                    """
                ),
                {
                    "version": PHASE_ONE_VERSION,
                    "state": MIGRATION_STATE_LEGACY_ONLY,
                    "now": now,
                },
            )
            _notify(step_hook, "receiver_credential_migration_state")

            connection.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (:version, :name, :now)"
                ),
                {"version": PHASE_ONE_VERSION, "name": PHASE_ONE_NAME, "now": now},
            )
            _notify(step_hook, "complete")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    return MigrationResult(PHASE_ONE_VERSION, MIGRATION_STATE_LEGACY_ONLY, True)
