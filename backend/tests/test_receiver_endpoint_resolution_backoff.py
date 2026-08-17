"""Resolving the shop's audio endpoint must not starve the heartbeat.

WHAT WAS SEEN FROM A LIVE SHOP

Store 31 reconnected every sixty to eighty seconds, reported its own volume
not once in that time, and a broadcast started against it never received a
receiver_ready - so the console refused with "no Receiver reported READY"
about a Receiver that was running the whole time and had just said so.

Three facts join up. `_ensure_windows_endpoint` caches SUCCESS only. The
volume poll calls it every three seconds. And resolution is a blocking COM
enumeration of every playback endpoint on the machine, which was running on
the event loop. A Store whose device name stops resolving therefore blocks its
own loop over and over; the heartbeat goes late; HQ's snapshot ages past
thirty seconds and it closes the socket with 4408. The Receiver reconnects,
and does it again.

Both halves are tested here: the failure is remembered for a while, and the
resolution does not run on the event loop.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools  # noqa: E402
from tools import audio_receiver_pilot  # noqa: E402


def _install_endpoint_module(monkeypatch, module):
    """Replace tools.windows_endpoint_volume for the duration of one test.

    BOTH places, and that is not belt and braces. `from tools import X` reads
    the ATTRIBUTE on the package when the submodule has already been imported,
    and only falls back to sys.modules when it has not - so patching sys.modules
    alone worked when this file ran on its own and was ignored under xdist,
    where another test in the same worker had already imported the real thing.
    """
    monkeypatch.setitem(sys.modules, "tools.windows_endpoint_volume", module)
    monkeypatch.setattr(tools, "windows_endpoint_volume", module, raising=False)


class _Sink:
    is_hardware = True
    device = types.SimpleNamespace(name="Shop Speaker", selector="Shop Speaker")


def _pilot():
    pilot = audio_receiver_pilot.AudioReceiverPilot.__new__(
        audio_receiver_pilot.AudioReceiverPilot)
    pilot.sink = _Sink()
    pilot.windows_endpoint_id = None
    pilot._endpoint_backend = None
    return pilot


@pytest.fixture()
def refusing_endpoints(monkeypatch):
    """A machine where the device name resolves to nothing, and counts tries."""
    calls = []

    def resolve(name, backend=None):
        calls.append(name)
        raise RuntimeError("no endpoint matches that device")

    _install_endpoint_module(
        monkeypatch,
        types.SimpleNamespace(resolve_endpoint_for_playback_device=resolve))
    return calls


def test_a_failed_resolution_is_not_retried_at_poll_speed(refusing_endpoints,
                                                          monkeypatch):
    pilot = _pilot()
    clock = {"now": 1000.0}
    monkeypatch.setattr(audio_receiver_pilot.time, "monotonic",
                        lambda: clock["now"])

    assert pilot._ensure_windows_endpoint() is None
    assert len(refusing_endpoints) == 1

    # The poll runs every three seconds. Fifteen more attempts - forty-five
    # seconds, comfortably inside the sixty-second window - must cost nothing.
    # That is the whole defect: each one was a full COM enumeration, and they
    # were happening on the event loop.
    for _ in range(15):
        clock["now"] += 3.0
        assert pilot._ensure_windows_endpoint() is None
    assert len(refusing_endpoints) == 1

    # But it does try again. Somebody may have switched the speaker back on,
    # and a Store that gave up permanently would need a restart to notice.
    clock["now"] += audio_receiver_pilot.AudioReceiverPilot.\
        ENDPOINT_RESOLVE_RETRY_SECONDS
    assert pilot._ensure_windows_endpoint() is None
    assert len(refusing_endpoints) == 2


def test_resolving_to_nothing_is_a_failure_too(monkeypatch):
    """The path that does not raise was the one that retried at full speed."""
    calls = []

    def resolve(name, backend=None):
        calls.append(name)
        return None

    _install_endpoint_module(
        monkeypatch,
        types.SimpleNamespace(resolve_endpoint_for_playback_device=resolve))
    clock = {"now": 500.0}
    monkeypatch.setattr(audio_receiver_pilot.time, "monotonic",
                        lambda: clock["now"])

    pilot = _pilot()
    assert pilot._ensure_windows_endpoint() is None
    clock["now"] += 3.0
    assert pilot._ensure_windows_endpoint() is None
    assert len(calls) == 1


def test_the_volume_poll_resolves_off_the_event_loop(monkeypatch):
    """A blocked loop is a heartbeat that does not go out.

    Proved by refusing to run it any other way: this stand-in raises if it is
    called from the thread the event loop is on.
    """
    loop_thread = None
    seen = {}

    def resolve_on_the_wrong_thread(self):
        import threading
        seen["thread"] = threading.current_thread().ident
        if seen["thread"] == loop_thread:
            raise AssertionError(
                "endpoint resolution ran on the event loop - this is the "
                "blocking COM call that starved the heartbeat")
        return None

    monkeypatch.setattr(
        audio_receiver_pilot.AudioReceiverPilot, "_ensure_windows_endpoint",
        resolve_on_the_wrong_thread, raising=True)

    pilot = _pilot()
    pilot._next_volume_poll = 0.0

    async def drive():
        nonlocal loop_thread
        import threading
        loop_thread = threading.current_thread().ident
        return await pilot._report_store_volume_if_changed(None, force=True)

    assert asyncio.run(drive()) is True
    assert seen["thread"] is not None
