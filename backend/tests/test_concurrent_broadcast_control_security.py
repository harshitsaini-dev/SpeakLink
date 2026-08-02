"""Who may stop everyone's broadcasts, and who may see whose.

TWO CAPABILITIES THAT USED TO BE ONE

BROADCASTER inherited ``broadcast.emergency_stop`` through the shared
_BROADCAST_CODES group. With one global broadcast that was nearly harmless -
the only thing to stop was the one broadcast, usually your own. With
concurrent sessions the identical permission means "terminate every other
operator's broadcast estate-wide", which is not a capability that should
arrive by inheritance.

VISIBILITY IS ALSO A CAPABILITY

Knowing that BP is busy is operational information a Broadcaster needs - they
have to pick different Stores. Knowing WHO is using it, for WHICH campaign, is
not. An operator who can enumerate other people's live campaigns by reading a
dashboard has been handed a directory nobody meant to publish, so ownership
detail sits behind its own permission rather than behind a React conditional.

WHAT IS DELIBERATELY NOT ASSERTED

Nothing here claims a speaker fell silent. Emergency Stop sends STOP and
releases leases; whether audio actually stopped in a Store is acoustic
evidence this system does not have.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

PASSWORD = "a-long-enough-temporary-password"
OWNERSHIP_CODE = "broadcast.view_ownership"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for name in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api",
            "store_scope", "ws_manager", "broadcast_runtime",
            "broadcast_reservation", "broadcast_reconciliation")]:
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
            # The runtime registry lives on the module-level manager and
            # therefore outlives this test's database. A broadcast left live
            # here makes its Store a live target for every later test in the
            # same worker, and archiving or deleting such a Store is - quite
            # correctly - refused. That surfaced as an unrelated Store
            # lifecycle test failing several files later.
            import asyncio

            for session_id in server_module.manager.broadcasts.active_session_ids():
                asyncio.run(server_module.manager.broadcasts.end(session_id))


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
    started = client.post(f"/api/broadcast/sessions/{session_id}/start",
                          headers=headers)
    assert started.status_code == 200, started.text
    return session_id


def set_override(client, owner, user_id, code, effect):
    r = client.put(f"/api/users/{user_id}/permissions", headers=owner,
                   json={"changes": [{"code": code, "effect": effect}]})
    assert r.status_code in (200, 204), r.text


# ===========================================================================
# The default matrix
# ===========================================================================
def test_broadcaster_no_longer_holds_emergency_stop_by_default():
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS
    from rbac import Role

    assert "broadcast.emergency_stop" not in \
        DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]


def test_broadcaster_keeps_start_and_stop():
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS
    from rbac import Role

    broadcaster = DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    assert "broadcast.start" in broadcaster
    assert "broadcast.stop" in broadcaster


def test_admin_and_owner_hold_emergency_stop():
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS
    from rbac import Role

    for role in (Role.OWNER, Role.ADMIN):
        assert "broadcast.emergency_stop" in DEFAULT_ROLE_PERMISSIONS[role]


def test_the_coarse_rbac_matrix_agrees_about_emergency_stop():
    """Two matrices that disagree is one of them being wrong somewhere nobody
    looks. The broadcaster WebSocket handshake uses the coarse one."""
    from rbac import Permission, ROLE_PERMISSIONS, Role

    assert Permission.EMERGENCY_STOP not in ROLE_PERMISSIONS[Role.BROADCASTER]
    assert Permission.START_BROADCAST in ROLE_PERMISSIONS[Role.BROADCASTER]
    assert Permission.STOP_BROADCAST in ROLE_PERMISSIONS[Role.BROADCASTER]
    for role in (Role.OWNER, Role.ADMIN):
        assert Permission.EMERGENCY_STOP in ROLE_PERMISSIONS[role]


def test_the_ownership_view_capability_exists_with_the_right_defaults():
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS, PERMISSION_CODES
    from rbac import Role

    assert OWNERSHIP_CODE in PERMISSION_CODES
    assert OWNERSHIP_CODE in DEFAULT_ROLE_PERMISSIONS[Role.OWNER]
    assert OWNERSHIP_CODE in DEFAULT_ROLE_PERMISSIONS[Role.ADMIN]
    assert OWNERSHIP_CODE not in DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    assert OWNERSHIP_CODE not in DEFAULT_ROLE_PERMISSIONS[Role.VIEWER]


def test_ownership_view_grants_nothing_else():
    """Separate capabilities stay separate: seeing who owns a broadcast is not
    permission to stop it, nor to manage anything."""
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS, PERMISSIONS_BY_CODE
    from rbac import Role

    assert OWNERSHIP_CODE in PERMISSIONS_BY_CODE
    broadcaster = DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    for unrelated in ("broadcast.emergency_stop", "users.update",
                      "stores.update", "users.permissions.manage"):
        assert unrelated not in broadcaster or unrelated == "broadcast.start"


def test_the_catalog_reseed_is_idempotent(client):
    """Adding a permission must upgrade an existing database, not duplicate
    rows in it."""
    from sqlalchemy import text
    from permission_catalog import ensure_permission_schema

    engine = client.server_module.engine
    ensure_permission_schema(engine)
    ensure_permission_schema(engine)

    with engine.begin() as connection:
        permissions = connection.execute(text(
            "SELECT COUNT(*) FROM permissions WHERE code = :c"),
            {"c": OWNERSHIP_CODE}).scalar_one()
        role_rows = connection.execute(text(
            "SELECT COUNT(*) FROM role_permissions "
            "WHERE permission_code = :c AND role = 'ADMIN'"),
            {"c": OWNERSHIP_CODE}).scalar_one()
    assert permissions == 1
    assert role_rows == 1


# ===========================================================================
# Emergency Stop enforcement
# ===========================================================================
def test_a_broadcaster_cannot_call_emergency_stop(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    refused = client.post("/api/broadcast/emergency-stop",
                          headers=sign_in(client, "alice"))
    assert refused.status_code == 403, refused.text


def test_an_admin_can_call_emergency_stop(client):
    owner = sign_in(client)
    make_user(client, owner, "adminuser", role="ADMIN")
    allowed = client.post("/api/broadcast/emergency-stop",
                          headers=sign_in(client, "adminuser"))
    assert allowed.status_code == 200, allowed.text


def test_an_owner_can_call_emergency_stop(client):
    owner = sign_in(client)
    assert client.post("/api/broadcast/emergency-stop",
                       headers=owner).status_code == 200


def test_an_explicit_grant_lets_a_broadcaster_emergency_stop(client):
    """The existing override model must keep working for the new default."""
    owner = sign_in(client)
    user_id = make_user(client, owner, "alice")
    set_override(client, owner, user_id, "broadcast.emergency_stop", "ALLOW")

    allowed = client.post("/api/broadcast/emergency-stop",
                          headers=sign_in(client, "alice"))
    assert allowed.status_code == 200, allowed.text


def test_an_explicit_deny_removes_it_from_an_admin(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "adminuser", role="ADMIN")
    set_override(client, owner, user_id, "broadcast.emergency_stop", "DENY")

    refused = client.post("/api/broadcast/emergency-stop",
                          headers=sign_in(client, "adminuser"))
    assert refused.status_code == 403, refused.text


# ===========================================================================
# Emergency Stop behaviour
# ===========================================================================
def test_emergency_stop_ends_every_live_session(client):
    from broadcast_reservation import active_busy_store_ids

    owner = sign_in(client)
    for name in ("alice", "bob", "carol"):
        make_user(client, owner, name)
    catalog = stores(client, owner)
    ids = []
    for index, name in enumerate(("alice", "bob", "carol")):
        ids.append(start_broadcast(client, sign_in(client, name),
                                   name.title(), [catalog[index]["id"]]))

    stopped = client.post("/api/broadcast/emergency-stop", headers=owner)
    assert stopped.status_code == 200, stopped.text
    assert set(stopped.json()["session_ids"]) == set(ids)

    history = {row["id"]: row for row in
               client.get("/api/broadcast/history", headers=owner).json()}
    for session_id in ids:
        assert history[session_id]["status"] == "emergency_stopped"
        assert history[session_id]["ended_at"] is not None

    assert client.server_module.manager.broadcasts.active_session_ids() == ()
    assert active_busy_store_ids(client.server_module.engine) == frozenset()


def test_emergency_stop_with_two_sessions_releases_both_sets_of_leases(client):
    from broadcast_reservation import active_busy_store_ids

    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice",
                    [catalog[0]["id"], catalog[1]["id"]])
    start_broadcast(client, sign_in(client, "bob"), "Bob", [catalog[2]["id"]])

    client.post("/api/broadcast/emergency-stop", headers=owner)

    assert active_busy_store_ids(client.server_module.engine) == frozenset()


def test_emergency_stop_with_nothing_live_is_a_safe_no_op(client):
    owner = sign_in(client)
    first = client.post("/api/broadcast/emergency-stop", headers=owner)
    assert first.status_code == 200
    assert first.json()["session_ids"] == []


def test_calling_emergency_stop_twice_is_safe(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice",
                    [catalog[0]["id"]])

    first = client.post("/api/broadcast/emergency-stop", headers=owner)
    second = client.post("/api/broadcast/emergency-stop", headers=owner)

    assert first.status_code == second.status_code == 200
    assert len(first.json()["session_ids"]) == 1
    assert second.json()["session_ids"] == []


def test_emergency_stop_writes_an_audit_record(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice",
                    [catalog[0]["id"]])

    client.post("/api/broadcast/emergency-stop", headers=owner)

    logs = client.get("/api/logs", headers=owner).json()
    rows = logs["items"] if isinstance(logs, dict) else logs
    text_blob = " ".join(str(row.get("message", "")) for row in rows)
    lowered = text_blob.lower()
    assert "founder" in lowered, "the actor was not recorded"
    assert "emergency stop" in lowered, "the action was not recorded"
    for forbidden in ("password", "ticket", "jwt", "hmac", "bearer"):
        assert forbidden not in lowered


def test_normal_stop_is_still_own_session_only(client):
    """The previous checkpoint's guarantee must survive this one."""
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    alice, bob = sign_in(client, "alice"), sign_in(client, "bob")
    catalog = stores(client, owner)
    session_id = start_broadcast(client, alice, "Alice", [catalog[0]["id"]])

    assert client.post(f"/api/broadcast/sessions/{session_id}/stop",
                       headers=bob).status_code == 404
    assert client.post(f"/api/broadcast/sessions/{session_id}/stop",
                       headers=owner).status_code == 404
    assert client.post(f"/api/broadcast/sessions/{session_id}/stop",
                       headers=alice).status_code == 200


# ===========================================================================
# Active broadcast visibility
# ===========================================================================
def test_a_broadcaster_sees_their_own_session(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    alice = sign_in(client, "alice")
    catalog = stores(client, owner)
    session_id = start_broadcast(client, alice, "Alice Campaign",
                                 [catalog[0]["id"]])

    body = client.get("/api/broadcast/active", headers=alice).json()
    mine = body["mine"]
    assert mine is not None
    assert mine["session_id"] == session_id
    assert mine["campaign_name"] == "Alice Campaign"


def test_a_broadcaster_learns_a_store_is_busy_without_learning_whose(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = stores(client, owner)
    busy_store = catalog[0]
    start_broadcast(client, sign_in(client, "alice"), "Alice Secret Campaign",
                    [busy_store["id"]])

    body = client.get("/api/broadcast/active", headers=sign_in(client, "bob")).json()

    assert busy_store["id"] in body["busy_store_ids"]
    assert body["mine"] is None
    assert body["sessions"] == [], "ownership detail was served without permission"

    blob = str(body).lower()
    for leak in ("alice", "secret campaign", "started_by", "owner_user_id",
                 "display_name"):
        assert leak not in blob, f"{leak!r} leaked: {blob}"


def test_a_broadcaster_never_receives_another_session_id(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "bob")
    catalog = stores(client, owner)
    hidden = start_broadcast(client, sign_in(client, "alice"), "Alice",
                             [catalog[0]["id"]])

    body = client.get("/api/broadcast/active", headers=sign_in(client, "bob")).json()
    assert str(hidden) not in str(body.get("sessions"))
    assert body["sessions"] == []


def test_a_privileged_admin_sees_owner_and_campaign(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    make_user(client, owner, "adminuser", role="ADMIN")
    catalog = stores(client, owner)
    session_id = start_broadcast(client, sign_in(client, "alice"),
                                 "Alice Campaign", [catalog[0]["id"]])

    body = client.get("/api/broadcast/active",
                      headers=sign_in(client, "adminuser")).json()
    sessions = {s["session_id"]: s for s in body["sessions"]}
    assert session_id in sessions
    detail = sessions[session_id]
    assert detail["campaign_name"] == "Alice Campaign"
    assert detail["owner_username"] == "alice"
    assert detail["started_at"] is not None


def test_the_owner_also_sees_details(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice", [catalog[0]["id"]])

    body = client.get("/api/broadcast/active", headers=owner).json()
    assert body["sessions"], "OWNER could not see active broadcasts"


def test_removing_the_ownership_permission_removes_the_detail(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    admin_id = make_user(client, owner, "adminuser", role="ADMIN")
    set_override(client, owner, admin_id, OWNERSHIP_CODE, "DENY")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice Campaign",
                    [catalog[0]["id"]])

    body = client.get("/api/broadcast/active",
                      headers=sign_in(client, "adminuser")).json()
    assert body["sessions"] == []
    assert "alice" not in str(body).lower()


def test_no_speaker_verified_is_manufactured(client):
    owner = sign_in(client)
    make_user(client, owner, "alice")
    catalog = stores(client, owner)
    start_broadcast(client, sign_in(client, "alice"), "Alice", [catalog[0]["id"]])

    body = client.get("/api/broadcast/active", headers=owner).json()
    blob = str(body).lower()
    assert "speaker_verified" not in blob
    assert "playback_confirmed" not in blob or "play_status" in blob


# ===========================================================================
# Store Scope
# ===========================================================================
def test_a_scoped_admin_does_not_learn_out_of_scope_targets(client):
    from store_scope import set_user_scope

    owner = sign_in(client)
    make_user(client, owner, "alice")
    admin_id = make_user(client, owner, "adminuser", role="ADMIN")
    catalog = stores(client, owner)
    visible, hidden = catalog[0], catalog[1]
    set_user_scope(client.server_module.engine, user_id=admin_id, actor_id=1,
                   entries=[{"scope_type": "STORE", "store_id": visible["id"]}])

    # One broadcast spanning both an in-scope and an out-of-scope Store.
    start_broadcast(client, sign_in(client, "alice"), "Alice",
                    [visible["id"], hidden["id"]])

    body = client.get("/api/broadcast/active",
                      headers=sign_in(client, "adminuser")).json()
    blob = str(body)
    assert hidden["store_code"] not in blob
    assert str(hidden["id"]) not in str(body["busy_store_ids"])

    session = body["sessions"][0]
    assert session["target_store_ids"] == [visible["id"]]
    # The count must describe what the viewer may see, not the real total -
    # "2 Stores" while showing one is an existence disclosure about the other.
    assert session["target_store_count"] == 1


def test_a_scoped_broadcaster_does_not_learn_out_of_scope_busy_stores(client):
    from store_scope import set_user_scope

    owner = sign_in(client)
    make_user(client, owner, "alice")
    bob_id = make_user(client, owner, "bob")
    catalog = stores(client, owner)
    visible, hidden = catalog[0], catalog[1]
    set_user_scope(client.server_module.engine, user_id=bob_id, actor_id=1,
                   entries=[{"scope_type": "STORE", "store_id": visible["id"]}])

    start_broadcast(client, sign_in(client, "alice"), "Alice", [hidden["id"]])

    body = client.get("/api/broadcast/active", headers=sign_in(client, "bob")).json()
    assert body["busy_store_ids"] == []
