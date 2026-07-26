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
        return FakeOutputStream(kwargs)


class FakeOutputStream:
    def __init__(self, kwargs, fail_on_write=False):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.written_frames = 0
        self.fail_on_write = fail_on_write

    def start(self):
        self.started = True

    def write(self, data):
        if self.fail_on_write:
            raise OSError("output stream failed mid-session")
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
    assert facts["channels"] == 1
    assert facts["sample_rate"] > 0
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
