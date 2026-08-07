"""Bounded live delivery of one Broadcast to browser listeners.

WHERE THIS SITS

The broadcaster socket already fans one chunk out three ways::

    HQ chunk ->  Store bounded queues        (audio_streaming.AudioFanout)
             ->  Recording bounded queue     (broadcast_recording)
             ->  Web listener bounded queues (this module)

The three are siblings and share nothing but the bytes. A web listener cannot
delay a Store, a Store cannot delay a listener, and neither can delay the
recording - because none of the three is ever awaited on the broadcaster's read
loop.

WHY THE PARSER IS HERE ONCE, NOT PER LISTENER

Framing a live WebM stream into decodable units is work that depends only on the
stream, not on who is listening. Doing it per listener would multiply CPU by
audience size for an identical result, and per-listener FFmpeg would multiply
processes. One ``WebmStreamFramer`` per Broadcast produces the init segment and
whole Clusters once; those frames are then handed to each listener's own bounded
queue. See ``webm_stream`` for why whole Clusters, and not the broadcaster's
250 ms chunks, are the unit that a late joiner can decode.

THE SLOW LISTENER POLICY, AND WHY IT DIFFERS FROM A STORE

A Store queue drops its OLDEST chunk to stay live. That is right for a Store:
the Receiver decodes a continuous stream and stale audio is worth less than
current audio.

It is wrong here. A listener's ``SourceBuffer`` is fed structured Clusters, and
silently dropping one from the middle leaves a hole in a timeline the decoder is
still tracking. So a listener that cannot keep up is not quietly degraded - it is
disconnected, told why, and allowed to rejoin, at which point it gets a fresh
bootstrap and starts again at the live edge. A gap the listener knows about beats
a gap it does not.

NOTHING HERE WRITES TO A DATABASE, and no audio bytes are ever logged.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from webm_stream import Frame, WebmFramingError, WebmStreamFramer

logger = logging.getLogger("speaklink.webaudience")

__all__ = [
    "ListenerQueue",
    "ListenerBootstrap",
    "WebAudienceRelay",
    "DEFAULT_LISTENER_QUEUE_CAPACITY",
    "DEFAULT_LIVE_EDGE_CLUSTERS",
]


# One cluster is roughly one 250 ms timeslice, so 16 is about four seconds of
# backlog. A listener further behind than that is not listening live in any
# meaningful sense, and holding more only delays the moment we admit it.
DEFAULT_LISTENER_QUEUE_CAPACITY = 16
MIN_LISTENER_QUEUE_CAPACITY = 2
MAX_LISTENER_QUEUE_CAPACITY = 512

# How much recent media a joiner is given so its decoder has something to start
# on immediately rather than waiting for the next Cluster boundary. Deliberately
# tiny: this is a decoder priming buffer, NOT a recording, and every extra
# cluster here is extra delay between a listener joining and hearing live audio.
DEFAULT_LIVE_EDGE_CLUSTERS = 2
MAX_LIVE_EDGE_CLUSTERS = 8


SenderCallable = Callable[[bytes], None | Awaitable[None]]


class ListenerQueueClosedError(RuntimeError):
    """Raised when a frame is offered to a closed listener queue."""


@dataclass(frozen=True, slots=True)
class ListenerBootstrap:
    """Everything a joining browser needs before it can decode the live edge."""

    init_segment: bytes
    #: Whole Clusters, oldest first. May be empty when a listener joins in the
    #: moment before the first Cluster has completed - which is not an error,
    #: only a listener that will start on the next one.
    clusters: tuple[bytes, ...]
    #: Index of the next Cluster this listener will be sent. Makes a reconnect
    #: distinguishable from a first join without consulting a clock.
    next_cluster_index: int

    @property
    def total_bytes(self) -> int:
        return len(self.init_segment) + sum(len(c) for c in self.clusters)


class ListenerQueue:
    """A bounded queue for exactly one browser listener.

    Overflow is NOT drop-oldest. When a listener falls further behind than its
    capacity allows it is marked slow and closed, because a listener that has
    missed a Cluster cannot be repaired by sending it the next one.
    """

    __slots__ = ("listener_id", "_capacity", "_items", "_closed", "_available",
                 "_enqueued", "_delivered", "_max_depth", "_slow", "_bytes_out",
                 "_since_index", "_skipped")

    def __init__(self, *, listener_id: str,
                 capacity: int = DEFAULT_LISTENER_QUEUE_CAPACITY,
                 since_index: int = 0) -> None:
        if (isinstance(capacity, bool) or not isinstance(capacity, int)
                or not MIN_LISTENER_QUEUE_CAPACITY <= capacity <= MAX_LISTENER_QUEUE_CAPACITY):
            raise ValueError("listener queue capacity is outside the supported range")
        self.listener_id = listener_id
        self._capacity = capacity
        self._items: deque[bytes] = deque()
        self._closed = False
        self._available = asyncio.Event()
        self._enqueued = 0
        self._delivered = 0
        self._max_depth = 0
        self._bytes_out = 0
        self._slow = False
        #: The first Cluster index this listener still needs. Everything at or
        #: before it is already in the bootstrap it was handed, so re-sending it
        #: would duplicate audio the decoder has.
        self._since_index = since_index
        self._skipped = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def slow(self) -> bool:
        """Whether this listener overflowed and is being disconnected."""
        return self._slow

    def metrics(self) -> dict[str, int | bool | str]:
        return {
            "listener_id": self.listener_id,
            "capacity": self._capacity,
            "depth": len(self._items),
            "max_depth": self._max_depth,
            "enqueued": self._enqueued,
            "delivered": self._delivered,
            "bytes_delivered": self._bytes_out,
            "slow": self._slow,
            "closed": self._closed,
            "since_index": self._since_index,
            "skipped_already_bootstrapped": self._skipped,
        }

    def enqueue(self, frame: bytes, *, index: int | None = None) -> bool:
        """Offer one frame. False means this listener is too slow to continue.

        Never raises for an ordinary overflow: the audio path calls this and
        must not be interrupted by one listener's network.

        A frame the listener already received in its bootstrap is skipped rather
        than delivered twice. That is what makes attaching safe regardless of
        when, relative to a Cluster completing, the listener arrived.
        """
        if self._closed:
            raise ListenerQueueClosedError("this listener queue is closed")
        if index is not None and index < self._since_index:
            self._skipped += 1
            return True
        self._enqueued += 1
        if len(self._items) >= self._capacity:
            # Marked, not dropped. Dropping a Cluster leaves the decoder a hole
            # it cannot see; disconnecting gives the listener a clean rejoin.
            self._slow = True
            return False
        self._items.append(frame)
        self._available.set()
        self._max_depth = max(self._max_depth, len(self._items))
        return True

    async def get(self) -> bytes:
        while True:
            if self._items:
                frame = self._items.popleft()
                if not self._items:
                    self._available.clear()
                self._delivered += 1
                self._bytes_out += len(frame)
                return frame
            if self._closed:
                raise ListenerQueueClosedError("this listener queue is closed")
            self._available.clear()
            await self._available.wait()

    def close(self) -> int:
        discarded = len(self._items)
        self._items.clear()
        self._closed = True
        self._available.set()      # wake the sender so its task can exit
        return discarded


class WebAudienceRelay:
    """One Broadcast's framer, bootstrap cache and listener fanout.

    ``on_listener_dropped(listener_id, reason)`` is called when a listener is
    removed by the relay itself rather than by its own socket closing. The
    transport layer uses it to close the WebSocket and record the reason; this
    module deliberately knows nothing about WebSockets.
    """

    def __init__(self, *, session_id: int,
                 capacity: int = DEFAULT_LISTENER_QUEUE_CAPACITY,
                 live_edge_clusters: int = DEFAULT_LIVE_EDGE_CLUSTERS,
                 on_listener_dropped: Callable[[str, str], None] | None = None) -> None:
        if not 0 <= live_edge_clusters <= MAX_LIVE_EDGE_CLUSTERS:
            raise ValueError("live edge cluster count is outside the supported range")
        self.session_id = session_id
        self._framer = WebmStreamFramer()
        self._init: bytes | None = None
        self._recent: deque[bytes] = deque(maxlen=live_edge_clusters)
        self._capacity = capacity
        self._queues: dict[str, ListenerQueue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._on_dropped = on_listener_dropped
        self._next_index = 1
        self._closed = False
        self._degraded: str | None = None
        self._slow_disconnects = 0
        self._clusters_out = 0

    # -- introspection ----------------------------------------------------
    @property
    def ready(self) -> bool:
        """Whether a listener could be bootstrapped right now."""
        return self._init is not None and not self._closed

    @property
    def degraded_reason(self) -> str | None:
        """Why web delivery stopped, if it did. Stores and recording continue."""
        return self._degraded

    @property
    def listener_count(self) -> int:
        return len(self._queues)

    def metrics(self) -> dict:
        """Non-secret runtime counters. Never an audio payload."""
        return {
            "session_id": self.session_id,
            "ready": self.ready,
            "degraded_reason": self._degraded,
            "listeners": len(self._queues),
            "clusters_distributed": self._clusters_out,
            "slow_disconnects": self._slow_disconnects,
            "init_segment_bytes": len(self._init) if self._init else 0,
            "live_edge_clusters": len(self._recent),
            "live_edge_bytes": sum(len(c) for c in self._recent),
            "framer": self._framer.metrics(),
            "queues": {qid: queue.metrics() for qid, queue in sorted(self._queues.items())},
        }

    # -- the audio path ---------------------------------------------------
    def offer(self, chunk: bytes) -> int:
        """Feed one broadcaster chunk. Synchronous, and never awaits a listener.

        Returns how many complete Clusters this chunk completed. Never raises:
        a stream this framer cannot read costs web listeners, and must not cost
        the Stores or the recording that share the same call site.
        """
        if self._closed or self._degraded is not None:
            return 0
        try:
            frames = self._framer.feed(chunk)
        except WebmFramingError as error:
            # Payload-free. The reason is a class name, never audio bytes.
            self._degraded = type(error).__name__
            logger.warning("web relay for session %s stopped framing after %s",
                           self.session_id, self._degraded)
            return 0

        produced = 0
        for frame in frames:
            if frame.is_init:
                self._init = frame.data
                continue
            self._recent.append(frame.data)
            self._next_index = frame.index + 1
            self._distribute(frame)
            produced += 1
            self._clusters_out += 1
        return produced

    def _distribute(self, frame: Frame) -> None:
        too_slow: list[str] = []
        for listener_id, queue in self._queues.items():
            try:
                if not queue.enqueue(frame.data, index=frame.index):
                    too_slow.append(listener_id)
            except ListenerQueueClosedError:
                continue
        for listener_id in too_slow:
            self._slow_disconnects += 1
            # Closing wakes the sender task, which performs the actual removal.
            queue = self._queues.get(listener_id)
            if queue is not None:
                queue.close()
            if self._on_dropped is not None:
                try:
                    self._on_dropped(listener_id, "slow_listener")
                except Exception:
                    logger.warning("listener drop callback failed for %s", listener_id)

    # -- listeners --------------------------------------------------------
    def bootstrap(self) -> ListenerBootstrap | None:
        """What a joining listener must receive before any live Cluster.

        None until the first Cluster has completed - a listener joining in that
        window simply waits, rather than being handed a header it cannot use.
        """
        if self._init is None or self._closed:
            return None
        return ListenerBootstrap(
            init_segment=self._init,
            clusters=tuple(self._recent),
            next_cluster_index=self._next_index,
        )

    async def add_listener(self, listener_id: str, sender: SenderCallable) -> ListenerBootstrap | None:
        """Attach one listener and begin delivering future Clusters to it.

        Replacing an existing id tears the previous queue and task down first,
        so a reconnect never leaves a second sender writing to a dead socket and
        never inherits the stale contents of one.
        """
        if self._closed:
            return None
        await self.remove_listener(listener_id)

        # Bootstrap and registration happen with no await between them, and the
        # queue records the index the bootstrap ended at. Both matter: the first
        # keeps a Cluster from completing in the gap, and the second means that
        # even if a future change introduces a gap, a Cluster already in the
        # bootstrap is skipped rather than played twice. Neither alone is
        # enough - one prevents a hole, the other prevents a stutter.
        boot = self.bootstrap()
        since = boot.next_cluster_index if boot is not None else 0
        queue = ListenerQueue(listener_id=listener_id, capacity=self._capacity,
                              since_index=since)
        self._queues[listener_id] = queue
        self._tasks[listener_id] = asyncio.create_task(
            self._pump(queue, sender), name=f"speaklink-web-listener-{listener_id}")
        return boot

    async def _pump(self, queue: ListenerQueue, sender: SenderCallable) -> None:
        while True:
            try:
                frame = await queue.get()
            except ListenerQueueClosedError:
                return
            try:
                result = sender(frame)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.info("web listener %s send failed after %s",
                            queue.listener_id, type(error).__name__)
                return

    async def remove_listener(self, listener_id: str) -> dict | None:
        queue = self._queues.pop(listener_id, None)
        task = self._tasks.pop(listener_id, None)
        metrics = None
        if queue is not None:
            metrics = queue.metrics()
            queue.close()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return metrics

    async def close(self) -> dict:
        """End the Broadcast's web delivery and leave nothing behind."""
        self._closed = True
        collected = {}
        for listener_id in list(self._queues):
            metrics = await self.remove_listener(listener_id)
            if metrics is not None:
                collected[listener_id] = metrics
        # The bootstrap cache is this Broadcast's alone. Clearing it is what
        # stops one Broadcast's init segment ever reaching another's listener.
        self._init = None
        self._recent.clear()
        return collected
