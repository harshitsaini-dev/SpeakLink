"""The concurrent-broadcast behaviours that are not throughput measurements.

Churn, a contended Store, Emergency Stop at scale and restart recovery. Each
answers a correctness question that a streaming run cannot: they are about
what the system OWNS afterwards - leases, runtime sessions, history - rather
than how fast it moved bytes.

They share one backend process and one throwaway pilot profile, because
standing up a fresh backend per question would take longer than the questions
do and would test the bootstrap rather than the product.

Nothing here touches the live catalog, live ports or live credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIRECTORY):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import requests  # noqa: E402

from tools.concurrent_broadcast_load import _start_backend  # noqa: E402
from tools.load_test_receivers import _store_credentials  # noqa: E402
from tools.local_audio_pilot import _api_post, _free_loopback_port, prepare  # noqa: E402
from tools.local_pilot import resolve_pilot_paths  # noqa: E402


def _login(base_url):
    return _api_post(base_url, "/api/auth/login", None, {
        "username": os.environ["ADMIN_USERNAME"],
        "password": os.environ["ADMIN_PASSWORD"],
    })["access_token"]


def _active(base_url, token):
    response = requests.get(f"{base_url}/api/broadcast/active",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10)
    return response.json()


def _create_and_start(base_url, token, name, store_ids):
    created = _api_post(base_url, "/api/broadcast/sessions", token, {
        "campaign_name": name, "target_mode": "selected", "store_ids": store_ids})
    session_id = created["id"]
    response = requests.post(
        f"{base_url}/api/broadcast/sessions/{session_id}/start",
        headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return session_id, response


def churn(base_url, token, store_ids, cycles=20) -> dict:
    """Start and stop repeatedly. Nothing may accumulate.

    The failure this looks for is a Store that becomes permanently BUSY after
    enough cycles - a lease that was claimed and never released, which no
    single-cycle test can see.
    """
    started = time.perf_counter()
    failures = []
    for cycle in range(cycles):
        groups = [store_ids[0:2], store_ids[2:4], store_ids[4:6]]
        sessions = []
        for index, group in enumerate(groups):
            session_id, response = _create_and_start(
                base_url, token, f"Churn {cycle}-{index}", group)
            if response.status_code != 200:
                failures.append({"cycle": cycle, "group": index,
                                 "status": response.status_code})
                continue
            sessions.append(session_id)
        for session_id in sessions:
            requests.post(f"{base_url}/api/broadcast/sessions/{session_id}/stop",
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)

    final = _active(base_url, token)
    return {
        "cycles": cycles,
        "duration_seconds": round(time.perf_counter() - started, 1),
        "start_failures": failures,
        "busy_store_ids_after": final.get("busy_store_ids"),
        "sessions_after": final.get("sessions"),
        "mine_after": final.get("mine"),
    }


def collision(base_url, token, contended_store, background_stores) -> dict:
    """Two operators race for one free Store while others are streaming."""
    background_id, background_response = _create_and_start(
        base_url, token, "Collision background", background_stores)

    def attempt(index):
        return _create_and_start(base_url, token, f"Collision {index}",
                                 [contended_store])

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))

    statuses = [response.status_code for _sid, response in outcomes]
    bodies = []
    for _sid, response in outcomes:
        try:
            bodies.append(response.json())
        except Exception:
            bodies.append(None)

    winners = statuses.count(200)
    busy = [b for b in bodies
            if isinstance(b, dict) and (b.get("detail") or {}).get("code") == "STORE_BUSY"]
    result = {
        "statuses": statuses,
        "winners": winners,
        "store_busy_refusals": len(busy),
        "background_still_live": background_response.status_code == 200,
        "conflict_body_names_no_owner": all(
            not any(key in json.dumps(b).lower()
                    for key in ("owner", "username", "started_by", "campaign_name"))
            for b in busy) if busy else None,
    }
    # Clean up whatever won.
    for session_id, response in outcomes + [(background_id, background_response)]:
        if response.status_code == 200:
            requests.post(f"{base_url}/api/broadcast/sessions/{session_id}/stop",
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
    return result


def emergency_stop_at_scale(base_url, token, store_ids, sessions=6) -> dict:
    """Several live sessions, then one Emergency Stop All."""
    per_session = max(1, len(store_ids) // sessions)
    live = []
    for index in range(sessions):
        group = store_ids[index * per_session:(index + 1) * per_session]
        if not group:
            break
        session_id, response = _create_and_start(
            base_url, token, f"Emergency {index}", group)
        if response.status_code == 200:
            live.append(session_id)

    before = _active(base_url, token)
    started = time.perf_counter()
    response = requests.post(f"{base_url}/api/broadcast/emergency-stop",
                             headers={"Authorization": f"Bearer {token}"}, timeout=60)
    api_ms = (time.perf_counter() - started) * 1000

    settled = time.perf_counter()
    after = _active(base_url, token)
    while after.get("busy_store_ids") and time.perf_counter() - settled < 20:
        time.sleep(0.2)
        after = _active(base_url, token)
    settle_ms = (time.perf_counter() - settled) * 1000

    second = requests.post(f"{base_url}/api/broadcast/emergency-stop",
                           headers={"Authorization": f"Bearer {token}"}, timeout=60)
    return {
        "sessions_started": len(live),
        "stores_covered": len(store_ids),
        "busy_before": len(before.get("busy_store_ids") or []),
        "api_status": response.status_code,
        "api_ms": round(api_ms, 1),
        "stopped_session_ids": (response.json() or {}).get("session_ids"),
        "settle_ms": round(settle_ms, 1),
        "busy_after": after.get("busy_store_ids"),
        "sessions_after": after.get("sessions"),
        "second_call_status": second.status_code,
        "second_call_session_ids": (second.json() or {}).get("session_ids"),
    }


def restart_recovery(paths, port, base_url, token, store_ids, backend) -> dict:
    """Live sessions, then a real process restart, then reconciliation."""
    live = []
    for index in range(3):
        group = store_ids[index * 2:(index + 1) * 2]
        session_id, response = _create_and_start(
            base_url, token, f"Restart {index}", group)
        if response.status_code == 200:
            live.append(session_id)
    before = _active(base_url, token)

    backend.terminate()
    try:
        backend.wait(timeout=15)
    except subprocess.TimeoutExpired:
        backend.kill()

    restarted = _start_backend(paths, port)
    time.sleep(1.0)
    token2 = _login(base_url)
    after = _active(base_url, token2)

    statuses = {}
    for session_id in live:
        detail = requests.get(f"{base_url}/api/broadcast/sessions/{session_id}",
                              headers={"Authorization": f"Bearer {token2}"}, timeout=15)
        statuses[session_id] = detail.json().get("status") if detail.status_code == 200 else detail.status_code

    reused_id, reused = _create_and_start(base_url, token2, "After restart",
                                          store_ids[0:2])
    if reused.status_code == 200:
        requests.post(f"{base_url}/api/broadcast/sessions/{reused_id}/stop",
                      headers={"Authorization": f"Bearer {token2}"}, timeout=30)
    return {
        "sessions_live_before_restart": len(live),
        "busy_before_restart": len(before.get("busy_store_ids") or []),
        "busy_after_restart": after.get("busy_store_ids"),
        "session_statuses_after_restart": statuses,
        "store_reusable_after_restart": reused.status_code == 200,
    }, restarted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="concurrent_broadcast_scenarios")
    parser.add_argument("--pilot-root", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--churn-cycles", type=int, default=20)
    arguments = parser.parse_args(argv)

    paths = resolve_pilot_paths(arguments.pilot_root)
    prepare(paths)
    store_ids = [row[0] for row in _store_credentials(paths.database_path, 24)]

    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    backend = _start_backend(paths, port)
    results = {}
    try:
        token = _login(base_url)
        results["churn"] = churn(base_url, token, store_ids,
                                 cycles=arguments.churn_cycles)
        results["collision"] = collision(base_url, token, store_ids[10],
                                         store_ids[11:15])
        results["emergency_stop"] = emergency_stop_at_scale(
            base_url, token, store_ids[:18], sessions=6)
        recovery, backend = restart_recovery(paths, port, base_url, token,
                                             store_ids[:6], backend)
        results["restart_recovery"] = recovery
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()

    for name, value in results.items():
        print(f"\n=== {name} ===")
        for key, item in value.items():
            print(f"  {key}: {item}")

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(results, indent=2, sort_keys=True,
                                               default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
