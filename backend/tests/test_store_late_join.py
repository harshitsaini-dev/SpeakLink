"""A Store joining mid-Broadcast must be handed something decodable.

Stores present when a Broadcast starts get the broadcaster's chunks untouched,
from byte zero, and that path is physically accepted - so it is deliberately
left exactly as it was. These tests hold it to that, and prove the new path
only for Stores that join late.

The failure this is really guarding against is subtle: an implementation that
sends a correct initialization segment and then goes back to arbitrary raw
timeslice chunks. That looks right in a log and hands the decoder the middle of
a Cluster.
"""

from __future__ import annotations

import asyncio
import json
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from broadcast_runtime import BroadcastRuntime  # noqa: E402
from store_late_join import (  # noqa: E402
    DELIVERY_INITIAL_RAW,
    DELIVERY_LATE_JOIN_FRAMED,
    StoreLateJoinSource,
)
from webm_stream import WebmStreamFramer  # noqa: E402

CAPTURE = REPOSITORY_ROOT / "backend" / "tests" / "fixtures" / "mediarecorder-live.webm"
CAPTURE_INDEX = (REPOSITORY_ROOT / "backend" / "tests" / "fixtures"
                 / "mediarecorder-live.chunks.json")

pytestmark = pytest.mark.skipif(
    not (CAPTURE.exists() and CAPTURE_INDEX.exists()),
    reason="needs the real MediaRecorder capture fixture")


def real_chunks():
    """The real browser timeslices, exactly as a broadcaster sends them."""
    sizes = json.loads(CAPTURE_INDEX.read_text())["chunkSizes"]
    data = CAPTURE.read_bytes()
    out, offset = [], 0
    for size in sizes:
        out.append(data[offset:offset + size])
        offset += size
    return out


class Store:
    def __init__(self):
        self.received: list[bytes] = []

    def sender(self, store_id):
        async def send(chunk: bytes) -> None:
            self.received.append(chunk)
        return send


async def settle(times: int = 8):
    for _ in range(times):
        await asyncio.sleep(0)


# ===========================================================================
# The framing source
# ===========================================================================

def test_the_source_is_not_ready_until_it_has_a_header():
    source = StoreLateJoinSource(session_id=1)
    assert source.ready is False
    assert source.bootstrap() is None, (
        "a Store must never be started before there is an init segment")

    for chunk in real_chunks():
        source.offer(chunk)
        if source.ready:
            break
    assert source.ready is True
    assert source.bootstrap().init_segment.startswith(b"\x1a\x45\xdf\xa3")


def test_the_bootstrap_replays_no_history_by_default():
    """The live edge means the live edge."""
    source = StoreLateJoinSource(session_id=1)
    for chunk in real_chunks():
        source.offer(chunk)

    bootstrap = source.bootstrap()
    assert bootstrap.clusters == (), (
        f"{len(bootstrap.clusters)} historical Clusters would be replayed - a "
        "Store must not play an announcement that has already been made")
    assert bootstrap.total_bytes == len(bootstrap.init_segment)


def test_the_source_holds_one_header_however_long_the_broadcast_runs():
    source = StoreLateJoinSource(session_id=1)
    chunks = real_chunks()
    # One continuous stream, as a real MediaRecorder produces: the header
    # arrives once and never again. Re-feeding the whole capture would inject a
    # second EBML header mid-stream, which is not a long Broadcast - it is a
    # malformed one, and the framer is right to refuse it.
    for chunk in chunks:
        source.offer(chunk)
    header_bytes = len(source.bootstrap().init_segment)
    for _ in range(40):                       # a long announcement
        for chunk in chunks[4:]:
            source.offer(chunk)

    metrics = source.metrics()
    assert metrics["bootstrap_clusters"] == 0
    assert metrics["init_bytes"] == header_bytes, (
        "the cached header changed during a long Broadcast")
    assert metrics["clusters_seen"] > 0


def test_an_unframeable_stream_refuses_rather_than_improvising():
    source = StoreLateJoinSource(session_id=1)
    source.offer(b"this is not a WebM stream at all" * 40)
    assert source.ready is False
    assert source.bootstrap() is None


# ===========================================================================
# The existing raw path must not change
# ===========================================================================

def test_a_store_present_at_the_start_receives_the_identical_bytes():
    """Byte-for-byte, chunk-for-chunk, in the same order as before."""
    store = Store()
    chunks = real_chunks()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=store.sender)
        await runtime.start(session_id=1, owner_user_id=1, target_store_ids=[10])
        for chunk in chunks:
            await runtime.fanout(1, chunk, connected_store_ids=[10])
            await settle(4)
        await settle(30)
        await runtime.end(1)

    asyncio.run(scenario())

    assert store.received == chunks, (
        "the initial Store's byte path changed - it must stay exactly as it "
        "was physically accepted")


def test_an_initial_store_stays_on_the_raw_delivery_mode():
    store = Store()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=store.sender)
        await runtime.start(session_id=2, owner_user_id=1, target_store_ids=[10])
        for chunk in real_chunks()[:10]:
            await runtime.fanout(2, chunk, connected_store_ids=[10])
        await settle()
        mode = runtime.delivery_mode(2, 10)
        await runtime.end(2)
        return mode

    assert asyncio.run(scenario()) == DELIVERY_INITIAL_RAW


# ===========================================================================
# The late joiner
# ===========================================================================

def test_a_late_joiner_starts_with_a_header_then_whole_clusters():
    early, late = Store(), Store()
    chunks = real_chunks()

    def factory(store_id):
        return early.sender(store_id) if store_id == 10 else late.sender(store_id)

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=factory)
        await runtime.start(session_id=3, owner_user_id=1, target_store_ids=[10])

        # The Broadcast is well underway before anybody thinks of Store 11.
        for chunk in chunks[:20]:
            await runtime.fanout(3, chunk, connected_store_ids=[10])
            await settle(4)

        result = await runtime.join_store_at_live_edge(3, 11)
        assert result["joined"] is True, result
        await settle(10)

        for chunk in chunks[20:]:
            await runtime.fanout(3, chunk, connected_store_ids=[10, 11])
            await settle(4)
        await settle(30)

        mode = runtime.delivery_mode(3, 11)
        await runtime.end(3)
        return result, mode

    result, mode = asyncio.run(scenario())

    assert mode == DELIVERY_LATE_JOIN_FRAMED
    assert result["bootstrap_clusters"] == 0, "no historical audio was replayed"
    assert late.received, "the late joiner received nothing at all"

    # First: a real initialization segment.
    assert late.received[0].startswith(b"\x1a\x45\xdf\xa3"), (
        "the first thing a late joiner received was not an EBML header")

    # Then: nothing but whole Clusters. This is the assertion that catches
    # "correct init, then arbitrary raw timeslices" - the plausible wrong
    # implementation.
    #
    # A Cluster cannot be re-parsed on its own, because a framer only learns
    # that one ended when the next element starts. So the whole delivered
    # stream is re-framed and required to come back as exactly the payloads
    # that were sent: same count, same boundaries, same bytes. Raw timeslices
    # would not survive that, because their boundaries fall inside Clusters.
    framer = WebmStreamFramer()
    emitted = framer.feed(b"".join(late.received))
    init = [f for f in emitted if f.is_init]
    clusters = [f for f in emitted if not f.is_init]
    assert init and init[0].data == late.received[0], (
        "the delivered stream does not re-frame to the header that was sent")
    delivered_clusters = late.received[1:]
    assert [c.data for c in clusters] == delivered_clusters[:len(clusters)], (
        "a late joiner was sent something that is not exactly one whole "
        "Cluster per message - its decoder would meet a partial element")
    assert len(clusters) >= len(delivered_clusters) - 1, (
        f"{len(delivered_clusters)} payloads re-framed to only "
        f"{len(clusters)} Clusters")

    # And the early Store was not disturbed by any of it.
    assert early.received == chunks[:20] + chunks[20:]


def test_joining_is_refused_before_a_header_exists():
    store = Store()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=store.sender)
        await runtime.start(session_id=4, owner_user_id=1, target_store_ids=[10])
        # Not one byte has been broadcast yet.
        result = await runtime.join_store_at_live_edge(4, 11)
        await runtime.end(4)
        return result

    result = asyncio.run(scenario())
    assert result["joined"] is False
    assert "initialization segment" in result["reason"]


def test_a_late_joiner_never_receives_the_backlog():
    """It joins the live edge, not the beginning."""
    early, late = Store(), Store()
    chunks = real_chunks()

    def factory(store_id):
        return early.sender(store_id) if store_id == 10 else late.sender(store_id)

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=factory)
        await runtime.start(session_id=5, owner_user_id=1, target_store_ids=[10])
        for chunk in chunks:
            await runtime.fanout(5, chunk, connected_store_ids=[10])
            await settle(4)

        await runtime.join_store_at_live_edge(5, 11)
        await settle(20)
        joined_with = len(late.received)
        await runtime.end(5)
        return joined_with

    joined_with = asyncio.run(scenario())
    assert joined_with == 1, (
        f"the joining Store was handed {joined_with} payloads before any live "
        "audio - it should have received the header and nothing else")


def test_one_late_joiner_does_not_disturb_another_store():
    early, late = Store(), Store()
    chunks = real_chunks()

    def factory(store_id):
        return early.sender(store_id) if store_id == 10 else late.sender(store_id)

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=factory)
        await runtime.start(session_id=6, owner_user_id=1, target_store_ids=[10])
        for chunk in chunks[:15]:
            await runtime.fanout(6, chunk, connected_store_ids=[10])
            await settle(4)
        await runtime.join_store_at_live_edge(6, 11)
        for chunk in chunks[15:]:
            await runtime.fanout(6, chunk, connected_store_ids=[10, 11])
            await settle(4)
        await settle(30)
        await runtime.end(6)

    asyncio.run(scenario())
    assert early.received == chunks, (
        "adding a Store changed what an existing Store received")
