"""The logic behind EchoCastStoreSetup.exe, with no GUI import in it.

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
from datetime import datetime, timezone
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

# Packaged resources are resolved through the shared resolver, never through
# REPOSITORY_ROOT. Frozen, REPOSITORY_ROOT is PyInstaller's private _internal
# directory - which is why the packaged wizard went looking for
# _internalrtifacts and _internal\scripts and an operator was told to
# hand-create them. See tools/resource_paths.
from tools import resource_paths
from tools import store_kit_settings_password as settings_password  # noqa: E402

from tools.audio_receiver_pilot import hidden_child_process_options  # noqa: E402
from tools.receiver_agent import (  # noqa: E402
    AgentError,
    AlreadyEnrolled,
    CONFIRMATION_WORD,
    EnrolmentAmbiguous,
    EnrolmentOutcome,
    EnrolmentRefused,
    EnrolmentUnavailable,
    EnrolmentUnreachable,
    InsecureBackendError,
    TerminalAuthentication,
    default_config_path,
    default_log_directory,
    describe_status,
    diagnose_report,
    enrol,
    load_config,
    normalise_backend_url,
    read_status,
    receiver_status_path,
    remove_local_credential,
    save_config,
    ReceiverConfig,
)
from tools.receiver_credential_store import (  # noqa: E402
    DeviceCredentialProtector,
    default_credential_path,
    load_credential,
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



# ===========================================================================
# Settings Password authorization
# ===========================================================================
#: Every MUTATING Store Kit action passes through this one gate.
#:
#: A dataclass rather than a bare boolean because it makes the proof travel
#: with the call: a function that requires a SettingsAuthorization cannot be
#: invoked by a caller that never verified anything, and the type is the
#: reminder. Scattering `if password == ...` beside each button is how one
#: button ends up unguarded, and the GUI is not the security boundary -
#: anything reaching these helpers directly is gated here too.
#:
#: It deliberately carries NO password. Once verification has happened the
#: plaintext has no further use, and an object holding it would be an object
#: that eventually gets logged or pickled into a diagnostics bundle.
@dataclass(frozen=True, slots=True)
class SettingsAuthorization:
    """Proof that somebody entered the correct Settings Password."""

    granted_at: datetime


def authorize_settings(password: str, *, verifier_path=None) -> SettingsAuthorization:
    """Verify the Settings Password and return proof, or raise.

    Raises SettingsPasswordNotSet when this Store has never had one - which is
    the upgrade case, and must NOT read as "allowed". The caller establishes
    one first.
    """
    settings_password.require_authorization(verifier_path, password)
    return SettingsAuthorization(granted_at=datetime.now(timezone.utc))


def _require_authorization(authorization, action: str) -> None:
    """Refuse a mutating action that carries no proof.

    Deliberately a hard refusal rather than a default of None-means-allowed:
    a new mutating helper that forgets to demand authorization should fail
    loudly the first time it is called, not quietly protect nothing.
    """
    if not isinstance(authorization, SettingsAuthorization):
        raise settings_password.SettingsPasswordRefused(
            f"{action} changes this Store's configuration and needs the "
            "Settings Password."
        )

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
            detail=f"{base} answered but not as an EchoCast HQ backend",
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
    authorization=None,
) -> EnrolmentUiResult:
    """Spend the code once. The Store sees only a generic failure; the reason
    stays out of this process's own hands, because the backend never sends it
    to an unauthenticated caller in the first place.

    PROTECTED. Enrolment writes this computer's Device identity, so it is a
    configuration mutation like any other. The fresh-setup wizard establishes
    the Settings Password BEFORE it reaches this step, which is why an
    unconditional gate does not trap a new machine: by the time enrolment
    runs, an authorization exists. Re-enrolling an already-installed Store
    goes through the same gate, which is the case this protects.

    Automatic Device AUTHENTICATION and reconnect are a different thing
    entirely and never come near here - they use the stored credential and are
    the Agent's business, not the Store Kit's.
    """
    _require_authorization(authorization, "Enrolling this computer")
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
    SPEAKER_VERIFIED - that requires acoustic evidence from EchoGuard, and
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

    # THE ABSOLUTE PACKAGED BINARY, never shutil.which("ffmpeg").
    #
    # This line used to be a PATH lookup, and on the second PC it produced
    # "ffmpeg was not found on PATH" on the Audio Output page - while the package
    # it was running from contained Receiver\ffmpeg.exe the whole time. A Store
    # desktop has no FFmpeg on PATH and must never be asked to acquire one.
    #
    # An absolute path also cannot be shadowed by some other FFmpeg that happens
    # to be installed, so a Store runs the version this package was tested with.
    try:
        ffmpeg = str(resource_paths.resolve_packaged_ffmpeg())
    except resource_paths.PackagedFfmpegMissing as failure:
        sink.close()
        return TestSoundResult(state=TestSoundState.DEVICE_ERROR, detail=str(failure))

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
# Locating the Receiver package - never a hardcoded placeholder path
# ===========================================================================
#: Files that must never be inside a Receiver package. A build step gone wrong
#: is the only way one of these gets in, and it must not ship to a Store.
_FORBIDDEN_PACKAGE_MARKERS = (
    ".env", ".db", ".sqlite", ".sqlite3", ".pem", ".key", "server.py",
)
_FORBIDDEN_TEXT_PATTERNS = (
    "echocast_rcv_v", "BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
    "ADMIN_PASSWORD", "JWT_SECRET",
)


def read_pe_subsystem(path) -> int:
    """The PE Optional Header Subsystem field, read from the file itself.

    2 = WINDOWS_GUI (no console), 3 = WINDOWS_CUI (console). Never inferred
    from a PyInstaller flag or a build log - the same rule the HQ runtime and
    the HQ package verifier already hold every other executable to.
    """
    with open(path, "rb") as handle:
        handle.seek(0x3C)
        pe_offset = int.from_bytes(handle.read(4), "little")
        handle.seek(pe_offset + 4 + 20 + 68)
        return int.from_bytes(handle.read(2), "little")


@dataclass(frozen=True, slots=True)
class PackageVerdict:
    package_path: Path
    ok: bool
    reasons: "tuple[str, ...]" = ()


def verify_receiver_package(package_path) -> PackageVerdict:
    """Check one candidate package. Read-only; never repairs, never trusts a
    filename alone.

    This is a second, independent check inside the Python that is about to
    install from the package - not a replacement for
    Test-EchoCastReceiverPackage.ps1, which remains the authoritative build-time
    gate. Defence in depth: a StoreSetup wizard about to hand a Store computer
    a Receiver should not simply trust that whatever sorts newest is safe.
    """
    package_path = Path(package_path)
    reasons: "list[str]" = []

    console_exe = package_path / "EchoCastReceiver.exe"
    background_exe = package_path / "EchoCastReceiverBackground.exe"
    ffmpeg_exe = package_path / "ffmpeg.exe"
    manifest_file = package_path / "manifest.json"
    sums_file = package_path / "SHA256SUMS.txt"

    for required in (console_exe, background_exe, ffmpeg_exe, manifest_file, sums_file):
        if not required.exists():
            reasons.append(f"missing {required.name}")

    if not manifest_file.exists() or not sums_file.exists():
        return PackageVerdict(package_path=package_path, ok=False, reasons=tuple(reasons))

    if background_exe.exists():
        try:
            if read_pe_subsystem(background_exe) != 2:
                reasons.append("EchoCastReceiverBackground.exe is not WINDOWS_GUI")
        except (OSError, ValueError):
            reasons.append("could not read EchoCastReceiverBackground.exe's PE header")
    if console_exe.exists():
        try:
            if read_pe_subsystem(console_exe) != 3:
                reasons.append("EchoCastReceiver.exe is not WINDOWS_CUI")
        except (OSError, ValueError):
            reasons.append("could not read EchoCastReceiver.exe's PE header")

    import hashlib

    for line in sums_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, relative = line.partition("  ")
        target = package_path / relative.replace("/", "\\")
        if not target.exists():
            reasons.append(f"{relative} is listed but missing")
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual.lower() != digest.strip().lower():
            reasons.append(f"{relative} does not match its recorded hash")

    for item in package_path.rglob("*"):
        if not item.is_file():
            continue
        lowered = item.name.lower()
        if any(lowered.endswith(marker) or lowered == marker
              for marker in _FORBIDDEN_PACKAGE_MARKERS):
            reasons.append(f"forbidden file present: {item.relative_to(package_path)}")

    return PackageVerdict(package_path=package_path, ok=(len(reasons) == 0),
                          reasons=tuple(reasons))


class NoVerifiedReceiverPackage(AgentError):
    """Every candidate under artifacts/ failed verification, or none exist."""


def locate_verified_receiver_package(artifacts_root=None) -> Path:
    """The newest EchoCastReceiver-* package under artifacts/ that verifies.

    Never a hardcoded 'artifacts/receiver-package' placeholder: package
    directories are versioned and timestamped
    (EchoCastReceiver-<version>-<commit>-<timestamp>), sort lexicographically
    newest-last for a fixed version/commit width, and are re-verified here
    rather than trusted by name alone.
    """
    if artifacts_root is not None:
        root = Path(artifacts_root)
    elif resource_paths.receiver_root().is_dir():
        # A built package ships the verified Receiver beside the executable as
        # Receiver/, so there is nothing to search and nothing to choose.
        return resource_paths.receiver_root()
    else:
        root = REPOSITORY_ROOT / "artifacts"
    if not root.exists():
        raise NoVerifiedReceiverPackage(
            f"there is no artifacts directory at {root}. Build a Receiver package "
            "with Build-EchoCastReceiver.ps1 first."
        )
    candidates = sorted(
        (item for item in root.iterdir() if item.is_dir() and item.name.startswith("EchoCastReceiver-")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        raise NoVerifiedReceiverPackage(
            f"no EchoCastReceiver-* package exists under {root}. Build one with "
            "Build-EchoCastReceiver.ps1 first."
        )

    failures = []
    for candidate in candidates:
        verdict = verify_receiver_package(candidate)
        if verdict.ok:
            return candidate
        failures.append(f"{candidate.name}: {'; '.join(verdict.reasons)}")

    raise NoVerifiedReceiverPackage(
        "no Receiver package under artifacts/ passed verification:\n" + "\n".join(failures)
    )


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
    """Invoke the existing, verified Install-EchoCastStoreReceiver.ps1.

    Not reimplemented: that script already validates the package hash, the
    background executable's PE subsystem, quotes every interpolated
    -ArgumentList value and preserves an existing credential. This only calls
    it, hidden, exactly the way the HQ auto-start scripts are called from
    Python elsewhere in this project.
    """
    script = script_path or resource_paths.script("Install-EchoCastStoreReceiver.ps1", required=False)
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


# ===========================================================================
# Rerun workflow: Status, Repair, Restart, Stop, Diagnostics, Uninstall
# ===========================================================================
DEFAULT_TASK_NAME = "EchoCast Store Receiver"
DEFAULT_INSTALL_ROOT_NAME = "receiver-app"


def _manage_task_script() -> Path:
    return resource_paths.script("Manage-EchoCastStoreReceiverTask.ps1", required=False)


def _run_powershell_script(script: Path, arguments: "list[str]", *, run=None,
                          timeout: float = 60.0) -> "subprocess.CompletedProcess":
    """Every task/installer action goes through a named, tested .ps1 file with
    real parameters - never a -Command string assembled from a task name or a
    path, which is the injection shape this project refuses everywhere else."""
    command = ["powershell.exe", "-NoProfile", "-NonInteractive",
              "-ExecutionPolicy", "Bypass", "-File", str(script)] + list(arguments)
    executor = run or subprocess.run
    return executor(command, capture_output=True, text=True, timeout=timeout,
                    **hidden_child_process_options())


@dataclass(frozen=True, slots=True)
class TaskState:
    registered: bool
    is_ours: "bool | None" = None
    state: "str | None" = None
    process_count: "int | None" = None
    detail: str = ""


def query_task_state(*, task_name: str = DEFAULT_TASK_NAME, run=None) -> TaskState:
    """Ask the task manager script. Never assumes a registered task is ours."""
    result = _run_powershell_script(_manage_task_script(),
                                    ["-TaskName", task_name, "-Action", "Status"], run=run)
    output = result.stdout
    if "NOT_REGISTERED" in output:
        return TaskState(registered=False, detail="no Store Receiver task is registered")
    if "NOT_OURS" in output:
        return TaskState(registered=True, is_ours=False,
                         detail=f"a task named '{task_name}' exists but is not ours")
    state = None
    process_count = None
    for line in output.splitlines():
        if line.startswith("STATE="):
            state = line.split("=", 1)[1].strip()
        elif line.startswith("PROCESS_COUNT="):
            try:
                process_count = int(line.split("=", 1)[1].strip())
            except ValueError:
                process_count = None
    return TaskState(registered=True, is_ours=True, state=state, process_count=process_count)


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Everything Status may show. No field here can hold a secret."""

    is_installed: bool
    device_public_id: "str | None" = None
    store_id: "int | None" = None
    backend_origin: "str | None" = None
    enrolled_at_utc: "str | None" = None
    audio_sink: "str | None" = None
    audio_output_device: "str | None" = None
    task: "TaskState | None" = None
    receiver_state: "str | None" = None
    receiver_detail: str = ""
    hq_reachable: "bool | None" = None


def get_status_snapshot(*, credential_path, protector, config_path=None,
                        status_path=None, task_name: str = DEFAULT_TASK_NAME,
                        connection_opener=None) -> StatusSnapshot:
    """Read-only. Never opens the credential's contents, never shows a task
    XML, never prints an Authorization header."""
    status = describe_status(credential_path, protector=protector)
    if not status.get("enrolled"):
        return StatusSnapshot(is_installed=False)

    config = load_config(config_path or default_config_path())
    receiver_status = read_status(status_path or receiver_status_path())
    task = query_task_state(task_name=task_name)

    hq_reachable = None
    backend_url = (config.backend_url if config else None) or status.get("backend_origin")
    if backend_url:
        connection = test_hq_connection(backend_url, opener=connection_opener)
        hq_reachable = connection.state in (ConnectionState.CONNECTED_TO_HQ,
                                            ConnectionState.PRIVATE_LAN_WARNING)

    return StatusSnapshot(
        is_installed=True,
        device_public_id=status.get("device_public_id"),
        store_id=status.get("store_id"),
        backend_origin=status.get("backend_origin"),
        enrolled_at_utc=status.get("enrolled_at_utc"),
        audio_sink=config.audio_sink if config else None,
        audio_output_device=config.audio_output_device if config else None,
        task=task,
        receiver_state=receiver_status.get("state"),
        receiver_detail=receiver_status.get("detail", ""),
        hq_reachable=hq_reachable,
    )


@dataclass(frozen=True, slots=True)
class RepairResult:
    ok: bool
    detail: str


def repair_installation(*, package_path, task_name: str = DEFAULT_TASK_NAME,
                        run=None, authorization=None) -> RepairResult:
    """Invoke the existing, tested Repair-EchoCastStoreReceiver.ps1. It already
    preserves the credential, config, selected output and logs - not
    reimplemented here."""
    _require_authorization(authorization, "Repairing the installation")
    script = resource_paths.script("Repair-EchoCastStoreReceiver.ps1", required=False)
    result = _run_powershell_script(script, ["-PackagePath", str(package_path),
                                             "-TaskName", task_name], run=run,
                                    timeout=180.0)
    if result.returncode != 0:
        return RepairResult(ok=False, detail=(result.stdout + result.stderr)[-2000:])
    return RepairResult(ok=True, detail="repair completed")


@dataclass(frozen=True, slots=True)
class TaskActionResult:
    state: InstallState
    detail: str


def restart_receiver(*, task_name: str = DEFAULT_TASK_NAME, timeout_seconds: float = 30.0,
                     run=None, sleep=time.sleep, clock=time.monotonic) -> TaskActionResult:
    """Stop, then start, the verified task - never a broad process kill - and
    wait for literal CONNECTED. A restarted process is not proof it worked."""
    # The primitive, not the protected entry point: a restart is one operation
    # and must not ask for the password twice. Restarting changes no
    # configuration, so it is not itself a protected mutation.
    stop_result = _stop_receiver_task(task_name=task_name, run=run)
    if not stop_result.ok:
        return TaskActionResult(state=InstallState.INSTALL_FAILED, detail=stop_result.detail)
    started = _run_powershell_script(_manage_task_script(),
                                     ["-TaskName", task_name, "-Action", "Start"], run=run)
    if started.returncode != 0:
        return TaskActionResult(state=InstallState.INSTALL_FAILED,
                                detail=(started.stdout + started.stderr)[-1000:])
    outcome = wait_for_connected(timeout_seconds=timeout_seconds, sleep=sleep, clock=clock)
    return TaskActionResult(state=outcome.state, detail=outcome.detail)


@dataclass(frozen=True, slots=True)
class StopResult:
    """Deliberately its own type, not a reuse of InstallState/TaskActionResult:
    'the Receiver was stopped' and 'the Receiver is CONNECTED' are two
    different facts, and folding one into the other's vocabulary is the exact
    shape of overclaim this project keeps finding and removing (CONFIG_OK
    counted as a start; a broadcast stop counted as the Receiver stopping)."""

    ok: bool
    detail: str


def _stop_receiver_task(*, task_name: str = DEFAULT_TASK_NAME, run=None) -> StopResult:
    """Stop the verified task. THE PRIMITIVE, WITH NO AUTHORIZATION IN IT.

    Private on purpose, and this shape replaced an earlier ``_internal=True``
    argument on the public stop_receiver. That flag was a real defect: an
    authorization check whose bypass is an ordinary keyword argument is not an
    authorization check, because anything able to call the function could pass
    it. Naming a separate private primitive means the public entry point has
    no bypass parameter to discover.

    It is still reachable by a determined caller inside this process - Python
    has no private functions - but it is no longer part of the callable API,
    and a test asserts stop_receiver itself refuses.
    """
    result = _run_powershell_script(_manage_task_script(),
                                    ["-TaskName", task_name, "-Action", "Stop"], run=run)
    if result.returncode != 0:
        return StopResult(ok=False, detail=(result.stdout + result.stderr).strip()[-1000:])
    return StopResult(ok=("STOPPED" in result.stdout), detail=result.stdout.strip())


def stop_receiver(*, task_name: str = DEFAULT_TASK_NAME, run=None,
                  authorization=None) -> StopResult:
    """Stop only the verified task. Never touches the credential or the task
    registration itself.

    Protected because a stopped Receiver is a silent Store until somebody
    starts it again - the outcome this product exists to prevent.
    """
    _require_authorization(authorization, "Stopping the Receiver")
    return _stop_receiver_task(task_name=task_name, run=run)


def change_audio_output(*, device, config_path=None, task_name: str = DEFAULT_TASK_NAME,
                        run=None, timeout_seconds: float = 30.0,
                        authorization=None) -> TaskActionResult:
    """Save the newly confirmed selector, then restart so it takes effect.
    Never saves before Test Sound + the heard confirmation - that gate lives
    in the GUI, which only calls this after both have happened."""
    # Verified BEFORE anything is read or written, so a refusal leaves the
    # last-known-good configuration byte-identical.
    _require_authorization(authorization, "Changing the audio output device")
    path = Path(config_path) if config_path is not None else default_config_path()
    existing = load_config(path) or ReceiverConfig()
    updated = ReceiverConfig(
        backend_url=existing.backend_url,
        expected_hq_host=existing.expected_hq_host,
        allow_insecure_private_lan=existing.allow_insecure_private_lan,
        audio_sink="windows",
        audio_output_device=device.verified_selector,
        log_directory=existing.log_directory,
        installed_version=existing.installed_version,
        source_commit=existing.source_commit,
    )
    save_config(path, updated)
    return restart_receiver(task_name=task_name, timeout_seconds=timeout_seconds, run=run)


def build_redacted_diagnostics(*, credential_path, config_path=None, devices=None,
                              backend=None, task_name: str = DEFAULT_TASK_NAME) -> str:
    """Everything Redacted Diagnostics shows. Reuses diagnose_report()
    verbatim and appends task/HQ facts - never a second, parallel redaction
    implementation that could disagree with the first."""
    report = diagnose_report(config_path=config_path or default_config_path(),
                             credential_path=credential_path, devices=devices, backend=backend)
    task = query_task_state(task_name=task_name)
    return report + (
        f"\n\nscheduled task    : {task_name}\n"
        f"  registered      : {task.registered}\n"
        f"  owned by us     : {task.is_ours}\n"
        f"  state           : {task.state or '<unknown>'}\n"
        f"  process count   : {task.process_count if task.process_count is not None else '<unknown>'}\n"
    )


_FORBIDDEN_EXPORT_PATTERNS = ("echocast_rcv_v", "Bearer ", "BEGIN PRIVATE KEY",
                              "BEGIN RSA PRIVATE KEY")


class UnsafeDiagnosticsExport(AgentError):
    """The generated text matched a forbidden pattern. Refused, not filtered -
    a report worth trusting is one that never had the secret in it, not one
    where a regex tried to catch it after the fact."""


def export_diagnostics(text: str, *, export_directory=None, now=None) -> Path:
    from datetime import datetime, timezone

    for pattern in _FORBIDDEN_EXPORT_PATTERNS:
        if pattern in text:
            raise UnsafeDiagnosticsExport(
                f"the diagnostics text matched a forbidden pattern ({pattern!r}) and "
                "was not written to disk."
            )
    directory = Path(export_directory) if export_directory is not None else default_log_directory()
    directory.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(timezone.utc)
    target = directory / f"diagnostics-{moment.strftime('%Y%m%d-%H%M%S')}.txt"
    target.write_text(text, encoding="utf-8")
    return target


class UntrustedLogPath(AgentError):
    """Refuses to open anything but the one known log directory."""


def open_log_folder(path=None, *, opener=None) -> Path:
    """Open exactly the known Receiver log directory. Never a path built from
    user text - there is no user text here at all, only the fixed constant."""
    target = Path(path) if path is not None else default_log_directory()
    expected = default_log_directory()
    if target.resolve() != expected.resolve():
        raise UntrustedLogPath(f"refusing to open {target}: it is not the known log directory")
    target.mkdir(parents=True, exist_ok=True)
    if opener is not None:
        opener(str(target))
    else:
        import os

        os.startfile(str(target))  # noqa: S606 - a fixed, known directory only
    return target


@dataclass(frozen=True, slots=True)
class UninstallResult:
    ok: bool
    detail: str


def uninstall_receiver(*, task_name: str = DEFAULT_TASK_NAME, run=None,
                      authorization=None) -> UninstallResult:
    """Invoke the existing, tested Uninstall-EchoCastStoreReceiver.ps1. It
    already preserves the credential, config and logs by default and never
    revokes the HQ Device - not reimplemented here."""
    _require_authorization(authorization, "Uninstalling the Receiver")
    script = resource_paths.script("Uninstall-EchoCastStoreReceiver.ps1", required=False)
    result = _run_powershell_script(script, ["-TaskName", task_name], run=run, timeout=120.0)
    if result.returncode != 0:
        return UninstallResult(ok=False, detail=(result.stdout + result.stderr)[-1500:])
    return UninstallResult(ok=True, detail="uninstalled; credential, config and logs preserved")


def replace_device_identity(*, credential_path, confirmation_word: str,
                            authorization=None) -> bool:
    """Delete the local credential ONLY on an exact typed confirmation. Never
    revokes the HQ Device - an administrator does that separately, and the
    caller is told so before this runs."""
    # Authorization first, then the typed confirmation. Both are required:
    # the password says WHO, the typed word says THEY MEANT IT.
    _require_authorization(authorization, "Replacing the Device identity")
    if confirmation_word.strip().lower() != CONFIRMATION_WORD:
        return False
    return remove_local_credential(credential_path, confirm=lambda _prompt: CONFIRMATION_WORD)


# ===========================================================================
# Asking the CURRENT HQ whether it still knows this Device
# ===========================================================================
class DeviceLookupUnavailable(Exception):
    """HQ could not be asked. NEVER the same as "HQ said no".

    The caller must map this to HQ_UNREACHABLE, not to a stale credential:
    offering to delete an identity because a network was down is worse than
    showing nothing at all.
    """


def lookup_device_at_hq(public_id, *, credential_path=None, protector=None,
                        backend_url=None, connect=None, timeout=8.0):
    """Return a Device record from the current HQ, or None if HQ rejects it.

    THERE IS NO UNAUTHENTICATED "IS MY DEVICE VALID" ROUTE, and inventing one
    would be an enumeration oracle: every /receiver-devices route requires a
    signed-in HQ user, which a Store PC does not have and should never have.

    So this uses the flow that already exists and is already the authority - the
    Receiver's own WebSocket handshake, presenting the sealed Device credential
    in an Authorization header exactly as receiver_agent does. HQ accepting that
    connection IS the definition of "this Device is still valid here".

    Three outcomes, kept distinct on purpose:
      * a record   - HQ accepted the credential;
      * None       - HQ answered and REFUSED the credential (stale or revoked);
      * raises     - HQ could not be reached at all.
    """
    from tools.receiver_agent import receiver_websocket_url

    path = Path(credential_path) if credential_path else default_credential_path()
    guard = protector or DeviceCredentialProtector()
    try:
        record = load_credential(path, protector=guard)
    except Exception as failure:
        raise DeviceLookupUnavailable(
            f"the local credential could not be opened ({failure.__class__.__name__})"
        ) from None

    secret = getattr(record, "credential", None) or getattr(record, "token", None)
    if not secret:
        raise DeviceLookupUnavailable("the local credential carries no usable secret")

    base = backend_url or getattr(record, "backend_url", None)
    if not base:
        raise DeviceLookupUnavailable("no HQ address is recorded for this computer")

    opener = connect or _default_ws_connect
    try:
        accepted, detail = opener(receiver_websocket_url(base), secret, timeout)
    except DeviceLookupUnavailable:
        raise
    except Exception as failure:
        raise DeviceLookupUnavailable(
            f"HQ could not be reached ({failure.__class__.__name__})") from None

    if not accepted:
        # HQ answered and said no. That is a real verdict, not a network problem.
        return None
    return {
        "store_id": detail.get("store_id"),
        "store_name": detail.get("store_name"),
        "store_code": detail.get("store_code"),
        "zone": detail.get("zone"),
        "device_name": detail.get("device_name"),
        "store_archived": bool(detail.get("store_archived")),
    }


def _default_ws_connect(url, credential, timeout):
    """One short handshake. The credential travels in a header, never in the URL.

    A URL is written to proxy logs, browser history and process lists; this
    project refuses credential-in-URL everywhere and this is not an exception.
    """
    import json as _json

    try:
        from websockets.sync.client import connect as ws_connect
    except Exception as failure:
        raise DeviceLookupUnavailable(
            f"the WebSocket client is unavailable ({failure.__class__.__name__})"
        ) from None

    try:
        with ws_connect(url, additional_headers={"Authorization": f"Bearer {credential}"},
                        open_timeout=timeout, close_timeout=2) as socket:
            try:
                first = socket.recv(timeout=timeout)
            except Exception:
                # Connected and said nothing yet. The handshake succeeding is
                # already HQ accepting the credential.
                return True, {}
            try:
                payload = _json.loads(first)
            except Exception:
                return True, {}
            return True, payload if isinstance(payload, dict) else {}
    except Exception as failure:
        name = failure.__class__.__name__
        text = str(failure)
        # 4401/4403 and an HTTP 401/403 on the upgrade are HQ REFUSING the
        # credential. Anything else - DNS, refused connection, timeout - means
        # HQ was never reached and must not be reported as a stale identity.
        rejected = ("401" in text or "403" in text or "4401" in text or "4403" in text
                    or "InvalidStatus" in name)
        if rejected:
            return False, {}
        raise DeviceLookupUnavailable(f"HQ could not be reached ({name})") from None
