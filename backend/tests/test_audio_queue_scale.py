"""Bounded-queue behaviour the existing suite did not already cover.

WHAT ALREADY EXISTS, AND IS NOT REPEATED HERE

test_audio_protocol.py already proves, against the real StoreAudioQueue and
AudioFanout: the queue is bounded and never exceeds capacity, overflow drops the
OLDEST chunk and records it, one slow Store never blocks another, stop_store
clears a queue and removes it, stop_all leaves no running task, and an invalid
chunk is rejected rather than crashing the fanout. Twenty-nine tests. Re-asserting
any of it would add coverage numbers and no safety.

WHAT WAS GENUINELY MISSING, AND IS HERE

* the high-water mark. ``depth`` is sampled, so a Store that filled its queue and
  drained a moment before anybody looked reads as zero - indistinguishable from a
  Store that never queued anything. ``max_depth`` did not exist at all.
* five Stores at once, rather than the two the isolation tests use.
* a stopped session leaving no queued audio behind, so the next announcement
  cannot open with the tail of the previous one.
* one Store that never accepts anything, alongside Stores that do.

Nothing here opens a socket. The per-Store sender is a plain callable, which is
the seam AudioFanout is built around.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from audio_streaming import AudioFanout, StoreAudioQueue  # noqa: E402

CHUNK = b"\x1a\x45\xdf\xa3" + b"opus-ish-payload"


# ===========================================================================
# The high-water mark
# ===========================================================================
def test_max_depth_records_the_peak_even_after_the_queue_drains():
    """The whole reason this metric exists. A Store that nearly overflowed and
    recovered is the Store worth knowing about BEFORE it starts dropping - and
    a sampled depth of 0 cannot tell it from a Store that queued nothing."""
    queue = StoreAudioQueue(store_id=1, capacity=8)
    for _ in range(5):
        queue.enqueue(CHUNK)
    assert queue.metrics()["depth"] == 5
    assert queue.metrics()["max_depth"] == 5

    async def drain():
        for _ in range(5):
            await queue.get()

    asyncio.run(drain())
    assert queue.metrics()["depth"] == 0, "the queue drained"
    assert queue.metrics()["max_depth"] == 5, "but the peak is still reported"


def test_max_depth_starts_at_zero_and_never_exceeds_capacity():
    queue = StoreAudioQueue(store_id=1, capacity=4)
    assert queue.metrics()["max_depth"] == 0
    for _ in range(50):
        queue.enqueue(CHUNK)
    assert queue.metrics()["max_depth"] == 4
    assert queue.metrics()["dropped"] == 46


def test_max_depth_is_exposed_through_the_fanout_metrics():
    async def scenario():
        fanout = AudioFanout(capacity=6)
        never_sends = asyncio.Event()

        async def blocked(_chunk):
            await never_sends.wait()

        await fanout.start_store(3, blocked)
        for _ in range(4):
            fanout.broadcast([3], CHUNK)
        await asyncio.sleep(0.01)
        metrics = fanout.metrics(3)
        never_sends.set()
        await fanout.stop_all()
        return metrics

    metrics = asyncio.run(scenario())
    assert metrics["max_depth"] >= 1
    assert "max_depth" in metrics


# ===========================================================================
# Five Stores
# ===========================================================================
def test_five_stores_each_get_every_chunk_and_none_is_starved():
    async def scenario():
        fanout = AudioFanout(capacity=16)
        received = {store_id: [] for store_id in (1, 2, 3, 4, 5)}

        def make_sender(store_id):
            async def send(chunk):
                received[store_id].append(chunk)
            return send

        for store_id in received:
            await fanout.start_store(store_id, make_sender(store_id))
        for _ in range(6):
            fanout.broadcast(list(received), CHUNK)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        metrics = fanout.all_metrics()
        await fanout.stop_all()
        return received, metrics

    received, metrics = asyncio.run(scenario())
    for store_id, chunks in received.items():
        assert len(chunks) == 6, f"Store {store_id} received {len(chunks)} of 6"
    for store_id, per_store in metrics.items():
        assert per_store["dropped"] == 0, f"Store {store_id} dropped audio it should not have"


def test_five_stores_have_five_independent_queues():
    """Independent means independent: filling one must leave the others empty."""
    async def scenario():
        fanout = AudioFanout(capacity=3)
        blocked = asyncio.Event()

        async def stuck(_chunk):
            await blocked.wait()

        async def fine(_chunk):
            return None

        await fanout.start_store(1, stuck)
        for store_id in (2, 3, 4, 5):
            await fanout.start_store(store_id, fine)
        # A yield between chunks, because that is what reality does: chunks
        # arrive roughly 250 ms apart, so a healthy sender drains between them.
        # Without it, broadcast() is synchronous and every chunk is enqueued
        # before any sender task is scheduled - so a HEALTHY Store overflows
        # too, and the test measures "how much fits in a queue nothing drains"
        # rather than "does a slow Store punish a healthy one".
        for _ in range(12):
            fanout.broadcast([1, 2, 3, 4, 5], CHUNK)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        metrics = fanout.all_metrics()
        blocked.set()
        await fanout.stop_all()
        return metrics

    metrics = asyncio.run(scenario())
    assert metrics[1]["dropped"] > 0, "the stuck Store should have overflowed"
    for store_id in (2, 3, 4, 5):
        assert metrics[store_id]["dropped"] == 0, (
            f"Store {store_id} was punished for Store 1 being slow")
        assert metrics[store_id]["delivered"] == 12


# ===========================================================================
# A Store that never accepts anything
# ===========================================================================
def test_a_disconnected_store_overflows_alone_and_is_cleaned_up():
    async def scenario():
        fanout = AudioFanout(capacity=2)
        healthy = []

        async def never(_chunk):
            await asyncio.Event().wait()  # never returns

        async def send(chunk):
            healthy.append(chunk)

        await fanout.start_store(9, never)
        await fanout.start_store(10, send)
        # Yielding between chunks, so the healthy Store's sender actually runs -
        # see the note in the five-Store test above.
        for _ in range(8):
            fanout.broadcast([9, 10], CHUNK)
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        before = fanout.all_metrics()
        stopped = await fanout.stop_store(9)
        remaining = fanout.active_store_ids()
        await fanout.stop_all()
        return before, stopped, remaining, healthy

    before, stopped, remaining, healthy = asyncio.run(scenario())
    assert before[9]["dropped"] > 0
    assert before[10]["dropped"] == 0
    assert len(healthy) == 8, "the healthy Store received everything"
    assert stopped is not None, "stopping the dead Store reported its final metrics"
    assert 9 not in remaining, "its queue was removed"
    assert 10 in remaining, "the healthy Store was left alone"


# ===========================================================================
# A stopped session leaves nothing queued
# ===========================================================================
def test_stopping_every_store_leaves_no_queued_audio_behind():
    """Otherwise the next announcement opens with the tail of the previous one -
    the Store plays yesterday's message over today's."""
    async def scenario():
        fanout = AudioFanout(capacity=8)
        blocked = asyncio.Event()

        async def stuck(_chunk):
            await blocked.wait()

        for store_id in (1, 2):
            await fanout.start_store(store_id, stuck)
        for _ in range(6):
            fanout.broadcast([1, 2], CHUNK)
        await asyncio.sleep(0.01)
        queued_before = {sid: m["depth"] for sid, m in fanout.all_metrics().items()}

        final = await fanout.stop_all()
        blocked.set()
        return queued_before, final, fanout.active_store_ids(), fanout.all_metrics()

    queued_before, final, remaining, after = asyncio.run(scenario())
    assert any(depth > 0 for depth in queued_before.values()), (
        "the scenario did not actually build a backlog, so it proves nothing")
    assert remaining == (), "a queue survived the stop"
    assert after == {}, "metrics still describe queues that should be gone"
    assert set(final) == {1, 2}, "the final metrics of each Store were reported"


def test_a_new_session_after_a_stop_starts_from_an_empty_queue():
    """No stale audio can cross a session boundary."""
    async def scenario():
        fanout = AudioFanout(capacity=8)
        blocked = asyncio.Event()
        second_round = []

        async def stuck(_chunk):
            await blocked.wait()

        async def send(chunk):
            second_round.append(chunk)

        await fanout.start_store(1, stuck)
        for _ in range(6):
            fanout.broadcast([1], CHUNK)
        await asyncio.sleep(0.01)
        await fanout.stop_all()
        blocked.set()

        # Second session, same Store id, a Receiver that works.
        await fanout.start_store(1, send)
        metrics_at_start = fanout.metrics(1)
        fanout.broadcast([1], CHUNK)
        await asyncio.sleep(0.02)
        await fanout.stop_all()
        return metrics_at_start, second_round

    metrics_at_start, second_round = asyncio.run(scenario())
    assert metrics_at_start["depth"] == 0
    assert metrics_at_start["enqueued"] == 0, "a fresh queue, not the old one"
    assert metrics_at_start["max_depth"] == 0
    assert len(second_round) == 1, "exactly the new chunk, nothing from before"


# ===========================================================================
# Broadcasting to a Store with no queue must not invent one
# ===========================================================================
def test_broadcasting_to_an_untargeted_store_is_ignored():
    """A Store that was never started has no queue, and one must not appear as
    a side effect of a stray chunk - that is how an unbounded map of queues for
    Stores nobody selected would grow."""
    async def scenario():
        fanout = AudioFanout(capacity=4)
        fanout.broadcast([42], CHUNK)
        return fanout.active_store_ids(), fanout.metrics(42)

    active, metrics = asyncio.run(scenario())
    assert active == ()
    assert metrics is None


# ===========================================================================
# The manager surface
# ===========================================================================
def test_the_manager_exposes_per_store_metrics_without_audio():
    from ws_manager import WSManager

    manager = WSManager()
    metrics = manager.audio_metrics()
    assert isinstance(metrics, dict)
    # No payload of any kind can be in a metrics dict of ints.
    for per_store in metrics.values():
        for value in per_store.values():
            assert isinstance(value, int)


def test_the_capacity_is_about_six_seconds_of_live_audio():
    """24 chunks at ~250 ms. Documented as a latency decision, so a change to it
    is a decision about how far behind a Store may fall - not a tuning knob."""
    from audio_streaming import DEFAULT_STORE_QUEUE_CAPACITY

    assert DEFAULT_STORE_QUEUE_CAPACITY == 24
    approximate_seconds = DEFAULT_STORE_QUEUE_CAPACITY * 0.25
    assert 5.0 <= approximate_seconds <= 7.0
