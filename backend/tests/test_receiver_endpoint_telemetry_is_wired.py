"""The Receiver must actually SEND what it observes.

THE GAP THIS EXISTS TO CLOSE

Every part of Store-to-HQ volume telemetry was already written and already
tested: the Core Audio observer, the coalescing, the sequence counter, the
endpoint_state message, the backend handler that updates only the ACTUAL
fields, and the Console slider that follows them. It still did not work,
because nothing ever started the loop that sends the message. The reading was
observed, coalesced into the observer's single slot, and overwritten by the
next one.

Every existing test passed throughout. They tested the parts and not the
wiring, so this file tests the wiring: that connecting schedules the reporter,
and that a change at the till really leaves the Receiver as a message.

Coroutines are driven with ``asyncio.run`` rather than pytest-asyncio, which
this repository does not depend on.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from tools.audio_receiver_pilot import AudioReceiverPilot  # noqa: E402
from tools.windows_endpoint_observer import EndpointObserver  # noqa: E402


class FakeObserverBackend:
    """A test standing in for Core Audio raising a notification."""

    def __init__(self):
        self.registered = {}

    def register(self, endpoint_id, on_change):
        self.registered[endpoint_id] = on_change

    def unregister(self, endpoint_id):
        self.registered.pop(endpoint_id, None)

    def change(self, endpoint_id, volume_percent, muted=False):
        self.registered[endpoint_id](volume_percent, muted)


class RecordingConnection:
    """Collects what the Receiver puts on the wire."""

    def __init__(self):
        self.sent = []

    async def send(self, payload):
        import json
        self.sent.append(
            json.loads(payload) if isinstance(payload, str) else payload)

    def endpoint_states(self):
        return [m for m in self.sent if m.get("type") == "endpoint_state"]


class DeadConnection:
    async def send(self, payload):
        raise ConnectionError("socket closed")


ENDPOINT = "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}"


def make_pilot(session_id=None, backend=None, observing=True):
    """A pilot with no sockets, no audio and no Windows, for the loop alone."""
    pilot = AudioReceiverPilot.__new__(AudioReceiverPilot)
    pilot.session_id = session_id
    pilot._endpoint_observer = None
    pilot._endpoint_state_sequence = 0
    pilot._sequence = 0
    pilot.report = {}
    if observing and backend is not None:
        pilot._endpoint_observer = EndpointObserver(ENDPOINT, backend=backend)
        pilot._endpoint_observer.start()
    return pilot


async def drive(pilot, connection, before=None, after=None, seconds=0.7):
    """Run the reporter the way a session does, then stop it the same way."""
    task = asyncio.create_task(pilot._endpoint_state_loop(connection))
    if before is not None:
        before()
    await asyncio.sleep(seconds)
    if after is not None:
        after()
        await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ===========================================================================
# The wiring itself
# ===========================================================================

def test_connecting_schedules_the_reporter():
    """The defect in one assertion: something has to start the loop.

    Read from the source rather than by opening a socket, because the bug was
    an absent line and that is exactly what a source check can see.
    """
    source = (REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py").read_text(
        encoding="utf-8")
    assert "create_task(self._endpoint_state_loop(" in source, (
        "nothing starts the endpoint reporter, so no change at the till can "
        "ever reach HQ - which is the whole defect")


def test_a_change_at_the_till_leaves_the_receiver_as_a_message():
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=42, backend=backend)
    connection = RecordingConnection()

    asyncio.run(drive(pilot, connection,
                      before=lambda: backend.change(ENDPOINT, 25, muted=False)))

    reports = connection.endpoint_states()
    assert len(reports) == 1, f"expected exactly one report, got {reports}"
    assert reports[0]["volume_percent"] == 25
    assert reports[0]["muted"] is False
    assert reports[0]["session_id"] == 42
    assert reports[0]["state_sequence"] == 1


def test_the_reporter_waits_for_the_observer_instead_of_giving_up():
    """Connecting happens before PREPARE, so the observer is not running yet.

    Returning at that point would end the only task that can ever report a
    change, and nothing would start a second one - the loop would be scheduled
    and still useless.
    """
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=7, backend=backend, observing=False)
    connection = RecordingConnection()

    async def scenario():
        task = asyncio.create_task(pilot._endpoint_state_loop(connection))
        await asyncio.sleep(0.3)
        assert not task.done(), "the reporter gave up before the broadcast began"

        # PREPARE arrives and observation starts, as the real agent does it.
        pilot._endpoint_observer = EndpointObserver(ENDPOINT, backend=backend)
        pilot._endpoint_observer.start()
        backend.change(ENDPOINT, 55, muted=False)

        await asyncio.sleep(0.7)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert [r["volume_percent"] for r in connection.endpoint_states()] == [55]


def test_a_reading_outside_a_broadcast_is_not_reported():
    """Store output control exists only inside a broadcast, so it says nothing."""
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=None, backend=backend)
    connection = RecordingConnection()

    asyncio.run(drive(pilot, connection,
                      before=lambda: backend.change(ENDPOINT, 30, muted=False)))

    assert connection.endpoint_states() == []


# ===========================================================================
# Bounds - one noisy Store must not fill a socket that also carries audio
# ===========================================================================

def test_a_long_drag_becomes_a_handful_of_messages_ending_where_it_stopped():
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=9, backend=backend)
    connection = RecordingConnection()

    def one_long_drag():
        for step in range(100, 20, -1):        # 80 notifications, one gesture
            backend.change(ENDPOINT, step, muted=False)

    asyncio.run(drive(pilot, connection, before=one_long_drag, seconds=1.0))

    reports = connection.endpoint_states()
    assert reports, "the drag produced nothing at all"
    assert len(reports) < 80, (
        f"{len(reports)} messages for one drag - the coalescing is not working")
    assert reports[-1]["volume_percent"] == 21, (
        "the final resting place must never be the reading that gets dropped")
    sequences = [r["state_sequence"] for r in reports]
    assert sequences == sorted(set(sequences)), "sequences must strictly increase"


def test_a_dead_socket_ends_the_reporter_rather_than_spinning():
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=3, backend=backend)

    async def scenario():
        task = asyncio.create_task(pilot._endpoint_state_loop(DeadConnection()))
        backend.change(ENDPOINT, 40, muted=False)
        await asyncio.sleep(0.6)
        assert task.done(), "the reporter must end with its connection"
        await task

    asyncio.run(scenario())


def test_mute_at_the_till_is_reported_like_any_other_change():
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=11, backend=backend)
    connection = RecordingConnection()

    asyncio.run(drive(
        pilot, connection,
        before=lambda: backend.change(ENDPOINT, 60, muted=True),
        after=lambda: backend.change(ENDPOINT, 60, muted=False)))

    assert [r["muted"] for r in connection.endpoint_states()] == [True, False]


def test_the_reporter_carries_no_credential():
    backend = FakeObserverBackend()
    pilot = make_pilot(session_id=5, backend=backend)
    connection = RecordingConnection()

    asyncio.run(drive(pilot, connection,
                      before=lambda: backend.change(ENDPOINT, 45, muted=False)))

    body = str(connection.endpoint_states()).lower()
    for forbidden in ("token", "secret", "password", "credential", "hmac"):
        assert forbidden not in body, (
            f"{forbidden} must never appear in routine telemetry")
