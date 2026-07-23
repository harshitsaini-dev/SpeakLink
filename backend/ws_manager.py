"""WebSocket connection manager for HQ + store receivers.

Design:
- Only ONE HQ audio broadcaster is active at a time.
- Receivers connect using their store token; server tracks them by store_id.
- Broadcast session state kept in-memory (session_id + selected store_ids).
- Audio frames from HQ (binary) are fanned out only to LIVE targets.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from fastapi import WebSocket

from receiver_contract import (
    OFFLINE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    ConnectionState,
    ReceiverAcknowledgement,
    ReceiverSnapshot,
    activate_session,
    apply_receiver_ack,
    evaluate_freshness,
    mark_connected,
    mark_disconnected,
    parse_receiver_ack,
)

logger = logging.getLogger("echocast.ws")


class WSManager:
    def __init__(self):
        # store_id -> WebSocket
        self.receivers: Dict[int, WebSocket] = {}
        # Process-local immutable state; acknowledgements never mutate snapshots in place.
        self.receiver_snapshots: Dict[int, ReceiverSnapshot] = {}
        self.stale_after = timedelta(seconds=STALE_AFTER_SECONDS)
        self.offline_after = timedelta(seconds=OFFLINE_AFTER_SECONDS)
        # hq_user_id -> WebSocket (dashboards) — multiple HQ dashboards allowed
        self.hq_dashboards: Dict[str, WebSocket] = {}
        # The single active broadcaster WS (mic uplink)
        self.active_broadcaster_ws: Optional[WebSocket] = None
        # In-memory live session state
        self.live_session_id: Optional[int] = None
        self.live_target_store_ids: Set[int] = set()
        self._lock = asyncio.Lock()

    # ---------- Receivers ----------
    async def connect_receiver(
        self,
        store_id: int,
        ws: WebSocket,
        received_at: datetime | None = None,
    ):
        await ws.accept()
        # Kick out an older connection for the same store
        old = self.receivers.get(store_id)
        if old is not None:
            try:
                await old.close(code=4001)
            except Exception:
                pass
            previous = self.receiver_snapshots.get(store_id)
            if previous is not None and previous.connection is not ConnectionState.OFFLINE:
                self.receiver_snapshots[store_id] = mark_disconnected(
                    previous,
                    received_at or datetime.now(timezone.utc),
                )
        self.receivers[store_id] = ws
        previous = self.receiver_snapshots.get(store_id, ReceiverSnapshot())
        self.receiver_snapshots[store_id] = mark_connected(
            previous,
            received_at or datetime.now(timezone.utc),
        )
        await self._notify_dashboards({"type": "receiver_status", "store_id": store_id, "status": "online"})

    def disconnect_receiver(
        self,
        store_id: int,
        ws: WebSocket,
        received_at: datetime | None = None,
    ) -> bool:
        cur = self.receivers.get(store_id)
        if cur is ws:
            self.receivers.pop(store_id, None)
            snapshot = self.receiver_snapshots.get(store_id)
            if snapshot is not None and snapshot.connection is not ConnectionState.OFFLINE:
                self.receiver_snapshots[store_id] = mark_disconnected(
                    snapshot,
                    received_at or datetime.now(timezone.utc),
                )
            return True
        if cur is None:
            return True
        return False

    def get_receiver_snapshot(self, store_id: int) -> ReceiverSnapshot | None:
        return self.receiver_snapshots.get(store_id)

    def apply_receiver_payload(
        self,
        store_id: int,
        payload: object,
        received_at: datetime | None = None,
    ) -> tuple[ReceiverAcknowledgement, ReceiverSnapshot]:
        snapshot = self.receiver_snapshots.get(store_id)
        if snapshot is None or store_id not in self.receivers:
            raise RuntimeError("receiver has no authenticated connection snapshot")
        acknowledgement = parse_receiver_ack(payload)
        updated = apply_receiver_ack(
            snapshot,
            acknowledgement,
            received_at or datetime.now(timezone.utc),
        )
        self.receiver_snapshots[store_id] = updated
        return acknowledgement, updated

    def evaluate_receiver_freshness(
        self,
        store_id: int,
        now: datetime | None = None,
    ) -> ReceiverSnapshot:
        snapshot = self.receiver_snapshots.get(store_id)
        if snapshot is None:
            raise KeyError(f"receiver snapshot unavailable for store {store_id}")
        updated = evaluate_freshness(snapshot, now or datetime.now(timezone.utc))
        self.receiver_snapshots[store_id] = updated
        return updated

    def prepare_receiver_session(self, store_id: int, session_id: int) -> None:
        snapshot = self.receiver_snapshots.get(store_id)
        if snapshot is None:
            return
        self.receiver_snapshots[store_id] = activate_session(snapshot, session_id)

    def is_receiver_online(self, store_id: int) -> bool:
        snapshot = self.receiver_snapshots.get(store_id)
        return (
            store_id in self.receivers
            and snapshot is not None
            and snapshot.connection is ConnectionState.CONNECTED
        )

    def online_store_ids(self) -> Set[int]:
        return {
            store_id
            for store_id in self.receivers
            if self.is_receiver_online(store_id)
        }

    async def send_to_receiver(self, store_id: int, message: dict):
        ws = self.receivers.get(store_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(message))
            return True
        except Exception as error:
            logger.warning(
                "send_to_receiver(%s) failed after %s",
                store_id,
                type(error).__name__,
            )
            self.disconnect_receiver(store_id, ws)
            return False

    async def send_binary_to_receiver(self, store_id: int, data: bytes):
        ws = self.receivers.get(store_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except Exception as error:
            logger.warning(
                "send_binary(%s) failed after %s",
                store_id,
                type(error).__name__,
            )
            self.disconnect_receiver(store_id, ws)
            return False

    # ---------- HQ Dashboards ----------
    async def connect_hq(self, hq_id: str, ws: WebSocket):
        await ws.accept()
        self.hq_dashboards[hq_id] = ws

    def disconnect_hq(self, hq_id: str, ws: WebSocket):
        cur = self.hq_dashboards.get(hq_id)
        if cur is ws:
            self.hq_dashboards.pop(hq_id, None)

    async def _notify_dashboards(self, msg: dict):
        payload = json.dumps(msg)
        dead = []
        for hq_id, ws in list(self.hq_dashboards.items()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(hq_id)
        for hq_id in dead:
            self.hq_dashboards.pop(hq_id, None)

    async def notify_dashboards(self, msg: dict):
        await self._notify_dashboards(msg)

    # ---------- Broadcaster / Live session ----------
    async def set_broadcaster(self, ws: WebSocket) -> bool:
        async with self._lock:
            if self.active_broadcaster_ws is not None:
                return False
            self.active_broadcaster_ws = ws
            return True

    async def clear_broadcaster(self, ws: WebSocket):
        async with self._lock:
            if self.active_broadcaster_ws is ws:
                self.active_broadcaster_ws = None

    def start_live_session(self, session_id: int, store_ids: Set[int]):
        self.live_session_id = session_id
        self.live_target_store_ids = set(store_ids)
        for store_id in store_ids:
            self.prepare_receiver_session(store_id, session_id)

    def stop_live_session(self):
        self.live_session_id = None
        self.live_target_store_ids = set()

    def is_live(self) -> bool:
        return self.live_session_id is not None

    async def fanout_audio(self, data: bytes):
        if not self.is_live():
            return
        for sid in list(self.live_target_store_ids):
            await self.send_binary_to_receiver(sid, data)


manager = WSManager()
