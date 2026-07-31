"""Isolated tests for the explicitly injected Receiver runtime authenticator."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
from types import SimpleNamespace
import sys
import uuid

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from sqlalchemy import create_engine, event, text

from migrations import run_receiver_credential_phase_one
from receiver_connection_inventory import (
    ActiveReceiverConnectionInventory,
    ConnectionAuthenticationSource,
)
from receiver_contract import AcousticState, ConnectionState, PlaybackState, ReadinessState
from receiver_credential_backfill import rehearse_legacy_receiver_backfill
from receiver_device_service import enroll_receiver_device
from receiver_runtime_auth import (
    LegacyStoreTokenRuntimeAuthenticator,
    MigrationAwareReceiverRuntimeAuthenticator,
    ReceiverRuntimeAuthenticationError,
    ReceiverRuntimeIdentity,
)


UTC_NOW = datetime(2025, 1, 15, 10, 0, tzinfo=timezone.utc)
RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")
REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"
DISCONNECT = object()
GENERIC_FAILURE = "Receiver authentication failed"


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


def _create_legacy_schema(engine):
    tokens = {store_id: uuid.uuid4().hex for store_id in (11, 12, 13)}
    now = UTC_NOW.isoformat()
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
                "VALUES (1, 'runtime-actor', 'test-only', 'admin', 1, :now)"
            ),
            {"now": now},
        )
        for store_id in (11, 12, 13):
            connection.execute(
                text(
                    "INSERT INTO stores "
                    "(id, store_code, store_name, city, region, is_online_store, "
                    "receiver_token, is_active, status, last_seen, created_at, updated_at) "
                    "VALUES (:id, :code, :name, 'Test City', 'Test Region', 0, "
                    ":token, :active, 'offline', NULL, :now, :now)"
                ),
                {
                    "id": store_id,
                    "code": f"RUNTIME-{store_id}",
                    "name": f"Runtime Store {store_id}",
                    "token": tokens[store_id],
                    "active": 0 if store_id == 13 else 1,
                    "now": now,
                },
            )
    return tokens


def _set_state(engine, state: str, enabled: int):
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state "
                "SET state=:state, legacy_verification_enabled=:enabled, updated_at=:now "
                "WHERE id=1"
            ),
            {"state": state, "enabled": enabled, "now": UTC_NOW.isoformat()},
        )


def _credential_tables(engine):
    tables = (
        "receiver_devices",
        "receiver_credentials",
        "receiver_credential_events",
        "receiver_credential_migration_state",
        "schema_migrations",
    )
    with engine.connect() as connection:
        return {
            table: tuple(connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1'))
            for table in tables
        }


def _health(engine, store_id: int):
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT receiver_token, status, last_seen FROM stores WHERE id=:id"),
            {"id": store_id},
        ).one()


def _legacy_mapping(engine, store_id=11):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT d.id AS device_id, c.id AS credential_id "
                "FROM receiver_devices d JOIN receiver_credentials c ON c.device_id=d.id "
                "WHERE d.store_id=:store_id AND c.token_format='legacy_uuid_hex'"
            ),
            {"store_id": store_id},
        ).one()


@pytest.fixture(scope="module")
def isolated_runtime(tmp_path_factory):
    real_before = _metadata(REAL_DATABASE)
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("dual-auth-runtime") / "runtime.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    tokens = _create_legacy_schema(engine)
    run_receiver_credential_phase_one(engine)
    backfill_key = secrets.token_bytes(48)
    enrollment_key = secrets.token_bytes(48)
    rehearse_legacy_receiver_backfill(
        engine,
        hash_key=backfill_key,
        hash_key_version=1,
        now=UTC_NOW,
    )
    _set_state(engine, "legacy_only", 1)
    enrollment = enroll_receiver_device(
        engine,
        store_id=11,
        display_name="Explicit dual-auth test device",
        actor_user_id=1,
        hash_key=enrollment_key,
        hash_key_version=2,
        now=UTC_NOW,
    )
    enrollment_token = enrollment.take_raw_credential()
    with engine.connect() as connection:
        enrollment_ids = connection.execute(
            text(
                """
                SELECT d.id, c.id
                FROM receiver_devices AS d
                JOIN receiver_credentials AS c ON c.device_id = d.id
                WHERE d.public_id = :device_public_id
                  AND c.public_id = :credential_public_id
                """
            ),
            {
                "device_public_id": enrollment.device_public_id,
                "credential_public_id": enrollment.credential_public_id,
            },
        ).one()

    environment = {
        "ECHOCAST_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"dual-runtime-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.path.insert(0, str(backend_dir))
    for module_name in RUNTIME_MODULES:
        sys.modules.pop(module_name, None)
    try:
        server = importlib.import_module("server")
        db = importlib.import_module("db")
        ws_manager = importlib.import_module("ws_manager")
        assert Path(db.DB_PATH) == database_path.resolve()
        server.startup_event()
        yield SimpleNamespace(
            engine=engine,
            db=db,
            server=server,
            ws_manager=ws_manager,
            tokens=tokens,
            enrollment_token=enrollment_token,
            enrollment_ids=enrollment_ids,
            keys={1: backfill_key, 2: enrollment_key},
            database_path=database_path,
        )
    finally:
        engine.dispose()
        if "db" in locals():
            db.engine.dispose()
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


@pytest.fixture(autouse=True)
def restore_runtime_data(isolated_runtime):
    yield
    with isolated_runtime.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE stores SET receiver_token=CASE id "
                "WHEN 11 THEN :token11 WHEN 12 THEN :token12 WHEN 13 THEN :token13 END, "
                "is_active=CASE WHEN id=13 THEN 0 ELSE 1 END, status='offline', last_seen=NULL"
            ),
            {
                "token11": isolated_runtime.tokens[11],
                "token12": isolated_runtime.tokens[12],
                "token13": isolated_runtime.tokens[13],
            },
        )
        connection.execute(
            text("UPDATE receiver_devices SET status=CASE WHEN store_id=13 THEN 'disabled' ELSE 'active' END")
        )
        connection.execute(
            text(
                "UPDATE receiver_credentials SET status='active', revoked_at=NULL, "
                "expires_at=NULL, replaced_at=NULL, accept_until=NULL, last_used_at=NULL"
            )
        )
    _set_state(isolated_runtime.engine, "legacy_only", 1)


class RuntimeWebSocket:
    def __init__(self, authorization, app, *, query_token=None):
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.query_params = {} if query_token is None else {"token": query_token}
        self.app = app
        self.accepted = False
        self.close_calls = []
        self.incoming = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.close_calls.append((code, reason))

    async def send_text(self, message):
        json.loads(message)

    async def receive_text(self):
        value = await self.incoming.get()
        if value is DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        if isinstance(value, BaseException):
            raise value
        return value

    async def disconnect(self):
        await self.incoming.put(DISCONNECT)


def _migration_auth(runtime, keys=None):
    return MigrationAwareReceiverRuntimeAuthenticator(
        runtime.engine,
        hash_keys=runtime.keys if keys is None else keys,
    )


def _configured_app(runtime, authenticator, *, capacity=256):
    app = FastAPI()
    inventory = ActiveReceiverConnectionInventory(max_connections=capacity)
    manager = runtime.ws_manager.WSManager(receiver_connection_inventory=inventory)
    runtime.server.configure_receiver_runtime(
        app,
        authenticator=authenticator,
        connection_manager=manager,
    )
    return app, manager


async def _wait_until(predicate, timeout=1.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for dual-auth runtime state")


async def _open(runtime, app, manager, token):
    websocket = RuntimeWebSocket(f"Bearer {token}", app)
    task = asyncio.create_task(runtime.server.ws_receiver(websocket))
    await _wait_until(
        lambda: (websocket.accepted and bool(manager.receiver_connection_ids))
        or task.done()
    )
    if task.done() or not manager.receiver_connection_ids:
        await task
        raise AssertionError(
            f"Receiver was not registered; close_calls={websocket.close_calls!r}"
        )
    return websocket, task


async def _close(websocket, task):
    await websocket.disconnect()
    await asyncio.wait_for(task, timeout=1.5)


def _assert_rejected(runtime, app, manager, token):
    before_health = _health(runtime.engine, 11)
    before_credentials = _credential_tables(runtime.engine)
    before_snapshots = dict(manager.receiver_snapshots)

    async def scenario():
        websocket = RuntimeWebSocket(f"Bearer {token}", app)
        await runtime.server.ws_receiver(websocket)
        assert websocket.accepted is False
        assert websocket.close_calls == [(4401, GENERIC_FAILURE)]

    asyncio.run(scenario())
    assert manager.receivers == {}
    assert manager.receiver_connection_inventory.snapshot().total_active_count == 0
    assert manager.receiver_snapshots == before_snapshots
    assert _health(runtime.engine, 11) == before_health
    assert _credential_tables(runtime.engine) == before_credentials


def test_runtime_identity_is_immutable_non_secret_and_source_strict():
    identity = ReceiverRuntimeIdentity(
        store_id=11,
        authentication_source=ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL,
        device_id=21,
        credential_id=31,
    )
    assert "token" not in repr(identity).lower()
    assert "hash_key" not in repr(identity).lower()
    with pytest.raises(FrozenInstanceError):
        identity.store_id = 12


def test_default_application_constructs_legacy_only_authenticator(isolated_runtime):
    default = isolated_runtime.server.default_receiver_runtime_authenticator
    assert isinstance(default, LegacyStoreTokenRuntimeAuthenticator)
    # The app now holds a ReceiverAuthMode wrapper rather than the legacy
    # authenticator itself, because the choice is re-evaluated instead of frozen -
    # a backend that started before the phase-one tables existed used to refuse
    # every Device credential for its whole life. The BEHAVIOUR asserted here is
    # unchanged: on a database without those tables, only legacy Store tokens are
    # accepted, and the wrapper delegates to exactly this object.
    mode = isolated_runtime.server.app.state.receiver_runtime_authenticator
    assert mode.describe() == "legacy store tokens only"
    assert mode.current is default
    result = default.authenticate(
        presented_token=isolated_runtime.tokens[11],
        authenticated_at=UTC_NOW,
    )
    assert result.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
    assert result.device_id is result.credential_id is None


def test_default_legacy_application_rejects_new_credential(isolated_runtime):
    default = isolated_runtime.server.default_receiver_runtime_authenticator
    app, manager = _configured_app(isolated_runtime, default)
    _assert_rejected(isolated_runtime, app, manager, isolated_runtime.enrollment_token)


def test_default_legacy_authenticator_rejects_oversized_input(isolated_runtime):
    with pytest.raises(ReceiverRuntimeAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
        isolated_runtime.server.default_receiver_runtime_authenticator.authenticate(
            presented_token="x" * 129,
            authenticated_at=UTC_NOW,
        )


@pytest.mark.parametrize(
    ("state", "enabled", "expected_source", "expect_enrollment"),
    [
        ("legacy_only", 1, "legacy_store_token", False),
        ("backfilled", 1, "legacy_store_token", False),
        ("dual_verify", 1, "hashed_device_credential", True),
        ("hash_only", 0, "hashed_device_credential", True),
        ("raw_neutralized", 0, "hashed_device_credential", True),
    ],
)
def test_explicit_migration_authenticator_preserves_state_matrix(
    isolated_runtime, state, enabled, expected_source, expect_enrollment
):
    _set_state(isolated_runtime.engine, state, enabled)
    authenticator = _migration_auth(isolated_runtime)
    result = authenticator.authenticate(
        presented_token=isolated_runtime.tokens[11],
        authenticated_at=UTC_NOW,
    )
    assert result.authentication_source.value == expected_source
    if expected_source == "hashed_device_credential":
        mapping = _legacy_mapping(isolated_runtime.engine)
        assert (result.device_id, result.credential_id) == (
            mapping.device_id,
            mapping.credential_id,
        )
    elif state == "legacy_only":
        assert result.device_id is result.credential_id is None

    if expect_enrollment:
        enrolled = authenticator.authenticate(
            presented_token=isolated_runtime.enrollment_token,
            authenticated_at=UTC_NOW,
        )
        assert enrolled.authentication_source.value == "hashed_device_credential"
        assert (enrolled.device_id, enrolled.credential_id) == isolated_runtime.enrollment_ids
    else:
        with pytest.raises(ReceiverRuntimeAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
            authenticator.authenticate(
                presented_token=isolated_runtime.enrollment_token,
                authenticated_at=UTC_NOW,
            )


@pytest.mark.parametrize(
    ("state", "enabled"),
    [("legacy_only", 0), ("dual_verify", 0), ("hash_only", 1), ("unknown", 1)],
)
def test_inconsistent_state_fails_before_websocket_side_effects(
    isolated_runtime, state, enabled
):
    with isolated_runtime.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state "
                "SET state=:state, legacy_verification_enabled=:enabled WHERE id=1"
            ),
            {"state": state, "enabled": enabled},
        )
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))
    _assert_rejected(isolated_runtime, app, manager, isolated_runtime.tokens[11])


def test_missing_hash_key_version_fails_generically(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    app, manager = _configured_app(
        isolated_runtime,
        _migration_auth(isolated_runtime, keys={2: isolated_runtime.keys[2]}),
    )
    _assert_rejected(isolated_runtime, app, manager, isolated_runtime.tokens[11])


@pytest.mark.parametrize("bad_token", ["", "x" * 300, "non-ascii-\u2603", "control\nvalue"])
def test_malformed_tokens_fail_safely(isolated_runtime, bad_token):
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))
    _assert_rejected(isolated_runtime, app, manager, bad_token)


def test_disabled_store_fails_for_both_authenticators(isolated_runtime):
    with isolated_runtime.engine.begin() as connection:
        connection.execute(text("UPDATE stores SET is_active=0 WHERE id=11"))
    for authenticator in (
        isolated_runtime.server.default_receiver_runtime_authenticator,
        _migration_auth(isolated_runtime),
    ):
        with pytest.raises(ReceiverRuntimeAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
            authenticator.authenticate(
                presented_token=isolated_runtime.tokens[11],
                authenticated_at=UTC_NOW,
            )


@pytest.mark.parametrize(
    ("statement", "token_selector"),
    [
        ("UPDATE receiver_devices SET status='disabled' WHERE store_id=11", "legacy"),
        ("UPDATE receiver_devices SET status='retired' WHERE store_id=11", "legacy"),
        ("UPDATE receiver_credentials SET status='inactive' WHERE id=:credential_id", "new"),
        ("UPDATE receiver_credentials SET revoked_at=:now WHERE id=:credential_id", "new"),
        ("UPDATE receiver_credentials SET expires_at=:now WHERE id=:credential_id", "new"),
    ],
)
def test_ineligible_device_or_credential_fails(
    isolated_runtime, statement, token_selector
):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    with isolated_runtime.engine.begin() as connection:
        connection.execute(
            text(statement),
            {
                "credential_id": isolated_runtime.enrollment_ids[1],
                "now": UTC_NOW.isoformat(),
            },
        )
    token = (
        isolated_runtime.tokens[11]
        if token_selector == "legacy"
        else isolated_runtime.enrollment_token
    )
    with pytest.raises(ReceiverRuntimeAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
        _migration_auth(isolated_runtime).authenticate(
            presented_token=token,
            authenticated_at=UTC_NOW,
        )


@pytest.mark.parametrize(
    ("state", "expected_source"),
    [("legacy_only", "legacy_store_token"), ("dual_verify", "hashed_device_credential")],
)
def test_websocket_inventory_preserves_authenticator_identity(
    isolated_runtime, state, expected_source
):
    _set_state(isolated_runtime.engine, state, 1)
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))

    async def scenario():
        websocket, task = await _open(
            isolated_runtime, app, manager, isolated_runtime.tokens[11]
        )
        record = manager.receiver_connection_inventory.snapshot().records[0]
        assert record.authentication_source.value == expected_source
        if expected_source == "hashed_device_credential":
            mapping = _legacy_mapping(isolated_runtime.engine)
            assert (record.device_id, record.credential_id) == (
                mapping.device_id,
                mapping.credential_id,
            )
        else:
            assert record.device_id is record.credential_id is None
        summary = manager.get_active_receiver_transition_summary(now=UTC_NOW)
        assert summary.hashed_authenticated_count == (
            expected_source == "hashed_device_credential"
        )
        assert summary.legacy_authenticated_count == (
            expected_source == "legacy_store_token"
        )
        snapshot = manager.get_receiver_snapshot(11)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness is ReadinessState.UNKNOWN
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        await _close(websocket, task)

    asyncio.run(scenario())


def test_mixed_store_summary_uses_canonical_sources(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    migration_app, manager = _configured_app(
        isolated_runtime, _migration_auth(isolated_runtime)
    )
    legacy_app = FastAPI()
    isolated_runtime.server.configure_receiver_runtime(
        legacy_app,
        authenticator=isolated_runtime.server.default_receiver_runtime_authenticator,
        connection_manager=manager,
    )

    async def scenario():
        hashed_ws, hashed_task = await _open(
            isolated_runtime, migration_app, manager, isolated_runtime.tokens[11]
        )
        legacy_ws, legacy_task = await _open(
            isolated_runtime, legacy_app, manager, isolated_runtime.tokens[12]
        )
        summary = manager.get_active_receiver_transition_summary(now=UTC_NOW)
        assert summary.legacy_authenticated_count == 1
        assert summary.hashed_authenticated_count == 1
        await _close(hashed_ws, hashed_task)
        await _close(legacy_ws, legacy_task)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("first_state", "second_state", "first_source", "second_source"),
    [
        ("legacy_only", "dual_verify", "legacy_store_token", "hashed_device_credential"),
        ("dual_verify", "backfilled", "hashed_device_credential", "legacy_store_token"),
    ],
)
def test_replacement_across_sources_keeps_only_new_identity(
    isolated_runtime, first_state, second_state, first_source, second_source
):
    authenticator = _migration_auth(isolated_runtime)
    app, manager = _configured_app(isolated_runtime, authenticator)

    async def scenario():
        _set_state(isolated_runtime.engine, first_state, 1)
        old_ws, old_task = await _open(
            isolated_runtime, app, manager, isolated_runtime.tokens[11]
        )
        old_id = manager.get_receiver_connection_id(11)
        assert manager.receiver_connection_inventory.get(old_id).authentication_source.value == first_source

        _set_state(isolated_runtime.engine, second_state, 1)
        new_ws, new_task = await _open(
            isolated_runtime, app, manager, isolated_runtime.tokens[11]
        )
        new_id = manager.get_receiver_connection_id(11)
        assert new_id != old_id
        records = manager.receiver_connection_inventory.snapshot().records
        assert len(records) == 1
        assert records[0].connection_id == new_id
        assert records[0].authentication_source.value == second_source

        await old_ws.disconnect()
        await asyncio.wait_for(old_task, timeout=1.5)
        assert manager.receiver_connection_inventory.get(new_id) is not None
        assert manager.get_receiver_snapshot(11).connection is ConnectionState.CONNECTED
        summary = manager.get_active_receiver_transition_summary(now=UTC_NOW)
        assert summary.hashed_authenticated_count == (second_source == "hashed_device_credential")
        assert summary.legacy_authenticated_count == (second_source == "legacy_store_token")
        await _close(new_ws, new_task)

    asyncio.run(scenario())


def test_disconnect_errors_remove_hashed_exact_record(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))

    async def scenario():
        websocket, task = await _open(
            isolated_runtime, app, manager, isolated_runtime.enrollment_token
        )
        connection_id = manager.get_receiver_connection_id(11)
        await websocket.incoming.put(RuntimeError("synthetic protocol transport error"))
        await asyncio.wait_for(task, timeout=1.5)
        assert manager.receiver_connection_inventory.get(connection_id) is None

    asyncio.run(scenario())


def test_credential_tables_remain_read_only_across_success_and_failure(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))
    before = _credential_tables(isolated_runtime.engine)

    async def scenario():
        websocket, task = await _open(
            isolated_runtime, app, manager, isolated_runtime.enrollment_token
        )
        await _close(websocket, task)

    asyncio.run(scenario())
    after_success = _credential_tables(isolated_runtime.engine)
    assert after_success == before
    _assert_rejected(isolated_runtime, app, manager, "invalid")
    assert _credential_tables(isolated_runtime.engine) == before


def test_capacity_failure_has_no_health_or_credential_write(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    app, manager = _configured_app(
        isolated_runtime,
        _migration_auth(isolated_runtime),
        capacity=1,
    )
    before_second = _health(isolated_runtime.engine, 12)
    before_credentials = _credential_tables(isolated_runtime.engine)

    async def scenario():
        first_ws, first_task = await _open(
            isolated_runtime, app, manager, isolated_runtime.tokens[11]
        )
        second_ws = RuntimeWebSocket(
            f"Bearer {isolated_runtime.tokens[12]}", app
        )
        await isolated_runtime.server.ws_receiver(second_ws)
        assert second_ws.close_calls == [(1013, "Receiver connection unavailable")]
        assert manager.get_receiver_connection_id(12) is None
        await _close(first_ws, first_task)

    asyncio.run(scenario())
    assert _health(isolated_runtime.engine, 12) == before_second
    assert _credential_tables(isolated_runtime.engine) == before_credentials


def test_query_token_not_restored_and_secrets_not_rendered(isolated_runtime, capsys, caplog):
    app, manager = _configured_app(isolated_runtime, _migration_auth(isolated_runtime))

    async def scenario():
        websocket = RuntimeWebSocket(
            None,
            app,
            query_token=isolated_runtime.tokens[11],
        )
        await isolated_runtime.server.ws_receiver(websocket)
        assert websocket.accepted is False
        assert manager.receiver_connection_inventory.snapshot().total_active_count == 0

    asyncio.run(scenario())
    rendered = repr(_migration_auth(isolated_runtime))
    assert isolated_runtime.tokens[11] not in rendered
    assert isolated_runtime.enrollment_token not in rendered
    assert all(key.hex() not in rendered for key in isolated_runtime.keys.values())
    output = capsys.readouterr()
    logs = " ".join(record.getMessage() for record in caplog.records)
    assert output.out == output.err == ""
    assert isolated_runtime.tokens[11] not in logs


def test_authenticator_instances_do_not_share_key_rings(isolated_runtime):
    _set_state(isolated_runtime.engine, "dual_verify", 1)
    valid = _migration_auth(isolated_runtime)
    wrong = _migration_auth(
        isolated_runtime,
        keys={1: secrets.token_bytes(48), 2: secrets.token_bytes(48)},
    )
    assert valid.authenticate(
        presented_token=isolated_runtime.enrollment_token,
        authenticated_at=UTC_NOW,
    ).credential_id == isolated_runtime.enrollment_ids[1]
    with pytest.raises(ReceiverRuntimeAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
        wrong.authenticate(
            presented_token=isolated_runtime.enrollment_token,
            authenticated_at=UTC_NOW,
        )
