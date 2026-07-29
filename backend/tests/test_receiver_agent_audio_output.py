"""Why a Store reported PLAYBACK_CONFIRMED and nobody heard anything.

Three separate facts, each proven from the source rather than the README:

**The packaged Receiver discards audio by default.** ``resolve_sink_configuration``
returns the *null* sink unless ``ECHOCAST_AUDIO_SINK_MODE`` says otherwise, and
in null mode the decoder runs ``_read_progress`` - it reads FFmpeg's progress
output and never opens a Windows device at all.

**PLAYBACK_CONFIRMED does not mean "a device played it".** It is emitted when
``decoder.wait_for_decode`` returns true, and in null mode that only proves
FFmpeg produced PCM. So a Store can be green on the dashboard while the
speakers are silent, which is exactly what happened on the two-PC test.

**The escape hatch was unreachable, and failed like a crash.** Windows mode
needs an exact, unambiguous device selector, and the same display name appears
under MME, DirectSound, WASAPI and WDM-KS - measured on this machine, one name
matched three devices. A Store PC has no Python, so there was no way to list
devices and find a stable ``index:N@Name`` selector. And when resolution failed,
``SinkConfigurationError`` inherits ``AudioReceiverError``, not ``AgentError``,
so it was never caught: the operator got a traceback and the Store went OFFLINE
with nothing explaining why.

Nothing here opens a real audio device or makes a sound. Every device list is
injected.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools.audio_receiver_pilot import (  # noqa: E402
    SINK_MODE_NULL,
    SINK_MODE_WINDOWS,
    SinkConfigurationError,
    resolve_sink_configuration,
)
from tools.windows_audio_devices import OutputDevice  # noqa: E402

from tools.receiver_agent import (  # noqa: E402
    EXIT_REFUSED,
    build_parser,
    describe_sink,
    main,
    resolve_agent_sink,
)


def device(index: int, name: str, host_api: str, channels: int = 2,
           rate: float = 44100.0, default: bool = False) -> OutputDevice:
    return OutputDevice(index=index, name=name, host_api=host_api,
                        max_output_channels=channels, default_samplerate=rate,
                        is_default=default)


#: What a real Realtek desktop looks like: one endpoint, four host APIs. The
#: MME name is truncated to 31 characters by Windows itself.
REALTEK = (
    device(1, "Speakers (Realtek(R) Audio", "MME", default=True),
    device(5, "Speakers (Realtek(R) Audio)", "Windows DirectSound"),
    device(9, "Speakers (Realtek(R) Audio)", "Windows WASAPI", rate=48000.0),
    device(13, "Speakers (Realtek(R) Audio)", "Windows WDM-KS"),
)


class FakeBackend:
    """Stands in for sounddevice. Opens nothing."""

    def __init__(self, devices=REALTEK):
        self._devices = devices

    def query_devices(self):
        return [
            {"name": d.name, "max_output_channels": d.max_output_channels,
             "default_samplerate": d.default_samplerate, "hostapi": index}
            for index, d in enumerate(self._devices)
        ]


# ===========================================================================
# What the default actually is
# ===========================================================================
def test_the_default_sink_discards_audio(monkeypatch):
    """The whole bug in one assertion."""
    monkeypatch.delenv("ECHOCAST_AUDIO_SINK_MODE", raising=False)
    monkeypatch.delenv("ECHOCAST_AUDIO_OUTPUT_DEVICE", raising=False)
    assert resolve_sink_configuration().sink_mode == SINK_MODE_NULL


def test_the_null_sink_opens_no_device(monkeypatch):
    monkeypatch.delenv("ECHOCAST_AUDIO_SINK_MODE", raising=False)
    assert resolve_sink_configuration().device is None


def test_a_leftover_device_variable_cannot_cause_a_sound(monkeypatch):
    monkeypatch.delenv("ECHOCAST_AUDIO_SINK_MODE", raising=False)
    monkeypatch.setenv("ECHOCAST_AUDIO_OUTPUT_DEVICE", "index:5")
    configuration = resolve_sink_configuration()
    assert configuration.sink_mode == SINK_MODE_NULL
    assert configuration.device is None


# ===========================================================================
# The Agent says which one it is using, loudly
# ===========================================================================
def test_a_discarding_sink_is_described_as_discarding():
    """An operator reading the console must not have to know what "null" means.

    The Store showed PLAYBACK_CONFIRMED on the dashboard and made no sound, and
    nothing on the Receiver's own console said the audio was being thrown away.
    """
    lines = describe_sink(resolve_agent_sink(sink_mode=SINK_MODE_NULL, selector=None))
    joined = " ".join(lines).lower()
    assert "discard" in joined or "no sound" in joined
    assert "playback_confirmed" in joined, (
        "the warning must say what PLAYBACK_CONFIRMED means in this mode")


def test_a_real_device_is_described_by_name_and_index():
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS, selector="index:9", devices=REALTEK)
    joined = " ".join(describe_sink(configuration))
    assert "Speakers (Realtek(R) Audio)" in joined
    assert "9" in joined
    assert "WASAPI" in joined


def test_the_description_never_contains_a_credential():
    for configuration in (
        resolve_agent_sink(sink_mode=SINK_MODE_NULL, selector=None),
        resolve_agent_sink(sink_mode=SINK_MODE_WINDOWS, selector="index:9", devices=REALTEK),
    ):
        joined = " ".join(describe_sink(configuration))
        assert "echocast_rcv_v1" not in joined
        assert "Bearer" not in joined


# ===========================================================================
# Choosing a device
# ===========================================================================
def test_a_bare_index_selects_one_endpoint():
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS, selector="index:5", devices=REALTEK)
    assert configuration.device.index == 5
    assert configuration.device.host_api == "Windows DirectSound"


def test_a_verified_selector_pins_the_index_to_the_name():
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS,
        selector="index:9@Speakers (Realtek(R) Audio)", devices=REALTEK)
    assert configuration.device.index == 9


def test_a_verified_selector_is_refused_after_a_renumber():
    """Windows renumbers whenever a device is added or removed. Opening the
    wrong endpoint silently would be worse than refusing."""
    renumbered = (device(9, "LG IPS QHD-1 (NVIDIA High Definition Audio)", "Windows WASAPI"),)
    with pytest.raises(SinkConfigurationError):
        resolve_agent_sink(sink_mode=SINK_MODE_WINDOWS,
                           selector="index:9@Speakers (Realtek(R) Audio)",
                           devices=renumbered)


def test_the_exact_name_an_operator_would_copy_is_ambiguous():
    """The failure that took the Store OFFLINE.

    'Speakers (Realtek(R) Audio)' is one physical endpoint exposed by three host
    APIs, so the name alone cannot say which to open. Measured on the build
    machine: one name matched three devices.
    """
    with pytest.raises(SinkConfigurationError) as refusal:
        resolve_agent_sink(sink_mode=SINK_MODE_WINDOWS,
                           selector="Speakers (Realtek(R) Audio)", devices=REALTEK)
    message = str(refusal.value)
    assert "ambiguous" in message.lower()
    # It must hand back selectors that do work, not just complain.
    assert "index:" in message


def test_a_missing_device_is_refused_with_a_usable_message():
    with pytest.raises(SinkConfigurationError) as refusal:
        resolve_agent_sink(sink_mode=SINK_MODE_WINDOWS,
                           selector="Speakers (Some Other Card)", devices=REALTEK)
    assert "no output device" in str(refusal.value).lower()


def test_windows_mode_without_a_selector_is_refused():
    """It never picks for you, and never uses the Windows default device."""
    with pytest.raises(SinkConfigurationError):
        resolve_agent_sink(sink_mode=SINK_MODE_WINDOWS, selector=None, devices=REALTEK)


def test_an_unknown_sink_mode_is_refused():
    with pytest.raises(SinkConfigurationError):
        resolve_agent_sink(sink_mode="speakers", selector=None, devices=REALTEK)


def test_the_device_format_comes_from_the_device_not_a_hardcoded_guess():
    """Hardcoding 48 kHz made the open fail on host APIs that advertise 44.1."""
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS, selector="index:5", devices=REALTEK)
    assert configuration.sample_rate == 44100.0
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS, selector="index:9", devices=REALTEK)
    assert configuration.sample_rate == 48000.0


def test_channels_are_clamped_to_something_a_desktop_can_open():
    surround = (device(1, "Surround", "Windows WASAPI", channels=8),)
    configuration = resolve_agent_sink(
        sink_mode=SINK_MODE_WINDOWS, selector="index:1", devices=surround)
    assert configuration.channels == 2


# ===========================================================================
# The command line
# ===========================================================================
def test_run_accepts_an_explicit_sink_and_device():
    arguments = build_parser().parse_args([
        "run", "--backend-url", "https://hq", "--audio-sink", "windows",
        "--audio-output-device", "index:9@Speakers (Realtek(R) Audio)",
    ])
    assert arguments.audio_sink == "windows"
    assert arguments.audio_output_device == "index:9@Speakers (Realtek(R) Audio)"


def test_the_sink_option_only_accepts_modes_that_exist():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["run", "--backend-url", "https://hq",
                                   "--audio-sink", "speakers"])


def test_the_sink_options_default_to_none_so_the_environment_still_works():
    arguments = build_parser().parse_args(["run", "--backend-url", "https://hq"])
    assert arguments.audio_sink is None
    assert arguments.audio_output_device is None


def test_there_is_a_command_to_list_devices():
    """A Store PC has no Python, so `python tools/windows_audio_devices.py` -
    the only way to discover a stable selector - was unreachable on exactly the
    machines that needed it."""
    arguments = build_parser().parse_args(["list-audio-devices"])
    assert arguments.command == "list-audio-devices"


def test_listing_devices_needs_no_credential_and_no_backend():
    parser = build_parser()
    arguments = parser.parse_args(["list-audio-devices"])
    assert getattr(arguments, "backend_url", None) is None
    assert getattr(arguments, "credential_path", "MISSING") == "MISSING"


def test_the_device_list_command_actually_runs(capsys):
    """Parsing it is not running it.

    The first version of this test only checked the parser, and the command
    crashed the moment it was invoked: main() computed a credential path for
    every command before dispatching, and this one has no --credential-path.
    It failed on the single command an operator runs on a Store desktop before
    anything has been enrolled.
    """
    assert main(["list-audio-devices"]) == 0
    printed = capsys.readouterr().out
    assert "OUTPUT" in printed.upper()
    assert "--audio-output-device" in printed
    assert "echocast_rcv_v1" not in printed


def test_a_device_selector_cannot_smuggle_a_credential():
    parser = build_parser()
    for forbidden in ("--credential", "--code", "--token"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--backend-url", "https://hq", forbidden, "x"])


# ===========================================================================
# A bad audio configuration must not look like a crash
# ===========================================================================
def test_an_audio_failure_is_a_clean_refusal_not_a_traceback(capsys, tmp_path):
    """What the operator actually saw was an unhandled exception.

    SinkConfigurationError inherits AudioReceiverError, and main() caught only
    AgentError and CredentialStoreError, so it escaped. The Store went OFFLINE
    and the console showed a Python traceback naming files that do not exist on
    a Store PC.
    """
    code = main([
        "run", "--backend-url", "http://192.168.4.134:8000",
        "--allow-insecure-private-lan", "--expected-hq-host", "192.168.4.134",
        "--audio-sink", "windows",
        "--audio-output-device", "Speakers (No Such Device Anywhere)",
        "--credential-path", str(tmp_path / "device-credential.bin"),
    ])
    assert code == EXIT_REFUSED
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Traceback" not in output
    assert "Refused" in output
    # Which refusal matters. Both a bad device and a missing credential exit
    # REFUSED, so asserting only on the exit code would pass even if the sink
    # were still resolved after the credential - which is the ordering that let
    # a Store connect, get promoted, and only then fall over.
    assert "no output device" in output.lower()
    # The word "credential" also appears in the plain-HTTP warning, so this
    # matches the credential-loading refusal specifically.
    assert "no Receiver Device credential at" not in output, (
        "the audio configuration must be refused before the credential is read")


def test_the_refusal_names_no_secret(capsys, tmp_path):
    main([
        "run", "--backend-url", "http://192.168.4.134:8000",
        "--allow-insecure-private-lan", "--expected-hq-host", "192.168.4.134",
        "--audio-sink", "windows", "--audio-output-device", "Nothing At All",
        "--credential-path", str(tmp_path / "device-credential.bin"),
    ])
    output = capsys.readouterr().out + capsys.readouterr().err
    assert "echocast_rcv_v1" not in output


# ===========================================================================
# What the packaged executable must carry
# ===========================================================================
def test_the_package_spec_bundles_the_audio_backend():
    """Without PortAudio in the package, windows mode fails on the one machine
    it matters on, and only at the moment somebody expects a sound."""
    spec = (REPOSITORY_ROOT / "receiver_agent.spec").read_text(encoding="utf-8")
    assert "sounddevice" in spec
    assert "_sounddevice_data" in spec or "portaudio" in spec.lower()


def test_the_device_lister_is_reachable_from_the_frozen_agent():
    """It lives in a module the Agent must actually import, or the new command
    would fail only after packaging."""
    import tools.receiver_agent as agent

    assert hasattr(agent, "list_audio_devices_report")
