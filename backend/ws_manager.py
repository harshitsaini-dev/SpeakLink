"""WebSocket connection manager for HQ + store receivers.

Design:
- SEVERAL HQ broadcasts may be live at once, one microphone socket each. Live
  session state lives in ``self.broadcasts`` (see broadcast_runtime), keyed by
  session id, so one session's audio, queues and teardown never touch another's.
- A Store belongs to at most one live session. That rule is enforced by the
  broadcast_store_leases unique index, NOT here - an in-memory copy of it could
  disagree with the database, and after a restart the in-memory one would be
  the one that is wrong.
- Receivers connect using their store token; server tracks them by store_id.
  Receiver connection and health state is deliberately SHARED across sessions:
  a Store has one Receiver whatever is being broadcast to it.
- Audio frames from HQ (binary) are fanned out only to the sending session's
  own live targets.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from fastapi import WebSocket

from audio_streaming import DEFAULT_STORE_QUEUE_CAPACITY
from broadcast_runtime import BroadcastRuntime
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
        #
        # store_id -> the PRIMARY's health. This stays the Store aggregate that the
        # dashboard, the freshness sweep and the session logic are all built on.
        self.receiver_snapshots: Dict[int, ReceiverSnapshot] = {}
        # device_id -> that standby's own health. A separate keyspace, because a
        # standby's acknowledgements are not the Store's:
        #   * sequence is per snapshot and must strictly increase, so a standby
        #     sharing the Store's line locks the primary out arithmetically;
        #   * freshness is decided by last_received_at, so a standby heartbeat
        #     sharing it keeps a switched-off primary looking online - a green
        #     Store and silent speakers;
        #   * a standby is not in the fanout at all, so a playback confirmation
        #     from one is evidence of something that cannot have happened.
        self.standby_snapshots: Dict[int, ReceiverSnapshot] = {}
        self.stale_after = timedelta(seconds=STALE_AFTER_SECONDS)
        self.offline_after = timedelta(seconds=OFFLINE_AFTER_SECONDS)
        # hq_user_id -> WebSocket (dashboards) — multiple HQ dashboards allowed
        self.hq_dashboards: Dict[str, WebSocket] = {}
        # Every live broadcast, keyed by session id. This replaced four
        # singleton fields - one broadcaster socket, one session id, one target
        # set and one AudioFanout - each of which asserted that SpeakLink has
        # exactly one broadcast. Store exclusivity is NOT duplicated here: the
        # broadcast_store_leases unique index is the only authority, and a
        # second in-memory rule could only ever disagree with it.
        self.broadcasts = BroadcastRuntime(
            sender_factory=self._audio_sender,
            capacity=DEFAULT_STORE_QUEUE_CAPACITY,
        )
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

        # This Device is arriving as the primary. If it was registered as a
        # standby, that registration is now stale: the same machine cannot be both,
        # and leaving it behind would show an operator a standby that does not
        # exist and would let its old sequence line rejoin later.
        if device_id is not None:
            await self._release_standby_registration(device_id)

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

            # Its own health line, on its own account. mark_connected accepts only
            # OFFLINE or NETWORK_ERROR, so a reconnecting standby is walked through
            # disconnected first rather than being asked for an illegal transition.
            existing = self.standby_snapshots.get(device_id)
            if existing is not None and existing.connection is not ConnectionState.OFFLINE:
                existing = mark_disconnected(existing, event_time)
            self.standby_snapshots[device_id] = mark_connected(
                existing if existing is not None else ReceiverSnapshot(),
                event_time,
            )

        await self._notify_dashboards({
            "type": "receiver_status",
            "store_id": record.store_id,
            "device_id": device_id,
            "role": "standby",
            "status": "online",
        })

    async def _release_standby_registration(self, device_id: int) -> None:
        """Drop a Device's standby registration because it is becoming the primary.

        The old standby socket is closed: it is a superseded connection from the
        same machine, and the Device now has a primary socket instead. It is marked
        superseded first so its own cleanup performs no Store health write - the
        primary owns the Store's health, and this socket never did.
        """
        async with self._receiver_lock:
            previous = self.standby_receivers.pop(device_id, None)
            previous_connection_id = self.standby_connection_ids.pop(device_id, None)
            self.standby_store_ids.pop(device_id, None)
            # Whatever it proved as a standby, it proved without carrying the
            # Store's audio. The promoted Device starts from honest ignorance.
            self.standby_snapshots.pop(device_id, None)
            if previous is None:
                return
            if previous_connection_id is not None:
                self._superseded_connection_ids.add(previous_connection_id)
            try:
                await previous.close(code=4001)
            except Exception:
                pass
            if previous_connection_id is not None:
                self.receiver_connection_inventory.remove(previous_connection_id)
                self._superseded_connection_ids.discard(previous_connection_id)

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
        # A spare machine unplugged and taken away must not remain in memory as a
        # standby for the lifetime of the process.
        self.standby_snapshots.pop(device_id, None)
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

    def find_device_connection(self, device_id: int):
        """The live socket for one Device, primary or standby, or None.

        Looked up by DEVICE, never by Store: a Store can be carrying a
        different Device's connection, and closing that one because this
        Device was revoked would silence a shop for a reason nobody asked for.
        """
        if device_id is None:
            return None
        standby = self.standby_receivers.get(device_id)
        if standby is not None:
            return ("standby", self.standby_store_ids.get(device_id), standby,
                    self.standby_connection_ids.get(device_id))
        for store_id, connection_id in list(self.receiver_connection_ids.items()):
            record = self.receiver_connection_inventory.get(connection_id)
            if record is not None and getattr(record, "device_id", None) == device_id:
                return ("primary", store_id, self.receivers.get(store_id),
                        connection_id)
        return None

    async def disconnect_device(self, device_id: int, *, code: int = 4403,
                                reason: str = "Receiver Device access withdrawn") -> bool:
        """Close a Device's live socket and drop its runtime registration.

        WHY THIS EXISTS

        Disabling, revoking, archiving or permanently deleting a Receiver
        Device changed the database and nothing else. The Device's WebSocket
        had already authenticated, so it stayed open, stayed registered, and
        kept receiving broadcast audio - a Store whose Device an operator had
        just deleted went READY, AUDIO_RECEIVING and PLAYBACK_CONFIRMED fifty
        seconds later, with the Devices page correctly showing nothing.
        Authentication happened once at connect and was never revisited.

        Closing the socket is the whole fix: the Receiver is removed from the
        fanout, the Store's health stops being refreshed by it, and the next
        connect attempt has to authenticate again - which the revoked
        credential can no longer do.

        Returns False when the Device has no live socket, which is the
        ordinary case and not an error.
        """
        found = self.find_device_connection(device_id)
        if found is None:
            return False
        role, store_id, socket, connection_id = found
        if socket is None:
            return False

        # Deregister BEFORE closing. A close can await, and a socket that is
        # still registered while closing can be handed audio in between.
        if role == "standby":
            self.standby_receivers.pop(device_id, None)
            self.standby_connection_ids.pop(device_id, None)
            self.standby_store_ids.pop(device_id, None)
            self.standby_snapshots.pop(device_id, None)
        else:
            if self.receivers.get(store_id) is socket:
                self.receivers.pop(store_id, None)
                self.receiver_connection_ids.pop(store_id, None)
            snapshot = self.receiver_snapshots.get(store_id)
            if snapshot is not None and snapshot.connection is not ConnectionState.OFFLINE:
                # The Store stops being green immediately rather than waiting
                # for the freshness sweep to notice a socket that is gone.
                self.receiver_snapshots[store_id] = mark_disconnected(
                    snapshot, datetime.now(timezone.utc))
        if connection_id is not None:
            self.receiver_connection_inventory.remove(connection_id)

        try:
            await socket.close(code=code, reason=reason)
        except Exception:
            # Already gone. The registration is removed either way, which is
            # what stops audio reaching it.
            pass
        return True

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

    def get_standby_snapshot(self, device_id: int) -> ReceiverSnapshot | None:
        """One standby's own health. A separate keyspace from Store IDs on purpose -
        Store 8 and Device 8 are different things and must not collide."""
        return self.standby_snapshots.get(device_id)

    def is_registered_standby(self, device_id: int | None) -> bool:
        return device_id is not None and device_id in self.standby_connection_ids

    def apply_receiver_payload(
        self,
        store_id: int,
        payload: object,
        received_at: datetime | None = None,
        device_id: int | None = None,
    ) -> tuple[ReceiverAcknowledgement, ReceiverSnapshot]:
        """Apply one acknowledgement to the snapshot that actually sent it.

        ``device_id`` is how a standby's acknowledgement is kept out of the Store's
        health. It is optional and defaults to the Store, because a Receiver on the
        shared Store token has no Device identity at all and must keep working
        exactly as before - anything else would take every un-enrolled Store off
        the air.
        """
        if self.is_registered_standby(device_id):
            return self.apply_standby_payload(device_id, payload, received_at)
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

    def apply_standby_payload(
        self,
        device_id: int,
        payload: object,
        received_at: datetime | None = None,
    ) -> tuple[ReceiverAcknowledgement, ReceiverSnapshot]:
        """Apply one standby's acknowledgement to that standby's own snapshot.

        The full contract still runs: a standby is not exempt from monotonic
        sequences or duplicate detection, it simply has its own line of them.
        Ignoring standby acknowledgements outright would have been a smaller change
        and would have thrown away the replay protection that makes the contract
        worth having.
        """
        snapshot = self.standby_snapshots.get(device_id)
        if snapshot is None:
            raise RuntimeError("standby has no authenticated connection snapshot")
        acknowledgement = parse_receiver_ack(payload)
        updated = apply_receiver_ack(
            snapshot,
            acknowledgement,
            received_at or datetime.now(timezone.utc),
        )
        self.standby_snapshots[device_id] = updated
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

    def evaluate_standby_freshness(
        self,
        device_id: int,
        now: datetime | None = None,
    ) -> ReceiverSnapshot:
        """A standby that stops answering must be reportable as dead on its own
        account, or the fix trades one blind spot for another."""
        snapshot = self.standby_snapshots.get(device_id)
        if snapshot is None:
            raise KeyError(f"standby snapshot unavailable for device {device_id}")
        updated = evaluate_freshness(snapshot, now or datetime.now(timezone.utc))
        self.standby_snapshots[device_id] = updated
        return updated

    def prepare_receiver_session(self, store_id: int, session_id: int) -> None:
        snapshot = self.receiver_snapshots.get(store_id)
        if snapshot is None:
            return
        self.receiver_snapshots[store_id] = activate_session(snapshot, session_id)

    def connected_device_ids(self) -> Set[int]:
        """Which Devices hold a live socket RIGHT NOW - primary and standby.

        Read-only, and deliberately about the present rather than the past.
        "Has this Device ever connected?" would need a stored event carrying a
        device id, and ``receiver_events`` records only a store_id - so
        answering it from this data would be a guess. This answers the question
        the data can actually support, and callers must not describe it as more.

        Process-local, like every other connection fact here, which is exactly
        why HQ runs one Uvicorn worker.
        """
        connected: Set[int] = set(self.standby_receivers)
        inventory = getattr(self, "receiver_connection_inventory", None)
        if inventory is not None:
            for record in inventory.snapshot().records:
                if record.device_id is not None:
                    connected.add(record.device_id)
        return connected

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

    async def wait_for_store_ready(self, store_id: int, *, timeout: float,
                                   poll: float = 0.25) -> bool:
        """Wait for ONE Store to acknowledge READY, or give up.

        Bounded on purpose. A Store added mid-broadcast has a Receiver that may
        be slow, wedged, or gone between the connectivity check and the prepare
        - and the operator is standing at a console waiting for an answer. A
        wait without a ceiling would leave the request hanging and the Store
        holding a lease nobody released.

        Polling rather than an event: readiness already lives on the connection
        snapshot, and a second notification path would be a second thing that
        can disagree with it.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if store_id in self.ready_store_ids():
                return True
            if store_id not in self.online_store_ids():
                # The socket went away. Waiting for the rest of the timeout
                # would tell the operator nothing they do not already know.
                return False
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(poll)

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

    # ---------- Broadcaster / Live sessions ----------
    async def start_live_session(self, session_id: int, store_ids: Set[int],
                                 *, owner_user_id: int):
        """Put one session on air. Others keep running untouched."""
        live = await self.broadcasts.start(
            session_id=session_id,
            owner_user_id=owner_user_id,
            target_store_ids=store_ids,
        )
        for store_id in store_ids:
            self.prepare_receiver_session(store_id, session_id)
        return live

    async def stop_live_session(self, session_id: int):
        """Tear down ONE session: its queues, its pumps, its socket."""
        return await self.broadcasts.end(session_id)

    def is_live(self, session_id: "int | None" = None) -> bool:
        """One session, or - with no argument - whether anything is on air."""
        return self.broadcasts.is_live(session_id)

    def live_store_ids(self) -> Set[int]:
        """Every Store receiving any live broadcast, across all sessions."""
        return set(self.broadcasts.live_store_ids())

    def _audio_sender(self, store_id: int):
        async def send(chunk: bytes) -> None:
            delivered = await self.send_binary_to_receiver(store_id, chunk)
            if not delivered:
                # Raising ends this Store's sender task; the fanout logs the
                # failure type only, never the audio payload.
                raise ConnectionError(f"receiver send failed for store {store_id}")

        return send

    async def fanout_audio(self, session_id: int, data: bytes) -> int:
        """Enqueue one chunk for ONE session's connected target Stores.

        The session id is required rather than inferred. Inferring it is what
        the singleton did, and it meant every socket fed whatever target set
        happened to be current - so audio from one operator could reach another
        operator's Stores.
        """
        return await self.broadcasts.fanout(
            session_id, data, connected_store_ids=set(self.receivers))

    def audio_metrics(self) -> dict:
        """Per-session, per-Store queue metrics. Never an audio payload."""
        return self.broadcasts.metrics()


manager = WSManager()
