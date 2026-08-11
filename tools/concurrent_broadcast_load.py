"""Several live broadcasts at once, measured rather than assumed.

WHAT THIS ADDS TO load_test_receivers.py

That harness drives ONE broadcast to N Stores and measures what the backend
cost. It cannot express the question this checkpoint asks, because every part
of it - one session id, one broadcaster socket, one target set - assumes the
singleton runtime that has since been replaced.

This one drives M concurrent sessions over disjoint Store sets, each with its
own microphone socket, and measures them together. It REUSES that harness's
bootstrap (throwaway pilot profile, one Uvicorn worker on a free loopback
port, synthetic Receivers) rather than reimplementing it: a second way to
start a backend is a second way to be wrong about what was measured.

MARKERS, NOT AUDIO

Each session streams a distinguishable byte payload rather than the WebM
fixture. That is deliberate and is the ONLY honest way to answer "did session
A's audio reach session B's Stores" - identical Opus frames are
indistinguishable once delivered, so a leak would be invisible.

It also means these runs measure ROUTING AND QUEUEING, not decoding. Nothing
here says anything about whether a Receiver could play the bytes; that is what
local_audio_pilot.py and the staging smoke already prove with a real fixture.
Chunk size and cadence still match the live profile (~32 kbps mono Opus at
250 ms) so queue pressure is realistic.

ONE UVICORN WORKER, ON PURPOSE

The deployment runs one worker; measuring anything else would measure a
system nobody plans to pilot. WebSocket state is process-local, so a second
worker would also break the product.

NEVER TOUCHES ANYTHING LIVE

A throwaway pilot database, synthetic Store credentials from it, and a free
loopback port. Ports 3000 and 8000 are never used and the live catalog is
never opened.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import websockets

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIRECTORY):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.load_test_receivers import (  # noqa: E402
    SyntheticReceiver,
    _process_cost,
    _store_credentials,
)
from tools.local_audio_pilot import (  # noqa: E402
    AudioPilotError,
    _api_post,
    _free_loopback_port,
    prepare,
)
from tools.local_pilot import resolve_pilot_paths  # noqa: E402

#: ~32 kbps mono Opus at 250 ms is about 1 kB per chunk. The exact number
#: matters less than that it is realistic: queue pressure is driven by chunks
#: per second per Store, and that is pinned exactly.
CHUNK_BYTES = 1024
CHUNK_INTERVAL_SECONDS = 0.25

#: Set to a path to keep the load backend's stderr for diagnosis.
BACKEND_LOG = os.environ.get("SPEAKLINK_LOAD_BACKEND_LOG")


class MarkedReceiver(SyntheticReceiver):
    """A synthetic Receiver that remembers WHICH session's bytes it saw.

    The base class counts chunks, which cannot answer the question that
    matters most here. Every payload begins with a fixed-width marker naming
    the session that sent it, and this records the distinct set - so a single
    leaked chunk is visible, not averaged away.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.markers_seen: set = set()
        self.chunk_arrivals: list = []

    async def run(self, broadcast_started: asyncio.Event) -> None:  # noqa: D102
        session_id = None
        last_beat = time.perf_counter()
        try:
            while True:
                # The heartbeat is on a CLOCK, not on the idle branch. The
                # base harness only beat when recv() timed out, which never
                # happens while audio is flowing - so a run longer than the
                # server's heartbeat window lost every Receiver, and it looked
                # like the product dropping connections under load. It was the
                # harness never saying anything.
                if time.perf_counter() - last_beat >= 3.0:
                    await self._send("heartbeat")
                    last_beat = time.perf_counter()
                try:
                    message = await asyncio.wait_for(self._socket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if isinstance(message, bytes):
                    self.markers_seen.add(message[:8])
                    self.chunk_arrivals.append(time.perf_counter())
                    if self.chunks == 0:
                        await self._send("audio_receiving", session_id=session_id)
                        await self._send("playback_confirmed", session_id=session_id)
                    self.chunks += 1
                    self.bytes += len(message)
                    continue

                payload = json.loads(message)
                kind = payload.get("type")
                if kind == "prepare":
                    session_id = (payload.get("broadcast_session_id")
                                  or payload.get("session_id"))
                    await self._send("receiver_ready", session_id=session_id)
                    self.states.append("READY")
                elif kind == "stop":
                    await self._send("stopped", session_id=session_id)
                    self.states.append("STOPPED")
                elif kind == "ack_rejected":
                    self.errors.append(str(payload.get("code")))
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            self.errors.append(type(failure).__name__)
            return


class StalledReceiver(MarkedReceiver):
    """A Receiver that stops reading, to fill one Store's bounded queue.

    Not a Receiver that is slow to acknowledge - one that stops draining its
    socket entirely, which is what a Store on a failing link actually looks
    like from HQ. The queue behind it must stay bounded and must not affect
    anybody else.
    """

    async def run(self, broadcast_started: asyncio.Event) -> None:  # noqa: D102
        # Answer PREPARE so the session can reach READY, then go silent.
        try:
            message = await asyncio.wait_for(self._socket.recv(), timeout=20)
            if not isinstance(message, bytes):
                payload = json.loads(message)
                if payload.get("type") == "prepare":
                    await self._send(
                        "receiver_ready",
                        session_id=(payload.get("broadcast_session_id")
                                    or payload.get("session_id")))
                    self.states.append("READY")
        except Exception as failure:
            self.errors.append(type(failure).__name__)
        # Deliberately never reads again.
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


@dataclass
class SessionPlan:
    name: str
    store_ids: list
    marker: bytes
    session_id: int = 0
    start_ms: float = 0.0
    lease_ms: float = 0.0
    socket: object = None
    chunks_sent: int = 0
    enqueue_latencies: list = field(default_factory=list)


def _summarise(values) -> dict:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"samples": 0}
    ordered = sorted(clean)
    return {
        "samples": len(ordered),
        "min_ms": round(ordered[0], 2),
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 2),
        "max_ms": round(ordered[-1], 2),
    }


def _audio_metrics(base_url: str, token: str) -> dict:
    import requests

    try:
        response = requests.get(f"{base_url}/api/broadcast/audio-metrics",
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=10)
        return response.json() if response.status_code == 200 else {
            "unavailable": f"HTTP {response.status_code}"}
    except Exception as failure:
        return {"unavailable": type(failure).__name__}


def _queue_summary(metrics: dict) -> dict:
    rows = (metrics or {}).get("stores") or []
    if not rows:
        return {"stores_measured": 0,
                "note": (metrics or {}).get("unavailable", "no queue existed when sampled")}
    capacity = metrics.get("capacity")
    dropped = {r["store_id"]: r.get("dropped", 0) for r in rows if r.get("dropped")}
    return {
        "capacity": capacity,
        "stores_measured": len(rows),
        "max_depth_observed": max(r.get("max_depth", 0) for r in rows),
        "depth_now_max": max(r.get("depth", 0) for r in rows),
        "enqueued_total": sum(r.get("enqueued", 0) for r in rows),
        "delivered_total": sum(r.get("delivered", 0) for r in rows),
        "dropped_total": sum(r.get("dropped", 0) for r in rows),
        "dropped_by_store": dropped,
        # If this is ever False the queue is not bounded, which is a P0 and
        # not a note.
        "every_queue_within_capacity": all(
            r.get("max_depth", 0) <= (capacity or 0) for r in rows),
    }


class ConcurrentLoadRun:
    """One scenario: M sessions over N Stores, measured end to end."""

    def __init__(self, *, name, plans, paths, port, backend_pid,
                 duration_seconds, stalled_store_id=None,
                 chunk_bytes=CHUNK_BYTES):
        self.name = name
        self.plans = plans
        self.paths = paths
        self.port = port
        self.backend_pid = backend_pid
        self.duration_seconds = duration_seconds
        self.stalled_store_id = stalled_store_id
        # Larger chunks are how a stalled Store is made to exert REAL
        # backpressure. At the live 1 kB profile a stalled socket simply
        # absorbs everything into TCP buffers for the length of a short run,
        # so the bounded queue is never reached and a "slow Store" scenario
        # proves nothing about the bound. Raising this fills the buffer in
        # seconds instead of minutes.
        self.chunk_bytes = chunk_bytes
        self.base_url = f"http://127.0.0.1:{port}"
        self.receiver_url = f"ws://127.0.0.1:{port}/api/ws/receiver"
        self.samples = []
        self._errors_at_stop = {}

    async def _post(self, path, token, body=None):
        """HTTP from inside the event loop, off the event loop.

        _api_post is synchronous requests. Calling it directly from a
        coroutine blocks the loop that owns the Receiver tasks and the
        microphone-socket drains - so the server's close handshake cannot be
        answered while a Stop request is in flight, and the Stop then waits
        for the full HTTP timeout. Found the hard way: every stop took exactly
        20 seconds.
        """
        return await asyncio.to_thread(_api_post, self.base_url, path, token, body)

    async def run(self) -> dict:
        token = _api_post(self.base_url, "/api/auth/login", None, {
            "username": os.environ["ADMIN_USERNAME"],
            "password": os.environ["ADMIN_PASSWORD"],
        })["access_token"]

        store_ids = [sid for plan in self.plans for sid in plan.store_ids]
        credentials = {row[0]: row for row in
                       _store_credentials(self.paths.database_path, max(store_ids) + 1)
                       if row[0] in store_ids}
        receivers = {}
        for store_id in store_ids:
            _sid, code, secret = credentials[store_id]
            cls = (StalledReceiver if store_id == self.stalled_store_id
                   else MarkedReceiver)
            receivers[store_id] = cls(store_id, code, secret, self.receiver_url)

        await asyncio.gather(*(r.connect() for r in receivers.values()))
        started = asyncio.Event()
        tasks = [asyncio.create_task(r.run(started)) for r in receivers.values()]
        drains = []

        cost_before = _process_cost(self.backend_pid)
        result = {"scenario": self.name,
                  "store_count": len(store_ids),
                  "session_count": len(self.plans),
                  "chunk_bytes": self.chunk_bytes,
                  "chunk_interval_seconds": CHUNK_INTERVAL_SECONDS,
                  "stalled_store_id": self.stalled_store_id}
        try:
            # ---- create and start every session ----
            for plan in self.plans:
                created = await self._post("/api/broadcast/sessions", token, {
                    "campaign_name": plan.name,
                    "target_mode": "selected",
                    "store_ids": plan.store_ids,
                })
                plan.session_id = created["id"]

                claim_started = time.perf_counter()
                await self._post(f"/api/broadcast/sessions/{plan.session_id}/start",
                                 token)
                plan.start_ms = (time.perf_counter() - claim_started) * 1000
                # The lease is claimed inside that call, so its duration is the
                # closest honest measure of acquisition cost available without
                # instrumenting the server.
                plan.lease_ms = plan.start_ms

            await asyncio.sleep(1.5)   # let PREPARE/READY settle

            # ---- one microphone socket per session ----
            for plan in self.plans:
                ticket = (await self._post("/api/auth/ws-ticket", token,
                                           {"audience": "broadcaster"}))["ticket"]
                plan.socket = await websockets.connect(
                    f"ws://127.0.0.1:{self.port}/api/ws/broadcaster"
                    f"?ticket={ticket}&session_id={plan.session_id}",
                    open_timeout=20, max_size=4 * 1024 * 1024)
                # A reader per microphone socket. Without one this harness
                # never processes the server's CLOSE frame, so _end_session's
                # `await socket.close()` waits for a handshake that can never
                # complete and every Stop takes the full HTTP timeout. That is
                # a defect in the harness, not the product - a real browser
                # always has a reader - and it cost a 20-second stop before it
                # was found.
                drains.append(asyncio.create_task(self._drain(plan)))

            started.set()
            await self._stream(token, result)

            # ---- stop every session, measuring ----
            stop_started = time.perf_counter()
            await asyncio.gather(*(
                self._post(f"/api/broadcast/sessions/{plan.session_id}/stop", token)
                for plan in self.plans))
            result["stop_all_ms"] = round((time.perf_counter() - stop_started) * 1000, 2)

            cleanup_started = time.perf_counter()
            leftover = await self._await_clean(token)
            result["cleanup_ms"] = round((time.perf_counter() - cleanup_started) * 1000, 2)
            result["leftover_after_stop"] = leftover
            # Snapshot BEFORE teardown. Cancelling the Receiver tasks and
            # terminating the backend closes every socket, so errors read
            # afterwards are the harness shutting itself down - which is not
            # what "a Receiver reported an error" is supposed to mean.
            self._errors_at_stop = {
                store_id: list(receiver.errors)
                for store_id, receiver in receivers.items() if receiver.errors
            }
        finally:
            for plan in self.plans:
                if plan.socket is not None:
                    try:
                        await plan.socket.close()
                    except Exception:
                        pass
            for task in tasks + drains:
                task.cancel()
            await asyncio.gather(*(tasks + drains), return_exceptions=True)
            await asyncio.gather(*(r.close() for r in receivers.values()),
                                 return_exceptions=True)

        cost_after = _process_cost(self.backend_pid)
        result.update(self._resource_summary(cost_before, cost_after))
        result.update(self._isolation_summary(receivers))
        result["start_latency"] = _summarise([p.start_ms for p in self.plans])
        result["lease_latency"] = _summarise([p.lease_ms for p in self.plans])
        result["enqueue_latency"] = _summarise(
            [v for p in self.plans for v in p.enqueue_latencies])
        result["samples"] = self.samples
        return result

    @staticmethod
    async def _drain(plan) -> None:
        """Consume whatever the server sends on a microphone socket.

        The uplink is write-mostly, but a WebSocket peer that never reads also
        never answers a close frame."""
        try:
            while True:
                await plan.socket.recv()
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _stream(self, token, result) -> None:
        """Send marked chunks to every session on the live cadence."""
        deadline = time.perf_counter() + self.duration_seconds
        next_sample = time.perf_counter()
        while time.perf_counter() < deadline:
            tick = time.perf_counter()
            for plan in self.plans:
                payload = plan.marker + os.urandom(self.chunk_bytes - len(plan.marker))
                send_started = time.perf_counter()
                try:
                    await plan.socket.send(payload)
                except Exception:
                    break
                plan.enqueue_latencies.append(
                    (time.perf_counter() - send_started) * 1000)
                plan.chunks_sent += 1

            if time.perf_counter() >= next_sample:
                self.samples.append(await asyncio.to_thread(self._sample, token))
                next_sample = time.perf_counter() + 5

            elapsed = time.perf_counter() - tick
            await asyncio.sleep(max(0.0, CHUNK_INTERVAL_SECONDS - elapsed))

        result["queue_metrics_during"] = _queue_summary(
            await asyncio.to_thread(_audio_metrics, self.base_url, token))

    def _sample(self, token) -> dict:
        cost = _process_cost(self.backend_pid)
        queues = _queue_summary(_audio_metrics(self.base_url, token))
        return {
            "at": round(time.perf_counter(), 2),
            "rss_bytes": cost.get("rss"),
            "cpu_seconds": cost.get("cpu"),
            "max_depth": queues.get("max_depth_observed"),
            "dropped_total": queues.get("dropped_total"),
        }

    async def _await_clean(self, token, timeout=20) -> dict:
        """Wait for the runtime and the leases to empty, and report what did
        not."""
        import requests

        deadline = time.perf_counter() + timeout
        leftover = {}
        while time.perf_counter() < deadline:
            metrics = await asyncio.to_thread(_audio_metrics, self.base_url, token)
            sessions = metrics.get("session_count")
            if sessions == 0:
                break
            await asyncio.sleep(0.25)
        leftover["runtime_sessions"] = (await asyncio.to_thread(
            _audio_metrics, self.base_url, token)).get("session_count")
        try:
            response = await asyncio.to_thread(
                requests.get, f"{self.base_url}/api/broadcast/active",
                headers={"Authorization": f"Bearer {token}"}, timeout=10)
            leftover["busy_store_ids"] = response.json().get("busy_store_ids")
        except Exception as failure:
            leftover["busy_store_ids"] = f"unreadable: {type(failure).__name__}"
        return leftover

    def _resource_summary(self, before, after) -> dict:
        rss_values = [s["rss_bytes"] for s in self.samples if s.get("rss_bytes")]
        cpu_seconds = None
        if before.get("cpu") is not None and after.get("cpu") is not None:
            cpu_seconds = round(after["cpu"] - before["cpu"], 2)
        return {
            "backend_cpu_seconds": cpu_seconds,
            "backend_cpu_percent_of_one_core": (
                round(100 * cpu_seconds / self.duration_seconds, 1)
                if cpu_seconds is not None and self.duration_seconds else None),
            "rss_baseline_bytes": before.get("rss"),
            "rss_peak_bytes": max(rss_values) if rss_values else after.get("rss"),
            "rss_delta_bytes": (
                (after.get("rss") or 0) - (before.get("rss") or 0)
                if before.get("rss") and after.get("rss") else None),
        }

    def _isolation_summary(self, receivers) -> dict:
        """Did any Store receive bytes from a session that was not targeting
        it? A single chunk is a failure, so this reports the offenders rather
        than a rate."""
        expected = {}
        for plan in self.plans:
            for store_id in plan.store_ids:
                expected[store_id] = plan.marker

        offenders = {}
        silent = []
        for store_id, receiver in receivers.items():
            seen = getattr(receiver, "markers_seen", set())
            foreign = {m.decode("ascii", "replace") for m in seen
                       if m != expected[store_id]}
            if foreign:
                offenders[store_id] = sorted(foreign)
            if not seen and store_id != self.stalled_store_id:
                silent.append(store_id)
        return {
            "cross_session_leaks": offenders,
            "stores_that_received_nothing": silent,
            "chunks_sent_by_session": {p.name: p.chunks_sent for p in self.plans},
            "chunks_received_total": sum(r.chunks for r in receivers.values()),
            "receiver_errors": self._errors_at_stop,
            "receiver_errors_after_teardown": {
                sid: r.errors for sid, r in receivers.items() if r.errors},
        }


def build_plans(store_ids, distribution) -> list:
    """Split Stores across sessions according to an explicit distribution."""
    plans = []
    cursor = 0
    for index, size in enumerate(distribution):
        marker = f"SESS{index:04d}".encode("ascii")  # exactly 8 bytes
        assert len(marker) == 8
        plans.append(SessionPlan(
            name=f"Load session {index}",
            store_ids=store_ids[cursor:cursor + size],
            marker=marker,
        ))
        cursor += size
    return plans


def _start_backend(paths, port):
    """One Uvicorn worker on a free loopback port, against the throwaway
    pilot profile. One worker because that is the deployment - and because
    WebSocket state is process-local, so a second would break the product."""
    environment = dict(os.environ)
    environment["SPEAKLINK_DB_PATH"] = str(paths.database_path)
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--workers", "1", "--log-level", "warning"],
        cwd=str(BACKEND_DIRECTORY), env=environment,
        stdout=subprocess.DEVNULL,
        # Captured, not discarded: a backend that refuses to start is the most
        # likely reason a load run fails, and DEVNULL makes that invisible.
        stderr=(open(BACKEND_LOG, "w", encoding="utf-8") if BACKEND_LOG else subprocess.DEVNULL),
    )
    import urllib.error
    import urllib.request

    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/", timeout=2)
            return backend
        except urllib.error.HTTPError:
            return backend
        except Exception:
            time.sleep(0.3)
    backend.kill()
    raise AudioPilotError("the load-test backend never became reachable")


def run_scenario(*, name, distribution, duration_seconds, stall_first_store=False,
                 pilot_root=None, chunk_bytes=CHUNK_BYTES) -> dict:
    paths = resolve_pilot_paths(pilot_root)
    prepare(paths)
    total = sum(distribution)
    credentials = _store_credentials(paths.database_path, total)
    store_ids = [row[0] for row in credentials]

    plans = build_plans(store_ids, distribution)
    stalled = plans[0].store_ids[0] if stall_first_store else None

    port = _free_loopback_port()
    backend = _start_backend(paths, port)
    try:
        run = ConcurrentLoadRun(
            name=name, plans=plans, paths=paths, port=port,
            backend_pid=backend.pid, duration_seconds=duration_seconds,
            stalled_store_id=stalled, chunk_bytes=chunk_bytes,
        )
        return asyncio.run(run.run())
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


def classify(result) -> str:
    """GREEN / YELLOW / RED from measured facts, never from a target number.

    RED is reserved for correctness: audio crossing sessions, an unbounded
    queue, a Store left permanently busy, or a leaked runtime session. Those
    block a release. Resource cost is a pilot-observation question, not a
    correctness one, so it never turns a run RED by itself.
    """
    queues = result.get("queue_metrics_during") or {}
    leftover = result.get("leftover_after_stop") or {}
    if result.get("cross_session_leaks"):
        return "RED: audio crossed between sessions"
    if queues.get("every_queue_within_capacity") is False:
        return "RED: a bounded queue exceeded its capacity"
    if leftover.get("runtime_sessions"):
        return "RED: runtime sessions survived stop"
    if leftover.get("busy_store_ids"):
        return "RED: Store leases survived stop"
    if result.get("receiver_errors"):
        return "YELLOW: a Receiver reported an error"
    healthy_drops = {
        store: count for store, count in (queues.get("dropped_by_store") or {}).items()
        if int(store) != (result.get("stalled_store_id") or -1)
    }
    if healthy_drops:
        return f"YELLOW: healthy Stores dropped chunks {healthy_drops}"
    return "GREEN"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="concurrent_broadcast_load",
        description="Measure several concurrent SpeakLink broadcasts.")
    parser.add_argument("--scenario", action="append", required=True,
                        help="name:dist,dist,dist:seconds[:stall]")
    parser.add_argument("--pilot-root", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--chunk-bytes", type=int, default=CHUNK_BYTES,
                        help="Payload size. Raise it to make a stalled Store "
                             "exert real backpressure within a short run.")
    arguments = parser.parse_args(argv)

    results = []
    for raw in arguments.scenario:
        parts = raw.split(":")
        name, distribution, seconds = parts[0], parts[1], float(parts[2])
        stall = len(parts) > 3 and parts[3] == "stall"
        result = run_scenario(
            name=name,
            distribution=[int(x) for x in distribution.split(",")],
            duration_seconds=seconds,
            stall_first_store=stall,
            pilot_root=arguments.pilot_root,
            chunk_bytes=arguments.chunk_bytes,
        )
        result["verdict"] = classify(result)
        results.append(result)
        print(f"\n=== {name} === {result['verdict']}", flush=True)
        for key in ("store_count", "session_count", "backend_cpu_percent_of_one_core",
                    "rss_peak_bytes", "rss_delta_bytes", "stop_all_ms", "cleanup_ms",
                    "cross_session_leaks", "queue_metrics_during", "enqueue_latency",
                    "start_latency", "leftover_after_stop"):
            print(f"  {key}: {result.get(key)}", flush=True)

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(results, indent=2, sort_keys=True,
                                               default=str), encoding="utf-8")
        print(f"\nreport written to {arguments.report}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
