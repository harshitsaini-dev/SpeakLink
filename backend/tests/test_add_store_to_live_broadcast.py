"""Adding one Store to a broadcast that is already on air.

Every gate that guards starting a broadcast guards this too, because this IS
starting one - on a single shop, while the rest keep playing. So the shape that
matters most here is not the happy path: it is that a refusal leaves the
running broadcast exactly as it was, and that anything claimed before a failure
is given back.

The Receiver is a fake. These tests prove the gates, the lease, the lifecycle
and the cleanup; whether a real Windows Receiver decodes the live edge is
proved against real FFmpeg in test_store_late_join_ffmpeg.py.
"""

from __future__ import annotations

import asyncio
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for name in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "store_scope", "ws_manager",
            "broadcast_runtime", "broadcast_reservation",
            "broadcast_target_lifecycle", "store_audio_control",
            "active_broadcast_management")]:
        sys.modules.pop(name, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        try:
            yield made
        finally:
            for session_id in list(
                    server_module.manager.broadcasts.active_session_ids()):
                asyncio.run(server_module.manager.broadcasts.end(session_id))


def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_store(client, headers, code, name=None):
    r = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": name or f"Store {code}",
        "city": "DELHI", "region": "NORTH"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


class FakeReceiver:
    """Stands in for a connected Store, and records what it was told."""

    def __init__(self):
        self.sent = []

    def install(self, server, store_ids, *, ready=True):
        """Make these Stores look online, and optionally ready."""
        manager = server.manager
        manager.receivers = {sid: object() for sid in store_ids}
        server.manager.online_store_ids = lambda: set(store_ids)
        server.manager.ready_store_ids = lambda: set(store_ids) if ready else set()

        async def send(store_id, message):
            self.sent.append((store_id, message.get("type")))
            return True
        server.manager.send_to_receiver = send

    def types_for(self, store_id):
        return [kind for sid, kind in self.sent if sid == store_id]


def start_live(client, headers, store_ids, mode="selected"):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "Evening announcement", "target_mode": mode,
        **({"store_ids": store_ids} if mode == "selected" else {})})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    started = client.post(f"/api/broadcast/sessions/{sid}/start", headers=headers)
    assert started.status_code == 200, started.text
    return sid


def feed_audio(server, sid, chunks=14):
    """Put real broadcast audio through the session, as a live one would.

    Needed because a Store cannot join a stream that has no header yet: the
    framer has nothing to hand it, and the endpoint refuses rather than sending
    a decoder the middle of a Cluster. A test that adds a Store to a silent
    session is testing that refusal, not the join.
    """
    import json as _json
    fixtures = BACKEND_ROOT / "tests" / "fixtures"
    sizes = _json.loads((fixtures / "mediarecorder-live.chunks.json").read_text())["chunkSizes"]
    data = (fixtures / "mediarecorder-live.webm").read_bytes()
    offset = 0
    async def pump():
        nonlocal offset
        for size in sizes[:chunks]:
            await server.manager.fanout_audio(sid, data[offset:offset + size])
            offset += size
            await asyncio.sleep(0)
    asyncio.run(pump())


def add(client, headers, sid, store_id):
    return client.post(f"/api/broadcast/sessions/{sid}/targets",
                       headers=headers, json={"store_id": store_id})


def targets(client, server, sid):
    from models import BroadcastTarget
    from db import SessionLocal
    with SessionLocal() as db:
        return {t.store_id: t for t in db.query(BroadcastTarget).filter(
            BroadcastTarget.session_id == sid).all()}


def leases(server, sid):
    from sqlalchemy import text
    with server.engine.connect() as connection:
        return sorted(r[0] for r in connection.execute(text(
            "SELECT store_id FROM broadcast_store_leases "
            "WHERE session_id = :s AND released_at IS NULL"), {"s": sid}))


# ===========================================================================
# The happy path
# ===========================================================================

def test_an_online_store_joins_a_running_broadcast(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])

    sid = start_live(client, headers, [a])
    feed_audio(server, sid)
    assert leases(server, sid) == [a]

    answer = add(client, headers, sid, b)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["lifecycle_state"] == "ACTIVE"
    assert body["generation"] == 1

    assert leases(server, sid) == sorted([a, b])
    rows = targets(client, server, sid)
    assert rows[b].lifecycle_state == "ACTIVE"
    # ACTIVE is delivery, not audibility - play_status stays what the Receiver
    # has actually said, which so far is nothing.
    assert rows[b].play_status == "pending"
    assert "prepare" in receiver.types_for(b)


def test_adding_the_same_store_twice_is_not_two_leases(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    assert add(client, headers, sid, b).status_code == 200
    second = add(client, headers, sid, b)

    assert second.status_code == 200
    assert second.json()["already_participating"] is True
    assert leases(server, sid) == sorted([a, b]), "a double click took a second lease"


# ===========================================================================
# Refusals - and what they must NOT disturb
# ===========================================================================

def test_a_store_another_broadcast_holds_is_refused_and_that_one_keeps_playing(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    c = make_store(client, headers, "CCC")
    FakeReceiver().install(server, [a, b, c])

    first = start_live(client, headers, [a])
    second = start_live(client, headers, [b])

    refused = add(client, headers, second, a)

    assert refused.status_code == 409
    assert "another broadcast" in refused.json()["detail"].lower()
    assert leases(server, first) == [a], (
        "the broadcast legitimately holding that Store was disturbed")
    assert leases(server, second) == [b]


def test_an_offline_store_is_refused_before_anything_is_claimed(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a])          # b is NOT online
    sid = start_live(client, headers, [a])

    refused = add(client, headers, sid, b)

    assert refused.status_code == 409
    assert "no Receiver connected" in refused.json()["detail"]
    assert leases(server, sid) == [a], "an offline Store took a lease"
    assert b not in targets(client, server, sid)


def test_only_with_link_refuses_a_physical_store(client):
    """Zero physical Stores means zero, and the backend is where that is said."""
    server = client.server_module
    headers = sign_in(client)
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [b])
    sid = start_live(client, headers, [], mode="only_with_link")

    refused = add(client, headers, sid, b)

    assert refused.status_code == 409
    assert "Only With Link" in refused.json()["detail"]
    assert leases(server, sid) == []


def test_a_finished_broadcast_cannot_be_added_to(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a])
    assert client.post(f"/api/broadcast/sessions/{sid}/stop",
                       headers=headers).status_code == 200

    refused = add(client, headers, sid, b)
    assert refused.status_code in (404, 409)
    assert leases(server, sid) == []


def test_an_unknown_store_is_a_404(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])

    assert add(client, headers, sid, 999_999).status_code == 404


# ===========================================================================
# Failure after something has been claimed
# ===========================================================================

def test_a_store_that_never_reports_ready_gives_its_lease_back(client):
    """The important one. A slow Store must not hold itself hostage."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b], ready=False)     # connected, never READY
    server.ADD_STORE_READY_TIMEOUT_SECONDS = 0.3      # keep the test honest and quick
    sid = start_live(client, headers, [a])

    refused = add(client, headers, sid, b)

    assert refused.status_code == 409
    assert "did not report ready" in refused.json()["detail"]
    assert leases(server, sid) == [a], (
        "a Store that never became ready kept its lease - nobody else can "
        "ever use it until this broadcast ends")
    assert targets(client, server, sid)[b].lifecycle_state == "FAILED"
    # prepare may already have taken over that shop's Windows output, so it is
    # told to stop rather than left holding it.
    assert "stop" in receiver.types_for(b)


def test_a_failed_add_can_be_retried_as_a_fresh_generation(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b], ready=False)
    server.ADD_STORE_READY_TIMEOUT_SECONDS = 0.3
    sid = start_live(client, headers, [a])
    assert add(client, headers, sid, b).status_code == 409

    # The Receiver comes good.
    receiver.install(server, [a, b], ready=True)
    server.ADD_STORE_READY_TIMEOUT_SECONDS = 5.0
    feed_audio(server, sid)
    second = add(client, headers, sid, b)

    assert second.status_code == 200, second.text
    assert second.json()["generation"] == 2, (
        "a retry reused the failed generation, so a late acknowledgement from "
        "the first attempt could not be told apart from this one")
    assert targets(client, server, sid)[b].lifecycle_state == "ACTIVE"


# ===========================================================================
# Permissions and scope
# ===========================================================================

def test_a_store_outside_scope_answers_exactly_like_one_that_does_not_exist(client):
    """Scope, on its OWN, with no other gate able to answer first.

    The scoped operator runs their own broadcast here. Adding somebody else's
    broadcast would be refused by the authority gate before scope was ever
    consulted, and the test would pass while proving nothing about scope.
    """
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])

    made = client.post("/api/users", headers=headers, json={
        "username": "scoped", "display_name": "Scoped", "role": "BROADCASTER",
        "password": PASSWORD})
    assert made.status_code == 201, made.text
    user_id = made.json()["id"]
    # Scoped to A only. B exists, is online, and is none of their business.
    scoped_ok = client.put(f"/api/users/{user_id}/store-scope", headers=headers,
                           json={"entries": [{"scope_type": "STORE", "store_id": a}]})
    assert scoped_ok.status_code in (200, 204), scoped_ok.text

    theirs = sign_in(client, "scoped")
    sid = start_live(client, theirs, [a])
    feed_audio(server, sid)

    answer = add(client, theirs, sid, b)

    assert answer.status_code == 404, (
        f"an out-of-scope Store answered {answer.status_code}, not the 404 a "
        "missing Store gets - which tells the caller it exists")
    assert leases(server, sid) == [a]
