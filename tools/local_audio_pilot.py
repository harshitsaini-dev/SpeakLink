"""One-Store live-audio software pilot orchestrator.

Runs the whole software audio path end to end against the existing isolated
local-pilot database:

    deterministic WebM/Opus fixture
      -> HQ broadcaster WebSocket
      -> FastAPI
      -> one bounded Store queue
      -> one local Receiver process
      -> FFmpeg software decode (null sink)
      -> real AUDIO_RECEIVING acknowledgement
      -> real PLAYBACK_CONFIRMED acknowledgement
      -> clean STOP and cleanup

A passing run means READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST and nothing
more. It does not prove a correct Windows output device, an amplifier, a
Bluetooth link, audible Store speakers, EchoGuard or SPEAKER_VERIFIED.

Secret handling:

- The pilot password comes from the process-scoped environment and is never
  printed, logged or persisted.
- The selected Store's Receiver credential is passed to the Receiver child
  process through its environment, never as a command argument or URL.
- The HQ broadcaster endpoint takes its JWT in the query string. That is a
  pre-existing architecture limitation recorded in PROJECT_STATE, so the pilot
  backend runs with ``--no-access-log`` to keep request URLs out of the log.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tools.generate_audio_fixture import (  # noqa: E402
    AudioFixtureError,
    generate_fixture,
)
from tools.local_pilot import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    PilotCredentialError,
    PilotSafetyError,
    ProtectedPathError,
    _pilot_environment,
    _pilot_username,
    checkpoint_pilot_database,
    prepare as prepare_local_pilot,
    reject_protected_database,
    require_pilot_password,
    resolve_pilot_paths,
    validate_pilot_root,
)

EXIT_OK = 0
EXIT_SAFETY = 1
EXIT_AUDIO_FAILED = 2
EXIT_CLEANUP_FAILED = 3

READINESS_SCOPE = "READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST"
PREFERRED_STORE_CODE = "UN"
CHUNK_MS = 250
STARTUP_TIMEOUT_SECONDS = 40
SHUTDOWN_TIMEOUT_SECONDS = 15


class AudioPilotError(RuntimeError):
    """Controlled, secret-free audio pilot failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audio_dir(paths) -> Path:
    return paths.root / "audio"


def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _select_store(database_path: Path) -> tuple[int, str, str]:
    """Return (store_id, store_code, receiver_token). Credential stays in memory."""
    connection = _read_only_connection(database_path)
    try:
        row = connection.execute(
            "SELECT id, store_code, receiver_token FROM stores WHERE store_code = ?",
            (PREFERRED_STORE_CODE,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT id, store_code, receiver_token FROM stores ORDER BY id LIMIT 1"
            ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AudioPilotError("the pilot database contains no Store to test with")
    return int(row[0]), str(row[1]), str(row[2])


def _receiver_event_types(database_path: Path, store_id: int) -> list[str]:
    connection = _read_only_connection(database_path)
    try:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT event_type FROM receiver_events WHERE store_id = ? ORDER BY id",
                (store_id,),
            )
        ]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _api_get(base_url: str, path: str, token: str):
    import requests

    response = requests.get(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if response.status_code != 200:
        raise AudioPilotError(f"GET {path} returned HTTP {response.status_code}")
    return response.json()


def _api_post(base_url: str, path: str, token: str, payload: dict | None = None):
    import requests

    response = requests.post(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        timeout=20,
    )
    if response.status_code not in (200, 201):
        raise AudioPilotError(f"POST {path} returned HTTP {response.status_code}")
    return response.json()


def _wait_until(predicate, *, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _target_play_status(base_url: str, token: str, session_id: int, store_id: int) -> str | None:
    detail = _api_get(base_url, f"/api/broadcast/sessions/{session_id}", token)
    for target in detail.get("targets", []):
        if target.get("store_id") == store_id:
            return target.get("play_status")
    return None


# ---------------------------------------------------------------------------
# Audio streaming from the fixture through the HQ broadcaster socket
# ---------------------------------------------------------------------------
async def _audio_phase(
    ws_url: str,
    fixture: Path,
    *,
    await_acknowledgements,
    request_stop,
    chunk_ms: int = CHUNK_MS,
) -> dict:
    """Stream the fixture, wait for real acknowledgements, then stop cleanly.

    The broadcaster socket is deliberately held open until after the explicit
    stop request. Closing it first would trip the backend's existing
    ``broadcaster_disconnected`` safety net, which ends the session on its own
    and would make an explicit stop impossible to test.
    """
    import websockets

    data = fixture.read_bytes()
    # Roughly one chunk per chunk_ms of the fixture's duration.
    total_ms = max(chunk_ms, int(_fixture_duration_ms(fixture)))
    chunk_count = max(1, total_ms // chunk_ms)
    step = max(1, len(data) // chunk_count)

    outcome = {"sent_chunks": 0, "sent_bytes": 0, "acknowledged": False, "stopped": False}
    connection = await websockets.connect(ws_url, open_timeout=15, max_size=4 * 1024 * 1024)
    try:
        for offset in range(0, len(data), step):
            chunk = data[offset:offset + step]
            if not chunk:
                continue
            await connection.send(chunk)
            outcome["sent_chunks"] += 1
            outcome["sent_bytes"] += len(chunk)
            await asyncio.sleep(chunk_ms / 1000)

        outcome["acknowledged"] = await asyncio.to_thread(await_acknowledgements)
        if outcome["acknowledged"]:
            # Stop while the broadcaster socket is still open, exactly as the
            # dashboard does when the operator clicks Stop.
            await asyncio.to_thread(request_stop)
            outcome["stopped"] = True
    finally:
        await connection.close()
        await connection.wait_closed()
    return outcome


_FIXTURE_DURATION_CACHE: dict[str, float] = {}


def _fixture_duration_ms(fixture: Path) -> float:
    key = str(fixture)
    if key not in _FIXTURE_DURATION_CACHE:
        from tools.generate_audio_fixture import probe

        _FIXTURE_DURATION_CACHE[key] = probe(fixture)["duration_seconds"] * 1000
    return _FIXTURE_DURATION_CACHE[key]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
def prepare(paths) -> dict:
    """Prepare the isolated pilot database and the deterministic audio fixture."""
    state = prepare_local_pilot(paths)
    try:
        fixture = generate_fixture(_audio_dir(paths))
    except AudioFixtureError as error:
        raise AudioPilotError(str(error)) from None
    return {
        "database": state,
        "fixture": fixture,
        "readiness_scope": READINESS_SCOPE,
    }


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------
def smoke(paths) -> dict:
    """Run the one-Store live-audio software pilot end to end."""
    import requests

    validate_pilot_root(paths.root)
    reject_protected_database(paths.database_path)
    require_pilot_password()
    if not paths.database_path.exists():
        raise AudioPilotError("the pilot database is not prepared; run 'prepare' first")

    fixture_facts = generate_fixture(_audio_dir(paths))
    fixture_path = Path(fixture_facts["path"])

    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = _pilot_environment(paths)
    logs_dir = paths.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    backend_log = logs_dir / "audio-backend.log"
    receiver_log = logs_dir / "audio-receiver.log"
    receiver_report = logs_dir / "audio-receiver-report.json"
    report_path = logs_dir / "audio-smoke-report.json"

    result: dict = {
        "started_at_utc": _utc_now(),
        "readiness_scope": READINESS_SCOPE,
        "pilot_database_path": str(paths.database_path),
        "backend_host": "127.0.0.1",
        "backend_port": port,
        "backend_url": base_url,
        "uvicorn_workers": 1,
        "fixture": {
            key: fixture_facts[key]
            for key in ("path", "sha256", "codec_name", "channels",
                        "format_name", "duration_seconds", "size_bytes")
        },
        "audio_format": {
            "container": "webm", "codec": "opus", "channels": 1,
            "target_bitrate": 32000, "expected_chunk_ms": CHUNK_MS,
        },
        "liveness": "failed",
        "login": "failed",
        "selected_store_code": None,
        "selected_store_id": None,
        "session_id": None,
        "observed_connected": False,
        "observed_ready": False,
        "observed_audio_receiving": False,
        "observed_playback_confirmed": False,
        "observed_stopped": False,
        "speaker_verified": False,
        "sent_chunks": 0,
        "sent_bytes": 0,
        "receiver_total_chunks": 0,
        "receiver_total_bytes": 0,
        "receiver_dropped_chunks": 0,
        "ffmpeg_returncode": None,
        "ffmpeg_decoded_microseconds": 0,
        "sink_mode": "null",
        "backend_process_running": True,
        "receiver_process_running": True,
        "shutdown": "failed",
        "overall_result": "ONE_STORE_AUDIO_SOFTWARE_PILOT_FAILED",
    }

    backend_handle = backend_log.open("w", encoding="utf-8")
    receiver_handle = receiver_log.open("w", encoding="utf-8")
    backend_process = None
    receiver_process = None

    try:
        backend_process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "server:app",
                "--host", "127.0.0.1", "--port", str(port),
                "--workers", "1",
                # The HQ broadcaster takes its JWT in the query string, so the
                # access log is disabled to keep request URLs out of the log.
                "--no-access-log",
            ],
            cwd=str(BACKEND_DIR), env=environment,
            stdout=backend_handle, stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if backend_process.poll() is not None:
                raise AudioPilotError("the pilot backend exited during startup")
            try:
                if requests.get(f"{base_url}/docs", timeout=1).status_code == 200:
                    result["liveness"] = "ok"
                    break
            except requests.RequestException:
                time.sleep(0.2)
        else:
            raise AudioPilotError("the pilot backend did not become live in time")

        login = requests.post(
            f"{base_url}/api/auth/login",
            json={"username": _pilot_username(), "password": require_pilot_password()},
            timeout=15,
        )
        if login.status_code != 200:
            raise AudioPilotError(f"pilot login failed with HTTP {login.status_code}")
        access_token = login.json()["access_token"]
        result["login"] = "ok"

        store_id, store_code, receiver_token = _select_store(paths.database_path)
        result["selected_store_id"] = store_id
        result["selected_store_code"] = store_code

        # Start the Receiver child process. The credential travels through its
        # environment only: never a command argument, never a URL.
        receiver_environment = os.environ.copy()
        receiver_environment["ECHOCAST_RECEIVER_TOKEN"] = receiver_token
        receiver_process = subprocess.Popen(
            [
                sys.executable, str(REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py"),
                "--url", f"ws://127.0.0.1:{port}/api/ws/receiver",
                "--report", str(receiver_report),
            ],
            cwd=str(REPOSITORY_ROOT), env=receiver_environment,
            stdout=receiver_handle, stderr=subprocess.STDOUT,
        )
        del receiver_token, receiver_environment

        def _store_online() -> bool:
            for store in _api_get(base_url, "/api/stores", access_token):
                if store["store_code"] == store_code:
                    return store["status"] in ("online", "playing")
            return False

        if not _wait_until(_store_online, timeout=25):
            raise AudioPilotError("the Receiver did not reach CONNECTED")
        result["observed_connected"] = True

        session = _api_post(
            base_url, "/api/broadcast/sessions", access_token,
            {
                "campaign_name": "One-Store live audio software pilot",
                "target_mode": "selected",
                "store_ids": [store_id],
            },
        )
        session_id = session["id"]
        result["session_id"] = session_id

        # Starting the session makes the backend send PREPARE then PLAY.
        _api_post(base_url, f"/api/broadcast/sessions/{session_id}/start", access_token)

        def _ready() -> bool:
            return "receiver_ready" in _receiver_event_types(paths.database_path, store_id)

        if not _wait_until(_ready, timeout=30):
            raise AudioPilotError(
                "the Receiver never reported READY after its FFmpeg/codec checks"
            )
        result["observed_ready"] = True

        # A single-use handshake ticket, not the access token: uvicorn writes
        # the whole WebSocket URL to its access log, so a reusable credential
        # there would be logged in clear on every connection.
        ticket = _api_post(base_url, "/api/auth/ws-ticket", access_token)["ticket"]
        broadcaster_url = f"ws://127.0.0.1:{port}/api/ws/broadcaster?ticket={ticket}"
        seen_states: set[str] = set()

        def _await_acknowledgements() -> bool:
            def _collect() -> bool:
                events = _receiver_event_types(paths.database_path, store_id)
                if "audio_receiving" in events:
                    seen_states.add("audio_receiving")
                if "playback_confirmed" in events:
                    seen_states.add("playback_confirmed")
                return {"audio_receiving", "playback_confirmed"} <= seen_states

            return _wait_until(_collect, timeout=30)

        def _request_stop() -> None:
            _api_post(base_url, f"/api/broadcast/sessions/{session_id}/stop", access_token)

        phase = asyncio.run(
            _audio_phase(
                broadcaster_url,
                fixture_path,
                await_acknowledgements=_await_acknowledgements,
                request_stop=_request_stop,
            )
        )
        result["sent_chunks"] = phase["sent_chunks"]
        result["sent_bytes"] = phase["sent_bytes"]

        if not phase["acknowledged"]:
            raise AudioPilotError(
                "the Receiver did not report both AUDIO_RECEIVING and "
                f"PLAYBACK_CONFIRMED (observed: {sorted(seen_states) or 'none'})"
            )
        result["observed_audio_receiving"] = True
        result["observed_playback_confirmed"] = True
        if not phase["stopped"]:
            raise AudioPilotError("the explicit stop request was not issued")

        def _stopped() -> bool:
            return "stopped" in _receiver_event_types(paths.database_path, store_id)

        if not _wait_until(_stopped, timeout=25):
            raise AudioPilotError("the Receiver did not report STOPPED")
        result["observed_stopped"] = True

        # SPEAKER_VERIFIED must never appear: EchoGuard is not part of this task
        # and the Receiver decodes to a null sink.
        events = _receiver_event_types(paths.database_path, store_id)
        if "speaker_verified" in events:
            raise AudioPilotError("a speaker_verified event was recorded; that is not possible here")
        result["speaker_verified"] = False

        result["overall_result"] = "ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED"

    finally:
        shutdown_ok = True
        if receiver_process is not None:
            shutdown_ok &= _stop_process(receiver_process)
            result["receiver_process_running"] = receiver_process.poll() is None
        if backend_process is not None:
            shutdown_ok &= _stop_process(backend_process)
            result["backend_process_running"] = backend_process.poll() is None
        backend_handle.close()
        receiver_handle.close()

        if receiver_report.exists():
            try:
                receiver_facts = json.loads(receiver_report.read_text(encoding="utf-8"))
                result["receiver_total_chunks"] = receiver_facts.get("total_chunks", 0)
                result["receiver_total_bytes"] = receiver_facts.get("total_bytes", 0)
                result["receiver_dropped_chunks"] = receiver_facts.get("dropped_chunks", 0)
                result["ffmpeg_returncode"] = receiver_facts.get("ffmpeg_returncode")
                result["ffmpeg_decoded_microseconds"] = receiver_facts.get(
                    "ffmpeg_decoded_microseconds", 0
                )
                result["sink_mode"] = receiver_facts.get("sink_mode", "null")
            except (OSError, json.JSONDecodeError):
                pass

        try:
            checkpoint_pilot_database(paths)
        except Exception:
            pass

        result["shutdown"] = "ok" if shutdown_ok else "failed"
        result["ended_at_utc"] = _utc_now()
        report_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    return result


def _stop_process(process: subprocess.Popen) -> bool:
    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False
        return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_audio_pilot",
        description=(
            "One-Store live-audio software pilot. Uses the isolated local-pilot "
            "database and a deterministic synthetic WebM/Opus fixture. It never "
            "touches the protected application database and never claims speaker "
            "output."
        ),
    )
    parser.add_argument("action", choices=("prepare", "smoke"))
    parser.add_argument("--pilot-root", default=None)
    return parser


def _print(payload: dict, indent: str = "  ") -> None:
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, dict):
            print(f"{indent}{key}:")
            _print(value, indent + "  ")
        else:
            print(f"{indent}{key}: {value}")


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        paths = resolve_pilot_paths(arguments.pilot_root)
        if arguments.action == "prepare":
            state = prepare(paths)
            print("One-Store audio pilot prepared (isolated database + synthetic fixture).")
            _print(state)
            return EXIT_OK

        result = smoke(paths)
        print(f"One-Store audio pilot: {result['overall_result']}")
        _print(result)
        if result["shutdown"] != "ok":
            return EXIT_CLEANUP_FAILED
        return (
            EXIT_OK
            if result["overall_result"] == "ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED"
            else EXIT_AUDIO_FAILED
        )
    except (ProtectedPathError, PilotSafetyError, PilotCredentialError) as error:
        print(f"Audio pilot refused: {error}", file=sys.stderr)
        return EXIT_SAFETY
    except (AudioPilotError, AudioFixtureError) as error:
        print(f"Audio pilot assertion failed: {error}", file=sys.stderr)
        return EXIT_AUDIO_FAILED


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
