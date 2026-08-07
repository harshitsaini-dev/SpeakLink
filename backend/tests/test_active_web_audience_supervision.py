"""Who may see a Broadcast's room credentials, and who may reach into its audience.

Two separate questions, deliberately answered by two separate permissions.

``broadcast.view_ownership`` governs SEEING: the public code is a credential -
anyone holding it can attempt to join, and with Auto Approve on they are in - so
it travels with the broadcaster's identity rather than with the page.

``broadcast.manage_web_audience`` governs DOING: approving, denying, removing.
That is an intervention the owning operator cannot see happening, which is
precisely why reading the page must not confer it.

Everything here goes through the HTTP surface, because redaction that exists
only in React is not redaction.
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
MANAGE = "broadcast.manage_web_audience"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("ECHOCAST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("ECHOCAST_LAN_HTTP_LISTENERS", "1")

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "web_rooms", "web_participant_runtime",
                               "active_broadcast_management")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username: str = "founder"):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client)


def make_operator(client, owner, username, codes):
    """A VIEWER with exactly the supervision codes named, and nothing else."""
    created = client.post("/api/users", headers=owner, json={
        "username": username, "display_name": username.title(),
        "role": "VIEWER", "password": PASSWORD})
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    changes = [{"code": code, "effect": "ALLOW"} for code in codes]
    assert client.put(f"/api/users/{user_id}/permissions", headers=owner,
                      json={"changes": changes}).status_code == 200
    return user_id, sign_in(client, username)


def live_link_only(client, headers, campaign="Link only"):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "only_with_link"})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=headers).status_code == 200
    return sid


def room_of(client, headers, sid):
    return client.get(f"/api/broadcast/sessions/{sid}/web-room",
                      headers=headers).json()


def join(client, room, name="Harshit"):
    joined = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                         json={"display_name": name, "password": room["password"]})
    assert joined.status_code == 200, joined.text


def request_access(client, room, name="Aman"):
    asked = client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                        json={"display_name": name})
    assert asked.status_code == 200, asked.text


def active_list(client, headers):
    response = client.get("/api/broadcast/active-management", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# OPERATOR A - the page and nothing else
# ===========================================================================

def test_active_view_alone_receives_no_room_credentials(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    _, headers = make_operator(client, owner, "opa", ["broadcast.active_view"])

    body = active_list(client, headers)
    row = next(r for r in body["items"] if r["session_id"] == sid)

    # Absent entirely, not present-and-null: an absent key cannot be un-hidden
    # by a frontend, and a null one invites somebody to try.
    assert "web_room" not in row
    assert "owner_user_id" not in row
    # And nothing leaks through the raw JSON either.
    assert room["public_code"] not in response_text(client, headers)
    assert room["password"] not in response_text(client, headers)


def response_text(client, headers):
    return client.get("/api/broadcast/active-management", headers=headers).text


def test_active_view_alone_cannot_open_the_audience_panel(client, owner):
    sid = live_link_only(client, owner)
    _, headers = make_operator(client, owner, "opa", ["broadcast.active_view"])
    response = client.get(
        f"/api/broadcast/active-management/{sid}/web-audience", headers=headers)
    assert response.status_code == 403


# ===========================================================================
# OPERATOR B - view_ownership: sees, does not touch
# ===========================================================================

def test_view_ownership_receives_the_compact_room_summary(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    _, headers = make_operator(client, owner, "opb",
                               ["broadcast.active_view", "broadcast.view_ownership"])

    row = next(r for r in active_list(client, headers)["items"]
               if r["session_id"] == sid)
    assert "web_room" in row
    summary = row["web_room"]
    assert summary["public_code"] == room["public_code"]
    assert summary["auto_approve"] is False
    # Compact: counts, not a participant list.
    assert set(summary) == {"public_code", "status", "auto_approve", "password",
                            "password_available", "waiting_count",
                            "connected_count", "listening_count"}
    # No hash, ever.
    assert "password_hash" not in row["web_room"]


def test_the_password_hash_is_never_serialized(client, owner):
    sid = live_link_only(client, owner)
    _, headers = make_operator(client, owner, "opb",
                               ["broadcast.active_view", "broadcast.view_ownership"])
    body = response_text(client, headers)
    assert "$2b$" not in body and "$2a$" not in body
    assert "password_hash" not in body
    assert "session_token" not in body


def test_view_ownership_does_not_confer_kick_or_approve(client, owner):
    """Seeing who is broadcasting is not permission to eject their audience."""
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room)
    request_access(client, room)
    pid = room_of(client, owner, sid)["listeners"][0]["id"]
    waiting_pid = room_of(client, owner, sid)["waiting"][0]["id"]

    _, headers = make_operator(client, owner, "opb",
                               ["broadcast.active_view", "broadcast.view_ownership"])

    base = f"/api/broadcast/active-management/{sid}/web-audience"
    assert client.get(base, headers=headers).status_code == 403
    assert client.post(f"{base}/{pid}/kick", headers=headers).status_code == 403
    assert client.post(f"{base}/{waiting_pid}/approve", headers=headers).status_code == 403
    assert client.post(f"{base}/{waiting_pid}/deny", headers=headers).status_code == 403


# ===========================================================================
# OPERATOR C - view_targets: Stores yes, credentials no
# ===========================================================================

def test_view_targets_sees_stores_but_no_room_credentials(client, owner):
    store = client.post("/api/stores", headers=owner, json={
        "store_code": "QAB1", "store_name": "QA Bindapur",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    assert store.status_code == 201
    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Physical", "target_mode": "all"})
    sid = created.json()["id"]
    client.post(f"/api/broadcast/sessions/{sid}/start", headers=owner)
    room = room_of(client, owner, sid)

    _, headers = make_operator(client, owner, "opc",
                               ["broadcast.active_view", "broadcast.view_targets"])

    stores = client.get(f"/api/broadcast/active-management/{sid}/stores",
                        headers=headers)
    assert stores.status_code == 200, "view_targets still sees the Stores"

    row = next(r for r in active_list(client, headers)["items"]
               if r["session_id"] == sid)
    assert "web_room" not in row, "Store visibility is not room visibility"
    assert room["public_code"] not in response_text(client, headers)
    assert client.get(f"/api/broadcast/active-management/{sid}/web-audience",
                      headers=headers).status_code == 403


# ===========================================================================
# OPERATOR D - manage_web_audience: touches, still cannot read credentials
# ===========================================================================

def test_manage_permission_opens_the_panel_without_revealing_the_code(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room)

    _, headers = make_operator(client, owner, "opd",
                               ["broadcast.active_view", MANAGE])

    panel = client.get(f"/api/broadcast/active-management/{sid}/web-audience",
                       headers=headers)
    assert panel.status_code == 200, panel.text
    body = panel.json()

    # Managing is allowed...
    assert body["capabilities"]["can_kick"] is True
    assert body["capabilities"]["can_approve"] is True
    # ...reading the credential is not, and rotation never is for a supervisor.
    assert body["capabilities"]["can_view_room_credentials"] is False
    assert body["capabilities"]["can_rotate_password"] is False
    assert "public_code" not in body
    assert "password" not in body
    assert room["public_code"] not in panel.text


def test_a_supervisor_can_approve_deny_and_kick(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room, "Harshit")
    request_access(client, room, "Aman")
    request_access(client, room, "Vikas")

    state = room_of(client, owner, sid)
    listener_pid = state["listeners"][0]["id"]
    approve_pid = state["waiting"][0]["id"]
    deny_pid = state["waiting"][1]["id"]

    _, headers = make_operator(client, owner, "opd",
                               ["broadcast.active_view", MANAGE])
    base = f"/api/broadcast/active-management/{sid}/web-audience"

    assert client.post(f"{base}/{approve_pid}/approve", headers=headers).status_code == 200
    assert client.post(f"{base}/{deny_pid}/deny", headers=headers).status_code == 200
    kicked = client.post(f"{base}/{listener_pid}/kick", headers=headers)
    assert kicked.status_code == 200, kicked.text

    final = room_of(client, owner, sid)
    statuses = {p["id"]: p["admission_status"]
                for p in final["listeners"] + final["waiting"]}
    assert statuses.get(approve_pid) == "APPROVED"
    assert listener_pid not in statuses, "the kicked listener is gone"


def test_a_kicked_listener_session_stops_working(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room)
    pid = room_of(client, owner, sid)["listeners"][0]["id"]

    _, headers = make_operator(client, owner, "opd",
                               ["broadcast.active_view", MANAGE])
    assert client.post(
        f"/api/broadcast/active-management/{sid}/web-audience/{pid}/kick",
        headers=headers).status_code == 200

    # The listener's own cookie is now worthless.
    assert client.get("/api/listen/me").status_code == 401


def test_a_supervisor_cannot_rotate_the_password(client, owner):
    """Rotation replaces a credential the owner has already shared."""
    sid = live_link_only(client, owner)
    _, headers = make_operator(client, owner, "opd",
                               ["broadcast.active_view", MANAGE,
                                "broadcast.view_ownership"])
    # The owner-only route stays owner-only whatever supervision codes are held.
    assert client.post(f"/api/broadcast/sessions/{sid}/web-room/password/rotate",
                       headers=headers).status_code == 403


def test_a_participant_of_another_broadcast_cannot_be_reached(client, owner):
    first = live_link_only(client, owner, "First")
    second = live_link_only(client, owner, "Second")
    other_room = room_of(client, owner, second)
    join(client, other_room, "Bob")
    theirs = room_of(client, owner, second)["listeners"][0]["id"]

    _, headers = make_operator(client, owner, "opd",
                               ["broadcast.active_view", MANAGE])
    for action in ("approve", "deny", "kick"):
        response = client.post(
            f"/api/broadcast/active-management/{first}/web-audience/{theirs}/{action}",
            headers=headers)
        assert response.status_code == 409, f"{action} reached another Broadcast"


# ===========================================================================
# OPERATOR E - everything
# ===========================================================================

def test_a_fully_privileged_supervisor_sees_and_manages(client, owner):
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room)

    _, headers = make_operator(client, owner, "ope",
                               ["broadcast.active_view", "broadcast.view_ownership",
                                "broadcast.view_targets", MANAGE])
    panel = client.get(f"/api/broadcast/active-management/{sid}/web-audience",
                       headers=headers)
    assert panel.status_code == 200
    body = panel.json()
    assert body["public_code"] == room["public_code"]
    assert body["capabilities"]["can_view_room_credentials"] is True
    assert body["capabilities"]["can_kick"] is True
    # Still not theirs to rotate.
    assert body["capabilities"]["can_rotate_password"] is False


# ===========================================================================
# The owner keeps their own Broadcast
# ===========================================================================

def test_the_owner_manages_their_own_room_without_any_supervision_permission(client, owner):
    """No regression: running your own announcement needs no supervision right."""
    created = client.post("/api/users", headers=owner, json={
        "username": "caster", "display_name": "Caster",
        "role": "BROADCASTER", "password": PASSWORD})
    assert created.status_code == 201
    headers = sign_in(client, "caster")

    sid = live_link_only(client, headers, "Their own")
    room = room_of(client, headers, sid)
    assert room["public_code"], "the owner sees their own room"
    join(client, room)
    pid = room_of(client, headers, sid)["listeners"][0]["id"]

    # Their own Console route, unchanged.
    assert client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/kick",
        headers=headers).status_code == 200
    # And rotation IS theirs.
    assert client.post(f"/api/broadcast/sessions/{sid}/web-room/password/rotate",
                       headers=headers).status_code == 200


def test_the_owner_sees_their_own_room_on_the_supervision_page(client, owner):
    """Identifying your own Broadcast to yourself is not a disclosure."""
    created = client.post("/api/users", headers=owner, json={
        "username": "caster2", "display_name": "Caster",
        "role": "BROADCASTER", "password": PASSWORD})
    user_id = created.json()["id"]
    assert client.put(f"/api/users/{user_id}/permissions", headers=owner, json={
        "changes": [{"code": "broadcast.active_view", "effect": "ALLOW"}]
    }).status_code == 200
    headers = sign_in(client, "caster2")

    sid = live_link_only(client, headers, "Their own")
    row = next(r for r in active_list(client, headers)["items"]
               if r["session_id"] == sid)
    assert row["is_mine"] is True
    assert "web_room" in row, "your own room is yours to see"

    panel = client.get(f"/api/broadcast/active-management/{sid}/web-audience",
                       headers=headers)
    assert panel.status_code == 200
    assert panel.json()["capabilities"]["can_rotate_password"] is True


# ===========================================================================
# Only With Link
# ===========================================================================

def test_only_with_link_supervision_needs_no_store_permission(client, owner):
    """A web-only Broadcast has no Stores, so view_targets cannot be required."""
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    join(client, room)

    _, headers = make_operator(client, owner, "oplink",
                               ["broadcast.active_view", MANAGE])
    panel = client.get(f"/api/broadcast/active-management/{sid}/web-audience",
                       headers=headers)
    assert panel.status_code == 200
    assert panel.json()["target_store_count"] == 0
    assert panel.json()["counts"]["admitted"] == 1


def test_the_active_row_of_a_link_only_broadcast_reports_zero_stores(client, owner):
    sid = live_link_only(client, owner)
    _, headers = make_operator(client, owner, "oplink2",
                               ["broadcast.active_view", "broadcast.view_targets"])
    row = next(r for r in active_list(client, headers)["items"]
               if r["session_id"] == sid)
    assert row["target_store_count"] == 0


# ===========================================================================
# Search cannot leak what the row redacted
# ===========================================================================

def test_searching_by_public_code_reveals_nothing(client, owner):
    """The shape of an answer is a disclosure too."""
    sid = live_link_only(client, owner)
    room = room_of(client, owner, sid)
    _, headers = make_operator(client, owner, "opsearch", ["broadcast.active_view"])

    found = client.get("/api/broadcast/active-management",
                       params={"q": room["public_code"]}, headers=headers)
    assert found.status_code == 200
    # The code is not a searchable field, so it must not act as one.
    assert found.json()["total"] == 0
    assert room["public_code"] not in found.text
