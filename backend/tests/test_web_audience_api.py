"""The web audience over the real HTTP surface: joining, approval, kick, isolation.

Everything here goes through the API as a real caller would - a signed-in
broadcaster on the HQ side, and an unauthenticated browser on the public side.
The point is not that the functions work; ``test_web_rooms`` already proves
that. The point is that the ROUTES enforce it, including for the crafted
requests a hidden button does not prevent.
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
    monkeypatch.setenv("ECHOCAST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    # The listener cookie is Secure by default; the TestClient speaks http.
    monkeypatch.setenv("ECHOCAST_LAN_HTTP_LISTENERS", "1")

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "web_rooms",
                               "web_participant_runtime")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username: str, password: str = PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client, "founder")


def make_user(client, headers, username, role, password=PASSWORD):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_link_only_session(client, headers, campaign="Link only"):
    response = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "only_with_link"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def room_of(client, headers, sid):
    response = client.get(f"/api/broadcast/sessions/{sid}/web-room", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# The room comes with the Broadcast
# ===========================================================================

def test_creating_a_broadcast_creates_exactly_one_room_with_a_password(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    assert room["public_code"].startswith("EC-")
    assert str(sid) not in room["public_code"], "the public code is not the session id"
    # The generated password is offered ONCE, on the page that created it.
    assert room["password"], "the broadcaster is given the password to share"
    assert room["auto_approve"] is False, "Auto Approve is OFF by default"
    assert room["counts"] == {"waiting": 0, "admitted": 0, "connected": 0,
                              "listening": 0, "buffering": 0, "paused": 0}


def test_a_physical_broadcast_also_gets_a_room(client, owner):
    """A Broadcast with Stores still has a web room. Sharing it is optional."""
    client.post("/api/stores", headers=owner, json={
        "store_code": "QAB1", "store_name": "QA Bindapur",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Physical", "target_mode": "all"})
    assert created.status_code == 201, created.text
    room = room_of(client, owner, created.json()["id"])
    assert room["public_code"]
    assert room["counts"]["admitted"] == 0, "zero listeners is perfectly valid"


# ===========================================================================
# Public join
# ===========================================================================

def test_the_correct_password_admits_and_sets_an_httponly_cookie(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    joined = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                         json={"display_name": "Harshit",
                               "password": room["password"]})
    assert joined.status_code == 200, joined.text
    body = joined.json()
    assert body["admitted"] is True
    assert body["admission_status"] == "PASSWORD_ADMITTED"
    assert body["display_name"] == "Harshit"

    cookie = joined.headers.get("set-cookie", "")
    assert "echocast_listener=" in cookie
    assert "HttpOnly" in cookie, "page script must never read the listener session"
    assert "SameSite=lax" in cookie.replace("samesite", "SameSite")


def test_a_wrong_password_is_refused_and_creates_nothing(client, owner):
    """A typo must not be quietly turned into 'waiting for approval'."""
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    refused = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                          json={"display_name": "Harshit", "password": "WRONG-PASS"})
    assert refused.status_code == 401
    assert "incorrect" in refused.json()["detail"].lower()

    after = room_of(client, owner, sid)
    assert after["counts"]["waiting"] == 0, "a wrong password is not a request"
    assert after["counts"]["admitted"] == 0


def test_the_submitted_password_is_never_echoed_back(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    attempted = "SOME-SECRET-GUESS"
    refused = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                          json={"display_name": "Harshit", "password": attempted})
    assert attempted not in refused.text


def test_an_unknown_room_and_an_ended_room_answer_alike(client, owner):
    """Telling them apart would let anyone probe for Broadcasts that existed."""
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    unknown = client.get("/api/listen/rooms/EC-ZZZZZZ")
    assert unknown.status_code == 404
    # No internal identifiers leak in the refusal.
    assert str(sid) not in unknown.text


def test_the_public_lookup_reveals_nothing_about_the_estate(client, owner):
    client.post("/api/stores", headers=owner, json={
        "store_code": "QAB1", "store_name": "QA Bindapur",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Secret Campaign Name", "target_mode": "all"})
    sid = created.json()["id"]
    room = room_of(client, owner, sid)

    looked_up = client.get(f"/api/listen/rooms/{room['public_code']}")
    assert looked_up.status_code == 200
    body = looked_up.text
    for leaked in ("QAB1", "QA Bindapur", "TEST ZONE", "Secret Campaign Name",
                   "founder"):
        assert leaked not in body, f"{leaked} reached a public response"
    assert "session_id" not in looked_up.json()


@pytest.mark.parametrize("name", ["", "   ", "x" * 41])
def test_an_unusable_name_is_refused(client, owner, name):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    response = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                           json={"display_name": name, "password": room["password"]})
    assert response.status_code in (400, 422)


def test_two_listeners_may_share_a_name(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    for _ in range(2):
        joined = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                             json={"display_name": "Harshit",
                                   "password": room["password"]})
        assert joined.status_code == 200

    state = room_of(client, owner, sid)
    assert state["counts"]["admitted"] == 2
    ids = {row["id"] for row in state["listeners"]}
    assert len(ids) == 2, "identity is the participant, never the name"


def test_repeated_wrong_passwords_are_rate_limited(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    statuses = [
        client.post(f"/api/listen/rooms/{room['public_code']}/join",
                    json={"display_name": "Guesser", "password": f"NOPE-{n:04d}"}
                    ).status_code
        for n in range(12)
    ]
    assert 429 in statuses, "a public password surface must be rate limited"


# ===========================================================================
# Request, approve, deny
# ===========================================================================

def test_a_passwordless_request_waits_and_the_broadcaster_sees_it(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    asked = client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                        json={"display_name": "Aman"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["admission_status"] == "REQUESTED"
    assert asked.json()["admitted"] is False
    # No listener session before admission.
    assert "echocast_listener=" not in asked.headers.get("set-cookie", "")

    state = room_of(client, owner, sid)
    assert state["counts"]["waiting"] == 1
    assert state["waiting"][0]["display_name"] == "Aman"


def test_approve_admits_and_the_waiting_browser_collects_its_session(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    listener = client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                           json={"display_name": "Aman"})
    assert listener.status_code == 200
    pid = room_of(client, owner, sid)["waiting"][0]["id"]

    approved = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/approve", headers=owner)
    assert approved.status_code == 200, approved.text
    assert approved.json()["counts"]["waiting"] == 0
    assert approved.json()["counts"]["admitted"] == 1
    # The listener's credential is never handed to the broadcaster.
    assert "echocast_listener" not in approved.text

    # The waiting browser polls its own state and is admitted without a refresh.
    mine = client.get("/api/listen/me")
    assert mine.status_code == 200, mine.text
    assert mine.json()["admitted"] is True
    assert "echocast_listener=" in mine.headers.get("set-cookie", "")


def test_deny_is_terminal_and_leaves_no_session(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                json={"display_name": "Aman"})
    pid = room_of(client, owner, sid)["waiting"][0]["id"]

    denied = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/deny", headers=owner)
    assert denied.status_code == 200
    assert denied.json()["counts"]["waiting"] == 0
    assert denied.json()["counts"]["admitted"] == 0

    mine = client.get("/api/listen/me")
    assert mine.status_code == 200
    assert mine.json()["admitted"] is False
    assert mine.json()["admission_status"] == "DENIED"

    # And not silently reversible.
    again = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/approve", headers=owner)
    assert again.status_code == 409


def test_approving_twice_is_idempotent(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                json={"display_name": "Aman"})
    pid = room_of(client, owner, sid)["waiting"][0]["id"]

    first = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/approve", headers=owner)
    second = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/approve", headers=owner)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["counts"]["admitted"] == 1, "one click, one listener"


# ===========================================================================
# Auto Approve
# ===========================================================================

def test_auto_approve_admits_a_request_at_once(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)

    toggled = client.put(f"/api/broadcast/sessions/{sid}/web-room/auto-approve",
                         headers=owner, json={"auto_approve": True})
    assert toggled.status_code == 200
    assert toggled.json()["auto_approve"] is True

    asked = client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                        json={"display_name": "Aman"})
    assert asked.json()["admitted"] is True
    assert "echocast_listener=" in asked.headers.get("set-cookie", "")
    assert room_of(client, owner, sid)["counts"]["waiting"] == 0


def test_a_password_join_is_unaffected_by_auto_approve(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    for enabled in (False, True):
        client.put(f"/api/broadcast/sessions/{sid}/web-room/auto-approve",
                   headers=owner, json={"auto_approve": enabled})
        joined = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                             json={"display_name": "Harshit",
                                   "password": room["password"]})
        assert joined.json()["admission_status"] == "PASSWORD_ADMITTED"


# ===========================================================================
# Password rotation
# ===========================================================================

def test_rotation_replaces_the_password_without_ejecting_the_audience(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    original = room["password"]

    client.post(f"/api/listen/rooms/{room['public_code']}/join",
                json={"display_name": "Harshit", "password": original})
    assert room_of(client, owner, sid)["counts"]["admitted"] == 1

    rotated = client.post(f"/api/broadcast/sessions/{sid}/web-room/password/rotate",
                          headers=owner)
    assert rotated.status_code == 200, rotated.text
    fresh = rotated.json()["password"]
    assert fresh and fresh != original
    # The audience is not ejected by a rotation.
    assert rotated.json()["counts"]["admitted"] == 1

    stale = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                        json={"display_name": "Late", "password": original})
    assert stale.status_code == 401, "the old password stopped working"
    good = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                       json={"display_name": "Late", "password": fresh})
    assert good.status_code == 200


def test_no_password_appears_in_the_system_log(client, owner):
    sid = make_link_only_session(client, owner)
    original = room_of(client, owner, sid)["password"]
    fresh = client.post(f"/api/broadcast/sessions/{sid}/web-room/password/rotate",
                        headers=owner).json()["password"]

    logs = client.get("/api/logs", headers=owner)
    assert logs.status_code == 200
    assert original not in logs.text and fresh not in logs.text
    # The EVENT is recorded, just never the secret.
    assert "rotated" in logs.text.lower()


# ===========================================================================
# Kick
# ===========================================================================

def test_kick_removes_the_listener_and_invalidates_the_session(client, owner):
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    client.post(f"/api/listen/rooms/{room['public_code']}/join",
                json={"display_name": "Harshit", "password": room["password"]})
    pid = room_of(client, owner, sid)["listeners"][0]["id"]

    kicked = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/kick", headers=owner)
    assert kicked.status_code == 200, kicked.text
    assert kicked.json()["counts"]["admitted"] == 0

    # The same session cannot come back.
    mine = client.get("/api/listen/me")
    assert mine.status_code == 401


# ===========================================================================
# Isolation
# ===========================================================================

def test_a_broadcaster_cannot_manage_another_operators_room(client, owner):
    """Ownership is the gate, and the session id is a small guessable integer."""
    make_user(client, owner, "rival", "BROADCASTER")
    mine = make_link_only_session(client, owner)
    rival = sign_in(client, "rival")

    assert client.get(f"/api/broadcast/sessions/{mine}/web-room",
                      headers=rival).status_code == 403
    assert client.post(f"/api/broadcast/sessions/{mine}/web-room/password/rotate",
                       headers=rival).status_code == 403
    assert client.put(f"/api/broadcast/sessions/{mine}/web-room/auto-approve",
                      headers=rival, json={"auto_approve": True}).status_code == 403
    assert client.post(f"/api/broadcast/sessions/{mine}/web-participants/1/kick",
                       headers=rival).status_code == 403


def test_a_participant_of_another_room_cannot_be_reached(client, owner):
    first = make_link_only_session(client, owner, "First")
    second = make_link_only_session(client, owner, "Second")
    other_room = room_of(client, owner, second)
    client.post(f"/api/listen/rooms/{other_room['public_code']}/join",
                json={"display_name": "Bob", "password": other_room["password"]})
    theirs = room_of(client, owner, second)["listeners"][0]["id"]

    # Same owner, wrong room: the room check is what stops it.
    for action in ("approve", "deny", "kick"):
        response = client.post(
            f"/api/broadcast/sessions/{first}/web-participants/{theirs}/{action}",
            headers=owner)
        assert response.status_code == 409, f"{action} reached another room"


def test_the_room_api_needs_an_hq_account(client, owner):
    sid = make_link_only_session(client, owner)
    assert client.get(f"/api/broadcast/sessions/{sid}/web-room").status_code == 401


def test_a_listener_cookie_cannot_reach_hq_apis(client, owner):
    """The listener credential authorises one room, and nothing in HQ."""
    sid = make_link_only_session(client, owner)
    room = room_of(client, owner, sid)
    client.post(f"/api/listen/rooms/{room['public_code']}/join",
                json={"display_name": "Harshit", "password": room["password"]})

    # The cookie jar now holds a valid listener session.
    for path in ("/api/stores", "/api/users", "/api/broadcast/target-stores",
                 "/api/logs", f"/api/broadcast/sessions/{sid}/web-room",
                 f"/api/broadcast/sessions/{sid}/web-participants"):
        response = client.get(path)
        assert response.status_code in (401, 403), \
            f"{path} accepted a listener cookie ({response.status_code})"
