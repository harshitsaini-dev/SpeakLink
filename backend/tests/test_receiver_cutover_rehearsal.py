"""Isolated controlled Receiver credential cutover rehearsal tests."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from threading import Thread
import time
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
import uvicorn
import websockets

from migrations import PROTECTED_DATABASE_PATH, run_receiver_credential_phase_one
from receiver_connection_inventory import (
    ActiveReceiverConnectionInventory,
    AuthenticatedReceiverConnection,
    ConnectionAuthenticationSource,
)
from receiver_contract import AcousticState, ConnectionState, PlaybackState, ReadinessState
from receiver_credential_backfill import rehearse_legacy_receiver_backfill
from receiver_cutover_rehearsal import (
    CutoverStepCode,
    InvalidCutoverConfigurationError,
    ProtectedCutoverDatabaseError,
    ReceiverCutoverRehearsal,
)
from receiver_device_service import enroll_receiver_device
from receiver_migration_transition_service import (
    ActiveConnectionBlockerError,
    ActiveReceiverConnectionSummary,
    StaleConnectionSummaryError,
    TransitionPersistenceError,
    TransitionReadinessError,
    TransitionStateMismatchError,
)
from receiver_runtime_auth import (
    LegacyStoreTokenRuntimeAuthenticator,
    MigrationAwareReceiverRuntimeAuthenticator,
    ReceiverRuntimeAuthenticationError,
)


ISSUED_AT = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
REAL_DATABASE = Path(__file__).resolve().parents[1] / "speaklink_live.db"
RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


class SummaryOnlyManager:
    """Pure inventory adapter used outside the real loopback runtime fixture."""

    def __init__(self):
        self.receiver_connection_inventory = ActiveReceiverConnectionInventory()

    def get_active_receiver_transition_summary(self, now=None):
        return self.receiver_connection_inventory.build_transition_summary(
            ActiveReceiverConnectionSummary,
            captured_at=now or datetime.now(timezone.utc),
        )


def _metadata(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _enable_foreign_keys(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _create_legacy_schema(engine: Engine) -> dict[int, str]:
    tokens = {store_id: uuid.uuid4().hex for store_id in (11, 12, 13)}
    timestamp = ISSUED_AT.isoformat()
    with engine.begin() as connection:
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
        connection.execute(
            text(
                "INSERT INTO hq_users "
                "(id, username, password_hash, role, is_active, created_at) "
                "VALUES (1, 'cutover-actor', 'test-only', 'admin', 1, :now)"
            ),
            {"now": timestamp},
        )
        for store_id in (11, 12, 13):
            connection.execute(
                text(
                    "INSERT INTO stores "
                    "(id, store_code, store_name, city, region, is_online_store, "
                    "receiver_token, is_active, status, last_seen, created_at, updated_at) "
                    "VALUES (:id, :code, :name, 'Loopback', 'Test', 0, :token, "
                    ":active, 'offline', NULL, :now, :now)"
                ),
                {
                    "id": store_id,
                    "code": f"CUTOVER-{store_id}",
                    "name": f"Cutover Store {store_id}",
                    "token": tokens[store_id],
                    "active": 0 if store_id == 13 else 1,
                    "now": timestamp,
                },
            )
    return tokens


def _set_state(engine: Engine, state: str, enabled: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state "
                "SET state=:state, legacy_verification_enabled=:enabled, updated_at=:now "
                "WHERE id=1"
            ),
            {"state": state, "enabled": enabled, "now": ISSUED_AT.isoformat()},
        )


def _state(engine: Engine) -> tuple[str, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, legacy_verification_enabled "
                "FROM receiver_credential_migration_state WHERE id=1"
            )
        ).one()
    return row.state, row.legacy_verification_enabled


def _rows(engine: Engine, table: str) -> tuple[tuple, ...]:
    with engine.connect() as connection:
        return tuple(connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1'))


def _transition_audits(engine: Engine) -> tuple[tuple, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    "SELECT event_type, outcome, reason_code, metadata_json "
                    "FROM receiver_credential_events "
                    "WHERE event_type='migration_state_changed' ORDER BY id"
                )
            )
        )


def _store_health(engine: Engine, store_id: int):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT receiver_token, status, last_seen, is_online_store "
                "FROM stores WHERE id=:store_id"
            ),
            {"store_id": store_id},
        ).one()


def _credential_identity(engine: Engine, store_id: int, token_format: str):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT d.id AS device_id, c.id AS credential_id "
                "FROM receiver_devices d JOIN receiver_credentials c ON c.device_id=d.id "
                "WHERE d.store_id=:store_id AND c.token_format=:token_format"
            ),
            {"store_id": store_id, "token_format": token_format},
        ).one()


def _new_database(tmp_path: Path, name: str):
    database_path = tmp_path / name
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    tokens = _create_legacy_schema(engine)
    run_receiver_credential_phase_one(engine)
    keys = {1: secrets.token_bytes(48), 2: secrets.token_bytes(48)}
    rehearse_legacy_receiver_backfill(
        engine,
        hash_key=keys[1],
        hash_key_version=1,
        now=ISSUED_AT,
    )
    _set_state(engine, "legacy_only", 1)
    enrolled_tokens: dict[int, str] = {}
    for store_id in (11, 12):
        enrollment = enroll_receiver_device(
            engine,
            store_id=store_id,
            display_name=f"Cutover test device {store_id}",
            actor_user_id=1,
            hash_key=keys[2],
            hash_key_version=2,
            now=ISSUED_AT,
        )
        enrolled_tokens[store_id] = enrollment.take_raw_credential()
    _set_state(engine, "backfilled", 1)
    return SimpleNamespace(
        engine=engine,
        database_path=database_path,
        tokens=tokens,
        enrolled_tokens=enrolled_tokens,
        keys=keys,
    )


def _coordinator(fixture, *, manager=None, keys=None):
    selected_keys = fixture.keys if keys is None else keys
    selected_manager = manager or SummaryOnlyManager()
    authenticator = MigrationAwareReceiverRuntimeAuthenticator(
        fixture.engine,
        hash_keys=selected_keys,
    )
    rehearsal = ReceiverCutoverRehearsal(
        engine=fixture.engine,
        ws_manager=selected_manager,
        runtime_authenticator=authenticator,
        hash_keys=selected_keys,
        actor_user_id=1,
    )
    return rehearsal, selected_manager


def _random_loopback_port() -> int:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        return port_socket.getsockname()[1]


def _start_uvicorn(application: FastAPI, port: int):
    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        workers=1,
        log_level="critical",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not server.started:
        if not thread.is_alive():
            raise AssertionError("Isolated cutover server exited during startup")
        time.sleep(0.02)
    if not server.started:
        raise AssertionError("Isolated cutover server did not start")
    return server, thread


@pytest.fixture(scope="module")
def loopback_cutover(tmp_path_factory):
    real_before = _metadata(REAL_DATABASE)
    fixture = _new_database(tmp_path_factory.mktemp("cutover-loopback"), "cutover.db")
    backend_dir = Path(__file__).resolve().parents[1]
    environment = {
        "SPEAKLINK_DB_PATH": str(fixture.database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"cutover-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.path.insert(0, str(backend_dir))
    for module_name in RUNTIME_MODULES:
        sys.modules.pop(module_name, None)
    isolated_server = None
    thread = None
    try:
        server_module = importlib.import_module("server")
        db_module = importlib.import_module("db")
        ws_manager_module = importlib.import_module("ws_manager")
        assert Path(db_module.DB_PATH) == fixture.database_path.resolve()
        server_module.startup_event()

        inventory = ActiveReceiverConnectionInventory(max_connections=3)
        manager = ws_manager_module.WSManager(receiver_connection_inventory=inventory)
        authenticator = MigrationAwareReceiverRuntimeAuthenticator(
            fixture.engine, hash_keys=fixture.keys
        )
        rehearsal = ReceiverCutoverRehearsal(
            engine=fixture.engine,
            ws_manager=manager,
            runtime_authenticator=authenticator,
            hash_keys=fixture.keys,
            actor_user_id=1,
        )
        application = FastAPI()
        application.add_api_websocket_route(
            "/api/ws/receiver", server_module.ws_receiver
        )
        server_module.configure_receiver_runtime(
            application,
            authenticator=rehearsal.runtime_authenticator,
            connection_manager=manager,
        )
        port = _random_loopback_port()
        isolated_server, thread = _start_uvicorn(application, port)
        yield SimpleNamespace(
            **vars(fixture),
            server_module=server_module,
            db_module=db_module,
            manager=manager,
            rehearsal=rehearsal,
            application=application,
            ws_url=f"ws://127.0.0.1:{port}/api/ws/receiver",
        )
    finally:
        if isolated_server is not None:
            isolated_server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
            assert not thread.is_alive()
        fixture.engine.dispose()
        if "db_module" in locals():
            db_module.engine.dispose()
        for module_name in RUNTIME_MODULES:
            sys.modules.pop(module_name, None)
        if sys.path and sys.path[0] == str(backend_dir):
            sys.path.pop(0)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        assert _metadata(REAL_DATABASE) == real_before


async def _connect(url: str, token: str):
    return await websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=3,
    )


async def _rejected(url: str, token: str) -> bool:
    try:
        websocket = await _connect(url, token)
    except Exception:
        return True
    await websocket.close()
    return False


async def _wait_until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("Timed out waiting for cutover rehearsal state")


def test_module_and_configuration_boundaries(tmp_path):
    fixture = _new_database(tmp_path, "configuration.db")
    rehearsal, manager = _coordinator(fixture)
    try:
        assert rehearsal.runtime_authenticator is not None
        assert manager.receiver_connection_inventory.snapshot().total_active_count == 0
        rendered = repr(rehearsal)
        assert "key_material=<redacted>" in rendered
        assert all(key.hex() not in rendered for key in fixture.keys.values())
        assert "token" not in rendered.lower()

        default = LegacyStoreTokenRuntimeAuthenticator(lambda: None)
        with pytest.raises(InvalidCutoverConfigurationError):
            ReceiverCutoverRehearsal(
                engine=fixture.engine,
                ws_manager=manager,
                runtime_authenticator=default,
                hash_keys=fixture.keys,
                actor_user_id=1,
            )
        for bad_keys in ({}, {True: secrets.token_bytes(48)}, {1: b"short"}):
            with pytest.raises(InvalidCutoverConfigurationError):
                ReceiverCutoverRehearsal(
                    engine=fixture.engine,
                    ws_manager=manager,
                    runtime_authenticator=MigrationAwareReceiverRuntimeAuthenticator(
                        fixture.engine,
                        hash_keys=fixture.keys,
                    ),
                    hash_keys=bad_keys,
                    actor_user_id=1,
                )
    finally:
        fixture.engine.dispose()


def test_protected_database_is_refused_before_connection():
    engine = create_engine(f"sqlite:///{PROTECTED_DATABASE_PATH}")
    authenticator = MigrationAwareReceiverRuntimeAuthenticator(
        engine, hash_keys={1: secrets.token_bytes(48)}
    )
    before = _metadata(REAL_DATABASE)
    try:
        with pytest.raises(ProtectedCutoverDatabaseError):
            ReceiverCutoverRehearsal(
                engine=engine,
                ws_manager=SummaryOnlyManager(),
                runtime_authenticator=authenticator,
                hash_keys={1: secrets.token_bytes(48)},
                actor_user_id=1,
            )
        assert _metadata(REAL_DATABASE) == before
    finally:
        engine.dispose()


def test_transition_results_are_immutable_redacted_and_append_one_audit(tmp_path):
    fixture = _new_database(tmp_path, "result.db")
    rehearsal, _ = _coordinator(fixture)
    protected_before = {
        table: _rows(fixture.engine, table)
        for table in ("stores", "receiver_devices", "receiver_credentials", "schema_migrations")
    }
    audits_before = _transition_audits(fixture.engine)
    try:
        result = rehearsal.transition_to_dual_verify()
        assert result.previous_state == "backfilled"
        assert result.new_state == "dual_verify"
        assert result.result_code is CutoverStepCode.ENABLE_DUAL_VERIFICATION
        assert result.succeeded is True
        with pytest.raises(FrozenInstanceError):
            result.new_state = "hash_only"
        rendered = repr(result)
        assert "token" not in rendered.lower()
        assert all(key.hex() not in rendered for key in fixture.keys.values())
        assert _state(fixture.engine) == ("dual_verify", 1)
        assert len(_transition_audits(fixture.engine)) == len(audits_before) + 1
        assert {
            table: _rows(fixture.engine, table) for table in protected_before
        } == protected_before
    finally:
        fixture.engine.dispose()


def test_blockers_stale_summary_and_failed_hook_roll_back(tmp_path):
    fixture = _new_database(tmp_path, "blockers.db")
    rehearsal, manager = _coordinator(fixture)
    now = datetime.now(timezone.utc)
    legacy_record = AuthenticatedReceiverConnection(
        connection_id=uuid.uuid4().hex,
        store_id=11,
        device_id=None,
        credential_id=None,
        authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
        authenticated_at=now,
    )
    manager.receiver_connection_inventory.register(legacy_record)
    try:
        rehearsal.transition_to_dual_verify(now=now)
        state_before = _state(fixture.engine)
        audits_before = _transition_audits(fixture.engine)
        inventory_before = manager.receiver_connection_inventory.snapshot(captured_at=now)
        with pytest.raises(ActiveConnectionBlockerError):
            rehearsal.transition_to_hash_only(now=now)
        assert _state(fixture.engine) == state_before
        assert _transition_audits(fixture.engine) == audits_before
        assert manager.receiver_connection_inventory.snapshot(
            captured_at=now
        ).records == inventory_before.records

        manager.receiver_connection_inventory.remove(legacy_record.connection_id)
        with pytest.raises(StaleConnectionSummaryError):
            rehearsal.transition_to_hash_only(
                now=now,
                summary_captured_at=now - timedelta(seconds=31),
            )
        assert _state(fixture.engine) == state_before

        def fail_after_state(step: str):
            if step == "after_state_update":
                raise RuntimeError("synthetic cutover hook failure")

        with pytest.raises(TransitionPersistenceError):
            rehearsal.transition_to_hash_only(now=now, step_hook=fail_after_state)
        assert _state(fixture.engine) == state_before
        assert _transition_audits(fixture.engine) == audits_before
    finally:
        fixture.engine.dispose()


def test_missing_and_incorrect_keys_fail_without_secret_output(tmp_path, capsys, caplog):
    missing_fixture = _new_database(tmp_path, "missing-key.db")
    missing, _ = _coordinator(missing_fixture, keys={1: missing_fixture.keys[1]})
    wrong_fixture = _new_database(tmp_path, "wrong-key.db")
    wrong_keys = {
        1: secrets.token_bytes(48),
        2: wrong_fixture.keys[2],
    }
    wrong, _ = _coordinator(wrong_fixture, keys=wrong_keys)
    try:
        for fixture, rehearsal in (
            (missing_fixture, missing),
            (wrong_fixture, wrong),
        ):
            with pytest.raises(TransitionReadinessError) as failure:
                rehearsal.transition_to_dual_verify()
            assert str(failure.value) == "Receiver migration transition readiness failed"
            assert _state(fixture.engine) == ("backfilled", 1)
            assert _transition_audits(fixture.engine)[-1][2] == "legacy_backfill_rehearsal"
        output = capsys.readouterr()
        logs = " ".join(record.getMessage() for record in caplog.records)
        assert output.out == output.err == logs == ""
        for key in (*missing_fixture.keys.values(), *wrong_keys.values()):
            assert key.hex() not in str(failure.value)
    finally:
        missing_fixture.engine.dispose()
        wrong_fixture.engine.dispose()


def test_concurrent_cutover_serializes_one_expected_state_caller(tmp_path):
    fixture = _new_database(tmp_path, "concurrent.db")
    first, _ = _coordinator(fixture)
    second, _ = _coordinator(fixture)
    barrier = __import__("threading").Barrier(2)

    def run(rehearsal):
        barrier.wait(timeout=3)
        try:
            return rehearsal.transition_to_dual_verify()
        except Exception as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(run, (first, second)))
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        assert len(successes) == len(failures) == 1
        assert isinstance(failures[0], TransitionStateMismatchError)
        assert _state(fixture.engine) == ("dual_verify", 1)
        assert len(_transition_audits(fixture.engine)) == 2
    finally:
        fixture.engine.dispose()


def test_full_loopback_forward_cutover_and_rollback(loopback_cutover, capsys, caplog):
    runtime = loopback_cutover
    tokens_before = {
        store_id: _store_health(runtime.engine, store_id).receiver_token
        for store_id in (11, 12, 13)
    }
    protected_before = {
        table: _rows(runtime.engine, table)
        for table in ("receiver_devices", "receiver_credentials", "schema_migrations")
    }
    audits_at_start = _transition_audits(runtime.engine)
    assert _state(runtime.engine) == ("backfilled", 1)
    assert isinstance(
        runtime.server_module.default_receiver_runtime_authenticator,
        LegacyStoreTokenRuntimeAuthenticator,
    )
    assert (
        runtime.server_module.app.state.receiver_runtime_authenticator
        is runtime.server_module.default_receiver_runtime_authenticator
    )
    assert runtime.application.state.receiver_runtime_authenticator is runtime.rehearsal.runtime_authenticator
    assert not any("cutover" in getattr(route, "path", "") for route in runtime.application.routes)

    async def scenario():
        # Backfilled permits only the raw Store path.
        legacy_11 = await _connect(runtime.ws_url, runtime.tokens[11])
        await _wait_until(lambda: runtime.manager.get_receiver_connection_id(11))
        legacy_11_id = runtime.manager.get_receiver_connection_id(11)
        record = runtime.manager.receiver_connection_inventory.get(legacy_11_id)
        assert record.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        snapshot = runtime.manager.get_receiver_snapshot(11)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness is ReadinessState.UNKNOWN
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED

        health_12 = _store_health(runtime.engine, 12)
        inventory_before_rejection = runtime.manager.receiver_connection_inventory.snapshot().records
        assert await _rejected(runtime.ws_url, runtime.enrolled_tokens[12])
        assert _store_health(runtime.engine, 12) == health_12
        assert runtime.manager.receiver_connection_inventory.snapshot().records == inventory_before_rejection
        assert runtime.manager.get_receiver_snapshot(12) is None

        # Expanding preserves the already-authenticated legacy source and socket.
        enabled = runtime.rehearsal.transition_to_dual_verify()
        assert enabled.new_state == "dual_verify"
        assert enabled.legacy_verification_enabled == 1
        assert runtime.manager.receiver_connection_inventory.get(
            legacy_11_id
        ).authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        await legacy_11.ping()

        hashed_12 = await _connect(runtime.ws_url, runtime.tokens[12])
        await _wait_until(lambda: runtime.manager.get_receiver_connection_id(12))
        hashed_12_id = runtime.manager.get_receiver_connection_id(12)
        mapped_12 = _credential_identity(runtime.engine, 12, "legacy_uuid_hex")
        record_12 = runtime.manager.receiver_connection_inventory.get(hashed_12_id)
        assert record_12.authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
        assert (record_12.device_id, record_12.credential_id) == (
            mapped_12.device_id,
            mapped_12.credential_id,
        )
        summary = runtime.rehearsal.connection_summary()
        assert (summary.legacy_authenticated_count, summary.hashed_authenticated_count) == (1, 1)

        blocked_state = _state(runtime.engine)
        blocked_audits = _transition_audits(runtime.engine)
        blocked_records = runtime.manager.receiver_connection_inventory.snapshot().records
        with pytest.raises(ActiveConnectionBlockerError):
            runtime.rehearsal.transition_to_hash_only()
        assert _state(runtime.engine) == blocked_state
        assert _transition_audits(runtime.engine) == blocked_audits
        assert runtime.manager.receiver_connection_inventory.snapshot().records == blocked_records
        await legacy_11.ping()
        await hashed_12.ping()

        # Re-authentication under dual_verify replaces the legacy source with hash-backed identity.
        hashed_11 = await _connect(runtime.ws_url, runtime.enrolled_tokens[11])
        await _wait_until(
            lambda: runtime.manager.get_receiver_connection_id(11) != legacy_11_id
        )
        hashed_11_id = runtime.manager.get_receiver_connection_id(11)
        await legacy_11.wait_closed()
        assert runtime.manager.get_receiver_connection_id(11) == hashed_11_id
        record_11 = runtime.manager.receiver_connection_inventory.get(hashed_11_id)
        enrolled_11 = _credential_identity(runtime.engine, 11, "speaklink_rcv")
        assert record_11.authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
        assert (record_11.device_id, record_11.credential_id) == (
            enrolled_11.device_id,
            enrolled_11.credential_id,
        )
        summary = runtime.rehearsal.connection_summary()
        assert (summary.legacy_authenticated_count, summary.hashed_authenticated_count) == (0, 2)

        hash_only = runtime.rehearsal.transition_to_hash_only()
        assert hash_only.new_state == "hash_only"
        assert hash_only.legacy_verification_enabled == 0
        await hashed_11.ping()
        await hashed_12.ping()
        assert await _rejected(runtime.ws_url, uuid.uuid4().hex)

        # Both legacy-format HMAC and new-format HMAC paths work in hash_only.
        replacement_11 = await _connect(runtime.ws_url, runtime.enrolled_tokens[11])
        await _wait_until(
            lambda: runtime.manager.get_receiver_connection_id(11) != hashed_11_id
        )
        replacement_11_id = runtime.manager.get_receiver_connection_id(11)
        await hashed_11.wait_closed()
        assert runtime.manager.receiver_connection_inventory.get(
            replacement_11_id
        ).authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
        hashed_11 = replacement_11
        hashed_11_id = replacement_11_id

        replacement_12 = await _connect(runtime.ws_url, runtime.tokens[12])
        await _wait_until(
            lambda: runtime.manager.get_receiver_connection_id(12) != hashed_12_id
        )
        replacement_12_id = runtime.manager.get_receiver_connection_id(12)
        await hashed_12.wait_closed()
        assert runtime.manager.receiver_connection_inventory.get(
            replacement_12_id
        ).authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
        assert all(
            item.authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
            for item in runtime.manager.receiver_connection_inventory.snapshot().records
        )

        # Existing acknowledgement behavior remains independent of authentication source.
        await hashed_11.send(
            json.dumps(
                {
                    "protocol_version": "1.0",
                    "type": "receiver_ready",
                    "message_id": str(uuid.uuid4()),
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "sequence": 0,
                    "software_checks_passed": True,
                    "output_device_checks_passed": True,
                }
            )
        )
        await _wait_until(
            lambda: runtime.manager.get_receiver_snapshot(11).readiness is ReadinessState.READY
        )
        ready_snapshot = runtime.manager.get_receiver_snapshot(11)
        assert ready_snapshot.playback is PlaybackState.STOPPED
        assert ready_snapshot.acoustic is AcousticState.UNVERIFIED

        # Expanding rollback retains both hash source records without relabeling.
        rolled_dual = runtime.rehearsal.rollback_to_dual_verify()
        assert rolled_dual.new_state == "dual_verify"
        assert rolled_dual.legacy_verification_enabled == 1
        assert runtime.manager.receiver_connection_inventory.get(
            hashed_11_id
        ).authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL

        rollback_state = _state(runtime.engine)
        rollback_audits = _transition_audits(runtime.engine)
        with pytest.raises(ActiveConnectionBlockerError):
            runtime.rehearsal.rollback_to_backfilled()
        assert _state(runtime.engine) == rollback_state
        assert _transition_audits(runtime.engine) == rollback_audits
        await hashed_11.ping()
        await replacement_12.ping()

        await hashed_11.close()
        await replacement_12.close()
        await _wait_until(
            lambda: runtime.rehearsal.connection_summary().hashed_authenticated_count == 0
        )
        backfilled = runtime.rehearsal.rollback_to_backfilled()
        assert backfilled.new_state == "backfilled"
        assert backfilled.legacy_verification_enabled == 1

        final_legacy = await _connect(runtime.ws_url, runtime.tokens[11])
        await _wait_until(lambda: runtime.manager.get_receiver_connection_id(11))
        final_id = runtime.manager.get_receiver_connection_id(11)
        assert final_id != hashed_11_id
        assert runtime.manager.receiver_connection_inventory.get(
            final_id
        ).authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        assert await _rejected(runtime.ws_url, runtime.enrolled_tokens[12])
        await final_legacy.close()
        await _wait_until(
            lambda: runtime.manager.receiver_connection_inventory.snapshot().total_active_count == 0
        )

        # Capacity rejection happens after authentication but before health mutation.
        now = datetime.now(timezone.utc)
        for index in range(3):
            runtime.manager.receiver_connection_inventory.register(
                AuthenticatedReceiverConnection(
                    connection_id=f"capacity-{index}",
                    store_id=100 + index,
                    device_id=None,
                    credential_id=None,
                    authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
                    authenticated_at=now,
                )
            )
        before_health = _store_health(runtime.engine, 11)
        capacity_socket = await _connect(runtime.ws_url, runtime.tokens[11])
        await capacity_socket.wait_closed()
        assert capacity_socket.close_code == 1013
        assert _store_health(runtime.engine, 11) == before_health
        assert runtime.manager.get_receiver_connection_id(11) is None
        for index in range(3):
            runtime.manager.receiver_connection_inventory.remove(f"capacity-{index}")

    asyncio.run(scenario())

    assert _state(runtime.engine) == ("backfilled", 1)
    assert len(_transition_audits(runtime.engine)) == len(audits_at_start) + 4
    assert {
        table: _rows(runtime.engine, table) for table in protected_before
    } == protected_before
    assert {
        store_id: _store_health(runtime.engine, store_id).receiver_token
        for store_id in (11, 12, 13)
    } == tokens_before
    for event_type, outcome, reason, metadata in _transition_audits(runtime.engine):
        assert event_type == "migration_state_changed"
        assert outcome == "success"
        parsed = json.loads(metadata)
        assert set(parsed) <= {
                "actor_user_id",
                "migration_phase",
                "migration_state",
                "outcome",
                "reason",
                "reason_code",
        }
        assert reason in {
            "legacy_backfill_rehearsal",
            "enable_dual_verification",
            "enable_hash_only",
            "rollback_to_dual_verification",
            "rollback_to_backfilled",
        }
    output = capsys.readouterr()
    logs = " ".join(record.getMessage() for record in caplog.records)
    assert output.out == output.err == ""
    for secret in (*runtime.tokens.values(), *runtime.enrolled_tokens.values()):
        assert secret not in logs
    for key in runtime.keys.values():
        assert key.hex() not in logs
