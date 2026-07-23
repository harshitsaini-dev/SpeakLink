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
from datetime import datetime, timezone
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger("speaklink.ws")


class WSManager:
    def __init__(self):
        # store_id -> WebSocket
        self.receivers: Dict[int, WebSocket] = {}
        # hq_user_id -> WebSocket (dashboards) — multiple HQ dashboards allowed
        self.hq_dashboards: Dict[str, WebSocket] = {}
        # The single active broadcaster WS (mic uplink)
        self.active_broadcaster_ws: Optional[WebSocket] = None
        # In-memory live session state
        self.live_session_id: Optional[int] = None
        self.live_target_store_ids: Set[int] = set()
        self._lock = asyncio.Lock()

    # ---------- Receivers ----------
    async def connect_receiver(self, store_id: int, ws: WebSocket):
        await ws.accept()
        # Kick out an older connection for the same store
        old = self.receivers.get(store_id)
        if old is not None:
            try:
                await old.close(code=4001)
            except Exception:
                pass
        self.receivers[store_id] = ws
        await self._notify_dashboards({"type": "receiver_status", "store_id": store_id, "status": "online"})

    def disconnect_receiver(self, store_id: int, ws: WebSocket):
        cur = self.receivers.get(store_id)
        if cur is ws:
            self.receivers.pop(store_id, None)

    def is_receiver_online(self, store_id: int) -> bool:
        return store_id in self.receivers

    def online_store_ids(self) -> Set[int]:
        return set(self.receivers.keys())

    async def send_to_receiver(self, store_id: int, message: dict):
        ws = self.receivers.get(store_id)
        if ws is None:
            return False
        try:
            await ws.send_text(json.dumps(message))
            return True
        except Exception as e:
            logger.warning(f"send_to_receiver({store_id}) failed: {e}")
            self.receivers.pop(store_id, None)
            return False

    async def send_binary_to_receiver(self, store_id: int, data: bytes):
        ws = self.receivers.get(store_id)
        if ws is None:
            return False
        try:
            await ws.send_bytes(data)
            return True
        except Exception as e:
            logger.warning(f"send_binary({store_id}) failed: {e}")
            self.receivers.pop(store_id, None)
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
