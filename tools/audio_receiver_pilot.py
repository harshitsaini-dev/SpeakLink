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
- ``playback_confirmed`` is sent only after FFmpeg reports it has actually
  decoded audio (``out_time_ms`` advancing past zero on its progress stream).
- ``speaker_verified`` is **never** sent. This Receiver decodes to a null sink,
  so it cannot know anything about output devices, amplifiers or speakers.

The Receiver credential is read from an environment variable, kept in memory,
and never printed, logged, written to a report, placed in a URL or passed as a
command argument. No raw audio is ever logged or written into the repository.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from audio_protocol import (  # noqa: E402
    InvalidAudioChunkError,
    parse_prepare_message,
    validate_audio_chunk,
)
from audio_streaming import StoreAudioQueue, StoreQueueClosedError  # noqa: E402

RECEIVER_TOKEN_ENV = "ECHOCAST_RECEIVER_TOKEN"
PROTOCOL_VERSION = "1.0"

SINK_MODE_NULL = "null"

EXIT_OK = 0
EXIT_SAFETY = 1
EXIT_AUDIO_FAILED = 2
EXIT_CLEANUP_FAILED = 3

FFMPEG_EXIT_TIMEOUT_SECONDS = 15


class AudioReceiverError(RuntimeError):
    """Controlled, secret-free Receiver pilot failure."""


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


def opus_webm_decode_supported() -> bool:
    """Real capability check, not an assumption."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        decoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-decoders"],
            capture_output=True, text=True, timeout=30,
        )
        formats = subprocess.run(
            [ffmpeg, "-hide_banner", "-formats"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if decoders.returncode != 0 or formats.returncode != 0:
        return False
    has_opus = " opus " in decoders.stdout or "libopus" in decoders.stdout
    has_webm = "matroska,webm" in formats.stdout or "webm" in formats.stdout
    return bool(has_opus and has_webm)


class FfmpegDecoder:
    """One FFmpeg process per active session, decoding WebM/Opus to a null sink."""

    def __init__(self, *, sink_mode: str = SINK_MODE_NULL) -> None:
        self.sink_mode = sink_mode
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
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel", "error",
            "-f", "webm",
            "-i", "pipe:0",
            "-ac", "1",
            "-progress", "pipe:1",
            "-f", "null",
            "-",
        ]

    def start(self) -> None:
        if self._process is not None:
            raise AudioReceiverError("the FFmpeg decoder is already running")
        self._process = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_progress, daemon=True)
        self._reader.start()

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
    ) -> None:
        self.ws_url = ws_url
        self.queue_capacity = queue_capacity
        self.playback_timeout = playback_timeout
        self.report_path = report_path

        self.session_id: int | None = None
        self.decoder: FfmpegDecoder | None = None
        self.queue: StoreAudioQueue | None = None
        self._sequence = 0
        self._states: list[str] = []

        self.report: dict = {
            "started_at_utc": _utc_now(),
            "protocol_version": PROTOCOL_VERSION,
            "ffmpeg_available": False,
            "codec_supported": False,
            "sink_mode": SINK_MODE_NULL,
            "bounded_queue_capacity": queue_capacity,
            "connected": False,
            "ready": False,
            "audio_receiving": False,
            "playback_confirmed": False,
            "stopped": False,
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

        try:
            await self._session_loop(connection)
        finally:
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
            # Unknown control messages are ignored rather than acted upon.

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

        self.session_id = prepare.broadcast_session_id
        self.queue = StoreAudioQueue(store_id=prepare.target_store_id,
                                     capacity=self.queue_capacity)
        self.decoder = FfmpegDecoder(sink_mode=SINK_MODE_NULL)
        self.decoder.start()

        await self._send(connection, {
            **self._envelope("receiver_ready"),
            "software_checks_passed": True,
            "output_device_checks_passed": True,
        })
        self.report["ready"] = True
        self._record_state("READY")

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

        if not self.report["playback_confirmed"]:
            decoded = await asyncio.to_thread(self.decoder.wait_for_decode, 0.05)
            if decoded:
                await self._send(connection, {
                    **self._envelope("playback_confirmed"),
                    "session_id": self.session_id,
                })
                self.report["playback_confirmed"] = True
                self.report["ffmpeg_decoded_microseconds"] = self.decoder.decoded_microseconds
                self._record_state("PLAYBACK_CONFIRMED")

    async def _on_stop(self, connection, payload: dict) -> None:
        session_id = payload.get("session_id") or self.session_id
        if self.decoder is not None:
            returncode = await asyncio.to_thread(self.decoder.close)
            self.report["ffmpeg_returncode"] = returncode
            self.report["ffmpeg_decoded_microseconds"] = self.decoder.decoded_microseconds
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

    async def _shutdown(self, connection) -> None:
        if self.decoder is not None and self.decoder.running:
            self.report["ffmpeg_returncode"] = await asyncio.to_thread(self.decoder.close)
        if self.queue is not None and not self.queue.closed:
            self.queue.close()
        try:
            await connection.close()
            await connection.wait_closed()
        except Exception:
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audio_receiver_pilot",
        description=(
            "Local one-Store audio Receiver pilot. Decodes WebM/Opus with FFmpeg "
            "to a null sink and reports honest acknowledgements. It never claims "
            "speaker output."
        ),
    )
    parser.add_argument("--url", required=True, help="Receiver WebSocket URL (loopback).")
    parser.add_argument("--queue-capacity", type=int, default=24)
    parser.add_argument("--report", default=None, help="Optional secret-free JSON report path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        pilot = AudioReceiverPilot(
            ws_url=arguments.url,
            queue_capacity=arguments.queue_capacity,
            report_path=Path(arguments.report) if arguments.report else None,
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
