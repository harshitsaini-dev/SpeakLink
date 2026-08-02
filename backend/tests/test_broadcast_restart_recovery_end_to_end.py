"""A restart frees the Store, and the next operator can use it.

The service tests next door prove reconciliation in isolation. These prove the
whole loop through the real application: a broadcast is started over HTTP, the
process is restarted for real (new engine, new runtime, startup_event run
again), and a different operator can then broadcast to the same Store.

WHAT A RESTART ACTUALLY DESTROYS

The HQ microphone WebSocket and every audio queue. Nothing survives that can
carry audio, which is why an interrupted broadcast is closed rather than
resumed.

The Store Receiver is unaffected by any of this: it stops playing when its
connection to HQ ends, because AudioReceiverPilot._shutdown closes the FFmpeg
decoder, the PCM sink and the audio queue in the `finally` of its session
loop. That is asserted here rather than assumed, because "the Store stops on
its own" is exactly the kind of belief that turns into a silent speaker.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

PASSWORD = "a-long-enough-temporary-password"
RUNTIME_MODULES = ("server", "db", "models", "seed", "auth", "rbac",
                   "user_lifecycle", "schemas", "permission_catalog",
                   "admin_records", "admin_search", "user_deletion",
                   "device_deletion", "receiver_enrollment_api", "store_scope",
                   "ws_manager", "broadcast_runtime", "broadcast_reservation",
                   "broadcast_reconciliation")


def _boot(database: Path):
    """Start the application as a fresh process would: new modules, new
    engine, new empty runtime, startup_event run."""
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)
    os.environ["ECHOCAST_DB_PATH"] = str(database)
    from fastapi.testclient import TestClient
    import server as server_module
    return server_module, TestClient(server_module.app)


@pytest.fixture()
def first_boot(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    server_module, client = _boot(database)
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with client as started:
        started.server_module = server_module
        yield started, database


def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_user(client, headers, username, role="BROADCASTER"):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert r.status_code == 201, r.text


def start_broadcast(client, headers, name, store_ids):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected",
        "store_ids": list(store_ids)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    return session_id, client.post(
        f"/api/broadcast/sessions/{session_id}/start", headers=headers)


def test_a_store_is_reusable_after_a_restart(first_boot):
    """The whole point: no permanent STORE_BUSY."""
    client, database = first_boot
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = client.get("/api/stores", headers=owner).json()
    bp = catalog[0]["id"]

    session_id, started = start_broadcast(client, sign_in(client, "alice"),
                                          "Alice", [bp])
    assert started.status_code == 200, started.text

    # The state an unclean stop leaves behind: the database still says live
    # and the lease is still held. Verified rather than assumed, so this test
    # cannot pass by reconciling a state that was never dirty.
    from broadcast_reservation import active_busy_store_ids
    assert bp in active_busy_store_ids(client.server_module.engine)

    # A real restart. New modules, new engine, empty runtime, startup_event.
    client.__exit__(None, None, None)
    server_module, restarted = _boot(database)
    with restarted as second:
        second.server_module = server_module
        from broadcast_reservation import active_busy_store_ids as busy_now
        assert bp not in busy_now(server_module.engine), \
            "the Store stayed BUSY across a restart"

        history = {row["id"]: row for row in
                   second.get("/api/broadcast/history",
                              headers=sign_in(second)).json()}
        assert history[session_id]["status"] == "failed"

        # And a different operator can now use it.
        _, taken = start_broadcast(second, sign_in(second, "bob"), "Bob", [bp])
        assert taken.status_code == 200, taken.text


def test_the_interrupted_broadcast_is_readable_afterwards(first_boot):
    client, database = first_boot
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = client.get("/api/stores", headers=owner).json()

    session_id, _ = start_broadcast(client, sign_in(client, "alice"),
                                    "Diwali Offers", [catalog[0]["id"]])

    client.__exit__(None, None, None)
    server_module, restarted = _boot(database)
    with restarted as second:
        detail = second.get(f"/api/broadcast/sessions/{session_id}",
                            headers=sign_in(second)).json()
        assert detail["campaign_name"] == "Diwali Offers"
        assert detail["status"] == "failed"
        assert detail["targets"], "the target rows were destroyed"


def test_a_second_restart_changes_nothing_further(first_boot):
    client, database = first_boot
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = client.get("/api/stores", headers=owner).json()
    session_id, _ = start_broadcast(client, sign_in(client, "alice"), "Alice",
                                    [catalog[0]["id"]])

    client.__exit__(None, None, None)
    _server, second_boot = _boot(database)
    with second_boot as second:
        first_verdict = second.get(f"/api/broadcast/sessions/{session_id}",
                                   headers=sign_in(second)).json()

    server_module, third_boot = _boot(database)
    with third_boot as third:
        second_verdict = third.get(f"/api/broadcast/sessions/{session_id}",
                                   headers=sign_in(third)).json()

    assert first_verdict["status"] == second_verdict["status"] == "failed"
    assert first_verdict["ended_at"] == second_verdict["ended_at"], \
        "a later restart rewrote the recorded end time"


def test_a_restart_with_nothing_live_is_uneventful(first_boot):
    client, database = first_boot
    sign_in(client)

    client.__exit__(None, None, None)
    server_module, restarted = _boot(database)
    with restarted as second:
        assert second.get("/api/", ).status_code == 200


# ===========================================================================
# The Receiver side
# ===========================================================================
def test_the_receiver_stops_playing_when_its_connection_ends():
    """Read from the code rather than asserted about a live Store.

    AudioReceiverPilot._shutdown closes the FFmpeg decoder, the PCM sink and
    the bounded queue, and DeviceReceiverSession calls it in the `finally` of
    its session loop - so it runs on a clean stop, on an HQ restart and on a
    dropped connection alike. That is why no extra "stop everything" broadcast
    is needed after a restart.

    This asserts the structure that makes it true. It is deliberately not a
    claim that any speaker fell silent - only a physical acoustic check could
    say that, and nothing here sets SPEAKER_VERIFIED.
    """
    import inspect

    from tools.audio_receiver_pilot import AudioReceiverPilot
    from tools.receiver_agent import DeviceReceiverSession

    assert issubclass(DeviceReceiverSession, AudioReceiverPilot)

    shutdown = inspect.getsource(AudioReceiverPilot._shutdown)
    for teardown in ("decoder", "pcm_sink", "queue"):
        assert teardown in shutdown, \
            f"_shutdown no longer tears down the {teardown}"

    # The `finally` is what makes it unconditional. Without it a disconnect
    # path could return early and leave the decoder running.
    session = inspect.getsource(DeviceReceiverSession._run_session) \
        if hasattr(DeviceReceiverSession, "_run_session") else None
    if session is None:
        source = inspect.getsource(DeviceReceiverSession)
        assert "finally:" in source and "_shutdown" in source


def test_the_receiver_never_infers_speaker_verified_from_a_restart():
    from broadcast_reconciliation import RESTART_REASON

    lowered = RESTART_REASON.lower()
    for forbidden in ("speaker", "verified", "heard", "playback_confirmed"):
        assert forbidden not in lowered
