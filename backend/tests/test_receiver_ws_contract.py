"""Isolated integration tests for the authenticated receiver WebSocket path."""
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

from receiver_contract import (
    AcousticState,
    ConnectionState,
    PlaybackState,
    ReadinessState,
)


RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")
DISCONNECT = object()


@dataclass(frozen=True)
class StoreIdentity:
    id: int
    token: str = field(repr=False)


class FakeWebSocket:
    def __init__(self, authorization=None):
        self.headers = {} if authorization is None else {"authorization": authorization}
        self.accepted = False
        self.closed_codes = []
        self.sent_text = []
        self.incoming = asyncio.Queue()

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000):
        self.closed_codes.append(code)

    async def send_text(self, message):
        self.sent_text.append(json.loads(message))

    async def send_bytes(self, data):
        raise AssertionError("These tests do not exercise audio streaming")

    async def receive_text(self):
        value = await self.incoming.get()
        if value is DISCONNECT:
            raise WebSocketDisconnect()
        return value

    async def send_receiver_message(self, payload):
        await self.incoming.put(json.dumps(payload))

    async def disconnect(self):
        await self.incoming.put(DISCONNECT)


@pytest.fixture(scope="module")
def isolated_runtime(tmp_path_factory):
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("receiver-ws-contract") / "contract.db"
    environment = {
        "SPEAKLINK_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"ws-contract-{secrets.token_hex(6)}",
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
        yield SimpleNamespace(
            db=db,
            server=server,
            models=models,
            ws_manager=ws_manager,
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


@pytest.fixture
def store(runtime):
    with runtime.db.SessionLocal() as db:
        row = db.query(runtime.models.Store).order_by(runtime.models.Store.id).first()
        return StoreIdentity(id=row.id, token=row.receiver_token)


def acknowledgement(message_type, sequence, **extra):
    payload = {
        "protocol_version": "1.0",
        "type": message_type,
        "message_id": str(uuid4()),
        "occurred_at": "2026-07-23T12:00:00Z",
        "sequence": sequence,
    }
    payload.update(extra)
    return payload


async def wait_until(predicate, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for receiver WebSocket state")


async def open_receiver(runtime, store):
    websocket = FakeWebSocket(f"Bearer {store.token}")
    task = asyncio.create_task(runtime.server.ws_receiver(websocket))
    await wait_until(
        lambda: websocket.accepted and runtime.manager.get_receiver_snapshot(store.id)
    )
    return websocket, task


async def close_receiver(websocket, task):
    await websocket.disconnect()
    await asyncio.wait_for(task, timeout=1)


def create_active_target(runtime, store_id):
    with runtime.db.SessionLocal() as db:
        user = db.query(runtime.models.HQUser).order_by(runtime.models.HQUser.id).first()
        session = runtime.models.BroadcastSession(
            campaign_name="WS contract test",
            started_by=user.id,
            status="live",
            target_mode="selected",
            selected_store_count=1,
            online_store_count=1,
            offline_store_count=0,
        )
        db.add(session)
        db.flush()
        target = runtime.models.BroadcastTarget(
            session_id=session.id,
            store_id=store_id,
            play_status="pending",
        )
        db.add(target)
        db.commit()
        session_id = session.id
        target_id = target.id
    runtime.manager.start_live_session(session_id, {store_id})
    return session_id, target_id


def test_authenticated_connection_is_connected_not_ready(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.connection is ConnectionState.CONNECTED
        assert snapshot.readiness is ReadinessState.UNKNOWN
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_play_dispatch_leaves_target_pending_and_playback_stopped(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        with runtime.db.SessionLocal() as db:
            user = db.query(runtime.models.HQUser).order_by(runtime.models.HQUser.id).first()
            session = runtime.models.BroadcastSession(
                campaign_name="PLAY must remain pending",
                started_by=user.id,
                status="pending",
                target_mode="selected",
                selected_store_count=1,
                online_store_count=0,
                offline_store_count=1,
            )
            db.add(session)
            db.flush()
            target = runtime.models.BroadcastTarget(
                session_id=session.id,
                store_id=store.id,
                play_status="pending",
            )
            db.add(target)
            db.commit()
            session_id = session.id

        with runtime.db.SessionLocal() as db:
            user = db.query(runtime.models.HQUser).order_by(runtime.models.HQUser.id).first()
            await runtime.server.start_session(session_id, db, user)

        with runtime.db.SessionLocal() as db:
            target = db.query(runtime.models.BroadcastTarget).filter_by(session_id=session_id).one()
            assert target.play_status == "pending"
            assert target.started_playing_at is None
            assert target.command_sent_at is not None
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.active_session_id == session_id
        assert snapshot.playback is PlaybackState.STOPPED
        assert any(message["type"] == "play" for message in websocket.sent_text)
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_heartbeat_refreshes_connection_only_and_is_not_persisted(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        before = runtime.manager.get_receiver_snapshot(store.id)
        await websocket.send_receiver_message(acknowledgement("heartbeat", 1))
        await wait_until(lambda: runtime.manager.get_receiver_snapshot(store.id).last_sequence == 1)
        after = runtime.manager.get_receiver_snapshot(store.id)
        assert after.connection is ConnectionState.CONNECTED
        assert after.readiness is ReadinessState.UNKNOWN
        assert after.playback is PlaybackState.STOPPED
        assert after.last_received_at >= before.last_received_at
        with runtime.db.SessionLocal() as db:
            assert db.query(runtime.models.ReceiverEvent).filter_by(
                store_id=store.id,
                event_type="heartbeat",
            ).count() == 0
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_receiver_ready_updates_readiness_only(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).readiness
            is ReadinessState.READY
        )
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_audio_receiving_updates_live_snapshot(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(
            acknowledgement("audio_receiving", 2, session_id=session_id)
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).playback
            is PlaybackState.AUDIO_RECEIVING
        )
        with runtime.db.SessionLocal() as db:
            target = db.query(runtime.models.BroadcastTarget).filter_by(
                session_id=session_id,
                store_id=store.id,
            ).one()
            assert target.play_status == "audio_receiving"
            assert target.started_playing_at is None
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_playback_confirmed_updates_target_at_server_receipt_time(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(
            acknowledgement("audio_receiving", 2, session_id=session_id)
        )
        before_receipt = datetime.now(timezone.utc)
        await websocket.send_receiver_message(
            acknowledgement("playback_confirmed", 3, session_id=session_id)
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).playback
            is PlaybackState.PLAYBACK_CONFIRMED
        )
        after_receipt = datetime.now(timezone.utc)
        with runtime.db.SessionLocal() as db:
            target = db.query(runtime.models.BroadcastTarget).filter_by(
                session_id=session_id,
                store_id=store.id,
            ).one()
            assert target.play_status == "playback_confirmed"
            assert target.started_playing_at is not None
            stored_receipt = target.started_playing_at
            if stored_receipt.tzinfo is None:
                stored_receipt = stored_receipt.replace(tzinfo=timezone.utc)
            assert before_receipt <= stored_receipt <= after_receipt
        await close_receiver(websocket, task)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("bad_payload", "expected_code"),
    [
        (lambda sid: acknowledgement("audio_receiving", 2, session_id=sid + 1), "WRONG_SESSION"),
        (
            lambda sid: acknowledgement(
                "speaker_verified",
                2,
                session_id=sid,
                source="linkguard",
            ),
            "INVALID_ACKNOWLEDGEMENT",
        ),
        (
            lambda sid: acknowledgement(
                "heartbeat",
                2,
                store_id=999999,
            ),
            "INVALID_ACKNOWLEDGEMENT",
        ),
    ],
)
def test_invalid_session_speaker_or_store_claim_is_safely_rejected(
    runtime,
    store,
    bad_payload,
    expected_code,
):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(bad_payload(session_id))
        rejection = await wait_until(
            lambda: next(
                (message for message in websocket.sent_text if message.get("type") == "ack_rejected"),
                None,
            )
        )
        assert rejection == {"type": "ack_rejected", "code": expected_code}
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_duplicate_and_out_of_order_messages_are_deterministically_rejected(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        heartbeat = acknowledgement("heartbeat", 1)
        await websocket.send_receiver_message(heartbeat)
        await wait_until(lambda: runtime.manager.get_receiver_snapshot(store.id).last_sequence == 1)
        await websocket.send_receiver_message(heartbeat)
        await websocket.send_receiver_message(acknowledgement("heartbeat", 0))
        await wait_until(
            lambda: len(
                [message for message in websocket.sent_text if message.get("type") == "ack_rejected"]
            ) == 2
        )
        codes = [
            message["code"]
            for message in websocket.sent_text
            if message.get("type") == "ack_rejected"
        ]
        assert codes == ["DUPLICATE_MESSAGE", "NON_MONOTONIC_SEQUENCE"]
        assert runtime.manager.get_receiver_snapshot(store.id).last_sequence == 1
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_malformed_message_is_rejected_without_closing_connection(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        await websocket.incoming.put("not-json")
        rejection = await wait_until(
            lambda: next(
                (message for message in websocket.sent_text if message.get("type") == "ack_rejected"),
                None,
            )
        )
        assert rejection == {"type": "ack_rejected", "code": "INVALID_ACKNOWLEDGEMENT"}
        await websocket.send_receiver_message(acknowledgement("heartbeat", 1))
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).last_sequence == 1
        )
        assert runtime.manager.get_receiver_snapshot(store.id).connection is ConnectionState.CONNECTED
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_stale_and_offline_boundaries_update_live_snapshot(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        connected = runtime.manager.get_receiver_snapshot(store.id)
        stale = runtime.manager.evaluate_receiver_freshness(
            store.id,
            connected.last_received_at + runtime.manager.stale_after,
        )
        assert stale.connection is ConnectionState.NETWORK_ERROR
        assert stale.readiness is ReadinessState.UNKNOWN
        assert stale.playback is PlaybackState.STOPPED

        offline = runtime.manager.evaluate_receiver_freshness(
            store.id,
            connected.last_received_at + runtime.manager.offline_after,
        )
        assert offline.connection is ConnectionState.OFFLINE
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_disconnect_clears_readiness_and_playback_confirmation(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(
            acknowledgement("audio_receiving", 2, session_id=session_id)
        )
        await websocket.send_receiver_message(
            acknowledgement("playback_confirmed", 3, session_id=session_id)
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).playback
            is PlaybackState.PLAYBACK_CONFIRMED
        )
        await close_receiver(websocket, task)
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.connection is ConnectionState.OFFLINE
        assert snapshot.readiness is ReadinessState.UNKNOWN
        assert snapshot.playback is PlaybackState.STOPPED
        assert snapshot.acoustic is AcousticState.UNVERIFIED

    asyncio.run(scenario())


def test_matching_stopped_acknowledgement_updates_snapshot_and_target(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(
            acknowledgement("audio_receiving", 2, session_id=session_id)
        )
        await websocket.send_receiver_message(
            acknowledgement("stopped", 3, session_id=session_id, reason="requested")
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).playback
            is PlaybackState.STOPPED
            and runtime.manager.get_receiver_snapshot(store.id).last_sequence == 3
        )
        with runtime.db.SessionLocal() as db:
            target = db.query(runtime.models.BroadcastTarget).filter_by(
                session_id=session_id,
                store_id=store.id,
            ).one()
            assert target.play_status == "stopped"
            assert target.stopped_at is not None
        await close_receiver(websocket, task)

    asyncio.run(scenario())


def test_device_and_playback_errors_remain_distinct(runtime, store):
    async def scenario():
        websocket, task = await open_receiver(runtime, store)
        await websocket.send_receiver_message(
            acknowledgement(
                "device_error",
                1,
                error_code="OUTPUT_DEVICE_MISSING",
                details="Output device unavailable.",
            )
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).readiness
            is ReadinessState.DEVICE_ERROR
        )
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.playback is PlaybackState.STOPPED
        await close_receiver(websocket, task)

        websocket, task = await open_receiver(runtime, store)
        session_id, _ = create_active_target(runtime, store.id)
        await websocket.send_receiver_message(
            acknowledgement(
                "receiver_ready",
                1,
                software_checks_passed=True,
                output_device_checks_passed=True,
            )
        )
        await websocket.send_receiver_message(
            acknowledgement("audio_receiving", 2, session_id=session_id)
        )
        await websocket.send_receiver_message(
            acknowledgement(
                "playback_error",
                3,
                session_id=session_id,
                error_code="PIPELINE_FAILURE",
                details="Decoder stopped.",
            )
        )
        await wait_until(
            lambda: runtime.manager.get_receiver_snapshot(store.id).playback
            is PlaybackState.PLAYBACK_ERROR
        )
        snapshot = runtime.manager.get_receiver_snapshot(store.id)
        assert snapshot.readiness is ReadinessState.UNKNOWN
        with runtime.db.SessionLocal() as db:
            target = db.query(runtime.models.BroadcastTarget).filter_by(
                session_id=session_id,
                store_id=store.id,
            ).one()
            assert target.play_status == "playback_error"
        await close_receiver(websocket, task)

    asyncio.run(scenario())
