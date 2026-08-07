"""Stores, web listeners and the recording, all on one Broadcast at once.

The three sinks share exactly one thing: the bytes. This drives all three from
one chunk stream with the REAL AudioFanout, the REAL WebAudienceRelay and a
bounded recording sink, and asserts the property the whole design exists for -
that none of them can delay the others.

The deliberately slow participants are the point. A healthy Store must not
notice a stalled browser, a healthy listener must not notice a stalled Store,
and the recording must not notice either.

No socket is opened and no file is written: every sink's seam is a callable.
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

from audio_streaming import AudioFanout  # noqa: E402
from web_audience import WebAudienceRelay  # noqa: E402

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CAPTURE = FIXTURE_DIR / "mediarecorder-live.webm"
CHUNK_INDEX = FIXTURE_DIR / "mediarecorder-live.chunks.json"

requires_capture = pytest.mark.skipif(
    not CAPTURE.exists() or not CHUNK_INDEX.exists(),
    reason="run: ECHOCAST_CAPTURE_WEBM=1 npx playwright test e2e/capture-fixture.spec.js",
)


def timeslice_chunks() -> list[bytes]:
    sizes = json.loads(CHUNK_INDEX.read_text())["chunkSizes"]
    data = CAPTURE.read_bytes()
    chunks, offset = [], 0
    for size in sizes:
        chunks.append(data[offset:offset + size])
        offset += size
    return chunks


class RecordingSink:
    """Stands in for the recording writer's bounded queue.

    The real writer also does no disk work on this path - offer() enqueues and a
    background task writes - so a counter is a faithful stand-in for what the
    audio path can observe.
    """

    def __init__(self) -> None:
        self.received = 0
        self.bytes_in = 0

    def offer(self, chunk: bytes) -> None:
        self.received += 1
        self.bytes_in += len(chunk)


@requires_capture
@pytest.mark.parametrize("stores,listeners", [(5, 10), (20, 50), (40, 100)])
def test_stores_web_listeners_and_recording_do_not_delay_each_other(stores, listeners):
    async def scenario():
        chunks = timeslice_chunks()
        dropped: list[tuple[str, str]] = []

        fanout = AudioFanout()
        relay = WebAudienceRelay(
            session_id=1,
            on_listener_dropped=lambda lid, reason: dropped.append((lid, reason)))
        recording = RecordingSink()

        store_counts = {store_id: 0 for store_id in range(1, stores + 1)}
        stalled = asyncio.Event()

        def store_sender(store_id):
            async def send(chunk: bytes) -> None:
                if store_id == 1:
                    await stalled.wait()      # one Store that never drains
                store_counts[store_id] += 1
            return send

        for store_id in store_counts:
            await fanout.start_store(store_id, store_sender(store_id))

        # Prime the relay so joining listeners have a bootstrap.
        for chunk in chunks[:4]:
            relay.offer(chunk)

        listener_counts = [0] * listeners

        def listener_sender(index):
            async def send(frame: bytes) -> None:
                if index == 0:
                    await stalled.wait()      # one browser that never reads
                listener_counts[index] += 1
            return send

        for index in range(listeners):
            await relay.add_listener(f"listener-{index}", listener_sender(index))

        target_ids = list(store_counts)
        started = time.perf_counter()
        for chunk in chunks[4:]:
            # Exactly the broadcaster loop's order and its non-awaiting shape.
            fanout.broadcast(target_ids, chunk)
            recording.offer(chunk)
            relay.offer(chunk)
            await asyncio.sleep(0)
        audio_path_seconds = time.perf_counter() - started

        await asyncio.sleep(0.3)
        # Snapshot everything BEFORE releasing the stalled sinks. Reading the
        # counters afterwards would measure a Store that had already drained and
        # would quietly turn this into a test of nothing.
        store_metrics = fanout.all_metrics()
        web_metrics = relay.metrics()
        result = (audio_path_seconds, dict(store_counts), list(listener_counts),
                  recording.received, store_metrics, web_metrics, list(dropped),
                  len(chunks) - 4)
        stalled.set()
        await asyncio.sleep(0.05)
        await fanout.stop_all()
        await relay.close()
        return result

    (seconds, store_counts, listener_counts, recorded, store_metrics,
     web_metrics, dropped, offered) = asyncio.run(scenario())

    # ---- the recording saw every chunk, whatever anyone else did ------------
    assert recorded == offered, "the recording is not affected by any sink"

    # ---- healthy Stores are unaffected by the stalled Store and the browsers -
    healthy_stores = {sid: count for sid, count in store_counts.items() if sid != 1}
    assert all(count > 0 for count in healthy_stores.values())
    assert store_counts[1] == 0, "the stalled Store received nothing, as expected"

    # ---- healthy listeners are unaffected by the stalled listener -----------
    healthy_listeners = listener_counts[1:]
    assert all(count > 0 for count in healthy_listeners), "healthy listeners kept up"
    assert len(set(healthy_listeners)) == 1, "and all received identical frames"

    # ---- the stalled browser was disconnected, not silently gapped ---------
    assert ("listener-0", "slow_listener") in dropped
    assert web_metrics["slow_disconnects"] >= 1

    # ---- nothing grew without bound ----------------------------------------
    for metrics in store_metrics.values():
        assert metrics["max_depth"] <= metrics["capacity"]
    for queue in web_metrics["queues"].values():
        assert queue["max_depth"] <= queue["capacity"]

    # ---- and the audio path stayed fast ------------------------------------
    assert seconds < 2.0, f"the audio path took {seconds:.2f}s"
    print(f"\n{stores:>3} Stores + {listeners:>3} listeners + recording: "
          f"audio path {seconds * 1000:.0f} ms for {offered} chunks, "
          f"store drops {sum(m['dropped'] for m in store_metrics.values())}, "
          f"web slow disconnects {web_metrics['slow_disconnects']}, "
          f"peak store depth {max(m['max_depth'] for m in store_metrics.values())}, "
          f"peak web depth {max(q['max_depth'] for q in web_metrics['queues'].values())}")
