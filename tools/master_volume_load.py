"""Load the always-on Master Volume path, with NO broadcast anywhere.

WHY THIS IS A SEPARATE HARNESS

``audio_control_load`` measures the estate while an announcement is playing.
This measures the other 23 hours of the day: Receivers connected, nothing on
air, and shop staff nudging Windows volume sliders. That is now a live path -
endpoint observation no longer starts at PREPARE - and it had never been
measured under load at all.

WHAT IT DELIBERATELY MIXES IN

A perfect estate proves only the happy path, so the roles below are the ones a
real rollout actually contains: a noisy Store whose slider is being dragged, a
slow Store, a Store with no controllable output, and one that is offline
throughout and can only be given a pending change.

Every Receiver here is synthetic and no audio device is opened. A pass says the
control plane and telemetry hold up at N Stores. It says nothing about
audibility.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIRECTORY):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.local_audio_pilot import (  # noqa: E402
    AudioPilotError,
    _api_post,
    _free_loopback_port,
    prepare,
)
from tools.local_pilot import _pilot_environment, resolve_pilot_paths  # noqa: E402
from tools.load_test_receivers import (  # noqa: E402
    SyntheticReceiver,
    _process_cost,
    _store_credentials,
    broadcast_started_at,
    prepare_clock,
)


def _install_primary_devices(database_path, store_ids) -> None:
    """Give each pilot Store an active primary Receiver Device."""
    import sqlite3
    from uuid import uuid4

    now = "2026-08-06T00:00:00+00:00"
    connection = sqlite3.connect(str(database_path))
    try:
        for store_id in store_ids:
            existing = connection.execute(
                "SELECT device_id FROM receiver_store_primary_device "
                "WHERE store_id = ?", (store_id,)).fetchone()
            if existing:
                continue
            cursor = connection.execute(
                "INSERT INTO receiver_devices "
                "(public_id, store_id, display_name, status, enrolled_at, "
                " created_at, updated_at) "
                "VALUES (?, ?, 'LOAD-TEST-PC', 'active', ?, ?, ?)",
                (str(uuid4()), store_id, now, now, now))
            connection.execute(
                "INSERT INTO receiver_store_primary_device "
                "(store_id, device_id, promoted_at) VALUES (?, ?, ?)",
                (store_id, cursor.lastrowid, now))
        connection.commit()
    finally:
        connection.close()


async def _drive(paths, store_count: int, port: int, backend_pid: int) -> dict:
    base_url = f"http://127.0.0.1:{port}"
    receiver_url = f"ws://127.0.0.1:{port}/api/ws/receiver"

    token = _api_post(
        base_url, "/api/auth/login", None,
        {"username": os.environ["ADMIN_USERNAME"],
         "password": os.environ["ADMIN_PASSWORD"]},
    )["access_token"]

    credentials = _store_credentials(paths.database_path, store_count)
    # The panel lists Stores with an INSTALLED active primary Receiver Device,
    # which is the product rule and not something this harness may skip. The
    # synthetic Receivers authenticate with legacy Store tokens, so the Device
    # rows have to be created explicitly or every Store would be absent.
    _install_primary_devices(paths.database_path,
                             [store_id for store_id, _, _ in credentials])
    receivers = []
    for index, (store_id, code, secret) in enumerate(credentials):
        receiver = SyntheticReceiver(store_id, code, secret, receiver_url)
        if store_count >= 5 and index == 2:
            # Upgraded, but the Windows output was never re-selected. It must
            # report nothing and be refused control rather than silently doing
            # nothing.
            receiver.endpoint_configured = False
        receivers.append(receiver)

    noisy = receivers[0]
    slow = receivers[3] if len(receivers) > 3 else receivers[0]
    # Deliberately never connected: the offline case is the reason the pending
    # mechanism exists, and it has to be measured rather than assumed.
    offline = receivers[-1]
    connecting = [r for r in receivers if r is not offline]

    started = asyncio.Event()
    started.set()                       # no broadcast; nothing to wait for
    prepare_clock[0] = time.perf_counter()
    broadcast_started_at[0] = time.perf_counter()

    await asyncio.gather(*(r.connect() for r in connecting))
    tasks = [asyncio.create_task(r.run(started)) for r in connecting]
    tasks += [asyncio.create_task(r.telemetry_pump()) for r in connecting]

    cost_before = _process_cost(backend_pid)
    test_started = time.perf_counter()
    http_latencies_ms: list[float] = []
    refusals: dict[str, int] = {}
    accepted = 0

    try:
        # Idle telemetry needs a session id on the Receiver side to be sent,
        # and there is none - which is exactly the change under test. The
        # synthetic Receiver reports with no session, so seed a first reading.
        for receiver in connecting:
            receiver.session_id = None
            receiver.local_change(50)
        await asyncio.sleep(1.0)

        def panel() -> dict:
            import requests
            issued = time.perf_counter()
            response = requests.get(f"{base_url}/api/store-audio/master",
                                    headers={"Authorization": f"Bearer {token}"},
                                    timeout=20)
            http_latencies_ms.append((time.perf_counter() - issued) * 1000)
            response.raise_for_status()
            return {row["store_id"]: row for row in response.json()["stores"]}

        def command(store_id, **body):
            import requests
            issued = time.perf_counter()
            response = requests.post(
                f"{base_url}/api/store-audio/master/{store_id}",
                headers={"Authorization": f"Bearer {token}"}, json=body,
                timeout=20)
            http_latencies_ms.append((time.perf_counter() - issued) * 1000)
            return response.status_code

        # ---- a hard drag on one Store, while everything else is quiet -----
        for level in range(30, 70):
            noisy.local_change(level)
        # ---- the slow Store reports too; its lateness is its own ----------
        slow.audio_control_delay_seconds = 0.75
        slow.local_change(41)
        # ---- the Store with no controllable output tries and reports none --
        receivers[2].local_change(42) if len(receivers) > 2 else None
        await asyncio.sleep(1.5)

        rows = panel()

        # ---- HQ drives every connected Store -------------------------------
        for receiver in connecting:
            status = command(receiver.store_id, volume_percent=60)
            if status == 200:
                accepted += 1
            else:
                refusals[str(status)] = refusals.get(str(status), 0) + 1
        await asyncio.sleep(1.5)

        # ---- the offline Store is given a TARGET state, and accepts it ----
        # It is offline throughout, so this measures the case the whole rework
        # exists for: the operator is never refused because a shop is off.
        offline_status = command(offline.store_id, volume_percent=70)
        # Latest wins: three requests are one intention.
        command(offline.store_id, volume_percent=30)
        command(offline.store_id, volume_percent=70)

        after = panel()
        cost_after = _process_cost(backend_pid)

        offline_row = after.get(offline.store_id, {})
        unconfigured = receivers[2] if len(receivers) > 2 else None

        cpu_seconds = None
        if cost_before.get("cpu") is not None and cost_after.get("cpu") is not None:
            cpu_seconds = round(cost_after["cpu"] - cost_before["cpu"], 2)

        return {
            "stores": store_count,
            "connected": len(connecting),
            "duration_seconds": round(time.perf_counter() - test_started, 1),
            "panel_rows": len(after),
            # Offline Stores MUST still be listed. Hiding them would hide the
            # ones worth looking at.
            "offline_store_listed": offline.store_id in after,
            "offline_store_is_stale": offline_row.get("stale"),
            "offline_store_says_offline": offline_row.get("control_status") == "OFFLINE",
            "offline_desired_volume": offline_row.get("target_volume_percent"),
            "offline_sync_state": offline_row.get("sync_state"),
            "offline_command_status": offline_status,
            # 43 local changes must not become 43 frames on the wire.
            "telemetry_generated": sum(r.endpoint_states_generated for r in connecting),
            "telemetry_transmitted": sum(
                r.endpoint_states_transmitted for r in connecting),
            "noisy_store_code": noisy.store_code,
            "noisy_generated": noisy.endpoint_states_generated,
            "noisy_transmitted": noisy.endpoint_states_transmitted,
            "hq_matches_noisy_store": (
                after.get(noisy.store_id, {}).get("volume_percent")
                == noisy.endpoint_volume),
            # A Store with no selected output reports nothing and is refused.
            "unconfigured_store_code": (
                unconfigured.store_code if unconfigured else None),
            "unconfigured_transmitted": (
                unconfigured.endpoint_states_transmitted if unconfigured else None),
            "commands_accepted": accepted,
            "commands_refused": refusals,
            "http_latency_ms": {
                "avg": round(sum(http_latencies_ms) / len(http_latencies_ms), 2)
                if http_latencies_ms else None,
                "max": round(max(http_latencies_ms), 2) if http_latencies_ms else None,
            },
            "cpu_seconds": cpu_seconds,
            "rss_mb_before": (round(cost_before["rss"] / 1048576, 1)
                              if cost_before.get("rss") else None),
            "rss_mb_after": (round(cost_after["rss"] / 1048576, 1)
                             if cost_after.get("rss") else None),
            "receiver_errors": {r.store_code: r.errors for r in connecting if r.errors},
            # Nothing on air at any point. If this is not zero the run measured
            # the wrong thing entirely.
            "broadcasts_started": 0,
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in connecting),
                             return_exceptions=True)


def run_master_volume_load(store_count: int, pilot_root: Path | None = None) -> dict:
    paths = resolve_pilot_paths(pilot_root)
    prepare(paths)

    port = _free_loopback_port()
    environment = _pilot_environment(paths)
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--workers", "1", "--log-level", "warning"],
        cwd=str(BACKEND_DIRECTORY), env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        import urllib.error
        import urllib.request

        deadline = time.time() + 40
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/", timeout=2)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise AudioPilotError("the pilot backend did not become reachable")
        return asyncio.run(_drive(paths, store_count, port, backend.pid))
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", type=int, action="append", default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--pilot-root", type=Path, default=None)
    arguments = parser.parse_args(argv)

    counts = arguments.stores or [5, 10, 20, 40]
    results = []
    for count in counts:
        print(f"--- {count} Stores, no broadcast ---", flush=True)
        result = run_master_volume_load(count, arguments.pilot_root)
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)

    if arguments.report:
        arguments.report.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"report written: {arguments.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
