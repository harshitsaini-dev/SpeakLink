"""The logic behind SpeakLinkStoreSetup.exe, with no GUI import in it.

WHY THE LOGIC LIVES HERE AND THE WINDOW DOES NOT

A tkinter window cannot be driven headlessly in CI the way a function can, so
every decision that matters - is this URL safe to use, did enrolment succeed,
which audio device did the operator actually pick, is the Receiver really
connected - lives in plain functions here and is tested directly. The GUI
module only calls these and paints results; if the GUI ever has to be rewritten
in something else, none of this changes.

NOTHING HERE REIMPLEMENTS THE RECEIVER.

Connecting, enrolling and sealing a credential are ``receiver_agent.enrol()``,
unchanged. Audio enumeration is ``windows_audio_devices.list_output_devices()``,
unchanged. The no-console child-process options are
``audio_receiver_pilot.hidden_child_process_options()``, unchanged. This module
composes them for a first-run wizard; it does not duplicate any of them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from tools.audio_receiver_pilot import hidden_child_process_options  # noqa: E402
from tools.receiver_agent import (  # noqa: E402
    AgentError,
    AlreadyEnrolled,
    EnrolmentAmbiguous,
    EnrolmentOutcome,
    EnrolmentRefused,
    EnrolmentUnavailable,
    EnrolmentUnreachable,
    InsecureBackendError,
    TerminalAuthentication,
    default_log_directory,
    describe_status,
    enrol,
    normalise_backend_url,
    read_status,
    receiver_status_path,
)
from tools.windows_audio_devices import (  # noqa: E402
    AudioDeviceError,
    OutputDevice,
    list_output_devices,
)


# ===========================================================================
# Screen 1 - HQ connection
# ===========================================================================
class ConnectionState(str, Enum):
    CONNECTING = "CONNECTING"
    CONNECTED_TO_HQ = "CONNECTED_TO_HQ"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    INSECURE_PUBLIC_URL_REFUSED = "INSECURE_PUBLIC_URL_REFUSED"
    PRIVATE_LAN_WARNING = "PRIVATE_LAN_WARNING"


@dataclass(frozen=True, slots=True)
class ConnectionResult:
    state: ConnectionState
    detail: str
    base_url: str = ""
    server_mode: "str | None" = None


def test_hq_connection(
    url: str,
    *,
    expected_hq_host: "str | None" = None,
    allow_insecure_private_lan: bool = False,
    allow_insecure_loopback: bool = False,
    timeout: float = 5.0,
    opener=None,
) -> ConnectionResult:
    """Refuse an unsafe URL before ever reaching the network; otherwise ask.

    ``normalise_backend_url`` is the ONLY place that decides whether a URL is
    safe to send a credential to later - reused verbatim so this screen and the
    real enrolment call can never disagree about what counts as safe.
    """
    try:
        base = normalise_backend_url(
            url,
            allow_insecure_loopback=allow_insecure_loopback,
            allow_insecure_private_lan=allow_insecure_private_lan,
            expected_hq_host=expected_hq_host,
        )
    except InsecureBackendError as refusal:
        return ConnectionResult(
            state=ConnectionState.INSECURE_PUBLIC_URL_REFUSED,
            detail=str(refusal),
        )
    except AgentError as refusal:
        return ConnectionResult(state=ConnectionState.CONNECTION_FAILED, detail=str(refusal))

    opener = opener or urllib.request.urlopen
    try:
        with opener(f"{base}/api/", timeout=timeout) as response:
            status = response.status
            body = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as failure:
        return ConnectionResult(
            state=ConnectionState.CONNECTION_FAILED,
            detail=f"could not reach {base} ({failure.__class__.__name__})",
            base_url=base,
        )

    if status != 200 or body.get("status") != "ok":
        return ConnectionResult(
            state=ConnectionState.CONNECTION_FAILED,
            detail=f"{base} answered but not as an SpeakLink HQ backend",
            base_url=base,
        )

    result_state = ConnectionState.CONNECTED_TO_HQ
    detail = f"{base} is reachable and answering"
    if base.startswith("http://") and not _is_loopback_url(base):
        result_state = ConnectionState.PRIVATE_LAN_WARNING
        detail = (
            f"{base} is reachable, but this is plain HTTP on a private LAN pilot "
            "path. Tokens travel unencrypted. Use HTTPS for anything beyond a "
            "private-LAN pilot."
        )
    return ConnectionResult(state=result_state, detail=detail, base_url=base,
                            server_mode=body.get("server_mode"))


def _is_loopback_url(base: str) -> bool:
    from urllib.parse import urlsplit

    host = urlsplit(base).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


# ===========================================================================
# Screen 2 - Enrolment
# ===========================================================================
class EnrolmentUiState(str, Enum):
    ENROLLING = "ENROLLING"
    ENROLLED = "ENROLLED"
    REFUSED = "REFUSED"


@dataclass(frozen=True, slots=True)
class EnrolmentUiResult:
    state: EnrolmentUiState
    detail: str
    outcome: "EnrolmentOutcome | None" = None


#: Safe, generic - the wire response the Store sees never carries a category.
#: The category the caller gave (revoked/expired/etc, if any) is not surfaced
#: here either: this module never receives it, because the backend does not
#: send it to an unauthenticated enrolment caller.
GENERIC_ENROLMENT_FAILURE = (
    "That enrolment code could not be used. Check it and try again, or ask HQ "
    "for a new one."
)


def redeem_enrollment(
    *,
    backend_url: str,
    code: str,
    device_name: str,
    hostname: str,
    credential_path,
    protector,
    allow_insecure_loopback: bool = False,
    allow_insecure_private_lan: bool = False,
    expected_hq_host: "str | None" = None,
    transport=None,
) -> EnrolmentUiResult:
    """Spend the code once. The Store sees only a generic failure; the reason
    stays out of this process's own hands, because the backend never sends it
    to an unauthenticated caller in the first place."""
    try:
        outcome = enrol(
            backend_url=backend_url,
            code=code,
            device_name=device_name,
            hostname=hostname,
            credential_path=credential_path,
            protector=protector,
            allow_insecure_loopback=allow_insecure_loopback,
            allow_insecure_private_lan=allow_insecure_private_lan,
            expected_hq_host=expected_hq_host,
            transport=transport,
        )
    except AlreadyEnrolled as refusal:
        return EnrolmentUiResult(state=EnrolmentUiState.REFUSED, detail=str(refusal))
    except (EnrolmentRefused, EnrolmentAmbiguous, EnrolmentUnavailable,
            EnrolmentUnreachable, InsecureBackendError, AgentError):
        return EnrolmentUiResult(state=EnrolmentUiState.REFUSED,
                                 detail=GENERIC_ENROLMENT_FAILURE)
    return EnrolmentUiResult(state=EnrolmentUiState.ENROLLED,
                             detail="this computer is now an enrolled Receiver Device",
                             outcome=outcome)


# ===========================================================================
# Screen 3 - Audio output
# ===========================================================================
class OutputKind(str, Enum):
    WIRED = "WIRED"
    BLUETOOTH = "BLUETOOTH"
    HDMI = "HDMI"
    OTHER = "OTHER"

    @property
    def suggestion_rank(self) -> int:
        # Lower sorts first. A suggestion only - the operator must still
        # confirm the exact output; nothing here selects one automatically.
        return {"WIRED": 0, "BLUETOOTH": 1, "HDMI": 2, "OTHER": 3}[self.value]


_HDMI_MARKERS = ("hdmi", "displayport", "display audio", "nvidia high definition",
                 "amd high definition", "intel(r) display")


def classify_output(device: OutputDevice) -> OutputKind:
    lowered = device.name.lower()
    if device.looks_wireless:
        return OutputKind.BLUETOOTH
    if any(marker in lowered for marker in _HDMI_MARKERS):
        return OutputKind.HDMI
    if "realtek" in lowered or "usb" in lowered:
        return OutputKind.WIRED
    return OutputKind.OTHER


@dataclass(frozen=True, slots=True)
class ClassifiedOutput:
    device: OutputDevice
    kind: OutputKind


def list_classified_outputs(*, devices=None, backend=None) -> "list[ClassifiedOutput]":
    """Enumerate and label. Never chooses - only orders a suggestion."""
    found = devices if devices is not None else list_output_devices(backend=backend)
    classified = [ClassifiedOutput(device=d, kind=classify_output(d)) for d in found]
    return sorted(classified, key=lambda c: (c.kind.suggestion_rank, c.device.index))


class TestSoundState(str, Enum):
    PLAYING = "PLAYING"
    PLAYED = "PLAYED"
    DEVICE_ERROR = "DEVICE_ERROR"
    PLAYBACK_ERROR = "PLAYBACK_ERROR"


@dataclass(frozen=True, slots=True)
class TestSoundResult:
    state: TestSoundState
    detail: str = ""


def play_test_tone(
    device: OutputDevice,
    *,
    duration_seconds: float = 2.0,
    frequency: int = 440,
    backend=None,
    popen=None,
) -> TestSoundResult:
    """Play an audible tone through exactly the selected device and nothing else.

    Reuses ``WindowsPcmSink`` (the same sink the real Receiver writes decoded
    audio to) rather than opening the device a second, different way. FFmpeg
    only generates the sine wave and resamples it to the device's own format -
    it never touches the audio endpoint itself, same division of duties as the
    Receiver's own playback path.

    Confirms only that the device accepted playback. This is NOT
    SPEAKER_VERIFIED - that requires acoustic evidence from LinkGuard, and
    remains the operator's own confirmation, asked for outside this function.
    """
    from tools.audio_receiver_pilot import SinkConfiguration, WindowsPcmSink

    configuration = SinkConfiguration(
        sink_mode="windows",
        device=device,
        sample_rate=device.default_samplerate,
        channels=min(max(device.max_output_channels, 1), 2),
    )
    try:
        sink = WindowsPcmSink(configuration, backend=backend)
        sink.open()
    except Exception as failure:  # noqa: BLE001 - reported, not swallowed
        return TestSoundResult(state=TestSoundState.DEVICE_ERROR, detail=str(failure))

    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sink.close()
        return TestSoundResult(state=TestSoundState.DEVICE_ERROR,
                               detail="ffmpeg was not found on PATH")

    command = [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration={duration_seconds}",
        "-ac", str(configuration.channels), "-ar", str(int(configuration.sample_rate)),
        "-f", "s16le", "pipe:1",
    ]
    spawn = popen or subprocess.Popen
    process = spawn(command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, **hidden_child_process_options())
    try:
        chunk_size = 4096
        wrote_any = False
        while True:
            chunk = process.stdout.read(chunk_size)
            if not chunk:
                break
            if sink.write(chunk):
                wrote_any = True
        process.wait(timeout=duration_seconds + 10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        sink.close()

    if sink.failed or not wrote_any:
        return TestSoundResult(state=TestSoundState.PLAYBACK_ERROR,
                               detail="the selected device did not accept the tone")
    return TestSoundResult(state=TestSoundState.PLAYED,
                           detail=f"{sink.frames_written} frame(s) written to {device.name}")


# ===========================================================================
# Screen 4 - Install, and CONNECTED evidence
# ===========================================================================
class InstallState(str, Enum):
    INSTALLING = "INSTALLING"
    WAITING_FOR_CONNECTION = "WAITING_FOR_CONNECTION"
    CONNECTED = "CONNECTED"
    TIMED_OUT = "TIMED_OUT"
    INSTALL_FAILED = "INSTALL_FAILED"


@dataclass(frozen=True, slots=True)
class InstallResult:
    state: InstallState
    detail: str
    log_path: "Path | None" = None


def run_receiver_installer(arguments: "list[str]", *, script_path: "Path | None" = None,
                           run=None) -> "subprocess.CompletedProcess":
    """Invoke the existing, verified Install-SpeakLinkStoreReceiver.ps1.

    Not reimplemented: that script already validates the package hash, the
    background executable's PE subsystem, quotes every interpolated
    -ArgumentList value and preserves an existing credential. This only calls
    it, hidden, exactly the way the HQ auto-start scripts are called from
    Python elsewhere in this project.
    """
    script = script_path or (REPOSITORY_ROOT / "scripts" / "Install-SpeakLinkStoreReceiver.ps1")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive",
              "-ExecutionPolicy", "Bypass", "-File", str(script)] + arguments
    executor = run or subprocess.run
    return executor(command, capture_output=True, text=True, timeout=300,
                    **hidden_child_process_options())


def wait_for_connected(
    *,
    status_path: "Path | None" = None,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
    sleep=time.sleep,
    clock=time.monotonic,
) -> InstallResult:
    """Poll the Receiver's own status file. A running process is not this proof.

    Bounded: an installer that waits forever for a Store with no network is an
    installer that never tells the operator anything is wrong.
    """
    path = status_path or receiver_status_path()
    deadline = clock() + timeout_seconds
    last_state = "UNKNOWN"
    while clock() < deadline:
        payload = read_status(path)
        last_state = payload.get("state", "UNKNOWN")
        if last_state == "CONNECTED":
            return InstallResult(state=InstallState.CONNECTED,
                                 detail="the backend accepted this Device credential",
                                 log_path=default_log_directory())
        sleep(poll_interval)
    return InstallResult(
        state=InstallState.TIMED_OUT,
        detail=f"the Receiver did not report CONNECTED within {timeout_seconds:.0f}s "
               f"(last known state: {last_state})",
        log_path=default_log_directory(),
    )


# ===========================================================================
# Rerun workflow - detecting what is already here
# ===========================================================================
@dataclass(frozen=True, slots=True)
class ExistingInstallation:
    is_installed: bool
    device_public_id: "str | None" = None
    store_id: "int | None" = None
    detail: str = ""


def detect_existing_installation(*, credential_path, protector) -> ExistingInstallation:
    """Never silently re-enrol: this is the check every rerun path is gated on."""
    status = describe_status(credential_path, protector=protector)
    if not status.get("enrolled"):
        return ExistingInstallation(is_installed=False, detail=status.get("note", ""))
    return ExistingInstallation(
        is_installed=True,
        device_public_id=status.get("device_public_id"),
        store_id=status.get("store_id"),
        detail="this computer is already enrolled as a Receiver Device",
    )
