"""Several live broadcasts at once, each owning only its own Stores and audio.

WHAT THIS REPLACES

``WSManager`` held one ``active_broadcaster_ws``, one ``live_session_id``, one
``live_target_store_ids`` and one ``AudioFanout``. Each of those is a
statement that SpeakLink has exactly one broadcast. Together they made the
concurrent case not merely unsupported but dangerous:

* a second session overwrote the first's target set, so the first kept
  streaming to Stores nothing had a record of;
* ``_end_session`` sent STOP to whatever the singleton currently listed, so
  stopping the second broadcast silenced the first one's Stores;
* a broadcaster disconnect auto-stopped ``live_session_id`` - whichever
  session happened to be in that field at the time.

WHY OWNERSHIP IS SESSION -> STORE, NOT STORE -> QUEUE

The database lease already guarantees a Store is in at most one live session,
so a global ``store_id -> queue`` map would be *correct* today. It is still
the wrong shape. Teardown and acknowledgement ownership have to be explicit:
ending session A must reap exactly A's pumps, and a late acknowledgement from
a Store must be attributable to the session that asked for it. A global map
makes both of those a lookup that happens to work rather than a fact.

WHAT THIS DELIBERATELY DOES NOT DO

It does not enforce Store exclusivity. That belongs to
``broadcast_reservation`` and its partial unique index. A second in-memory
exclusivity check here could disagree with the database - and after a restart
the in-memory one would be the one that is wrong. So this runtime accepts
whatever the reservation layer allowed, and has no opinion about it.

It holds no module-level mutable state. One instance is owned by WSManager,
which is where the Receiver connections it needs already live.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from audio_streaming import DEFAULT_STORE_QUEUE_CAPACITY, AudioFanout
from web_audience import WebAudienceRelay

logger = logging.getLogger("speaklink.broadcast")

__all__ = ["BroadcastRuntime", "LiveBroadcast"]


@dataclass
class LiveBroadcast:
    """One broadcast that is on air, and everything it owns."""

    session_id: int
    owner_user_id: int
    target_store_ids: frozenset
    fanout: AudioFanout
    started_at: datetime
    #: The HQ microphone socket, once one connects. None between the session
    #: going live and its operator opening the console - a real state, not a
    #: missing one, and audio simply has no source until it is filled.
    broadcaster_ws: object | None = None
    #: This Broadcast's web audience. One relay per Broadcast, created with the
    #: session and closed with it, so one Broadcast's initialization segment and
    #: Clusters can never reach another's listener. Web delivery is a sibling of
    #: Store fanout and recording: none of the three can delay the others.
    web_relay: WebAudienceRelay | None = None
    _started_stores: set = field(default_factory=set)


class BroadcastRuntime:
    """Every live broadcast, keyed by session id.

    ``sender_factory(store_id)`` returns the coroutine that actually writes a
    chunk to that Store's Receiver socket. Injected rather than imported so
    this module never reaches back into WSManager, and so the tests can record
    exactly what each Store received.
    """

    def __init__(self, *, sender_factory,
                 capacity: int = DEFAULT_STORE_QUEUE_CAPACITY) -> None:
        self._sender_factory = sender_factory
        self._capacity = capacity
        self._sessions: dict[int, LiveBroadcast] = {}
        self._lock = asyncio.Lock()

    # ---------- lifecycle ----------
    async def start(self, *, session_id: int, owner_user_id: int,
                    target_store_ids) -> LiveBroadcast:
        """Register a session as live. Its Stores were already reserved."""
        async with self._lock:
            live = LiveBroadcast(
                session_id=session_id,
                owner_user_id=owner_user_id,
                target_store_ids=frozenset(target_store_ids),
                fanout=AudioFanout(capacity=self._capacity),
                started_at=datetime.now(timezone.utc),
                web_relay=WebAudienceRelay(session_id=session_id),
            )
            self._sessions[session_id] = live
            return live

    async def end(self, session_id: int) -> "LiveBroadcast | None":
        """Tear down ONE session. Returns it, or None if it was not live.

        Closes only this session's queues, cancels only its pump tasks and
        closes only its broadcaster socket. Every other live session is
        untouched, which is the whole point of the split.

        Idempotent: ending an unknown or already-ended session returns None
        rather than raising, because the callers are cleanup paths - a
        disconnect handler, a stop route, a reconciliation sweep - and any of
        them can legitimately arrive second.
        """
        async with self._lock:
            live = self._sessions.pop(session_id, None)
        if live is None:
            return None

        await live.fanout.stop_all()
        if live.web_relay is not None:
            # Closes every listener queue and sender task and clears this
            # Broadcast's bootstrap cache, so no buffer outlives the session.
            await live.web_relay.close()
        socket = live.broadcaster_ws
        live.broadcaster_ws = None
        if socket is not None:
            try:
                await socket.close(code=1000)
            except Exception:
                # A socket that is already gone is the normal case here.
                pass
        return live

    # ---------- lookups ----------
    def get(self, session_id: int) -> "LiveBroadcast | None":
        return self._sessions.get(session_id)

    def is_live(self, session_id: "int | None" = None) -> bool:
        """Whether one session is live, or - with no argument - whether any is.

        The no-argument form exists only for the few callers that genuinely
        ask "is anything on air?", such as the guard that refuses to archive a
        Store mid-announcement.
        """
        if session_id is None:
            return bool(self._sessions)
        return session_id in self._sessions

    def active_session_ids(self) -> tuple:
        return tuple(sorted(self._sessions))

    def live_store_ids(self) -> frozenset:
        """Every Store currently receiving any live broadcast."""
        stores: set = set()
        for live in self._sessions.values():
            stores |= set(live.target_store_ids)
        return frozenset(stores)

    def session_id_for_store(self, store_id: int) -> "int | None":
        """Which session is broadcasting to this Store.

        Used when a Receiver reconnects mid-broadcast: it must be told the
        session it is rejoining, not "the" session. At most one can match -
        the database lease is what makes that true.
        """
        for session_id in sorted(self._sessions):
            if store_id in self._sessions[session_id].target_store_ids:
                return session_id
        return None

    def session_id_for_socket(self, ws) -> "int | None":
        """Which session a broadcaster socket belongs to.

        How a disconnect handler learns WHICH session just lost its operator.
        Getting this wrong is precisely how a disconnect on A stopped B.
        """
        for session_id, live in self._sessions.items():
            if live.broadcaster_ws is ws:
                return session_id
        return None

    def owner_of(self, session_id: int) -> "int | None":
        live = self._sessions.get(session_id)
        return live.owner_user_id if live else None

    # ---------- broadcaster sockets ----------
    async def attach_broadcaster(self, session_id: int, ws, *,
                                 owner_user_id: int) -> bool:
        """Bind one microphone socket to one session. False if refused.

        Two refusals, both deliberate:

        NOT THE OWNER. A session id is a small integer, so guessing one is
        trivial - ownership is what stops a second operator streaming into
        somebody else's broadcast, not obscurity.

        ALREADY HELD. The first socket keeps the session. Replacing it would
        let anyone holding a valid ticket evict the operator who is mid
        announcement, which is a denial of service wearing a reconnect's
        clothes. A genuine reconnect works because the disconnect path detaches
        first.
        """
        async with self._lock:
            live = self._sessions.get(session_id)
            if live is None:
                return False
            if live.owner_user_id != owner_user_id:
                return False
            if live.broadcaster_ws is not None:
                return False
            live.broadcaster_ws = ws
            return True

    async def detach_broadcaster(self, session_id: int, ws) -> bool:
        """Release the slot, but only if THIS socket still holds it.

        The identity check is what stops a late cleanup from a superseded
        socket evicting its own replacement - the same rule the Receiver
        connection code already follows, and for the same reason.
        """
        async with self._lock:
            live = self._sessions.get(session_id)
            if live is None or live.broadcaster_ws is not ws:
                return False
            live.broadcaster_ws = None
            return True

    # ---------- audio ----------
    async def fanout(self, session_id: int, data: bytes, *,
                     connected_store_ids) -> int:
        """Enqueue one chunk for this session's connected Stores.

        Never awaits a Receiver socket, so one slow Store cannot block the
        broadcaster read loop, the other Stores in this session, or any other
        session. Returns how many queues accepted the chunk without dropping.
        """
        live = self._sessions.get(session_id)
        if live is None:
            return 0
        targets = set(live.target_store_ids) & set(connected_store_ids)
        if not targets:
            return 0

        for store_id in targets - live._started_stores:
            await live.fanout.start_store(store_id,
                                          self._sender_factory(store_id))
            live._started_stores.add(store_id)

        return live.fanout.broadcast(targets, data)

    def metrics(self) -> dict:
        """Per-session, per-Store queue counters. Never an audio payload."""
        return {session_id: live.fanout.all_metrics()
                for session_id, live in sorted(self._sessions.items())}

    def web_relay(self, session_id: int) -> "WebAudienceRelay | None":
        """This session's web audience relay, if the session is live."""
        live = self._sessions.get(session_id)
        return live.web_relay if live is not None else None

    def web_metrics(self) -> dict:
        """Per-session web listener counters. Never an audio payload."""
        return {session_id: live.web_relay.metrics()
                for session_id, live in sorted(self._sessions.items())
                if live.web_relay is not None}
