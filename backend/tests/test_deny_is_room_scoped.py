"""A Deny refuses ONE admission attempt in ONE Broadcast. It is not a ban.

Manual testing found the same shape of defect the Kick had: after being denied
in Broadcast A the listener opened a completely different Broadcast B and was
told their request had been denied there too.

Deny and Kick reach the listener through the same two cookies, so these tests
deliberately re-prove the scope rules for the denial path rather than assuming
the Kick fix covered it. Where the two share a mechanism, that is worth stating
with a test rather than with a comment.
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


@pytest.fixture()
def owner(client):
    response = client.post("/api/auth/login",
                           json={"username": "founder", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_room(client, headers, campaign):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "only_with_link"})
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    room = client.get(f"/api/broadcast/sessions/{sid}/web-room", headers=headers)
    assert room.status_code == 200, room.text
    return sid, room.json()


def request_access(client, room, name="Harshit"):
    return client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                       json={"display_name": name})


def join(client, room, name="Harshit"):
    return client.post(f"/api/listen/rooms/{room['public_code']}/join",
                       json={"display_name": name, "password": room["password"]})


def state_of(client, headers, sid):
    body = client.get(f"/api/broadcast/sessions/{sid}/web-room",
                      headers=headers).json()
    return body["waiting"] + body["listeners"], body["counts"]


def stored(client, participant_id):
    """The participant row itself, which the console lists deliberately omit."""
    import web_rooms
    row = web_rooms.get_participant(client.server_module.engine,
                                    participant_id=participant_id)
    assert row is not None, "a denied participant row must not be deleted"
    return row


def me(client, public_code=None):
    params = {"public_code": public_code} if public_code else None
    return client.get("/api/listen/me", params=params)


def deny(client, headers, sid, pid):
    return client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/deny",
        headers=headers)


def another_browser(client):
    from fastapi.testclient import TestClient
    return TestClient(client.server_module.app)


# ===========================================================================
# TEST Y - the denial itself
# ===========================================================================

def test_Y_a_denial_ends_that_attempt_and_leaves_the_room_empty(client, owner):
    sid, room = make_room(client, owner, "Broadcast A")
    assert request_access(client, room).status_code == 200
    rows, counts = state_of(client, owner, sid)
    a1 = rows[0]["id"]
    assert counts["waiting"] == 1

    assert deny(client, owner, sid, a1).status_code == 200

    assert stored(client, a1).admission_status == "DENIED"
    rows, counts = state_of(client, owner, sid)
    assert counts["waiting"] == 0
    assert counts["connected"] == 0
    assert counts["listening"] == 0
    assert counts["buffering"] == 0
    assert a1 not in {row["id"] for row in rows}


def test_Y_the_denied_browser_is_told_the_truth_about_that_room(client, owner):
    """Asking about A after being denied in A is answered honestly."""
    sid, room = make_room(client, owner, "Broadcast A")
    assert request_access(client, room).status_code == 200
    rows, _ = state_of(client, owner, sid)
    assert deny(client, owner, sid, rows[0]["id"]).status_code == 200

    about_a = me(client, room["public_code"])
    assert about_a.status_code in (200, 401)
    if about_a.status_code == 200:
        assert about_a.json()["admission_status"] == "DENIED"
        assert about_a.json()["admitted"] is False


# ===========================================================================
# TEST Z, AG - a denial in A says nothing about B
# ===========================================================================

def test_Z_asking_about_another_broadcast_is_not_a_denial(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    _, b = make_room(client, owner, "Broadcast B")

    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    assert deny(client, owner, a_sid, rows[0]["id"]).status_code == 200

    # The pending claim naming A1 is still in this browser's jar - which is
    # exactly the condition the report describes.
    assert "echocast_listener_pending" in client.cookies

    about_b = me(client, b["public_code"])
    assert about_b.status_code == 401, (
        f"the A denial leaked into B: {about_b.text}")
    # And nothing about A is revealed in the process.
    assert a["public_code"] not in about_b.text
    assert "DENIED" not in about_b.text


def test_AG_a_crafted_a_session_neither_authorises_nor_denies_b(client, owner):
    """A denied session, and an admitted one, both mean nothing in B."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    b_sid, b = make_room(client, owner, "Broadcast B")

    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    assert deny(client, owner, a_sid, rows[0]["id"]).status_code == 200

    assert me(client, b["public_code"]).status_code == 401
    _, counts_b = state_of(client, owner, b_sid)
    assert counts_b == {"waiting": 0, "admitted": 0, "connected": 0,
                        "listening": 0, "buffering": 0, "paused": 0}, (
        "asking about B must not create anything in B")

    # The same holds for a LIVE A session: it is not authority in B either.
    healthy = another_browser(client)
    assert healthy.post(f"/api/listen/rooms/{a['public_code']}/join",
                        json={"display_name": "Aman",
                              "password": a["password"]}).status_code == 200
    assert healthy.get("/api/listen/me",
                       params={"public_code": b["public_code"]}).status_code == 401


# ===========================================================================
# TEST AA, AB - joining B afterwards
# ===========================================================================

def test_AA_a_password_join_in_b_works_after_being_denied_in_a(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    b_sid, b = make_room(client, owner, "Broadcast B")

    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert deny(client, owner, a_sid, a1).status_code == 200

    joined = join(client, b)
    assert joined.status_code == 200, joined.text
    assert joined.json()["admitted"] is True
    assert joined.json()["public_code"] == b["public_code"]

    rows_b, counts_b = state_of(client, owner, b_sid)
    assert counts_b["admitted"] == 1
    assert rows_b[0]["id"] != a1
    assert stored(client, a1).admission_status == "DENIED", (
        "the denial in A is untouched by anything that happens in B")


def test_AB_a_request_in_b_reaches_b_and_only_b(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    b_sid, b = make_room(client, owner, "Broadcast B")

    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert deny(client, owner, a_sid, a1).status_code == 200

    assert request_access(client, b).status_code == 200
    rows_b, counts_b = state_of(client, owner, b_sid)
    assert counts_b["waiting"] == 1
    b1 = rows_b[0]["id"]
    assert b1 != a1

    _, counts_a = state_of(client, owner, a_sid)
    assert counts_a["waiting"] == 0, "room A must not move because B did"

    approved = client.post(
        f"/api/broadcast/sessions/{b_sid}/web-participants/{b1}/approve",
        headers=owner)
    assert approved.status_code == 200, approved.text

    seen = me(client, b["public_code"])
    assert seen.status_code == 200 and seen.json()["admitted"] is True


# ===========================================================================
# TEST AC, AD, AE - trying again in the SAME Broadcast
# ===========================================================================

def test_AC_request_again_creates_a_new_attempt(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert deny(client, owner, a_sid, a1).status_code == 200

    # Request Again discards the spent attempt. It requests nothing by itself.
    assert client.post("/api/listen/forget").status_code == 200
    assert me(client, a["public_code"]).status_code == 401
    _, counts = state_of(client, owner, a_sid)
    assert counts["waiting"] == 0, "the retry button must not send the request"

    assert request_access(client, a).status_code == 200
    rows, counts = state_of(client, owner, a_sid)
    assert counts["waiting"] == 1
    a2 = rows[0]["id"]
    assert a2 != a1
    assert stored(client, a2).admission_status == "REQUESTED"
    assert stored(client, a1).admission_status == "DENIED", (
        "the denial is history and is never mutated back into a request")


def test_AD_the_current_password_admits_after_a_denial(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert deny(client, owner, a_sid, a1).status_code == 200

    assert client.post("/api/listen/forget").status_code == 200
    admitted = join(client, a)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["admitted"] is True

    rows, counts = state_of(client, owner, a_sid)
    assert counts["admitted"] == 1
    assert rows[0]["id"] != a1
    assert stored(client, a1).admission_status == "DENIED"
    # And a wrong password is still a wrong password afterwards.
    latecomer = another_browser(client)
    assert latecomer.post(f"/api/listen/rooms/{a['public_code']}/join",
                          json={"display_name": "Late",
                                "password": "not-it"}).status_code == 401


def test_AE_asking_again_about_the_denied_room_does_not_resubmit(client, owner):
    """A refresh is not a retry: it reports, it does not request."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    assert deny(client, owner, a_sid, rows[0]["id"]).status_code == 200

    for _ in range(3):
        me(client, a["public_code"])
    _, counts = state_of(client, owner, a_sid)
    assert counts["waiting"] == 0, "refreshing must never create a new request"
    assert counts["admitted"] == 0


# ===========================================================================
# Identity is the admission attempt. Not the name, not the address.
# ===========================================================================

def test_a_denied_name_does_not_deny_anybody_else(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a, name="Harshit").status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    assert deny(client, owner, a_sid, rows[0]["id"]).status_code == 200

    namesake = another_browser(client)
    asked = namesake.post(f"/api/listen/rooms/{a['public_code']}/request-access",
                          json={"display_name": "Harshit"})
    assert asked.status_code == 200, asked.text
    assert asked.json()["admission_status"] == "REQUESTED", (
        "a denied NAME must not deny a different person")


def test_a_denial_does_not_block_the_address_it_came_from(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    assert deny(client, owner, a_sid, rows[0]["id"]).status_code == 200

    neighbour = another_browser(client)      # same client host
    assert neighbour.post(f"/api/listen/rooms/{a['public_code']}/join",
                          json={"display_name": "Someone else",
                                "password": a["password"]}).status_code == 200


def test_a_stale_denied_claim_cannot_speak_for_the_new_attempt(client, owner):
    """The spent claim names A1 forever; it must never answer as A2."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a1 = rows[0]["id"]
    stale_claim = client.cookies.get("echocast_listener_pending")
    assert deny(client, owner, a_sid, a1).status_code == 200

    assert client.post("/api/listen/forget").status_code == 200
    assert request_access(client, a).status_code == 200
    rows, _ = state_of(client, owner, a_sid)
    a2 = rows[0]["id"]
    assert a2 != a1

    # Someone replays the old claim. It names a denied attempt, and that is all
    # it can ever mean - it cannot become the new one.
    replay = another_browser(client)
    replay.cookies.set("echocast_listener_pending", stale_claim,
                       path="/api/listen")
    answer = replay.get("/api/listen/me",
                        params={"public_code": a["public_code"]})
    if answer.status_code == 200:
        assert answer.json()["admitted"] is False
        assert answer.json()["admission_status"] == "DENIED"
    assert stored(client, a2).admission_status == "REQUESTED", (
        "the replay must not have disturbed the live attempt")
