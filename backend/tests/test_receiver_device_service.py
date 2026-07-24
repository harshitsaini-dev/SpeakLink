"""Isolated transactional tests for Receiver Device enrollment Phase 2."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hmac
import json
from pathlib import Path
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from migrations import MIGRATION_STATE_LEGACY_ONLY, run_receiver_credential_phase_one
from receiver_credentials import parse_receiver_token, verify_receiver_token
from receiver_device_service import (
    ActorNotFoundError,
    CredentialAlreadyDeliveredError,
    DeviceLimitExceededError,
    EnrollmentPersistenceError,
    InactiveActorError,
    InactiveStoreError,
    InvalidEnrollmentRequestError,
    MigrationNotReadyError,
    StoreNotFoundError,
    enroll_receiver_device,
)


REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"
TEST_HASH_KEY = b"phase-two-isolated-test-key-material-at-least-32-bytes"
OTHER_SECRET_TEXT = "Authorization: Bearer forbidden-test-value"
FIXED_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session", autouse=True)
def real_database_metadata_is_unchanged():
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


def seed_legacy_rows(engine: Engine, *, store_active: bool = True, actor_active: bool = True) -> str:
    legacy_token = "0123456789abcdef0123456789abcdef"
    timestamp = FIXED_NOW.isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO hq_users (
                    id, username, password_hash, role, is_active, created_at
                ) VALUES (7, 'phase2-actor', 'test-password-hash', 'admin', :active, :now)
                """
            ),
            {"active": int(actor_active), "now": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO stores (
                    id, store_code, store_name, city, region, is_online_store,
                    receiver_token, is_active, status, last_seen, created_at, updated_at
                ) VALUES (
                    41, 'STORE-041', 'Enrollment Store', 'Pune', 'West', 0,
                    :receiver_token, :active, 'offline', NULL, :now, :now
                )
                """
            ),
            {"receiver_token": legacy_token, "active": int(store_active), "now": timestamp},
        )
    return legacy_token


@pytest.fixture
def isolated_engine(tmp_path: Path) -> Engine:
    database_path = tmp_path / "receiver-enrollment.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    create_legacy_schema(engine)
    seed_legacy_rows(engine)
    run_receiver_credential_phase_one(engine)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        yield engine
    finally:
        engine.dispose()


def enroll(engine: Engine, **overrides):
    arguments = {
        "store_id": 41,
        "display_name": "Primary Receiver",
        "actor_user_id": 7,
        "hash_key": TEST_HASH_KEY,
        "hash_key_version": 3,
        "now": FIXED_NOW,
    }
    arguments.update(overrides)
    return enroll_receiver_device(engine, **arguments)


def enrollment_counts(engine: Engine) -> tuple[int, int, int]:
    with engine.connect() as connection:
        return (
            connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar_one(),
            connection.execute(text("SELECT COUNT(*) FROM receiver_credentials")).scalar_one(),
            connection.execute(text("SELECT COUNT(*) FROM receiver_credential_events")).scalar_one(),
        )


def test_success_returns_one_time_redacted_credential_and_persists_hash_only(
    isolated_engine: Engine,
):
    with isolated_engine.connect() as connection:
        legacy_before = connection.execute(
            text("SELECT receiver_token FROM stores WHERE id = 41")
        ).scalar_one()
        ledger_before = connection.execute(
            text("SELECT version, name, applied_at FROM schema_migrations")
        ).all()

    result = enroll(isolated_engine)
    rendered_before_delivery = repr(result)
    raw_credential = result.take_raw_credential()
    parsed = parse_receiver_token(raw_credential)

    assert result.credential_version == 1
    assert parsed.public_id == result.credential_public_id
    assert len(result.device_public_id) == 36
    assert "<redacted>" in rendered_before_delivery
    if raw_credential in rendered_before_delivery or raw_credential in str(result):
        pytest.fail("Enrollment result representation exposed credential material", pytrace=False)
    with pytest.raises(CredentialAlreadyDeliveredError):
        result.take_raw_credential()

    with isolated_engine.connect() as connection:
        device = connection.execute(
            text(
                "SELECT public_id, store_id, display_name, status, created_by "
                "FROM receiver_devices"
            )
        ).one()
        credential = connection.execute(
            text(
                """
                SELECT public_id, credential_version, token_format, token_hash,
                       hash_key_version, status, expiry_policy, expires_at
                FROM receiver_credentials
                """
            )
        ).one()
        audits = connection.execute(
            text(
                """
                SELECT event_type, outcome, reason_code, metadata_json
                FROM receiver_credential_events ORDER BY id
                """
            )
        ).all()
        legacy_after = connection.execute(
            text("SELECT receiver_token FROM stores WHERE id = 41")
        ).scalar_one()
        migration_state = connection.execute(
            text("SELECT state FROM receiver_credential_migration_state WHERE id = 1")
        ).scalar_one()
        ledger_after = connection.execute(
            text("SELECT version, name, applied_at FROM schema_migrations")
        ).all()

    assert device == (result.device_public_id, 41, "Primary Receiver", "active", 7)
    assert credential.public_id == result.credential_public_id
    assert credential.credential_version == 1
    assert credential.token_format == "echocast_rcv"
    assert credential.hash_key_version == 3
    assert credential.status == "active"
    assert credential.expiry_policy == "non_expiring"
    assert credential.expires_at is None
    if not verify_receiver_token(raw_credential, credential.token_hash, TEST_HASH_KEY):
        pytest.fail("Returned credential did not verify against its stored hash", pytrace=False)
    if raw_credential in credential.token_hash:
        pytest.fail("Persisted credential hash contained raw credential material", pytrace=False)
    assert [row.event_type for row in audits] == ["device_enrolled", "credential_issued"]
    assert all(row.outcome == "success" for row in audits)
    assert all(row.reason_code == "initial_enrollment" for row in audits)
    for audit in audits:
        metadata = json.loads(audit.metadata_json)
        assert metadata["store_id"] == 41
        assert metadata["actor_user_id"] == 7
        if raw_credential in audit.metadata_json or credential.token_hash in audit.metadata_json:
            pytest.fail("Audit metadata exposed credential material", pytrace=False)
    assert hmac.compare_digest(legacy_before, legacy_after)
    assert migration_state == MIGRATION_STATE_LEGACY_ONLY
    assert ledger_after == ledger_before
    assert enrollment_counts(isolated_engine) == (1, 1, 2)


def test_expiring_policy_and_trimmed_display_name(isolated_engine: Engine):
    expires_at = FIXED_NOW + timedelta(days=90)
    result = enroll(
        isolated_engine,
        display_name="  Back Office Receiver  ",
        expires_at=expires_at,
    )
    result.take_raw_credential()
    with isolated_engine.connect() as connection:
        device_name = connection.execute(text("SELECT display_name FROM receiver_devices")).scalar_one()
        policy, stored_expiry = connection.execute(
            text("SELECT expiry_policy, expires_at FROM receiver_credentials")
        ).one()
    assert device_name == "Back Office Receiver"
    assert policy == "expires_at"
    assert stored_expiry == expires_at.isoformat()


@pytest.mark.parametrize(
    ("store_active", "store_id", "error_type"),
    [(True, 999, StoreNotFoundError), (False, 41, InactiveStoreError)],
)
def test_missing_or_inactive_store_is_rejected_without_writes(
    tmp_path: Path,
    store_active: bool,
    store_id: int,
    error_type: type[Exception],
):
    engine = create_engine(f"sqlite:///{tmp_path / 'store-precondition.db'}")
    try:
        create_legacy_schema(engine)
        seed_legacy_rows(engine, store_active=store_active)
        run_receiver_credential_phase_one(engine)
        with pytest.raises(error_type):
            enroll(engine, store_id=store_id)
        assert enrollment_counts(engine) == (0, 0, 0)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("actor_active", "actor_id", "error_type"),
    [(True, 999, ActorNotFoundError), (False, 7, InactiveActorError)],
)
def test_missing_or_inactive_actor_is_rejected_without_writes(
    tmp_path: Path,
    actor_active: bool,
    actor_id: int,
    error_type: type[Exception],
):
    engine = create_engine(f"sqlite:///{tmp_path / 'actor-precondition.db'}")
    try:
        create_legacy_schema(engine)
        seed_legacy_rows(engine, actor_active=actor_active)
        run_receiver_credential_phase_one(engine)
        with pytest.raises(error_type):
            enroll(engine, actor_user_id=actor_id)
        assert enrollment_counts(engine) == (0, 0, 0)
    finally:
        engine.dispose()


def test_third_active_device_is_rejected(isolated_engine: Engine):
    enroll(isolated_engine)
    enroll(isolated_engine, display_name="Secondary Receiver")
    with pytest.raises(DeviceLimitExceededError):
        enroll(isolated_engine, display_name="Third Receiver")
    assert enrollment_counts(isolated_engine) == (2, 2, 4)


def test_disabled_device_does_not_count_toward_active_limit(isolated_engine: Engine):
    enroll(isolated_engine)
    enroll(isolated_engine, display_name="Secondary Receiver")
    with isolated_engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE receiver_devices
                SET status = 'disabled', disabled_at = :now, updated_at = :now
                WHERE display_name = 'Secondary Receiver'
                """
            ),
            {"now": FIXED_NOW.isoformat()},
        )
    enroll(isolated_engine, display_name="Replacement Receiver")
    with isolated_engine.connect() as connection:
        active_count = connection.execute(
            text("SELECT COUNT(*) FROM receiver_devices WHERE store_id = 41 AND status = 'active'")
        ).scalar_one()
    assert active_count == 2


@pytest.mark.parametrize("failure_step", ["after_device_insert", "after_credential_insert"])
def test_mid_enrollment_failure_rolls_back_every_row(
    isolated_engine: Engine,
    failure_step: str,
):
    def fail(step: str) -> None:
        if step == failure_step:
            raise RuntimeError("deliberate isolated enrollment failure")

    with pytest.raises(EnrollmentPersistenceError) as captured:
        enroll(isolated_engine, step_hook=fail)
    assert str(captured.value) == "receiver enrollment could not be persisted"
    assert enrollment_counts(isolated_engine) == (0, 0, 0)


@pytest.mark.parametrize(
    "expires_at",
    [
        datetime(2026, 8, 1, 12, 0),
        datetime(2026, 8, 1, 12, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        FIXED_NOW,
    ],
)
def test_invalid_expiry_is_rejected_without_writes(
    isolated_engine: Engine,
    expires_at: datetime,
):
    with pytest.raises(InvalidEnrollmentRequestError):
        enroll(isolated_engine, expires_at=expires_at)
    assert enrollment_counts(isolated_engine) == (0, 0, 0)


@pytest.mark.parametrize(
    "display_name",
    ["", "   ", "line one\nline two", "x" * 201, 123],
)
def test_invalid_display_name_is_rejected_without_writes(
    isolated_engine: Engine,
    display_name,
):
    with pytest.raises(InvalidEnrollmentRequestError):
        enroll(isolated_engine, display_name=display_name)
    assert enrollment_counts(isolated_engine) == (0, 0, 0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"hash_key": b"short"},
        {"hash_key_version": 0},
        {"hash_key_version": True},
        {"now": datetime(2026, 7, 24, 12, 0)},
    ],
)
def test_invalid_key_inputs_and_now_are_rejected_without_writes(
    isolated_engine: Engine,
    overrides: dict,
):
    with pytest.raises(InvalidEnrollmentRequestError):
        enroll(isolated_engine, **overrides)
    assert enrollment_counts(isolated_engine) == (0, 0, 0)


def test_secrets_never_appear_in_database_output_logs_repr_or_errors(
    isolated_engine: Engine,
    capsys,
    caplog,
):
    for unsafe_name in (
        OTHER_SECRET_TEXT,
        TEST_HASH_KEY.decode("ascii"),
        "echocast_rcv_v1.credential-material",
    ):
        with pytest.raises(InvalidEnrollmentRequestError) as error:
            enroll(isolated_engine, display_name=unsafe_name)
        if unsafe_name in str(error.value):
            pytest.fail("Enrollment error reflected protected input", pytrace=False)

    result = enroll(isolated_engine)
    raw_credential = result.take_raw_credential()
    with isolated_engine.connect() as connection:
        persisted_values = []
        for table_name in (
            "receiver_devices",
            "receiver_credentials",
            "receiver_credential_events",
        ):
            for row in connection.exec_driver_sql(f'SELECT * FROM "{table_name}"'):
                persisted_values.extend("" if value is None else str(value) for value in row)
    captured_output = capsys.readouterr()
    rendered_logs = " ".join(record.getMessage() for record in caplog.records)
    forbidden_values = (
        raw_credential,
        TEST_HASH_KEY.decode("ascii"),
        OTHER_SECRET_TEXT,
    )
    surfaces = (
        " ".join(persisted_values),
        repr(result),
        captured_output.out,
        captured_output.err,
        rendered_logs,
    )
    for forbidden in forbidden_values:
        if any(forbidden in surface for surface in surfaces):
            pytest.fail("Enrollment exposed protected credential material", pytrace=False)


def test_missing_or_unknown_phase_one_state_fails_closed(tmp_path: Path):
    missing_engine = create_engine(f"sqlite:///{tmp_path / 'missing-phase-one.db'}")
    unknown_engine = create_engine(f"sqlite:///{tmp_path / 'unknown-state.db'}")
    try:
        create_legacy_schema(missing_engine)
        seed_legacy_rows(missing_engine)
        with pytest.raises(MigrationNotReadyError):
            enroll(missing_engine)

        create_legacy_schema(unknown_engine)
        seed_legacy_rows(unknown_engine)
        run_receiver_credential_phase_one(unknown_engine)
        with unknown_engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                text("UPDATE receiver_credential_migration_state SET state = 'unexpected'")
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(MigrationNotReadyError):
            enroll(unknown_engine)
        assert enrollment_counts(unknown_engine) == (0, 0, 0)
    finally:
        missing_engine.dispose()
        unknown_engine.dispose()


def test_enrollment_does_not_change_store_runtime_status(isolated_engine: Engine):
    with isolated_engine.connect() as connection:
        before = connection.execute(
            text("SELECT status, last_seen, is_online_store FROM stores WHERE id = 41")
        ).one()
    enroll(isolated_engine)
    with isolated_engine.connect() as connection:
        after = connection.execute(
            text("SELECT status, last_seen, is_online_store FROM stores WHERE id = 41")
        ).one()
    assert after == before


def test_concurrent_enrollment_never_exceeds_two_active_devices(isolated_engine: Engine):
    start_barrier = threading.Barrier(3)

    def attempt(index: int):
        start_barrier.wait(timeout=5)
        try:
            return enroll(isolated_engine, display_name=f"Concurrent Receiver {index}")
        except DeviceLimitExceededError as error:
            return error

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(attempt, range(3)))

    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    rejections = [outcome for outcome in outcomes if isinstance(outcome, DeviceLimitExceededError)]
    assert len(successes) == 2
    assert len(rejections) == 1
    with isolated_engine.connect() as connection:
        active_count = connection.execute(
            text("SELECT COUNT(*) FROM receiver_devices WHERE store_id = 41 AND status = 'active'")
        ).scalar_one()
    assert active_count == 2
    assert enrollment_counts(isolated_engine) == (2, 2, 4)
