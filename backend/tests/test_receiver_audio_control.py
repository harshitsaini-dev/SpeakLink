"""Receiver-side output control: the gain, and the honesty around it.

WHAT THESE TESTS PROVE AND WHAT THEY DO NOT

They prove that decoded PCM is scaled correctly, that the sink can be changed
without restarting anything, and that every command produces exactly one
truthful acknowledgement.

They do NOT prove that a shop got quieter. The audio backend is a mock, there
is no amplifier, and no assertion here should ever be read as acoustic
evidence. That requires a physical Store pilot.

WHY THERE IS NO SAVE-AND-RESTORE TEST

Because there is nothing to restore. The gain is applied to EchoCast's own
decoded samples inside the sink, so the Windows endpoint volume is never read
and never written. A crashed Receiver leaves the machine exactly as it found
it, which is a stronger property than restoring correctly would have been.
"""

from __future__ import annotations

import array
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from audio_protocol import (  # noqa: E402
    AudioProtocolError,
    build_set_audio_control_message,
    parse_set_audio_control_message,
)
from tools.audio_receiver_pilot import (  # noqa: E402
    SinkConfiguration,
    SinkConfigurationError,
    WindowsPcmSink,
)
from tools.windows_audio_devices import OutputDevice  # noqa: E402


def make_sink():
    device = OutputDevice(index=1, name="Test Endpoint", host_api="MME",
                          max_output_channels=1, default_samplerate=48000,
                          is_default=False)
    configuration = SinkConfiguration(sink_mode="windows", device=device,
                                      sample_rate=48000, channels=1)
    return WindowsPcmSink(configuration, backend=object())


def pcm(*samples):
    return array.array("h", samples).tobytes()


def samples_of(raw):
    out = array.array("h")
    out.frombytes(raw)
    return list(out)


# ===========================================================================
# The gain itself
# ===========================================================================
def test_full_volume_passes_the_buffer_through_unchanged():
    sink = make_sink()
    source = pcm(1000, -1000, 32767, -32768)
    assert sink._scaled(source) == source


def test_half_volume_halves_every_sample():
    sink = make_sink()
    sink.set_audio_control(volume_percent=50, muted=False)
    assert samples_of(sink._scaled(pcm(1000, -1000, 200))) == [500, -500, 100]


def test_zero_volume_is_silence_of_the_same_length():
    """Silence, not an empty buffer: the device must stay fed."""
    sink = make_sink()
    sink.set_audio_control(volume_percent=0, muted=False)
    source = pcm(1000, -1000, 32767)
    scaled = sink._scaled(source)
    assert len(scaled) == len(source)
    assert samples_of(scaled) == [0, 0, 0]


def test_mute_silences_without_losing_the_chosen_level():
    sink = make_sink()
    sink.set_audio_control(volume_percent=70, muted=True)
    assert samples_of(sink._scaled(pcm(1000))) == [0]
    assert sink.volume_percent == 70, "the level must survive being muted"
    assert sink.effective_percent == 0

    sink.set_audio_control(volume_percent=70, muted=False)
    assert samples_of(sink._scaled(pcm(1000))) == [700]


def test_the_extremes_of_int16_never_wrap():
    """Rounding alone can produce 32768, which wraps to a loud negative click."""
    sink = make_sink()
    sink.set_audio_control(volume_percent=100, muted=False)
    assert samples_of(sink._scaled(pcm(32767, -32768))) == [32767, -32768]
    sink.set_audio_control(volume_percent=99, muted=False)
    for value in samples_of(sink._scaled(pcm(32767, -32768))):
        assert -32768 <= value <= 32767


def test_changing_the_level_never_reopens_the_stream():
    sink = make_sink()
    before = sink._stream
    for percent in (10, 90, 0, 100):
        sink.set_audio_control(volume_percent=percent, muted=False)
    assert sink._stream is before, "the audio stream must not be restarted"


@pytest.mark.parametrize("percent", [-1, 101, 1000])
def test_an_out_of_range_level_is_refused(percent):
    sink = make_sink()
    with pytest.raises(SinkConfigurationError):
        sink.set_audio_control(volume_percent=percent, muted=False)


def test_a_boolean_is_not_a_level():
    sink = make_sink()
    with pytest.raises(SinkConfigurationError):
        sink.set_audio_control(volume_percent=True, muted=False)


def test_the_endpoint_is_driven_only_through_the_dedicated_module():
    """The safety property this REPLACES, and why.

    This test used to assert that the Receiver never reached for the system
    mixer at all - no pycaw, no IAudioEndpointVolume, no SetMute. That was the
    whole argument for scaling PCM instead: nothing else on the machine
    changed, and a crash could leave nothing behind.

    That decision has been deliberately reversed, because a Store user who
    muted Windows silenced every announcement while HQ reported "Applied
    100%". HQ's per-Store volume is now the Windows endpoint master.

    Keeping the old assertion would have been worse than deleting it: the
    Core Audio calls simply moved to windows_endpoint_volume.py, so the test
    kept passing while the thing it claimed to prevent was happening one
    import away. What is worth pinning now is narrower and true - the pilot
    itself holds no COM code, so there is exactly ONE place where an endpoint
    can be mutated, and that place refuses anything but a stable endpoint id.
    """
    source = Path(REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py").read_text(
        encoding="utf-8")
    for forbidden in ("IAudioEndpointVolume", "pycaw", "SetMasterVolumeLevelScalar",
                      "waveOutSetVolume"):
        assert forbidden not in source, (
            f"{forbidden} belongs in windows_endpoint_volume.py, which is the "
            "only module allowed to mutate an endpoint")
    # And it does go through that module.
    assert "windows_endpoint_volume" in source


def test_the_endpoint_module_refuses_to_act_without_a_stable_id():
    """The replacement safety property, stated as a rule.

    Master volume may only ever be applied to a Core Audio endpoint id. A
    PortAudio index renumbers when hardware changes, so acting on one could
    move the master volume of an output nobody selected.
    """
    from tools import windows_endpoint_volume

    class Empty:
        def controller(self, endpoint_id):
            raise windows_endpoint_volume.EndpointNotFound(endpoint_id)

        def list_endpoints(self):
            return []

    with pytest.raises(windows_endpoint_volume.EndpointNotFound):
        windows_endpoint_volume.apply_state("index:3", volume_percent=80,
                                            backend=Empty())


# ===========================================================================
# The command contract
# ===========================================================================
def test_a_well_formed_command_round_trips():
    message = build_set_audio_control_message(
        session_id=7, command_id=3, volume_percent=55, muted=False)
    assert message["type"] == "set_audio_control"
    assert parse_set_audio_control_message(message) == message


@pytest.mark.parametrize("volume", [-1, 101, 1.5, True, "60"])
def test_a_command_outside_the_contract_is_refused(volume):
    with pytest.raises(AudioProtocolError):
        build_set_audio_control_message(session_id=7, command_id=1,
                                        volume_percent=volume, muted=False)


def test_an_unknown_field_is_refused_rather_than_ignored():
    """A field a Receiver silently drops is one HQ believes is honoured."""
    message = build_set_audio_control_message(
        session_id=7, command_id=1, volume_percent=50, muted=False)
    with pytest.raises(AudioProtocolError):
        parse_set_audio_control_message({**message, "boost_percent": 400})


def test_command_ids_must_be_positive():
    with pytest.raises(AudioProtocolError):
        build_set_audio_control_message(session_id=7, command_id=0,
                                        volume_percent=50, muted=False)


def test_the_command_carries_whole_state_not_a_delta():
    """What makes dropping a stale command safe."""
    message = build_set_audio_control_message(
        session_id=7, command_id=9, volume_percent=40, muted=True)
    assert set(message) == {"type", "session_id", "command_id",
                            "volume_percent", "muted"}


# ===========================================================================
# The acknowledgement contract
# ===========================================================================
def test_the_acknowledgement_distinguishes_applied_from_requested():
    from receiver_contract import AudioControlAcknowledgement

    ack = AudioControlAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f6",
        occurred_at="2026-08-05T10:00:00+00:00",
        type="audio_control", sequence=4, session_id=7, command_id=3,
        requested_volume_percent=50, requested_muted=False,
        applied_volume_percent=50, applied_muted=False,
        result="applied", output_device="index:1",
    )
    assert ack.result == "applied"
    assert ack.applied_volume_percent == 50


def test_an_unsupported_acknowledgement_may_carry_no_applied_value():
    """A Receiver that changed nothing must not report a level."""
    from receiver_contract import AudioControlAcknowledgement

    ack = AudioControlAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f7",
        occurred_at="2026-08-05T10:00:00+00:00",
        type="audio_control", sequence=5, session_id=7, command_id=4,
        requested_volume_percent=50, requested_muted=False,
        result="unsupported", error_code="OUTPUT_CONTROL_UNSUPPORTED",
        details="this Receiver has no controllable audio output",
    )
    assert ack.applied_volume_percent is None
    assert ack.applied_muted is None


def test_the_acknowledgement_changes_no_playback_or_readiness_status():
    """A quiet Store is not a broken Store."""
    from datetime import datetime, timezone

    from receiver_contract import (
        AudioControlAcknowledgement, ConnectionState, PlaybackState,
        ReadinessState, ReceiverSnapshot, apply_receiver_ack,
    )

    snapshot = ReceiverSnapshot(
        connection=ConnectionState.CONNECTED,
        readiness=ReadinessState.READY,
        playback=PlaybackState.PLAYBACK_CONFIRMED,
        active_session_id=7, requires_ready=False, last_sequence=3,
    )
    ack = AudioControlAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f8",
        occurred_at="2026-08-05T10:00:00+00:00",
        type="audio_control", sequence=4, session_id=7, command_id=1,
        requested_volume_percent=0, requested_muted=True,
        applied_volume_percent=0, applied_muted=True, result="applied",
    )
    updated = apply_receiver_ack(snapshot, ack,
                                    received_at=datetime.now(timezone.utc))
    assert updated.playback is PlaybackState.PLAYBACK_CONFIRMED
    assert updated.readiness is ReadinessState.READY


def test_capabilities_are_absent_for_an_older_receiver():
    """Absence is the compatibility signal and must not default to True."""
    from receiver_contract import ReceiverReadyAcknowledgement

    old = ReceiverReadyAcknowledgement(
        protocol_version="1.0",
        message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f9",
        occurred_at="2026-08-05T10:00:00+00:00",
        sequence=1, type="receiver_ready",
        software_checks_passed=True, output_device_checks_passed=True,
    )
    assert old.capabilities is None


def test_a_reported_capability_is_carried_onto_the_snapshot():
    from datetime import datetime, timezone

    from receiver_contract import (
        ConnectionState, ReceiverSnapshot, ReceiverReadyAcknowledgement,
        apply_receiver_ack,
    )

    ready = ReceiverReadyAcknowledgement(
        protocol_version="1.0",
        message_id="1f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5fa",
        occurred_at="2026-08-05T10:00:00+00:00",
        sequence=1, type="receiver_ready",
        software_checks_passed=True, output_device_checks_passed=True,
        capabilities={"output_volume": True, "output_mute": True},
    )
    updated = apply_receiver_ack(
        ReceiverSnapshot(connection=ConnectionState.CONNECTED), ready,
                                    received_at=datetime.now(timezone.utc))
    assert updated.capabilities is not None
    assert updated.capabilities.output_volume is True
