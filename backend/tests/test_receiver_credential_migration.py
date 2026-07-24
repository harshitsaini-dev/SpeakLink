"""Isolated tests for Receiver Credential Lifecycle migration Phase 1."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from migrations import (
    MIGRATION_STATE_LEGACY_ONLY,
    PHASE_ONE_VERSION,
    ProtectedDatabaseError,
    run_receiver_credential_phase_one,
)


REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"
PHASE_ONE_TABLES = {
    "receiver_devices",
    "receiver_credentials",
    "receiver_credential_events",
    "receiver_credential_migration_state",
    "schema_migrations",
}
EXPECTED_INDEXES = {
    "receiver_devices": {
        "ix_receiver_devices_public_id",
        "ix_receiver_devices_store_id",
        "ix_receiver_devices_store_status",
        "ix_receiver_devices_status",
    },
    "receiver_credentials": {
        "ix_receiver_credentials_auth_lookup",
        "ix_receiver_credentials_device_status",
        "ix_receiver_credentials_expires_at",
        "ix_receiver_credentials_public_id",
        "ix_receiver_credentials_revoked_at",
    },
    "receiver_credential_events": {
        "ix_receiver_credential_events_credential_time",
        "ix_receiver_credential_events_device_time",
        "ix_receiver_credential_events_store_time",
        "ix_receiver_credential_events_type_time",
    },
}


@pytest.fixture
def isolated_engine(tmp_path: Path) -> Engine:
    database_path = tmp_path / "phase_one.db"
    engine = create_engine(f"sqlite:///{database_path}")
    try:
        yield engine
    finally:
        engine.dispose()


def create_isolated_legacy_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.exec_driver_sql(
            """
            CREATE TABLE hq_users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL,
                is_active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE stores (
                id INTEGER PRIMARY KEY,
                store_code VARCHAR(50) NOT NULL UNIQUE,
                store_name VARCHAR(200) NOT NULL,
                city VARCHAR(100) NOT NULL,
                region VARCHAR(100) NOT NULL,
                is_online_store BOOLEAN NOT NULL,
                receiver_token VARCHAR(64) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL,
                status VARCHAR(20) NOT NULL,
                last_seen DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )


def insert_legacy_store(engine: Engine) -> str:
    legacy_token = "0123456789abcdef0123456789abcdef"
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO stores (
                    id, store_code, store_name, city, region, is_online_store,
                    receiver_token, is_active, status, created_at, updated_at
                ) VALUES (
                    41, 'STORE-041', 'Migration Store', 'Pune', 'West', 0,
                    :receiver_token, 1, 'offline', :now, :now
                )
                """
            ),
            {
                "receiver_token": legacy_token,
                "now": "2026-07-24T10:00:00+00:00",
            },
        )
    return legacy_token


def test_empty_isolated_database_migrates_with_version_and_legacy_only_state(
    isolated_engine: Engine,
):
    result = run_receiver_credential_phase_one(isolated_engine)

    assert result.applied is True
    assert result.version == PHASE_ONE_VERSION
    assert result.state == MIGRATION_STATE_LEGACY_ONLY
    assert PHASE_ONE_TABLES <= set(inspect(isolated_engine).get_table_names())
    with isolated_engine.connect() as connection:
        migration = connection.execute(
            text("SELECT version, name FROM schema_migrations")
        ).one()
        state = connection.execute(
            text(
                "SELECT schema_version, state, legacy_verification_enabled "
                "FROM receiver_credential_migration_state WHERE id = 1"
            )
        ).one()
    assert migration.version == PHASE_ONE_VERSION
    assert migration.name == "receiver_credential_lifecycle_phase_one"
    assert state == (PHASE_ONE_VERSION, MIGRATION_STATE_LEGACY_ONLY, 1)


def test_existing_store_identity_and_receiver_token_are_preserved(
    isolated_engine: Engine,
):
    create_isolated_legacy_schema(isolated_engine)
    legacy_token = insert_legacy_store(isolated_engine)

    run_receiver_credential_phase_one(isolated_engine)

    with isolated_engine.connect() as connection:
        stores = connection.execute(
            text("SELECT id, store_code, receiver_token FROM stores ORDER BY id")
        ).all()
        new_row_counts = {
            table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            for table in (
                "receiver_devices",
                "receiver_credentials",
                "receiver_credential_events",
            )
        }
    assert stores == [(41, "STORE-041", legacy_token)]
    assert new_row_counts == {
        "receiver_devices": 0,
        "receiver_credentials": 0,
        "receiver_credential_events": 0,
    }


def test_new_tables_have_required_indexes_and_no_raw_token_column(isolated_engine: Engine):
    run_receiver_credential_phase_one(isolated_engine)
    inspector = inspect(isolated_engine)

    for table, expected in EXPECTED_INDEXES.items():
        actual = {index["name"] for index in inspector.get_indexes(table)}
        assert expected <= actual

    credential_columns = {
        column["name"] for column in inspector.get_columns("receiver_credentials")
    }
    assert "token_hash" in credential_columns
    assert "receiver_token" not in credential_columns
    assert "raw_token" not in credential_columns


def test_uuid_status_and_token_format_constraints_are_enforced(isolated_engine: Engine):
    create_isolated_legacy_schema(isolated_engine)
    insert_legacy_store(isolated_engine)
    run_receiver_credential_phase_one(isolated_engine)

    valid_device = {
        "public_id": "00000000-0000-4000-8000-000000000001",
        "now": "2026-07-24T10:00:00+00:00",
    }
    with pytest.raises(IntegrityError):
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_devices (
                        public_id, store_id, display_name, status,
                        enrolled_at, created_at, updated_at
                    ) VALUES (:public_id, 41, 'Invalid Receiver', 'active', :now, :now, :now)
                    """
                ),
                {"public_id": "x" * 36, "now": valid_device["now"]},
            )
    with pytest.raises(IntegrityError):
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_devices (
                        public_id, store_id, display_name, status,
                        enrolled_at, created_at, updated_at
                    ) VALUES (:public_id, 41, 'Naive Time Receiver', 'active', :now, :now, :now)
                    """
                ),
                {"public_id": valid_device["public_id"], "now": "2026-07-24T10:00:00"},
            )
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO receiver_devices (
                    public_id, store_id, display_name, status,
                    enrolled_at, created_at, updated_at
                ) VALUES (:public_id, 41, 'Primary Receiver', 'active', :now, :now, :now)
                """
            ),
            valid_device,
        )

    with pytest.raises(IntegrityError):
        with isolated_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credentials (
                        public_id, device_id, credential_version, token_format,
                        token_hash, hash_key_version, status, expiry_policy,
                        issued_at, created_at
                    ) VALUES (
                        '00000000-0000-4000-8000-000000000002', 1, 1,
                        'raw_token', :token_hash, 1, 'active', 'non_expiring',
                        :now, :now
                    )
                    """
                ),
                {"token_hash": "h" * 64, "now": valid_device["now"]},
            )


def test_foreign_keys_are_valid_and_enforced(isolated_engine: Engine):
    create_isolated_legacy_schema(isolated_engine)
    run_receiver_credential_phase_one(isolated_engine)

    with isolated_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        connection.commit()
        with pytest.raises(IntegrityError):
            with connection.begin():
                connection.execute(
                    text(
                        """
                        INSERT INTO receiver_devices (
                            public_id, store_id, display_name, status,
                            enrolled_at, created_at, updated_at
                        ) VALUES (
                            '00000000-0000-4000-8000-000000000001', 999,
                            'Invalid Store Receiver', 'active', :now, :now, :now
                        )
                        """
                    ),
                    {"now": "2026-07-24T10:00:00+00:00"},
                )


def test_running_phase_one_twice_is_idempotent(isolated_engine: Engine):
    first = run_receiver_credential_phase_one(isolated_engine)
    second = run_receiver_credential_phase_one(isolated_engine)

    assert first.applied is True
    assert second.applied is False
    with isolated_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 1
        assert connection.execute(
            text("SELECT COUNT(*) FROM receiver_credential_migration_state")
        ).scalar_one() == 1


def test_deliberate_mid_migration_failure_rolls_back_all_phase_one_ddl(
    isolated_engine: Engine,
):
    def fail_after_credentials(step: str) -> None:
        if step == "receiver_credentials":
            raise RuntimeError("deliberate migration test failure")

    with pytest.raises(RuntimeError, match="deliberate migration test failure"):
        run_receiver_credential_phase_one(isolated_engine, step_hook=fail_after_credentials)

    assert PHASE_ONE_TABLES.isdisjoint(inspect(isolated_engine).get_table_names())


def test_protected_real_database_is_refused_before_connection():
    before = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    protected_engine = create_engine(f"sqlite:///{REAL_DATABASE}")
    try:
        with pytest.raises(ProtectedDatabaseError):
            run_receiver_credential_phase_one(protected_engine)
    finally:
        protected_engine.dispose()
    after = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    assert after == before
