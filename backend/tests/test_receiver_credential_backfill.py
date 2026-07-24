"""Isolated fleet-level rehearsal tests for legacy Receiver credentials."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hmac
import json
from pathlib import Path
import secrets
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from migrations import run_receiver_credential_phase_one
from receiver_credentials import hash_legacy_receiver_token, verify_legacy_receiver_token
from receiver_credential_backfill import (
    BackfillAlreadyAppliedError,
    BackfillConflictError,
    BackfillMigrationNotReadyError,
    BackfillPersistenceError,
    BackfillValidationError,
    InvalidLegacyCredentialError,
    ProtectedDatabaseError,
    rehearse_legacy_receiver_backfill,
)


REAL_DATABASE = Path(__file__).resolve().parents[1] / "speaklink_live.db"
FIXED_NOW = datetime(2026, 7, 24, 14, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def protected_database_metadata_is_unchanged():
    before = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    yield
    after = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    assert after == before


def create_legacy_schema(engine: Engine) -> None:
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


def insert_stores(engine: Engine, specifications: list[tuple[int, bool]]) -> dict[int, str]:
    tokens: dict[int, str] = {}
    with engine.begin() as connection:
        for store_id, active in specifications:
            token = secrets.token_hex(16)
            tokens[store_id] = token
            connection.execute(
                text(
                    """
                    INSERT INTO stores (
                        id, store_code, store_name, city, region, is_online_store,
                        receiver_token, is_active, status, last_seen, created_at, updated_at
                    ) VALUES (
                        :id, :code, :name, 'Test City', 'Test Region', :online,
                        :token, :active, :status, :last_seen, :now, :now
                    )
                    """
                ),
                {
                    "id": store_id,
                    "code": f"STORE-{store_id:03d}",
                    "name": f"Backfill Store {store_id}",
                    "online": store_id % 2,
                    "token": token,
                    "active": int(active),
                    "status": "offline" if active else "error",
                    "last_seen": None if active else "2026-07-23T10:00:00+00:00",
                    "now": "2026-07-20T09:00:00+00:00",
                },
            )
    return tokens


def create_database(
    tmp_path: Path,
    *,
    stores: list[tuple[int, bool]] | None = None,
    apply_phase_one: bool = True,
) -> tuple[Engine, dict[int, str], bytes]:
    engine = create_engine(
        f"sqlite:///{tmp_path / f'{uuid.uuid4()}.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    create_legacy_schema(engine)
    tokens = insert_stores(engine, stores or [])
    if apply_phase_one:
        run_receiver_credential_phase_one(engine)
    return engine, tokens, secrets.token_bytes(48)


@pytest.fixture
def fleet(tmp_path: Path):
    engine, tokens, hash_key = create_database(
        tmp_path,
        stores=[(11, True), (12, False), (13, True)],
    )
    try:
        yield engine, tokens, hash_key
    finally:
        engine.dispose()


def rehearse(engine: Engine, hash_key: bytes, **overrides):
    arguments = {
        "hash_key": hash_key,
        "hash_key_version": 4,
        "now": FIXED_NOW,
    }
    arguments.update(overrides)
    return rehearse_legacy_receiver_backfill(engine, **arguments)


def new_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return (
            connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar_one(),
            connection.execute(text("SELECT COUNT(*) FROM receiver_credentials")).scalar_one(),
            connection.execute(text("SELECT COUNT(*) FROM receiver_credential_events")).scalar_one(),
        )


def migration_state(engine: Engine) -> tuple[str, int]:
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT state, legacy_verification_enabled "
                "FROM receiver_credential_migration_state WHERE id = 1"
            )
        ).one()


def store_snapshot(engine: Engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT id, receiver_token, is_active, status, last_seen, is_online_store
                FROM stores ORDER BY id
                """
            )
        ).all()


def assert_store_snapshot_unchanged(before, after) -> None:
    assert len(after) == len(before)
    for before_row, after_row in zip(before, after, strict=True):
        assert after_row.id == before_row.id
        if not hmac.compare_digest(after_row.receiver_token, before_row.receiver_token):
            pytest.fail("Backfill changed a legacy Store credential", pytrace=False)
        assert after_row[2:] == before_row[2:]


def assert_no_generated_secret_on_surfaces(
    tokens: dict[int, str],
    hash_key: bytes,
    surfaces: tuple[str, ...],
) -> None:
    forbidden = (*tokens.values(), hash_key.hex())
    for secret_value in forbidden:
        if any(secret_value in surface for surface in surfaces):
            pytest.fail("Backfill exposed protected credential material", pytrace=False)


def test_complete_fleet_backfill_maps_active_and_inactive_stores(fleet, capsys, caplog):
    engine, tokens, hash_key = fleet
    stores_before = store_snapshot(engine)
    with engine.connect() as connection:
        ledger_before = connection.execute(
            text("SELECT version, name, applied_at FROM schema_migrations")
        ).all()

    result = rehearse(engine, hash_key)
    rendered_result = repr(result)

    assert result.total_store_count == 3
    assert result.active_store_count == 2
    assert result.inactive_store_count == 1
    assert result.device_count == 3
    assert result.credential_count == 3
    assert result.audit_event_count == 7
    assert result.final_state == "backfilled"
    assert "<redacted>" in rendered_result

    with engine.connect() as connection:
        devices = connection.execute(
            text(
                """
                SELECT id, public_id, store_id, display_name, status, disabled_at
                FROM receiver_devices ORDER BY store_id
                """
            )
        ).all()
        credentials = connection.execute(
            text(
                """
                SELECT c.public_id, d.store_id, c.credential_version, c.token_format,
                       c.token_hash, c.hash_key_version, c.status,
                       c.expiry_policy, c.expires_at, c.created_by
                FROM receiver_credentials c
                JOIN receiver_devices d ON d.id = c.device_id
                ORDER BY d.store_id
                """
            )
        ).all()
        audits = connection.execute(
            text(
                """
                SELECT event_type, outcome, reason_code, metadata_json
                FROM receiver_credential_events ORDER BY id
                """
            )
        ).all()
        ledger_after = connection.execute(
            text("SELECT version, name, applied_at FROM schema_migrations")
        ).all()
        foreign_key_errors = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        persisted_values = []
        for table_name in (
            "receiver_devices",
            "receiver_credentials",
            "receiver_credential_events",
            "receiver_credential_migration_state",
        ):
            for row in connection.exec_driver_sql(f'SELECT * FROM "{table_name}"'):
                persisted_values.extend("" if value is None else str(value) for value in row)

    assert [(row.store_id, row.status) for row in devices] == [
        (11, "active"),
        (12, "disabled"),
        (13, "active"),
    ]
    assert devices[0].disabled_at is None
    assert devices[1].disabled_at == FIXED_NOW.isoformat()
    assert devices[2].disabled_at is None
    assert all(row.display_name == f"Legacy Receiver {row.store_id}" for row in devices)
    assert len({row.public_id for row in devices}) == 3

    for credential in credentials:
        assert credential.credential_version == 1
        assert credential.token_format == "legacy_uuid_hex"
        assert credential.hash_key_version == 4
        assert credential.status == "active"
        assert credential.expiry_policy == "non_expiring"
        assert credential.expires_at is None
        assert credential.created_by is None
        if not verify_legacy_receiver_token(
            tokens[credential.store_id], credential.token_hash, hash_key
        ):
            pytest.fail("Stored legacy credential hash did not verify", pytrace=False)

    assert [row.event_type for row in audits].count("device_enrolled") == 3
    assert [row.event_type for row in audits].count("credential_issued") == 3
    assert [row.event_type for row in audits].count("migration_state_changed") == 1
    assert all(row.outcome == "success" for row in audits)
    assert all(row.reason_code == "legacy_backfill_rehearsal" for row in audits)
    for audit in audits:
        metadata = json.loads(audit.metadata_json)
        assert metadata["outcome"] == "success"

    assert migration_state(engine) == ("backfilled", 1)
    assert ledger_after == ledger_before
    assert foreign_key_errors == []
    assert_store_snapshot_unchanged(stores_before, store_snapshot(engine))
    captured = capsys.readouterr()
    logs = " ".join(record.getMessage() for record in caplog.records)
    assert_no_generated_secret_on_surfaces(
        tokens,
        hash_key,
        (" ".join(persisted_values), rendered_result, str(result), captured.out, captured.err, logs),
    )


def test_empty_store_database_fails_closed(tmp_path: Path):
    engine, _, hash_key = create_database(tmp_path)
    try:
        with engine.connect() as connection:
            ledger_before = connection.execute(
                text("SELECT version, name, applied_at FROM schema_migrations")
            ).all()
        with pytest.raises(BackfillValidationError):
            rehearse(engine, hash_key)
        assert new_counts(engine) == (0, 0, 0)
        assert migration_state(engine) == ("legacy_only", 1)
        with engine.connect() as connection:
            ledger_after = connection.execute(
                text("SELECT version, name, applied_at FROM schema_migrations")
            ).all()
        assert ledger_after == ledger_before
    finally:
        engine.dispose()


def test_invalid_legacy_token_aborts_complete_fleet(tmp_path: Path):
    engine, _, hash_key = create_database(tmp_path, stores=[(21, True), (22, False)])
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE stores SET receiver_token = 'invalid-format' WHERE id = 22")
            )
        with pytest.raises(InvalidLegacyCredentialError) as error:
            rehearse(engine, hash_key)
        assert str(error.value) == "a Store has an invalid legacy receiver credential"
        assert new_counts(engine) == (0, 0, 0)
        assert migration_state(engine) == ("legacy_only", 1)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "failure_step",
    [
        "after_first_device_insert",
        "after_first_credential_insert",
        "before_first_audit_insert",
        "after_state_update_before_commit",
    ],
)
def test_deliberate_failure_rolls_back_all_writes(fleet, failure_step: str):
    engine, _, hash_key = fleet

    def fail(step: str) -> None:
        if step == failure_step:
            raise RuntimeError("deliberate backfill rehearsal failure")

    with pytest.raises(BackfillPersistenceError) as error:
        rehearse(engine, hash_key, step_hook=fail)
    assert str(error.value) == "legacy receiver backfill rehearsal could not be persisted"
    assert new_counts(engine) == (0, 0, 0)
    assert migration_state(engine) == ("legacy_only", 1)


@pytest.mark.parametrize("conflict_kind", ["device_only", "credential_present"])
def test_partial_or_conflicting_rows_fail_closed(
    tmp_path: Path,
    conflict_kind: str,
):
    engine, tokens, hash_key = create_database(tmp_path, stores=[(31, True)])
    now = FIXED_NOW.isoformat()
    try:
        with engine.begin() as connection:
            device_result = connection.execute(
                text(
                    """
                    INSERT INTO receiver_devices (
                        public_id, store_id, display_name, status, enrolled_at,
                        disabled_at, created_by, created_at, updated_at
                    ) VALUES (:public_id, 31, 'Existing Device', 'active', :now, NULL, NULL, :now, :now)
                    """
                ),
                {"public_id": str(uuid.uuid4()), "now": now},
            )
            if conflict_kind == "credential_present":
                connection.execute(
                    text(
                        """
                        INSERT INTO receiver_credentials (
                            public_id, device_id, credential_version, token_format,
                            token_hash, hash_key_version, status, expiry_policy,
                            issued_at, expires_at, created_by, created_at
                        ) VALUES (
                            :public_id, :device_id, 1, 'legacy_uuid_hex',
                            :token_hash, 4, 'active', 'non_expiring',
                            :now, NULL, NULL, :now
                        )
                        """
                    ),
                    {
                        "public_id": str(uuid.uuid4()),
                        "device_id": device_result.lastrowid,
                        "token_hash": hash_legacy_receiver_token(tokens[31], hash_key, key_version=4),
                        "now": now,
                    },
                )
        before = new_counts(engine)
        with pytest.raises(BackfillConflictError):
            rehearse(engine, hash_key)
        assert new_counts(engine) == before
        assert migration_state(engine) == ("legacy_only", 1)
    finally:
        engine.dispose()


def test_missing_and_unexpected_migration_state_fail_closed(tmp_path: Path):
    missing_engine, _, missing_key = create_database(
        tmp_path, stores=[(41, True)], apply_phase_one=False
    )
    unexpected_engine, _, unexpected_key = create_database(
        tmp_path, stores=[(42, True)]
    )
    disabled_engine, _, disabled_key = create_database(
        tmp_path, stores=[(43, True)]
    )
    unknown_engine, _, unknown_key = create_database(
        tmp_path, stores=[(44, True)]
    )
    try:
        with pytest.raises(BackfillMigrationNotReadyError):
            rehearse(missing_engine, missing_key)

        with unexpected_engine.begin() as connection:
            connection.execute(
                text("UPDATE receiver_credential_migration_state SET state = 'dual_verify'")
            )
        with pytest.raises(BackfillMigrationNotReadyError):
            rehearse(unexpected_engine, unexpected_key)

        with disabled_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE receiver_credential_migration_state "
                    "SET legacy_verification_enabled = 0"
                )
            )
        with pytest.raises(BackfillMigrationNotReadyError):
            rehearse(disabled_engine, disabled_key)

        with unknown_engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                text("UPDATE receiver_credential_migration_state SET state = 'unexpected'")
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(BackfillMigrationNotReadyError):
            rehearse(unknown_engine, unknown_key)

        assert new_counts(unexpected_engine) == (0, 0, 0)
        assert new_counts(disabled_engine) == (0, 0, 0)
    finally:
        missing_engine.dispose()
        unexpected_engine.dispose()
        disabled_engine.dispose()
        unknown_engine.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"hash_key": b"short"},
        {"hash_key_version": 0},
        {"hash_key_version": True},
        {"now": datetime(2026, 7, 24, 14, 30)},
        {
            "now": datetime(
                2026,
                7,
                24,
                14,
                30,
                tzinfo=timezone(timedelta(hours=5, minutes=30)),
            )
        },
    ],
)
def test_invalid_key_and_time_inputs_fail_before_writes(fleet, overrides: dict):
    engine, _, hash_key = fleet
    arguments = {"hash_key": hash_key, **overrides}
    with pytest.raises(BackfillValidationError):
        rehearse(engine, **arguments)
    assert new_counts(engine) == (0, 0, 0)
    assert migration_state(engine) == ("legacy_only", 1)


def test_broken_foreign_keys_fail_closed(tmp_path: Path):
    engine, _, hash_key = create_database(tmp_path, stores=[(51, True)])
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_devices (
                        public_id, store_id, display_name, status, enrolled_at,
                        disabled_at, created_by, created_at, updated_at
                    ) VALUES (:public_id, 999, 'Broken Device', 'active', :now, NULL, NULL, :now, :now)
                    """
                ),
                {"public_id": str(uuid.uuid4()), "now": FIXED_NOW.isoformat()},
            )
            connection.commit()
        with pytest.raises(BackfillValidationError):
            rehearse(engine, hash_key)
        assert migration_state(engine) == ("legacy_only", 1)
    finally:
        engine.dispose()


def test_second_execution_is_typed_no_write_error(fleet):
    engine, _, hash_key = fleet
    rehearse(engine, hash_key)
    counts_before = new_counts(engine)
    with pytest.raises(BackfillAlreadyAppliedError):
        rehearse(engine, hash_key)
    assert new_counts(engine) == counts_before
    assert migration_state(engine) == ("backfilled", 1)


def test_concurrent_calls_cannot_both_backfill(fleet):
    engine, _, hash_key = fleet
    start = threading.Barrier(2)

    def attempt():
        start.wait(timeout=5)
        try:
            return rehearse(engine, hash_key)
        except BackfillAlreadyAppliedError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt(), range(2)))

    assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, BackfillAlreadyAppliedError) for outcome in outcomes) == 1
    assert new_counts(engine) == (3, 3, 7)
    assert migration_state(engine) == ("backfilled", 1)


def test_protected_database_is_refused_before_connection():
    before = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    engine = create_engine(f"sqlite:///{REAL_DATABASE}")
    try:
        with pytest.raises(ProtectedDatabaseError):
            rehearse(engine, secrets.token_bytes(48))
    finally:
        engine.dispose()
    after = (
        (REAL_DATABASE.stat().st_size, REAL_DATABASE.stat().st_mtime_ns)
        if REAL_DATABASE.exists()
        else None
    )
    assert after == before
