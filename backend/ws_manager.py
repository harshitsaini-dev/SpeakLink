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

from audio_streaming import DEFAULT_STORE_QUEUE_CAPACITY, AudioFanout
from receiver_connection_inventory import (
    ActiveReceiverConnectionInventory,
    AuthenticatedReceiverConnection,
    ConnectionAuthenticationSource,
)
from receiver_contract import (
    OFFLINE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    ConnectionState,
    ReadinessState,
    ReceiverAcknowledgement,
    ReceiverSnapshot,
    activate_session,
    apply_receiver_ack,
    evaluate_freshness,
    mark_connected,
    mark_disconnected,
    parse_receiver_ack,
)

logger = logging.getLogger("speaklink.ws")


class WSManager:
    def __init__(
        self,
        *,
        receiver_connection_inventory: ActiveReceiverConnectionInventory | None = None,
    ):
        # store_id -> WebSocket. This is the ONLY thing fanout_audio sends to, so
        # membership here is exactly "receives live audio". A standby is
        # deliberately absent rather than present-and-filtered: one place to be
        # right about it, and no way for a future caller to iterate the wrong dict.
        self.receivers: Dict[int, WebSocket] = {}
        # store_id -> server-generated ID for the exact current Receiver socket.
        self.receiver_connection_ids: Dict[int, str] = {}
        # device_id -> WebSocket for Devices that are connected but not primary.
        # They authenticate, heartbeat and report health; they receive no audio.
        self.standby_receivers: Dict[int, WebSocket] = {}
        self.standby_connection_ids: Dict[int, str] = {}
        # device_id -> store_id, so a standby can be found without a database.
        self.standby_store_ids: Dict[int, int] = {}
        # Connection IDs handed over to a replacement but not yet torn down.
        # Their own cleanup must never claim to be the current connection.
        self._superseded_connection_ids: Set[str] = set()
        self.receiver_connection_inventory = (
            receiver_connection_inventory or ActiveReceiverConnectionInventory()
        )
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
        # One bounded audio queue and one sender task per targeted Store, so a
        # slow Receiver can never stall the broadcaster read loop or the other
        # Stores, and no backlog can grow without limit.
        self.audio_fanout = AudioFanout(capacity=DEFAULT_STORE_QUEUE_CAPACITY)
        self._lock = asyncio.Lock()
        self._receiver_lock = asyncio.Lock()

    # ---------- Receivers ----------
    async def connect_receiver(
        self,
        store_id: int,
        ws: WebSocket,
        connection_id: str,
        authenticated_at: datetime,
        authentication_source: ConnectionAuthenticationSource = (
            ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        ),
        device_id: int | None = None,
        credential_id: int | None = None,
        received_at: datetime | None = None,
        is_primary: bool = True,
        demote_superseded_device: bool = False,
    ) -> AuthenticatedReceiverConnection:
        await ws.accept()
        event_time = received_at or authenticated_at
        record = AuthenticatedReceiverConnection(
            connection_id=connection_id,
            store_id=store_id,
            device_id=device_id,
            credential_id=credential_id,
            authentication_source=authentication_source,
            authenticated_at=authenticated_at,
        )

        if device_id is not None and not is_primary:
            await self._connect_standby(record, ws, event_time)
            return record

        async with self._receiver_lock:
            # Preserve the existing one-current-connection-per-Store policy.
            old = self.receivers.get(store_id)
            old_connection_id = self.receiver_connection_ids.get(store_id)

            # A *different* Device taking over can be a promotion rather than a
            # replacement: the demoted computer stays connected and keeps
            # reporting health, and simply leaves the fanout. Closing it would
            # look like a fault on a machine that is working perfectly well.
            #
            # But this is never inferred from the device ids alone, and a test
            # found out why. Under dual_verify a legacy Store token authenticates
            # through the *backfilled* Device, so an ordinary reconnect with an
            # enrolled credential also presents two different device ids while
            # plainly meaning "replace me". Only a caller that has consulted the
            # primary policy knows which of the two this is, so only a caller can
            # ask for it. The default is exactly the old behaviour.
            old_device_id = None
            if old_connection_id is not None:
                old_record = self.receiver_connection_inventory.get(old_connection_id)
                old_device_id = getattr(old_record, "device_id", None)
            if (
                demote_superseded_device
                and old is not None
                and old_device_id is not None
                and device_id is not None
                and old_device_id != device_id
            ):
                self.receivers.pop(store_id, None)
                self.receiver_connection_ids.pop(store_id, None)
                self.standby_receivers[old_device_id] = old
                self.standby_connection_ids[old_device_id] = old_connection_id
                self.standby_store_ids[old_device_id] = store_id
                # The socket survives the handover; the Store's health claims must
                # not. READY and PLAYBACK_CONFIRMED were the old Device's, proved
                # on the old Device's sound card. The incoming primary has to earn
                # them again on its own hardware.
                previous_snapshot = self.receiver_snapshots.get(store_id)
                if (
                    previous_snapshot is not None
                    and previous_snapshot.connection is not ConnectionState.OFFLINE
                ):
                    self.receiver_snapshots[store_id] = mark_disconnected(
                        previous_snapshot, event_time
                    )
                old = None

            if old is not None:
                # Mark the outgoing connection before yielding to close().
                # close() yields the event loop, and the old socket's cleanup
                # path (disconnect_receiver) is synchronous and does not take
                # this lock, so without this marker the old handler's finally
                # block would see itself as the Store's current connection and
                # drive a Store health write for a connection this replacement
                # already owns. The Store keeps a current connection ID
                # throughout the handover, so no observer sees a gap.
                if old_connection_id is not None:
                    self._superseded_connection_ids.add(old_connection_id)
                try:
                    await old.close(code=4001)
                except Exception:
                    pass
                self.receivers.pop(store_id, None)
                self.receiver_connection_ids.pop(store_id, None)
                if old_connection_id is not None:
                    self.receiver_connection_inventory.remove(old_connection_id)
                    self._superseded_connection_ids.discard(old_connection_id)
                previous = self.receiver_snapshots.get(store_id)
                if previous is not None and previous.connection is not ConnectionState.OFFLINE:
                    self.receiver_snapshots[store_id] = mark_disconnected(
                        previous,
                        event_time,
                    )

            # Registration occurs before manager online state. A capacity or
            # conflict failure therefore cannot leave an orphan current socket.
            self.receiver_connection_inventory.register(record)
            self.receivers[store_id] = ws
            self.receiver_connection_ids[store_id] = connection_id
            previous = self.receiver_snapshots.get(store_id, ReceiverSnapshot())
            self.receiver_snapshots[store_id] = mark_connected(previous, event_time)

        await self._notify_dashboards({"type": "receiver_status", "store_id": store_id, "status": "online"})
        return record

    async def _connect_standby(
        self,
        record: AuthenticatedReceiverConnection,
        ws: WebSocket,
        event_time: datetime,
    ) -> None:
        """Register a Device that is connected but must not receive audio.

        It never touches ``self.receivers``, so it cannot end up in the fanout,
        and it replaces only its own previous socket - a standby reconnecting
        must not disturb the primary or any other Device.
        """
        device_id = record.device_id
        async with self._receiver_lock:
            previous = self.standby_receivers.get(device_id)
            previous_connection_id = self.standby_connection_ids.get(device_id)
            if previous is not None:
                if previous_connection_id is not None:
                    self._superseded_connection_ids.add(previous_connection_id)
                try:
                    await previous.close(code=4001)
                except Exception:
                    pass
                if previous_connection_id is not None:
                    self.receiver_connection_inventory.remove(previous_connection_id)
                    self._superseded_connection_ids.discard(previous_connection_id)

            self.receiver_connection_inventory.register(record)
            self.standby_receivers[device_id] = ws
            self.standby_connection_ids[device_id] = record.connection_id
            self.standby_store_ids[device_id] = record.store_id

        await self._notify_dashboards({
            "type": "receiver_status",
            "store_id": record.store_id,
            "device_id": device_id,
            "role": "standby",
            "status": "online",
        })

    def is_standby_connection(self, device_id: int, ws: WebSocket, connection_id: str) -> bool:
        return (
            self.standby_receivers.get(device_id) is ws
            and self.standby_connection_ids.get(device_id) == connection_id
        )

    def disconnect_standby(self, device_id: int, ws: WebSocket, connection_id: str) -> bool:
        """Remove one standby. The primary and every other Device are untouched."""
        if connection_id in self._superseded_connection_ids:
            return False
        if not self.is_standby_connection(device_id, ws, connection_id):
            # An orphan from a replaced socket: drop only its own record.
            if connection_id not in self.standby_connection_ids.values():
                self.receiver_connection_inventory.remove(connection_id)
            return False
        self.standby_receivers.pop(device_id, None)
        self.standby_connection_ids.pop(device_id, None)
        self.standby_store_ids.pop(device_id, None)
        self.receiver_connection_inventory.remove(connection_id)
        return True

    def disconnect_receiver(
        self,
        store_id: int,
        ws: WebSocket,
        connection_id: str | None = None,
        received_at: datetime | None = None,
        device_id: int | None = None,
    ) -> bool:
        if (
            device_id is not None
            and connection_id is not None
            and self.standby_connection_ids.get(device_id) == connection_id
        ):
            return self.disconnect_standby(device_id, ws, connection_id)
        cur = self.receivers.get(store_id)
        current_connection_id = self.receiver_connection_ids.get(store_id)
        exact_connection_id = connection_id or (
            current_connection_id if cur is ws else None
        )
        if (
            exact_connection_id is not None
            and exact_connection_id in self._superseded_connection_ids
        ):
            # A replacement already owns this Store. The outgoing socket must
            # not report itself as current, so the caller performs no Store
            # health write. connect_receiver removes its inventory record.
            return False
        if cur is ws and exact_connection_id == current_connection_id:
            self.receivers.pop(store_id, None)
            self.receiver_connection_ids.pop(store_id, None)
            if exact_connection_id is not None:
                self.receiver_connection_inventory.remove(exact_connection_id)
            snapshot = self.receiver_snapshots.get(store_id)
            if snapshot is not None and snapshot.connection is not ConnectionState.OFFLINE:
                self.receiver_snapshots[store_id] = mark_disconnected(
                    snapshot,
                    received_at or datetime.now(timezone.utc),
                )
            return True

        # Cleanup after replacement is intentionally a no-op for manager state.
        # Remove only an exact orphan that is not the identity of any current
        # Store connection; never remove a newer replacement by Store ID alone.
        if (
            exact_connection_id is not None
            and exact_connection_id not in self.receiver_connection_ids.values()
        ):
            self.receiver_connection_inventory.remove(exact_connection_id)
        return False

    def get_receiver_connection_id(self, store_id: int) -> str | None:
        return self.receiver_connection_ids.get(store_id)

    def is_current_receiver_connection(
        self,
        store_id: int,
        ws: WebSocket,
        connection_id: str,
        device_id: int | None = None,
    ) -> bool:
        """Is this socket the live one for its role?

        A standby is current in its own right. Without this it would look stale
        the moment it connected, and its heartbeats would be discarded - which
        would show an operator a standby that is plainly running as OFFLINE.
        """
        if device_id is not None and self.standby_connection_ids.get(device_id) == connection_id:
            return self.is_standby_connection(device_id, ws, connection_id)
        return (
            self.receivers.get(store_id) is ws
            and self.receiver_connection_ids.get(store_id) == connection_id
        )

    def get_active_receiver_transition_summary(self, now: datetime | None = None):
        """Return source counts only; never invoke a migration transition."""

        from receiver_migration_transition_service import ActiveReceiverConnectionSummary

        return self.receiver_connection_inventory.build_transition_summary(
            ActiveReceiverConnectionSummary,
            captured_at=now or datetime.now(timezone.utc),
        )

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

    def ready_store_ids(self) -> Set[int]:
        """Stores whose Receiver has explicitly acknowledged READY.

        READY is never inferred from being connected: it comes only from a
        ``receiver_ready`` acknowledgement recorded on the live snapshot.
        """
        return {
            store_id
            for store_id, snapshot in self.receiver_snapshots.items()
            if store_id in self.receivers
            and snapshot is not None
            and snapshot.readiness is ReadinessState.READY
        }

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

    def _audio_sender(self, store_id: int):
        async def send(chunk: bytes) -> None:
            delivered = await self.send_binary_to_receiver(store_id, chunk)
            if not delivered:
                # Raising ends this Store's sender task; the fanout logs the
                # failure type only, never the audio payload.
                raise ConnectionError(f"receiver send failed for store {store_id}")

        return send

    async def fanout_audio(self, data: bytes) -> int:
        """Enqueue one audio chunk for every live, connected target Store.

        This never awaits a Receiver socket, so one slow Store cannot block the
        broadcaster read loop or any other Store. Returns how many Store queues
        accepted the chunk without dropping.
        """
        if not self.is_live():
            return 0
        targets = set(self.live_target_store_ids) & set(self.receivers)
        if not targets:
            return 0

        active = set(self.audio_fanout.active_store_ids())
        for store_id in targets - active:
            await self.audio_fanout.start_store(store_id, self._audio_sender(store_id))

        return self.audio_fanout.broadcast(targets, data)

    async def stop_audio_fanout(self) -> dict:
        """Close every Store audio queue and cancel its sender task."""
        return await self.audio_fanout.stop_all()

    def audio_metrics(self) -> dict:
        """Non-secret per-Store queue metrics. Contains no audio payload."""
        return self.audio_fanout.all_metrics()


manager = WSManager()
