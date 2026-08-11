"""Chat between a Broadcast's web audience and the operator hosting it.

Driven through the real HTTP surface, because every rule here is one a browser
could simply not send: a muted listener posting anyway, a second listener
asking for somebody else's private message, another operator reading a room
that is not theirs.

The properties worth stating plainly, because they are what these tests are
for rather than incidental behaviour:

  * PRIVATE is a property of the stored message, not of the fanout. A listener
    who refetches, reconnects or calls the API directly still cannot read
    another listener's private message.
  * chat_enabled silences the AUDIENCE, never the host.
  * deletion is a tombstone: the row and the author stay, the words go.
  * chat retention is the BROADCAST's. Delete the broadcast from history and
    the chat goes with it, in the same transaction.
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
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPEAKLINK_LAN_HTTP_LISTENERS", "1")

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "web_rooms", "web_chat",
                               "web_participant_runtime")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client)


def make_session(client, headers, campaign="Link only"):
    response = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "only_with_link"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def room_of(client, headers, sid):
    response = client.get(f"/api/broadcast/sessions/{sid}/web-room", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def listener(client, room, name):
    """A second browser: a listener is a session, so each is its own cookie jar."""
    from fastapi.testclient import TestClient
    browser = TestClient(client.server_module.app)
    joined = browser.post(f"/api/listen/rooms/{room['public_code']}/join",
                          json={"display_name": name, "password": room["password"]})
    assert joined.status_code == 200, joined.text
    return browser


@pytest.fixture()
def live_room(client, owner):
    """A Broadcast with a room and one admitted listener."""
    sid = make_session(client, owner)
    room = room_of(client, owner, sid)
    return sid, room, listener(client, room, "Harshit")


def say(browser, body):
    return browser.post("/api/listen/chat", json={"body": body})


def host_say(client, headers, sid, body):
    return client.post(f"/api/broadcast/sessions/{sid}/chat", headers=headers,
                       json={"body": body})


def host_view(client, headers, sid):
    response = client.get(f"/api/broadcast/sessions/{sid}/chat", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def listener_view(browser):
    response = browser.get("/api/listen/chat")
    assert response.status_code == 200, response.text
    return response.json()


def bodies(view):
    return [m["body"] for m in view["messages"]]


# ===========================================================================
# The ordinary conversation
# ===========================================================================

def test_a_listener_can_ask_and_the_host_can_answer(client, owner, live_room):
    sid, _room, harshit = live_room

    assert say(harshit, "We cannot hear you at the till").status_code == 200
    assert host_say(client, owner, sid, "Repeating it now").status_code == 200

    seen_by_host = host_view(client, owner, sid)
    assert bodies(seen_by_host) == ["We cannot hear you at the till", "Repeating it now"]
    assert [m["author_kind"] for m in seen_by_host["messages"]] == ["LISTENER", "HOST"]

    # Order is the order it was said in, not newest-first: a conversation read
    # backwards is not a conversation.
    assert bodies(listener_view(harshit)) == ["We cannot hear you at the till",
                                              "Repeating it now"]


def test_chat_is_on_and_public_by_default(client, owner, live_room):
    _sid, _room, harshit = live_room
    view = listener_view(harshit)
    assert view["chat_enabled"] is True
    assert view["chat_mode"] == "PUBLIC"


def test_two_listeners_see_each_other_in_public_mode(client, owner, live_room):
    _sid, room, harshit = live_room
    priya = listener(client, room, "Priya")

    assert say(harshit, "Hello from the till").status_code == 200
    assert bodies(listener_view(priya)) == ["Hello from the till"]


# ===========================================================================
# PRIVATE is a property of the message, not of the screen
# ===========================================================================

def test_in_private_mode_one_listener_cannot_read_another(client, owner, live_room):
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")

    assert client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
                      json={"chat_mode": "PRIVATE"}).status_code == 200

    assert say(harshit, "Our amplifier is off").status_code == 200

    # The author still sees their own message; nobody else does.
    assert bodies(listener_view(harshit)) == ["Our amplifier is off"]
    assert bodies(listener_view(priya)) == []
    # And the host, who it was addressed to, does.
    assert bodies(host_view(client, owner, sid)) == ["Our amplifier is off"]


def test_the_hosts_own_replies_stay_public_in_private_mode(client, owner, live_room):
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PRIVATE"})

    assert host_say(client, owner, sid, "Sorry, saying it again").status_code == 200

    # Both hear the announcement, so both read the answer about it.
    assert bodies(listener_view(harshit)) == ["Sorry, saying it again"]
    assert bodies(listener_view(priya)) == ["Sorry, saying it again"]


def test_going_public_does_not_publish_what_was_said_in_private(client, owner, live_room):
    """The rule that makes PRIVATE mean anything.

    A message sent in confidence must not become readable because the host
    later flipped a switch. If this ever fails, private mode is a display
    setting rather than a promise.
    """
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PRIVATE"})
    say(harshit, "Please do not read this out")

    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PUBLIC"})

    assert bodies(listener_view(priya)) == []


def test_going_private_does_not_retract_what_was_already_public(client, owner, live_room):
    # The reverse, and equally important: hiding what was already said in
    # public fools nobody who was in the room, and a transcript that quietly
    # loses messages is worse than one that keeps them.
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    say(harshit, "Already said out loud")

    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PRIVATE"})

    assert bodies(listener_view(priya)) == ["Already said out loud"]


# ===========================================================================
# The host's controls
# ===========================================================================

def test_turning_chat_off_stops_listeners_and_not_the_host(client, owner, live_room):
    sid, _room, harshit = live_room

    assert client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
                      json={"chat_enabled": False}).status_code == 200

    refused = say(harshit, "Anyone there?")
    assert refused.status_code == 403
    assert "turned chat off" in refused.json()["detail"]

    # The operator may still answer the last question before the room goes quiet.
    assert host_say(client, owner, sid, "Chat is closing, thank you").status_code == 200
    assert bodies(listener_view(harshit)) == ["Chat is closing, thank you"]


def test_turning_chat_back_on_lets_the_audience_speak_again(client, owner, live_room):
    sid, _room, harshit = live_room
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_enabled": False})
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_enabled": True})
    assert say(harshit, "Back again").status_code == 200


def test_one_switch_does_not_move_the_other(client, owner, live_room):
    sid, _room, _harshit = live_room
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PRIVATE"})
    state = client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
                       json={"chat_enabled": False}).json()
    assert state["chat_mode"] == "PRIVATE", "turning chat off changed the mode too"
    assert state["chat_enabled"] is False


def test_a_muted_listener_is_refused_and_the_others_are_not(client, owner, live_room):
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    participants = client.get(f"/api/broadcast/sessions/{sid}/web-participants",
                              headers=owner).json()
    harshit_id = next(p["id"] for p in participants["listeners"]
                      if p["display_name"] == "Harshit")

    muted = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{harshit_id}/chat-mute",
        headers=owner, json={"muted": True})
    assert muted.status_code == 200, muted.text

    refused = say(harshit, "let me in")
    assert refused.status_code == 403
    assert "muted" in refused.json()["detail"]
    # Muting one person is not closing the room.
    assert say(priya, "still here").status_code == 200
    # And it is not a Kick: the muted listener is still admitted and still
    # hearing the Broadcast.
    assert listener_view(harshit)["muted"] is True


def test_unmuting_gives_the_listener_their_voice_back(client, owner, live_room):
    sid, _room, harshit = live_room
    pid = client.get(f"/api/broadcast/sessions/{sid}/web-participants",
                     headers=owner).json()["listeners"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/web-participants/{pid}/chat-mute",
                headers=owner, json={"muted": True})
    client.post(f"/api/broadcast/sessions/{sid}/web-participants/{pid}/chat-mute",
                headers=owner, json={"muted": False})
    assert say(harshit, "thank you").status_code == 200


def test_a_removed_message_is_a_tombstone_for_listeners(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "something regrettable")
    message_id = host_view(client, owner, sid)["messages"][0]["id"]

    removed = client.post(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
        headers=owner)
    assert removed.status_code == 200, removed.text

    seen = listener_view(harshit)["messages"]
    assert len(seen) == 1, "the row is a tombstone, not a hole"
    assert seen[0]["deleted"] is True
    assert seen[0]["body"] is None, "a listener could still read a removed message"
    assert seen[0]["author_name"] == "Harshit", "who said it is still recorded"


def test_an_account_with_see_deleted_chat_reads_what_was_removed(client, owner, live_room):
    """Removal takes a message OUT OF THE ROOM; it does not erase it.

    Who may still read it is a PERMISSION, not the ownership of the Broadcast:
    removing a message is a moderation act, and the person who moderates is not
    automatically the person entitled to keep reading what they took down.
    OWNER holds chat.view_deleted by default.
    """
    sid, _room, harshit = live_room
    say(harshit, "something regrettable")
    message_id = host_view(client, owner, sid)["messages"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
                headers=owner)

    view = host_view(client, owner, sid)
    assert view["may_see_removed"] is True
    message = view["messages"][0]
    assert message["deleted"] is True
    assert message["body"] == "something regrettable"


def test_a_broadcaster_without_the_right_sees_only_the_tombstone(client, owner, live_room):
    """The separation this permission exists for.

    The operator running the Broadcast is not, by that fact alone, entitled to
    keep reading messages they removed. They see what their audience sees.
    """
    sid, room, _harshit = live_room
    made = client.post("/api/users", headers=owner, json={
        "username": "runner", "display_name": "Runner", "role": "BROADCASTER",
        "password": PASSWORD})
    assert made.status_code == 201, made.text
    runner = sign_in(client, "runner")

    # Their own Broadcast, so ownership is not what refuses them.
    their_sid = make_session(client, runner, "Theirs")
    their_room = room_of(client, runner, their_sid)
    guest = listener(client, their_room, "Guest")
    say(guest, "something regrettable")
    message_id = host_view(client, runner, their_sid)["messages"][0]["id"]
    client.post(
        f"/api/broadcast/sessions/{their_sid}/chat/messages/{message_id}/delete",
        headers=runner)

    view = host_view(client, runner, their_sid)
    assert view["may_see_removed"] is False
    assert view["messages"][0]["deleted"] is True
    assert view["messages"][0]["body"] is None


def test_the_transcript_shows_removed_messages_in_full(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "regrettable")
    message_id = host_view(client, owner, sid)["messages"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
                headers=owner)
    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)

    transcript = client.get(f"/api/broadcast/history/{sid}/chat", headers=owner)
    assert transcript.status_code == 200, transcript.text
    message = transcript.json()["messages"][0]
    assert message["deleted"] is True and message["body"] == "regrettable"

    # And an account without the right reads the same transcript with the
    # removed half still removed.
    client.post("/api/users", headers=owner, json={
        "username": "reader", "display_name": "Reader", "role": "BROADCASTER",
        "password": PASSWORD})
    reader = sign_in(client, "reader")
    theirs = client.get(f"/api/broadcast/history/{sid}/chat", headers=reader)
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["messages"][0]["body"] is None


def test_deleting_the_broadcast_really_does_erase_a_removed_message(client, owner, live_room):
    """The line between the two kinds of deletion.

    Removing a message hides it from the room. Deleting the BROADCAST is what
    makes it unrecoverable - including the ones already removed.
    """
    sid, _room, harshit = live_room
    say(harshit, "regrettable")
    message_id = host_view(client, owner, sid)["messages"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
                headers=owner)
    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)

    removed = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                          json={"ids": [sid], "confirm": "DELETE", "acknowledged": True})
    assert removed.status_code == 200, removed.text

    server = client.server_module
    from sqlalchemy import text
    with server.engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM web_chat_messages")).scalar_one() == 0


def test_deleting_the_same_message_twice_is_a_404_not_a_second_delete(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "once")
    message_id = host_view(client, owner, sid)["messages"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
                headers=owner)
    again = client.post(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
        headers=owner)
    assert again.status_code == 404


# ===========================================================================
# Who may do what
# ===========================================================================

def test_another_operator_cannot_read_or_change_this_chat(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "we cannot hear you")
    client.post("/api/users", headers=owner, json={
        "username": "other", "display_name": "Other", "role": "BROADCASTER",
        "password": PASSWORD})
    stranger = sign_in(client, "other")

    assert client.get(f"/api/broadcast/sessions/{sid}/chat",
                      headers=stranger).status_code == 403
    assert host_say(client, stranger, sid, "hello").status_code == 403
    assert client.put(f"/api/broadcast/sessions/{sid}/chat/settings",
                      headers=stranger, json={"chat_enabled": False}).status_code == 403


def test_a_browser_that_never_joined_cannot_read_or_post(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "private to this room")
    from fastapi.testclient import TestClient
    stranger = TestClient(client.server_module.app)

    assert stranger.get("/api/listen/chat").status_code == 401
    assert stranger.post("/api/listen/chat", json={"body": "hi"}).status_code == 401


def test_a_kicked_listener_can_no_longer_post_or_read(client, owner, live_room):
    """A Kick removes somebody from the room, and chat is part of the room."""
    sid, _room, harshit = live_room
    say(harshit, "before the kick")
    pid = client.get(f"/api/broadcast/sessions/{sid}/web-participants",
                     headers=owner).json()["listeners"][0]["id"]
    kicked = client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/kick", headers=owner)
    assert kicked.status_code == 200, kicked.text

    assert say(harshit, "let me back in").status_code == 401
    assert harshit.get("/api/listen/chat").status_code == 401


# ===========================================================================
# What will not be stored
# ===========================================================================

def test_an_empty_or_whitespace_message_is_refused(client, owner, live_room):
    _sid, _room, harshit = live_room
    assert say(harshit, "").status_code == 422
    assert say(harshit, "     ").status_code == 403


def test_an_over_long_message_is_refused_rather_than_truncated(client, owner, live_room):
    """Truncating changes what somebody said, which is worse than declining it."""
    sid, _room, harshit = live_room
    assert say(harshit, "x" * 501).status_code == 422
    assert host_view(client, owner, sid)["messages"] == []


def test_markup_is_stored_exactly_as_typed(client, owner, live_room):
    """No escaping on the way in.

    The client renders messages as TEXT, which is what decides whether markup
    executes. Escaping here would corrupt a message that legitimately contains
    < or &, and would double-escape once the renderer did its job.
    """
    sid, _room, harshit = live_room
    payload = '<script>alert("x")</script> & <b>bold</b>'
    assert say(harshit, payload).status_code == 200
    assert bodies(host_view(client, owner, sid)) == [payload]


def test_a_flood_is_rate_limited_and_says_to_wait(client, owner, live_room):
    sid, _room, harshit = live_room
    accepted = 0
    refusal = None
    for index in range(12):
        response = say(harshit, f"message {index}")
        if response.status_code == 200:
            accepted += 1
        else:
            refusal = response
            break

    assert refusal is not None, "a listener could post twelve messages in a second"
    assert refusal.status_code == 429
    assert "Wait a few seconds" in refusal.json()["detail"]
    # The messages that WERE accepted are still there - a rate limit refuses
    # the next one, it does not discard the conversation.
    assert len(host_view(client, owner, sid)["messages"]) == accepted


def test_the_rate_limit_is_per_listener(client, owner, live_room):
    _sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    for index in range(6):
        say(harshit, f"flood {index}")
    assert say(priya, "my first message").status_code == 200


# ===========================================================================
# Retention: the chat belongs to the broadcast
# ===========================================================================

def test_deleting_the_broadcast_from_history_takes_the_chat_with_it(client, owner, live_room):
    sid, _room, harshit = live_room
    say(harshit, "said during the broadcast")
    server = client.server_module
    from sqlalchemy import text

    def stored():
        with server.engine.connect() as connection:
            return connection.execute(text(
                "SELECT COUNT(*) FROM web_chat_messages")).scalar_one()

    assert stored() == 1

    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)
    removed = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                          json={"ids": [sid], "confirm": "DELETE", "acknowledged": True})
    assert removed.status_code == 200, removed.text

    # No second cleanup job, no orphaned record of what somebody typed
    # outliving the thing it was about.
    assert stored() == 0
