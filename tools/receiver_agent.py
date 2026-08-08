"""The production Windows Receiver Agent: enrol once, then run for months.

``audio_receiver_pilot.py`` proved a Store can receive and play audio, and it is
staying exactly as it is - it is the tool that produced every piece of amplifier
evidence, and it is still the right thing for a hardware test bench. But it is a
pilot: an operator exports a shared per-Store token and starts it by hand.

This is the thing that gets installed on 44 tills, so different questions matter.

**Neither secret may reach a command line.** ``tasklist``, Windows event logs,
crash dumps and any screen-share expose process arguments to whoever is standing
there. So the enrolment code is typed into a hidden prompt or piped on stdin, and
the Device credential is read from a DPAPI-sealed file and appears in exactly one
place: an ``Authorization: Bearer`` header. Not in a URL, not in a log line, not
in an environment variable a child FFmpeg process would inherit.

**A revoked Device must stop.** An Agent that reconnects forever after being
revoked is a Store that cannot be taken off the air, and 44 of them retrying in
a tight loop is a denial of service HQ inflicted on itself. The backend refuses
revoked, disabled and simply-wrong credentials identically - deliberately, so a
caller cannot map out which Stores have Devices - which means the Agent cannot
tell them apart either. It does not need to: none of the three will ever start
working again on their own, so all three are terminal. Anything else, including
every ordinary network fault, is retried with a bounded, jittered backoff.

**CONNECTED is not READY, and READY is not PLAYBACK_CONFIRMED.** The whole status
model rests on that. Rather than reimplement it, ``DeviceReceiverSession``
subclasses the pilot and overrides only where the credential comes from; the
prepare, audio, playback and stop handling are the pilot's own methods, running
unchanged. A test compares them by identity, so the amplifier evidence path is
preserved by being *the same code* rather than a careful copy of it.

SPEAKER_VERIFIED is never claimed here. It is LinkGuard's to give.

Nothing in this file imports from ``backend/``. It runs on a till.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.audio_receiver_pilot import (  # noqa: E402
    AudioReceiverPilot,
    AudioReceiverError,
    OUTPUT_SAMPLE_RATE,
    SINK_MODE_NULL,
    SINK_MODE_WINDOWS,
    SUPPORTED_SINK_MODES,
    SinkConfiguration,
    SinkConfigurationError,
    resolve_sink_configuration,
)
from tools.windows_audio_devices import (  # noqa: E402
    AudioDeviceError,
    format_device_table,
    list_output_devices,
    resolve_output_device,
)
from tools import windows_endpoint_restore  # noqa: E402
from tools.receiver_credential_store import (  # noqa: E402
    CredentialStoreError,
    DeviceCredentialProtector,
    delete_credential,
    default_credential_path,
    load_credential,
    replace_credential,
    save_credential,
)


#: What this Receiver reports to HQ and prints for --version.
#:
#: The packaging script takes a -Version and names the package directory with
#: it, but nothing ever wrote it here - so every Receiver ever built announced
#: 1.0.0 whatever its package said, and HQ recorded that as each device's
#: software version. An operator reading the fleet could not tell 1.4.0 from
#: the first build. Kept in step with the packaged version by hand, which is at
#: least a step that exists.
AGENT_VERSION = "1.5.0"
ENROLMENT_PATH = "/api/receiver-devices/enroll"
RECEIVER_WEBSOCKET_PATH = "/api/ws/receiver"

REQUEST_TIMEOUT_SECONDS = 20.0
MAX_CODE_LENGTH = 200

#: 4401 is ``RECEIVER_AUTH_FAILURE_CODE`` in ``backend/server.py``. It is the one
#: close code that means "this credential will never work"; every other close is
#: something that might succeed on the next attempt.
TERMINAL_CLOSE_CODES = frozenset({4401})

CONFIRMATION_WORD = "remove"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_AUTHENTICATION = 2
EXIT_NETWORK = 3
#: Distinct on purpose. Task Scheduler restarts a task that fails; if a duplicate
#: launch reported an ordinary failure, the restart policy would keep relaunching
#: something that is already working perfectly.
EXIT_ALREADY_RUNNING = 4

#: Ten files of one megabyte. Enough history to explain a Store that went quiet
#: overnight, small enough that a Store desktop cannot fill its disk with logs.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 9
LOGGER_NAME = "speaklink.receiver"


def packaged_ffmpeg_directory() -> Path | None:
    """The directory holding the FFmpeg shipped beside this executable.

    A Store desktop has no FFmpeg on PATH and no reason to acquire one, so the
    package carries its own. Resolved relative to the executable rather than the
    working directory: a Task Scheduler job, a shortcut and an operator typing
    the path all start somewhere different, and only the executable's own
    location is the same in all three.

    Returns None when running from source, where PATH is the right answer.
    """
    if not getattr(sys, "frozen", False):
        return None
    beside = Path(sys.executable).resolve().parent
    return beside if (beside / "ffmpeg.exe").is_file() else None


def prefer_packaged_ffmpeg() -> str | None:
    """Put the packaged FFmpeg ahead of anything on PATH, for this process only.

    ``audio_receiver_pilot`` finds FFmpeg with ``shutil.which("ffmpeg")``, and it
    is the hardware-proven file that produced every amplifier result - not
    something to edit for a packaging concern. Prepending to this process's PATH
    makes ``which`` return the packaged binary and leaves that file untouched.

    A Store PC that happens to have some other FFmpeg installed then still runs
    the version this package was tested with, which is the point.
    """
    directory = packaged_ffmpeg_directory()
    if directory is None:
        return None
    os.environ["PATH"] = f"{directory}{os.pathsep}" + os.environ.get("PATH", "")
    return str(directory / "ffmpeg.exe")


class AgentError(RuntimeError):
    """Controlled, secret-free Agent failure."""


class InsecureBackendError(AgentError):
    """The backend URL would send a credential over plain HTTP."""


class EnrolmentRefused(AgentError):
    """HQ said no. The code was wrong, expired, already used, or rate limited."""


class EnrolmentUnavailable(AgentError):
    """The server is not ready - no key container, or no phase-one schema."""


class EnrolmentUnreachable(AgentError):
    """HQ could not be reached at all. Nothing was issued, so this is safe."""


class EnrolmentAmbiguous(AgentError):
    """A Device was created at HQ but this computer could not keep its credential.

    The dangerous case, and the reason it has its own class: the code is spent
    and a Device exists that nothing can authenticate as. Resubmitting the code
    cannot help - it would be an attacker's move, not a recovery. An
    administrator has to revoke the orphan and issue a new code.
    """


class AlreadyEnrolled(AgentError):
    """This computer already holds a credential, and it will not be discarded."""


class TerminalAuthentication(AgentError):
    """The server refused this credential. Retrying will not change that."""


class AlreadyRunning(AgentError):
    """Another Agent already holds this credential. Not a failure."""


# ===========================================================================
# One Agent per credential
# ===========================================================================
class InstanceLock:
    """A single-instance guard scoped to one credential file.

    Two Agents on one credential is not a theoretical concern once Task
    Scheduler is involved: a task that starts at logon, plus an operator who
    double-clicks the executable to check it works, is two Receivers for one
    Store. They compete for the same socket, the backend keeps replacing one
    connection with the other, and the Store flickers between them.

    Scoped to the credential path rather than to the machine. A global mutex
    would stop a second Windows account on the same computer from running its
    own Receiver - a restriction invented by the lock, not asked for by anyone.

    Staleness is handled by the operating system rather than by us. A PID file
    has to answer "is process 4812 still the Agent, or is it a text editor that
    was given the same number after a reboot?", and answers it wrong eventually.
    An advisory byte-range lock on an open handle is released by Windows when
    the holding process dies, however it dies - crash, power cut, or Task
    Scheduler ending the task - so a Store can always start again.
    """

    def __init__(self, credential_path) -> None:
        resolved = Path(credential_path).expanduser()
        try:
            resolved = resolved.resolve()
        except OSError:  # pragma: no cover - a path we cannot resolve still locks
            resolved = resolved.absolute()
        # The path itself never appears in the lock file name. It can contain a
        # Windows username, and lock files are world-readable.
        digest = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:32]
        self.credential_path = resolved
        self.lock_path = _agent_state_directory() / "locks" / f"receiver-{digest}.lock"
        self._handle = None

    def acquire(self) -> "InstanceLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Opened r+ where possible so an existing lock file is not truncated
        # while another Agent holds a lock on it.
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            _lock_exclusive(handle)
        except OSError:
            handle.close()
            raise AlreadyRunning(
                "Another SpeakLink Receiver is already running for this Device on "
                "this computer. Only one may run at a time. Nothing was changed."
            ) from None
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:  # pragma: no cover - closing releases it regardless
            pass
        finally:
            handle.close()

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *_exception) -> None:
        self.release()


def _lock_exclusive(handle) -> None:
    """Take a non-blocking exclusive lock on the first byte of ``handle``."""
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - the Receiver ships for Windows
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _agent_state_directory() -> Path:
    """Per-user, never shared. ``%LOCALAPPDATA%`` is not roamed, which is right
    for a lock that describes one physical computer."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "SpeakLink" / "receiver"


# ===========================================================================
# Logging, with nothing secret in it
# ===========================================================================
#: A sealed Device credential, whole. This is the one string that must never be
#: written down, so it is matched first and matched greedily.
_CREDENTIAL_PATTERN = re.compile(r"speaklink_rcv_v1\.[A-Za-z0-9._\-]+")
#: An enrolment code. Single-use, but still a credential until it is used.
_ENROLMENT_CODE_PATTERN = re.compile(r"\bECHO(?:-[A-Z0-9]{4}){2,}\b")
#: Whatever follows a bearer scheme, whatever shape it happens to be.
_AUTHORIZATION_PATTERN = re.compile(r"(?i)\b(Bearer|Basic)\s+\S+")
#: Every query string on a URL. Tickets are single-use and JWTs are not, and a
#: log line is the wrong place to be deciding which one this is.
_URL_QUERY_PATTERN = re.compile(r"(?i)\b(wss?|https?)://\S*?\?\S+")
#: A long unbroken run of credential-shaped characters that none of the above
#: recognised. Deliberately long: short identifiers like a Device public id or a
#: Store code are operational detail worth keeping, and a redactor that eats
#: those is a redactor an operator turns off.
_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9+/_\-]{40,}={0,2}\b")

_REDACTIONS = (
    (_CREDENTIAL_PATTERN, "<REDACTED credential>"),
    (_AUTHORIZATION_PATTERN, r"\1 <REDACTED>"),
    (_URL_QUERY_PATTERN, lambda match: match.group(0).split("?", 1)[0] + "?<REDACTED>"),
    (_ENROLMENT_CODE_PATTERN, "<REDACTED enrolment code>"),
    (_LONG_TOKEN_PATTERN, "<REDACTED>"),
)


def redact(message: str) -> str:
    """Remove anything credential-shaped from a line about to be written down.

    Applied to the formatted record rather than trusted to each call site. Every
    logging call in this file is careful; the one that will not be is the one
    somebody adds in a hurry while a Store is down at 6am.
    """
    text = str(message)
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # A traceback can carry an argument value into the log.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def default_log_directory() -> Path:
    return _agent_state_directory() / "logs"


# ===========================================================================
# A status file any process here can be read through, not just watched
# ===========================================================================
def write_status(path, state, *, detail: str = "") -> None:
    """Replace the status file whole, and never let a secret into it.

    Originally hq_runtime's own helper, moved here so the Receiver Agent can
    write the same shape of evidence: a windowed or backgrounded process has
    nowhere to print, so whatever it is doing has to be readable from disk.
    Replaced rather than appended, because a status file is a current fact,
    not a history - the rotating log is the history. Written through a
    temporary file and renamed so a reader never sees half a document.
    """
    from enum import Enum

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state.value if isinstance(state, Enum) else str(state),
        "detail": redact(detail),
        "pid": os.getpid(),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def read_status(path) -> dict:
    """Unreadable is UNKNOWN, not FAIL. A checker that scores 'I could not
    look' as 'it is broken' has already misled somebody in this repository."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def receiver_status_path(state_directory: "Path | None" = None) -> Path:
    """Where this computer's Receiver writes what it is doing right now.

    Distinct from the rotating log: the log is history, this is 'what is true
    at this instant', which is exactly what an installer waiting for a real
    connection needs to read - not a process that merely exists.
    """
    base = Path(state_directory) if state_directory is not None else _agent_state_directory()
    return base / "receiver-status.json"


def configure_logging(
    directory=None,
    *,
    max_bytes: int = LOG_MAX_BYTES,
    backup_count: int = LOG_BACKUP_COUNT,
) -> logging.Logger:
    """Rotating, bounded, UTC, and redacted.

    Bounded because a Store desktop that fills its own disk with Receiver logs
    stops working for a reason nobody will guess. UTC because 44 Stores that
    disagree about what time it is cannot be read side by side.
    """
    target = Path(directory) if directory is not None else default_log_directory()
    target.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(LOGGER_NAME)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()

    handler = logging.handlers.RotatingFileHandler(
        target / "receiver.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    formatter = logging.Formatter(
        "%(asctime)s UTC %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    handler.addFilter(_RedactingFilter())

    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# ===========================================================================
# The settings a Store keeps between restarts
# ===========================================================================
CONFIG_FILENAME = "config.json"

#: Everything the installer records so nobody has to retype it after a reboot,
#: a repair or an upgrade. Every field here is deliberately NON-SECRET: a
#: backend address, a device selector, a folder. The Device credential stays
#: sealed by DPAPI in its own file, because a config file is a plain file on a
#: shop counter and anything written in it is written in the clear.
_CONFIG_FIELDS = (
    "backend_url",
    "expected_hq_host",
    "allow_insecure_private_lan",
    "audio_sink",
    "audio_output_device",
    "windows_endpoint_id",
    "log_directory",
    "installed_version",
    "source_commit",
)

#: Keys that must never appear in a config file. If one does, the file is
#: refused rather than loaded: an installer bug - or somebody being helpful -
#: must not be able to turn this into a plaintext credential store.
_FORBIDDEN_CONFIG_KEYS = frozenset({
    "credential", "password", "code", "token", "secret",
    "enrolment_code", "enrollment_code", "authorization",
})


@dataclass
class ReceiverConfig:
    """Non-secret settings, read at every start."""

    backend_url: str | None = None
    expected_hq_host: str | None = None
    allow_insecure_private_lan: bool = False
    audio_sink: str | None = None
    audio_output_device: str | None = None
    #: The STABLE Windows Core Audio endpoint id for the device above, captured
    #: when the technician selected it. Master volume/mute is only ever applied
    #: to this id - never to a PortAudio index, which renumbers when hardware
    #: changes and would point SpeakLink at the wrong output. None means an
    #: installation that predates master control: playback still works, and HQ
    #: reports the control as unavailable until the output is re-selected.
    windows_endpoint_id: str | None = None
    log_directory: str | None = None
    installed_version: str | None = None
    source_commit: str | None = None


def default_config_path() -> Path:
    return _agent_state_directory() / CONFIG_FILENAME


def load_config(path) -> "ReceiverConfig | None":
    """Read the saved settings, or ``None`` if this machine has none.

    A missing file is not an error: a technician must still be able to run the
    Receiver by hand with everything on the command line, exactly as the pilot
    did. A *corrupt* file is an error, because half-applied settings are how a
    Store ends up pointed at the right HQ with the wrong speaker.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as failure:
        raise AgentError(
            f"the Receiver configuration at {path} could not be read "
            f"({failure.__class__.__name__}). Repair the installation."
        ) from None
    if not isinstance(raw, dict):
        raise AgentError(f"the Receiver configuration at {path} is not a set of settings.")

    present = {str(key).lower() for key in raw}
    smuggled = sorted(present & _FORBIDDEN_CONFIG_KEYS)
    if smuggled:
        # The offending value is deliberately not echoed.
        raise AgentError(
            f"the Receiver configuration at {path} contains {', '.join(smuggled)}, "
            "which must never be stored in a plain file. Delete it and re-run the "
            "installer; the Device credential belongs in the DPAPI-sealed store."
        )

    # Unknown keys are ignored on purpose: a newer installer must not stop an
    # older executable from starting before it has been upgraded.
    return ReceiverConfig(**{
        field: raw[field] for field in _CONFIG_FIELDS if field in raw
    })


def save_config(path, config: "ReceiverConfig") -> None:
    """Write the settings, replacing any earlier file in one step.

    Written to a temporary file and moved into place, so a power cut leaves
    either the old settings or the new ones - never a half-written file that
    stops the Store starting at all.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {field: getattr(config, field) for field in _CONFIG_FIELDS}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def merge_config_into_arguments(arguments, config: "ReceiverConfig | None") -> None:
    """Fill in whatever the command line left out. The command line wins.

    A technician debugging on site overrides with an option, and their override
    is not remembered - it lasts exactly as long as that one run.
    """
    if config is None:
        return
    for field in ("backend_url", "expected_hq_host", "audio_sink",
                  "audio_output_device", "log_directory"):
        if getattr(arguments, field, None) in (None, ""):
            value = getattr(config, field)
            if value:
                setattr(arguments, field, value)
    if not getattr(arguments, "allow_insecure_private_lan", False):
        arguments.allow_insecure_private_lan = bool(config.allow_insecure_private_lan)


# ===========================================================================
# Which audio output, and saying so out loud
# ===========================================================================
#: The Receiver has always defaulted to a sink that decodes audio and throws it
#: away. That is right for a software smoke test and wrong on a Store desktop,
#: and nothing said which one you were getting: a Store reported
#: PLAYBACK_CONFIRMED on the dashboard while its speakers stayed silent.
#:
#: PLAYBACK_CONFIRMED is emitted when ``decoder.wait_for_decode`` returns true.
#: In windows mode that only happens after frames have been written to the
#: output stream, so it genuinely means "handed to the device". In null mode it
#: means "FFmpeg produced PCM" and nothing more. Both are honest; neither was
#: visible. These helpers make the mode explicit on the command line and in the
#: console, and give a Store PC a way to find a device selector at all.


def resolve_agent_sink(*, sink_mode, selector, devices=None, backend=None) -> SinkConfiguration:
    """Resolve the sink from explicit arguments rather than the environment.

    Same rules as ``resolve_sink_configuration``: never picks a device, never
    falls back to the Windows default, never guesses from a partial name. The
    difference is only where the answer comes from, so an operator can see the
    choice on the command line instead of in a variable somebody exported.
    """
    mode = (sink_mode or SINK_MODE_NULL).strip().lower()
    if mode not in SUPPORTED_SINK_MODES:
        raise SinkConfigurationError(
            f"{mode!r} is not a supported audio sink; use one of {SUPPORTED_SINK_MODES}"
        )

    if mode == SINK_MODE_NULL:
        # A leftover device selector is ignored on purpose, so a stale value can
        # never produce an unexpected sound.
        return SinkConfiguration(sink_mode=SINK_MODE_NULL, device=None)

    chosen = (selector or "").strip()
    if not chosen:
        raise SinkConfigurationError(
            "the windows audio sink needs exactly one output device. Run "
            "'SpeakLinkReceiver.exe list-audio-devices' and copy a verified "
            "'index:N@Name' selector. The Receiver will not choose one for you "
            "and will never use the Windows default device."
        )

    try:
        device = resolve_output_device(chosen, devices=devices, backend=backend)
    except AudioDeviceError as error:
        raise SinkConfigurationError(str(error)) from None

    # Adopt the device's own advertised format. Hardcoding 48 kHz mono made the
    # open fail on strict host APIs such as WDM-KS, where the same endpoint may
    # advertise 44.1 kHz stereo. FFmpeg resamples to whatever the device wants.
    sample_rate = device.default_samplerate or OUTPUT_SAMPLE_RATE
    channels = min(max(device.max_output_channels, 1), 2)
    return SinkConfiguration(
        sink_mode=SINK_MODE_WINDOWS,
        device=device,
        sample_rate=sample_rate,
        channels=channels,
    )


def describe_sink(configuration: SinkConfiguration) -> "list[str]":
    """What to print at start-up so nobody has to guess. Carries no secret."""
    if configuration.sink_mode != SINK_MODE_WINDOWS or configuration.device is None:
        return [
            "AUDIO: null sink. Decoded audio is DISCARDED and no sound will be",
            "       played on this computer. PLAYBACK_CONFIRMED will still be",
            "       reported to HQ, because in this mode it means only that",
            "       FFmpeg decoded the audio - not that anything was heard.",
            "       For a real speaker test, run list-audio-devices and pass",
            "       --audio-sink windows --audio-output-device index:N@Name",
        ]
    device = configuration.device
    return [
        f"AUDIO: windows sink, device index {device.index}",
        f"       name    : {device.name}",
        f"       host API: {device.host_api}",
        f"       format  : {int(configuration.sample_rate)} Hz, "
        f"{configuration.channels} channel(s)",
        "       PLAYBACK_CONFIRMED in this mode means decoded audio was",
        "       accepted by that device. It is still not proof anybody heard it.",
    ]


def diagnose_report(*, config_path, credential_path, devices=None, backend=None) -> str:
    """Everything a technician needs, over the phone, showing nothing secret.

    Read-only. It opens no audio device, starts nothing, and never reads the
    credential - only whether the file is there.
    """
    config_path = Path(config_path)
    credential_path = Path(credential_path)
    lines = [
        "SpeakLink Receiver diagnostics",
        "=============================",
        f"agent version    : {AGENT_VERSION}",
        f"executable       : {Path(sys.executable).name if getattr(sys, 'frozen', False) else 'running from source'}",
        f"windows user     : {os.environ.get('USERNAME', '<unknown>')}",
        "",
        f"config file      : {config_path}",
    ]

    try:
        config = load_config(config_path)
    except AgentError as failure:
        config = None
        lines.append(f"config status    : UNREADABLE - {failure}")
    else:
        lines.append(f"config status    : {'present' if config else 'ABSENT (never installed?)'}")

    if config is not None:
        lines += [
            f"  backend        : {config.backend_url or '<not set>'}",
            f"  expected host  : {config.expected_hq_host or '<not set>'}",
            f"  plain HTTP     : {'allowed (pilot only)' if config.allow_insecure_private_lan else 'no'}",
            f"  audio sink     : {config.audio_sink or 'null (audio is DISCARDED)'}",
            f"  audio device   : {config.audio_output_device or '<not set>'}",
            f"  master endpoint: {config.windows_endpoint_id or '<not set - re-select the output>'}",
            f"  log directory  : {config.log_directory or default_log_directory()}",
            f"  installed ver  : {config.installed_version or '<not recorded>'}",
            f"  source commit  : {config.source_commit or '<not recorded>'}",
        ]

    # Windows master-volume control, read-only. Nothing here changes a level;
    # it exists so a technician can see whether SpeakLink can drive this
    # computer's output at all, and so the packaged build proves it can reach
    # Core Audio - comtypes generates its COM wrappers at runtime and can fail
    # when frozen, which no amount of "the module is bundled" would catch.
    lines += ["", "windows master control:"]
    try:
        from tools import windows_endpoint_volume

        endpoints = windows_endpoint_volume.list_endpoints()
        lines.append(f"  core audio     : reachable ({len(endpoints)} active endpoint(s))")
        configured = config.windows_endpoint_id if config else None
        if configured:
            try:
                state = windows_endpoint_volume.read_state(configured)
                lines.append(f"  master volume  : {state.volume_percent}%")
                lines.append(f"  master mute    : {'ON' if state.muted else 'off'}")
            except Exception as failure:
                lines.append(f"  master volume  : UNAVAILABLE - {failure}")
        else:
            lines.append("  master volume  : not configured; re-select the audio "
                         "output in Store Setup")
        # Whether this Store can report a change made at the till back to HQ.
        # Asked HERE, from the frozen executable, because COM callbacks are
        # exactly the thing that can work in a source checkout and fail once
        # packaged - and a Store that cannot report is not broken, it simply
        # leaves HQ showing its own last command.
        try:
            from tools import windows_endpoint_observer

            supported, detail = windows_endpoint_observer.notifications_supported()
            lines.append(f"  change reports : {'yes' if supported else 'NO'} - {detail}")
        except Exception as failure:
            lines.append(f"  change reports : UNAVAILABLE - {failure}")
    except Exception as failure:
        lines.append(f"  core audio     : UNAVAILABLE - {failure}")

    # Presence only. The contents are sealed and are never read here.
    lines += [
        "",
        f"credential file  : {credential_path}",
        f"credential status: {'present' if credential_path.exists() else 'ABSENT (not enrolled)'}",
    ]

    if config is not None and config.audio_sink == SINK_MODE_WINDOWS and config.audio_output_device:
        lines.append("")
        lines.append("configured audio device, resolved now:")
        try:
            resolved = resolve_agent_sink(
                sink_mode=SINK_MODE_WINDOWS, selector=config.audio_output_device,
                devices=devices, backend=backend)
        except SinkConfigurationError as failure:
            lines.append(f"  UNAVAILABLE - {failure}")
        else:
            lines.append(f"  index {resolved.device.index}  {resolved.device.name}")
            lines.append(f"  host API {resolved.device.host_api}, "
                         f"{int(resolved.sample_rate)} Hz, {resolved.channels} channel(s)")

    log_directory = Path(config.log_directory) if (config and config.log_directory) \
        else default_log_directory()
    lines += ["", f"log directory    : {log_directory}"]
    if log_directory.exists():
        files = sorted(log_directory.glob("receiver*.log*"))
        total = sum(path.stat().st_size for path in files)
        lines.append(f"log files        : {len(files)} ({total} bytes)")
        newest = max(files, key=lambda path: path.stat().st_mtime, default=None)
        if newest is not None:
            lines.append(f"newest log       : {newest.name}")
            tail = newest.read_text(encoding="utf-8", errors="replace").splitlines()[-5:]
            lines.append("last lines       :")
            # Already redacted when written; redacted again in case an older
            # file predates the filter.
            lines += [f"  {redact(line)}" for line in tail]
    else:
        lines.append("log files        : none yet")

    lines += [
        "",
        "This report is read-only and contains no credential, code or password.",
        "It does not prove any sound was heard - only a person in the room can.",
    ]
    return "\n".join(lines)


def list_audio_devices_report(*, devices=None, backend=None) -> str:
    """The device inventory, reachable from the frozen executable.

    This existed only as ``python tools/windows_audio_devices.py``, which is
    unavailable on precisely the machines that need it: a Store desktop has no
    Python. Without it an operator can only guess a device name - and the same
    display name appears under MME, DirectSound, WASAPI and WDM-KS, so a guess
    is usually ambiguous and refused.
    """
    inventory = tuple(devices) if devices is not None else list_output_devices(backend=backend)
    return format_device_table(inventory)


# ===========================================================================
# Where the backend is, and how to reach it safely
# ===========================================================================
def _is_loopback(hostname: str | None) -> bool:
    """Exact loopback only.

    ``127.0.0.1.evil.example`` resolves wherever its owner likes and merely looks
    like loopback, which is precisely the trick this rejects.
    """
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname.strip("[]")).is_loopback
    except ValueError:
        return False


#: The three RFC1918 ranges, named explicitly.
#:
#: ``ipaddress.is_private`` is a wider set than RFC1918: it also covers loopback,
#: link-local, benchmarking and the documentation ranges. 203.0.113.10 is
#: TEST-NET-3, and ``is_private`` calls it private - so a check written on that
#: property would have accepted a documentation address as a LAN. A test found
#: exactly that. These are the ranges a Store network actually uses.
_RFC1918_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_private_lan_address(hostname: str | None) -> bool:
    """A literal RFC1918 IPv4 address, and nothing that merely resembles one.

    A hostname is refused however internal it looks. A name resolves wherever
    its owner points it and can be repointed tomorrow, so it cannot be checked
    at the moment it is used - which is the only moment that matters.
    """
    if not hostname:
        return False
    try:
        parsed = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return False
    if parsed.version != 4:
        return False
    if not any(parsed in network for network in _RFC1918_NETWORKS):
        return False
    return not (
        parsed.is_loopback or parsed.is_link_local
        or parsed.is_multicast or parsed.is_unspecified
    )


def normalise_backend_url(
    url: object,
    *,
    allow_insecure_loopback: bool,
    allow_insecure_private_lan: bool = False,
    expected_hq_host: str | None = None,
) -> str:
    """Return a clean base URL, or refuse to send a credential in the clear.

    HTTPS needs nothing. Plain HTTP needs one of two explicit, separate
    decisions, and neither implies the other:

    ``--allow-insecure-loopback``
        Loopback only, for local testing.

    ``--allow-insecure-private-lan`` with ``--expected-hq-host``
        A literal RFC1918 IPv4 address that equals the host the operator named.
        "Any private address" is not the same as "the address the operator
        assigned and firewalled": a typo pointing at another machine on the same
        subnet would otherwise be accepted in silence, and a Receiver would hand
        its credential to whatever answered.

    A public address is refused under both, always. So is a hostname, however
    internal it looks.
    """
    if not isinstance(url, str) or not url.strip():
        raise AgentError("a backend URL is required")
    cleaned = url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise AgentError(
            "the backend URL must start with https:// (or http:// for loopback "
            "or an approved private LAN address during testing)"
        )
    if parts.scheme == "https":
        return cleaned

    hostname = parts.hostname

    if _is_loopback(hostname):
        if not allow_insecure_loopback:
            raise InsecureBackendError(
                "plain HTTP to loopback is allowed only with "
                "--allow-insecure-loopback, so it can never happen by accident "
                "in a Store."
            )
        return cleaned

    if allow_insecure_private_lan:
        if not expected_hq_host:
            raise AgentError(
                "--allow-insecure-private-lan requires --expected-hq-host. The "
                "flag permits one named HQ address, not any private address that "
                "happens to answer."
            )
        if not _is_private_lan_address(hostname):
            raise InsecureBackendError(
                f"refusing plain HTTP to {hostname!r}: the private LAN pilot mode "
                "accepts only a literal RFC1918 IPv4 address. A hostname can be "
                "repointed, and a public address is never a LAN."
            )
        if hostname != expected_hq_host.strip():
            raise InsecureBackendError(
                f"the backend URL points at {hostname}, but this Receiver was told "
                f"to expect {expected_hq_host}. Refusing rather than handing a "
                "credential to whatever answered."
            )
        return cleaned

    raise InsecureBackendError(
        "refusing to send a Receiver credential over plain HTTP to "
        f"{hostname!r}. Use https://, or - for a private LAN pilot only - pass "
        "--allow-insecure-private-lan together with --expected-hq-host."
    )


def enrolment_endpoint(base_url: str) -> str:
    return f"{base_url.rstrip('/')}{ENROLMENT_PATH}"


def receiver_websocket_url(base_url: str) -> str:
    """The Receiver socket. No query string, because the credential is a header."""
    base = base_url.rstrip("/")
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return f"{scheme}://{parts.netloc}{RECEIVER_WEBSOCKET_PATH}"


# ===========================================================================
# HTTP, kept behind a seam so the tests never open a socket
# ===========================================================================
class UrllibTransport:
    """POST JSON with the standard library. No new dependency on a till."""

    def post_json(self, url: str, payload: dict, *, timeout: float) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _read_json(response.read())
        except urllib.error.HTTPError as error:
            # A refusal is an answer, not a transport failure: it must not be retried.
            return error.code, _read_json(error.read())
        except urllib.error.URLError as error:
            raise ConnectionError(f"the backend could not be reached ({error.reason})") from None


def _read_json(raw: bytes) -> dict:
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ===========================================================================
# Reading the code
# ===========================================================================
#: A UTF-8 byte-order mark, as a character.
#:
#: Windows PowerShell prepends one when piping a string to a native command:
#:
#:     "ECHO-XXXX-XXXX" | SpeakLinkReceiver.exe enrol --from-stdin
#:
#: arrives as "ï»¿ECHO-XXXX-XXXX". ``str.strip()`` does not remove it - a BOM
#: is not whitespace - so the code silently fails to match and the operator is
#: told their code cannot be used, with nothing pointing at the real cause.
#: Measured, not guessed: a round trip through this exact pipeline produced a
#: 34-character value from a 33-character password.
BYTE_ORDER_MARK = "\ufeff"


def _clean_secret_line(raw: object) -> str:
    return (raw or "").lstrip(BYTE_ORDER_MARK).strip()


def read_enrolment_code(*, stream=None, prompt=getpass.getpass) -> str:
    """From a hidden prompt, or from stdin. Never from a command argument."""
    if stream is not None:
        raw = stream.readline()
    else:
        raw = prompt("Enrolment code (it will not be shown): ")
    code = _clean_secret_line(raw)
    if not code:
        raise AgentError("no enrolment code was provided")
    if len(code) > MAX_CODE_LENGTH:
        raise AgentError("that enrolment code is not a plausible length")
    return code


# ===========================================================================
# Enrolling
# ===========================================================================
@dataclass(frozen=True, slots=True)
class EnrolmentOutcome:
    """What the operator is allowed to see. Deliberately holds no credential."""

    device_public_id: str
    store_id: int


def enrol(
    *,
    backend_url: str,
    code: str,
    device_name: str,
    hostname: str = "",
    software_version: str = AGENT_VERSION,
    credential_path,
    protector,
    transport=None,
    now: datetime | None = None,
    allow_insecure_loopback: bool = False,
    allow_insecure_private_lan: bool = False,
    expected_hq_host: str | None = None,
    attempts: int = 1,
    retry_sleep=time.sleep,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> EnrolmentOutcome:
    """Spend a code, receive one credential, seal it to this computer.

    The local check comes first on purpose. Asking HQ before noticing that this
    computer is already enrolled would burn a good code *and* leave the Device it
    is currently using stranded.
    """
    base = normalise_backend_url(
        backend_url,
        allow_insecure_loopback=allow_insecure_loopback,
        allow_insecure_private_lan=allow_insecure_private_lan,
        expected_hq_host=expected_hq_host,
    )
    credential_path = Path(credential_path)
    if not isinstance(code, str) or not code.strip():
        raise AgentError("an enrolment code is required")

    if credential_path.exists():
        raise AlreadyEnrolled(
            f"this computer already holds a Receiver Device credential at "
            f"{credential_path}. Enrolling again would strand that Device. Use "
            "'remove-local-credential' deliberately if this computer really is "
            "being re-enrolled, and ask an administrator to revoke the old Device."
        )

    payload = {
        "code": code.strip(),
        "device_name": device_name,
        "hostname": hostname or "",
        "software_version": software_version or "",
    }
    endpoint = enrolment_endpoint(base)
    transport = transport or UrllibTransport()

    status, body = _post_with_safe_retries(
        transport, endpoint, payload, timeout=timeout,
        attempts=attempts, retry_sleep=retry_sleep,
    )
    # From here on the code may have been spent. Nothing below resubmits it.
    _raise_for_status(status, body)

    device_public_id = str(body.get("device_public_id") or "").strip()
    credential = body.get("credential")
    store_id = body.get("store_id")

    if not device_public_id or not isinstance(store_id, int) or store_id <= 0:
        raise EnrolmentAmbiguous(
            "HQ accepted the code but its answer was not usable, so this computer "
            "cannot store a credential. The code is spent. Ask an administrator to "
            "check for a new Receiver Device, revoke it, and issue a fresh code."
        )

    try:
        save_credential(
            credential_path,
            credential=credential,
            device_public_id=device_public_id,
            store_id=store_id,
            backend_origin=base,
            protector=protector,
            now=now,
        )
    except CredentialStoreError as failure:
        # The dangerous case: a Device now exists at HQ that nothing can use.
        raise EnrolmentAmbiguous(
            f"Device {device_public_id} was created at HQ, but its credential "
            f"could not be stored on this computer ({failure.__class__.__name__}). "
            "The enrolment code is spent and must not be used again. Ask an "
            f"administrator to revoke Device {device_public_id} and issue a new code."
        ) from None

    return EnrolmentOutcome(device_public_id=device_public_id, store_id=store_id)


def _post_with_safe_retries(transport, endpoint, payload, *, timeout, attempts, retry_sleep):
    """Retry only while nothing can have been issued.

    A transport failure before any response means HQ never answered, so nothing
    was created and trying again is safe. Once *any* status arrives the code may
    be spent, and resubmitting it would be replaying a single-use secret.
    """
    attempts = max(1, int(attempts))
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return transport.post_json(endpoint, payload, timeout=timeout)
        except (ConnectionError, TimeoutError, OSError) as failure:
            last = failure
            if attempt < attempts:
                retry_sleep(min(2.0 * attempt, 10.0))
    raise EnrolmentUnreachable(
        f"HQ could not be reached after {attempts} attempt(s) "
        f"({last.__class__.__name__ if last else 'unknown'}). Nothing was issued, "
        "so the enrolment code is still good."
    ) from None


def _raise_for_status(status: int, body: dict) -> None:
    if status in (200, 201):
        return
    if status == 503:
        raise EnrolmentUnavailable(
            "HQ is not ready to enrol Receiver Devices. This is a server-side "
            "problem, not a bad code - the code has not been used."
        )
    if status == 429:
        raise EnrolmentRefused(
            "HQ is rate limiting enrolment attempts from this computer. Wait, then "
            "try again with the same code."
        )
    # Everything else, including 400 and 409, is a refusal. The server's detail is
    # deliberately generic; the code itself is never quoted back into an error.
    raise EnrolmentRefused(
        "HQ refused that enrolment code. It may be unknown, expired or already "
        "used. Ask an administrator for a new one."
    )


# ===========================================================================
# Reconnecting: bounded, jittered, and willing to give up
# ===========================================================================
MINIMUM_DELAY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential, capped, and jittered.

    The jitter is the part that matters at 44 Stores: when a shared link comes
    back, every Agent must not reconnect in the same millisecond and knock it
    over again.
    """

    initial_seconds: float = 1.0
    maximum_seconds: float = 60.0
    multiplier: float = 2.0
    jitter_fraction: float = 0.25

    def delay(self, attempt: int, *, random_value: float) -> float:
        base = min(
            self.initial_seconds * (self.multiplier ** max(0, attempt - 1)),
            self.maximum_seconds,
        )
        spread = base * self.jitter_fraction
        jittered = base - spread + (2 * spread * random_value)
        return max(MINIMUM_DELAY_SECONDS, min(jittered, self.maximum_seconds))


@dataclass(frozen=True, slots=True)
class SupervisorOutcome:
    state: str
    should_retry: bool
    attempts: int
    detail: str = ""


async def supervise(
    *,
    attempt_session,
    policy: BackoffPolicy,
    sleep=asyncio.sleep,
    random_value=None,
    stable_seconds: float = 30.0,
    max_attempts: int | None = None,
    exit_on_stop: bool = True,
) -> SupervisorOutcome:
    """Run sessions until one ends terminally, or the attempts run out.

    ``attempt_session`` returns a mapping describing the session that just ended
    (``seconds``, and ``stopped`` when the operator stopped it), or raises. A
    ``TerminalAuthentication`` ends everything immediately and without sleeping:
    waiting would be pretending the credential might start working.
    """
    if random_value is None:
        import random as _random

        random_value = _random.random

    attempt = 0
    consecutive_failures = 0
    while max_attempts is None or attempt < max_attempts:
        attempt += 1
        try:
            result = await attempt_session()
        except TerminalAuthentication as refusal:
            return SupervisorOutcome(
                state="AUTHENTICATION_REFUSED", should_retry=False,
                attempts=attempt, detail=str(refusal),
            )
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            consecutive_failures += 1
            detail = failure.__class__.__name__
        else:
            detail = ""
            duration = float((result or {}).get("seconds", 0.0))
            if exit_on_stop and (result or {}).get("stopped"):
                return SupervisorOutcome(
                    state="STOPPED", should_retry=False, attempts=attempt,
                )
            # A session that stood up for a while is evidence the network is fine.
            # Without this reset, one blip after eight hours would be treated as
            # failure number nine and wait a minute for no reason.
            consecutive_failures = 0 if duration >= stable_seconds else consecutive_failures + 1

        if max_attempts is not None and attempt >= max_attempts:
            break
        await sleep(policy.delay(consecutive_failures, random_value=random_value()))

    return SupervisorOutcome(
        state="NETWORK_ERROR", should_retry=True, attempts=attempt, detail=detail,
    )


def classify_connection_failure(error: BaseException) -> BaseException:
    """Turn a refusal into ``TerminalAuthentication``; leave everything else alone.

    Only the close code is consulted. The backend answers a revoked Device, a
    disabled Device and a typo with the same 4401 on purpose, so this cannot and
    must not try to tell them apart - all three are equally permanent from here.
    """
    code = getattr(error, "code", None)
    if code is None:
        code = getattr(getattr(error, "rcvd", None), "code", None)
    if code is None:
        code = getattr(getattr(error, "sent", None), "code", None)
    status = getattr(getattr(error, "response", None), "status_code", None)
    if code in TERMINAL_CLOSE_CODES or status in (401, 403):
        return TerminalAuthentication(
            "HQ refused this Device credential. It may have been revoked or "
            "disabled, or this computer may need enrolling again. The Agent will "
            "not keep retrying a credential that cannot work."
        )
    return error


# ===========================================================================
# The session: the pilot's own state machine, with a Device credential
# ===========================================================================
class DeviceReceiverSession(AudioReceiverPilot):
    """One authenticated Receiver session.

    Only ``run`` is overridden, and only because the credential comes from a
    sealed file instead of an environment variable. Every method that produces
    evidence - prepare, ready, audio, playback, stop - is the pilot's, unchanged
    and untouched, which is what preserves the amplifier evidence path.
    """

    def __init__(self, *, credential: str, connect=None, status_path=None,
                 windows_endpoint_id=None, endpoint_record_path=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._credential = credential
        self._connect = connect
        # The stable Core Audio endpoint this Store may drive, and where the
        # crash-recovery record lives. Both come from the saved config, never
        # from a lookup at broadcast time: resolving a device by name while an
        # announcement is starting is how the wrong output gets muted.
        self.windows_endpoint_id = windows_endpoint_id
        self._endpoint_record_path = endpoint_record_path
        # Optional: where to write "what is true right now". A Store technician
        # waiting for the Device to come online cannot see this process's stdout
        # (it is backgrounded, on purpose), so the evidence has to be on disk.
        self._status_path = Path(status_path) if status_path is not None else None

    def __repr__(self) -> str:
        return f"DeviceReceiverSession(ws_url={self.ws_url!r}, credential=<redacted>)"

    __str__ = __repr__

    def _write_status(self, state: str, *, detail: str = "") -> None:
        if self._status_path is not None:
            write_status(self._status_path, state, detail=detail)

    async def run(self) -> dict:
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect

        connection = await connect(
            self.ws_url,
            # The only place the credential appears. Not the URL: Uvicorn, every
            # proxy and the browser history would all record that.
            additional_headers={"Authorization": f"Bearer {self._credential}"},
            open_timeout=15,
            max_size=4 * 1024 * 1024,
        )
        self.report["connected"] = True
        self._record_state("CONNECTED")
        self._write_status("CONNECTED", detail="the backend accepted this Device credential")

        heartbeat = asyncio.create_task(self._heartbeat_loop(connection))
        # Reports what the Windows output is actually doing, so a change made
        # at the till reaches HQ. Started alongside the heartbeat and torn down
        # with it: it must not outlive the connection it writes to.
        telemetry = asyncio.create_task(self._endpoint_state_loop(connection))
        try:
            await self._session_loop(connection)
        finally:
            telemetry.cancel()
            try:
                await telemetry
            except (asyncio.CancelledError, Exception):
                pass
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await self._shutdown(connection)
        # report["stopped"] means a BROADCAST session ended by an operator's
        # stop command - it says nothing about whether THIS RECEIVER keeps
        # running. The connection to HQ has ended either way; the outer
        # supervise() loop decides whether to reconnect. Folding a broadcast
        # stop into a top-level "STOPPED" Receiver state would have been the
        # exact kind of overclaim this project keeps finding and removing.
        self._write_status(
            "DISCONNECTED",
            detail=("the last broadcast session ended" if self.report.get("stopped")
                    else "the connection to HQ ended"),
        )

        self.report["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
        if (
            self.report["ready"]
            and self.report["audio_receiving"]
            and self.report["playback_confirmed"]
            and self.report["stopped"]
            and not self.report["speaker_verified"]
        ):
            self.report["overall_result"] = "RECEIVER_DEVICE_SESSION_PASSED"

        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, sort_keys=True), encoding="utf-8"
            )
        return self.report


# ===========================================================================
# status / remove-local-credential
# ===========================================================================
def describe_status(credential_path, *, protector) -> dict:
    """What this computer is. Never what it knows."""
    path = Path(credential_path)
    if not path.exists():
        return {
            "enrolled": False,
            "credential_path": str(path),
            "note": "this computer has not been enrolled",
        }
    try:
        record = load_credential(path, protector=protector)
    except CredentialStoreError as failure:
        return {
            "enrolled": False,
            "credential_path": str(path),
            "note": f"a credential file exists but could not be opened ({failure.__class__.__name__})",
        }
    return {
        "enrolled": True,
        "credential_path": str(path),
        "device_public_id": record.device_public_id,
        "store_id": record.store_id,
        "backend_origin": record.backend_origin,
        "enrolled_at_utc": record.created_at.isoformat(),
        "credential_updated_at_utc": record.updated_at.isoformat(),
        "agent_version": AGENT_VERSION,
    }


def remove_local_credential(credential_path, *, confirm=input) -> bool:
    """Delete this computer's credential, only when it is asked for by name.

    Removing it does not revoke anything: the Device still exists at HQ and an
    administrator has to retire it. Saying so matters, because a half-done
    removal leaves a Device that looks live and never connects.
    """
    path = Path(credential_path)
    if not path.exists():
        print(f"There is no Receiver Device credential at {path}. Nothing to remove.")
        return False

    print(f"About to delete this computer's Receiver Device credential:\n  {path}")
    print("This computer will stop being able to authenticate and will need a new")
    print("enrolment code. The Device is NOT revoked - ask an administrator to do that.")
    try:
        answer = confirm(f"Type '{CONFIRMATION_WORD}' to delete it, anything else to cancel: ")
    except EOFError:
        print("Cancelled: removal needs an interactive confirmation.")
        return False
    if str(answer).strip().lower() != CONFIRMATION_WORD:
        print("Cancelled. Nothing was deleted.")
        return False

    delete_credential(path)
    print("Deleted.")
    return True


def rotate_local_credential(credential_path, *, protector, stream=None, prompt=getpass.getpass) -> None:
    """Replace the stored secret after an administrator rotated it at HQ.

    The new credential is read the same way an enrolment code is - hidden prompt
    or stdin - because it is the same kind of secret and a command line is the
    same kind of leak.
    """
    raw = (stream.readline() if stream is not None else prompt(
        "New Device credential (it will not be shown): "
    ))
    candidate = _clean_secret_line(raw)
    if not candidate:
        raise AgentError("no credential was provided")
    replace_credential(Path(credential_path), credential=candidate, protector=protector)


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # Frozen, the operator typed SpeakLinkReceiver.exe, so usage text that
        # says "receiver_agent" sends them looking for a file that is not there.
        prog="SpeakLinkReceiver" if getattr(sys, "frozen", False) else "receiver_agent",
        description=(
            "SpeakLink Receiver Agent. Enrols this computer once with a one-time "
            "code, then reconnects using a DPAPI-protected Device credential. "
            "Neither the code nor the credential is ever accepted as a command "
            "argument or placed in a URL."
        ),
        # Without this, argparse accepts any unambiguous prefix - so
        # `--credential <SECRET>` would be quietly taken as `--credential-path`
        # and the credential would end up in the process arguments after all.
        # A test found exactly that. Abbreviation is off everywhere below too.
        allow_abbrev=False,
    )
    # Before the subparsers, so `--version` answers on its own rather than being
    # refused for a missing command. An operator checking which build is on a
    # till should not have to guess a subcommand to find out.
    parser.add_argument(
        "--version", action="version",
        version=f"SpeakLinkReceiver {AGENT_VERSION}",
        help="Print the Agent version and exit.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, **kwargs) -> argparse.ArgumentParser:
        return commands.add_parser(name, allow_abbrev=False, **kwargs)

    def common(subparser, *, needs_url: bool, url_required: bool = True) -> None:
        if needs_url:
            # `run` no longer requires it on the command line: an installed
            # Store keeps it in its config file, so the scheduled task's
            # arguments stay short and boring. Everything else still demands it,
            # because enrolling against the wrong HQ by omission would be worse
            # than being asked.
            subparser.add_argument("--backend-url", required=url_required, default=None,
                                   help="HQ base URL, https:// in production. "
                                        "For 'run', may come from the saved configuration.")
            subparser.add_argument("--allow-insecure-loopback", action="store_true",
                                   help="Permit plain HTTP to 127.0.0.1 for local testing only.")
            subparser.add_argument(
                "--allow-insecure-private-lan", action="store_true",
                help=(
                    "PRIVATE LAN PILOT ONLY. Permit plain HTTP to one named "
                    "RFC1918 address. Requires --expected-hq-host. Never permits "
                    "a public address, and never a hostname."
                ),
            )
            subparser.add_argument(
                "--expected-hq-host", default=None,
                help=(
                    "The literal HQ IPv4 address this Receiver is allowed to talk "
                    "to, e.g. 192.168.4.134. Not a secret."
                ),
            )
        subparser.add_argument("--credential-path", default=None,
                               help="Where the sealed credential FILE lives (not a secret).")
        subparser.add_argument("--config-path", default=None,
                               help="Where the saved non-secret settings live.")

    enrol_command = add_command("enrol", help="Enrol this computer with a one-time code.")
    common(enrol_command, needs_url=True)
    enrol_command.add_argument("--device-name", required=True,
                               help="How this computer appears in HQ, e.g. 'Store UN till 1'.")
    enrol_command.add_argument("--hostname", default="")
    enrol_command.add_argument("--software-version", default=AGENT_VERSION)
    enrol_command.add_argument("--from-stdin", action="store_true",
                               help="Read the one-time code from stdin instead of a hidden prompt.")
    enrol_command.add_argument("--attempts", type=int, default=3,
                               help="Retries are attempted only while nothing can have been issued.")

    run_command = add_command("run", help="Run using the stored Device credential.")
    common(run_command, needs_url=True, url_required=False)
    run_command.add_argument("--queue-capacity", type=int, default=24)
    run_command.add_argument("--report", default=None, help="Optional secret-free JSON report path.")
    run_command.add_argument("--max-attempts", type=int, default=None)
    run_command.add_argument("--exit-after-stop", action="store_true",
                             help="Exit once a broadcast stops, instead of waiting for the next.")
    run_command.add_argument(
        "--log-directory", default=None,
        help=(
            "Write rotating, bounded, secret-free logs here. Required in practice "
            "when Task Scheduler runs this hidden, because there is then no "
            "console for anything printed to reach. Defaults to "
            # argparse expands help through %-formatting, so a literal percent
            # must be doubled. A single one here crashed `run --help` outright.
            r"%%LOCALAPPDATA%%\SpeakLink\receiver\logs."
        ),
    )
    run_command.add_argument(
        "--audio-sink", choices=SUPPORTED_SINK_MODES, default=None,
        help=(
            "Where decoded audio goes. 'null' decodes and DISCARDS it - correct "
            "for a software test, silent on a Store desktop. 'windows' plays it "
            "through the device named by --audio-output-device. Defaults to the "
            "SPEAKLINK_AUDIO_SINK_MODE environment variable, and to 'null'."
        ),
    )
    run_command.add_argument(
        "--audio-output-device", default=None, metavar="SELECTOR",
        help=(
            "Which output device, from 'list-audio-devices'. Prefer the verified "
            "form 'index:N@Exact Name', which refuses to open the wrong endpoint "
            "after Windows renumbers. A bare device name is usually ambiguous, "
            "because one endpoint appears under MME, DirectSound, WASAPI and "
            "WDM-KS."
        ),
    )
    run_command.add_argument(
        "--legacy-pilot-mode", action="store_true",
        help=(
            "MIGRATION ONLY. Authenticate with the shared per-Store token from "
            "SPEAKLINK_RECEIVER_TOKEN instead of a Device credential. Never the "
            "default, never recommended, and removed at cutover."
        ),
    )

    status_command = add_command("status", help="Show what this computer is enrolled as.")
    common(status_command, needs_url=False)

    rotate_command = add_command(
        "rotate-local-credential",
        help="Store a credential an administrator has just rotated at HQ.",
    )
    common(rotate_command, needs_url=False)
    rotate_command.add_argument(
        "--from-stdin", action="store_true",
        help="Read the new credential from stdin instead of a hidden prompt.",
    )

    # Deliberately takes no backend URL and no credential path: it reads the
    # sound card inventory and nothing else, so an operator can safely run it on
    # a Store desktop before anything is enrolled.
    add_command(
        "list-audio-devices",
        help="List Windows audio output devices and their stable selectors.",
    )

    # Read-only, and safe to read out over the phone: it shows no credential,
    # no code and no password.
    diagnose_command = add_command(
        "diagnose",
        help="Read-only report: version, settings, audio device, logs.",
    )
    common(diagnose_command, needs_url=False)

    remove_command = add_command(
        "remove-local-credential",
        help="Delete this computer's credential after typed confirmation.",
    )
    common(remove_command, needs_url=False)

    return parser


def _protector():
    return DeviceCredentialProtector()


def _credential_path(arguments) -> Path:
    return Path(arguments.credential_path) if arguments.credential_path else default_credential_path()


def _run_command(arguments) -> int:
    # Logging first, so a refusal during start-up is still written down. Under
    # Task Scheduler this is the only place anything is recorded at all.
    log = configure_logging(arguments.log_directory)
    log.info("SpeakLink Receiver %s starting", AGENT_VERSION)

    base = normalise_backend_url(
        arguments.backend_url,
        allow_insecure_loopback=arguments.allow_insecure_loopback,
        allow_insecure_private_lan=arguments.allow_insecure_private_lan,
        expected_hq_host=arguments.expected_hq_host,
    )
    if arguments.allow_insecure_private_lan:
        # Loud, and without any secret in it. An operator who sees this on a
        # machine that is not part of a pilot has found a real problem.
        print("WARNING: private LAN pilot mode. This Receiver is sending its")
        print("         credential over plain HTTP to " + str(arguments.expected_hq_host) + ".")
        print("         Only ever correct on a trusted private network.")
        print("         Production requires HTTPS and WSS.")
    ws_url = receiver_websocket_url(base)
    path = _credential_path(arguments)

    # Resolved BEFORE the credential is unsealed and before the lock is taken.
    # A bad device selector is a configuration mistake, and finding out about it
    # after a Store has connected and been promoted is how a silent Receiver
    # ends up looking like a working one.
    sink = _agent_sink(arguments)
    for line in describe_sink(sink):
        print(line)
        log.info("%s", line)

    with InstanceLock(path):
        return _run_locked(arguments, ws_url=ws_url, path=path, log=log, sink=sink)


def _agent_sink(arguments) -> SinkConfiguration:
    """Command line first, environment second, discard-everything last.

    The environment variables keep working unchanged, so every existing smoke
    test and script behaves exactly as before; the options simply make the same
    choice visible in the command that a scheduled task stores.
    """
    if arguments.audio_sink is None and arguments.audio_output_device is None:
        return resolve_sink_configuration()
    return resolve_agent_sink(
        sink_mode=arguments.audio_sink or os.environ.get("SPEAKLINK_AUDIO_SINK_MODE"),
        selector=arguments.audio_output_device
        or os.environ.get("SPEAKLINK_AUDIO_OUTPUT_DEVICE"),
    )


def _run_locked(arguments, *, ws_url: str, path: Path, log: logging.Logger,
                sink: SinkConfiguration) -> int:
    if arguments.legacy_pilot_mode:
        print("WARNING: legacy pilot mode. This computer is authenticating with the")
        print("         shared per-Store token, which every computer in that Store")
        print("         has. It cannot be revoked individually. Enrol a Device.")
        from tools.audio_receiver_pilot import AudioReceiverPilot as _Pilot

        pilot = _Pilot(
            ws_url=ws_url,
            queue_capacity=arguments.queue_capacity,
            report_path=Path(arguments.report) if arguments.report else None,
            sink=sink,
        )
        report = asyncio.run(pilot.run())
        return EXIT_OK if report["overall_result"].endswith("PASSED") else EXIT_NETWORK

    record = load_credential(path, protector=_protector())
    print(f"Device  : {record.device_public_id}")
    print(f"Store   : {record.store_id}")
    print(f"Backend : {record.backend_origin}")
    log.info(
        "device=%s store=%s backend=%s",
        record.device_public_id, record.store_id, record.backend_origin,
    )

    report_path = Path(arguments.report) if arguments.report else None

    # Read once, here, rather than at broadcast time. The endpoint this Store
    # may drive is a decision the technician made when they selected the
    # output; re-deriving it later - especially from a device name - is how
    # SpeakLink would end up controlling whatever happened to match today.
    try:
        config = load_config(default_config_path())
    except AgentError:
        config = None

    # A Receiver that died mid-announcement left the shop at the announcement
    # volume. Put it back before doing anything else, and say so.
    recovery = windows_endpoint_restore.recover_on_startup(
        windows_endpoint_restore.default_record_path(),
        apply_state=lambda endpoint_id, **kw: __import__(
            "tools.windows_endpoint_volume", fromlist=["x"]
        ).apply_state(endpoint_id, **kw),
        logger=log,
    )
    if recovery.get("recovered"):
        log.warning("Restored a Windows audio output left changed by a previous run")
    elif recovery.get("endpoint_id"):
        log.warning("Could not restore a previous run's Windows audio output: %s",
                    recovery.get("reason"))

    async def attempt() -> dict:
        session = DeviceReceiverSession(
            ws_url=ws_url,
            credential=load_credential(path, protector=_protector()).credential(),
            queue_capacity=arguments.queue_capacity,
            report_path=report_path,
            sink=sink,
            # Written automatically, like the log directory - not a flag an
            # installer has to know to pass. This is the only evidence a
            # backgrounded process can offer of what it is doing right now.
            status_path=receiver_status_path(),
            # From the saved config. None on an installation that has not
            # re-selected its output since master control shipped, and that is
            # reported to HQ as unsupported rather than guessed at.
            windows_endpoint_id=(config.windows_endpoint_id if config else None),
            endpoint_record_path=windows_endpoint_restore.default_record_path(),
        )
        started = time.monotonic()
        try:
            report = await session.run()
        except Exception as error:
            raise classify_connection_failure(error) from None
        return {"seconds": time.monotonic() - started, "stopped": bool(report.get("stopped"))}

    outcome = asyncio.run(
        supervise(
            attempt_session=attempt,
            policy=BackoffPolicy(),
            max_attempts=arguments.max_attempts,
            exit_on_stop=arguments.exit_after_stop,
        )
    )
    print(f"  state: {outcome.state}")
    print(f"  attempts: {outcome.attempts}")
    log.info("finished state=%s attempts=%s", outcome.state, outcome.attempts)
    if outcome.state == "AUTHENTICATION_REFUSED":
        print(f"  {outcome.detail}")
        log.error("authentication refused: %s", outcome.detail)
        return EXIT_AUTHENTICATION
    return EXIT_OK if outcome.state == "STOPPED" else EXIT_NETWORK


def main(argv: list[str] | None = None) -> int:
    # Before anything looks for FFmpeg. Harmless when running from source.
    prefer_packaged_ffmpeg()
    arguments = build_parser().parse_args(argv)

    try:
        # Before the credential machinery is touched at all. list-audio-devices
        # deliberately has no --credential-path, and computing one here crashed
        # it outright - on the one command an operator runs on a Store desktop
        # before anything has been enrolled.
        if arguments.command == "list-audio-devices":
            # Read-only. Opens nothing, plays nothing, needs no credential.
            print(list_audio_devices_report())
            print()
            print("Copy a VERIFIED selector (index:N@Name) and pass it as:")
            print("  --audio-sink windows --audio-output-device \"index:N@Name\"")
            return EXIT_OK

        protector = _protector()
        path = _credential_path(arguments)
        config_path = Path(arguments.config_path) if arguments.config_path \
            else default_config_path()

        if arguments.command == "diagnose":
            print(diagnose_report(config_path=config_path, credential_path=path))
            return EXIT_OK

        # Saved settings fill in whatever the command line left out, so an
        # installed Store keeps working after a reboot without anybody retyping
        # a device selector. An explicit option always wins and is never saved.
        if arguments.command == "run":
            merge_config_into_arguments(arguments, load_config(config_path))
            if not arguments.backend_url:
                raise AgentError(
                    "no backend URL was given and none is saved. Pass --backend-url, "
                    "or install this Receiver so its settings are recorded."
                )

        if arguments.command == "enrol":
            code = read_enrolment_code(stream=sys.stdin if arguments.from_stdin else None)
            outcome = enrol(
                backend_url=arguments.backend_url,
                code=code,
                device_name=arguments.device_name,
                hostname=arguments.hostname,
                software_version=arguments.software_version,
                credential_path=path,
                protector=protector,
                allow_insecure_loopback=arguments.allow_insecure_loopback,
                allow_insecure_private_lan=arguments.allow_insecure_private_lan,
                expected_hq_host=arguments.expected_hq_host,
                attempts=arguments.attempts,
            )
            del code
            print("Enrolled.")
            print(f"  Device : {outcome.device_public_id}")
            print(f"  Store  : {outcome.store_id}")
            print(f"  Sealed : {path}")
            return EXIT_OK

        if arguments.command == "status":
            for key, value in describe_status(path, protector=protector).items():
                print(f"  {key}: {value}")
            return EXIT_OK

        if arguments.command == "rotate-local-credential":
            rotate_local_credential(
                path,
                protector=protector,
                stream=sys.stdin if arguments.from_stdin else None,
            )
            print("The rotated credential is stored. The previous one no longer works.")
            return EXIT_OK

        if arguments.command == "remove-local-credential":
            return EXIT_OK if remove_local_credential(path, confirm=input) else EXIT_REFUSED

        return _run_command(arguments)

    # Before AgentError: a duplicate launch is a clean, ordinary outcome, and a
    # restart policy that treated it as a failure would relaunch forever.
    except AlreadyRunning as duplicate:
        print(str(duplicate), file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except TerminalAuthentication as refusal:
        print(f"Refused: {refusal}", file=sys.stderr)
        return EXIT_AUTHENTICATION
    # AudioReceiverError is NOT an AgentError, and SinkConfigurationError is one
    # of its subclasses. Without this clause, naming a device that does not
    # exist - or one whose name is ambiguous across host APIs, which most are -
    # escaped as an unhandled exception. The operator got a Python traceback
    # naming files that do not exist on a Store desktop, and the Store simply
    # went OFFLINE with nothing explaining why.
    except (AgentError, CredentialStoreError, AudioReceiverError) as failure:
        print(f"Refused: {failure}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
