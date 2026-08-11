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
from store_late_join import (
    DELIVERY_INITIAL_RAW,
    DELIVERY_LATE_JOIN_FRAMED,
    StoreLateJoinSource,
)
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
    #: Enough of this Broadcast's own stream for a Store to start mid-way: the
    #: initialization segment, and whole Clusters as they complete. Separate
    #: from the web relay on purpose - a physical Store must not fail to join
    #: because the web audience is disabled or degraded for its own reasons.
    late_join: "StoreLateJoinSource | None" = None
    #: store_id -> INITIAL_RAW or LATE_JOIN_FRAMED. Explicit, never inferred
    #: from what happens to be in a queue: a Store that joined late stays on
    #: framed delivery for its whole participation, because the moment it went
    #: back to raw chunks would be the middle of a Cluster.
    delivery_modes: dict = field(default_factory=dict)
    #: Stores added AFTER this Broadcast went live. Kept beside the frozen
    #: initial set rather than replacing it, so "who was targeted at start"
    #: stays answerable and a dynamic Add cannot quietly rewrite history.
    #: Mutated only under the runtime lock.
    dynamic_store_ids: set = field(default_factory=set)

    @property
    def all_target_store_ids(self) -> frozenset:
        return frozenset(self.target_store_ids) | frozenset(self.dynamic_store_ids)
    #: Which Stores have a pump is asked of the fanout, which knows whether the
    #: task is still alive. This used to be a set here that was only ever added
    #: to, so a Store whose pump had died stayed "started" and never got
    #: another - the whole reason a reconnecting Receiver went silent.


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
                late_join=StoreLateJoinSource(session_id=session_id),
            )
            # Everything targeted at start is on the raw path, unchanged.
            for store_id in live.target_store_ids:
                live.delivery_modes[store_id] = DELIVERY_INITIAL_RAW
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
        targets = set(live.all_target_store_ids) & set(connected_store_ids)
        if not targets:
            return 0

        # Ask the fanout whether each Store has a LIVE pump, rather than
        # remembering that it once started one.
        #
        # A pump ends the first time a send raises, which is what a dropped
        # Receiver socket produces. The old add-only set meant that Store was
        # marked started for the rest of the Broadcast, so when its Receiver
        # came back it got a `play` and no audio - its queue accepted chunks
        # and dropped the oldest with nothing draining it. Asking about the
        # present state instead means a reconnection simply gets a new pump and
        # a new empty queue on the next chunk.
        for store_id in targets:
            if not live.fanout.is_pumping(store_id):
                await live.fanout.start_store(store_id,
                                              self._sender_factory(store_id))

        # Frame the stream for whoever may join later. Done for every chunk so
        # the initialization segment is already in hand when an operator adds a
        # Store, rather than making them wait for the next header - which, in a
        # single continuous MediaRecorder stream, never comes.
        clusters = []
        if live.late_join is not None:
            clusters = live.late_join.offer(data)

        raw_targets = {s for s in targets
                       if live.delivery_modes.get(s, DELIVERY_INITIAL_RAW)
                       != DELIVERY_LATE_JOIN_FRAMED}
        framed_targets = targets - raw_targets

        # Stores present from the start receive the broadcaster's chunks
        # untouched. That path is physically accepted and is not being changed.
        accepted = live.fanout.broadcast(raw_targets, data)

        # Late joiners receive WHOLE Clusters and nothing else, for the whole
        # of their participation. A chunk that completes no Cluster sends them
        # nothing, which is correct: a partial Cluster is not deliverable.
        if framed_targets and clusters:
            for frame in clusters:
                accepted += live.fanout.broadcast(framed_targets, frame.data)
        return accepted

    async def join_store_at_live_edge(self, session_id: int, store_id: int):
        """Start delivering to a Store that was not there at the beginning.

        Sends the initialization segment, then leaves the Store on framed
        delivery so everything after it arrives as whole Clusters. Returns a
        small, secret-free summary, or None if this session is not live.

        Refuses rather than improvises when the stream cannot be framed yet:
        sending a joining Store the middle of a Cluster would leave its decoder
        with nothing to open, and a Store that fails to join is a far better
        outcome than a Store that appears to have joined and is silent.
        """
        async with self._lock:
            live = self._sessions.get(session_id)
            if live is None or live.late_join is None:
                return None
            bootstrap = live.late_join.bootstrap()
            if bootstrap is None:
                return {"joined": False,
                        "reason": live.late_join.framing_error
                        or "no initialization segment yet"}

            # The mode is set BEFORE the queue exists, so the very first chunk
            # this Store could be offered is already routed as framed.
            live.delivery_modes[store_id] = DELIVERY_LATE_JOIN_FRAMED
            live.dynamic_store_ids.add(store_id)
            await live.fanout.start_store(store_id,
                                          self._sender_factory(store_id))
            for payload in bootstrap.payloads:
                live.fanout.broadcast({store_id}, payload)
        return {
            "joined": True,
            "init_bytes": len(bootstrap.init_segment),
            "bootstrap_clusters": len(bootstrap.clusters),
            "next_cluster_index": bootstrap.next_cluster_index,
        }

    def delivery_mode(self, session_id: int, store_id: int) -> str | None:
        live = self._sessions.get(session_id)
        return None if live is None else live.delivery_modes.get(store_id)

    def late_join_metrics(self, session_id: int) -> dict | None:
        live = self._sessions.get(session_id)
        if live is None or live.late_join is None:
            return None
        return live.late_join.metrics()

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
