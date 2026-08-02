"""Concurrent broadcasts and Stop ownership, through the real HTTP routes.

The runtime tests next door prove the per-session machinery in isolation.
These prove the routes actually use it - that two operators can be on air at
once, that the Store lease still refuses an overlap, and above all that one
operator cannot stop another's broadcast.

THE STOP DEFECT THIS CLOSES

/broadcast/sessions/{id}/stop checked only that the caller held
STOP_BROADCAST. With one global broadcast that was harmless: there was one
thing to stop and it was almost always yours. With concurrent sessions the
identical code is a cross-user kill - any holder of the permission could
silence another operator mid-announcement by passing their session id, which
is a small guessable integer.

Ownership is now required for EVERY role, including OWNER, and stopping
somebody else's broadcast is a separate, audited, differently-permissioned act.
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for name in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api",
            "store_scope", "ws_manager", "broadcast_runtime",
            "broadcast_reservation")]:
        sys.modules.pop(name, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


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
    return r.json()["id"]


def stores(client, headers):
    return client.get("/api/stores", headers=headers).json()


def start_broadcast(client, headers, name, store_ids):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected",
        "store_ids": list(store_ids)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    return session_id, client.post(
        f"/api/broadcast/sessions/{session_id}/start", headers=headers)


# ===========================================================================
# Several broadcasts at once
# ===========================================================================
def test_two_operators_with_different_stores_are_both_live(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    _, first = start_broadcast(client, alice, "Alice", [catalog[0]["id"]])
    _, second = start_broadcast(client, bob, "Bob", [catalog[1]["id"]])

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text


def test_three_operators_can_be_live_together(client):
    owner = sign_in(client)
    for name in ("alice", "bob", "carol"):
        make_user(client, owner, name)
    catalog = stores(client, owner)

    codes = []
    for index, name in enumerate(("alice", "bob", "carol")):
        _, response = start_broadcast(client, sign_in(client, name),
                                      name.title(), [catalog[index]["id"]])
        codes.append(response.status_code)

    assert codes == [200, 200, 200]


def test_the_same_store_is_still_refused(client):
    """The lease from the previous checkpoint still governs, now that the old
    one-broadcast-at-a-time gate is gone."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = stores(client, owner)
    shared = catalog[0]["id"]

    _, first = start_broadcast(client, sign_in(client, "alice"), "Alice", [shared])
    _, second = start_broadcast(client, sign_in(client, "bob"), "Bob", [shared])

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "STORE_BUSY"


def test_the_conflict_still_names_no_owner(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = stores(client, owner)
    shared = catalog[0]["id"]

    start_broadcast(client, sign_in(client, "alice"), "Alice Private Campaign",
                    [shared])
    _, refused = start_broadcast(client, sign_in(client, "bob"), "Bob", [shared])

    body = str(refused.json()).lower()
    for leak in ("alice", "private campaign", "started_by", "owner_user_id"):
        assert leak not in body, f"{leak!r} leaked: {body}"


# ===========================================================================
# Stop is your own session only
# ===========================================================================
def test_an_operator_can_stop_their_own_broadcast(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    alice = sign_in(client, "alice")
    catalog = stores(client, owner)

    session_id, started = start_broadcast(client, alice, "Alice",
                                          [catalog[0]["id"]])
    assert started.status_code == 200, started.text

    stopped = client.post(f"/api/broadcast/sessions/{session_id}/stop",
                          headers=alice)
    assert stopped.status_code == 200, stopped.text


def test_one_operator_cannot_stop_anothers_broadcast(client):
    """The cross-user kill. Bob holds STOP_BROADCAST and knows the id."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    session_id, started = start_broadcast(client, alice, "Alice",
                                          [catalog[0]["id"]])
    assert started.status_code == 200

    refused = client.post(f"/api/broadcast/sessions/{session_id}/stop",
                          headers=bob)
    assert refused.status_code == 404, refused.text

    # And it really is still live, not merely reported as refused.
    still = client.get("/api/broadcast/current", headers=alice).json()
    assert still["live"] is True


def test_even_an_owner_cannot_normal_stop_someone_elses_broadcast(client):
    """OWNER is unrestricted in what it may administer, but ordinary Stop is
    an own-session action for every role. Stopping another operator is the
    deliberate, audited Emergency Stop path."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    alice = sign_in(client, "alice")
    catalog = stores(client, owner)

    session_id, started = start_broadcast(client, alice, "Alice",
                                          [catalog[0]["id"]])
    assert started.status_code == 200

    refused = client.post(f"/api/broadcast/sessions/{session_id}/stop",
                          headers=owner)
    assert refused.status_code == 404, refused.text


def test_the_refusal_does_not_confirm_the_session_exists(client):
    """404 for somebody else's session and 404 for a session that was never
    created. A 403 on the first would turn this route into an oracle for
    enumerating other people's broadcasts."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    session_id, _ = start_broadcast(client, alice, "Alice", [catalog[0]["id"]])
    someone_elses = client.post(f"/api/broadcast/sessions/{session_id}/stop",
                                headers=bob)
    never_existed = client.post("/api/broadcast/sessions/987654/stop",
                                headers=bob)

    assert someone_elses.status_code == never_existed.status_code == 404
    assert someone_elses.json() == never_existed.json()


def test_stopping_one_broadcast_leaves_the_other_live(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    alice_session, _ = start_broadcast(client, alice, "Alice", [catalog[0]["id"]])
    bob_session, _ = start_broadcast(client, bob, "Bob", [catalog[1]["id"]])

    client.post(f"/api/broadcast/sessions/{alice_session}/stop", headers=alice)

    assert client.get("/api/broadcast/current", headers=alice).json()["live"] is False
    bobs = client.get("/api/broadcast/current", headers=bob).json()
    assert bobs["live"] is True
    assert bobs["session"]["id"] == bob_session


def test_stopping_one_broadcast_releases_only_its_own_leases(client):
    from broadcast_reservation import active_busy_store_ids

    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)
    alice_store, bob_store = catalog[0]["id"], catalog[1]["id"]

    alice_session, _ = start_broadcast(client, alice, "Alice", [alice_store])
    start_broadcast(client, bob, "Bob", [bob_store])

    client.post(f"/api/broadcast/sessions/{alice_session}/stop", headers=alice)

    busy = active_busy_store_ids(client.server_module.engine)
    assert alice_store not in busy
    assert bob_store in busy, "Bob's Store was freed while he was still on air"


# ===========================================================================
# Privacy of the current-broadcast view
# ===========================================================================
def test_current_broadcast_reports_only_your_own(client):
    """Until the ownership-visibility permission lands, this endpoint must not
    hand every viewer another operator's campaign name and target list."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    start_broadcast(client, alice, "Alice Private Campaign", [catalog[0]["id"]])

    seen_by_bob = client.get("/api/broadcast/current", headers=bob).json()
    assert seen_by_bob["live"] is False
    assert "alice" not in str(seen_by_bob).lower()


# ===========================================================================
# History
# ===========================================================================
def test_each_session_records_its_own_history(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)

    alice_session, _ = start_broadcast(client, alice, "Alice", [catalog[0]["id"]])
    bob_session, _ = start_broadcast(client, bob, "Bob", [catalog[1]["id"]])
    client.post(f"/api/broadcast/sessions/{alice_session}/stop", headers=alice)

    history = {row["id"]: row for row in
               client.get("/api/broadcast/history", headers=owner).json()}
    assert history[alice_session]["status"] == "ended"
    assert history[bob_session]["status"] == "live"
