"""The Cluster framer, tested against bytes a real Chromium actually produced.

A framer tested only against bytes the test itself constructed proves that the
framer agrees with the test's idea of WebM. These tests replay a capture taken
from a real MediaRecorder at the same 250 ms timeslice the HQ broadcaster uses
(see ``frontend/e2e/support/capture-webm-stream.js``), and skip honestly when
that capture has not been generated rather than quietly proving less.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from webm_stream import (
    ClusterTooLargeError,
    InitSegmentTooLargeError,
    MalformedWebmStreamError,
    WebmStreamFramer,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CAPTURE = FIXTURE_DIR / "mediarecorder-live.webm"
CHUNK_INDEX = FIXTURE_DIR / "mediarecorder-live.chunks.json"

requires_capture = pytest.mark.skipif(
    not CAPTURE.exists() or not CHUNK_INDEX.exists(),
    reason="run: SPEAKLINK_CAPTURE_WEBM=1 npx playwright test e2e/capture-fixture.spec.js",
)


def _stream() -> bytes:
    return CAPTURE.read_bytes()


def _timeslice_chunks() -> list[bytes]:
    """The stream split exactly as MediaRecorder delivered it."""
    sizes = json.loads(CHUNK_INDEX.read_text())["chunkSizes"]
    data = _stream()
    chunks = []
    offset = 0
    for size in sizes:
        chunks.append(data[offset:offset + size])
        offset += size
    return chunks


def _frames(feeds):
    framer = WebmStreamFramer()
    produced = []
    for feed in feeds:
        produced.extend(framer.feed(feed))
    return framer, produced


# ---------------------------------------------------------------------------
# Real captured audio
# ---------------------------------------------------------------------------

@requires_capture
def test_the_init_segment_is_everything_before_the_first_cluster():
    framer, frames = _frames(_timeslice_chunks())
    init = [frame for frame in frames if frame.is_init]

    assert len(init) == 1, "the initialization segment is emitted exactly once"
    assert init[0].index == 0
    # EBML header, Segment header, Info and Tracks - and no Cluster.
    assert init[0].data.startswith(bytes([0x1A, 0x45, 0xDF, 0xA3]))
    assert bytes([0x1F, 0x43, 0xB6, 0x75]) not in init[0].data
    assert framer.init_emitted


@requires_capture
def test_every_emitted_cluster_starts_with_a_cluster_identifier():
    _, frames = _frames(_timeslice_chunks())
    clusters = [frame for frame in frames if not frame.is_init]

    assert clusters, "the capture contains complete Clusters"
    for cluster in clusters:
        # The property the whole late-join architecture rests on.
        assert cluster.data.startswith(bytes([0x1F, 0x43, 0xB6, 0x75]))


@requires_capture
def test_framing_loses_no_bytes():
    _, frames = _frames(_timeslice_chunks())
    rebuilt = b"".join(frame.data for frame in frames)
    # Everything emitted is an exact prefix of the original stream: the framer
    # reorders nothing, rewrites nothing and invents nothing.
    assert _stream().startswith(rebuilt)


@requires_capture
@pytest.mark.parametrize("describe_feed", ["one byte at a time", "whole stream", "random splits"])
def test_output_does_not_depend_on_how_the_bytes_arrive(describe_feed):
    """Socket reads do not align to anything, so framing must not depend on them."""
    data = _stream()
    if describe_feed == "one byte at a time":
        feeds = [data[i:i + 1] for i in range(len(data))]
    elif describe_feed == "whole stream":
        feeds = [data]
    else:
        random.seed(11)
        feeds = []
        offset = 0
        while offset < len(data):
            take = random.randint(1, 3000)
            feeds.append(data[offset:offset + take])
            offset += take

    _, reference = _frames(_timeslice_chunks())
    _, actual = _frames(feeds)
    assert [frame.data for frame in actual] == [frame.data for frame in reference]


@requires_capture
def test_a_cluster_identifier_split_across_two_feeds_is_not_lost():
    """The identifier that ends a Cluster can straddle a socket read."""
    data = _stream()
    marker = bytes([0x1F, 0x43, 0xB6, 0x75])
    second = data.find(marker, data.find(marker) + 4)
    assert second > 0, "the capture has more than one Cluster"

    # Split squarely inside the identifier that terminates the first Cluster.
    _, frames = _frames([data[:second + 2], data[second + 2:]])
    clusters = [frame for frame in frames if not frame.is_init]
    assert clusters
    for cluster in clusters:
        assert cluster.data.startswith(marker)


@requires_capture
def test_the_buffer_never_holds_more_than_one_cluster():
    """Bounded memory is the point: a listener count cannot change this."""
    framer = WebmStreamFramer()
    largest = 0
    for chunk in _timeslice_chunks():
        framer.feed(chunk)
        largest = max(largest, framer.buffered_bytes)
    # Only the Cluster currently being assembled is ever held.
    assert largest < 64 * 1024


@requires_capture
def test_a_broadcast_ending_mid_cluster_emits_nothing_partial():
    """A Broadcast can stop between Cluster boundaries. Nothing partial escapes."""
    data = _stream()
    framer = WebmStreamFramer()
    frames = framer.feed(data[:-200])
    clusters = [frame for frame in frames if not frame.is_init]
    for cluster in clusters:
        assert cluster.data.startswith(bytes([0x1F, 0x43, 0xB6, 0x75]))
    # The trailing partial Cluster is held, never emitted.
    assert framer.buffered_bytes > 0


# ---------------------------------------------------------------------------
# Bounds and malformed input
# ---------------------------------------------------------------------------

def test_an_empty_chunk_is_ignored_rather_than_failing():
    framer = WebmStreamFramer()
    assert framer.feed(b"") == []


def test_non_binary_input_is_refused():
    framer = WebmStreamFramer()
    with pytest.raises(MalformedWebmStreamError):
        framer.feed("not bytes")


def test_a_stream_that_never_reaches_a_cluster_is_bounded():
    """Otherwise a hostile or broken stream buys unlimited memory."""
    framer = WebmStreamFramer(max_init_bytes=4096)
    header = bytes([0x1A, 0x45, 0xDF, 0xA3, 0x84, 0, 0, 0, 0])
    segment = bytes([0x18, 0x53, 0x80, 0x67, 0x01]) + b"\xff" * 7
    # Perfectly well-formed padding that simply never becomes a Cluster. The
    # bound has to hold for a valid stream, not only for a corrupt one.
    void = bytes([0xEC, 0x81, 0x00])
    with pytest.raises(InitSegmentTooLargeError):
        framer.feed(header + segment + void * 4000)


def test_a_cluster_that_never_ends_is_bounded():
    framer = WebmStreamFramer(max_cluster_bytes=2048)
    stream = bytearray()
    stream += bytes([0x1A, 0x45, 0xDF, 0xA3, 0x84, 0, 0, 0, 0])       # EBML
    stream += bytes([0x18, 0x53, 0x80, 0x67, 0x01]) + b"\xff" * 7      # Segment, unknown
    stream += bytes([0x1F, 0x43, 0xB6, 0x75, 0x01]) + b"\xff" * 7      # Cluster, unknown
    # SimpleBlocks for ever, and no following top-level element.
    block = bytes([0xA3, 0xA0]) + b"\x00" * 0x20
    with pytest.raises(ClusterTooLargeError):
        framer.feed(bytes(stream) + block * 400)


def test_an_identifier_that_is_not_legal_inside_a_cluster_fails_closed():
    """Guessing at an unrecognised identifier is how a framer emits corruption."""
    framer = WebmStreamFramer()
    stream = bytearray()
    stream += bytes([0x1A, 0x45, 0xDF, 0xA3, 0x84, 0, 0, 0, 0])
    stream += bytes([0x18, 0x53, 0x80, 0x67, 0x01]) + b"\xff" * 7
    stream += bytes([0x1F, 0x43, 0xB6, 0x75, 0x01]) + b"\xff" * 7
    stream += bytes([0x88, 0x81, 0x00])           # not a Cluster child
    with pytest.raises(MalformedWebmStreamError):
        framer.feed(bytes(stream))


def test_a_stream_starting_with_a_cluster_and_no_header_is_refused():
    """There would be no initialization segment to bootstrap anyone with."""
    framer = WebmStreamFramer()
    with pytest.raises(MalformedWebmStreamError):
        framer.feed(bytes([0x1F, 0x43, 0xB6, 0x75, 0x01]) + b"\xff" * 7)


def test_a_failed_framer_refuses_to_continue():
    """Resuming after a framing failure would emit clusters from a lost position."""
    framer = WebmStreamFramer()
    with pytest.raises(MalformedWebmStreamError):
        framer.feed(bytes([0x1F, 0x43, 0xB6, 0x75, 0x01]) + b"\xff" * 7)
    with pytest.raises(MalformedWebmStreamError):
        framer.feed(b"\x00")
