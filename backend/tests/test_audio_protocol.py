"""Pure tests for the one-Store audio control protocol and bounded queues.

These tests use no SQLite database, no network socket, no Uvicorn and no
FFmpeg. They cover the message validation and the per-Store bounded queue that
protects the backend from an unbounded audio backlog.
"""

from __future__ import annotations

import asyncio

import pytest

from audio_protocol import (
    AUDIO_PROTOCOL_VERSION,
    DEFAULT_AUDIO_FORMAT,
    DEFAULT_MIME_TYPE,
    MAX_AUDIO_CHUNK_BYTES,
    SUPPORTED_MIME_TYPES,
    AudioFormat,
    AudioProtocolError,
    InvalidAudioChunkError,
    UnsupportedAudioFormatError,
    build_prepare_message,
    build_stop_message,
    parse_prepare_message,
    validate_audio_chunk,
)
from audio_streaming import (
    AudioFanout,
    StoreAudioQueue,
    StoreQueueClosedError,
)


# ---------------------------------------------------------------------------
# Audio format descriptor
# ---------------------------------------------------------------------------
def test_default_audio_format_matches_the_approved_milestone_values():
    assert DEFAULT_AUDIO_FORMAT.container == "webm"
    assert DEFAULT_AUDIO_FORMAT.codec == "opus"
    assert DEFAULT_AUDIO_FORMAT.channels == 1
    assert DEFAULT_AUDIO_FORMAT.target_bitrate == 32000
    assert DEFAULT_AUDIO_FORMAT.expected_chunk_ms == 250


def test_default_mime_type_is_a_supported_webm_opus_string():
    assert DEFAULT_MIME_TYPE == "audio/webm;codecs=opus"
    assert DEFAULT_MIME_TYPE in SUPPORTED_MIME_TYPES


def test_audio_format_rejects_a_non_webm_container():
    with pytest.raises(UnsupportedAudioFormatError):
        AudioFormat(container="mp4", codec="opus", channels=1,
                    target_bitrate=32000, expected_chunk_ms=250)


def test_audio_format_rejects_a_non_opus_codec():
    with pytest.raises(UnsupportedAudioFormatError):
        AudioFormat(container="webm", codec="aac", channels=1,
                    target_bitrate=32000, expected_chunk_ms=250)


def test_audio_format_rejects_multichannel_for_this_milestone():
    with pytest.raises(UnsupportedAudioFormatError):
        AudioFormat(container="webm", codec="opus", channels=2,
                    target_bitrate=32000, expected_chunk_ms=250)


# ---------------------------------------------------------------------------
# prepare / stop control messages
# ---------------------------------------------------------------------------
def test_prepare_message_carries_session_store_and_format():
    message = build_prepare_message(session_id=7, store_id=11)
    assert message["type"] == "prepare"
    assert message["protocol_version"] == AUDIO_PROTOCOL_VERSION
    assert message["broadcast_session_id"] == 7
    assert message["target_store_id"] == 11
    assert message["audio_format"]["codec"] == "opus"
    assert message["audio_format"]["container"] == "webm"
    assert message["audio_format"]["channels"] == 1
    assert message["audio_format"]["expected_chunk_ms"] == 250


def test_prepare_message_requires_a_positive_session_id():
    for bad in (0, -1, None, "7", True):
        with pytest.raises(AudioProtocolError):
            build_prepare_message(session_id=bad, store_id=11)


def test_prepare_message_requires_a_positive_store_id():
    for bad in (0, -1, None, "11", True):
        with pytest.raises(AudioProtocolError):
            build_prepare_message(session_id=7, store_id=bad)


def test_prepare_message_never_carries_a_credential():
    message = build_prepare_message(session_id=7, store_id=11)
    serialised = repr(message).lower()
    for marker in ("token", "authorization", "bearer", "password", "secret", "jwt"):
        assert marker not in serialised


def test_parse_prepare_round_trips_and_rejects_rubbish():
    message = build_prepare_message(session_id=7, store_id=11)
    parsed = parse_prepare_message(message)
    assert parsed.broadcast_session_id == 7
    assert parsed.target_store_id == 11
    assert parsed.audio_format == DEFAULT_AUDIO_FORMAT

    for bad in (None, [], "prepare", {}, {"type": "prepare"}):
        with pytest.raises(AudioProtocolError):
            parse_prepare_message(bad)


def test_parse_prepare_rejects_an_unknown_message_type():
    message = build_prepare_message(session_id=7, store_id=11)
    message["type"] = "definitely-not-prepare"
    with pytest.raises(AudioProtocolError):
        parse_prepare_message(message)


def test_parse_prepare_rejects_an_unsupported_codec():
    message = build_prepare_message(session_id=7, store_id=11)
    message["audio_format"]["codec"] = "aac"
    with pytest.raises(UnsupportedAudioFormatError):
        parse_prepare_message(message)


def test_stop_message_carries_session_and_reason():
    message = build_stop_message(session_id=7, reason="operator_stop")
    assert message["type"] == "stop"
    assert message["session_id"] == 7
    assert message["reason"] == "operator_stop"


# ---------------------------------------------------------------------------
# Audio chunk validation
# ---------------------------------------------------------------------------
def test_valid_chunk_is_accepted():
    assert validate_audio_chunk(b"\x1a\x45\xdf\xa3some-webm-bytes") is True


def test_empty_chunk_is_rejected():
    with pytest.raises(InvalidAudioChunkError):
        validate_audio_chunk(b"")


def test_non_binary_chunk_is_rejected():
    for bad in ("text", None, 123, ["bytes"]):
        with pytest.raises(InvalidAudioChunkError):
            validate_audio_chunk(bad)


def test_oversized_chunk_is_rejected():
    oversized = b"\x00" * (MAX_AUDIO_CHUNK_BYTES + 1)
    with pytest.raises(InvalidAudioChunkError):
        validate_audio_chunk(oversized)


def test_chunk_at_the_maximum_size_is_accepted():
    assert validate_audio_chunk(b"\x00" * MAX_AUDIO_CHUNK_BYTES) is True


# ---------------------------------------------------------------------------
# Bounded per-Store queue
# ---------------------------------------------------------------------------
def test_store_queue_is_bounded_and_never_exceeds_capacity():
    async def scenario():
        queue = StoreAudioQueue(store_id=11, capacity=4)
        for index in range(20):
            queue.enqueue(bytes([index]))
        assert queue.depth <= 4
        assert queue.capacity == 4
        assert queue.enqueued_count == 20
        assert queue.dropped_count == 16

    asyncio.run(scenario())


def test_store_queue_overflow_drops_oldest_and_records_metrics():
    async def scenario():
        queue = StoreAudioQueue(store_id=11, capacity=2)
        queue.enqueue(b"a")
        queue.enqueue(b"b")
        queue.enqueue(b"c")
        assert queue.depth == 2
        assert queue.dropped_count == 1
        # Oldest chunk is discarded so live audio stays current.
        assert await queue.get() == b"b"
        assert await queue.get() == b"c"

    asyncio.run(scenario())


def test_store_queue_clear_empties_and_reports_discarded():
    async def scenario():
        queue = StoreAudioQueue(store_id=11, capacity=8)
        for index in range(5):
            queue.enqueue(bytes([index]))
        discarded = queue.clear()
        assert discarded == 5
        assert queue.depth == 0

    asyncio.run(scenario())


def test_store_queue_rejects_use_after_close():
    async def scenario():
        queue = StoreAudioQueue(store_id=11, capacity=2)
        queue.close()
        with pytest.raises(StoreQueueClosedError):
            queue.enqueue(b"a")

    asyncio.run(scenario())


def test_store_queue_rejects_an_invalid_chunk():
    async def scenario():
        queue = StoreAudioQueue(store_id=11, capacity=2)
        with pytest.raises(InvalidAudioChunkError):
            queue.enqueue(b"")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Fanout: per-Store isolation, cleanup and metrics
# ---------------------------------------------------------------------------
def test_fanout_delivers_chunks_to_one_store():
    async def scenario():
        delivered: list[bytes] = []
        fanout = AudioFanout(capacity=8)
        await fanout.start_store(11, delivered.append)
        fanout.broadcast({11}, b"chunk-1")
        fanout.broadcast({11}, b"chunk-2")
        await fanout.drain(11, timeout=2)
        assert delivered == [b"chunk-1", b"chunk-2"]
        await fanout.stop_all()

    asyncio.run(scenario())


def test_fanout_does_not_let_one_slow_store_block_another():
    async def scenario():
        fast: list[bytes] = []
        release = asyncio.Event()

        async def slow_sender(chunk: bytes) -> None:
            await release.wait()

        fanout = AudioFanout(capacity=4)
        await fanout.start_store(11, slow_sender)
        await fanout.start_store(12, fast.append)

        # Yield between chunks so the sender tasks can run, which is what a
        # real 250 ms chunk cadence does. Without a yield a synchronous burst
        # would legitimately overflow every bounded queue, including the fast
        # one, which would not tell us anything about isolation.
        for index in range(10):
            fanout.broadcast({11, 12}, bytes([index]))
            await asyncio.sleep(0)

        # Store 12 keeps flowing even though Store 11 is stuck on its sender.
        await fanout.drain(12, timeout=2)
        assert len(fast) == 10
        assert fanout.metrics(12)["dropped"] == 0
        # Store 11's backlog is capped, not unbounded, and it dropped chunks.
        assert fanout.metrics(11)["depth"] <= 4
        assert fanout.metrics(11)["dropped"] > 0

        release.set()
        await fanout.stop_all()

    asyncio.run(scenario())


def test_fanout_stop_store_clears_the_queue_and_removes_it():
    async def scenario():
        fanout = AudioFanout(capacity=4)
        await fanout.start_store(11, lambda chunk: None)
        fanout.broadcast({11}, b"a")
        await fanout.stop_store(11)
        assert fanout.metrics(11) is None
        # Broadcasting to a removed Store is a no-op, not an error.
        fanout.broadcast({11}, b"b")
        await fanout.stop_all()

    asyncio.run(scenario())


def test_fanout_reports_dropped_chunks_per_store():
    async def scenario():
        release = asyncio.Event()

        async def blocked(chunk: bytes) -> None:
            await release.wait()

        fanout = AudioFanout(capacity=2)
        await fanout.start_store(11, blocked)
        for index in range(12):
            fanout.broadcast({11}, bytes([index]))
        metrics = fanout.metrics(11)
        assert metrics["capacity"] == 2
        assert metrics["depth"] <= 2
        assert metrics["dropped"] >= 1
        assert metrics["enqueued"] == 12
        release.set()
        await fanout.stop_all()

    asyncio.run(scenario())


def test_fanout_stop_all_leaves_no_running_task():
    async def scenario():
        fanout = AudioFanout(capacity=4)
        await fanout.start_store(11, lambda chunk: None)
        await fanout.start_store(12, lambda chunk: None)
        await fanout.stop_all()
        assert fanout.active_store_ids() == ()
        # No sender task should still be pending.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        assert pending == []

    asyncio.run(scenario())


def test_fanout_broadcast_ignores_an_invalid_chunk_without_crashing():
    async def scenario():
        fanout = AudioFanout(capacity=4)
        await fanout.start_store(11, lambda chunk: None)
        # An empty chunk must never reach a Store queue.
        rejected = fanout.broadcast({11}, b"")
        assert rejected == 0
        assert fanout.metrics(11)["enqueued"] == 0
        await fanout.stop_all()

    asyncio.run(scenario())
