"""Isolated WebSocket lifecycle tests for Receiver inventory runtime wiring."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import secrets
from types import SimpleNamespace
from uuid import uuid4
import sys

import pytest
from fastapi import WebSocketDisconnect

from receiver_connection_inventory import ActiveReceiverConnectionInventory
from receiver_contract import AcousticState, ConnectionState, PlaybackState, ReadinessState


RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")
REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"
NORMAL_DISCONNECT = object()
AUTH_FAILURE_CODE = 4401
AUTH_FAILURE_REASON = "Receiver authentication failed"
CONNECTION_FAILURE_CODE = 1013
CONNECTION_FAILURE_REASON = "Receiver connection unavailable"


def _database_metadata(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


@dataclass(frozen=True)
class StoreIdentity:
    id: int
    token: str = field(repr=False)


class InventoryWebSocket:
    def __init__(self, authorization: str | None, *, query_token: str | None = None):
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.query_params = {} if query_token is None else {"token": query_token}
        self.accepted = False
        self.close_calls: list[tuple[int, str | None]] = []
        self.sent_text: list[dict] = []
        self.incoming = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.close_calls.append((code, reason))

    async def send_text(self, message):
        self.sent_text.append(json.loads(message))

    async def send_bytes(self, _data):
        raise AssertionError("Inventory tests do not exercise audio streaming")

    async def receive_text(self):
        value = await self.incoming.get()
        if value is NORMAL_DISCONNECT:
            raise WebSocketDisconnect(code=1000)
        if isinstance(value, BaseException):
            raise value
        return value

    async def send_receiver_message(self, payload):
        await self.incoming.put(json.dumps(payload))

    async def disconnect(self):
        await self.incoming.put(NORMAL_DISCONNECT)

    async def fail(self, error: BaseException):
        await self.incoming.put(error)


@pytest.fixture(scope="module")
def isolated_runtime(tmp_path_factory):
    real_database_before = _database_metadata(REAL_DATABASE)
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("receiver-inventory-runtime") / "runtime.db"
    environment = {
        "ECHOCAST_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"inventory-runtime-{secrets.token_hex(6)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
    }
    previous_environment = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.path.insert(0, str(backend_dir))
    for module_name in RUNTIME_MODULES:
        sys.modules.pop(module_name, None)

    try:
        db = importlib.import_module("db")
        server = importlib.import_module("server")
        models = importlib.import_module("models")
        ws_manager = importlib.import_module("ws_manager")
        assert Path(db.DB_PATH) == database_path.resolve()
        server.startup_event()
        with db.SessionLocal() as session:
            rows = (
                session.query(models.Store)
                .filter(models.Store.is_active.is_(True))
                .order_by(models.Store.id)
                .limit(3)
                .all()
            )
            stores = tuple(StoreIdentity(row.id, row.receiver_token) for row in rows)
        assert len(stores) == 3
        yield SimpleNamespace(
            db=db,
            server=server,
            models=models,
            ws_manager=ws_manager,
            stores=stores,
            database_path=database_path,
        )
    finally:
        if "db" in locals():
            db.engine.dispose()
        for module_name in RUNTIME_MODULES:
            sys.modules.pop(module_name, None)
        if sys.path and sys.path[0] == str(backend_dir):
            sys.path.pop(0)
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        assert _database_metadata(REAL_DATABASE) == real_database_before


@pytest.fixture
def runtime(isolated_runtime):
    manager = isolated_runtime.ws_manager.WSManager()
    isolated_runtime.server.manager = manager
    isolated_runtime.ws_manager.manager = manager
    return SimpleNamespace(**vars(isolated_runtime), manager=manager)


async def _wait_until(predicate, timeout=1.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for Receiver inventory state")


async def _open_receiver(runtime, store: StoreIdentity):
    websocket = InventoryWebSocket(f"Bearer {store.token}")
    task = asyncio.create_task(runtime.server.ws_receiver(websocket))
    await _wait_until(
        lambda: websocket.accepted
        and runtime.manager.get_receiver_connection_id(store.id) is not None
    )
    return websocket, task


async def _close_receiver(websocket, task):
    await websocket.disconnect()
    await asyncio.wait_for(task, timeout=1.5)


def _health(runtime, store_id: int):
    with runtime.db.SessionLocal() as session:
        store = session.query(runtime.models.Store).filter_by(id=store_id).one()
        event_count = session.query(runtime.models.ReceiverEvent).filter_by(store_id=store_id).count()
        return store.receiver_token, store.status, store.last_seen, event_count


def _schema_signature(runtime):
    with runtime.db.engine.connect() as connection:
        tables = tuple(
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        )
    return tables


def _ack(message_type: str, sequence: int, **extra):
    payload = {
        "protocol_version": "1.0",
        "type": message_type,
        "message_id": str(uuid4()),
        "occurred_at": "2026-07-24T08:30:00Z",
        "sequence": sequence,
    }
    payload.update(extra)
    return payload


def test_manager_owns_one_receiver_connection_inventory(runtime):
    manager = runtime.ws_manager.WSManager()
    assert isinstance(manager.receiver_connection_inventory, ActiveReceiverConnectionInventory)
    assert manager.receiver_connection_inventory.snapshot().total_active_count == 0


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Basic invalid", "Bearer invalid", "Bearer invalid extra"],
)
def test_failed_authentication_has_no_manager_inventory_or_health_side_effect(
    runtime, authorization
):
    store = runtime.stores[0]
    before = _health(runtime, store.id)

    async def scenario():
        websocket = InventoryWebSocket(authorization)
        await runtime.server.ws_receiver(websocket)
        assert websocket.accepted is False
        assert websocket.close_calls == [(AUTH_FAILURE_CODE, AUTH_FAILURE_REASON)]

    asyncio.run(scenario())
    assert runtime.manager.receiver_connection_inventory.snapshot().total_active_count == 0
    assert runtime.manager.online_store_ids() == set()
    assert runtime.manager.get_receiver_snapshot(store.id) is None
    assert _health(runtime, store.id) == before


def test_browser_query_credential_remains_unsupported(runtime):
    store = runtime.stores[0]

    async def scenario():
        websocket = InventoryWebSocket(None, query_token=store.token)
        await runtime.server.ws_receiver(websocket)
        assert websocket.accepted is False
        assert websocket.close_calls == [(AUTH_FAILURE_CODE, AUTH_FAILURE_REASON)]

    asyncio.run(scenario())
    assert runtime.manager.receiver_connection_inventory.snapshot().total_active_count == 0


def test_valid_legacy_authentication_registers_identity_without_health_promotion(runtime):
    store = runtime.stores[0]
    before_token = _health(runtime, store.id)[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        inventory = runtime.manager.receiver_connection_inventory.snapshot()
        assert inventory.total_active_count == 1
        record = inventory.records[0]
        assert record.connection_id == runtime.manager.get_receiver_connection_id(store.id)
        assert record.store_id == store.id
        assert record.device_id is None
        assert record.credential_id is None
        assert record.authentication_source.value == "legacy_store_token"
        assert record.authenticated_at.tzinfo is not None
        assert record.authenticated_at.utcoffset().total_seconds() == 0

        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness is ReadinessState.UNKNOWN
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        assert not hasattr(record, "readiness")
        assert not hasattr(record, "playback")
        manager_metadata = repr(runtime.manager.receiver_connection_ids)
        assert store.token not in manager_metadata
        assert "Authorization" not in manager_metadata
        await _close_receiver(websocket, task)

    asyncio.run(scenario())
    assert _health(runtime, store.id)[0] == before_token


def test_normal_disconnect_removes_exact_record_and_marks_current_offline(runtime):
    store = runtime.stores[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        connection_id = runtime.manager.get_receiver_connection_id(store.id)
        await _close_receiver(websocket, task)
        assert runtime.manager.receiver_connection_inventory.get(connection_id) is None
        assert runtime.manager.get_receiver_connection_id(store.id) is None
        assert runtime.manager.online_store_ids() == set()
        assert runtime.manager.get_receiver_snapshot(store.id).connection is ConnectionState.OFFLINE

    asyncio.run(scenario())


def test_abrupt_disconnect_and_protocol_exception_both_cleanup(runtime):
    stores = runtime.stores[:2]

    async def scenario():
        first_ws, first_task = await _open_receiver(runtime, stores[0])
        first_id = runtime.manager.get_receiver_connection_id(stores[0].id)
        await first_ws.fail(WebSocketDisconnect(code=1006))
        await asyncio.wait_for(first_task, timeout=1.5)
        assert runtime.manager.receiver_connection_inventory.get(first_id) is None

        second_ws, second_task = await _open_receiver(runtime, stores[1])
        second_id = runtime.manager.get_receiver_connection_id(stores[1].id)
        await second_ws.fail(RuntimeError("synthetic transport failure"))
        await asyncio.wait_for(second_task, timeout=1.5)
        assert runtime.manager.receiver_connection_inventory.get(second_id) is None

    asyncio.run(scenario())


def test_task_cancellation_runs_exact_finally_cleanup(runtime):
    store = runtime.stores[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        connection_id = runtime.manager.get_receiver_connection_id(store.id)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.manager.receiver_connection_inventory.get(connection_id) is None
        assert runtime.manager.get_receiver_connection_id(store.id) is None
        assert runtime.manager.get_receiver_snapshot(store.id).connection is ConnectionState.OFFLINE

    asyncio.run(scenario())


def test_repeated_exact_cleanup_is_idempotent(runtime):
    store = runtime.stores[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        connection_id = runtime.manager.get_receiver_connection_id(store.id)
        await _close_receiver(websocket, task)
        assert runtime.manager.disconnect_receiver(store.id, websocket, connection_id) is False
        assert runtime.manager.disconnect_receiver(store.id, websocket, connection_id) is False
        assert runtime.manager.receiver_connection_inventory.snapshot().generation == 2

    asyncio.run(scenario())


def test_two_stores_have_independent_records_and_legacy_only_summary(runtime):
    first, second = runtime.stores[:2]

    async def scenario():
        (first_ws, first_task), (second_ws, second_task) = await asyncio.gather(
            _open_receiver(runtime, first),
            _open_receiver(runtime, second),
        )
        snapshot = runtime.manager.receiver_connection_inventory.snapshot()
        assert snapshot.total_active_count == 2
        assert {record.store_id for record in snapshot.records} == {first.id, second.id}
        assert all(record.authentication_source.value == "legacy_store_token" for record in snapshot.records)

        captured_at = datetime.now(timezone.utc)
        summary = runtime.manager.get_active_receiver_transition_summary(now=captured_at)
        assert summary.legacy_authenticated_count == 2
        assert summary.hashed_authenticated_count == 0
        assert summary.captured_at == captured_at
        await asyncio.gather(
            _close_receiver(first_ws, first_task),
            _close_receiver(second_ws, second_task),
        )

    asyncio.run(scenario())


def test_replacement_uses_new_id_and_delayed_old_cleanup_cannot_remove_it(runtime):
    store = runtime.stores[0]

    async def scenario():
        old_ws, old_task = await _open_receiver(runtime, store)
        old_id = runtime.manager.get_receiver_connection_id(store.id)
        new_ws, new_task = await _open_receiver(runtime, store)
        new_id = runtime.manager.get_receiver_connection_id(store.id)

        assert new_id != old_id
        assert old_ws.close_calls == [(4001, None)]
        inventory = runtime.manager.receiver_connection_inventory.snapshot()
        assert inventory.total_active_count == 1
        assert inventory.records[0].connection_id == new_id
        assert runtime.manager.receivers[store.id] is new_ws

        await old_ws.disconnect()
        await asyncio.wait_for(old_task, timeout=1.5)
        assert runtime.manager.receivers[store.id] is new_ws
        assert runtime.manager.get_receiver_connection_id(store.id) == new_id
        assert runtime.manager.receiver_connection_inventory.get(new_id) is not None
        assert runtime.manager.get_receiver_snapshot(store.id).connection is ConnectionState.CONNECTED
        assert _health(runtime, store.id)[1] == "online"
        await _close_receiver(new_ws, new_task)

    asyncio.run(scenario())


def test_concurrent_same_store_attempts_leave_one_current_record(runtime):
    store = runtime.stores[0]

    async def scenario():
        sockets = [InventoryWebSocket(f"Bearer {store.token}") for _ in range(4)]
        tasks = [asyncio.create_task(runtime.server.ws_receiver(ws)) for ws in sockets]
        await _wait_until(lambda: all(ws.accepted for ws in sockets))
        await _wait_until(
            lambda: runtime.manager.receiver_connection_inventory.snapshot().total_active_count == 1
        )
        inventory = runtime.manager.receiver_connection_inventory.snapshot()
        current_id = runtime.manager.get_receiver_connection_id(store.id)
        assert len(inventory.records) == 1
        assert inventory.records[0].connection_id == current_id

        for websocket in sockets:
            await websocket.disconnect()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert runtime.manager.receiver_connection_inventory.snapshot().total_active_count == 0

    asyncio.run(scenario())


def test_capacity_failure_has_no_orphan_or_health_write(runtime, capsys, caplog):
    first, second = runtime.stores[:2]
    inventory = ActiveReceiverConnectionInventory(max_connections=1)
    manager = runtime.ws_manager.WSManager(receiver_connection_inventory=inventory)
    runtime.server.manager = manager
    runtime.ws_manager.manager = manager
    runtime.manager = manager
    second_before = _health(runtime, second.id)

    async def scenario():
        first_ws, first_task = await _open_receiver(runtime, first)
        second_ws = InventoryWebSocket(f"Bearer {second.token}")
        await runtime.server.ws_receiver(second_ws)
        assert second_ws.accepted is True
        assert second_ws.close_calls == [(CONNECTION_FAILURE_CODE, CONNECTION_FAILURE_REASON)]
        assert manager.get_receiver_connection_id(second.id) is None
        assert second.id not in manager.online_store_ids()
        assert inventory.snapshot().total_active_count == 1
        await _close_receiver(first_ws, first_task)

    asyncio.run(scenario())
    assert _health(runtime, second.id) == second_before
    output = capsys.readouterr()
    rendered_logs = " ".join(record.getMessage() for record in caplog.records)
    assert output.out == output.err == ""
    assert first.token not in rendered_logs
    assert second.token not in rendered_logs


def test_acknowledgements_change_only_contract_axes_not_inventory_source(runtime):
    store = runtime.stores[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        connection_id = runtime.manager.get_receiver_connection_id(store.id)
        await websocket.send_receiver_message(
            _ack(
                "receiver_ready",
                0,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await _wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).readiness is ReadinessState.READY
        )
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        record = runtime.manager.receiver_connection_inventory.get(connection_id)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness is ReadinessState.READY
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        assert record.authentication_source.value == "legacy_store_token"
        assert record.device_id is None and record.credential_id is None
        await _close_receiver(websocket, task)

    asyncio.run(scenario())


def test_inventory_does_not_change_credential_schema_or_raw_store_identity(runtime):
    store = runtime.stores[0]
    before_schema = _schema_signature(runtime)
    before_token = _health(runtime, store.id)[0]

    async def scenario():
        websocket, task = await _open_receiver(runtime, store)
        await _close_receiver(websocket, task)

    asyncio.run(scenario())
    after_schema = _schema_signature(runtime)
    assert before_schema == after_schema
    assert _health(runtime, store.id)[0] == before_token
    assert "receiver_credentials" not in after_schema
    assert "receiver_credential_migration_state" not in after_schema
    assert "schema_migrations" not in after_schema


def test_new_manager_is_empty_process_local_state(runtime):
    first = runtime.ws_manager.WSManager()
    restarted = runtime.ws_manager.WSManager()
    assert first is not restarted
    assert restarted.receivers == {}
    assert restarted.receiver_connection_ids == {}
    assert restarted.receiver_connection_inventory.snapshot().total_active_count == 0
