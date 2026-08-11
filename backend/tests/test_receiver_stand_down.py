"""The Receiver's own behaviour when it is told to stand down and resume.

The contract tests next door prove what the STATE does. This file drives the
real Agent object and proves what the SHOP experiences, because the three
things that matter here are all side effects rather than status:

  * the Windows endpoint is handed back, so a paused shop plays its own music
    at its own volume rather than sitting at announcement level;
  * the output device is released, so a paused Store is not squatting on a
    device something else may need;
  * the SESSION is kept, so resume is a resumption and not a new arrival.

No real audio device is opened: the Agent runs in null-sink mode, which is the
same mode the packaged Receiver uses when no Windows device is selected.
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
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend",
                  REPOSITORY_ROOT / "tools"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import audio_receiver_pilot as agent_module  # noqa: E402
from audio_protocol import (  # noqa: E402
    build_resume_message, build_stand_down_message,
)

SESSION_ID = 42
STORE_ID = 7


class FakeConnection:
    """Records what the Agent sent, and never touches a socket."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message) if isinstance(message, str) else message)

    def types(self):
        return [m.get("type") for m in self.sent]

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


class FakeDecoder:
    """Stands in for FFmpeg. Records that it was closed exactly once."""

    def __init__(self, *args, **kwargs):
        self.closed = 0
        self.running = True
        self.decoded_microseconds = 1000

    def start(self):
        self.running = True

    def close(self):
        self.closed += 1
        self.running = False
        return 0


class FakeSink:
    def __init__(self, *args, **kwargs):
        self.closed = 0
        self.opened = 0
        self.frames_written = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1


@pytest.fixture()
def agent(monkeypatch):
    """A Receiver mid-broadcast, with FFmpeg and the audio device faked out."""
    made = agent_module.AudioReceiverPilot(ws_url="ws://test.invalid/api/receiver")
    monkeypatch.setattr(agent_module, "FfmpegDecoder", FakeDecoder)

    made.session_id = SESSION_ID
    made.store_id = STORE_ID
    made.decoder = FakeDecoder()
    made.pcm_sink = FakeSink()
    made.queue = agent_module.StoreAudioQueue(store_id=STORE_ID, capacity=4)

    # The Windows endpoint is the thing a paused shop notices most, so its
    # restore is recorded rather than really performed.
    made.restored = 0

    def restore():
        made.restored += 1
    monkeypatch.setattr(made, "restore_windows_endpoint", restore)
    return made


def run(coroutine):
    return asyncio.run(coroutine)


# ===========================================================================
# Standing down
# ===========================================================================

def test_standing_down_gives_the_shop_its_volume_back(agent):
    connection = FakeConnection()
    run(agent._on_stand_down(connection, build_stand_down_message(session_id=SESSION_ID)))

    # The one thing a customer in the shop would notice.
    assert agent.restored == 1
    assert "stood_down" in connection.types()
    assert connection.of_type("stood_down")[0]["session_id"] == SESSION_ID


def test_standing_down_releases_the_decoder_and_the_device(agent):
    decoder, sink = agent.decoder, agent.pcm_sink
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))

    assert decoder.closed == 1, "FFmpeg was left running on a paused Store"
    assert sink.closed == 1, "the output device was left open on a paused Store"
    assert agent.decoder is None and agent.pcm_sink is None


def test_standing_down_keeps_the_session(agent):
    """The whole difference from stop. Forgetting the session here would turn
    every Pause into a dropout and a rejoin."""
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))
    assert agent.session_id == SESSION_ID
    assert agent.store_id == STORE_ID
    assert agent.stood_down is True


def test_a_stood_down_receiver_ignores_audio(agent):
    """Chunks in flight when the pause began must not be decoded afterwards."""
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))
    connection = FakeConnection()
    run(agent._on_audio(connection, b"\x1a\x45\xdf\xa3" + b"0" * 64))
    assert connection.sent == []
    assert agent.report["total_chunks"] == 0


def test_the_reason_is_carried_and_bounded(agent):
    connection = FakeConnection()
    run(agent._on_stand_down(connection, {"type": "stand_down",
                                          "session_id": SESSION_ID,
                                          "reason": "x" * 400}))
    assert len(connection.of_type("stood_down")[0]["reason"]) <= 128


# ===========================================================================
# Resuming
# ===========================================================================

def test_resuming_rebuilds_the_pipeline_for_the_same_session(agent, monkeypatch):
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))

    prepared = []
    async def fake_prepare(connection):
        prepared.append(True)
        return True
    monkeypatch.setattr(agent, "_prepare_windows_endpoint", fake_prepare)

    connection = FakeConnection()
    run(agent._on_resume(connection, build_resume_message(
        session_id=SESSION_ID, store_id=STORE_ID, generation=2)))

    assert agent.session_id == SESSION_ID
    assert agent.decoder is not None and agent.queue is not None
    assert agent.stood_down is False
    # The endpoint is taken over again, from the shop's own level - a resumed
    # Store must not inherit the previous participation's volume.
    assert prepared == [True]

    resumed = connection.of_type("resumed")
    assert resumed and resumed[0]["generation"] == 2


def test_audio_flows_again_after_a_resume(agent, monkeypatch):
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))
    async def fake_prepare(connection):
        return True
    monkeypatch.setattr(agent, "_prepare_windows_endpoint", fake_prepare)
    run(agent._on_resume(FakeConnection(), build_resume_message(
        session_id=SESSION_ID, store_id=STORE_ID)))

    assert agent.queue is not None and not agent.queue.closed


def test_a_resume_that_cannot_open_the_device_says_so_and_stays_down(agent, monkeypatch):
    """A shop that cannot open its output has not resumed.

    Reporting device_error now is better than reporting success and being
    silent - which is the failure this whole readiness protocol exists to stop.
    """
    run(agent._on_stand_down(FakeConnection(), build_stand_down_message(session_id=SESSION_ID)))

    # Hardware mode, with an output device that refuses to open.
    agent.sink = agent_module.SinkConfiguration(
        sink_mode=agent_module.SINK_MODE_WINDOWS, device="index:1@Speakers")

    class RefusingSink:
        def __init__(self, *args, **kwargs):
            pass

        def open(self):
            raise agent_module.SinkConfigurationError("device busy")
    monkeypatch.setattr(agent_module, "WindowsPcmSink", RefusingSink)

    connection = FakeConnection()
    run(agent._on_resume(connection, build_resume_message(
        session_id=SESSION_ID, store_id=STORE_ID)))

    errors = connection.of_type("device_error")
    assert errors and errors[0]["error_code"] == "OUTPUT_DEVICE_UNAVAILABLE"
    assert "resumed" not in connection.types()
    assert agent.stood_down is True, "a failed resume claimed the Store was back"


def test_a_resume_without_a_session_does_nothing(agent):
    agent.session_id = None
    connection = FakeConnection()
    run(agent._on_resume(connection, {"type": "resume", "session_id": 0,
                                      "store_id": STORE_ID}))
    assert connection.sent == []
