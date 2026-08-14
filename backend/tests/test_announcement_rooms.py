"""A listening link for a recorded announcement.

Its own room with its own life, and the tests are mostly about that: a
broadcast room exists only while somebody holds a microphone, and an
announcement is DUCKED for the whole of any broadcast - so a link hung off the
broadcast room would work only at the exact times the announcement was silent.

The rest is about what a link is: something that leaves the building. It has
to be withdrawable, it must not tell a stranger which half of their guess was
right, and it must not open any campaign except its own.
"""

from __future__ import annotations

import io
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
    monkeypatch.setenv("SPEAKLINK_KEY_PROTECTOR", "fake")
    monkeypatch.setenv("SPEAKLINK_KEY_CONTAINER",
                       str(tmp_path / "keys" / "receiver-hmac-keys.bin"))
    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "announcements",
                               "announcement_service", "announcement_rooms")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    server_module.manager.receivers.clear()
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_store(client, headers, code="NA", region="NORTH"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": region})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def upload(client, headers, title="Diwali Offer"):
    response = client.post(
        "/api/announcements/audio", headers=headers,
        files={"file": ("d.mp3", io.BytesIO(b"ID3the-recording"), "audio/mpeg")},
        data={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def make_template(client, headers, audio_id, name="Festival", **fields):
    payload = {"name": name,
               "items": fields.pop("items", None)
                        or [{"audio_id": audio_id, "zone": "NORTH"}]}
    payload.update(fields)
    response = client.post("/api/announcements/templates", headers=headers,
                           json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def open_room(client, headers, template_id, **payload):
    response = client.post(f"/api/announcements/templates/{template_id}/room",
                           headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def join(client, code, password, name="Bz"):
    return client.post("/api/announce/join",
                       json={"id": code, "password": password, "name": name})


# ===========================================================================
# The link itself
# ===========================================================================

def test_a_link_carries_an_id_and_a_password_shown_once(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])

    opened = open_room(client, headers, template["id"])
    assert opened["room"]["public_code"].startswith("AN-")
    assert opened["password_shown_once"]
    assert opened["room"]["listen_path"].endswith(opened["room"]["public_code"])

    # And it is never readable again. A page that could show it would make
    # "who has this link" unanswerable.
    listed = client.get("/api/announcements/rooms", headers=headers).json()
    body = str(listed)
    assert opened["password_shown_once"] not in body
    assert "password_hash" not in body


def test_a_listener_joins_with_the_id_and_password(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])

    admitted = join(client, opened["room"]["public_code"],
                    opened["password_shown_once"])
    assert admitted.status_code == 200, admitted.text
    assert admitted.json()["token"]


def test_a_wrong_id_and_a_wrong_password_are_refused_identically(client):
    """Telling a stranger that an ID exists but the password is wrong tells
    them which half to keep guessing at."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])

    wrong_password = join(client, opened["room"]["public_code"], "NOPE-NOPE")
    wrong_id = join(client, "AN-XXXXXX", opened["password_shown_once"])

    assert wrong_password.status_code == wrong_id.status_code == 401
    assert wrong_password.json()["detail"] == wrong_id.json()["detail"]


def test_no_hq_account_is_needed_to_listen(client):
    """Whoever holds the link is not a user of this product and must never
    need to be."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])

    admitted = join(client, opened["room"]["public_code"],
                    opened["password_shown_once"])
    token = admitted.json()["token"]

    state = client.get("/api/announce/state",
                       headers={"Authorization": f"Bearer {token}"})
    assert state.status_code == 200, state.text
    assert state.json()["template_name"] == "Festival"


# ===========================================================================
# What the link follows
# ===========================================================================

def test_the_page_follows_whether_the_announcement_is_running(client):
    """A link that kept playing a campaign HQ had stopped is the one failure
    that would embarrass somebody in front of a customer."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])
    token = join(client, opened["room"]["public_code"],
                 opened["password_shown_once"]).json()["token"]
    listener = {"Authorization": f"Bearer {token}"}

    # Nothing is playing yet.
    assert client.get("/api/announce/state", headers=listener).json()["playing"] is False

    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    assert client.get("/api/announce/state", headers=listener).json()["playing"] is True

    client.post("/api/announcements/pause-all", headers=headers)
    paused = client.get("/api/announce/state", headers=listener).json()
    assert paused["playing"] is False
    assert "paused" in paused["reason"].lower()


def test_an_expired_template_says_so_rather_than_playing(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"],
                             expires_at="2020-01-01T00:00:00+00:00")
    opened = open_room(client, headers, template["id"])
    token = join(client, opened["room"]["public_code"],
                 opened["password_shown_once"]).json()["token"]

    state = client.get("/api/announce/state",
                       headers={"Authorization": f"Bearer {token}"}).json()
    assert state["playing"] is False
    assert "expired" in state["reason"]


# ===========================================================================
# Withdrawing it
# ===========================================================================

def test_closing_a_link_turns_away_everybody_already_using_it(client):
    """A closed room whose existing listeners kept playing would be a link
    that cannot actually be withdrawn - which is the only thing the button is
    for."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])
    token = join(client, opened["room"]["public_code"],
                 opened["password_shown_once"]).json()["token"]
    listener = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/announce/state", headers=listener).status_code == 200

    closed = client.post(f"/api/announcements/rooms/{opened['room']['id']}/close",
                         headers=headers)
    assert closed.status_code == 200, closed.text

    refused = client.get("/api/announce/state", headers=listener)
    assert refused.status_code == 401
    assert "no longer open" in refused.json()["detail"]


def test_a_closed_link_admits_nobody_new(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])
    client.post(f"/api/announcements/rooms/{opened['room']['id']}/close",
                headers=headers)

    assert join(client, opened["room"]["public_code"],
                opened["password_shown_once"]).status_code == 401


# ===========================================================================
# What a link must NOT open
# ===========================================================================

def test_a_room_token_cannot_fetch_another_campaigns_recording(client):
    """A link to one campaign that opened every campaign would be a link
    nobody could reason about."""
    headers = sign_in(client)
    ours = upload(client, headers, title="Ours")
    theirs = upload(client, headers, title="Theirs")
    template = make_template(client, headers, ours["id"])
    make_template(client, headers, theirs["id"], name="Other")
    opened = open_room(client, headers, template["id"])
    token = join(client, opened["room"]["public_code"],
                 opened["password_shown_once"]).json()["token"]

    allowed = client.get(f"/api/announce/audio/{ours['id']}?token={token}")
    assert allowed.status_code == 200
    assert allowed.content == b"ID3the-recording"

    refused = client.get(f"/api/announce/audio/{theirs['id']}?token={token}")
    assert refused.status_code == 404
    assert "not part of this announcement" in refused.json()["detail"]


def test_the_audio_needs_a_token_at_all(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    make_template(client, headers, audio["id"])
    assert client.get(f"/api/announce/audio/{audio['id']}").status_code == 401


def test_opening_a_link_is_its_own_right(client):
    """A link leaves the building: anybody holding it can hear the campaign
    without an account, from anywhere, until somebody closes it."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    client.post("/api/users", headers=headers, json={
        "username": "voice", "password": PASSWORD, "display_name": "voice",
        "role": "BROADCASTER"})
    broadcaster = sign_in(client, "voice")

    # A broadcaster may look at the announcements pages...
    assert client.get("/api/announcements/rooms",
                      headers=broadcaster).status_code == 200
    # ...and may not hand the campaign to the outside world.
    assert client.post(f"/api/announcements/templates/{template['id']}/room",
                       headers=broadcaster, json={}).status_code == 403
