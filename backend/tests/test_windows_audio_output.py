"""Tests for Windows output-device discovery, selection and hardware sink mode.

**No test in this file ever opens a real audio device or plays a sound.**
Every test uses an injected fake backend, so the suite is safe on any machine,
including CI without a sound card.

The pilot's default remains the null sink. Hardware mode must be selected
explicitly and must fail closed on anything ambiguous.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.windows_audio_devices import (  # noqa: E402
    AmbiguousDeviceError,
    AudioDeviceError,
    DeviceEnumerationUnsupportedError,
    DeviceNotFoundError,
    OutputDevice,
    format_device_table,
    list_output_devices,
    resolve_output_device,
)
from tools.audio_receiver_pilot import (  # noqa: E402
    SINK_MODE_NULL,
    SINK_MODE_WINDOWS,
    AUDIO_OUTPUT_DEVICE_ENV,
    AUDIO_SINK_MODE_ENV,
    SinkConfigurationError,
    resolve_sink_configuration,
)


# ---------------------------------------------------------------------------
# A fake PortAudio-shaped backend. It can never make a sound.
# ---------------------------------------------------------------------------
class FakeAudioBackend:
    """Mirrors the tiny slice of the sounddevice API the tool actually uses."""

    def __init__(self, devices=None, hostapis=None, default_output=1, fail=False):
        self.fail = fail
        self.opened: list[dict] = []
        self.streams: list["FakeOutputStream"] = []
        self._hostapis = hostapis or [
            {"name": "MME"},
            {"name": "Windows DirectSound"},
            {"name": "Windows WASAPI"},
        ]
        self._devices = devices if devices is not None else [
            # index 0: input only, must be filtered out
            {"name": "Microphone (USB)", "hostapi": 0,
             "max_output_channels": 0, "max_input_channels": 2,
             "default_samplerate": 44100.0},
            {"name": "LG IPS QHD-1 (NVIDIA)", "hostapi": 0,
             "max_output_channels": 2, "max_input_channels": 0,
             "default_samplerate": 44100.0},
            # index 2 and 3 share an exact name across host APIs, exactly like
            # this machine really does. Name-only selection is ambiguous.
            {"name": "USB Audio Device", "hostapi": 1,
             "max_output_channels": 2, "max_input_channels": 0,
             "default_samplerate": 44100.0},
            {"name": "USB Audio Device", "hostapi": 2,
             "max_output_channels": 2, "max_input_channels": 0,
             "default_samplerate": 48000.0},
            {"name": "Headset (Bluetooth Hands-Free)", "hostapi": 2,
             "max_output_channels": 1, "max_input_channels": 1,
             "default_samplerate": 8000.0},
        ]
        self.default = type("D", (), {"device": (0, default_output)})()

    def query_hostapis(self):
        if self.fail:
            raise OSError("PortAudio unavailable")
        return self._hostapis

    def query_devices(self):
        if self.fail:
            raise OSError("PortAudio unavailable")
        return list(self._devices)

    # Only the Receiver's output stream uses this; enumeration must not.
    def RawOutputStream(self, **kwargs):  # noqa: N802 - mirrors sounddevice
        self.opened.append(kwargs)
        stream = FakeOutputStream(kwargs)
        self.streams.append(stream)
        return stream


class FakeOutputStream:
    def __init__(self, kwargs, fail_on_write=False):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.written_frames = 0
        self.fail_on_write = fail_on_write
        # Captured so a test can inspect the interleaving without a real device.
        self.data = bytearray()

    def start(self):
        self.started = True

    def write(self, data):
        if self.fail_on_write:
            raise OSError("output stream failed mid-session")
        self.data += bytes(data)
        self.written_frames += len(data) // 2  # 16-bit mono

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


@pytest.fixture()
def backend():
    return FakeAudioBackend()


# ---------------------------------------------------------------------------
# 7-8. Enumeration is read-only
# ---------------------------------------------------------------------------
def test_enumeration_lists_only_output_devices(backend):
    devices = list_output_devices(backend=backend)
    assert all(isinstance(d, OutputDevice) for d in devices)
    assert all(d.max_output_channels > 0 for d in devices)
    # The input-only device at index 0 must not appear.
    assert 0 not in {d.index for d in devices}
    assert {d.index for d in devices} == {1, 2, 3, 4}


def test_enumeration_reports_stable_identity_fields(backend):
    devices = {d.index: d for d in list_output_devices(backend=backend)}
    usb = devices[3]
    assert usb.name == "USB Audio Device"
    assert usb.host_api == "Windows WASAPI"
    assert usb.max_output_channels == 2
    assert usb.default_samplerate == 48000
    assert usb.selector == "index:3"


def test_enumeration_marks_the_current_default_without_changing_it(backend):
    devices = list_output_devices(backend=backend)
    defaults = [d for d in devices if d.is_default]
    assert len(defaults) == 1
    assert defaults[0].index == 1
    # Enumeration must never open a stream or alter the default.
    assert backend.opened == []
    assert backend.default.device == (0, 1)


def test_enumeration_opens_no_playback_stream(backend):
    list_output_devices(backend=backend)
    format_device_table(list_output_devices(backend=backend))
    assert backend.opened == []


def test_enumeration_failure_is_a_controlled_error():
    with pytest.raises(DeviceEnumerationUnsupportedError):
        list_output_devices(backend=FakeAudioBackend(fail=True))


def test_device_table_contains_no_credential(backend):
    table = format_device_table(list_output_devices(backend=backend))
    lowered = table.lower()
    for marker in ("token", "password", "authorization", "bearer", "jwt", "secret"):
        assert marker not in lowered
    assert "index:3" in table


# ---------------------------------------------------------------------------
# 3-6, 9. Selection safety
# ---------------------------------------------------------------------------
def test_explicit_stable_index_selects_exactly_one_device(backend):
    device = resolve_output_device("index:3", backend=backend)
    assert device.index == 3
    assert device.host_api == "Windows WASAPI"


def test_exact_unique_name_is_accepted(backend):
    device = resolve_output_device("LG IPS QHD-1 (NVIDIA)", backend=backend)
    assert device.index == 1


def test_duplicate_exact_name_fails_closed(backend):
    # Two host APIs expose the same name; the tool must refuse to guess.
    with pytest.raises(AmbiguousDeviceError) as error:
        resolve_output_device("USB Audio Device", backend=backend)
    assert "index:2" in str(error.value)
    assert "index:3" in str(error.value)


def test_partial_name_is_never_silently_accepted(backend):
    for partial in ("USB", "USB Audio", "LG", "Headset"):
        with pytest.raises(DeviceNotFoundError):
            resolve_output_device(partial, backend=backend)


def test_case_mismatched_name_is_not_silently_accepted(backend):
    with pytest.raises(DeviceNotFoundError):
        resolve_output_device("lg ips qhd-1 (nvidia)", backend=backend)


def test_unknown_device_fails(backend):
    with pytest.raises(DeviceNotFoundError):
        resolve_output_device("index:99", backend=backend)
    with pytest.raises(DeviceNotFoundError):
        resolve_output_device("No Such Device", backend=backend)


def test_input_only_device_cannot_be_selected(backend):
    with pytest.raises(DeviceNotFoundError):
        resolve_output_device("index:0", backend=backend)
    with pytest.raises(DeviceNotFoundError):
        resolve_output_device("Microphone (USB)", backend=backend)


def test_blank_selector_is_refused(backend):
    for blank in ("", "   ", None):
        with pytest.raises(AudioDeviceError):
            resolve_output_device(blank, backend=backend)


def test_a_bluetooth_device_is_listed_but_never_chosen_automatically(backend):
    devices = list_output_devices(backend=backend)
    bluetooth = [d for d in devices if "Bluetooth" in d.name]
    assert len(bluetooth) == 1
    # It is only reachable through an explicit, exact selector.
    assert resolve_output_device("index:4", backend=backend).index == 4


def test_selected_device_facts_contain_no_credential(backend):
    device = resolve_output_device("index:3", backend=backend)
    serialised = repr(device.as_dict()).lower()
    for marker in ("token", "password", "authorization", "bearer", "jwt", "secret"):
        assert marker not in serialised


# ---------------------------------------------------------------------------
# 1-2. Sink configuration boundary
# ---------------------------------------------------------------------------
def test_default_sink_mode_is_null(monkeypatch):
    monkeypatch.delenv(AUDIO_SINK_MODE_ENV, raising=False)
    monkeypatch.delenv(AUDIO_OUTPUT_DEVICE_ENV, raising=False)
    configuration = resolve_sink_configuration(backend=FakeAudioBackend())
    assert configuration.sink_mode == SINK_MODE_NULL
    assert configuration.device is None


def test_null_sink_ignores_any_configured_device(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_NULL)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:3")
    configuration = resolve_sink_configuration(backend=backend)
    assert configuration.sink_mode == SINK_MODE_NULL
    assert configuration.device is None
    assert backend.opened == []


def test_hardware_mode_without_a_device_fails(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.delenv(AUDIO_OUTPUT_DEVICE_ENV, raising=False)
    with pytest.raises(SinkConfigurationError) as error:
        resolve_sink_configuration(backend=backend)
    assert AUDIO_OUTPUT_DEVICE_ENV in str(error.value)


def test_hardware_mode_with_an_unknown_device_fails(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:99")
    with pytest.raises(SinkConfigurationError):
        resolve_sink_configuration(backend=backend)


def test_hardware_mode_with_an_ambiguous_device_fails(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "USB Audio Device")
    with pytest.raises(SinkConfigurationError):
        resolve_sink_configuration(backend=backend)


def test_hardware_mode_resolves_an_explicit_index(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:3")
    configuration = resolve_sink_configuration(backend=backend)
    assert configuration.sink_mode == SINK_MODE_WINDOWS
    assert configuration.device.index == 3
    # Resolving a device must not open it.
    assert backend.opened == []


def test_unknown_sink_mode_fails(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, "speakers-please")
    with pytest.raises(SinkConfigurationError):
        resolve_sink_configuration(backend=backend)


def test_sink_configuration_diagnostics_are_secret_free(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:3")
    facts = resolve_sink_configuration(backend=backend).as_dict()
    assert facts["sink_mode"] == SINK_MODE_WINDOWS
    assert facts["selected_device_id"] == "index:3"
    assert facts["selected_device_name"] == "USB Audio Device"
    # The format is negotiated from the device, not hardcoded: index:3 in the
    # fake advertises 2 channels at 48000 Hz.
    assert facts["channels"] == 2
    assert facts["sample_rate"] == 48000
    lowered = repr(facts).lower()
    for marker in ("token", "password", "authorization", "bearer", "jwt", "secret"):
        assert marker not in lowered


def test_output_device_name_is_not_a_receiver_identity(monkeypatch, backend):
    """A device name must never be reused as a Store or Receiver credential."""
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:3")
    facts = resolve_sink_configuration(backend=backend).as_dict()
    assert "store_code" not in facts
    assert "store_id" not in facts
    assert "receiver_token" not in facts


# ---------------------------------------------------------------------------
# 11-13, 21-25. Hardware sink behaviour, all against the fake backend
# ---------------------------------------------------------------------------
from tools.audio_receiver_pilot import (  # noqa: E402
    CHIME_GAIN,
    FfmpegDecoder,
    SinkConfiguration,
    WindowsPcmSink,
    play_test_chime,
)


def _hardware_config(backend, selector="index:3") -> SinkConfiguration:
    return SinkConfiguration(
        sink_mode=SINK_MODE_WINDOWS,
        device=resolve_output_device(selector, backend=backend),
    )


def test_pcm_sink_opens_only_the_selected_device(backend):
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    sink.open()
    try:
        assert len(backend.opened) == 1
        assert backend.opened[0]["device"] == 3
        assert backend.opened[0]["channels"] == 1
        assert backend.opened[0]["samplerate"] == 48000
    finally:
        sink.close()


def test_pcm_sink_counts_only_frames_the_device_accepted(backend):
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    sink.open()
    try:
        assert sink.write(b"\x00\x01" * 480) is True
        assert sink.frames_written == 480
        assert sink.failed is False
    finally:
        sink.close()


def test_pcm_sink_records_failure_instead_of_pretending_success(backend):
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    sink.open()
    sink._stream.fail_on_write = True
    try:
        assert sink.write(b"\x00\x01" * 480) is False
        assert sink.failed is True
        assert sink.frames_written == 0
    finally:
        sink.close()


def test_pcm_sink_close_releases_the_device(backend):
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    sink.open()
    stream = sink._stream
    sink.close()
    assert stream.closed is True
    assert sink.is_open is False
    # Closing twice must stay safe.
    sink.close()


def test_pcm_sink_refuses_a_null_configuration():
    with pytest.raises(Exception):
        WindowsPcmSink(SinkConfiguration(sink_mode=SINK_MODE_NULL, device=None))


def test_pcm_sink_open_failure_is_a_controlled_error(backend):
    def exploding(**kwargs):
        raise OSError("device busy")

    backend.RawOutputStream = exploding
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    with pytest.raises(Exception) as error:
        sink.open()
    assert "index:3" in str(error.value)


def test_decoder_null_mode_command_uses_no_output_device():
    command = " ".join(FfmpegDecoder(sink_mode=SINK_MODE_NULL).command())
    assert "-f null" in command
    assert "s16le" not in command


def test_decoder_windows_mode_emits_pcm_and_still_opens_no_device(backend):
    sink = WindowsPcmSink(_hardware_config(backend), backend=backend)
    command = " ".join(
        FfmpegDecoder(sink_mode=SINK_MODE_WINDOWS, pcm_sink=sink).command()
    )
    assert "-f s16le" in command
    assert "pipe:1" in command
    # FFmpeg itself must never be pointed at a Windows audio device.
    for forbidden in ("dshow", "waveaudio", "directsound", "-f null"):
        assert forbidden not in command


def test_decoder_windows_mode_requires_a_sink():
    with pytest.raises(Exception):
        FfmpegDecoder(sink_mode=SINK_MODE_WINDOWS)


def test_decoder_rejects_an_unknown_sink_mode():
    with pytest.raises(Exception):
        FfmpegDecoder(sink_mode="speakers")


# ---------------------------------------------------------------------------
# Phase 8. The manual chime is opt-in and never claims verification
# ---------------------------------------------------------------------------
def test_chime_requires_hardware_mode():
    with pytest.raises(Exception):
        play_test_chime(SinkConfiguration(sink_mode=SINK_MODE_NULL, device=None))


def test_chime_is_cancelled_unless_the_operator_types_yes(backend):
    outcome = play_test_chime(
        _hardware_config(backend), backend=backend, confirm=lambda _prompt: "n"
    )
    assert outcome["cancelled"] is True
    assert outcome["played"] is False
    assert outcome["frames_written"] == 0
    # Nothing was opened, so nothing could have made a sound.
    assert backend.opened == []


def test_chime_plays_only_after_explicit_confirmation(backend):
    outcome = play_test_chime(
        _hardware_config(backend), backend=backend,
        confirm=lambda _prompt: "yes", seconds=0.05,
    )
    assert outcome["played"] is True
    assert outcome["frames_written"] > 0
    assert len(backend.opened) == 1
    assert backend.opened[0]["device"] == 3


def test_chime_never_reports_speaker_verified(backend):
    outcome = play_test_chime(
        _hardware_config(backend), backend=backend,
        confirm=lambda _prompt: "yes", seconds=0.05,
    )
    assert outcome["speaker_verified"] is False


def test_chime_uses_a_conservative_gain():
    # A loud chime into an amplifier is a real hazard.
    assert 0 < CHIME_GAIN <= 0.15


def test_chime_in_a_non_interactive_shell_is_a_controlled_error(backend):
    """Found during hardware validation: a non-interactive shell raised a raw
    EOFError traceback instead of a controlled refusal."""
    def eof_confirm(_prompt):
        raise EOFError("EOF when reading a line")

    with pytest.raises(Exception) as error:
        play_test_chime(_hardware_config(backend), backend=backend, confirm=eof_confirm)
    assert "interactive" in str(error.value).lower()
    # Nothing may be opened when confirmation is impossible.
    assert backend.opened == []


# ---------------------------------------------------------------------------
# Chime channel interleaving
#
# Found during Bluetooth amplifier validation, when the operator heard nothing
# at all. The chime generated ONE int16 sample per frame - mono - but the sink
# opens the stream with the device's own channel count, which is 2 on every
# real endpoint here. PortAudio therefore read that mono buffer as interleaved
# stereo pairs, so the chime played for half its duration at double its pitch.
# A 0.75 s quiet tone is easily lost inside a Bluetooth A2DP link's wake-up
# delay, which is why nothing was audible.
# ---------------------------------------------------------------------------
def _stereo_config(backend, selector="index:2") -> SinkConfiguration:
    """A hardware config shaped like the real amplifier: 44100 Hz, 2 channels."""
    device = resolve_output_device(selector, backend=backend)
    return SinkConfiguration(
        sink_mode=SINK_MODE_WINDOWS,
        device=device,
        sample_rate=device.default_samplerate,
        channels=min(max(device.max_output_channels, 1), 2),
    )


def test_chime_fills_every_channel_of_a_stereo_device(backend):
    configuration = _stereo_config(backend)
    assert configuration.channels == 2, "fixture must be a stereo device"
    seconds = 0.2
    play_test_chime(
        configuration, backend=backend, confirm=lambda _p: "yes", seconds=seconds,
    )
    expected_frames = int(configuration.sample_rate * seconds)
    written = backend.streams[0].data
    # One frame carries one sample per channel, so the buffer must be
    # frames * channels * 2 bytes. Half of that means a mono buffer leaked in.
    assert len(written) == expected_frames * configuration.channels * 2


def test_chime_reports_the_full_frame_count_on_a_stereo_device(backend):
    configuration = _stereo_config(backend)
    seconds = 0.2
    outcome = play_test_chime(
        configuration, backend=backend, confirm=lambda _p: "yes", seconds=seconds,
    )
    assert outcome["frames_written"] == int(configuration.sample_rate * seconds)


def test_chime_sends_the_same_waveform_to_both_channels(backend):
    """A stereo frame must hold the same sample twice, not two consecutive
    samples of a mono wave - that is what doubled the pitch."""
    import struct

    configuration = _stereo_config(backend)
    play_test_chime(
        configuration, backend=backend, confirm=lambda _p: "yes", seconds=0.05,
    )
    written = bytes(backend.streams[0].data)
    samples = struct.unpack(f"<{len(written) // 2}h", written)
    left, right = samples[0::2], samples[1::2]
    assert left == right


def test_chime_still_correct_on_a_mono_device(backend):
    """Regression guard: the mono path must keep working after the fix."""
    device = resolve_output_device("index:4", backend=backend)  # 1 channel
    configuration = SinkConfiguration(
        sink_mode=SINK_MODE_WINDOWS, device=device,
        sample_rate=device.default_samplerate, channels=1,
    )
    seconds = 0.2
    outcome = play_test_chime(
        configuration, backend=backend, confirm=lambda _p: "yes", seconds=seconds,
    )
    expected_frames = int(configuration.sample_rate * seconds)
    assert outcome["frames_written"] == expected_frames
    assert len(backend.streams[0].data) == expected_frames * 2


# ---------------------------------------------------------------------------
# Chime duration is operator-selectable, because a Bluetooth amplifier can
# take a second or more to wake its DAC after a stream starts. The default
# stays the documented 1.5 s, and the range is bounded so nobody can hold an
# amplifier open indefinitely.
# ---------------------------------------------------------------------------
def test_chime_duration_defaults_to_the_documented_value():
    from tools.audio_receiver_pilot import CHIME_SECONDS, _build_parser

    assert CHIME_SECONDS == 1.5
    assert _build_parser().parse_args(["test-output"]).seconds == CHIME_SECONDS


def test_chime_duration_can_be_extended_for_a_slow_bluetooth_endpoint():
    from tools.audio_receiver_pilot import _build_parser

    assert _build_parser().parse_args(["test-output", "--seconds", "5"]).seconds == 5.0


def test_chime_duration_refuses_a_non_positive_value(backend):
    configuration = _stereo_config(backend)
    with pytest.raises(SinkConfigurationError):
        play_test_chime(
            configuration, backend=backend, confirm=lambda _p: "yes", seconds=0,
        )
    assert backend.opened == []


def test_chime_duration_refuses_an_unbounded_value(backend):
    from tools.audio_receiver_pilot import CHIME_MAX_SECONDS

    configuration = _stereo_config(backend)
    with pytest.raises(SinkConfigurationError):
        play_test_chime(
            configuration, backend=backend, confirm=lambda _p: "yes",
            seconds=CHIME_MAX_SECONDS + 1,
        )
    assert backend.opened == []


def test_chime_gain_is_not_operator_selectable():
    """Duration is safe to extend; loudness is not. There must be no CLI knob
    that can drive an amplifier harder than the conservative fixed gain."""
    from tools.audio_receiver_pilot import _build_parser

    options = {action.dest for action in _build_parser()._actions}
    assert "gain" not in options


# ---------------------------------------------------------------------------
# Device format negotiation
#
# Found during hardware validation: the sink hardcoded 48000 Hz / 1 channel
# and ignored what the device actually advertised. On this machine index:7
# reports 44100 Hz / 2 channels under WDM-KS, which is strict about formats,
# so the open could fail or the audio could be wrong.
# ---------------------------------------------------------------------------
def test_hardware_sink_adopts_the_device_sample_rate(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")  # 44100 Hz in the fake
    configuration = resolve_sink_configuration(backend=backend)
    assert configuration.device.default_samplerate == 44100
    assert configuration.sample_rate == 44100


def test_hardware_sink_adopts_the_device_channel_count(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")  # 2 output channels
    configuration = resolve_sink_configuration(backend=backend)
    assert configuration.channels == 2


def test_hardware_sink_keeps_mono_for_a_mono_device(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:4")  # 1 output channel
    configuration = resolve_sink_configuration(backend=backend)
    assert configuration.channels == 1


def test_null_sink_keeps_the_documented_defaults(monkeypatch):
    monkeypatch.delenv(AUDIO_SINK_MODE_ENV, raising=False)
    configuration = resolve_sink_configuration(backend=FakeAudioBackend())
    assert configuration.sample_rate == 48000
    assert configuration.channels == 1


def test_pcm_sink_opens_the_device_with_its_own_format(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")
    configuration = resolve_sink_configuration(backend=backend)
    sink = WindowsPcmSink(configuration, backend=backend)
    sink.open()
    try:
        opened = backend.opened[0]
        assert opened["samplerate"] == 44100
        assert opened["channels"] == 2
    finally:
        sink.close()


def test_ffmpeg_windows_command_matches_the_device_format(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")
    configuration = resolve_sink_configuration(backend=backend)
    sink = WindowsPcmSink(configuration, backend=backend)
    command = FfmpegDecoder(sink_mode=SINK_MODE_WINDOWS, pcm_sink=sink).command()
    joined = " ".join(command)
    # FFmpeg must resample and re-channel to whatever the device wants.
    assert "-ar 44100" in joined
    assert "-ac 2" in joined
    assert "-f s16le" in joined


def test_frame_accounting_uses_the_configured_channel_count(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")  # stereo
    configuration = resolve_sink_configuration(backend=backend)
    sink = WindowsPcmSink(configuration, backend=backend)
    sink.open()
    try:
        # 400 bytes of 16-bit stereo is 100 frames, not 200.
        sink.write(b"\x00\x01" * 200)
        assert sink.frames_written == 100
    finally:
        sink.close()


def test_sink_diagnostics_report_the_negotiated_format(monkeypatch, backend):
    monkeypatch.setenv(AUDIO_SINK_MODE_ENV, SINK_MODE_WINDOWS)
    monkeypatch.setenv(AUDIO_OUTPUT_DEVICE_ENV, "index:2")
    facts = resolve_sink_configuration(backend=backend).as_dict()
    assert facts["sample_rate"] == 44100
    assert facts["channels"] == 2


# ---------------------------------------------------------------------------
# Index stability
#
# Found during hardware validation: connecting a Bluetooth earbud set
# renumbered EVERY device index, and the previously chosen wired endpoint
# disappeared. A bare index saved in a runbook can therefore silently point at
# a completely different device later.
# ---------------------------------------------------------------------------
def test_a_verified_selector_pins_the_index_to_an_exact_name(backend):
    device = resolve_output_device("index:3@USB Audio Device", backend=backend)
    assert device.index == 3
    assert device.name == "USB Audio Device"


def test_a_verified_selector_fails_closed_after_a_renumber(backend):
    """The index still exists but now belongs to a different device."""
    with pytest.raises(DeviceNotFoundError) as error:
        resolve_output_device("index:1@USB Audio Device", backend=backend)
    message = str(error.value).lower()
    assert "renumber" in message or "no longer" in message


def test_a_verified_selector_reports_what_it_actually_found(backend):
    with pytest.raises(DeviceNotFoundError) as error:
        resolve_output_device("index:1@USB Audio Device", backend=backend)
    # The operator needs to see what is really at that index now.
    assert "LG IPS QHD-1 (NVIDIA)" in str(error.value)


def test_verified_selector_is_offered_for_every_listed_device(backend):
    table = format_device_table(list_output_devices(backend=backend))
    assert "index:3@USB Audio Device" in table


def test_bare_index_still_works_but_is_documented_as_unstable(backend):
    # Bare indices remain supported for convenience.
    assert resolve_output_device("index:3", backend=backend).index == 3
    table = format_device_table(list_output_devices(backend=backend))
    assert "not stable" in table.lower()


# ---------------------------------------------------------------------------
# Wireless detection
#
# Found during hardware validation: "Headphones (Nirvana X TWS Stereo)" is a
# Bluetooth A2DP endpoint but was not flagged, because only the hands-free
# variants contained an obvious marker.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "Headset (Bluetooth Hands-Free)",
        "Headphones (Nirvana X TWS Stereo)",
        "Headset (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free)",
        "Some A2DP Sink",
        "Wireless Earbuds",
        "AirPods Pro",
    ],
)
def test_wireless_endpoints_are_flagged(name):
    device = OutputDevice(
        index=1, name=name, host_api="Windows WASAPI",
        max_output_channels=2, default_samplerate=44100, is_default=False,
    )
    assert device.looks_wireless is True


@pytest.mark.parametrize(
    "name",
    [
        "Speakers (Realtek High Definition Audio)",
        "USB Audio Device",
        "LG IPS QHD-1 (NVIDIA High Definition Audio)",
        "Headphones ()",
    ],
)
def test_wired_endpoints_are_not_flagged(name):
    device = OutputDevice(
        index=1, name=name, host_api="Windows WDM-KS",
        max_output_channels=2, default_samplerate=44100, is_default=False,
    )
    assert device.looks_wireless is False


def test_wireless_flag_is_presented_as_a_heuristic(backend):
    table = format_device_table(list_output_devices(backend=backend))
    # It must not be presented as a certainty; the operator still confirms.
    assert "wireless?" in table
