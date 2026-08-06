"""Per-Store output volume under load, against a real backend.

Reuses the existing synthetic-Receiver harness rather than inventing a second
one: the same ``SyntheticReceiver`` (now able to answer ``set_audio_control``),
the same loopback backend bootstrap, the same CPU/RAM and audio-queue sampling
from ``load_test_receivers``. What is new here is the control traffic and what
is measured about it.

WHAT A PASS MEANS

The control path holds up while audio is streaming: commands are coalesced
rather than queued without bound, a deliberately slow Store does not delay any
other, stale acknowledgements are discarded, and the audio fan-out is unharmed
by volume traffic moving underneath it.

WHAT A PASS DOES NOT MEAN

Nothing about loudness. Every Receiver here is synthetic, opens no audio
device, decodes nothing and drives no amplifier. A 40-Store pass is not a
40-Store rollout, and no result here may set SPEAKER_VERIFIED.

The protected live database is never used; the harness runs against a
throwaway pilot database on a free loopback port.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
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
    prepare,
    resolve_pilot_paths,
)
from tools.local_pilot import _pilot_environment  # noqa: E402
from tools.load_test_receivers import (  # noqa: E402
    SyntheticReceiver,
    _process_cost,
    _read_audio_metrics,
    _store_credentials,
    _summarise,
    _summarise_queue_metrics,
    broadcast_started_at,
    prepare_clock,
)

CHUNK_INTERVAL_SECONDS = 0.25

#: The wave pattern applied to every Store group, mirroring what an operator
#: actually does: set a level, then drag it.
GROUP_LEVELS = (30, 60, 90, None)   # None means "mute this group"

#: A rapid drag, sent as fast as the client can issue it. Only the last value
#: may survive; anything else means coalescing or ordering is broken.
DRAG_SEQUENCE = (30, 45, 70)


def _post_audio_control(base_url, token, session_id, store_id, **body):
    import requests

    response = requests.post(
        f"{base_url}/api/broadcast/sessions/{session_id}/audio-control",
        headers={"Authorization": f"Bearer {token}"},
        json={"store_id": store_id, **body},
        timeout=20,
    )
    return response.status_code, (response.json() if response.content else {})


def _read_audio_control(base_url, token, session_id) -> dict:
    """Read the control state without issuing a command.

    Reading with a POST would make the very thing under test - that HQ learns
    the Store's actual level without asking - impossible to tell apart from HQ
    hearing its own command back.
    """
    import requests

    response = requests.get(
        f"{base_url}/api/broadcast/sessions/{session_id}/audio-control",
        headers={"Authorization": f"Bearer {token}"}, timeout=20)
    return response.json() if response.status_code == 200 else {}


def _actual_row(state: dict, store_id: int) -> dict:
    for row in (state or {}).get("stores", []):
        if row.get("store_id") == store_id:
            return row
    return {}


def _recording_summary(base_url, token, session_id) -> dict:
    """What the recording of this load run turned out to be.

    Read from the API rather than the filesystem, because what matters is what
    an operator would be told - and because a status the API cannot report is
    a status that does not exist as far as the product is concerned.
    """
    import requests

    try:
        response = requests.get(
            f"{base_url}/api/broadcast/sessions/{session_id}/recording",
            headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception as failure:
        return {"unavailable": str(failure)[:120]}
    if response.status_code != 200:
        return {"unavailable": f"HTTP {response.status_code}"}
    body = response.json()
    return {
        "status": body.get("status"),
        "codec": body.get("codec"),
        "container": body.get("container"),
        "byte_size": body.get("byte_size"),
        "duration_seconds": body.get("duration_seconds"),
        "chunks_written": body.get("chunks_written"),
        # Non-zero means the disk could not keep up. The announcement still
        # went out - that is the entire point of the bounded queue - and the
        # recording is honestly marked PARTIAL.
        "chunks_dropped": body.get("chunks_dropped"),
        "error": body.get("error"),
    }


def _latest_wins_summary(state: dict, receivers) -> dict:
    """Did the last value of the drag survive, on every Store that can apply it?

    Stores modelling an older Receiver or a failing output are excluded from
    the "applied" expectation on purpose - they are supposed NOT to apply, and
    counting them as failures would make the harness lie about its own fixture.
    """
    rows = {row["store_id"]: row for row in state.get("stores", [])}
    controllable = {r.store_id for r in receivers
                    if r.supports_audio_control and r.audio_control_result == "applied"}
    expected = DRAG_SEQUENCE[-1]
    applied_latest = sum(
        1 for store_id in controllable
        if rows.get(store_id, {}).get("applied_volume_percent") == expected)
    stale_survivors = sum(
        1 for store_id in controllable
        if rows.get(store_id, {}).get("applied_volume_percent") not in (expected, None))
    # The replayed stale acknowledgement claims 1%. If the backend had
    # accepted it, exactly that value would be sitting here.
    replayed_value_accepted = sum(
        1 for row in rows.values() if row.get("applied_volume_percent") == 1)
    return {
        "expected_latest_percent": expected,
        "controllable_stores": len(controllable),
        "stores_at_latest_value": applied_latest,
        "stores_left_at_a_stale_value": stale_survivors,
        "replayed_stale_ack_accepted": replayed_value_accepted,
        "requested_matches_latest": all(
            rows.get(store_id, {}).get("requested_volume_percent") == expected
            for store_id in controllable),
    }


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 2)


async def _drive_audio_control(paths, store_count: int, port: int,
                               backend_pid: int, fixture: Path) -> dict:
    base_url = f"http://127.0.0.1:{port}"
    receiver_url = f"ws://127.0.0.1:{port}/api/ws/receiver"

    token = _api_post(
        base_url, "/api/auth/login", None,
        {"username": os.environ["ADMIN_USERNAME"],
         "password": os.environ["ADMIN_PASSWORD"]},
    )["access_token"]

    credentials = _store_credentials(paths.database_path, store_count)

    # A deliberately mixed estate rather than 40 identical Stores. A harness
    # where every Receiver behaves perfectly proves only the happy path, and
    # the failures below are the ones a real rollout will actually meet.
    receivers = []
    for index, (store_id, code, secret) in enumerate(credentials):
        supports = True
        result = "applied"
        delay = 0.0
        if store_count >= 10 and index == 1:
            supports = False             # an older Receiver build
        elif store_count >= 10 and index == 2:
            result = "failed"            # output device refuses
        elif store_count >= 5 and index == 3:
            delay = 0.75                 # the slow Store
        receivers.append(SyntheticReceiver(
            store_id, code, secret, receiver_url,
            supports_audio_control=supports,
            audio_control_result=result,
            audio_control_delay_seconds=delay,
        ))
    # One Store replays a stale acknowledgement, so the discard rule is proven
    # against the real backend rather than only in a unit test.
    if len(receivers) > 4:
        receivers[4].replay_stale_ack = True
    # And one models the upgrade case: current software, output never
    # re-selected, so master control is legitimately unavailable and HQ must
    # say WHY rather than calling the Receiver unsupported.
    if len(receivers) > 5:
        receivers[5].endpoint_configured = False
    # And one replays a telemetry frame whose state_sequence goes BACKWARDS.
    if len(receivers) > 6:
        receivers[6].replay_stale_state = True

    # The noisy Store: somebody at the till drags the Windows slider hard while
    # the broadcast runs. Store 0 by choice, because it is also the Store the
    # driver reads state back from.
    noisy = receivers[0]

    started = asyncio.Event()
    await asyncio.gather(*(r.connect() for r in receivers))
    tasks = [asyncio.create_task(r.run(started)) for r in receivers]
    # Every Store reports its own endpoint. The pumps run alongside the receive
    # loops, exactly as they do in the real Receiver.
    tasks += [asyncio.create_task(r.telemetry_pump()) for r in receivers]

    state_after_drag = {}
    state_before_stop = {}
    requested = 0
    accepted = 0
    refused = {}
    control_latencies_ms = []
    test_started = time.perf_counter()

    try:
        session = _api_post(
            base_url, "/api/broadcast/sessions", token,
            {"campaign_name": f"Audio control load - {store_count} Stores",
             "target_mode": "selected",
             "store_ids": [r.store_id for r in receivers]},
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

        raw = fixture.read_bytes()
        chunk_size = max(1, len(raw) // 20)
        chunks = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)]

        ticket = _api_post(base_url, "/api/auth/ws-ticket", token,
                           {"audience": "broadcaster"})["ticket"]

        cost_before = _process_cost(backend_pid)
        broadcast_started_at[0] = time.perf_counter()
        started.set()
        queue_metrics = {"unavailable": "never sampled"}

        async with websockets.connect(
            f"ws://127.0.0.1:{port}/api/ws/broadcaster"
            f"?ticket={ticket}&session_id={session_id}",
            open_timeout=20, max_size=4 * 1024 * 1024,
        ) as uplink:
            await uplink.send(json.dumps(
                {"type": "init", "mime": "audio/webm;codecs=opus"}))

            for index, chunk in enumerate(chunks):
                await uplink.send(chunk)

                # ---- Wave 1: set each group to its level, while audio flows.
                if index == 2:
                    for position, receiver in enumerate(receivers):
                        level = GROUP_LEVELS[position % len(GROUP_LEVELS)]
                        body = ({"muted": True} if level is None
                                else {"volume_percent": level})
                        issued = time.perf_counter()
                        status, payload = _post_audio_control(
                            base_url, token, session_id, receiver.store_id, **body)
                        control_latencies_ms.append(
                            (time.perf_counter() - issued) * 1000)
                        requested += 1
                        if status == 200:
                            accepted += 1
                        else:
                            refused[status] = refused.get(status, 0) + 1

                # ---- Wave 2: a rapid drag on every Store. Latest must win.
                if index == 6:
                    for receiver in receivers:
                        for level in DRAG_SEQUENCE:
                            issued = time.perf_counter()
                            status, payload = _post_audio_control(
                                base_url, token, session_id, receiver.store_id,
                                volume_percent=level)
                            control_latencies_ms.append(
                                (time.perf_counter() - issued) * 1000)
                            requested += 1
                            if status == 200:
                                accepted += 1
                            else:
                                refused[status] = refused.get(status, 0) + 1

                # ---- Failure cases, mid-broadcast, on purpose.
                if index == 9:
                    # A Store that is not in this broadcast at all.
                    status, _ = _post_audio_control(
                        base_url, token, session_id, 999_999, volume_percent=50)
                    refused[f"out_of_session_{status}"] = 1
                    # A session that does not exist.
                    status, _ = _post_audio_control(
                        base_url, token, 999_999, receivers[0].store_id,
                        volume_percent=50)
                    refused[f"stale_session_{status}"] = 1
                    # Outside the documented range.
                    status, _ = _post_audio_control(
                        base_url, token, session_id, receivers[0].store_id,
                        volume_percent=150)
                    refused[f"out_of_range_{status}"] = 1

                # ---- Telemetry churn, while audio is flowing.
                if index == 4:
                    # A hard drag: forty local changes inside one chunk
                    # interval. HQ must end up with where it STOPPED, and the
                    # count on the wire must be far smaller than forty.
                    for level in range(30, 70):
                        noisy.local_change(level)
                if index == 7:
                    # The till mutes, then unmutes, at the Windows mixer.
                    noisy.local_change(55, muted=True)
                    await asyncio.sleep(0.4)
                    noisy.local_change(55, muted=False)
                if index == 8:
                    # A Store whose endpoint cannot be controlled, and one that
                    # never re-selected an output, both have a go. Neither may
                    # put anything on the wire.
                    for receiver in receivers[1:6]:
                        receiver.local_change(42)
                    # The slow Store reports too; its lateness must not delay
                    # anybody else's telemetry.
                    if len(receivers) > 3:
                        receivers[3].local_change(37)
                # The stale-telemetry Store needs TWO readings before it can
                # replay an older one, so it changes twice with a pump tick in
                # between. HQ must keep the second and discard the replay.
                if index == 12 and len(receivers) > 6:
                    receivers[6].local_change(64)
                if index == 14 and len(receivers) > 6:
                    receivers[6].local_change(58)

                if index == len(chunks) // 2:
                    queue_metrics = _read_audio_metrics(base_url, token)



                # ---- Unmute everything before the end.
                if index == len(chunks) - 6:
                    for receiver in receivers:
                        status, _ = _post_audio_control(
                            base_url, token, session_id, receiver.store_id,
                            muted=False)
                        requested += 1
                        if status == 200:
                            accepted += 1

                await asyncio.sleep(CHUNK_INTERVAL_SECONDS)

            # Still INSIDE the uplink block, because closing it ends the
            # session. Wait for the deliberately slow Store's acknowledgements
            # to land before reading: measuring earlier counts an in-flight
            # command as a stale value, which is a bug in the harness rather
            # than in the product.
            await asyncio.sleep(3.0)
            # A final local change with NOTHING in flight from HQ, so what is
            # read back below can only have come from telemetry.
            noisy.local_change(23, muted=False)
            await asyncio.sleep(1.5)
            state_before_stop = _read_audio_control(base_url, token, session_id)
            _, state_after_drag = _post_audio_control(
                base_url, token, session_id, receivers[0].store_id, muted=False)

        await asyncio.sleep(1.0)
        cost_after = _process_cost(backend_pid)

        import requests

        stop_status = requests.post(
            f"{base_url}/api/broadcast/sessions/{session_id}/stop",
            headers={"Authorization": f"Bearer {token}"}, timeout=20,
        ).status_code
        await asyncio.sleep(1.0)

        # A command AFTER the broadcast ended must be refused, not applied.
        after_stop_status, _ = _post_audio_control(
            base_url, token, session_id, receivers[0].store_id, volume_percent=10)

        cpu_seconds = None
        if cost_before.get("cpu") is not None and cost_after.get("cpu") is not None:
            cpu_seconds = round(cost_after["cpu"] - cost_before["cpu"], 2)

        ack_latencies = [ms for r in receivers for ms in r.audio_latencies_ms]
        return {
            "stores": store_count,
            "duration_seconds": round(time.perf_counter() - test_started, 1),
            "ready_receivers": ready_count,
            "control_commands_requested": requested,
            "control_commands_accepted": accepted,
            "control_commands_refused": refused,
            # What actually reached a Receiver. Lower than requested is CORRECT
            # for the unsupported Store, which is never sent a command at all.
            "commands_transmitted": sum(r.audio_commands_received for r in receivers),
            "acknowledgements": sum(r.audio_acks_sent for r in receivers),
            "stale_commands_ignored": sum(
                r.audio_stale_commands_ignored for r in receivers),
            "ack_latency_ms": {
                "min": round(min(ack_latencies), 2) if ack_latencies else None,
                "avg": round(statistics.fmean(ack_latencies), 2) if ack_latencies else None,
                "p95": _percentile(ack_latencies, 0.95),
                "max": round(max(ack_latencies), 2) if ack_latencies else None,
            },
            "http_latency_ms": {
                "avg": round(statistics.fmean(control_latencies_ms), 2)
                if control_latencies_ms else None,
                "p95": _percentile(control_latencies_ms, 0.95),
            },
            "audio_chunks_sent": len(chunks),
            "audio_chunks_received": _summarise(
                [float(r.chunks) for r in receivers]),
            "audio_queue": _summarise_queue_metrics(queue_metrics),
            "recording": _recording_summary(base_url, token, session_id),
            "cpu_seconds": cpu_seconds,
            # _process_cost reports raw bytes under "rss"; converted here so
            # the report reads in the units a person thinks in.
            "rss_mb_before": (round(cost_before["rss"] / 1048576, 1)
                              if cost_before.get("rss") else None),
            "rss_mb_after": (round(cost_after["rss"] / 1048576, 1)
                             if cost_after.get("rss") else None),
            "backend_processes": cost_after.get("processes"),
            # Proof that the newest value survived the drag AND the replayed
            # stale acknowledgement, read from the server rather than inferred.
            "latest_wins": _latest_wins_summary(state_after_drag, receivers),
            "stop_status": stop_status,
            "after_stop_status": after_stop_status,
            "receiver_errors": {r.store_code: r.errors for r in receivers if r.errors},
            # ---- Windows endpoint outcomes ---------------------------------
            "endpoints_prepared": sum(1 for r in receivers if r.endpoint_prepared),
            "endpoints_restored": sum(1 for r in receivers if r.endpoint_restored),
            "endpoints_left_changed": [
                r.store_code for r in receivers
                if r.endpoint_prepared and not r.endpoint_restored],
            "endpoints_at_original_state": sum(
                1 for r in receivers
                if r.endpoint_volume == r.endpoint_original_volume
                and r.endpoint_muted == r.endpoint_original_muted),
            "needs_output_selection": [
                r.store_code for r in receivers if not r.endpoint_configured],
            # The proof there is no double attenuation: the PCM gain must not
            # have followed the HQ slider anywhere.
            "pcm_gain_left_at_unity": all(
                r.volume_percent == 100 and r.muted is False for r in receivers),
            # ---- two-way synchronisation -----------------------------------
            # Generated is what happened at the tills; transmitted is what
            # reached HQ. Transmitted being much smaller is the coalescing
            # working, not telemetry being lost.
            "endpoint_states_generated": sum(
                r.endpoint_states_generated for r in receivers),
            "endpoint_states_transmitted": sum(
                r.endpoint_states_transmitted for r in receivers),
            "noisy_store_code": noisy.store_code,
            "noisy_store_generated": noisy.endpoint_states_generated,
            "noisy_store_transmitted": noisy.endpoint_states_transmitted,
            # What HQ believed the noisy Store was doing, read from the server.
            "hq_actual_volume_percent": _actual_row(
                state_before_stop, noisy.store_id).get("actual_volume_percent"),
            "hq_actual_muted": _actual_row(
                state_before_stop, noisy.store_id).get("actual_muted"),
            "hq_matches_store": (
                _actual_row(state_before_stop, noisy.store_id).get(
                    "actual_volume_percent") == 23),
            # Stores with no controllable endpoint must report nothing at all.
            # The replayed OLDER reading claimed 1%. HQ must still be showing
            # the newer 58, read back from the server.
            "stale_telemetry_store_code": (
                receivers[6].store_code if len(receivers) > 6 else None),
            "stale_telemetry_transmitted": (
                receivers[6].endpoint_states_transmitted if len(receivers) > 6
                else None),
            "hq_ignored_stale_telemetry": (
                _actual_row(state_before_stop, receivers[6].store_id).get(
                    "actual_volume_percent") == 58 if len(receivers) > 6 else None),
            "silent_stores_transmitted": sum(
                r.endpoint_states_transmitted for r in receivers
                if not r.endpoint_configured or not r.supports_audio_control),
            # THE restoration property: a live change at the till must not have
            # become the thing that gets put back.
            "restored_to_original_not_live": all(
                r.endpoint_volume == r.endpoint_original_volume
                and r.endpoint_muted == r.endpoint_original_muted
                for r in receivers if r.endpoint_prepared),
            "slow_store_code": receivers[3].store_code if len(receivers) > 3 else None,
            "unsupported_store_code": (
                receivers[1].store_code if len(receivers) > 1
                and not receivers[1].supports_audio_control else None),
            "failing_store_code": (
                receivers[2].store_code if len(receivers) > 2
                and receivers[2].audio_control_result == "failed" else None),
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in receivers),
                             return_exceptions=True)


def run_audio_control_load(store_count: int, pilot_root: Path | None = None) -> dict:
    paths = resolve_pilot_paths(pilot_root)
    prepare(paths)

    fixture = paths.root / "audio" / "fixture.webm"
    if not fixture.exists():
        candidates = sorted((paths.root / "audio").glob("*.webm"))
        if not candidates:
            raise AudioPilotError("no WebM fixture was prepared")
        fixture = candidates[0]

    port = _free_loopback_port()
    # The pilot's OWN environment builder, not a hand-rolled copy: it supplies
    # JWT_SECRET and the CORS origin as well as the database path. Building it
    # by hand omitted JWT_SECRET, and the symptom was a 500 on login rather
    # than anything naming the missing variable.
    environment = _pilot_environment(paths)
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app",
         "--host", "127.0.0.1", "--port", str(port),
         "--workers", "1", "--log-level", "warning"],
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
                time.sleep(0.5)
        else:
            raise AudioPilotError("the pilot backend did not become reachable")

        return asyncio.run(
            _drive_audio_control(paths, store_count, port, backend.pid, fixture))
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


async def _drive_concurrent_isolation(paths, store_count: int, port: int,
                                      backend_pid: int) -> dict:
    """Two disjoint broadcasts, two operators, one estate.

    Alice owns the first half of the Stores and Bob the second. Each changes
    their own Stores; neither may move the other's, and neither may reach into
    the other's session even by naming its id exactly.

    No audio is streamed here on purpose. What is under test is control-plane
    isolation, and a fan-out would only make a failure harder to read.
    """
    base_url = f"http://127.0.0.1:{port}"
    receiver_url = f"ws://127.0.0.1:{port}/api/ws/receiver"

    owner_token = _api_post(
        base_url, "/api/auth/login", None,
        {"username": os.environ["ADMIN_USERNAME"],
         "password": os.environ["ADMIN_PASSWORD"]},
    )["access_token"]

    # Two real BROADCASTER accounts, so ownership is genuinely two identities
    # rather than the same account twice.
    password = "concurrent-isolation-throwaway-password"
    operators = {}
    for name in ("alice", "bob"):
        _api_post(base_url, "/api/users", owner_token,
                  {"username": name, "display_name": name.title(),
                   "role": "BROADCASTER", "password": password})
        operators[name] = _api_post(
            base_url, "/api/auth/login", None,
            {"username": name, "password": password})["access_token"]

    credentials = _store_credentials(paths.database_path, store_count)
    half = store_count // 2
    groups = {"alice": credentials[:half], "bob": credentials[half:]}

    receivers = [SyntheticReceiver(sid, code, tok, receiver_url)
                 for sid, code, tok in credentials]
    started = asyncio.Event()
    await asyncio.gather(*(r.connect() for r in receivers))
    tasks = [asyncio.create_task(r.run(started)) for r in receivers]
    tasks += [asyncio.create_task(r.telemetry_pump()) for r in receivers]
    started.set()

    refusals = 0
    accepted = 0
    try:
        sessions = {}
        for name, rows in groups.items():
            session = _api_post(
                base_url, "/api/broadcast/sessions", operators[name],
                {"campaign_name": f"{name} concurrent isolation",
                 "target_mode": "selected",
                 "store_ids": [sid for sid, _, _ in rows]},
            )
            sessions[name] = session["id"]
            prepare_clock[0] = time.perf_counter()
            _api_post(base_url,
                      f"/api/broadcast/sessions/{session['id']}/start",
                      operators[name])

        await asyncio.sleep(1.5)

        alice_store = groups["alice"][0][0]
        bob_store = groups["bob"][0][0]
        for level in (30, 45, 70):
            _post_audio_control(base_url, operators["alice"], sessions["alice"],
                                alice_store, volume_percent=level)
        _post_audio_control(base_url, operators["bob"], sessions["bob"],
                            bob_store, muted=True)
        await asyncio.sleep(1.5)

        # Each estate's tills move their own Windows sliders. One operator's
        # telemetry must never appear in the other's Console, and it must not
        # look like a command either operator issued.
        by_store = {r.store_id: r for r in receivers}
        by_store[alice_store].local_change(18, muted=False)
        by_store[bob_store].local_change(64, muted=False)
        await asyncio.sleep(1.0)
        alice_live = _read_audio_control(base_url, operators["alice"],
                                         sessions["alice"])
        bob_live = _read_audio_control(base_url, operators["bob"], sessions["bob"])

        alice_status, alice_state = _post_audio_control(
            base_url, operators["alice"], sessions["alice"], alice_store,
            volume_percent=70)
        bob_status, bob_state = _post_audio_control(
            base_url, operators["bob"], sessions["bob"], bob_store, muted=True)

        # Each operator reaches into the other's session, naming its ids exactly.
        cross_status, _ = _post_audio_control(
            base_url, operators["bob"], sessions["alice"], alice_store,
            volume_percent=5)
        accepted += cross_status == 200
        refusals += cross_status != 200
        reverse_status, _ = _post_audio_control(
            base_url, operators["alice"], sessions["bob"], bob_store,
            volume_percent=5)
        accepted += reverse_status == 200
        refusals += reverse_status != 200

        alice_rows = {r["store_id"]: r for r in alice_state.get("stores", [])}
        bob_rows = {r["store_id"]: r for r in bob_state.get("stores", [])}
        return {
            "stores": store_count,
            "alice_session": sessions["alice"],
            "bob_session": sessions["bob"],
            "alice_store_applied": alice_rows.get(alice_store, {}).get(
                "applied_volume_percent"),
            "bob_store_applied_muted": bob_rows.get(bob_store, {}).get("applied_muted"),
            # Every OTHER Store in each session must still sit at the default.
            "alice_others_untouched": all(
                row["requested_volume_percent"] == 100 and not row["requested_muted"]
                for store_id, row in alice_rows.items() if store_id != alice_store),
            "bob_others_untouched": all(
                row["requested_volume_percent"] == 100 and not row["requested_muted"]
                for store_id, row in bob_rows.items() if store_id != bob_store),
            "alice_session_size": len(alice_rows),
            "bob_session_size": len(bob_rows),
            "cross_owner_refused": refusals,
            "cross_owner_accepted": accepted,
            "cross_status_codes": [cross_status, reverse_status],
            "alice_actual_volume_percent": _actual_row(
                alice_live, alice_store).get("actual_volume_percent"),
            "bob_actual_volume_percent": _actual_row(
                bob_live, bob_store).get("actual_volume_percent"),
            # Nobody else in either session may have acquired a reading.
            "alice_others_have_no_actual_state": all(
                row.get("actual_volume_percent") is None
                for store_id, row in
                {r["store_id"]: r for r in alice_live.get("stores", [])}.items()
                if store_id != alice_store),
            "bob_others_have_no_actual_state": all(
                row.get("actual_volume_percent") is None
                for store_id, row in
                {r["store_id"]: r for r in bob_live.get("stores", [])}.items()
                if store_id != bob_store),
            "alice_read_status": alice_status,
            "bob_read_status": bob_status,
        }
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in receivers), return_exceptions=True)


def run_concurrent_isolation(store_count: int = 8,
                             pilot_root: Path | None = None) -> dict:
    return _with_pilot_backend(
        pilot_root,
        lambda paths, port, pid, fixture: _drive_concurrent_isolation(
            paths, store_count, port, pid),
    )


def _with_pilot_backend(pilot_root, driver):
    """Bootstrap a throwaway backend and hand it to one driver coroutine."""
    paths = resolve_pilot_paths(pilot_root)
    prepare(paths)

    fixture = paths.root / "audio" / "fixture.webm"
    if not fixture.exists():
        candidates = sorted((paths.root / "audio").glob("*.webm"))
        if not candidates:
            raise AudioPilotError("no WebM fixture was prepared")
        fixture = candidates[0]

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
                time.sleep(0.5)
        else:
            raise AudioPilotError("the pilot backend did not become reachable")
        return asyncio.run(driver(paths, port, backend.pid, fixture))
    finally:
        backend.terminate()
        try:
            backend.wait(timeout=15)
        except subprocess.TimeoutExpired:
            backend.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores", type=int, action="append", default=None,
                        help="Store count; repeat for several runs")
    parser.add_argument("--report", type=Path, default=None)
    # A fresh root per invocation keeps a previously seeded pilot database
    # from deciding this run's credentials - and keeps the live one untouched.
    parser.add_argument("--pilot-root", type=Path, default=None)
    parser.add_argument("--concurrent", action="store_true",
                        help="run the two-broadcast isolation scenario")
    arguments = parser.parse_args(argv)

    if arguments.concurrent:
        result = run_concurrent_isolation(pilot_root=arguments.pilot_root)
        print(json.dumps(result, indent=2), flush=True)
        if arguments.report:
            arguments.report.parent.mkdir(parents=True, exist_ok=True)
            arguments.report.write_text(json.dumps(result, indent=2),
                                        encoding="utf-8")
        return 0

    counts = arguments.stores or [5, 10, 20, 40]
    results = []
    for count in counts:
        print(f"--- {count} Stores ---", flush=True)
        result = run_audio_control_load(count, arguments.pilot_root)
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)

    if arguments.report:
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"report written: {arguments.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
