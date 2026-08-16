"""Local one-Store audio Receiver pilot.

Connects one Store Receiver to the pilot backend, performs the real readiness
checks, receives binary WebM/Opus chunks through a bounded queue, feeds them to
one FFmpeg process and reports honest acknowledgements using the existing
frozen ``receiver_contract`` message shapes.

Evidence rules this file obeys:

- ``receiver_ready`` is sent only after FFmpeg is proven present, the Opus/WebM
  decode path is proven supported, and the bounded queue exists.
- ``audio_receiving`` is sent only after real audio bytes arrive for the active
  session.
- ``playback_confirmed`` is sent only after real processing evidence: in
  ``null`` sink mode that is FFmpeg's own ``out_time_ms`` progress counter
  advancing past zero; in ``windows`` sink mode it is PCM frames actually
  accepted by the explicitly selected output device.
- ``speaker_verified`` is **never** sent. Even in ``windows`` mode the Receiver
  only knows that a device accepted frames. It cannot know whether an amplifier
  is on, an input is selected, or any sound was audible. Only future LinkGuard
  acoustic detection may set SPEAKER_VERIFIED.

Sink modes: ``null`` (default, used by every automated test) and ``windows``
(requires an explicitly selected device; never the Windows default).

The Receiver credential is read from an environment variable, kept in memory,
and never printed, logged, written to a report, placed in a URL or passed as a
command argument. No raw audio is ever logged or written into the repository.
"""

from __future__ import annotations

import argparse
import array
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
import tempfile
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audio_protocol import (  # noqa: E402
    AudioProtocolError,
    InvalidAudioChunkError,
    parse_prepare_message,
    parse_set_audio_control_message,
    validate_audio_chunk,
)
from audio_streaming import StoreAudioQueue, StoreQueueClosedError  # noqa: E402

if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.windows_audio_devices import (  # noqa: E402
    AudioDeviceError,
    OutputDevice,
    format_device_table,
    list_output_devices,
    resolve_output_device,
)

RECEIVER_TOKEN_ENV = "SPEAKLINK_RECEIVER_TOKEN"
PROTOCOL_VERSION = "1.0"

# Pilot-only, process-scoped sink configuration. The default is always the
# null sink so automated tests and CI can never open a real speaker.
AUDIO_SINK_MODE_ENV = "SPEAKLINK_AUDIO_SINK_MODE"
AUDIO_OUTPUT_DEVICE_ENV = "SPEAKLINK_AUDIO_OUTPUT_DEVICE"

SINK_MODE_NULL = "null"
SINK_MODE_WINDOWS = "windows"
SUPPORTED_SINK_MODES = (SINK_MODE_NULL, SINK_MODE_WINDOWS)

# Decoded PCM shape for hardware playback.

logger = logging.getLogger("speaklink.receiver.pilot")

OUTPUT_SAMPLE_RATE = 48_000
OUTPUT_CHANNELS = 1
OUTPUT_DTYPE = "int16"
OUTPUT_BYTES_PER_FRAME = 2 * OUTPUT_CHANNELS

# The backend waits HEARTBEAT_INTERVAL_SECONDS for a message and closes an idle
# Receiver socket with code 4408 once its snapshot ages past
# OFFLINE_AFTER_SECONDS (5 s and 30 s in backend/receiver_contract.py). This
# value is kept here rather than imported so tools/ stays independent of
# backend/; backend/tests/test_receiver_heartbeat.py pins the relationship.
# It must stay small enough that one dropped beat is still not fatal.
HEARTBEAT_SECONDS = 5.0

EXIT_OK = 0
EXIT_SAFETY = 1
EXIT_AUDIO_FAILED = 2
EXIT_CLEANUP_FAILED = 3

FFMPEG_EXIT_TIMEOUT_SECONDS = 15


class AudioReceiverError(RuntimeError):
    """Controlled, secret-free Receiver pilot failure."""


class SinkConfigurationError(AudioReceiverError):
    """Raised when the sink mode or the selected output device is not valid."""


@dataclass(frozen=True, slots=True)
class SinkConfiguration:
    """Resolved, process-scoped playback configuration for one Receiver run."""

    sink_mode: str
    device: OutputDevice | None
    sample_rate: int = OUTPUT_SAMPLE_RATE
    channels: int = OUTPUT_CHANNELS

    @property
    def is_hardware(self) -> bool:
        return self.sink_mode == SINK_MODE_WINDOWS

    @property
    def bytes_per_frame(self) -> int:
        return 2 * self.channels

    def as_dict(self) -> dict:
        """Secret-free diagnostics. A device name is never an identity."""
        return {
            "channels": self.channels,
            "sample_rate": self.sample_rate,
            "selected_device_id": self.device.selector if self.device else None,
            "selected_device_name": self.device.name if self.device else None,
            "selected_device_host_api": self.device.host_api if self.device else None,
            "sink_mode": self.sink_mode,
        }


def resolve_sink_configuration(*, backend=None) -> SinkConfiguration:
    """Resolve the pilot sink from the process environment.

    The default is always the null sink. Hardware mode requires an explicit,
    unambiguous device selector: the pilot never picks a device, never falls
    back to the Windows default, and never guesses from a partial name.
    """
    raw_mode = (os.environ.get(AUDIO_SINK_MODE_ENV) or SINK_MODE_NULL).strip().lower()
    if raw_mode not in SUPPORTED_SINK_MODES:
        raise SinkConfigurationError(
            f"{AUDIO_SINK_MODE_ENV}={raw_mode!r} is not supported; "
            f"use one of {SUPPORTED_SINK_MODES}"
        )

    if raw_mode == SINK_MODE_NULL:
        # A configured device is deliberately ignored in null mode so a leftover
        # variable can never cause an unexpected sound.
        return SinkConfiguration(sink_mode=SINK_MODE_NULL, device=None)

    selector = (os.environ.get(AUDIO_OUTPUT_DEVICE_ENV) or "").strip()
    if not selector:
        raise SinkConfigurationError(
            f"{SINK_MODE_WINDOWS} sink mode requires {AUDIO_OUTPUT_DEVICE_ENV} to name "
            "exactly one output device. List devices first and copy a stable "
            "'index:N' selector. The pilot will not choose one for you and will "
            "never use the Windows default device."
        )

    try:
        device = resolve_output_device(selector, backend=backend)
    except AudioDeviceError as error:
        raise SinkConfigurationError(str(error)) from None

    # Adopt the device's own advertised format. Hardcoding 48 kHz mono made
    # the open fail or the audio wrong on strict host APIs such as WDM-KS,
    # where a device may advertise 44.1 kHz stereo. FFmpeg resamples and
    # re-channels to whatever the device wants.
    sample_rate = device.default_samplerate or OUTPUT_SAMPLE_RATE
    channels = min(max(device.max_output_channels, 1), 2)
    return SinkConfiguration(
        sink_mode=SINK_MODE_WINDOWS,
        device=device,
        sample_rate=sample_rate,
        channels=channels,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_token() -> str:
    value = os.environ.get(RECEIVER_TOKEN_ENV)
    if value is None or not value.strip():
        raise AudioReceiverError(
            f"{RECEIVER_TOKEN_ENV} is not set. Provide the Store credential through "
            "the environment; it is never accepted as a command argument."
        )
    return value.strip()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _receiver_version() -> str:
    """This build's version AND the commit it was built from.

    The version alone is not enough to answer "is this shop running today's
    Receiver". Six builds went out in one day, every one of them 1.7.5,
    because the version names the release and not the build - so HQ could see
    a version, believe the Store was current, and be wrong. The commit is in
    the package manifest already; this reads it back out.
    """
    version = "unknown"
    try:
        from tools.speaklink_version import STORE_KIT_VERSION
        version = str(STORE_KIT_VERSION)
    except ImportError:  # pragma: no cover - a checkout laid out differently
        try:
            from speaklink_version import STORE_KIT_VERSION
            version = str(STORE_KIT_VERSION)
        except ImportError:
            pass

    for candidate in _manifest_candidates():
        try:
            import json as _json

            data = _json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a missing manifest is not a fault
            continue
        commit = (data.get("source_commit_short")
                  or str(data.get("source_commit") or "")[:7])
        if commit:
            return f"{version} ({commit})"
    return version


def _manifest_candidates():
    """Where a packaged Receiver's manifest.json might be."""
    from pathlib import Path as _Path

    here = []
    try:
        here.append(_Path(sys.executable).resolve().parent / "manifest.json")
    except Exception:  # noqa: BLE001
        pass
    try:
        from tools.resource_paths import resource_root

        here.append(_Path(resource_root()) / "manifest.json")
    except Exception:  # noqa: BLE001
        pass
    return [path for path in here if path.is_file()]


def hidden_child_process_options() -> dict:
    """Start a child process with no console window. Windows only.

    THE BUG THIS ENDS

    ``SpeakLinkReceiverBackground.exe`` is built GUI-subsystem, so it has no
    console. ``ffmpeg.exe`` is a console application. When a parent with no
    console starts a console child and does not ask for ``CREATE_NO_WINDOW``,
    Windows gives that child a **brand-new console** - and a new console is a
    black window on the Store counter. It appeared exactly when a broadcast
    started, because that is when the decoder starts.

    Measured with ``pythonw.exe`` as the parent, which is GUI subsystem exactly
    like the background Receiver::

        parent_has_console            : False
        child, no creation flags      : has_console=True,  console_hwnd=721134
        child, with CREATE_NO_WINDOW  : has_console=False, console_hwnd=0

    ``CREATE_NO_WINDOW`` alone is what fixes it. ``STARTUPINFO`` with
    ``SW_HIDE`` is added as well because it costs nothing and covers a child
    that opens a window of its own for some other reason.

    Deliberately **not** ``shell=True``: running through cmd.exe to hide a
    window swaps one console for another and adds a shell that parses the
    command line. And deliberately empty off Windows, where these constants do
    not exist and passing them would raise.
    """
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


def opus_webm_decode_supported() -> bool:
    """Real capability check, not an assumption."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        decoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=30,
            **hidden_child_process_options(),
        )
        formats = subprocess.run(
            [ffmpeg, "-hide_banner", "-formats"],
            capture_output=True, text=True, timeout=30,
            **hidden_child_process_options(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if decoders.returncode != 0 or formats.returncode != 0:
        return False
    has_opus = " opus " in decoders.stdout or "libopus" in decoders.stdout
    has_webm = "matroska,webm" in formats.stdout or "webm" in formats.stdout
    return bool(has_opus and has_webm)


class WindowsPcmSink:
    """Writes decoded PCM to exactly one explicitly selected output device.

    It opens only the device the operator named. It never enumerates-and-tries,
    never falls back to the Windows default, never changes the system default
    device and never touches the system volume.
    """

    def __init__(self, configuration: SinkConfiguration, *, backend=None) -> None:
        if not configuration.is_hardware or configuration.device is None:
            raise SinkConfigurationError("a Windows PCM sink needs a selected device")
        self._configuration = configuration
        self._backend = backend
        self._stream = None
        self._frames_written = 0
        self._failed = False
        self._lock = threading.Lock()
        # SpeakLink's own PCM level. DELIBERATELY LEFT AT UNITY.
        #
        # This used to follow the HQ per-Store slider. It no longer does: that
        # slider now sets the WINDOWS ENDPOINT MASTER volume, and applying the
        # same percentage in both places would attenuate twice - HQ at 50%
        # would produce 25% of the signal, and nobody could explain why the
        # shop was quiet.
        #
        # The scaling code is kept rather than deleted because it is the only
        # mechanism that can silence SpeakLink WITHOUT touching the shared
        # endpoint, and a future per-Store DSP control is the obvious use for
        # it. Today nothing moves it off 100/unmuted, and a test asserts that
        # the master-volume path does not.
        self._volume_percent = 100
        self._muted = False

    @property
    def configuration(self) -> SinkConfiguration:
        return self._configuration

    @property
    def frames_written(self) -> int:
        with self._lock:
            return self._frames_written

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def open(self) -> None:
        """Open the selected device. Raises if it cannot be opened."""
        backend = self._backend
        if backend is None:
            try:
                import sounddevice

                backend = sounddevice
            except Exception as error:
                raise SinkConfigurationError(
                    "the sounddevice/PortAudio backend is unavailable, so the "
                    "selected Windows output device cannot be opened"
                ) from error
        device = self._configuration.device
        try:
            self._stream = backend.RawOutputStream(
                samplerate=self._configuration.sample_rate,
                channels=self._configuration.channels,
                dtype=OUTPUT_DTYPE,
                device=device.index,
            )
            self._stream.start()
        except Exception as error:
            self._stream = None
            raise SinkConfigurationError(
                f"the selected output device {device.selector} "
                f"({device.name}) could not be opened"
            ) from error

    @property
    def volume_percent(self) -> int:
        with self._lock:
            return self._volume_percent

    @property
    def muted(self) -> bool:
        with self._lock:
            return self._muted

    @property
    def effective_percent(self) -> int:
        with self._lock:
            return 0 if self._muted else self._volume_percent

    def set_audio_control(self, *, volume_percent: int, muted: bool) -> None:
        """Change the output level. Takes effect on the next buffer written.

        Never restarts the stream and never touches the device: the next
        ``write`` simply scales differently. Mute is kept separate from volume
        so unmuting restores the operator's level rather than a remembered
        copy of it.
        """
        if isinstance(volume_percent, bool) or not isinstance(volume_percent, int):
            raise SinkConfigurationError("volume_percent must be a whole number")
        if not 0 <= volume_percent <= 100:
            raise SinkConfigurationError("volume_percent must be between 0 and 100")
        if not isinstance(muted, bool):
            raise SinkConfigurationError("muted must be true or false")
        with self._lock:
            self._volume_percent = volume_percent
            self._muted = muted

    def _scaled(self, pcm: bytes) -> bytes:
        """Apply the current level to one buffer of signed 16-bit samples.

        Two shortcuts that matter: at 100% unmuted the buffer is passed through
        untouched, so the ordinary case costs nothing at all; and at zero the
        result is a silent buffer of the same length, which keeps the device
        fed at a steady rate. Writing nothing instead would starve the stream
        and produce underruns that sound like faults rather than like silence.
        """
        with self._lock:
            percent = 0 if self._muted else self._volume_percent
        if percent == 100:
            return pcm
        if percent == 0:
            return b"\x00" * len(pcm)
        # int16 little-endian, the format this sink opened the device with.
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % samples.itemsize)])
        if sys.byteorder != "little":  # pragma: no cover - Windows is LE
            samples.byteswap()
        scale = percent / 100.0
        for index, value in enumerate(samples):
            # Rounded, then clamped. Rounding alone can produce 32768, which is
            # one past int16 and would wrap to a loud negative click.
            scaled = int(value * scale)
            samples[index] = -32768 if scaled < -32768 else (32767 if scaled > 32767 else scaled)
        if sys.byteorder != "little":  # pragma: no cover
            samples.byteswap()
        return samples.tobytes()

    def write(self, pcm: bytes) -> bool:
        stream = self._stream
        if stream is None or self._failed:
            return False
        try:
            pcm = self._scaled(pcm)
            stream.write(pcm)
        except Exception:
            # Never log the audio payload; record the failure for PLAYBACK_ERROR.
            self._failed = True
            return False
        with self._lock:
            self._frames_written += len(pcm) // self._configuration.bytes_per_frame
        return True

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        for step in ("stop", "close"):
            try:
                getattr(stream, step)()
            except Exception:
                pass


class FfmpegDecoder:
    """One FFmpeg process per active session.

    In ``null`` mode it decodes to FFmpeg's null muxer and uses FFmpeg's own
    progress counter as processing evidence. In ``windows`` mode it decodes to
    raw PCM on stdout, which is streamed to the selected output device, and the
    frames actually accepted by that device are the processing evidence.
    """

    def __init__(
        self,
        *,
        sink_mode: str = SINK_MODE_NULL,
        pcm_sink: "WindowsPcmSink | None" = None,
    ) -> None:
        if sink_mode not in SUPPORTED_SINK_MODES:
            raise SinkConfigurationError(f"unsupported sink mode {sink_mode!r}")
        if sink_mode == SINK_MODE_WINDOWS and pcm_sink is None:
            raise SinkConfigurationError("windows sink mode requires an open PCM sink")
        self.sink_mode = sink_mode
        self._pcm_sink = pcm_sink
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._decoded_us = 0
        self._lock = threading.Lock()
        self._progress_seen = threading.Event()

    @property
    def decoded_microseconds(self) -> int:
        with self._lock:
            return self._decoded_us

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.poll()

    def command(self) -> list[str]:
        base = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-f", "webm",
            "-i", "pipe:0",
        ]
        if self.sink_mode == SINK_MODE_WINDOWS:
            # Raw PCM on stdout, resampled and re-channelled to exactly what
            # the selected device advertised. FFmpeg still never opens an audio
            # device: the endpoint is opened by WindowsPcmSink.
            configuration = self._pcm_sink.configuration
            return base + [
                "-ac", str(configuration.channels),
                "-ar", str(configuration.sample_rate),
                "-f", "s16le",
                "pipe:1",
            ]
        return base + [
            "-ac", str(OUTPUT_CHANNELS),
            "-progress", "pipe:1", "-f", "null", "-",
        ]

    @property
    def frames_written(self) -> int:
        return self._pcm_sink.frames_written if self._pcm_sink is not None else 0

    @property
    def sink_failed(self) -> bool:
        return bool(self._pcm_sink is not None and self._pcm_sink.failed)

    def start(self) -> None:
        if self._process is not None:
            raise AudioReceiverError("the FFmpeg decoder is already running")
        # The spawn that produced the black window on the Store counter. The
        # pipes are unchanged - stdin carries the audio in, stdout carries the
        # decoded PCM and progress back - only the console is suppressed.
        self._process = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **hidden_child_process_options(),
        )
        target = self._pump_pcm if self.sink_mode == SINK_MODE_WINDOWS else self._read_progress
        self._reader = threading.Thread(target=target, daemon=True)
        self._reader.start()

    def _pump_pcm(self) -> None:
        """Stream decoded PCM to the selected device. Never logs the payload."""
        process = self._process
        if process is None or process.stdout is None or self._pcm_sink is None:
            return
        # ~20 ms of 48 kHz mono 16-bit audio per write keeps latency low.
        configuration = self._pcm_sink.configuration
        block = configuration.sample_rate // 50 * configuration.bytes_per_frame
        while True:
            chunk = process.stdout.read(block)
            if not chunk:
                return
            if not self._pcm_sink.write(chunk):
                return
            frames = self._pcm_sink.frames_written
            if frames > 0:
                with self._lock:
                    self._decoded_us = int(frames * 1_000_000 / configuration.sample_rate)
                self._progress_seen.set()

    def _read_progress(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("out_time_ms="):
                continue
            value = line.split("=", 1)[1]
            if not value.isdigit():
                continue
            microseconds = int(value)
            if microseconds > 0:
                with self._lock:
                    self._decoded_us = microseconds
                self._progress_seen.set()

    def feed(self, chunk: bytes) -> bool:
        """Write one chunk to FFmpeg. Never logs the payload."""
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            return False
        try:
            process.stdin.write(chunk)
            process.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def wait_for_decode(self, timeout: float) -> bool:
        """Block until FFmpeg proves it decoded audio, or the timeout expires."""
        return self._progress_seen.wait(timeout)

    def close(self, *, timeout: float = FFMPEG_EXIT_TIMEOUT_SECONDS) -> int | None:
        """Close stdin cleanly and wait for FFmpeg to exit. Never leaks a child."""
        process = self._process
        if process is None:
            return None
        try:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        if self._reader is not None:
            self._reader.join(timeout=3)
        # Always release the output device, even if FFmpeg had to be killed.
        if self._pcm_sink is not None:
            self._pcm_sink.close()
        return process.poll()


class AudioReceiverPilot:
    """Drives one Store Receiver session end to end."""

    def __init__(
        self,
        *,
        ws_url: str,
        queue_capacity: int = 24,
        playback_timeout: float = 20.0,
        report_path: Path | None = None,
        sink: SinkConfiguration | None = None,
        audio_backend=None,
    ) -> None:
        self.ws_url = ws_url
        self.queue_capacity = queue_capacity
        self.playback_timeout = playback_timeout
        self.report_path = report_path
        self.sink = sink or SinkConfiguration(sink_mode=SINK_MODE_NULL, device=None)
        self.pcm_sink = None
        #: The recorded announcement this Store is playing, if any. Outlives
        #: every broadcast session, because that is the whole point of it.
        self._announcement = None
        self._announcement_volume_percent = 80
        # Newest output-volume command applied. Monotonic within a
        # session; a command that is not strictly newer is dropped.
        self._last_audio_command_id = 0

        # ---- Windows endpoint master control ------------------------------
        #: The STABLE Core Audio endpoint id this Receiver may control, or
        #: None for an installation that has not re-selected its output since
        #: master control existed. None means "unsupported", never "guess".
        self.windows_endpoint_id = None
        #: Captured once, before the first mutation of this broadcast.
        self._endpoint_original = None
        #: Where the crash-recovery record lives for this run.
        self._endpoint_record_path = None
        #: Test seam. None means real Core Audio.
        self._endpoint_backend = None
        #: The level HQ last asked for, so unmute can return to it.
        self._endpoint_volume_percent = 100
        #: Watches the endpoint and reports what it is ACTUALLY doing, so HQ
        #: sees a change made at the till and not only its own commands.
        self._endpoint_observer = None
        self._endpoint_state_sequence = 0
        #: Test seam for the observer, kept separate from the control backend
        #: so a test can fake one without the other.
        self._observer_backend = None
        self._audio_backend = audio_backend

        self.session_id: int | None = None
        #: Remembered from prepare so resume can rebuild the same
        #: participation without HQ having to re-send a whole prepare.
        self.store_id: int | None = None
        #: True between a stand_down and the resume that answers it. The
        #: session is still ours; the output device is not open.
        self.stood_down = False
        self.decoder: FfmpegDecoder | None = None
        self.pcm_sink: WindowsPcmSink | None = None
        self.queue: StoreAudioQueue | None = None
        self._sequence = 0
        self._states: list[str] = []

        self.report: dict = {
            "started_at_utc": _utc_now(),
            "protocol_version": PROTOCOL_VERSION,
            "ffmpeg_available": False,
            "codec_supported": False,
            **self.sink.as_dict(),
            "output_stream_open": "not_applicable",
            "output_frames_written": 0,
            "bounded_queue_capacity": queue_capacity,
            "connected": False,
            "ready": False,
            "audio_receiving": False,
            "playback_confirmed": False,
            "stopped": False,
            "stood_down": False,
            "resumed": False,
            "playback_error": False,
            "speaker_verified": False,
            "total_chunks": 0,
            "total_bytes": 0,
            "dropped_chunks": 0,
            "ffmpeg_returncode": None,
            "ffmpeg_decoded_microseconds": 0,
            "states": [],
            "overall_result": "AUDIO_RECEIVER_PILOT_FAILED",
        }

    # -- acknowledgement helpers -----------------------------------------
    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def _envelope(self, message_type: str) -> dict:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "type": message_type,
            "message_id": str(uuid.uuid4()),
            "occurred_at": _utc_now(),
            "sequence": self._next_sequence(),
        }

    def _record_state(self, state: str) -> None:
        self._states.append(state)
        self.report["states"] = list(self._states)

    async def _send(self, websocket, payload: dict) -> None:
        await websocket.send(json.dumps(payload))

    # -- lifecycle --------------------------------------------------------
    async def run(self) -> dict:
        import websockets

        token = _require_token()
        connection = await websockets.connect(
            self.ws_url,
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=15,
            max_size=4 * 1024 * 1024,
        )
        # The credential is not needed again; drop the local reference.
        del token
        self.report["connected"] = True
        self._record_state("CONNECTED")

        await self._announce_self(connection)

        # Report changes made at the till back to HQ.
        #
        # Everything this needs was already here - the Core Audio observer, the
        # coalescing, the sequence counter, the endpoint_state message, the
        # backend handler and the Console's display of it - and none of it ran,
        # because nothing ever started this loop. A Store employee turning the
        # volume down was observed, coalesced, and then sat in the observer's
        # single slot until it was overwritten by the next change. HQ went on
        # showing its own last command, which is exactly what the operator saw.
        endpoint_state = asyncio.create_task(self._endpoint_state_loop(connection))
        try:
            await self._session_loop(connection)
        finally:
            for task in (heartbeat, endpoint_state):
                task.cancel()
            for task in (heartbeat, endpoint_state):
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._shutdown(connection)

        self.report["ended_at_utc"] = _utc_now()
        if (
            self.report["ready"]
            and self.report["audio_receiving"]
            and self.report["playback_confirmed"]
            and self.report["stopped"]
            and not self.report["speaker_verified"]
        ):
            self.report["overall_result"] = "AUDIO_RECEIVER_PILOT_PASSED"

        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(
                json.dumps(self.report, indent=2, sort_keys=True), encoding="utf-8"
            )
        return self.report

    async def _heartbeat_loop(
        self, connection, interval: float = HEARTBEAT_SECONDS
    ) -> None:
        """Prove liveness while idle. It never claims readiness or playback.

        The session loop owns connection failures, so a send error here just
        ends this task quietly rather than racing it with a second exception.
        """
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send(connection, self._envelope("heartbeat"))
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    async def _session_loop(self, connection) -> None:
        import websockets

        while True:
            try:
                message = await connection.recv()
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                return

            if isinstance(message, (bytes, bytearray)):
                await self._on_audio(connection, bytes(message))
                continue

            try:
                payload = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue

            kind = payload.get("type")
            if kind == "prepare":
                await self._on_prepare(connection, payload)
            elif kind == "stop":
                await self._on_stop(connection, payload)
                return
            elif kind == "stand_down":
                # NOT a return. Standing down goes quiet and stays in the
                # session loop - that is the whole difference from stop, and
                # returning here would close the socket and turn every Pause
                # into a dropout.
                await self._on_stand_down(connection, payload)
            elif kind == "resume":
                await self._on_resume(connection, payload)
            elif kind == "set_audio_control":
                await self._on_set_audio_control(connection, payload)
            # Announcements and speaker selection. Handled in the same loop
            # because they arrive on the same connection and, unlike every
            # verb above, are meaningful when NO broadcast is running - which
            # is most of the time.
            elif kind == "announcement_play":
                await self._on_announcement_play(connection, payload)
            elif kind == "announcement_pause":
                await self._on_announcement_pause(connection, payload)
            elif kind == "announcement_stop":
                await self._on_announcement_stop(connection, payload)
            elif kind == "announcement_set_volume":
                await self._on_announcement_set_volume(connection, payload)
            elif kind == "list_output_devices":
                await self._on_list_output_devices(connection, payload)
            elif kind == "set_output_device":
                await self._on_set_output_device(connection, payload)
            # Unknown control messages are ignored rather than acted upon.

    # ------------------------------------------------------------------
    # Recorded announcements and remote speaker selection
    #
    # These run OUTSIDE any broadcast session, which is most of the time. They
    # deliberately touch no session state: an announcement is not a broadcast,
    # and confusing the two is how a Store ends up reporting playback for a
    # session that does not exist.
    # ------------------------------------------------------------------

    def _announcement_credential(self) -> str:
        return getattr(self, "_credential", "") or ""

    def _announcement_backend_url(self) -> str:
        """The HTTP origin of the HQ this Store is already connected to.

        Derived from the socket URL rather than configured separately, so a
        Store cannot end up fetching recordings from one HQ while taking
        commands from another.
        """
        url = self.ws_url or ""
        if url.startswith("wss://"):
            return "https://" + url[len("wss://"):].split("/", 1)[0]
        if url.startswith("ws://"):
            return "http://" + url[len("ws://"):].split("/", 1)[0]
        return url

    def _announcement_state_root(self):
        from pathlib import Path

        root = getattr(self, "state_root", None)
        if root is not None:
            return Path(root)
        return Path(tempfile.gettempdir()) / "speaklink-receiver"

    def _ensure_pcm_sink(self, user: str = "announcement"):
        """The speaker, open, whether or not a broadcast is running.

        THE BUG THIS ENDS

        `pcm_sink` was opened when a BROADCAST was prepared and closed when it
        stood down. An announcement arriving in between found it None, wrote
        its decoded audio into nothing, and the Receiver then told HQ
        "announcement_playing" - so the console showed a shop playing a
        promotion that could not have made a sound. Every layer was honest
        about what it had been told and nobody was holding the speaker.

        A recorded announcement runs for days with nobody present; a broadcast
        is somebody holding a microphone. The shop's speaker cannot belong
        only to the second one.

        Returns None in software/no-hardware modes, which is what the sink
        already meant there.
        """
        users = self._sink_users()
        if self.pcm_sink is not None:
            users.add(user)
            return self.pcm_sink
        if not getattr(self.sink, "is_hardware", False):
            # No hardware sink in this mode, and saying so is not the same as
            # opening one. The caller decides what that means for it.
            return None
        opened = WindowsPcmSink(self.sink, backend=self._audio_backend)
        opened.open()
        self.pcm_sink = opened
        users.add(user)
        return opened

    def _sink_users(self) -> set:
        """Who is using the speaker right now.

        A SET, not a flag. The first version remembered "the announcement
        opened it" in a boolean, which cannot survive a broadcast opening a
        second sink underneath it - and then named the wrong object when it
        came time to close one.
        """
        current = getattr(self, "_pcm_sink_users", None)
        if current is None:
            current = set()
            self._pcm_sink_users = current
        return current

    def _release_pcm_sink(self, user: str) -> None:
        """Let go. The speaker closes when the last user does.

        A Store with nothing to play must not hold an output device open -
        that is what stops somebody else's application using it - but a
        broadcast ending is not a reason to close a device an announcement is
        still writing into.
        """
        users = self._sink_users()
        users.discard(user)
        if users:
            return
        if self.pcm_sink is not None:
            try:
                self.pcm_sink.close()
            except Exception:  # noqa: BLE001
                pass
            self.pcm_sink = None

    def _release_announcement_sink(self) -> None:
        """The announcement lets go of the speaker."""
        self._release_pcm_sink("announcement")

    async def _on_announcement_play(self, connection, payload: dict) -> None:
        from tools import announcement_player

        volume = payload.get("volume_percent", 80)
        try:
            path = await asyncio.to_thread(
                announcement_player.fetch_if_absent,
                state_root=self._announcement_state_root(),
                sha256=str(payload.get("sha256") or ""),
                download_path=payload.get("download_path"),
                backend_url=self._announcement_backend_url(),
                credential=self._announcement_credential(),
            )
        except Exception as failure:  # noqa: BLE001
            # Reported, never swallowed. A Store that cannot fetch a recording
            # is silent, and silence is the failure HQ cannot see for itself:
            # without this message the console would show PLAYING for a shop
            # playing nothing.
            await self._send(connection, {
                "type": "announcement_failed",
                "audio_id": payload.get("audio_id"),
                "error": str(failure)[:500],
            })
            return

        if self._announcement is not None:
            self._announcement.stop()
            # Cleared, not merely stopped. Leaving the object behind after a
            # failure below made every later broadcast teardown skip closing
            # its sink, for the life of the process.
            self._announcement = None
        self._announcement_volume_percent = volume

        try:
            # OFF the event loop. Opening a Windows audio endpoint blocks, and
            # the switch path already runs it in a thread for that reason - on
            # the loop it stalls this Receiver's socket, live broadcast audio
            # and heartbeats included, so an announcement starting could get
            # the Store marked offline.
            sink = await asyncio.to_thread(self._ensure_pcm_sink, "announcement")
        except Exception as failure:  # noqa: BLE001
            # Said out loud rather than played into nothing. HQ can then show
            # the shop as unheard instead of as playing.
            await self._send(connection, {
                "type": "announcement_failed",
                "audio_id": payload.get("audio_id"),
                "error": f"the speaker could not be opened: {failure}"[:500],
            })
            return

        if sink is None:
            # This Receiver has no audio output to write to. Saying "playing"
            # here is the lie the whole of this feature has been chasing.
            await self._send(connection, {
                "type": "announcement_failed",
                "audio_id": payload.get("audio_id"),
                "error": "this Receiver has no audio output configured, so "
                         "the recording cannot be played here.",
            })
            return

        self._announcement = announcement_player.AnnouncementPlayback(
            path=path, sink=sink, volume_percent=volume)
        self._apply_master_volume(volume)
        self._announcement.start()

        # WAIT FOR THE FIRST FRAME THE SPEAKER ACCEPTED.
        #
        # "playing" used to be sent the instant the thread was started, which
        # is a claim about intent, not about sound. A decoder that dies on its
        # first read and a sink that refuses every write both looked identical
        # to success from here - and that is how a silent shop was reported as
        # playing for a whole day.
        #
        # Two seconds is far longer than starting ffmpeg and writing one
        # buffer, and far shorter than an operator wondering whether the
        # button worked.
        started = await asyncio.to_thread(
            self._announcement.wait_until_audible, 2.0)
        if not started:
            reason = (self._announcement.failure_reason()
                      or "the recording did not start playing on this computer")
            self._announcement.stop()
            self._announcement = None
            self._release_announcement_sink()
            await self._send(connection, {
                "type": "announcement_failed",
                "audio_id": payload.get("audio_id"),
                "error": reason[:500],
            })
            return

        await self._send(connection, {
            "type": "announcement_playing",
            "audio_id": payload.get("audio_id"),
            "template_id": payload.get("template_id"),
        })

    async def _on_announcement_pause(self, connection, payload: dict) -> None:
        if self._announcement is not None:
            self._announcement.pause()
        await self._send(connection, {
            "type": "announcement_paused",
            "reason": payload.get("reason") or "hq",
        })

    async def _on_announcement_stop(self, connection, payload: dict) -> None:
        if self._announcement is not None:
            self._announcement.stop()
            self._announcement = None
        # And let the speaker go, if the announcement is what opened it. A
        # Store with nothing to play should not hold an output device open -
        # that is what stops somebody else's application using it.
        self._release_announcement_sink()
        await self._send(connection, {"type": "announcement_stopped"})

    async def _on_announcement_set_volume(self, connection, payload: dict) -> None:
        """Level only, without restating what is playing.

        Restating would restart the recording, so the jingle would jump back to
        its first word every time somebody nudged the slider.
        """
        try:
            percent = int(payload.get("volume_percent"))
        except (TypeError, ValueError):
            return
        self._announcement_volume_percent = max(0, min(100, percent))
        if self._announcement is not None:
            self._announcement.set_volume(self._announcement_volume_percent)
        # AND THE SHOP'S MASTER VOLUME.
        #
        # Asked for twice from the estate: this slider is what an operator
        # reaches for when a shop is too loud, and scaling only our own
        # samples left the Windows master exactly where it was - so the shop's
        # own music stayed loud and the change looked like it had done
        # nothing.
        #
        # NOT restored afterwards, unlike the broadcast's ducking of this same
        # control: a broadcast borrows the volume for a minute, whereas this
        # is somebody deciding how loud this shop should be.
        self._apply_master_volume(self._announcement_volume_percent)

    async def _report_store_volume(self, connection, percent, muted) -> bool:
        """Tell HQ what the shop's speaker is set to. False if the socket died."""
        self._last_reported_volume = (percent, bool(muted))
        try:
            await self._send(connection, {
                "type": "store_volume",
                "volume_percent": percent,
                "muted": bool(muted),
            })
        except Exception:  # noqa: BLE001 - the session loop owns reconnection
            return False
        return True

    #: How often the shop's level is READ, as a backstop to the callback.
    #:
    #: The observer's own wait coalesces at 150ms, which is right for a dial
    #: being dragged and far too fast for a property read every time nothing
    #: happened. Three seconds is quicker than anybody walks back to the till
    #: and slow enough to be free.
    STORE_VOLUME_POLL_SECONDS = 3.0

    async def _report_store_volume_if_changed(self, connection,
                                              *, force: bool = False) -> bool:
        """Read the endpoint and report only a change.

        Cheap - one Core Audio property read - and silent when nothing moved,
        so an idle Store costs one local call every few seconds and no traffic
        at all.
        """
        now = time.monotonic()
        if not force:
            due = getattr(self, "_next_volume_poll", 0.0)
            if now < due:
                return True
        self._next_volume_poll = now + self.STORE_VOLUME_POLL_SECONDS

        endpoint_id = self._ensure_windows_endpoint()
        if not endpoint_id:
            return True
        try:
            from tools import windows_endpoint_volume as volume
        except ImportError:  # pragma: no cover - not on Windows
            return True
        try:
            state = await asyncio.to_thread(
                volume.read_state, endpoint_id,
                backend=getattr(self, "_endpoint_backend", None))
        except Exception:  # noqa: BLE001
            return True
        current = (state.volume_percent, bool(state.muted))
        if current == getattr(self, "_last_reported_volume", None):
            return True
        return await self._report_store_volume(connection, *current)

    async def _announce_self(self, connection) -> None:
        """Everything a Receiver says the moment it is connected.

        ONE method because there are TWO connect paths: this class's `run()`
        and `DeviceReceiverSession.run()`, which is the one a real Store runs.
        Adding this to the first only meant a shop reported nothing at all,
        and the console accused it of running an old build when it was
        running today's.

        Nothing here is allowed to stop the connection: a Receiver that cannot
        say its version still has to carry audio.
        """
        try:
            await self._send(connection, {
                "type": "receiver_version",
                "version": _receiver_version(),
            })
        except Exception:  # noqa: BLE001
            logger.debug("Could not report the Receiver version", exc_info=True)

        try:
            # Watch the shop's own speaker from the moment we know which one
            # it is. A Store playing announcements all day never prepares a
            # broadcast, and used to be unwatched all day.
            self._ensure_windows_endpoint()
            self._start_endpoint_observer()
            # The first reading immediately, so the console is right before
            # anybody touches anything.
            await self._report_store_volume_if_changed(connection, force=True)
        except Exception:  # noqa: BLE001
            logger.debug("Could not start the endpoint observer", exc_info=True)

    def _ensure_windows_endpoint(self) -> str | None:
        """The Core Audio endpoint for the speaker this Store is set to.

        WHY THIS IS RESOLVED HERE AND NOT ONLY READ FROM CONFIG

        `windows_endpoint_id` is written into the Receiver's config when
        somebody picks an output in Store Setup. A Store set up before that
        existed - or upgraded from one - has a perfectly good output device
        and no endpoint id, and everything that needs the id then silently
        does nothing:

          * the volume set from HQ never reaches the shop's master;
          * a change made at the till is never reported back.

        Both were reported from a live shop, and both looked like features
        that did not work rather than a field that was never filled in. The
        device name is right there in the sink configuration, and Core Audio
        can turn it into an endpoint id, so this asks rather than giving up.

        Cached on the instance: it is a property of this computer's hardware,
        not of the request.
        """
        existing = getattr(self, "windows_endpoint_id", None)
        if existing:
            return existing
        device = getattr(getattr(self, "sink", None), "device", None)
        name = getattr(device, "name", None) or getattr(device, "selector", None)
        if not name:
            return None
        try:
            from tools import windows_endpoint_volume as volume
        except ImportError:  # pragma: no cover - not on Windows
            return None
        try:
            resolved = volume.resolve_endpoint_for_playback_device(
                str(name), backend=getattr(self, "_endpoint_backend", None))
        except Exception:  # noqa: BLE001
            logger.info("Could not resolve a Core Audio endpoint for %r; the "
                        "master volume cannot be read or set on this Store.",
                        str(name)[:80])
            return None
        endpoint_id = getattr(resolved, "endpoint_id", None) or resolved
        self.windows_endpoint_id = endpoint_id
        logger.info("Resolved this Store's audio endpoint from its output "
                    "device; master volume is now controllable.")
        return endpoint_id

    def _apply_master_volume(self, percent: int) -> None:
        """Set the Windows endpoint's own volume for this Store.

        The same control, backend and endpoint id the broadcast path already
        uses - not a second way of doing it.

        Best effort and never fatal: a Store whose master volume cannot be set
        still plays the announcement, and raising here would stop the audio
        over a control that is not the audio.
        """
        endpoint_id = self._ensure_windows_endpoint()
        if not endpoint_id:
            return
        try:
            from tools import windows_endpoint_volume as volume
        except ImportError:  # pragma: no cover - not on Windows
            return
        try:
            volume.apply_state(endpoint_id, volume_percent=percent,
                               muted=False,
                               backend=getattr(self, "_endpoint_backend", None))
        except Exception:  # noqa: BLE001
            logger.warning("Could not set the master volume for this Store to "
                           "%s%%; the announcement is still scaled in software.",
                           percent)

    async def _on_list_output_devices(self, connection, payload: dict) -> None:
        """Tell HQ what this computer actually has.

        Sent whole every time rather than as a change: HQ replaces its list
        with this one, so a speaker that has been unplugged DISAPPEARS from
        what an operator may choose. Offering an endpoint that is no longer
        there is exactly the mistake that leaves a shop silent.
        """
        from tools.windows_audio_devices import AudioDeviceError, list_output_devices

        try:
            found = await asyncio.to_thread(list_output_devices)
            devices = [{**device.as_dict(),
                        "looks_wireless": device.looks_wireless}
                       for device in found]
        except AudioDeviceError as failure:
            await self._send(connection, {
                "type": "output_devices", "devices": [],
                "error": str(failure)[:500]})
            return
        await self._send(connection, {"type": "output_devices",
                                      "devices": devices})

    async def _on_set_output_device(self, connection, payload: dict) -> None:
        """Switch speaker, and say which one this computer ended up on.

        Resolved BEFORE anything changes, with the same resolver the setup
        wizard uses - so HQ and the Store cannot disagree about what a
        selector means, and an ambiguous one fails closed rather than picking
        something.

        The reply names the device rather than merely reporting success.
        Nobody at HQ can hear the result, and "applied" on its own is not an
        answer to "which speaker is the shop on".
        """
        from tools.windows_audio_devices import (
            AudioDeviceError, resolve_output_device)

        try:
            device = await asyncio.to_thread(resolve_output_device,
                                             payload.get("selector"))
        except AudioDeviceError as failure:
            await self._send(connection, {
                "type": "output_device_result", "result": "refused",
                "error": str(failure)[:500]})
            return

        try:
            await asyncio.to_thread(self._switch_output_device, device)
        except Exception as failure:  # noqa: BLE001
            await self._send(connection, {
                "type": "output_device_result", "result": "refused",
                "error": f"that speaker could not be opened: {failure}"[:500]})
            return

        await self._send(connection, {
            "type": "output_device_result", "result": "applied",
            "applied_selector": device.verified_selector,
            "applied_device_name": device.name,
        })

    def _switch_output_device(self, device) -> None:
        """Open the new speaker, then remember it.

        The order matters and is the whole of this method. The new device is
        opened FIRST: if it cannot be opened, the Store keeps playing through
        the one it has and reports a refusal, rather than being left with
        nothing while HQ believes the change worked.

        Saving it to the configuration is not an afterthought either. Without
        that, the next restart silently returns the shop to its old speaker -
        and it would do so hours later, with nobody connecting the silence to
        a change made from HQ that morning.
        """
        replacement = SinkConfiguration(sink_mode=SINK_MODE_WINDOWS,
                                        device=device)
        # With this Receiver's audio backend, like every other sink here. It
        # was the one call site that left it out, so a Store silently fell
        # back to the default backend after a change made from HQ.
        opened = WindowsPcmSink(replacement, backend=self._audio_backend)
        # `open()`, which is what this class has. It was `start()` - a method
        # that does not exist on it - so every remote speaker change failed at
        # the first line with an AttributeError, and HQ reported it, correctly
        # and uselessly, as "the Store refused that change".
        #
        # Nothing caught it because the only tests of this path handed it a
        # fake sink, and a fake answers to whatever it is asked. See the
        # contract test beside them: the double now has to have the same
        # surface as the real class.
        opened.open()

        previous = self.pcm_sink
        self.sink = replacement
        self.pcm_sink = opened
        # POINTED AT THE NEW SPEAKER BEFORE THE OLD ONE CLOSES.
        #
        # The playback holds its sink directly, so closing first left its pump
        # writing into a closed device: the announcement went silent, nothing
        # was reported, and HQ went on showing it as playing.
        if self._announcement is not None:
            self._announcement._sink = opened
        if previous is not None:
            try:
                previous.close()
            except Exception:  # noqa: BLE001
                pass

        self._remember_output_device(device)

    def _remember_output_device(self, device) -> None:
        """Persist the selection, so a restart does not undo it."""
        config_path = getattr(self, "config_path", None)
        if config_path is None:
            return
        try:
            from tools.receiver_agent import load_config, save_config

            config = load_config(config_path)
            save_config(config_path,
                        replace(config,
                                audio_output_device=device.verified_selector))
        except Exception:  # noqa: BLE001
            logger.warning(
                "The speaker was changed but could not be saved to %s, so a "
                "restart will return this Store to its previous one.",
                config_path)

    async def _on_prepare(self, connection, payload: dict) -> None:
        try:
            prepare = parse_prepare_message(payload)
        except Exception:
            await self._send(connection, {
                **self._envelope("device_error"),
                "error_code": "UNSUPPORTED_AUDIO_FORMAT",
                "details": "the prepare message was rejected",
                "recoverable": False,
            })
            self._record_state("DEVICE_ERROR")
            return

        # Real readiness checks. READY is never claimed without these.
        has_ffmpeg = ffmpeg_available()
        has_codec = opus_webm_decode_supported()
        self.report["ffmpeg_available"] = has_ffmpeg
        self.report["codec_supported"] = has_codec

        if not has_ffmpeg or not has_codec:
            await self._send(connection, {
                **self._envelope("device_error"),
                "error_code": "FFMPEG_OR_CODEC_UNAVAILABLE",
                "details": "required decode support is missing",
                "recoverable": False,
            })
            self._record_state("DEVICE_ERROR")
            return

        # In hardware mode the selected device must actually open before READY
        # can be claimed. A device that cannot be opened is a DEVICE_ERROR.
        pcm_sink = None
        if self.sink.is_hardware:
            try:
                # THE SAME speaker the announcement uses, through the same
                # register. Opening a second one here left the first orphaned
                # with a pump still writing into it.
                pcm_sink = self._ensure_pcm_sink("broadcast")
            except SinkConfigurationError:
                await self._send(connection, {
                    **self._envelope("device_error"),
                    "error_code": "OUTPUT_DEVICE_UNAVAILABLE",
                    "details": "the selected Windows output device could not be opened",
                    "recoverable": False,
                })
                self.report["output_stream_open"] = "failed"
                self._record_state("DEVICE_ERROR")
                return
            self.report["output_stream_open"] = "ok"

        self.session_id = prepare.broadcast_session_id
        self.store_id = prepare.target_store_id
        self.stood_down = False
        self.queue = StoreAudioQueue(store_id=prepare.target_store_id,
                                     capacity=self.queue_capacity)
        self.decoder = FfmpegDecoder(sink_mode=self.sink.sink_mode, pcm_sink=pcm_sink)
        self.decoder.start()

        # ---- Windows endpoint master control ------------------------------
        #
        # Done BEFORE receiver_ready is sent, because READY is what tells HQ
        # this Store is fit to broadcast to - and a Store whose output is muted
        # at the Windows level is not. If the endpoint cannot be prepared, the
        # capability is not claimed and the failure is reported rather than
        # being discovered later as silence.
        endpoint_ready = await self._prepare_windows_endpoint(connection)
        if endpoint_ready is False:
            return

        # Capabilities are reported HONESTLY, from what this run can actually
        # do rather than from the build version. Master control needs a stable
        # endpoint id captured when the output was selected; an installation
        # that has not re-selected since this feature shipped has none, and
        # says so instead of claiming a control that would act on a guess.
        # Four states, because "unsupported" was true for two very different
        # situations and only one of them was this software's fault.
        controllable = self.windows_endpoint_id is not None
        if not controllable:
            # A current build whose Store has not re-selected its output since
            # upgrading. Thirty seconds in Store Setup, not a new Store Kit.
            status = "needs_output_selection"
        elif endpoint_ready is False:
            status = "unavailable"
        else:
            status = "ready"
        await self._send(connection, {
            **self._envelope("receiver_ready"),
            "software_checks_passed": True,
            "output_device_checks_passed": True,
            "capabilities": {
                "output_volume": controllable,
                "output_mute": controllable,
                "output_control_status": status,
            },
        })
        self.report["ready"] = True
        self._record_state("READY")

    # -- Windows endpoint master control ----------------------------------
    def _endpoint_module(self):
        from tools import windows_endpoint_volume

        return windows_endpoint_volume

    async def _prepare_windows_endpoint(self, connection) -> "bool | None":
        """Capture, persist, unmute and open this broadcast's output.

        Returns False only when the run must stop; None when there is nothing
        to control, which is a normal state for an installation that has not
        re-selected its output yet.
        """
        if not self.windows_endpoint_id:
            return None

        volume = self._endpoint_module()
        try:
            original = volume.read_state(self.windows_endpoint_id,
                                         backend=self._endpoint_backend)
        except Exception as failure:
            await self._send(connection, {
                **self._envelope("device_error"),
                "error_code": "OUTPUT_ENDPOINT_UNAVAILABLE",
                "details": "the selected Windows audio output could not be read",
                "recoverable": True,
            })
            self._record_state("DEVICE_ERROR")
            self.report["windows_endpoint_error"] = str(failure)[:200]
            return False

        # Persisted BEFORE the first mutation. If the machine loses power one
        # instruction later, the next start finds this and puts it back.
        self._endpoint_original = original
        if self._endpoint_record_path is not None:
            from tools import windows_endpoint_restore

            windows_endpoint_restore.write_record(
                self._endpoint_record_path,
                session_id=self.session_id or 0,
                endpoint_id=self.windows_endpoint_id,
                original_volume_percent=original.volume_percent,
                original_muted=original.muted,
            )

        try:
            applied = volume.apply_state(
                self.windows_endpoint_id,
                volume_percent=self._endpoint_volume_percent,
                muted=False,
                backend=self._endpoint_backend,
            )
        except Exception as failure:
            await self._send(connection, {
                **self._envelope("device_error"),
                "error_code": "OUTPUT_ENDPOINT_CONTROL_FAILED",
                "details": "the selected Windows audio output could not be prepared",
                "recoverable": True,
            })
            self._record_state("DEVICE_ERROR")
            self.report["windows_endpoint_error"] = str(failure)[:200]
            return False

        self.report["windows_endpoint_prepared"] = {
            "original_volume_percent": original.volume_percent,
            "original_muted": original.muted,
            "applied_volume_percent": applied.volume_percent,
            "applied_muted": applied.muted,
        }

        # Observation begins HERE and ends at restoration: SpeakLink has no
        # business watching a shop's mixer when it is not broadcasting to it.
        # Started AFTER the original state has been captured and persisted, so
        # the observer's own reading can never be mistaken for the
        # pre-broadcast snapshot - the one thing that must never come from
        # telemetry.
        self._start_endpoint_observer()
        return True

    def _start_endpoint_observer(self) -> bool:
        """Watch the saved endpoint whenever we know which one it is.

        It used to start at PREPARE and stop at restoration, so a shop turning
        its own volume down was invisible to HQ unless a broadcast happened to
        be running - which, for a shop playing recorded announcements all day,
        is almost never. The console then showed a level nobody had touched in
        hours as though it were the shop's.
        """
        if not self.windows_endpoint_id:
            # Nothing safe to watch. Falling back to the Windows DEFAULT
            # endpoint would silently observe - and later control - whatever
            # output happens to be default today, which may be a headset
            # somebody plugged in.
            self.report["endpoint_observer"] = "not configured"
            return False
        if self._endpoint_observer is not None and self._endpoint_observer.started:
            return True
        try:
            from tools import windows_endpoint_observer

            self._endpoint_observer = windows_endpoint_observer.EndpointObserver(
                self.windows_endpoint_id, backend=self._observer_backend)
            self._endpoint_observer.start()
            self.report["endpoint_observer"] = "watching"
            return True
        except Exception as failure:
            # A Receiver that cannot watch can still CONTROL the endpoint. HQ
            # simply will not learn about changes made at the till, which is a
            # degradation worth recording rather than a reason to refuse to
            # broadcast.
            self._endpoint_observer = None
            self.report["endpoint_observer"] = "unavailable: " + str(failure)[:120]
            return False

    def restore_windows_endpoint(self) -> dict:
        """Put the Store's own volume and mute back. Safe to call twice.

        Called from every path that ends a broadcast, which is why it is
        idempotent and never raises: an exception here would turn a clean stop
        into a crash and leave the shop at the announcement level.
        """
        # Detach the observer FIRST. Once this broadcast is over SpeakLink has
        # no business watching the shop's mixer, and a COM callback left
        # attached would keep a finished session's object alive and could
        # report the restoration itself into the NEXT broadcast as though
        # somebody had moved the slider.
        if self._endpoint_observer is not None:
            self._endpoint_observer.stop()
            self._endpoint_observer = None

        original = self._endpoint_original
        if original is None or not self.windows_endpoint_id:
            return {"restored": False, "reason": "nothing was changed"}
        volume = self._endpoint_module()
        try:
            applied = volume.apply_state(
                self.windows_endpoint_id,
                volume_percent=original.volume_percent,
                muted=original.muted,
                backend=self._endpoint_backend,
            )
        except Exception as failure:
            # The record is deliberately NOT cleared, so the next start tries
            # again rather than forgetting a shop left loud.
            return {"restored": False, "reason": str(failure)[:200]}

        self._endpoint_original = None
        if self._endpoint_record_path is not None:
            from tools import windows_endpoint_restore

            windows_endpoint_restore.clear_record(self._endpoint_record_path)
        self.report["windows_endpoint_restored"] = {
            "volume_percent": applied.volume_percent, "muted": applied.muted}
        return {"restored": True, "volume_percent": applied.volume_percent,
                "muted": applied.muted}

    async def _endpoint_state_loop(self, connection) -> None:
        """Report what the endpoint is actually doing, coalesced.

        A Windows slider drag emits a notification per step, so this takes only
        the LATEST reading each time round and drops the intermediates. Nobody
        needs to know the slider passed through 43 on its way to 55, and one
        noisy Store must not be able to fill a socket that is also carrying
        audio. The observer keeps a single slot rather than a queue, so this
        cannot grow without bound however fast the changes arrive.
        """
        from tools import windows_endpoint_observer

        while True:
            observer = self._endpoint_observer
            if observer is None or not observer.started:
                # NO OBSERVER IS NOT A REASON TO STOP LOOKING.
                #
                # This used to sleep and `continue`, which skipped the read
                # below - so on a computer where Core Audio's change
                # notification never arrives, or where the observer could not
                # start at all, HQ received exactly one level (the one taken
                # at connect) and never another. A shop moving its dial from
                # 83 to 100 changed nothing on the console, and the console
                # was showing a real reading, which is the most convincing way
                # to be wrong.
                #
                # The read is throttled to a few seconds and costs one local
                # property call, so a Store with no observer is a Store that
                # reports a little later rather than not at all.
                await asyncio.sleep(windows_endpoint_observer.COALESCE_SECONDS)
                if not await self._report_store_volume_if_changed(connection):
                    return
                continue
            # Waking on the event rather than polling: the wait returns
            # immediately when something changed and otherwise costs nothing.
            await asyncio.to_thread(
                observer.wait_for_change,
                windows_endpoint_observer.COALESCE_SECONDS)
            reading = observer.take()
            if reading is None:
                # NOTHING FROM THE CALLBACK. That is usually because nothing
                # changed - and sometimes because the Core Audio notification
                # never arrived at all: the callback is delivered on an
                # apartment this process does not control, and a Store where
                # it goes missing looks exactly like a Store where nobody
                # touched the dial.
                #
                # So the level is also READ, on a slow beat, and reported when
                # it differs from what HQ was last told. Polling as a backstop
                # to an event, not instead of one: the callback still gives an
                # immediate answer when it works.
                await self._report_store_volume_if_changed(connection)
                continue
            if self.session_id is None:
                # OUTSIDE a broadcast this is still worth saying. It carries no
                # session and no sequence, because there is no session to
                # attribute it to - it is simply what the speaker is set to
                # now, which is exactly what the Announcements console has to
                # show while a recording plays all day.
                if not await self._report_store_volume(
                        connection, reading.volume_percent, reading.muted):
                    return
                continue
            self._endpoint_state_sequence += 1
            try:
                await self._send(connection, {
                    **self._envelope("endpoint_state"),
                    "session_id": self.session_id,
                    "state_sequence": self._endpoint_state_sequence,
                    "volume_percent": reading.volume_percent,
                    "muted": reading.muted,
                })
            except Exception:
                # The socket has gone. The outer session loop owns reconnection.
                return
            self.report["last_endpoint_state"] = {
                "volume_percent": reading.volume_percent, "muted": reading.muted}

    async def _on_set_audio_control(self, connection, payload: dict) -> None:
        """Apply an HQ output-volume command and report what really happened.

        Every path here ends in exactly one acknowledgement carrying the
        command_id it answers, because HQ resolves ordering by that id: a
        silent failure would leave a Store pending for ever, and an
        acknowledgement without the id could be mistaken for an answer to a
        newer command.

        The command is whole state rather than a delta, so an older one that
        arrives late can simply be dropped - the newest already says everything
        the Store should be doing.
        """
        try:
            command = parse_set_audio_control_message(payload)
        except AudioProtocolError:
            # Malformed and therefore unanswerable: there is no trustworthy
            # command_id to acknowledge against, and inventing one would let a
            # corrupt message overwrite a good command's state.
            return

        session_id = command["session_id"]
        command_id = command["command_id"]

        # A command for a session this Receiver is not running. Ignored rather
        # than applied: Store output control exists only inside a broadcast, so
        # a command naming no session - or a finished one - must never change
        # the level of the announcement that is on air now.
        if self.session_id is None or session_id != self.session_id:
            return

        # Older than something already applied - a late arrival that a newer
        # command has superseded.
        if command_id <= self._last_audio_command_id:
            return
        self._last_audio_command_id = command_id

        base = {
            **self._envelope("audio_control"),
            "session_id": session_id,
            "command_id": command_id,
            "requested_volume_percent": command["volume_percent"],
            "requested_muted": command["muted"],
        }
        device = self.sink.device.selector if self.sink.device else None

        # The HQ slider is the WINDOWS ENDPOINT MASTER now, not a PCM gain.
        # An installation with no stable endpoint id cannot be controlled
        # safely - acting on a PortAudio index could move the wrong output -
        # so it reports unsupported and changes nothing.
        if not self.windows_endpoint_id:
            await self._send(connection, {
                **base,
                "result": "unsupported",
                "output_device": device,
                "error_code": "OUTPUT_ENDPOINT_NOT_CONFIGURED",
                "details": ("this Receiver has no Windows audio output selected "
                            "for master control; re-select it in Store Setup"),
            })
            return

        volume = self._endpoint_module()
        # Mute is the endpoint's own mute, and the chosen level survives it so
        # unmuting returns to what HQ last asked for rather than to full.
        if command["volume_percent"] is not None:
            self._endpoint_volume_percent = command["volume_percent"]
        try:
            applied = volume.apply_state(
                self.windows_endpoint_id,
                volume_percent=self._endpoint_volume_percent,
                muted=command["muted"],
                backend=self._endpoint_backend,
            )
        except Exception as failure:
            await self._send(connection, {
                **base,
                "result": "failed",
                "output_device": device,
                "error_code": "OUTPUT_CONTROL_FAILED",
                "details": "the Windows output master volume could not be applied",
            })
            self.report["windows_endpoint_error"] = str(failure)[:200]
            return

        # The value READ BACK from Windows, never the one that was asked for.
        # Windows can clamp and policy can override; "applied" has to mean the
        # endpoint reported it afterwards.
        await self._send(connection, {
            **base,
            "result": "applied",
            "applied_volume_percent": applied.volume_percent,
            "applied_muted": applied.muted,
            "output_device": device,
        })
        self.report["audio_control_applied"] = {
            "volume_percent": applied.volume_percent,
            "muted": applied.muted,
            "windows_endpoint": True,
        }

    async def _on_audio(self, connection, chunk: bytes) -> None:
        if self.session_id is None or self.decoder is None or self.queue is None:
            # Audio before prepare/ready is refused outright.
            return
        try:
            validate_audio_chunk(chunk)
        except InvalidAudioChunkError:
            return

        try:
            self.queue.enqueue(chunk)
        except StoreQueueClosedError:
            return

        try:
            queued = await asyncio.wait_for(self.queue.get(), timeout=1)
        except (asyncio.TimeoutError, StoreQueueClosedError):
            return

        self.report["total_chunks"] += 1
        self.report["total_bytes"] += len(queued)
        self.report["dropped_chunks"] = self.queue.dropped_count

        if not self.report["audio_receiving"]:
            await self._send(connection, {
                **self._envelope("audio_receiving"),
                "session_id": self.session_id,
            })
            self.report["audio_receiving"] = True
            self._record_state("AUDIO_RECEIVING")

        # Feeding FFmpeg can block briefly; keep it off the event loop.
        await asyncio.to_thread(self.decoder.feed, queued)

        # A hardware output stream that fails mid-session is a PLAYBACK_ERROR,
        # never a silent downgrade.
        if self.decoder.sink_failed and not self.report["playback_error"]:
            await self._send(connection, {
                **self._envelope("playback_error"),
                "error_code": "OUTPUT_STREAM_FAILED",
                "details": "the selected output device stopped accepting audio",
                "recoverable": False,
            })
            self.report["playback_error"] = True
            self._record_state("PLAYBACK_ERROR")
            return

        if not self.report["playback_confirmed"]:
            decoded = await asyncio.to_thread(self.decoder.wait_for_decode, 0.05)
            if decoded:
                await self._send(connection, {
                    **self._envelope("playback_confirmed"),
                    "session_id": self.session_id,
                })
                self.report["playback_confirmed"] = True
                self.report["ffmpeg_decoded_microseconds"] = self.decoder.decoded_microseconds
                self.report["output_frames_written"] = self.decoder.frames_written
                self._record_state("PLAYBACK_CONFIRMED")

    async def _on_stop(self, connection, payload: dict) -> None:
        session_id = payload.get("session_id") or self.session_id
        # The Store's own volume and mute go back FIRST, before anything that
        # could fail. Normal Stop, HQ Stop and Emergency Stop all arrive here,
        # so this one call covers all three - and a shop left at announcement
        # volume is the failure this whole feature has to avoid.
        self.restore_windows_endpoint()
        if self.decoder is not None:
            returncode = await asyncio.to_thread(self.decoder.close)
            self.report["ffmpeg_returncode"] = returncode
            self.report["ffmpeg_decoded_microseconds"] = self.decoder.decoded_microseconds
        if self.pcm_sink is not None:
            self.pcm_sink.close()
            self.report["output_frames_written"] = self.pcm_sink.frames_written
        if self.queue is not None:
            self.queue.close()

        if isinstance(session_id, int) and session_id > 0:
            await self._send(connection, {
                **self._envelope("stopped"),
                "session_id": session_id,
                "reason": "operator_stop",
            })
            self.report["stopped"] = True
            self._record_state("STOPPED")

    async def _on_stand_down(self, connection, payload: dict) -> None:
        """Go quiet, give the shop its volume back, and stay in the Broadcast.

        Everything stop tears down is torn down here - the decoder, the output
        device, the queue, the Windows endpoint override - because a paused
        shop must not be holding a device it is not using, and must not be
        sitting at announcement volume while its own music plays.

        What is NOT given up is the session. self.session_id stays, so resume
        rebuilds the same participation rather than negotiating a new one.
        """
        session_id = payload.get("session_id") or self.session_id
        self.restore_windows_endpoint()
        if self.decoder is not None:
            returncode = await asyncio.to_thread(self.decoder.close)
            self.report["ffmpeg_returncode"] = returncode
            self.decoder = None
        # The broadcast lets go. The speaker closes only if nothing else is
        # using it - a broadcast ending is not a reason for the shop's
        # promotion to stop, which is the whole point of ducking.
        self._release_pcm_sink("broadcast")
        if self.queue is not None:
            self.queue.close()
            self.queue = None

        self.stood_down = True
        if isinstance(session_id, int) and session_id > 0:
            await self._send(connection, {
                **self._envelope("stood_down"),
                "session_id": session_id,
                "reason": str(payload.get("reason") or "operator_pause")[:128],
            })
            self.report["stood_down"] = True
            self._record_state("STOOD_DOWN")

    async def _on_resume(self, connection, payload: dict) -> None:
        """Re-open the output for the session this Store never left.

        The device is opened BEFORE `resumed` is sent, for the same reason
        READY is only claimed after a successful open: a shop that cannot open
        its output has not resumed, and saying so late is worse than saying no
        now. A failure here reports device_error and leaves the Store stood
        down rather than pretending.
        """
        session_id = payload.get("session_id") or self.session_id
        if not isinstance(session_id, int) or session_id <= 0:
            return
        generation = payload.get("generation")
        generation = generation if isinstance(generation, int) and generation > 0 else 1

        pcm_sink = None
        if self.sink.is_hardware:
            try:
                pcm_sink = WindowsPcmSink(self.sink, backend=self._audio_backend)
                pcm_sink.open()
            except SinkConfigurationError:
                await self._send(connection, {
                    **self._envelope("device_error"),
                    "error_code": "OUTPUT_DEVICE_UNAVAILABLE",
                    "details": "the output device could not be re-opened on resume",
                    "recoverable": False,
                })
                self.report["output_stream_open"] = "failed"
                self._record_state("DEVICE_ERROR")
                return
            self.report["output_stream_open"] = "ok"

        store_id = payload.get("store_id") or self.store_id
        self.session_id = session_id
        self.queue = StoreAudioQueue(store_id=store_id or 0,
                                     capacity=self.queue_capacity)
        self.pcm_sink = pcm_sink
        self.decoder = FfmpegDecoder(sink_mode=self.sink.sink_mode, pcm_sink=pcm_sink)
        self.decoder.start()

        # The Windows endpoint is taken over again from the shop's own level,
        # not from whatever the last participation left behind: the volume
        # baseline belongs to the generation, and a resumed Store starting at
        # the previous announcement's level is exactly the surprise this
        # avoids.
        await self._prepare_windows_endpoint(connection)

        self.stood_down = False
        await self._send(connection, {
            **self._envelope("resumed"),
            "session_id": session_id,
            "generation": generation,
        })
        self.report["resumed"] = True
        self._record_state("RESUMED")

    async def _shutdown(self, connection) -> None:
        # Every other way a run can end: the broadcaster disconnecting, the
        # stream dying, a controlled Agent shutdown, an exception that reaches
        # cleanup. Idempotent, so a stop that already restored costs nothing.
        self.restore_windows_endpoint()
        if self.decoder is not None and self.decoder.running:
            self.report["ffmpeg_returncode"] = await asyncio.to_thread(self.decoder.close)
        if self.pcm_sink is not None:
            self.pcm_sink.close()
        if self.queue is not None and not self.queue.closed:
            self.queue.close()
        try:
            await connection.close()
            await connection.wait_closed()
        except Exception:
            pass


CHIME_SECONDS = 1.5
CHIME_FREQUENCY_HZ = 440
CHIME_GAIN = 0.08  # deliberately quiet; the operator raises the amplifier, not us
# A Bluetooth A2DP endpoint can take a second or more to wake its DAC once a
# stream starts, so the operator may need a longer diagnostic tone. Loudness
# stays fixed - only the duration is selectable, and it is bounded so nobody
# can hold an amplifier open indefinitely.
CHIME_MAX_SECONDS = 10.0


def play_test_chime(
    configuration: SinkConfiguration,
    *,
    backend=None,
    confirm=input,
    seconds: float = CHIME_SECONDS,
) -> dict:
    """Play a short, quiet chime to the explicitly selected device.

    Manual only. It prints the exact device first and requires interactive
    confirmation. It never changes the system volume or the default device, and
    it never sets SPEAKER_VERIFIED - hearing it is operator observation, not
    acoustic verification.
    """
    if not configuration.is_hardware or configuration.device is None:
        raise SinkConfigurationError(
            "the test chime requires windows sink mode and an explicitly "
            "selected output device"
        )
    if not 0 < seconds <= CHIME_MAX_SECONDS:
        raise SinkConfigurationError(
            f"the chime duration must be greater than 0 and at most "
            f"{CHIME_MAX_SECONDS} seconds; {seconds!r} was refused so an "
            "amplifier is never held open by a bad value"
        )
    device = configuration.device
    print("About to play a short, quiet test chime on EXACTLY this device:")
    print(f"  selector : {device.selector}")
    print(f"  name     : {device.name}")
    print(f"  host API : {device.host_api}")
    print(f"  rate     : {configuration.sample_rate} Hz, {configuration.channels} channel(s)")
    print(f"  duration : {seconds} s")
    if device.looks_like_bluetooth:
        print("  WARNING  : this looks like a Bluetooth endpoint. A wired USB or")
        print("             3.5 mm output is strongly preferred for a Store pilot.")
    print("Keep the amplifier volume LOW. Nothing here changes Windows volume.")

    try:
        answer = confirm("Type 'yes' to play the chime, anything else to cancel: ")
    except EOFError:
        raise SinkConfigurationError(
            "the test chime needs an interactive terminal to confirm before it "
            "plays. Run it yourself in a PowerShell window; it is never played "
            "automatically."
        ) from None
    if str(answer).strip().lower() != "yes":
        return {"played": False, "cancelled": True, "frames_written": 0,
                "speaker_verified": False}

    import math
    import struct

    total_frames = int(configuration.sample_rate * seconds)
    samples = bytearray()
    for frame in range(total_frames):
        # Simple fade in/out so the chime cannot click or thump.
        envelope = min(1.0, frame / 2000, (total_frames - frame) / 2000)
        value = CHIME_GAIN * envelope * math.sin(
            2 * math.pi * CHIME_FREQUENCY_HZ * frame / configuration.sample_rate
        )
        sample = struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
        # One sample per channel, or PortAudio reads a mono buffer as
        # interleaved stereo: half the duration at double the pitch.
        samples += sample * configuration.channels

    sink = WindowsPcmSink(configuration, backend=backend)
    sink.open()
    try:
        written = sink.write(bytes(samples))
    finally:
        sink.close()

    return {
        "played": bool(written),
        "cancelled": False,
        "frames_written": sink.frames_written,
        "device": device.as_dict(),
        # Hearing this is useful operator evidence. It is NOT verification.
        "speaker_verified": False,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_receiver_pilot",
        description=(
            "Local one-Store audio Receiver pilot. Decodes WebM/Opus with FFmpeg. "
            "The default sink is null; a Windows output device is used only when "
            "explicitly selected. It never claims speaker verification."
        ),
    )
    parser.add_argument(
        "action", nargs="?", default="run",
        choices=("run", "list-output-devices", "test-output"),
        help="run (default), list-output-devices, or test-output (manual chime).",
    )
    parser.add_argument("--url", help="Receiver WebSocket URL (loopback). Required for 'run'.")
    parser.add_argument("--queue-capacity", type=int, default=24)
    parser.add_argument("--report", default=None, help="Optional secret-free JSON report path.")
    parser.add_argument(
        "--seconds", type=float, default=CHIME_SECONDS,
        help=(
            f"test-output only: chime duration in seconds (default {CHIME_SECONDS}, "
            f"maximum {CHIME_MAX_SECONDS}). A Bluetooth endpoint can take a second "
            "or more to wake, so a longer tone is sometimes needed to tell a sleeping "
            "link apart from no audio path at all. Loudness is never selectable."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)

    if arguments.action == "list-output-devices":
        try:
            print(format_device_table(list_output_devices()))
        except AudioDeviceError as error:
            print(f"Device enumeration refused: {error}", file=sys.stderr)
            return EXIT_SAFETY
        return EXIT_OK

    if arguments.action == "test-output":
        try:
            configuration = resolve_sink_configuration()
            outcome = play_test_chime(configuration, seconds=arguments.seconds)
        except AudioReceiverError as error:
            print(f"Test chime refused: {error}", file=sys.stderr)
            return EXIT_SAFETY
        if outcome["cancelled"]:
            print("Cancelled. Nothing was played.")
            return EXIT_OK
        print(f"  played: {outcome['played']}")
        print(f"  frames_written: {outcome['frames_written']}")
        print("  speaker_verified: False  (hearing it is operator observation only)")
        return EXIT_OK if outcome["played"] else EXIT_AUDIO_FAILED

    if not arguments.url:
        print("--url is required for the 'run' action.", file=sys.stderr)
        return EXIT_SAFETY

    try:
        sink = resolve_sink_configuration()
        pilot = AudioReceiverPilot(
            ws_url=arguments.url,
            queue_capacity=arguments.queue_capacity,
            report_path=Path(arguments.report) if arguments.report else None,
            sink=sink,
        )
        report = asyncio.run(pilot.run())
    except AudioReceiverError as error:
        print(f"Audio Receiver pilot refused: {error}", file=sys.stderr)
        return EXIT_SAFETY
    except Exception as error:  # pragma: no cover - defensive
        print(f"Audio Receiver pilot failed: {type(error).__name__}", file=sys.stderr)
        return EXIT_AUDIO_FAILED

    for key in sorted(report):
        if key == "states":
            continue
        print(f"  {key}: {report[key]}")
    print(f"  states: {' -> '.join(report['states'])}")
    return EXIT_OK if report["overall_result"] == "AUDIO_RECEIVER_PILOT_PASSED" else EXIT_AUDIO_FAILED


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
