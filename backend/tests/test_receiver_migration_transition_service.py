from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import logging
import os
import secrets
import sys
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from migrations import PROTECTED_DATABASE_PATH, run_receiver_credential_phase_one
from receiver_auth_service import (
    VerificationSource,
    authenticate_receiver_credential,
)
from receiver_credential_backfill import rehearse_legacy_receiver_backfill
from receiver_device_service import enroll_receiver_device
from receiver_migration_transition_service import (
    ActiveConnectionBlockerError,
    ActiveReceiverConnectionSummary,
    InvalidConnectionSummaryError,
    InvalidStateTransitionError,
    MigrationTransitionResult,
    ProtectedDatabaseError,
    RuntimeAction,
    StaleConnectionSummaryError,
    TransitionAlreadyAppliedError,
    TransitionMigrationNotReadyError,
    TransitionPersistenceError,
    TransitionReadinessError,
    TransitionStateMismatchError,
    transition_receiver_migration_state,
)


UTC_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class TransitionFixture:
    engine: Engine
    active_token: str
    inactive_token: str
    backfill_key: bytes
    enrollment_key: bytes
    enrollment_token: str | None = None

    @property
    def keys(self) -> dict[int, bytes]:
        return {1: self.backfill_key, 2: self.enrollment_key}


def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _create_legacy_schema(
    engine: Engine,
    *,
    include_stores: bool = True,
    enforce_token_uniqueness: bool = True,
) -> tuple[str, str]:
    active_token = uuid.uuid4().hex
    inactive_token = uuid.uuid4().hex
    timestamp = UTC_NOW.isoformat()
    with engine.begin() as connection:
        token_constraint = " UNIQUE" if enforce_token_uniqueness else ""
        connection.exec_driver_sql(
            """
            CREATE TABLE hq_users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(80) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(120) NOT NULL,
                role VARCHAR(30) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at VARCHAR(40) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE stores (
                id INTEGER PRIMARY KEY,
                store_code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(120) NOT NULL,
                city VARCHAR(80) NOT NULL,
                region VARCHAR(80) NOT NULL,
                is_online_store BOOLEAN NOT NULL DEFAULT 0,
                receiver_token VARCHAR(64) NOT NULL{token_constraint},
                is_active BOOLEAN NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'offline',
                last_seen VARCHAR(40),
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
            """
        )
        connection.execute(
            text(
                "INSERT INTO hq_users "
                "(id, username, password_hash, full_name, role, is_active, created_at) "
                "VALUES (1, 'transition-actor', 'test-only-value', "
                "'Transition Actor', 'admin', 1, :now)"
            ),
            {"now": timestamp},
        )
        if include_stores:
            connection.execute(
                text(
                    """
                    INSERT INTO stores (
                        id, store_code, name, city, region, is_online_store,
                        receiver_token, is_active, status, last_seen, created_at, updated_at
                    ) VALUES (
                        :id, :code, :name, 'Test City', 'Test Region', :online,
                        :token, :active, :status, :last_seen, :now, :now
                    )
                    """
                ),
                [
                    {
                        "id": 11,
                        "code": "ACTIVE-11",
                        "name": "Active Test Store",
                        "online": 1,
                        "token": active_token,
                        "active": 1,
                        "status": "playing",
                        "last_seen": timestamp,
                        "now": timestamp,
                    },
                    {
                        "id": 12,
                        "code": "INACTIVE-12",
                        "name": "Inactive Test Store",
                        "online": 0,
                        "token": inactive_token,
                        "active": 0,
                        "status": "offline",
                        "last_seen": None,
                        "now": timestamp,
                    },
                ],
            )
    return active_token, inactive_token


def _new_fixture(
    tmp_path: Path,
    name: str,
    *,
    include_stores: bool = True,
    backfill: bool = True,
    enrollment: bool = False,
    enforce_token_uniqueness: bool = True,
) -> TransitionFixture:
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    active_token, inactive_token = _create_legacy_schema(
        engine,
        include_stores=include_stores,
        enforce_token_uniqueness=enforce_token_uniqueness,
    )
    run_receiver_credential_phase_one(engine)
    fixture = TransitionFixture(
        engine=engine,
        active_token=active_token,
        inactive_token=inactive_token,
        backfill_key=secrets.token_bytes(48),
        enrollment_key=secrets.token_bytes(48),
    )
    if backfill and include_stores:
        rehearse_legacy_receiver_backfill(
            engine,
            hash_key=fixture.backfill_key,
            hash_key_version=1,
            now=UTC_NOW,
        )
    if enrollment:
        _set_state(engine, "legacy_only", 1)
        result = enroll_receiver_device(
            engine,
            store_id=11,
            display_name="Transition test receiver",
            actor_user_id=1,
            hash_key=fixture.enrollment_key,
            hash_key_version=2,
            now=UTC_NOW,
        )
        fixture.enrollment_token = result.take_raw_credential()
        _set_state(engine, "backfilled", 1)
    return fixture


def _set_state(engine: Engine, state: str, enabled: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state "
                "SET state = :state, legacy_verification_enabled = :enabled, updated_at = :now"
            ),
            {"state": state, "enabled": enabled, "now": UTC_NOW.isoformat()},
        )


def _summary(*, legacy: int = 0, hashed: int = 0, age_seconds: int = 0):
    return ActiveReceiverConnectionSummary(
        legacy_authenticated_count=legacy,
        hashed_authenticated_count=hashed,
        captured_at=UTC_NOW - timedelta(seconds=age_seconds),
    )


def _transition(
    fixture: TransitionFixture,
    current: str,
    target: str,
    *,
    summary: ActiveReceiverConnectionSummary | None = None,
    keys: dict[int, bytes] | None = None,
    step_hook=None,
) -> MigrationTransitionResult:
    return transition_receiver_migration_state(
        fixture.engine,
        expected_current_state=current,
        target_state=target,
        actor_user_id=1,
        hash_keys=fixture.keys if keys is None else keys,
        active_connections=summary,
        now=UTC_NOW,
        step_hook=step_hook,
    )


def _state(engine: Engine) -> tuple[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, legacy_verification_enabled "
                "FROM receiver_credential_migration_state WHERE id = 1"
            )
        ).one()
    return row.state, row.legacy_verification_enabled


def _rows(engine: Engine, table: str) -> tuple[tuple, ...]:
    with engine.connect() as connection:
        return tuple(connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1'))


def _protected_snapshot(engine: Engine) -> dict[str, tuple[tuple, ...]]:
    return {
        table: _rows(engine, table)
        for table in ("stores", "receiver_devices", "receiver_credentials", "schema_migrations")
    }


def test_backfilled_to_dual_verify_updates_only_state_and_one_audit(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "expand-dual.db")
    protected_before = _protected_snapshot(fixture.engine)
    events_before = _rows(fixture.engine, "receiver_credential_events")

    result = _transition(fixture, "backfilled", "dual_verify")

    assert _state(fixture.engine) == ("dual_verify", 1)
    assert result.runtime_action is RuntimeAction.NONE
    assert _protected_snapshot(fixture.engine) == protected_before
    events_after = _rows(fixture.engine, "receiver_credential_events")
    assert events_after[:-1] == events_before
    assert len(events_after) == len(events_before) + 1
    metadata = json.loads(events_after[-1][11])
    assert metadata == {
        "actor_user_id": 1,
        "migration_phase": "dual_verify",
        "outcome": "success",
        "reason": "enable_dual_verification",
    }


def test_dual_verify_to_hash_only_requires_fresh_zero_legacy_summary(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "narrow-hash.db", enrollment=True)
    _transition(fixture, "backfilled", "dual_verify")

    result = _transition(
        fixture,
        "dual_verify",
        "hash_only",
        summary=_summary(legacy=0, hashed=2, age_seconds=30),
    )

    assert _state(fixture.engine) == ("hash_only", 0)
    assert result.runtime_action is RuntimeAction.LEGACY_CONNECTIONS_MUST_BE_ZERO
    assert result.active_store_count == 1
    assert result.active_device_count == 2


@pytest.mark.parametrize(
    ("summary", "error_type"),
    [
        (None, StaleConnectionSummaryError),
        (_summary(age_seconds=31), StaleConnectionSummaryError),
        (_summary(legacy=1), ActiveConnectionBlockerError),
    ],
)
def test_hash_only_transition_rejects_missing_stale_or_legacy_connections(
    tmp_path: Path, summary, error_type
):
    fixture = _new_fixture(tmp_path, "hash-connection-block.db")
    _transition(fixture, "backfilled", "dual_verify")
    before = _rows(fixture.engine, "receiver_credential_events")
    with pytest.raises(error_type):
        _transition(fixture, "dual_verify", "hash_only", summary=summary)
    assert _state(fixture.engine) == ("dual_verify", 1)
    assert _rows(fixture.engine, "receiver_credential_events") == before


@pytest.mark.parametrize(
    "mutation",
    [
        "no_active_device",
        "active_device_without_credential",
        "no_usable_credential",
        "malformed_hash",
    ],
)
def test_hash_readiness_failures_roll_back_without_audit(tmp_path: Path, mutation: str):
    fixture = _new_fixture(
        tmp_path,
        f"hash-readiness-{mutation}.db",
        enrollment=mutation == "active_device_without_credential",
    )
    _transition(fixture, "backfilled", "dual_verify")
    with fixture.engine.begin() as connection:
        if mutation == "no_active_device":
            connection.execute(
                text(
                    "UPDATE receiver_devices SET status = 'disabled', disabled_at = :now "
                    "WHERE store_id = 11"
                ),
                {"now": UTC_NOW.isoformat()},
            )
        elif mutation == "active_device_without_credential":
            connection.execute(
                text(
                    "DELETE FROM receiver_credentials WHERE token_format = 'echocast_rcv'"
                )
            )
        elif mutation == "no_usable_credential":
            connection.execute(
                text(
                    "UPDATE receiver_credentials SET status = 'revoked', revoked_at = :now "
                    "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
                ),
                {"now": UTC_NOW.isoformat()},
            )
        else:
            connection.execute(
                text(
                    "UPDATE receiver_credentials SET token_hash = :bad "
                    "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
                ),
                {"bad": "x" * 64},
            )
    before = _rows(fixture.engine, "receiver_credential_events")
    with pytest.raises(TransitionReadinessError):
        _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    assert _state(fixture.engine) == ("dual_verify", 1)
    assert _rows(fixture.engine, "receiver_credential_events") == before


def test_hash_readiness_requires_every_key_version(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "missing-key.db", enrollment=True)
    _transition(fixture, "backfilled", "dual_verify")
    with pytest.raises(TransitionReadinessError):
        _transition(
            fixture,
            "dual_verify",
            "hash_only",
            summary=_summary(),
            keys={1: fixture.backfill_key},
        )


def test_hash_only_rolls_back_to_dual_when_raw_readiness_remains(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "rollback-dual.db")
    _transition(fixture, "backfilled", "dual_verify")
    _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    result = _transition(fixture, "hash_only", "dual_verify")
    assert _state(fixture.engine) == ("dual_verify", 1)
    assert result.runtime_action is RuntimeAction.NONE


@pytest.mark.parametrize("bad_token", ["invalid", "A" * 32, ""])
def test_hash_only_rollback_rejects_invalid_raw_store_tokens(tmp_path: Path, bad_token: str):
    fixture = _new_fixture(tmp_path, "invalid-raw.db")
    _transition(fixture, "backfilled", "dual_verify")
    _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    with fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE stores SET receiver_token = :token WHERE id = 11"),
            {"token": bad_token},
        )
    with pytest.raises(TransitionReadinessError):
        _transition(fixture, "hash_only", "dual_verify")
    assert _state(fixture.engine) == ("hash_only", 0)


def test_hash_only_rollback_rejects_duplicate_raw_store_tokens(tmp_path: Path):
    fixture = _new_fixture(
        tmp_path,
        "duplicate-raw.db",
        enforce_token_uniqueness=False,
    )
    _transition(fixture, "backfilled", "dual_verify")
    _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    with fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE stores SET receiver_token = :token WHERE id = 12"),
            {"token": fixture.active_token},
        )
    with pytest.raises(TransitionReadinessError):
        _transition(fixture, "hash_only", "dual_verify")
    assert _state(fixture.engine) == ("hash_only", 0)


def test_hash_only_rollback_rejects_incomplete_store_mapping(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "incomplete-rollback.db")
    _transition(fixture, "backfilled", "dual_verify")
    _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    with fixture.engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM receiver_credentials WHERE device_id IN "
                "(SELECT id FROM receiver_devices WHERE store_id = 12)"
            )
        )
    with pytest.raises(TransitionReadinessError):
        _transition(fixture, "hash_only", "dual_verify")
    assert _state(fixture.engine) == ("hash_only", 0)


def test_dual_verify_rolls_back_to_backfilled_with_zero_hashed_connections(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "rollback-backfilled.db")
    _transition(fixture, "backfilled", "dual_verify")
    result = _transition(
        fixture,
        "dual_verify",
        "backfilled",
        summary=_summary(legacy=2, hashed=0),
    )
    assert _state(fixture.engine) == ("backfilled", 1)
    assert result.runtime_action is RuntimeAction.HASHED_CONNECTIONS_MUST_BE_ZERO


def test_backfilled_rollback_rejects_active_hashed_connections(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "hashed-blocker.db")
    _transition(fixture, "backfilled", "dual_verify")
    with pytest.raises(ActiveConnectionBlockerError):
        _transition(
            fixture,
            "dual_verify",
            "backfilled",
            summary=_summary(hashed=1),
        )


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("backfilled", "hash_only"),
        ("hash_only", "backfilled"),
        ("legacy_only", "dual_verify"),
        ("dual_verify", "raw_neutralized"),
        ("raw_neutralized", "hash_only"),
    ],
)
def test_forbidden_transitions_are_typed_and_write_nothing(
    tmp_path: Path, current: str, target: str
):
    fixture = _new_fixture(tmp_path, f"forbidden-{current}-{target}.db")
    enabled = 0 if current in {"hash_only", "raw_neutralized"} else 1
    _set_state(fixture.engine, current, enabled)
    before = _rows(fixture.engine, "receiver_credential_events")
    with pytest.raises(InvalidStateTransitionError):
        _transition(fixture, current, target, summary=_summary())
    assert _state(fixture.engine) == (current, enabled)
    assert _rows(fixture.engine, "receiver_credential_events") == before


def test_same_state_and_stale_expected_state_have_distinct_typed_errors(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "state-errors.db")
    with pytest.raises(TransitionAlreadyAppliedError):
        _transition(fixture, "backfilled", "backfilled")
    with pytest.raises(TransitionStateMismatchError):
        _transition(fixture, "dual_verify", "hash_only", summary=_summary())


def test_state_flag_inconsistency_fails_closed(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "bad-flag.db")
    _set_state(fixture.engine, "backfilled", 0)
    with pytest.raises(TransitionMigrationNotReadyError):
        _transition(fixture, "backfilled", "dual_verify")


def test_unknown_persisted_state_fails_closed(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "unknown-state.db")
    with fixture.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text("UPDATE receiver_credential_migration_state SET state = 'unknown' WHERE id = 1")
        )
    with pytest.raises(TransitionMigrationNotReadyError):
        _transition(fixture, "backfilled", "dual_verify")


@pytest.mark.parametrize("actor_change", ["missing", "inactive"])
def test_missing_or_inactive_actor_fails_without_writes(tmp_path: Path, actor_change: str):
    fixture = _new_fixture(tmp_path, f"actor-{actor_change}.db")
    with fixture.engine.begin() as connection:
        if actor_change == "missing":
            connection.execute(text("DELETE FROM hq_users WHERE id = 1"))
        else:
            connection.execute(text("UPDATE hq_users SET is_active = 0 WHERE id = 1"))
    before = _rows(fixture.engine, "receiver_credential_events")
    with pytest.raises(TransitionReadinessError):
        _transition(fixture, "backfilled", "dual_verify")
    assert _rows(fixture.engine, "receiver_credential_events") == before


def test_missing_schema_and_empty_store_fleet_fail_closed(tmp_path: Path):
    missing = create_engine(f"sqlite:///{tmp_path / 'missing-schema.db'}")
    with pytest.raises(TransitionMigrationNotReadyError):
        transition_receiver_migration_state(
            missing,
            expected_current_state="backfilled",
            target_state="dual_verify",
            actor_user_id=1,
            hash_keys={1: secrets.token_bytes(48)},
            active_connections=None,
            now=UTC_NOW,
        )

    empty = _new_fixture(
        tmp_path,
        "empty-fleet.db",
        include_stores=False,
        backfill=False,
    )
    _set_state(empty.engine, "backfilled", 1)
    with pytest.raises(TransitionReadinessError):
        _transition(empty, "backfilled", "dual_verify")


def test_broken_foreign_keys_fail_closed(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "broken-fk.db")
    with fixture.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("UPDATE receiver_devices SET store_id = 999 WHERE store_id = 11"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(TransitionMigrationNotReadyError):
        _transition(fixture, "backfilled", "dual_verify")


@pytest.mark.parametrize(
    "failure_step",
    [
        "before_state_update",
        "after_state_update",
        "before_audit_insert",
        "after_audit_insert",
        "after_validation_before_commit",
    ],
)
def test_deliberate_failures_roll_back_state_and_audit(tmp_path: Path, failure_step: str):
    fixture = _new_fixture(tmp_path, f"rollback-{failure_step}.db")
    state_before = _state(fixture.engine)
    events_before = _rows(fixture.engine, "receiver_credential_events")
    protected_before = _protected_snapshot(fixture.engine)

    def fail(step: str) -> None:
        if step == failure_step:
            raise RuntimeError("injected transition failure")

    with pytest.raises(TransitionPersistenceError):
        _transition(
            fixture,
            "backfilled",
            "dual_verify",
            step_hook=fail,
        )
    assert _state(fixture.engine) == state_before
    assert _rows(fixture.engine, "receiver_credential_events") == events_before
    assert _protected_snapshot(fixture.engine) == protected_before


def test_result_is_immutable_redacted_and_contains_only_counts(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "result.db")
    result = _transition(fixture, "backfilled", "dual_verify")
    assert result.previous_state == "backfilled"
    assert result.new_state == "dual_verify"
    assert result.store_count == 2
    assert result.active_store_count == 1
    with pytest.raises(FrozenInstanceError):
        result.new_state = "hash_only"
    rendered = repr(result) + str(result)
    assert fixture.active_token not in rendered
    assert fixture.backfill_key.hex() not in rendered
    assert "authorization" not in rendered.lower()


def test_secrets_never_reach_errors_logs_output_or_transition_audit(
    tmp_path: Path, capsys, caplog
):
    fixture = _new_fixture(tmp_path, "secret-safety.db")
    caplog.set_level(logging.DEBUG)
    with fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE stores SET receiver_token = 'invalid' WHERE id = 11")
        )
    with pytest.raises(TransitionReadinessError) as captured:
        _transition(fixture, "backfilled", "dual_verify")
    output = capsys.readouterr()
    rendered = str(captured.value) + repr(captured.value) + output.out + output.err + caplog.text
    assert fixture.active_token not in rendered
    assert fixture.backfill_key.hex() not in rendered
    assert "authorization" not in rendered.lower()


def test_protected_database_is_refused_before_connection():
    engine = create_engine(f"sqlite:///{PROTECTED_DATABASE_PATH}")
    connected = False

    def record_connection(*_args, **_kwargs):
        nonlocal connected
        connected = True

    event.listen(engine, "connect", record_connection)
    with pytest.raises(ProtectedDatabaseError):
        transition_receiver_migration_state(
            engine,
            expected_current_state="backfilled",
            target_state="dual_verify",
            actor_user_id=1,
            hash_keys={1: secrets.token_bytes(48)},
            active_connections=None,
            now=UTC_NOW,
        )
    assert connected is False


def test_concurrent_transition_attempts_create_one_audit(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "concurrent.db")
    before_count = len(_rows(fixture.engine, "receiver_credential_events"))

    def attempt(_index: int):
        return _transition(fixture, "backfilled", "dual_verify")

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = []
        for future in [executor.submit(attempt, index) for index in range(2)]:
            try:
                outcomes.append(future.result())
            except TransitionStateMismatchError as error:
                outcomes.append(error)
    assert sum(isinstance(item, MigrationTransitionResult) for item in outcomes) == 1
    assert sum(isinstance(item, TransitionStateMismatchError) for item in outcomes) == 1
    assert len(_rows(fixture.engine, "receiver_credential_events")) == before_count + 1


def test_isolated_authentication_observes_transitioned_temporary_state(tmp_path: Path):
    fixture = _new_fixture(tmp_path, "auth-observation.db")
    before = authenticate_receiver_credential(
        fixture.engine,
        presented_token=fixture.active_token,
        hash_keys=fixture.keys,
        now=UTC_NOW,
    )
    assert before.verification_source is VerificationSource.LEGACY_STORE_TOKEN
    _transition(fixture, "backfilled", "dual_verify")
    during = authenticate_receiver_credential(
        fixture.engine,
        presented_token=fixture.active_token,
        hash_keys=fixture.keys,
        now=UTC_NOW,
    )
    assert during.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL
    _transition(fixture, "dual_verify", "hash_only", summary=_summary())
    after = authenticate_receiver_credential(
        fixture.engine,
        presented_token=fixture.active_token,
        hash_keys=fixture.keys,
        now=UTC_NOW,
    )
    assert after.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL


def test_focused_service_imports_no_runtime_and_changes_no_health_state(tmp_path: Path):
    # Asked in a fresh interpreter, not of this one. The property under test is
    # "importing this service does not drag in the runtime", which is a fact
    # about the module's imports - but reading the ambient sys.modules made it a
    # fact about whatever else happened to run first in this pytest worker, and
    # any test file that imports ws_manager would fail it from a distance.
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, %r); "
        "import receiver_migration_transition_service; "
        "print('server' in sys.modules, 'ws_manager' in sys.modules)"
        % str(Path(__file__).resolve().parents[1])
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ECHOCAST_DB_PATH": str(tmp_path / "probe.db")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False", (
        f"the focused service pulled in the runtime: {completed.stdout.strip()}"
    )

    fixture = _new_fixture(tmp_path, "runtime-boundary.db")
    stores_before = _rows(fixture.engine, "stores")
    _transition(fixture, "backfilled", "dual_verify")
    assert _rows(fixture.engine, "stores") == stores_before
    result = _transition(
        fixture,
        "dual_verify",
        "backfilled",
        summary=_summary(),
    )
    assert not hasattr(result, "readiness")
    assert not hasattr(result, "playback")
    assert not hasattr(result, "speaker_verified")


@pytest.mark.parametrize(
    "bad_arguments",
    [
        {"legacy_authenticated_count": True},
        {"hashed_authenticated_count": -1},
        {"captured_at": datetime(2026, 7, 24, 12, 0)},
        {"captured_at": datetime(2026, 7, 24, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))},
    ],
)
def test_connection_summary_is_strict_immutable_and_utc(bad_arguments: dict):
    arguments = {
        "legacy_authenticated_count": 0,
        "hashed_authenticated_count": 0,
        "captured_at": UTC_NOW,
    }
    arguments.update(bad_arguments)
    with pytest.raises(InvalidConnectionSummaryError):
        ActiveReceiverConnectionSummary(**arguments)

    valid = _summary()
    with pytest.raises(FrozenInstanceError):
        valid.legacy_authenticated_count = 1


@pytest.mark.parametrize(
    "invalid_keys",
    [
        {},
        {0: secrets.token_bytes(48)},
        {1: b"short"},
        {version: secrets.token_bytes(48) for version in range(1, 18)},
    ],
)
def test_key_ring_is_bounded_and_strict(tmp_path: Path, invalid_keys: dict):
    fixture = _new_fixture(tmp_path, "invalid-key-ring.db")
    with pytest.raises(TransitionMigrationNotReadyError):
        _transition(
            fixture,
            "backfilled",
            "dual_verify",
            keys=invalid_keys,
        )
