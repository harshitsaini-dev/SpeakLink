"""Bounded per-Store audio queues for the one-Store live-audio pilot.

The original broadcaster path awaited ``send_bytes`` for each target Store in
sequence, so a single slow Receiver stalled every other Store and there was no
upper bound on backlog. This module replaces that with one bounded queue and
one sender task per Store.

Design rules:

- Every Store owns an independent bounded queue. One Store's backlog can never
  block another Store, and can never grow without limit.
- Overflow is explicit and measured: the oldest chunk is dropped so live audio
  stays current, and a per-Store ``dropped`` counter records it.
- ``broadcast`` is synchronous and never awaits a Receiver, so the WebSocket
  read loop is never blocked by a slow Store.
- Nothing here writes to a database, and no audio bytes are ever logged.

This module is transport-agnostic: the caller supplies the per-Store send
callable, so it imports no FastAPI, WebSocket or SQLAlchemy code.
"""

from __future__ import annotations

import asyncio
from collections import deque
import inspect
import logging
from typing import Awaitable, Callable, Iterable

from audio_protocol import InvalidAudioChunkError, validate_audio_chunk


logger = logging.getLogger("echocast.audio")

# ~250 ms per chunk, so 24 chunks is roughly six seconds of live audio. Beyond
# that a Receiver is too far behind for a live announcement to be useful, and
# holding more only adds latency and memory.
DEFAULT_STORE_QUEUE_CAPACITY = 24
MIN_STORE_QUEUE_CAPACITY = 1
MAX_STORE_QUEUE_CAPACITY = 512

SenderCallable = Callable[[bytes], None | Awaitable[None]]


class AudioStreamingError(RuntimeError):
    """Base class for controlled audio streaming failures."""


class StoreQueueClosedError(AudioStreamingError):
    """Raised when a chunk is offered to a closed Store queue."""


class InvalidQueueCapacityError(AudioStreamingError):
    """Raised when a queue capacity is outside the supported range."""


class StoreAudioQueue:
    """A bounded, drop-oldest audio queue for exactly one Store."""

    __slots__ = (
        "store_id",
        "_capacity",
        "_items",
        "_closed",
        "_available",
        "_enqueued",
        "_dropped",
        "_delivered",
        "_max_depth",
    )

    def __init__(self, *, store_id: int, capacity: int = DEFAULT_STORE_QUEUE_CAPACITY) -> None:
        if (
            isinstance(capacity, bool)
            or not isinstance(capacity, int)
            or not MIN_STORE_QUEUE_CAPACITY <= capacity <= MAX_STORE_QUEUE_CAPACITY
        ):
            raise InvalidQueueCapacityError("store queue capacity is outside the supported range")
        self.store_id = store_id
        self._capacity = capacity
        self._items: deque[bytes] = deque()
        self._closed = False
        self._available = asyncio.Event()
        self._enqueued = 0
        self._dropped = 0
        self._delivered = 0
        # High-water mark, never reset by draining. A sampled depth of 0
        # cannot distinguish a Store that never queued anything from one
        # that filled up and recovered a moment before anybody looked.
        self._max_depth = 0

    # -- introspection ----------------------------------------------------
    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def depth(self) -> int:
        return len(self._items)

    @property
    def enqueued_count(self) -> int:
        return self._enqueued

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def max_depth(self) -> int:
        return self._max_depth

    @property
    def delivered_count(self) -> int:
        return self._delivered

    @property
    def closed(self) -> bool:
        return self._closed

    def metrics(self) -> dict[str, int]:
        """Non-secret counters. Never any audio payload.

        ``depth`` alone is close to useless for diagnosing a Store: it is
        sampled, and a queue that filled up and drained reads as zero by the
        time anybody looks. ``max_depth`` is the high-water mark, which is what
        actually answers "was this Store ever close to dropping audio?" - and it
        answers it for a Store that dropped nothing, which is precisely the
        Store worth knowing about before it does.
        """
        return {
            "capacity": self._capacity,
            "depth": len(self._items),
            "max_depth": self._max_depth,
            "delivered": self._delivered,
            "dropped": self._dropped,
            "enqueued": self._enqueued,
            "store_id": self.store_id,
        }

    # -- mutation ---------------------------------------------------------
    def enqueue(self, chunk: bytes) -> bool:
        """Offer one chunk. Returns True if it was queued, False if dropped."""
        if self._closed:
            raise StoreQueueClosedError("this Store audio queue is closed")
        validate_audio_chunk(chunk)

        self._enqueued += 1
        payload = bytes(chunk)
        if len(self._items) >= self._capacity:
            # Drop the oldest chunk: for live announcements, stale audio is
            # worth less than current audio, and the queue must stay bounded.
            self._items.popleft()
            self._dropped += 1
            self._items.append(payload)
            self._available.set()
            self._max_depth = max(self._max_depth, len(self._items))
            return False

        self._items.append(payload)
        self._available.set()
        self._max_depth = max(self._max_depth, len(self._items))
        return True

    async def get(self) -> bytes:
        """Await the next chunk. Raises when the queue is closed and drained."""
        while True:
            if self._items:
                chunk = self._items.popleft()
                if not self._items:
                    self._available.clear()
                self._delivered += 1
                return chunk
            if self._closed:
                raise StoreQueueClosedError("this Store audio queue is closed")
            self._available.clear()
            await self._available.wait()

    def clear(self) -> int:
        """Discard every pending chunk. Returns how many were discarded."""
        discarded = len(self._items)
        self._items.clear()
        self._available.clear()
        return discarded

    def close(self) -> int:
        """Close the queue and discard anything still pending."""
        discarded = self.clear()
        self._closed = True
        # Wake any waiting consumer so its task can exit.
        self._available.set()
        return discarded


class AudioFanout:
    """Owns one bounded queue and one sender task per targeted Store."""

    def __init__(self, *, capacity: int = DEFAULT_STORE_QUEUE_CAPACITY) -> None:
        self._capacity = capacity
        self._queues: dict[int, StoreAudioQueue] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def active_store_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._queues))

    def metrics(self, store_id: int) -> dict[str, int] | None:
        queue = self._queues.get(store_id)
        return queue.metrics() if queue is not None else None

    def all_metrics(self) -> dict[int, dict[str, int]]:
        return {store_id: queue.metrics() for store_id, queue in sorted(self._queues.items())}

    async def start_store(self, store_id: int, sender: SenderCallable) -> None:
        """Begin buffering and forwarding audio for one Store."""
        await self.stop_store(store_id)
        queue = StoreAudioQueue(store_id=store_id, capacity=self._capacity)
        self._queues[store_id] = queue
        self._tasks[store_id] = asyncio.create_task(
            self._pump(queue, sender), name=f"echocast-audio-store-{store_id}"
        )

    async def _pump(self, queue: StoreAudioQueue, sender: SenderCallable) -> None:
        while True:
            try:
                chunk = await queue.get()
            except StoreQueueClosedError:
                return
            try:
                result = sender(chunk)
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Never log the audio payload itself.
                logger.warning(
                    "audio send failed for store=%s after %s",
                    queue.store_id,
                    type(error).__name__,
                )
                return

    def broadcast(self, store_ids: Iterable[int], chunk: bytes) -> int:
        """Enqueue one chunk for each targeted Store. Never awaits a Receiver.

        Returns how many Store queues accepted the chunk without dropping.
        """
        try:
            validate_audio_chunk(chunk)
        except InvalidAudioChunkError:
            # A malformed frame must never reach a Store queue, and must not
            # take down the broadcaster read loop either.
            return 0

        accepted = 0
        for store_id in store_ids:
            queue = self._queues.get(store_id)
            if queue is None or queue.closed:
                continue
            try:
                if queue.enqueue(chunk):
                    accepted += 1
            except (StoreQueueClosedError, InvalidAudioChunkError):
                continue
        return accepted

    async def drain(self, store_id: int, *, timeout: float = 5.0) -> bool:
        """Wait until this Store's queue is empty. Used by tests and stop."""
        queue = self._queues.get(store_id)
        if queue is None:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if queue.depth == 0:
                return True
            await asyncio.sleep(0.01)
        return queue.depth == 0

    async def stop_store(self, store_id: int) -> dict[str, int] | None:
        """Close one Store's queue, cancel its task and remove it."""
        queue = self._queues.pop(store_id, None)
        task = self._tasks.pop(store_id, None)
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

    async def stop_all(self) -> dict[int, dict[str, int]]:
        """Close every Store queue. Leaves no sender task running."""
        collected: dict[int, dict[str, int]] = {}
        for store_id in list(self._queues):
            metrics = await self.stop_store(store_id)
            if metrics is not None:
                collected[store_id] = metrics
        return collected
