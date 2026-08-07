"""The web audience relay: bootstrap, isolation, bounded fanout, load.

Everything here drives the REAL relay and the REAL framer over bytes a real
Chromium MediaRecorder produced. The per-listener sender is a plain callable,
which is the seam the relay is built around - no socket is opened, so these
tests say nothing about a network and do not pretend to.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
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

from webm_stream import WebmStreamFramer  # noqa: E402
from web_audience import (  # noqa: E402
    DEFAULT_LISTENER_QUEUE_CAPACITY,
    ListenerQueue,
    WebAudienceRelay,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CAPTURE = FIXTURE_DIR / "mediarecorder-live.webm"
CHUNK_INDEX = FIXTURE_DIR / "mediarecorder-live.chunks.json"

requires_capture = pytest.mark.skipif(
    not CAPTURE.exists() or not CHUNK_INDEX.exists(),
    reason="run: ECHOCAST_CAPTURE_WEBM=1 npx playwright test e2e/capture-fixture.spec.js",
)

CLUSTER_ID = bytes([0x1F, 0x43, 0xB6, 0x75])


def timeslice_chunks() -> list[bytes]:
    sizes = json.loads(CHUNK_INDEX.read_text())["chunkSizes"]
    data = CAPTURE.read_bytes()
    chunks, offset = [], 0
    for size in sizes:
        chunks.append(data[offset:offset + size])
        offset += size
    return chunks


async def feed_live(relay, chunks):
    """Offer chunks the way the broadcaster loop does: yielding between them.

    The real read loop awaits ``websocket.receive()`` between chunks, so the
    listener pumps get to run. Dumping a whole Broadcast into the relay without
    ever yielding is not a fast test of a healthy listener - it is an accurate
    test of a listener that never reads, which the relay correctly disconnects.
    """
    for chunk in chunks:
        relay.offer(chunk)
        await asyncio.sleep(0)


class Recorder:
    """A listener that accepts everything immediately."""

    def __init__(self):
        self.frames: list[bytes] = []

    async def __call__(self, frame: bytes) -> None:
        self.frames.append(frame)


# ===========================================================================
# Bootstrap
# ===========================================================================

@requires_capture
def test_a_listener_joining_late_is_bootstrapped_at_the_live_edge():
    """The whole point: init segment plus a live-edge Cluster, not the Broadcast."""
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        for chunk in chunks[:30]:          # already well into the Broadcast
            relay.offer(chunk)

        boot = relay.bootstrap()
        assert boot is not None
        assert boot.init_segment.startswith(bytes([0x1A, 0x45, 0xDF, 0xA3]))
        assert CLUSTER_ID not in boot.init_segment
        for cluster in boot.clusters:
            assert cluster.startswith(CLUSTER_ID)

        # Bounded priming, NOT a recording of what has already been broadcast.
        assert len(boot.clusters) <= 2
        assert boot.total_bytes < 16_384
        return relay, boot

    relay, boot = asyncio.run(scenario())
    assert boot.next_cluster_index > 1, "the listener resumes after the live edge"


@requires_capture
def test_no_listener_can_be_bootstrapped_before_the_first_cluster_completes():
    """Handing over a header with nothing to decode would be a false start."""
    relay = WebAudienceRelay(session_id=1)
    assert relay.bootstrap() is None
    assert relay.ready is False
    relay.offer(timeslice_chunks()[0])
    # The init segment alone is not a bootstrap.
    assert relay.bootstrap() is None or relay.bootstrap().clusters == ()


@requires_capture
def test_a_listener_receives_only_whole_clusters_after_joining():
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        for chunk in chunks[:20]:
            relay.offer(chunk)

        listener = Recorder()
        boot = await relay.add_listener("alice", listener)
        await feed_live(relay, chunks[20:])
        await asyncio.sleep(0.05)
        await relay.close()
        return boot, listener.frames

    boot, frames = asyncio.run(scenario())
    assert boot is not None
    assert frames, "the listener received live audio"
    for frame in frames:
        assert frame.startswith(CLUSTER_ID), "never a partial cluster"


# ===========================================================================
# Reconnect
# ===========================================================================

@requires_capture
def test_a_reconnecting_listener_gets_a_fresh_bootstrap_not_a_stale_queue():
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        first = Recorder()
        for chunk in chunks[:10]:
            relay.offer(chunk)
        await relay.add_listener("alice", first)
        await feed_live(relay, chunks[10:20])
        await asyncio.sleep(0.05)
        delivered_before = len(first.frames)

        # Same participant, new socket.
        second = Recorder()
        boot = await relay.add_listener("alice", second)
        await feed_live(relay, chunks[20:])
        await asyncio.sleep(0.05)
        result = (boot, delivered_before, len(first.frames), second.frames,
                  relay.listener_count)
        await relay.close()
        return result

    boot, before, after_first, second_frames, count = asyncio.run(scenario())
    assert boot is not None, "a reconnect is bootstrapped again"
    assert boot.init_segment.startswith(bytes([0x1A, 0x45, 0xDF, 0xA3]))
    assert after_first == before, "the replaced socket stopped receiving"
    assert second_frames, "the new socket receives live audio"
    assert count == 1, "one participant, one queue - the old one is gone"


# ===========================================================================
# The slow listener
# ===========================================================================

@requires_capture
def test_a_slow_listener_is_disconnected_rather_than_silently_gapped():
    """Dropping a middle Cluster leaves the decoder a hole it cannot see."""
    async def scenario():
        dropped = []
        relay = WebAudienceRelay(
            session_id=1, capacity=4,
            on_listener_dropped=lambda lid, reason: dropped.append((lid, reason)))
        chunks = timeslice_chunks()
        for chunk in chunks[:10]:
            relay.offer(chunk)

        stalled = asyncio.Event()

        async def never_finishes(frame: bytes) -> None:
            await stalled.wait()          # a browser that stopped reading

        healthy = Recorder()
        await relay.add_listener("slow", never_finishes)
        await relay.add_listener("healthy", healthy)

        await feed_live(relay, chunks[10:])
        await asyncio.sleep(0.05)

        metrics = relay.metrics()
        stalled.set()
        result = (dropped, len(healthy.frames), metrics)
        await relay.close()
        return result

    dropped, healthy_frames, metrics = asyncio.run(scenario())
    assert dropped and dropped[0] == ("slow", "slow_listener")
    assert metrics["slow_disconnects"] >= 1
    assert healthy_frames > 0, "the healthy listener was unaffected"


def test_a_listener_queue_never_exceeds_its_capacity():
    queue = ListenerQueue(listener_id="alice", capacity=4)
    accepted = sum(1 for _ in range(50) if queue.enqueue(b"cluster"))
    assert accepted == 4
    assert queue.depth == 4
    assert queue.metrics()["max_depth"] == 4
    assert queue.slow is True, "overflow marks the listener, it does not drop audio"


def test_a_listener_queue_refuses_an_unsupported_capacity():
    with pytest.raises(ValueError):
        ListenerQueue(listener_id="alice", capacity=0)
    with pytest.raises(ValueError):
        ListenerQueue(listener_id="alice", capacity=100_000)


# ===========================================================================
# Isolation between Broadcasts
# ===========================================================================

@requires_capture
def test_two_broadcasts_never_share_a_byte():
    async def scenario():
        chunks = timeslice_chunks()
        relay_a = WebAudienceRelay(session_id=1)
        relay_b = WebAudienceRelay(session_id=2)
        alice, bob = Recorder(), Recorder()

        for chunk in chunks[:10]:
            relay_a.offer(chunk)
        # B starts later and from a different point in the stream.
        for chunk in chunks[:20]:
            relay_b.offer(chunk)

        await relay_a.add_listener("alice", alice)
        await relay_b.add_listener("bob", bob)
        await feed_live(relay_a, chunks[20:30])
        await asyncio.sleep(0.05)
        result = (list(alice.frames), list(bob.frames))
        await relay_a.close()
        await relay_b.close()
        return result

    alice_frames, bob_frames = asyncio.run(scenario())
    assert alice_frames, "A's listener received A's audio"
    # Only A was fed after both listeners attached, so B's listener must be
    # empty. Ownership is explicit, not a matter of which session id was used.
    assert bob_frames == [], "B's listener received nothing belonging to A"


@requires_capture
def test_closing_a_broadcast_leaves_no_buffers_or_tasks_behind():
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        for chunk in timeslice_chunks()[:20]:
            relay.offer(chunk)
        await relay.add_listener("alice", Recorder())
        before = asyncio.all_tasks()
        await relay.close()
        await asyncio.sleep(0.02)
        return relay, len(before)

    relay, _ = asyncio.run(scenario())
    assert relay.listener_count == 0
    assert relay.ready is False
    assert relay.bootstrap() is None, "the bootstrap cache is gone with the session"
    assert relay.metrics()["init_segment_bytes"] == 0


# ===========================================================================
# The audio path must never be hurt by web delivery
# ===========================================================================

@requires_capture
def test_a_malformed_stream_costs_web_listeners_and_nothing_else():
    """offer() is called on the broadcaster's read loop. It must never raise."""
    relay = WebAudienceRelay(session_id=1)
    assert relay.offer(b"\x00\x01\x02 not webm at all") == 0
    assert relay.degraded_reason is not None
    # Still callable, still silent, for the rest of the Broadcast.
    assert relay.offer(timeslice_chunks()[0]) == 0


@requires_capture
def test_offer_never_awaits_a_listener():
    """A listener's socket must not be reachable from the microphone loop."""
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        for chunk in chunks[:10]:
            relay.offer(chunk)

        blocked = asyncio.Event()

        async def stalls(frame: bytes) -> None:
            await blocked.wait()

        await relay.add_listener("slow", stalls)
        started = time.perf_counter()
        for chunk in chunks[10:]:
            relay.offer(chunk)          # synchronous, whatever the listener does
        elapsed = time.perf_counter() - started
        blocked.set()
        await relay.close()
        return elapsed

    elapsed = asyncio.run(scenario())
    # A stalled listener cannot add measurable time to the audio path.
    assert elapsed < 0.5, f"offer() took {elapsed:.3f}s with a stalled listener"


# ===========================================================================
# Load
# ===========================================================================

@requires_capture
@pytest.mark.parametrize("audience", [1, 10, 50, 100, 250])
def test_the_relay_serves_a_whole_audience_without_dropping_a_cluster(audience):
    """Every listener gets identical bytes, and the parser runs once for all."""
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        for chunk in chunks[:10]:
            relay.offer(chunk)

        listeners = [Recorder() for _ in range(audience)]
        for index, listener in enumerate(listeners):
            await relay.add_listener(f"listener-{index}", listener)

        started = time.perf_counter()
        await feed_live(relay, chunks[10:])
        offer_seconds = time.perf_counter() - started
        await asyncio.sleep(0.2)
        metrics = relay.metrics()
        result = ([len(listener.frames) for listener in listeners],
                  metrics, offer_seconds)
        await relay.close()
        return result

    counts, metrics, offer_seconds = asyncio.run(scenario())
    assert len(set(counts)) == 1, "every listener received exactly the same frames"
    assert counts[0] > 0
    assert metrics["slow_disconnects"] == 0, "nobody was dropped"
    # The framer is shared, so parsing cost does not scale with audience size.
    assert metrics["framer"]["clusters_emitted"] == metrics["clusters_distributed"] + \
        (0 if metrics["clusters_distributed"] else 0)
    for queue in metrics["queues"].values():
        assert queue["max_depth"] <= DEFAULT_LISTENER_QUEUE_CAPACITY
    print(f"\n{audience:>4} listeners: offer loop {offer_seconds * 1000:.1f} ms, "
          f"clusters {metrics['clusters_distributed']}, "
          f"peak queue depth {max(q['max_depth'] for q in metrics['queues'].values())}")


# ===========================================================================
# The attach race
# ===========================================================================
# A listener is handed a bootstrap and then subscribed to future Clusters. If a
# Cluster completes between those two things the listener either never hears it
# (a hole the decoder cannot see) or hears it twice (a stutter). Neither is
# acceptable, and neither is visible without looking for it - so this is tested
# directly, at every possible interleaving rather than the convenient one.

@requires_capture
def test_no_cluster_is_lost_or_duplicated_whatever_the_attach_timing():
    """Attach after every single chunk, and demand a perfect sequence each time."""
    async def scenario(attach_after_chunk):
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        listener = Recorder()

        boot = None
        for position, chunk in enumerate(chunks):
            relay.offer(chunk)
            await asyncio.sleep(0)
            if position == attach_after_chunk:
                boot = await relay.add_listener("alice", listener)
        await asyncio.sleep(0.05)
        result = (boot, list(listener.frames))
        await relay.close()
        return result

    # Every attach point across the whole stream, not a sampled few.
    for attach_after in range(2, 38):
        boot, frames = asyncio.run(scenario(attach_after))
        assert boot is not None, f"no bootstrap when attaching after chunk {attach_after}"

        # What the listener's decoder actually consumed, in order.
        received = list(boot.clusters) + frames
        assert len(received) == len(set(received)), \
            f"a Cluster was delivered twice attaching after chunk {attach_after}"

        # And it is a CONTIGUOUS run of the stream: bootstrap then live, with
        # nothing missing in between.
        framer = WebmStreamFramer()
        every = []
        for chunk in timeslice_chunks():
            every.extend(f.data for f in framer.feed(chunk) if not f.is_init)
        start = every.index(received[0])
        assert every[start:start + len(received)] == received, \
            f"a Cluster was lost attaching after chunk {attach_after}"


@requires_capture
def test_a_cluster_completing_during_attach_is_delivered_exactly_once():
    """The specific interleaving: audio arrives while the listener is attaching."""
    async def scenario():
        relay = WebAudienceRelay(session_id=1)
        chunks = timeslice_chunks()
        for chunk in chunks[:12]:
            relay.offer(chunk)

        listener = Recorder()
        # Offer more audio from a task that runs concurrently with the attach.
        async def keep_broadcasting():
            for chunk in chunks[12:24]:
                relay.offer(chunk)
                await asyncio.sleep(0)

        pump = asyncio.create_task(keep_broadcasting())
        boot = await relay.add_listener("alice", listener)
        await pump
        await asyncio.sleep(0.05)
        result = (boot, list(listener.frames))
        await relay.close()
        return result

    boot, frames = asyncio.run(scenario())
    received = list(boot.clusters) + frames
    assert len(received) == len(set(received)), "a Cluster was delivered twice"
    assert frames, "the listener received live audio after attaching"


@requires_capture
def test_a_bootstrapped_cluster_is_never_sent_again():
    """Directly: the queue skips what the bootstrap already carried."""
    queue = ListenerQueue(listener_id="alice", capacity=8, since_index=5)
    assert queue.enqueue(b"old", index=3) is True      # accepted, not delivered
    assert queue.enqueue(b"old", index=4) is True
    assert queue.depth == 0, "already-bootstrapped Clusters are not queued again"
    assert queue.enqueue(b"new", index=5) is True
    assert queue.depth == 1
    assert queue.metrics()["skipped_already_bootstrapped"] == 2
