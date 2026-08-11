"""stand_down and resume: a Store that goes quiet without leaving.

WHY THIS PRIMITIVE EXISTS

Stop is terminal. A Receiver that is stopped gives the Windows output back and
forgets the session, and HQ releases the Store's lease - which is exactly right
for removal and exactly wrong for Pause. Pausing one shop for ninety seconds
must not make it drop out of the Broadcast, re-negotiate readiness, and rejoin
as a stranger.

So the state machine here has to say three things, and each has its own test:

  * a stood-down Store is NOT ready - its output device is closed, so claiming
    readiness would let HQ resume into a device something else has taken;
  * a stood-down Store is still IN the session - stopped is what releases it,
    and pause is not stop;
  * resume restores readiness and nothing more. It says the device is open,
    never that sound is audible; only a playback acknowledgement says that.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

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

from audio_protocol import (  # noqa: E402
    AudioProtocolError, build_resume_message, build_stand_down_message,
)
from receiver_contract import (  # noqa: E402
    PlaybackState, ReadinessState, ReceiverSnapshot, WrongSessionError,
    apply_receiver_ack, mark_connected, parse_receiver_ack,
)

UTC_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def payload(message_type, sequence=0, **extra):
    data = {
        "protocol_version": "1.0",
        "type": message_type,
        "message_id": str(uuid4()),
        "occurred_at": UTC_NOW.isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
    }
    data.update(extra)
    return data


def apply(snapshot, message_type, sequence, **extra):
    ack = parse_receiver_ack(payload(message_type, sequence, **extra))
    return apply_receiver_ack(snapshot, ack, UTC_NOW + timedelta(seconds=1))


def playing_snapshot(session_id=42):
    """A Store that is connected, ready, and receiving audio."""
    snapshot = replace(mark_connected(ReceiverSnapshot(), UTC_NOW),
                       active_session_id=session_id)
    snapshot = apply(snapshot, "receiver_ready", 1,
                     software_checks_passed=True,
                     output_device_checks_passed=True)
    snapshot = apply(snapshot, "audio_receiving", 2, session_id=session_id)
    assert snapshot.readiness == ReadinessState.READY
    assert snapshot.playback == PlaybackState.AUDIO_RECEIVING
    return snapshot


# ===========================================================================
# The control messages
# ===========================================================================

def test_stand_down_and_stop_are_different_messages():
    """If these were ever the same wire message, Pause would release the lease."""
    stand_down = build_stand_down_message(session_id=7)
    assert stand_down["type"] == "stand_down"
    assert stand_down["session_id"] == 7
    assert stand_down["reason"] == "operator_pause"


def test_resume_carries_the_generation_it_comes_back_on():
    """Without it, a `stopped` in flight when the pause began would land on the
    resumed participation and mark a playing shop as stopped."""
    resumed = build_resume_message(session_id=7, store_id=3, generation=2)
    assert resumed == {"type": "resume", "session_id": 7, "store_id": 3,
                       "generation": 2}


@pytest.mark.parametrize("bad", [0, -1, "3", None])
def test_a_message_without_a_real_session_is_refused(bad):
    with pytest.raises(AudioProtocolError):
        build_stand_down_message(session_id=bad)
    with pytest.raises(AudioProtocolError):
        build_resume_message(session_id=bad, store_id=1)


def test_a_reason_with_control_characters_is_refused():
    with pytest.raises(AudioProtocolError):
        build_stand_down_message(session_id=1, reason="pause\nthen\rlie")


# ===========================================================================
# What the acknowledgements do to the state
# ===========================================================================

def test_standing_down_surrenders_readiness():
    """The output device was closed, so the Store is no longer proven fit.

    If readiness survived a pause, HQ would resume into a device that might
    since have been taken by something else - and find out at the speaker.
    """
    snapshot = playing_snapshot()
    stood_down = apply(snapshot, "stood_down", 3, session_id=42,
                       reason="operator_pause")

    assert stood_down.readiness == ReadinessState.UNKNOWN
    assert stood_down.requires_ready is True
    assert stood_down.playback == PlaybackState.STOPPED


def test_a_stood_down_store_is_still_in_the_session():
    """Pause is not Stop. The session pointer is what HQ reads to decide the
    Store still belongs to this Broadcast, and it must survive a pause."""
    snapshot = playing_snapshot(session_id=42)
    stood_down = apply(snapshot, "stood_down", 3, session_id=42)
    assert stood_down.active_session_id == 42


def test_resuming_restores_readiness_and_claims_nothing_about_sound():
    snapshot = playing_snapshot()
    stood_down = apply(snapshot, "stood_down", 3, session_id=42)
    resumed = apply(stood_down, "resumed", 4, session_id=42, generation=2)

    assert resumed.readiness == ReadinessState.READY
    assert resumed.requires_ready is False
    # Deliberately NOT audio_receiving. The device being open is not the same
    # fact as sound leaving it, and only the Store can report the second.
    assert resumed.playback == PlaybackState.STOPPED


def test_audio_after_a_resume_reports_playback_again():
    snapshot = playing_snapshot()
    snapshot = apply(snapshot, "stood_down", 3, session_id=42)
    snapshot = apply(snapshot, "resumed", 4, session_id=42, generation=2)
    snapshot = apply(snapshot, "audio_receiving", 5, session_id=42)
    assert snapshot.playback == PlaybackState.AUDIO_RECEIVING


def test_an_acknowledgement_for_another_session_is_refused():
    """The guard that stops a late message from a previous participation
    landing on the current one."""
    snapshot = playing_snapshot(session_id=42)
    with pytest.raises(WrongSessionError):
        apply(snapshot, "stood_down", 3, session_id=99)
    with pytest.raises(WrongSessionError):
        apply(snapshot, "resumed", 3, session_id=99)


def test_a_stood_down_store_can_still_be_stopped():
    """Removal must work on a paused Store - otherwise Pause becomes a way to
    make a shop unremovable."""
    snapshot = playing_snapshot()
    snapshot = apply(snapshot, "stood_down", 3, session_id=42)
    stopped = apply(snapshot, "stopped", 4, session_id=42, reason="operator_stop")
    assert stopped.playback == PlaybackState.STOPPED
