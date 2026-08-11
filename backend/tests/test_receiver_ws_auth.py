"""Isolated authentication tests for the production receiver WebSocket."""
import asyncio
import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import secrets
from types import SimpleNamespace
import sys

import pytest
from fastapi import WebSocketDisconnect

from receiver_contract import ConnectionState


RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")
DISCONNECT = object()
AUTH_FAILURE_CODE = 4401
AUTH_FAILURE_REASON = "Receiver authentication failed"


@dataclass(frozen=True)
class StoreIdentity:
    id: int
    token: str = field(repr=False)


class HeaderWebSocket:
    def __init__(self, authorization: str | None):
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.accepted = False
        self.close_code = None
        self.close_reason = None
        self.incoming = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.close_code = code
        self.close_reason = reason

    async def send_text(self, message):
        json.loads(message)

    async def receive_text(self):
        value = await self.incoming.get()
        if value is DISCONNECT:
            raise WebSocketDisconnect()
        return value

    async def disconnect(self):
        await self.incoming.put(DISCONNECT)


@pytest.fixture(scope="module")
def isolated_runtime(tmp_path_factory):
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("receiver-header-auth") / "auth.db"
    environment = {
        "SPEAKLINK_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"ws-auth-{secrets.token_hex(6)}",
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
            row = session.query(models.Store).order_by(models.Store.id).first()
            store = StoreIdentity(row.id, row.receiver_token)
        yield SimpleNamespace(
            db=db,
            server=server,
            models=models,
            ws_manager=ws_manager,
            store=store,
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


@pytest.fixture
def runtime(isolated_runtime):
    manager = isolated_runtime.ws_manager.WSManager()
    isolated_runtime.server.manager = manager
    isolated_runtime.ws_manager.manager = manager
    return SimpleNamespace(**vars(isolated_runtime), manager=manager)


async def wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for receiver authentication state")


def database_health_state(runtime):
    with runtime.db.SessionLocal() as session:
        store = session.query(runtime.models.Store).filter_by(id=runtime.store.id).one()
        events = session.query(runtime.models.ReceiverEvent).filter_by(
            store_id=runtime.store.id
        ).count()
        return store.status, store.last_seen, events


def test_valid_bearer_header_authenticates_without_promoting_health(runtime):
    async def scenario():
        websocket = HeaderWebSocket(f"Bearer {runtime.store.token}")
        task = asyncio.create_task(runtime.server.ws_receiver(websocket))
        await wait_until(lambda: websocket.accepted)
        snapshot = runtime.manager.get_receiver_snapshot(runtime.store.id)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness.value == "UNKNOWN"
        assert snapshot.playback.value == "STOPPED"
        await websocket.disconnect()
        await asyncio.wait_for(task, timeout=1)

    asyncio.run(scenario())


def test_failed_authentication_has_fixed_safe_result_and_no_side_effects(runtime):
    malformed_with_real_credential = f"Bearer {runtime.store.token} unexpected"
    candidates = (
        None,
        "",
        "Bearer",
        "Basic not-a-receiver-credential",
        "Bearer not-a-receiver-credential",
        malformed_with_real_credential,
    )

    async def scenario():
        for authorization in candidates:
            manager = runtime.ws_manager.WSManager()
            runtime.server.manager = manager
            before = database_health_state(runtime)
            websocket = HeaderWebSocket(authorization)
            await runtime.server.ws_receiver(websocket)
            after = database_health_state(runtime)

            assert websocket.accepted is False
            assert websocket.close_code == AUTH_FAILURE_CODE
            assert websocket.close_reason == AUTH_FAILURE_REASON
            assert manager.get_receiver_snapshot(runtime.store.id) is None
            assert manager.online_store_ids() == set()
            assert after == before
            if runtime.store.token in (websocket.close_reason or ""):
                pytest.fail("Authentication response contained a receiver credential", pytrace=False)

    asyncio.run(scenario())
