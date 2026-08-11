"""The Receiver pilot must keep its socket fresh while it waits.

Found during the Bluetooth amplifier live test. The Receiver was started for a
manual browser-driven run, connected successfully, and then vanished about 30
seconds later. Store UN showed OFFLINE in the UI before the operator could even
open the Broadcast Console.

The backend contract requires a heartbeat: ``backend/server.py`` waits
HEARTBEAT_INTERVAL_SECONDS for a message and, once the snapshot ages past
OFFLINE_AFTER_SECONDS, closes the socket with code 4408. The pilot's session
loop only ever reacted to inbound messages, so an idle Receiver never sent
anything and was closed by the server exactly on schedule.

The automated smoke never caught this because it starts a broadcast within a
second or two, and the resulting acknowledgements kept the snapshot fresh.

Nothing here opens a real device, plays a sound or touches a database.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tools.audio_receiver_pilot import (  # noqa: E402
    HEARTBEAT_SECONDS,
    AudioReceiverPilot,
)
from receiver_contract import (  # noqa: E402
    OFFLINE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    HeartbeatAcknowledgement,
    parse_receiver_ack,
)


class FakeConnection:
    """Records what the pilot sends. It can never reach a network."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, payload: str) -> None:
        if self.closed:
            raise ConnectionError("socket is closed")
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _pilot() -> AudioReceiverPilot:
    return AudioReceiverPilot(ws_url="ws://127.0.0.1:8000/api/ws/receiver")


# ---------------------------------------------------------------------------
# The interval itself must sit safely inside the server's window
# ---------------------------------------------------------------------------
def test_heartbeat_interval_leaves_room_for_a_missed_beat():
    """One dropped heartbeat must not be enough to be declared OFFLINE."""
    assert HEARTBEAT_SECONDS > 0
    assert HEARTBEAT_SECONDS * 2 < STALE_AFTER_SECONDS
    assert HEARTBEAT_SECONDS * 2 < OFFLINE_AFTER_SECONDS


# ---------------------------------------------------------------------------
# An idle Receiver must keep sending
# ---------------------------------------------------------------------------
def test_idle_pilot_sends_repeated_heartbeats():
    async def scenario():
        pilot, connection = _pilot(), FakeConnection()
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.08)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(connection.sent) >= 3, (
            "an idle Receiver sent nothing, so the backend would close it with 4408 "
            f"after {OFFLINE_AFTER_SECONDS} s"
        )

    asyncio.run(scenario())


def test_every_heartbeat_satisfies_the_receiver_contract():
    async def scenario():
        import json

        pilot, connection = _pilot(), FakeConnection()
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert connection.sent
        for raw in connection.sent:
            acknowledgement = parse_receiver_ack(json.loads(raw))
            assert isinstance(acknowledgement, HeartbeatAcknowledgement)

    asyncio.run(scenario())


def test_heartbeat_sequence_numbers_strictly_increase():
    async def scenario():
        import json

        pilot, connection = _pilot(), FakeConnection()
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.06)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        sequences = [json.loads(raw)["sequence"] for raw in connection.sent]
        assert sequences == sorted(set(sequences)), f"sequence numbers repeated: {sequences}"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# It must stop cleanly, and must never mask a real session failure
# ---------------------------------------------------------------------------
def test_heartbeat_stops_once_cancelled():
    async def scenario():
        pilot, connection = _pilot(), FakeConnection()
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.04)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        settled = len(connection.sent)
        await asyncio.sleep(0.04)
        assert len(connection.sent) == settled, "heartbeats continued after cancellation"

    asyncio.run(scenario())


def test_heartbeat_exits_quietly_when_the_socket_closes():
    async def scenario():
        """The session loop owns connection errors; the heartbeat must not raise a
        second, competing exception during shutdown."""
        pilot, connection = _pilot(), FakeConnection()
        connection.closed = True
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.05)
        assert task.done(), "the heartbeat loop should have returned, not hung"
        assert task.exception() is None, f"heartbeat raised {task.exception()!r}"

    asyncio.run(scenario())


def test_heartbeat_never_claims_a_playback_state():
    async def scenario():
        """A heartbeat proves liveness only. It must never look like readiness or
        playback evidence to the backend."""
        import json

        pilot, connection = _pilot(), FakeConnection()
        task = asyncio.create_task(pilot._heartbeat_loop(connection, interval=0.01))
        await asyncio.sleep(0.03)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for raw in connection.sent:
            payload = json.loads(raw)
            assert payload["type"] == "heartbeat"
            for forbidden in (
                "receiver_ready", "audio_receiving", "playback_confirmed",
                "speaker_verified", "session_id",
            ):
                assert forbidden not in payload, f"heartbeat leaked {forbidden}"
        assert pilot.report["ready"] is False
        assert pilot.report["audio_receiving"] is False
        assert pilot.report["playback_confirmed"] is False
        assert pilot.report["speaker_verified"] is False

    asyncio.run(scenario())
