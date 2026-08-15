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
    server_module.manager.receiver_snapshots.clear()
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_store(client, headers, code="NA", region="NORTH", connected=True):
    """A Store, and by default one with its Receiver connected.

    The listening page reports `playing` only for a shop HQ is actually
    connected to - it is the one place that claim is made to a member of the
    public, who hears the recording in their browser and would otherwise be
    told a silent estate is playing. So a test about what the page says has to
    say whether anybody is listening at the other end.
    """
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": region})
    assert response.status_code == 201, response.text
    store_id = response.json()["id"]
    if connected:
        from datetime import datetime, timezone
        from receiver_contract import ReceiverSnapshot, mark_connected

        class SilentSocket:
            async def send_text(self, _message):
                return None

            async def send_json(self, _message):
                return None

        manager = client.server_module.manager
        manager.receivers[store_id] = SilentSocket()
        manager.receiver_snapshots[store_id] = mark_connected(
            ReceiverSnapshot(), datetime.now(timezone.utc))
    return store_id


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
    """A listener presenting a code, a password and - required - a name."""
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


# ===========================================================================
# Credentials somebody chose, and links that carry their own password
# ===========================================================================

def test_hq_may_choose_the_id_and_the_password(client):
    """A code somebody picked is a code they can say down a phone."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])

    opened = open_room(client, headers, template["id"],
                       id="DIWALI", password="front-of-house")
    assert opened["room"]["public_code"] == "AN-DIWALI"
    assert join(client, "AN-DIWALI", "front-of-house").status_code == 200
    # And the one they chose is the one that works - not a generated one
    # quietly substituted.
    assert opened["password_shown_once"] == "front-of-house"


def test_a_chosen_id_already_in_use_is_refused_rather_than_stolen(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    first = make_template(client, headers, audio["id"])
    second = make_template(client, headers, audio["id"], name="Second")
    open_room(client, headers, first["id"], id="DIWALI")

    clash = client.post(f"/api/announcements/templates/{second['id']}/room",
                        headers=headers, json={"id": "DIWALI"})
    assert clash.status_code == 400
    assert "still open" in clash.json()["detail"].lower()


def test_a_link_can_be_opened_with_no_password_at_all(client):
    """Whoever holds the link is in. Chosen explicitly, because a link that
    forwards is a room that forwards."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])

    opened = open_room(client, headers, template["id"], no_password=True)
    assert opened["password_shown_once"] is None
    assert opened["room"]["requires_password"] is False
    assert join(client, opened["room"]["public_code"], "").status_code == 200
    # Anything typed is accepted too - there is nothing to be wrong about.
    assert join(client, opened["room"]["public_code"], "junk").status_code == 200


def test_the_share_link_carries_the_password_so_nobody_has_to_type_it(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])

    opened = open_room(client, headers, template["id"], password="say-this")
    assert opened["share_link"].startswith(opened["room"]["listen_path"])
    assert "k=say-this" in opened["share_link"]

    # A password-free link has nothing to carry.
    free = open_room(client, headers, template["id"], no_password=True)
    assert "k=" not in free["share_link"]


def test_hq_can_see_and_remove_the_people_on_a_link(client):
    """A link that leaves the building is only manageable if HQ can see who
    walked in on it."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])
    room_id = opened["room"]["id"]
    token = join(client, opened["room"]["public_code"],
                 opened["password_shown_once"], name="Ravi").json()["token"]

    listed = client.get(f"/api/announcements/rooms/{room_id}/listeners",
                        headers=headers)
    assert listed.status_code == 200, listed.text
    people = listed.json()["items"]
    assert [row["display_name"] for row in people] == ["Ravi"]

    removed = client.post(
        f"/api/announcements/rooms/{room_id}/listeners/{people[0]['id']}/remove",
        headers=headers)
    assert removed.status_code == 200, removed.text
    # And they are actually out, not merely off the list.
    assert client.get("/api/announce/state",
                      headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_a_closed_links_id_can_be_used_again(client):
    """A closed link is withdrawn, not reserved.

    Keeping AN-DIWALI forever made a chosen ID a one-use thing: closing a link
    and opening the replacement everybody had already been told about was
    impossible, which is the opposite of why somebody picks a memorable code.
    """
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])

    first = open_room(client, headers, template["id"], id="DIWALI")
    client.post(f"/api/announcements/rooms/{first['room']['id']}/close",
                headers=headers)

    second = open_room(client, headers, template["id"], id="DIWALI",
                       password="new-secret")
    assert second["room"]["public_code"] == "AN-DIWALI"
    assert second["room"]["id"] != first["room"]["id"]

    # The new link works, and the old one's listeners are still turned away -
    # reusing the ID must not resurrect a link somebody withdrew.
    assert join(client, "AN-DIWALI", "new-secret").status_code == 200
    assert join(client, "AN-DIWALI", first["password_shown_once"]).status_code == 401


def test_an_existing_database_is_rebuilt_without_losing_its_links(tmp_path):
    """The live estate's table was created with UNIQUE(public_code).

    SQLite cannot drop a column constraint, so the table is rebuilt - and a
    rebuild is exactly how enrolment broke once before, by silently losing the
    indexes the new table needed. This asserts both halves: the rows survive,
    and every index exists afterwards.
    """
    from sqlalchemy import create_engine, text
    import announcement_rooms

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE announcement_rooms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL,
                public_code VARCHAR(24) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                label VARCHAR(120) NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                created_by INTEGER,
                created_at VARCHAR(40) NOT NULL,
                closed_at VARCHAR(40),
                closed_by INTEGER,
                requires_password INTEGER NOT NULL DEFAULT 1
            )
            """)
        connection.exec_driver_sql(
            "INSERT INTO announcement_rooms (template_id, public_code, "
            "password_hash, label, status, created_at) VALUES "
            "(1, 'AN-OLD', 'hash', 'Diwali', 'CLOSED', '2026-01-01T00:00:00')")

    announcement_rooms.ensure_announcement_room_schema(engine)

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT public_code, label, status FROM announcement_rooms")).all()
        assert [tuple(row) for row in rows] == [("AN-OLD", "Diwali", "CLOSED")]

        names = {row[1] for row in connection.exec_driver_sql(
            "PRAGMA index_list(announcement_rooms)")}
        assert "ux_announcement_rooms_open_code" in names
        assert "ix_announcement_rooms_template" in names
        assert "ix_announcement_rooms_status" in names

    # And the withdrawn code is free: this is the whole point of the rebuild.
    room, _password = announcement_rooms.create_room(
        engine, template_id=1, label="Diwali again", created_by=1,
        hash_password=lambda value: f"hashed:{value}", code="OLD")
    assert room["public_code"] == "AN-OLD"

    # While two OPEN links still cannot share one code.
    with pytest.raises(announcement_rooms.RoomRefused):
        announcement_rooms.create_room(
            engine, template_id=1, label="clash", created_by=1,
            hash_password=lambda value: f"hashed:{value}", code="OLD")


def test_a_listener_has_to_say_who_they_are(client):
    """HQ can see who is on a link and can throw somebody off it. Both are
    useless against a page of anonymous rows - and a rule that lives only in
    the browser is one a curl command skips."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"])

    nameless = client.post("/api/announce/join", json={
        "id": opened["room"]["public_code"],
        "password": opened["password_shown_once"]})
    assert nameless.status_code == 400
    assert "your name" in nameless.json()["detail"].lower()


def test_guessing_at_a_listening_password_runs_out_of_attempts(client):
    """A public endpoint where a correct password IS the authorisation.

    The RBAC matrix lists this route among the deliberately unauthenticated
    ones and justifies it, in prose, with "each is rate limited" - and the
    code that would have made that sentence true had never been written. An
    operator may choose a memorable ID (AN-DIWALI, by design) and a short
    password, so without a budget both are guessable at leisure.
    """
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio["id"])
    opened = open_room(client, headers, template["id"], id="DIWALI",
                       password="front-of-house")

    refusals = [join(client, "AN-DIWALI", f"guess-{attempt}").status_code
                for attempt in range(12)]
    assert 429 in refusals, "guessing was never slowed down"
    assert refusals.index(429) <= 10

    # And a correct password is not punished for the guessers' attempts
    # forever: the limiter is per client key and clears on success, which the
    # broadcast room already does.
    assert refusals.count(401) >= 1
