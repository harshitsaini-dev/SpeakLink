"""What the Store's Windows output is doing must reach HQ.

THE REPORTED GAP

HQ could set a Store's master volume but never see it. When the person at the
till moved the Windows slider from 80% to 25%, the shop really changed and the
Console went on displaying 80% - confidently wrong about the one number the
operator was reading.

THE LINE THIS MUST NOT CROSS

    ORIGINAL pre-broadcast state  -> the restoration authority
    CURRENT endpoint state        -> what HQ displays

Telemetry only ever produces the second. A Store user turning the volume down
mid-announcement must not change what gets put back at the end, and that is the
single most important thing asserted here.
"""

from __future__ import annotations

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

from store_audio_control import (  # noqa: E402
    InvalidVolumeError,
    StoreAudioControlRegistry,
)
from tools.windows_endpoint_observer import EndpointObserver  # noqa: E402

BP_ID = "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}"


class FakeObserverBackend:
    """Lets a test play the part of Core Audio raising a notification."""

    def __init__(self):
        self.registered = {}
        self.unregistered = []

    def register(self, endpoint_id, on_change):
        self.registered[endpoint_id] = on_change

    def unregister(self, endpoint_id):
        self.unregistered.append(endpoint_id)
        self.registered.pop(endpoint_id, None)

    def change(self, endpoint_id, volume_percent, muted=False):
        self.registered[endpoint_id](volume_percent, muted)


# ===========================================================================
# The observer
# ===========================================================================
def test_a_local_change_becomes_a_reading():
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()

    backend.change(BP_ID, 25, False)
    reading = observer.take()

    assert reading.volume_percent == 25
    assert reading.muted is False
    assert reading.sequence == 1


def test_mute_and_unmute_are_both_observed():
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()

    backend.change(BP_ID, 25, True)
    assert observer.take().muted is True
    backend.change(BP_ID, 25, False)
    assert observer.take().muted is False


def test_a_rapid_drag_is_coalesced_to_where_it_stopped():
    """30->31->32->40->55 is one fact: it ended at 55.

    Sending every step would put a dozen frames on a socket that is also
    carrying audio, and none of the intermediates is true by the time HQ draws
    it.
    """
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()

    for value in (30, 31, 32, 40, 55):
        backend.change(BP_ID, value, False)

    reading = observer.take()
    assert reading.volume_percent == 55
    assert observer.take() is None, "intermediates must be dropped, not queued"


def test_the_observer_holds_one_slot_however_noisy_the_store():
    """No unbounded queue: a Store hammering the slider cannot grow memory."""
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()
    for value in range(0, 101):
        backend.change(BP_ID, value, False)
    assert observer.take().volume_percent == 100
    assert observer.take() is None


def test_sequence_numbers_increase_so_staleness_is_detectable():
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()
    backend.change(BP_ID, 30, False)
    first = observer.take().sequence
    backend.change(BP_ID, 40, False)
    assert observer.take().sequence > first


def test_stopping_detaches_and_is_safe_twice():
    """A callback left attached would report into the NEXT broadcast."""
    backend = FakeObserverBackend()
    observer = EndpointObserver(BP_ID, backend=backend)
    observer.start()
    observer.stop()
    assert BP_ID in backend.unregistered
    assert observer.started is False
    observer.stop()


# ===========================================================================
# The backend runtime model
# ===========================================================================
@pytest.fixture()
def registry():
    made = StoreAudioControlRegistry()
    made.start_session(session_id=7, owner_user_id=1, store_ids=[10, 11])
    return made


def test_telemetry_updates_actual_and_leaves_requested_alone(registry):
    """The heart of it: two values, both true, never conflated."""
    registry.request(session_id=7, store_id=10, volume_percent=80)
    updated = registry.observe_endpoint_state(
        session_id=7, store_id=10, state_sequence=1,
        volume_percent=25, muted=False)

    assert updated.actual_volume_percent == 25
    # HQ asked for 80 and that request has not been retracted by somebody at
    # the till turning it down.
    assert updated.requested_volume_percent == 80
    assert updated.last_command_id == 1, "telemetry must not consume a command id"


def test_older_telemetry_cannot_drag_the_console_backwards(registry):
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=12,
                                    volume_percent=25, muted=False)
    stale = registry.observe_endpoint_state(session_id=7, store_id=10,
                                            state_sequence=11,
                                            volume_percent=40, muted=False)
    assert stale is None
    assert registry.state_for(7, 10).actual_volume_percent == 25


def test_repeated_sequence_is_ignored(registry):
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=5,
                                    volume_percent=25, muted=False)
    assert registry.observe_endpoint_state(
        session_id=7, store_id=10, state_sequence=5,
        volume_percent=99, muted=False) is None
    assert registry.state_for(7, 10).actual_volume_percent == 25


def test_telemetry_for_a_finished_session_is_discarded(registry):
    registry.end_session(7)
    assert registry.observe_endpoint_state(
        session_id=7, store_id=10, state_sequence=1,
        volume_percent=25, muted=False) is None


def test_telemetry_for_a_store_outside_the_session_is_discarded(registry):
    assert registry.observe_endpoint_state(
        session_id=7, store_id=999, state_sequence=1,
        volume_percent=25, muted=False) is None


def test_one_stores_telemetry_never_touches_another(registry):
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=1,
                                    volume_percent=25, muted=False)
    assert registry.state_for(7, 11).actual_volume_percent is None


def test_concurrent_broadcasts_do_not_share_actual_state(registry):
    registry.start_session(session_id=8, owner_user_id=2, store_ids=[20])
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=1,
                                    volume_percent=25, muted=False)
    registry.observe_endpoint_state(session_id=8, store_id=20, state_sequence=1,
                                    volume_percent=90, muted=False)
    assert registry.state_for(7, 10).actual_volume_percent == 25
    assert registry.state_for(8, 20).actual_volume_percent == 90


def test_mute_telemetry_flows_both_ways(registry):
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=1,
                                    volume_percent=25, muted=True)
    assert registry.state_for(7, 10).actual_muted is True
    registry.observe_endpoint_state(session_id=7, store_id=10, state_sequence=2,
                                    volume_percent=25, muted=False)
    assert registry.state_for(7, 10).actual_muted is False


def test_an_out_of_range_reading_is_refused(registry):
    # InvalidVolumeError is imported at the TOP of this file, from the same
    # import that produced StoreAudioControlRegistry, so the exception class
    # and the object that raises it come from one module object.
    #
    # Importing it here instead would look identical and be wrong: other suites
    # deliberately drop store_audio_control from sys.modules to build a clean
    # server, so a later import returns a NEW module whose InvalidVolumeError
    # is a different class - and pytest.raises would report "DID NOT RAISE"
    # while the exception was in fact raised.
    with pytest.raises(InvalidVolumeError):
        registry.observe_endpoint_state(session_id=7, store_id=10,
                                        state_sequence=1, volume_percent=150,
                                        muted=False)


# ===========================================================================
# Restoration must be immune to telemetry
# ===========================================================================
def test_live_telemetry_never_mutates_the_restoration_snapshot():
    """The single most important assertion in this file.

    Original 10% muted; HQ sets 80; the till moves it to 30. Stop must put back
    10% muted - not 30, and not 80.
    """
    from tools.audio_receiver_pilot import AudioReceiverPilot
    from tools import windows_endpoint_volume as endpoint

    class FakeEndpoint:
        def __init__(self):
            self.volume_percent, self.muted = 10, True
        def GetMasterVolumeLevelScalar(self): return self.volume_percent / 100.0
        def GetMute(self): return 1 if self.muted else 0
        def SetMasterVolumeLevelScalar(self, scalar, _c):
            self.volume_percent = max(0, min(100, round(scalar * 100)))
        def SetMute(self, muted, _c): self.muted = bool(muted)

    class Backend:
        def __init__(self, ep): self.ep = ep
        def controller(self, endpoint_id):
            if endpoint_id != BP_ID:
                raise endpoint.EndpointNotFound(endpoint_id)
            return self.ep
        def list_endpoints(self):
            return [{"endpoint_id": BP_ID, "name": "Speakers"}]

    hardware = FakeEndpoint()
    backend = Backend(hardware)

    pilot = AudioReceiverPilot(ws_url="ws://test/ws")
    pilot.windows_endpoint_id = BP_ID
    pilot._endpoint_backend = backend
    # The snapshot, captured before the first mutation exactly as PREPARE does.
    pilot._endpoint_original = endpoint.read_state(BP_ID, backend=backend)
    assert pilot._endpoint_original.volume_percent == 10
    assert pilot._endpoint_original.muted is True

    # HQ sets 80, then somebody at the till drags it to 30 and unmutes.
    endpoint.apply_state(BP_ID, volume_percent=80, muted=False, backend=backend)
    hardware.volume_percent, hardware.muted = 30, False

    result = pilot.restore_windows_endpoint()

    assert result["restored"] is True
    assert hardware.volume_percent == 10, "the ORIGINAL value, not the live one"
    assert hardware.muted is True


def test_telemetry_carries_no_secret():
    """The event travels a socket and lands in logs and diagnostics bundles."""
    from receiver_contract import EndpointStateAcknowledgement

    ack = EndpointStateAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f6",
        occurred_at="2026-08-05T10:00:00+00:00",
        sequence=4, type="endpoint_state", session_id=7,
        state_sequence=3, volume_percent=25, muted=False,
    )
    payload = ack.model_dump_json().lower()
    for leak in ("credential", "token", "password", "secret", "bearer",
                 "endpoint_id", "verifier"):
        assert leak not in payload, leak


def test_endpoint_state_changes_no_playback_or_readiness_status():
    """A quiet shop is not a broken shop."""
    from datetime import datetime, timezone

    from receiver_contract import (
        ConnectionState, EndpointStateAcknowledgement, PlaybackState,
        ReadinessState, ReceiverSnapshot, apply_receiver_ack,
    )

    snapshot = ReceiverSnapshot(
        connection=ConnectionState.CONNECTED,
        readiness=ReadinessState.READY,
        playback=PlaybackState.PLAYBACK_CONFIRMED,
        active_session_id=7, requires_ready=False, last_sequence=3)
    ack = EndpointStateAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f7",
        occurred_at="2026-08-05T10:00:00+00:00",
        sequence=4, type="endpoint_state", session_id=7,
        state_sequence=1, volume_percent=25, muted=True)

    updated = apply_receiver_ack(snapshot, ack,
                                 received_at=datetime.now(timezone.utc))
    assert updated.playback is PlaybackState.PLAYBACK_CONFIRMED
    assert updated.readiness is ReadinessState.READY
