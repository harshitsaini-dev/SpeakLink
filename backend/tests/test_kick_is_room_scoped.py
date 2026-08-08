"""A Kick removes a participant from ONE Broadcast. It is not a ban.

Manual testing found the opposite: after being kicked from Broadcast A a
listener could not ask to join A again, and could not join a completely
different Broadcast B either. The kick had become a property of the browser.

The cause was that ``/listen/me`` had no idea which room the browser was
looking at. A kicked listener's pending claim cookie still resolved, so the
endpoint answered with whatever room this browser had last touched - and the
page dutifully reported "You were removed from this Broadcast" about a
Broadcast the listener had never opened.

These tests go through the real routes with a real database, because the defect
was in what a route answered and not in what any function computed.
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPEAKLINK_LAN_HTTP_LISTENERS", "1")

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


def join(client, room, name="Harshit"):
    return client.post(f"/api/listen/rooms/{room['public_code']}/join",
                       json={"display_name": name, "password": room["password"]})


def request_access(client, room, name="Harshit"):
    return client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                       json={"display_name": name})


def participants(client, headers, sid):
    body = client.get(f"/api/broadcast/sessions/{sid}/web-room",
                      headers=headers).json()
    return body["waiting"] + body["listeners"], body["counts"]


def kick(client, headers, sid, participant_id):
    return client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{participant_id}/kick",
        headers=headers)


def kicked_row(client, participant_id):
    """The stored participant, read straight from the database.

    The console lists deliberately omit removed listeners, so their audit state
    has to be read where it lives rather than where it is displayed.
    """
    import web_rooms
    row = web_rooms.get_participant(client.server_module.engine,
                                    participant_id=participant_id)
    assert row is not None, "a kicked participant row must not be deleted"
    return row


def me(client, public_code=None):
    params = {"public_code": public_code} if public_code else None
    return client.get("/api/listen/me", params=params)


# ===========================================================================
# The reported bug, at the level it actually happened
# ===========================================================================

def test_a_kick_from_one_broadcast_says_nothing_about_another(client, owner):
    """The exact manual defect: kicked from A, then B claimed to have removed them."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    b_sid, b = make_room(client, owner, "Broadcast B")

    joined = join(client, a)
    assert joined.status_code == 200, joined.text
    rows, _ = participants(client, owner, a_sid)
    a1 = rows[0]["id"]

    assert kick(client, owner, a_sid, a1).status_code == 200

    # Asking about A, the removal is the truth and is reported.
    about_a = me(client, a["public_code"])
    assert about_a.status_code in (200, 401)
    if about_a.status_code == 200:
        assert about_a.json()["admission_status"] == "KICKED"

    # Asking about B, this browser simply has no session. NOT "removed".
    about_b = me(client, b["public_code"])
    assert about_b.status_code == 401, (
        f"the A kick leaked into B: {about_b.text}")

    # And B admits them normally, same browser, same cookies.
    into_b = join(client, b)
    assert into_b.status_code == 200, into_b.text
    assert into_b.json()["admitted"] is True
    assert into_b.json()["public_code"] == b["public_code"]

    rows_b, counts_b = participants(client, owner, b_sid)
    assert counts_b["admitted"] == 1
    assert rows_b[0]["id"] != a1, "B must be a new participant, not A's row"


def test_a_pending_claim_from_another_room_is_not_an_answer(client, owner):
    """The precise mechanism: the pending cookie outlived its room.

    Request Access leaves a claim cookie naming the participant. It was accepted
    by /listen/me without any room check, so it answered for room A no matter
    which Broadcast the browser had open.
    """
    a_sid, a = make_room(client, owner, "Broadcast A")
    _, b = make_room(client, owner, "Broadcast B")

    assert request_access(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert kick(client, owner, a_sid, a1).status_code == 200

    assert "speaklink_listener_pending" in client.cookies, (
        "the fixture no longer reproduces the original conditions")
    assert me(client, b["public_code"]).status_code == 401


def test_asking_about_a_broadcast_never_names_a_different_one(client, owner):
    """Scoping must not leak the other room's identity either."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    _, b = make_room(client, owner, "Broadcast B")

    assert join(client, a).status_code == 200
    scoped = me(client, b["public_code"])
    assert scoped.status_code == 401
    assert a["public_code"] not in scoped.text


# ===========================================================================
# Same Broadcast: removal is terminal until the listener chooses otherwise
# ===========================================================================

def test_a_kicked_session_cannot_simply_resume(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    assert kick(client, owner, a_sid, rows[0]["id"]).status_code == 200

    state = me(client, a["public_code"])
    assert state.status_code == 401 or state.json()["admitted"] is False


def test_join_again_then_the_password_creates_a_new_participant(client, owner):
    """Kick stays historically true; the rejoin is a new admission."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert kick(client, owner, a_sid, a1).status_code == 200

    # Join Again discards the spent session. It admits nobody by itself.
    assert client.post("/api/listen/forget").status_code == 200
    assert me(client, a["public_code"]).status_code == 401

    rejoined = join(client, a)
    assert rejoined.status_code == 200, rejoined.text
    assert rejoined.json()["admitted"] is True

    rows, counts = participants(client, owner, a_sid)
    # The kicked row is gone from the console, which is what the operator
    # should see, and still KICKED in the database, which is the audit truth.
    assert a1 not in {row["id"] for row in rows}, (
        "a removed listener must not linger in the Web Audience lists")
    kicked = kicked_row(client, a1)
    assert kicked.admission_status == "KICKED", (
        "the old participant must remain KICKED - Kick is history, not a toggle")

    assert len(rows) == 1, "exactly one new admission attempt"
    assert rows[0]["id"] != a1
    assert rows[0]["admission_status"] == "PASSWORD_ADMITTED"
    assert counts["admitted"] == 1, "the KICKED row is not counted as admitted"


def test_request_again_after_a_kick_reaches_the_broadcaster(client, owner):
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    a1 = rows[0]["id"]
    assert kick(client, owner, a_sid, a1).status_code == 200
    assert client.post("/api/listen/forget").status_code == 200

    assert request_access(client, a).status_code == 200
    rows, counts = participants(client, owner, a_sid)
    assert counts["waiting"] == 1, "the broadcaster sees a NEW request"
    waiting = [row for row in rows if row["admission_status"] == "REQUESTED"]
    assert len(waiting) == 1 and waiting[0]["id"] != a1

    approved = client.post(
        f"/api/broadcast/sessions/{a_sid}/web-participants/{waiting[0]['id']}/approve",
        headers=owner)
    assert approved.status_code == 200, approved.text

    state = me(client, a["public_code"])
    assert state.status_code == 200 and state.json()["admitted"] is True


def test_forget_grants_nothing_by_itself(client, owner):
    """Join Again must not be a button that undoes a Kick."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    assert kick(client, owner, a_sid, rows[0]["id"]).status_code == 200
    assert client.post("/api/listen/forget").status_code == 200

    assert me(client, a["public_code"]).status_code == 401
    wrong = client.post(f"/api/listen/rooms/{a['public_code']}/join",
                        json={"display_name": "Harshit", "password": "not-it"})
    assert wrong.status_code == 401, "a rejoin still needs the real password"


# ===========================================================================
# Identity is the session. Not the name, not the address.
# ===========================================================================

def test_the_same_display_name_is_not_a_ban(client, owner):
    """Two real people share a name far more often than they share a session."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a, name="Harshit").status_code == 200
    rows, _ = participants(client, owner, a_sid)
    assert kick(client, owner, a_sid, rows[0]["id"]).status_code == 200

    from fastapi.testclient import TestClient
    other = TestClient(client.server_module.app)     # its own cookie jar
    fresh = other.post(f"/api/listen/rooms/{a['public_code']}/join",
                       json={"display_name": "Harshit",
                             "password": a["password"]})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["admitted"] is True, (
        "a kicked NAME must not block a different person")


def test_a_second_browser_from_the_same_address_still_joins(client, owner):
    """No IP ban: a store, an office or a home is one address and many people."""
    a_sid, a = make_room(client, owner, "Broadcast A")
    assert join(client, a).status_code == 200
    rows, _ = participants(client, owner, a_sid)
    assert kick(client, owner, a_sid, rows[0]["id"]).status_code == 200

    from fastapi.testclient import TestClient
    neighbour = TestClient(client.server_module.app)   # same client host
    joined = neighbour.post(f"/api/listen/rooms/{a['public_code']}/join",
                            json={"display_name": "Someone else",
                                  "password": a["password"]})
    assert joined.status_code == 200, joined.text


def test_a_late_cleanup_from_the_kicked_socket_leaves_the_new_one_alone(client, owner):
    """A1's socket closing after A2 has connected must not disconnect A2.

    Kick closes a socket, and that close is handled asynchronously. The two
    connections are different participants, so the registry key differs - and
    the detach is additionally guarded by socket identity, so even a re-used id
    could not evict its replacement.
    """
    from web_participant_runtime import WebParticipantRegistry, PlaybackState

    registry = WebParticipantRegistry()
    old_socket, new_socket = object(), object()
    a1 = registry.attach(participant_id=17, room_id=1, session_id=1,
                         socket=old_socket)
    a2 = registry.attach(participant_id=18, room_id=1, session_id=1,
                         socket=new_socket)
    registry.heartbeat(participant_id=18, playback_state=PlaybackState.LISTENING)

    # The kicked socket finally unwinds, well after the rejoin.
    assert registry.detach(participant_id=17, socket=old_socket) is True
    assert registry.get(18) is a2, "the new session survived the old cleanup"
    assert registry.counts_for_room(1)["listening"] == 1
    assert registry.counts_for_room(1)["connected"] == 1

    # And a cleanup naming the new participant with the OLD socket is refused.
    assert registry.detach(participant_id=18, socket=old_socket) is False
    assert registry.get(18) is a2
    assert registry.get(17) is None and a1.participant_id == 17


def test_a_room_a_session_grants_nothing_in_room_b(client, owner):
    """Scoping fixed a denial leak; it must not open an access one."""
    _, a = make_room(client, owner, "Broadcast A")
    b_sid, b = make_room(client, owner, "Broadcast B")

    joined = join(client, a)
    assert joined.status_code == 200

    # A valid, live, non-kicked A session presented against B.
    assert me(client, b["public_code"]).status_code == 401, (
        "an A session must not authorise B")
    _, counts_b = participants(client, owner, b_sid)
    assert counts_b["admitted"] == 0, "no B admission was created"
