"""Synthetic multi-Store load test for the EchoCast broadcast path.

Runs N null-sink Receivers against a real backend over loopback and streams the
deterministic WebM/Opus fixture to all of them at once, then reports what
actually happened: how long each Receiver took to connect and report READY, how
many chunks and bytes each one received, how many were dropped, and what the
backend process cost in CPU and memory.

Every Receiver here is synthetic. None of them opens an audio device, decodes
with FFmpeg, or touches a speaker. A pass therefore says the fan-out, the
bounded per-Store queues and the acknowledgement path hold up at N Stores. It
says nothing about audio quality, amplifiers or audibility - a one-Store
hardware test does not become a 40-Store rollout because this passed.

The protected database backend/echocast_live.db is never used.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys
import time

import websockets

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIRECTORY):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.local_audio_pilot import (  # noqa: E402
    AudioPilotError,
    _api_post,
    _free_loopback_port,
    _read_only_connection,
    prepare,
)
from tools.local_pilot import resolve_pilot_paths  # noqa: E402
from tools.receiver_simulator import MessageFactory  # noqa: E402


CHUNK_INTERVAL_SECONDS = 0.25  # what the browser MediaRecorder actually uses
HEARTBEAT_SECONDS = 5.0


class SyntheticReceiver:
    """One null-sink Receiver: correct protocol, no audio device, no FFmpeg."""

    def __init__(self, store_id: int, store_code: str, token: str, url: str) -> None:
        self.store_id = store_id
        self.store_code = store_code
        self._token = token  # never logged, never placed in a URL
        self._url = url
        self._factory = MessageFactory()
        self._socket = None

        self.connect_ms: float | None = None
        self.ready_ms: float | None = None
        self.first_chunk_ms: float | None = None
        self.chunks = 0
        self.bytes = 0
        self.errors: list[str] = []
        self.states: list[str] = []

    async def connect(self) -> None:
        started = time.perf_counter()
        self._socket = await websockets.connect(
            self._url,
            additional_headers={"Authorization": f"Bearer {self._token}"},
            open_timeout=20,
            max_size=4 * 1024 * 1024,
        )
        self.connect_ms = (time.perf_counter() - started) * 1000
        self.states.append("CONNECTED")

    async def close(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:
                pass
            self._socket = None

    async def _send(self, message_type: str, **fields) -> None:
        await self._socket.send(json.dumps(self._factory.build(message_type, **fields)))

    async def run(self, broadcast_started: asyncio.Event) -> None:
        """Serve until the socket closes. Text is a command, binary is audio."""
        session_id: int | None = None
        last_beat = time.perf_counter()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(self._socket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Idle: keep the connection fresh, exactly as the real pilot does.
                    if time.perf_counter() - last_beat >= HEARTBEAT_SECONDS:
                        await self._send("heartbeat")
                        last_beat = time.perf_counter()
                    continue

                if isinstance(message, bytes):
                    if self.chunks == 0:
                        self.first_chunk_ms = (time.perf_counter() - broadcast_started_at[0]) * 1000
                        self.states.append("AUDIO_RECEIVING")
                        await self._send("audio_receiving", session_id=session_id)
                        # A null sink accepts frames immediately; a real Receiver
                        # would only say this once its output stream took PCM.
                        await self._send("playback_confirmed", session_id=session_id)
                        self.states.append("PLAYBACK_CONFIRMED")
                    self.chunks += 1
                    self.bytes += len(message)
                    continue

                payload = json.loads(message)
                kind = payload.get("type")
                if kind == "prepare":
                    # The prepare command names it broadcast_session_id; the stop
                    # command names it session_id. Accept either.
                    session_id = payload.get("broadcast_session_id") or payload.get("session_id")
                    await self._send("receiver_ready", session_id=session_id)
                    self.ready_ms = (time.perf_counter() - prepare_clock[0]) * 1000
                    self.states.append("READY")
                elif kind == "stop":
                    await self._send("stopped", session_id=session_id)
                    self.states.append("STOPPED")
                elif kind == "ack_rejected":
                    self.errors.append(str(payload.get("code")))
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            # Recording this rather than returning silently is how a protocol
            # mistake in this harness stops looking like a backend drop.
            self.errors.append(type(failure).__name__)
            return


# Shared clocks, set once by the driver so every Receiver measures the same t0.
broadcast_started_at = [0.0]
prepare_clock = [0.0]


def _process_cost(process_id: int) -> dict:
    """Working set and CPU time for the whole backend tree.

    Measuring only the recorded PID would measure the wrong thing: a venv
    python.exe spawns the base interpreter as a child, and the child is the one
    serving requests. The launcher reports about 3 MB and zero CPU forever.
    """
    script = (
        f"$ids = @({process_id}); "
        f"$ids += (Get-CimInstance Win32_Process -Filter 'ParentProcessId = {process_id}' "
        "  -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessId }); "
        "$procs = $ids | ForEach-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }; "
        "if ($procs) { "
        "  [pscustomobject]@{ "
        "    rss = ($procs | Measure-Object -Sum WorkingSet64).Sum; "
        "    cpu = ($procs | ForEach-Object { $_.TotalProcessorTime.TotalSeconds } "
        "           | Measure-Object -Sum).Sum; "
        "    processes = $procs.Count "
        "  } | ConvertTo-Json -Compress "
        "}"
    )
    try:
        output = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return json.loads(output) if output else {}
    except Exception:
        return {}


def _read_audio_metrics(base_url: str, token: str) -> dict:
    """The server's own per-Store queue counters, or an explicit absence.

    Returns ``{"unavailable": "..."}`` rather than ``{}`` on failure. A load
    report that silently shows no queue evidence reads as "the queues were fine";
    one that says the metrics could not be read says what actually happened.
    """
    import requests

    try:
        response = requests.get(
            f"{base_url}/api/broadcast/audio-metrics",
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
    except Exception as failure:  # noqa: BLE001 - reported, never swallowed
        return {"unavailable": failure.__class__.__name__}
    if response.status_code != 200:
        return {"unavailable": f"HTTP {response.status_code}"}
    try:
        return response.json()
    except ValueError:
        return {"unavailable": "the metrics response was not JSON"}


def _summarise_queue_metrics(metrics: dict) -> dict:
    """Fold the per-Store queue counters into the few numbers that matter."""
    if not isinstance(metrics, dict) or "stores" not in metrics:
        return {"unavailable": (metrics or {}).get("unavailable", "not collected")}
    stores = metrics.get("stores") or []
    if not stores:
        return {"stores_measured": 0, "note": "no queue existed when sampled"}
    return {
        "stores_measured": len(stores),
        "capacity": metrics.get("capacity"),
        "max_depth_observed": max(s.get("max_depth", 0) for s in stores),
        "max_depth_by_store": {s["store_id"]: s.get("max_depth", 0) for s in stores},
        "dropped_total": sum(s.get("dropped", 0) for s in stores),
        "dropped_by_store": {s["store_id"]: s.get("dropped", 0) for s in stores},
        "delivered_total": sum(s.get("delivered", 0) for s in stores),
        "enqueued_total": sum(s.get("enqueued", 0) for s in stores),
        # The property the whole design rests on. If this is ever False the
        # bounded queue is not bounded, and that is a P0, not a note.
        "every_queue_within_capacity": all(
            s.get("max_depth", 0) <= s.get("capacity", 0) for s in stores
        ),
    }


def _store_credentials(database_path: Path, count: int) -> list[tuple[int, str, str]]:
    connection = _read_only_connection(database_path)
    try:
        rows = connection.execute(
            "SELECT id, store_code, receiver_token FROM stores "
            "WHERE is_active = 1 AND receiver_token IS NOT NULL ORDER BY id LIMIT ?",
            (count,),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) < count:
        raise AudioPilotError(f"only {len(rows)} Stores have credentials; {count} were requested")
    return [(row[0], row[1], row[2]) for row in rows]


def _summarise(values: list[float]) -> dict:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"count": 0}
    ordered = sorted(clean)
    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 1),
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
        "max_ms": round(ordered[-1], 1),
    }


async def _drive(paths, store_count: int, port: int, backend_pid: int, fixture: Path) -> dict:
    base_url = f"http://127.0.0.1:{port}"
    receiver_url = f"ws://127.0.0.1:{port}/api/ws/receiver"

    token = _api_post(
        base_url, "/api/auth/login", None,
        {"username": os.environ["ADMIN_USERNAME"], "password": os.environ["ADMIN_PASSWORD"]},
    )["access_token"]

    credentials = _store_credentials(paths.database_path, store_count)
    receivers = [SyntheticReceiver(sid, code, tok, receiver_url) for sid, code, tok in credentials]

    started = asyncio.Event()
    connect_started = time.perf_counter()
    await asyncio.gather(*(r.connect() for r in receivers))
    all_connected_ms = (time.perf_counter() - connect_started) * 1000

    tasks = [asyncio.create_task(r.run(started)) for r in receivers]
    try:
        session = _api_post(
            base_url, "/api/broadcast/sessions", token,
            {
                "campaign_name": f"Synthetic load test - {store_count} Stores",
                "target_mode": "selected",
                "store_ids": [r.store_id for r in receivers],
            },
        )
        session_id = session["id"]

        prepare_clock[0] = time.perf_counter()
        _api_post(base_url, f"/api/broadcast/sessions/{session_id}/start", token)

        deadline = time.perf_counter() + 45
        while time.perf_counter() < deadline:
            if all(r.ready_ms is not None for r in receivers):
                break
            await asyncio.sleep(0.1)
        ready_count = sum(1 for r in receivers if r.ready_ms is not None)

        # Stream the fixture through the broadcaster socket, 250 ms at a time.
        # Scoped to the uplink: the ticket store refuses a dashboard ticket here.
        ticket = _api_post(base_url, "/api/auth/ws-ticket", token,
                           {"audience": "broadcaster"})["ticket"]
        raw = fixture.read_bytes()
        # Split into browser-sized chunks. The exact split does not matter to the
        # fan-out; the rate and the count do.
        chunk_size = max(1, len(raw) // 20)
        chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]

        cost_before = _process_cost(backend_pid)
        broadcast_started_at[0] = time.perf_counter()
        started.set()

        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/ws/broadcaster?ticket={ticket}&session_id={session_id}",
            open_timeout=20, max_size=4 * 1024 * 1024,
        ) as uplink:
            await uplink.send(json.dumps({"type": "init", "mime": "audio/webm;codecs=opus"}))
            queue_metrics = {"unavailable": "never sampled"}
            for index, chunk in enumerate(chunks):
                await uplink.send(chunk)
                # Sampled MID-BROADCAST, on purpose. Reading after the uplink
                # closes measures nothing: closing it ends the session, which
                # calls stop_audio_fanout() and removes every queue - correct
                # behaviour that leaves nothing to look at. The first version of
                # this sampled afterwards and reported "no queue existed when
                # sampled", which is why the absence is spelled out rather than
                # reported as a row of zeros.
                if index == len(chunks) // 2:
                    queue_metrics = _read_audio_metrics(base_url, token)
                await asyncio.sleep(CHUNK_INTERVAL_SECONDS)

        # Let the fan-out drain before measuring.
        await asyncio.sleep(1.5)
        broadcast_seconds = time.perf_counter() - broadcast_started_at[0]
        cost_after = _process_cost(backend_pid)

        # Closing the uplink already ends the session, so an explicit stop can
        # legitimately come back 400. Record which happened rather than failing.
        import requests

        stop_status = requests.post(
            f"{base_url}/api/broadcast/sessions/{session_id}/stop",
            headers={"Authorization": f"Bearer {token}"}, timeout=20,
        ).status_code
        await asyncio.sleep(1.0)

        sent_chunks, sent_bytes = len(chunks), len(raw)
        received = [r.chunks for r in receivers]
        cpu_seconds = None
        if cost_before.get("cpu") is not None and cost_after.get("cpu") is not None:
            cpu_seconds = round(cost_after["cpu"] - cost_before["cpu"], 2)

        return {
            "stores": store_count,
            "all_connected_ms": round(all_connected_ms, 1),
            "connect_latency": _summarise([r.connect_ms for r in receivers]),
            "ready_latency": _summarise([r.ready_ms for r in receivers]),
            "first_chunk_latency": _summarise([r.first_chunk_ms for r in receivers]),
            "receivers_ready": ready_count,
            "receivers_playback_confirmed": sum(1 for r in receivers if "PLAYBACK_CONFIRMED" in r.states),
            "receivers_stopped": sum(1 for r in receivers if "STOPPED" in r.states),
            "sent_chunks": sent_chunks,
            "sent_bytes": sent_bytes,
            "received_chunks_min": min(received) if received else 0,
            "received_chunks_max": max(received) if received else 0,
            "dropped_chunks_total": sum(max(0, sent_chunks - c) for c in received),
            "receivers_with_full_delivery": sum(1 for c in received if c >= sent_chunks),
            "broadcast_seconds": round(broadcast_seconds, 2),
            "fanout_bytes": sent_bytes * store_count,
            "fanout_throughput_kbps": round((sent_bytes * store_count * 8) / broadcast_seconds / 1000, 1),
            "backend_cpu_seconds": cpu_seconds,
            "backend_rss_mb_before": round(cost_before.get("rss", 0) / 1048576, 1) if cost_before else None,
            "backend_rss_mb_after": round(cost_after.get("rss", 0) / 1048576, 1) if cost_after else None,
            "receiver_errors": sum(len(r.errors) for r in receivers),
            "explicit_stop_http": stop_status,
            # Server-side truth about the bounded queues, sampled while live.
            # Distinct from dropped_chunks_total above, which is what the
            # Receivers noticed missing - the two answer different questions and
            # a report that conflated them would be guessing at the difference.
            "server_queue_metrics": _summarise_queue_metrics(queue_metrics),
            "queues_after_stop": _read_audio_metrics(base_url, token),
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in receivers), return_exceptions=True)


def run_load_test(store_count: int, pilot_root: Path | None = None) -> dict:
    paths = resolve_pilot_paths(pilot_root)
    prepare(paths)

    fixture = paths.root / "audio" / "fixture.webm"
    if not fixture.exists():
        candidates = sorted((paths.root / "audio").glob("*.webm"))
        if not candidates:
            raise AudioPilotError("no WebM fixture was prepared")
        fixture = candidates[0]

    port = _free_loopback_port()
    environment = dict(os.environ)
    environment["ECHOCAST_DB_PATH"] = str(paths.database_path)
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--log-level", "warning"],
        cwd=str(BACKEND_DIRECTORY), env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 40
        import urllib.error
        import urllib.request
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.3)
        else:
            raise AudioPilotError("the load-test backend never became reachable")

        return asyncio.run(_drive(paths, store_count, port, backend.pid, fixture))
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="load_test_receivers",
        description="Run a synthetic multi-Store load test against a loopback backend.",
    )
    parser.add_argument("--stores", type=int, action="append", required=True,
                        help="Store count for one run; repeat for a sweep.")
    parser.add_argument("--pilot-root", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None,
                        help="Optional secret-free JSON report path.")
    arguments = parser.parse_args(argv)

    results = []
    for count in arguments.stores:
        print(f"\n=== {count} Stores ===", flush=True)
        result = run_load_test(count, arguments.pilot_root)
        results.append(result)
        for key, value in sorted(result.items()):
            print(f"  {key}: {value}")

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\nreport written to {arguments.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
