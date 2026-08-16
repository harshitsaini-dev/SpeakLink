"""The announcement endpoints, driven over HTTP.

The state machine is tested on its own in test_announcement_state. What is
tested here is everything that can go wrong between it and a real estate:
which permission each route demands, which Stores a template actually reaches,
and whether the page shows a shop that has never played anything.
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
                               "announcement_service")]:
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


def make_store(client, headers, code, *, region="NORTH", city="DELHI",
               connected=True):
    """A Store, and by default one with its Receiver connected.

    HISTORY IS ONLY WRITTEN FOR A SHOP HQ IS CONNECTED TO - a play sent into a
    gap is not a run, and recording it put hours of imaginary "playing" into
    the record for shops that were silent. So a test about what history
    contains has to say that somebody was listening at the other end; pass
    connected=False for the tests that are about the opposite.
    """
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": city, "region": region})
    assert response.status_code == 201, response.text
    store_id = response.json()["id"]
    if connected:
        pretend_receiver_is_connected(client, store_id)
    return store_id


def pretend_receiver_is_connected(client, store_id):
    """Put this Store into the runtime registry as a connected Receiver.

    Reaches into the manager rather than opening a real socket: the sockets
    have their own tests, and what these tests need is the ONE fact the
    announcement code reads - whether HQ has this shop on the end of a
    connection right now.
    """
    from datetime import datetime, timezone
    from receiver_contract import ReceiverSnapshot, mark_connected

    class SilentSocket:
        """A socket that accepts everything and says nothing.

        It has to actually WORK: a stand-in that raised on send made the
        manager treat the Store as having dropped off, so the second play in
        a test was "unreachable" and wrote no history - which looked exactly
        like the feature being broken rather than the fixture being wrong.
        """

        async def send_text(self, _message):
            return None

        async def send_json(self, _message):
            return None

    manager = client.server_module.manager
    manager.receivers[store_id] = SilentSocket()
    manager.receiver_snapshots[store_id] = mark_connected(
        ReceiverSnapshot(), datetime.now(timezone.utc))


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD, "display_name": username,
        "role": role})
    assert response.status_code in (200, 201), response.text
    return response.json()


def upload(client, headers, title="Diwali Offer", data=b"ID3fake-audio"):
    response = client.post(
        "/api/announcements/audio", headers=headers,
        files={"file": ("diwali.mp3", io.BytesIO(data), "audio/mpeg")},
        data={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def make_template(client, headers, *, audio_id, name="Festival", **fields):
    payload = {"name": name, "items": fields.pop("items", None)
               or [{"audio_id": audio_id, "zone": "NORTH"}]}
    payload.update(fields)
    response = client.post("/api/announcements/templates", headers=headers,
                           json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# ===========================================================================
# Uploading
# ===========================================================================

def test_a_recording_can_be_uploaded_and_listed(client):
    headers = sign_in(client)
    created = upload(client, headers)
    assert created["title"] == "Diwali Offer"

    listed = client.get("/api/announcements/audio", headers=headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["items"]] == [created["id"]]


def test_the_uploaded_filename_is_not_the_name_on_disk(client):
    """The one part of an upload a stranger chooses must decide nothing."""
    headers = sign_in(client)
    created = upload(client, headers)
    assert created["original_filename"] == "diwali.mp3"
    assert created["storage_name"] != "diwali.mp3"
    assert created["storage_name"].endswith(".mp3")


def test_a_format_the_receiver_cannot_play_is_refused(client):
    headers = sign_in(client)
    response = client.post(
        "/api/announcements/audio", headers=headers,
        files={"file": ("offer.pdf", io.BytesIO(b"%PDF"), "application/pdf")})
    assert response.status_code == 400
    assert "mp3" in response.json()["detail"]


def test_archiving_a_recording_keeps_it_findable(client):
    """A template may still name it and the history must stay readable."""
    headers = sign_in(client)
    created = upload(client, headers)
    assert client.delete(f"/api/announcements/audio/{created['id']}",
                         headers=headers).status_code == 200

    active = client.get("/api/announcements/audio", headers=headers).json()
    assert active["items"] == []
    everything = client.get("/api/announcements/audio?status=all",
                            headers=headers).json()
    assert [row["id"] for row in everything["items"]] == [created["id"]]


# ===========================================================================
# Templates and what they reach
# ===========================================================================

def test_a_template_reaches_every_store_in_its_zone(client):
    headers = sign_in(client)
    north_a = make_store(client, headers, "NA", region="NORTH")
    north_b = make_store(client, headers, "NB", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    played = client.post(
        f"/api/announcements/templates/{template['id']}/play", headers=headers)
    assert played.status_code == 200, played.text
    assert sorted(played.json()["started"]) == sorted([north_a, north_b])


def test_a_store_named_twice_plays_once(client):
    """"Everything in the North, plus the flagship" is a real plan, and the
    flagship must not appear twice or play twice."""
    headers = sign_in(client)
    flagship = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"], items=[
        {"audio_id": audio["id"], "zone": "NORTH"},
        {"audio_id": audio["id"], "store_id": flagship},
    ])

    played = client.post(
        f"/api/announcements/templates/{template['id']}/play", headers=headers)
    assert played.json()["started"] == [flagship]


def test_a_template_line_naming_both_a_store_and_a_zone_is_refused(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    audio = upload(client, headers)
    response = client.post("/api/announcements/templates", headers=headers, json={
        "name": "Confused",
        "items": [{"audio_id": audio["id"], "store_id": store_id,
                   "zone": "NORTH"}]})
    assert response.status_code == 400
    assert "one Store or one zone" in response.json()["detail"]


def test_a_template_with_no_lines_is_refused_in_words(client):
    headers = sign_in(client)
    response = client.post("/api/announcements/templates", headers=headers,
                           json={"name": "Empty", "items": []})
    assert response.status_code == 400
    assert "plays nothing" in response.json()["detail"]


def test_an_expired_template_will_not_play_and_says_so(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             expires_at="2020-01-01T00:00:00+00:00")

    played = client.post(
        f"/api/announcements/templates/{template['id']}/play", headers=headers)
    assert played.status_code == 400
    assert "expired" in played.json()["detail"]


def test_a_template_that_has_not_started_yet_says_when_it_will(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             starts_at="2999-01-01T00:00:00+00:00")

    played = client.post(
        f"/api/announcements/templates/{template['id']}/play", headers=headers)
    assert played.status_code == 400
    assert "scheduled" in played.json()["detail"]


def test_a_template_whose_zone_is_now_empty_says_so(client):
    headers = sign_in(client)
    make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    played = client.post(
        f"/api/announcements/templates/{template['id']}/play", headers=headers)
    assert played.status_code == 400
    assert "no active Store" in played.json()["detail"]


# ===========================================================================
# Status, search, filter, pagination
# ===========================================================================

def test_a_store_that_never_played_anything_still_appears(client):
    """Listing only Stores with a playback row would hide exactly the shops
    somebody is looking for when they ask why a campaign is not everywhere."""
    headers = sign_in(client)
    make_store(client, headers, "NA")
    # Narrowed to this test's own Store: HQ is seeded with a real estate, and
    # asserting a total here would be asserting the size of the seed.
    status = client.get("/api/announcements/status?q=NA", headers=headers)
    assert status.status_code == 200
    rows = [row for row in status.json()["items"] if row["store_code"] == "NA"]
    assert len(rows) == 1, "a Store that has never played anything vanished"
    assert rows[0]["state"] == "STOPPED"


def test_status_can_be_searched_filtered_and_paged(client):
    headers = sign_in(client)
    for index in range(5):
        make_store(client, headers, f"N{index}", region="NORTH")
    make_store(client, headers, "S0", region="SOUTH")

    # NORTH and SOUTH are this test's own zones; the seeded estate uses names
    # of its own, so filtering by them isolates these six Stores.
    zoned = client.get("/api/announcements/status?zone=NORTH",
                       headers=headers).json()
    assert zoned["total"] == 5
    assert sorted(row["store_code"] for row in zoned["items"]) == [
        "N0", "N1", "N2", "N3", "N4"]

    southern = client.get("/api/announcements/status?zone=SOUTH",
                          headers=headers).json()
    assert [row["store_code"] for row in southern["items"]] == ["S0"]

    searched = client.get("/api/announcements/status?q=N3", headers=headers).json()
    assert [row["store_code"] for row in searched["items"]] == ["N3"]

    paged = client.get("/api/announcements/status?zone=NORTH&page=2&page_size=2",
                       headers=headers).json()
    assert len(paged["items"]) == 2
    assert paged["total"] == 5


def test_templates_can_be_searched_and_filtered_by_zone(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"], name="North Festival")
    make_template(client, headers, audio_id=audio["id"], name="South Sale",
                  items=[{"audio_id": audio["id"], "zone": "SOUTH"}])

    searched = client.get("/api/announcements/templates?q=festival",
                          headers=headers).json()
    assert [row["name"] for row in searched["items"]] == ["North Festival"]

    zoned = client.get("/api/announcements/templates?zone=SOUTH",
                       headers=headers).json()
    assert [row["name"] for row in zoned["items"]] == ["South Sale"]


# ===========================================================================
# Play, pause and volume
# ===========================================================================

def test_pausing_and_resuming_one_store(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    paused = client.post(f"/api/announcements/stores/{store_id}/pause",
                         headers=headers)
    assert paused.json()["state"] == "PAUSED"

    resumed = client.post(f"/api/announcements/stores/{store_id}/play",
                          headers=headers)
    assert resumed.json()["state"] == "PLAYING"


def test_resuming_a_store_with_nothing_chosen_says_what_to_do(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    response = client.post(f"/api/announcements/stores/{store_id}/play",
                           headers=headers)
    assert response.status_code == 400
    assert "Start a template first" in response.json()["detail"]


def test_volume_does_not_start_or_stop_anything(client):
    """Turning a jingle down must not start it, and must not stop it."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    client.post(f"/api/announcements/stores/{store_id}/pause", headers=headers)

    response = client.post(f"/api/announcements/stores/{store_id}/volume",
                           headers=headers, json={"volume_percent": 35})
    assert response.status_code == 200
    assert response.json()["volume_percent"] == 35
    assert response.json()["state"] == "PAUSED", (
        "changing the volume moved the Store out of the state it was in")


def test_a_volume_outside_the_range_is_refused_with_the_number(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    response = client.post(f"/api/announcements/stores/{store_id}/volume",
                           headers=headers, json={"volume_percent": 250})
    assert response.status_code == 400
    assert "250" in response.json()["detail"]


def test_pause_all_and_play_all(client):
    headers = sign_in(client)
    for index in range(3):
        make_store(client, headers, f"N{index}", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    paused = client.post("/api/announcements/pause-all", headers=headers)
    assert paused.json()["count"] == 3

    started = client.post("/api/announcements/play-all", headers=headers)
    assert started.json()["count"] == 3


def test_play_all_skips_a_store_with_nothing_chosen_rather_than_failing(client):
    """An estate always has a shop that was added yesterday, and one of those
    must not stop the other two hundred from resuming."""
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    client.post("/api/announcements/pause-all", headers=headers)

    started = client.post("/api/announcements/play-all", headers=headers)
    # Exactly the one Store the template reaches resumes. Every other Store on
    # this HQ - the seeded estate and the SOUTH one - has nothing chosen, and
    # is skipped rather than failing the call for the rest.
    assert started.json()["started"] == [north]
    assert south in started.json()["skipped"]


def test_one_user_can_run_several_templates_in_several_places(client):
    """Requirement 6, stated as a test: nothing here is owned by a session or
    a person, so no second account is needed to run a second campaign."""
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    northern = make_template(client, headers, audio_id=audio["id"], name="North")
    southern = make_template(client, headers, audio_id=audio["id"], name="South",
                             items=[{"audio_id": audio["id"], "zone": "SOUTH"}])

    assert client.post(f"/api/announcements/templates/{northern['id']}/play",
                       headers=headers).json()["started"] == [north]
    assert client.post(f"/api/announcements/templates/{southern['id']}/play",
                       headers=headers).json()["started"] == [south]

    rows = {row["store_id"]: row for row in
            client.get("/api/announcements/status", headers=headers).json()["items"]}
    assert rows[north]["template_name"] == "North"
    assert rows[south]["template_name"] == "South"


# ===========================================================================
# Authorization - every route, every role
# ===========================================================================

def test_a_viewer_may_look_but_not_touch(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    make_user(client, headers, "watcher", "VIEWER")
    viewer = sign_in(client, "watcher")

    assert client.get("/api/announcements/status",
                      headers=viewer).status_code == 200
    for method, path, body in (
        ("post", f"/api/announcements/stores/{store_id}/pause", None),
        ("post", f"/api/announcements/stores/{store_id}/play", None),
        ("post", "/api/announcements/pause-all", None),
        ("post", "/api/announcements/play-all", None),
        ("post", f"/api/announcements/stores/{store_id}/volume",
         {"volume_percent": 50}),
        ("post", "/api/announcements/templates", {"name": "x", "items": []}),
    ):
        response = getattr(client, method)(path, headers=viewer, json=body)
        assert response.status_code == 403, f"{path} let a VIEWER through"


def test_a_broadcaster_may_control_but_not_decide_what_exists(client):
    """A broadcaster interrupts announcements by definition, so settling them
    is part of the same job. Deciding what plays for the next fortnight is
    not."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"])
    make_user(client, headers, "voice", "BROADCASTER")
    broadcaster = sign_in(client, "voice")

    assert client.get("/api/announcements/status",
                      headers=broadcaster).status_code == 200
    assert client.post(f"/api/announcements/stores/{store_id}/pause",
                       headers=broadcaster).status_code == 200
    assert client.post(f"/api/announcements/stores/{store_id}/volume",
                       headers=broadcaster,
                       json={"volume_percent": 40}).status_code == 200

    # But not what exists, and not the whole estate at once.
    assert client.post("/api/announcements/templates", headers=broadcaster,
                       json={"name": "x", "items": []}).status_code == 403
    assert client.post("/api/announcements/pause-all",
                       headers=broadcaster).status_code == 403
    assert client.post("/api/announcements/play-all",
                       headers=broadcaster).status_code == 403
    assert client.post(
        "/api/announcements/audio", headers=broadcaster,
        files={"file": ("x.mp3", io.BytesIO(b"ID3"), "audio/mpeg")}
    ).status_code == 403


def test_an_unauthenticated_caller_reaches_nothing(client):
    for path in ("/api/announcements/status", "/api/announcements/audio",
                 "/api/announcements/templates"):
        assert client.get(path).status_code in (401, 403), path


# ===========================================================================
# Archiving and deleting are two different things
#
# They used to be one, and the button that did it wore a wastebin. Somebody
# archived a recording, watched it vanish from the list, and found the bytes
# still on the server: the interface said "deleted" and meant "hidden".
# ===========================================================================

def test_archiving_leaves_the_file_on_disk_and_the_row_findable(client):
    import announcements
    headers = sign_in(client)
    created = upload(client, headers)
    path = announcements.audio_directory() / created["storage_name"]
    assert path.is_file()

    client.delete(f"/api/announcements/audio/{created['id']}", headers=headers)
    assert path.is_file(), "archiving removed the file"
    everything = client.get("/api/announcements/audio?status=all",
                            headers=headers).json()
    assert [row["id"] for row in everything["items"]] == [created["id"]]


def test_deleting_permanently_removes_the_row_and_the_file(client):
    import announcements
    headers = sign_in(client)
    created = upload(client, headers)
    path = announcements.audio_directory() / created["storage_name"]

    response = client.post(
        f"/api/announcements/audio/{created['id']}/delete-permanently",
        headers=headers, json={"confirmation": "DELETE"})
    assert response.status_code == 200, response.text
    assert not path.exists(), "the file was left behind"
    everything = client.get("/api/announcements/audio?status=all",
                            headers=headers).json()
    assert everything["items"] == []


def test_deleting_without_the_confirmation_word_changes_nothing(client):
    import announcements
    headers = sign_in(client)
    created = upload(client, headers)
    path = announcements.audio_directory() / created["storage_name"]

    for wrong in ("", "delete this", "yes"):
        response = client.post(
            f"/api/announcements/audio/{created['id']}/delete-permanently",
            headers=headers, json={"confirmation": wrong})
        assert response.status_code == 400
    assert path.is_file()


def test_a_recording_a_template_still_uses_cannot_be_deleted(client):
    """Deleting the audio out from under a live campaign leaves a template
    that plays nothing and cannot say why."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"], name="Festival")

    response = client.post(
        f"/api/announcements/audio/{audio['id']}/delete-permanently",
        headers=headers, json={"confirmation": "DELETE"})
    assert response.status_code == 409
    assert "Festival" in response.json()["detail"]
    assert "archive it instead" in response.json()["detail"].lower()


def test_permanent_deletion_is_not_granted_by_the_upload_right(client):
    """Uploading is an everyday action; destroying an estate's recording is
    not, and holding the first must not imply the second."""
    headers = sign_in(client)
    audio = upload(client, headers)
    make_user(client, headers, "editor", "ADMIN")
    admin = sign_in(client, "editor")

    # ADMIN may archive...
    assert client.delete(f"/api/announcements/audio/{audio['id']}",
                         headers=admin).status_code == 200
    # ...but permanent deletion is withheld from ADMIN by default, like every
    # other *.delete_permanently code.
    assert client.post(
        f"/api/announcements/audio/{audio['id']}/delete-permanently",
        headers=admin, json={"confirmation": "DELETE"}).status_code == 403


# ===========================================================================
# History: what played, where, and why it stopped
# ===========================================================================

def test_playing_and_pausing_writes_a_history_row_with_the_reason(client):
    """"It went quiet at 4pm" is only answerable if the reason was recorded at
    the time. Paused by a person and ducked by a broadcast look identical
    afterwards and are not the same event."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    client.post(f"/api/announcements/stores/{store_id}/pause", headers=headers)

    history = client.get("/api/announcements/history", headers=headers).json()
    rows = [row for row in history["items"] if row["store_id"] == store_id]
    assert len(rows) == 1
    assert rows[0]["template_name"] == "Festival"
    assert rows[0]["audio_title"] == "Diwali Offer"
    assert rows[0]["ended_reason"] == "paused"
    assert rows[0]["ended_at"]


def test_a_history_row_stays_readable_after_the_recording_is_deleted(client):
    """A JOIN to a row that no longer exists renders "unknown" for something
    that was perfectly well known at the time."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    client.post(f"/api/announcements/stores/{store_id}/pause", headers=headers)

    client.delete(f"/api/announcements/templates/{template['id']}", headers=headers)
    client.post(f"/api/announcements/audio/{audio['id']}/delete-permanently",
                headers=headers, json={"confirmation": "DELETE"})

    history = client.get("/api/announcements/history", headers=headers).json()
    rows = [row for row in history["items"] if row["store_id"] == store_id]
    assert rows[0]["audio_title"] == "Diwali Offer"
    assert rows[0]["template_name"] == "Festival"


def test_history_can_be_searched_filtered_and_paged(client):
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    zoned = client.get("/api/announcements/history?zone=NORTH",
                       headers=headers).json()
    assert [row["store_id"] for row in zoned["items"]] == [north]

    searched = client.get("/api/announcements/history?q=diwali",
                          headers=headers).json()
    assert searched["total"] == 1

    # "Still playing" is the ABSENCE of an end, not a reason - spelled out
    # rather than left to somebody discovering that an empty filter differs.
    still = client.get("/api/announcements/history?reason=open",
                       headers=headers).json()
    assert still["total"] == 1
    assert still["items"][0]["ended_at"] is None

    paged = client.get("/api/announcements/history?page=2&page_size=1",
                       headers=headers).json()
    assert paged["items"] == []


def test_restarting_a_store_does_not_leave_two_open_history_rows(client):
    """Two open rows for one Store make every later "what was playing" answer
    ambiguous."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    for _ in range(3):
        client.post(f"/api/announcements/templates/{template['id']}/play",
                    headers=headers)
        client.post(f"/api/announcements/stores/{store_id}/pause", headers=headers)
        client.post(f"/api/announcements/stores/{store_id}/play", headers=headers)

    history = client.get("/api/announcements/history?page_size=100",
                         headers=headers).json()
    open_rows = [row for row in history["items"]
                 if row["store_id"] == store_id and row["ended_at"] is None]
    assert len(open_rows) == 1


def test_archiving_a_history_entry_keeps_it_in_the_record(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    entry = client.get("/api/announcements/history", headers=headers).json()["items"][0]

    assert client.post(f"/api/announcements/history/{entry['id']}/archive",
                       headers=headers).status_code == 200
    assert client.get("/api/announcements/history",
                      headers=headers).json()["total"] == 0
    assert client.get("/api/announcements/history?include_archived=true",
                      headers=headers).json()["total"] == 1


def test_deleting_a_history_entry_needs_the_word_and_the_right(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    entry = client.get("/api/announcements/history", headers=headers).json()["items"][0]

    assert client.post(f"/api/announcements/history/{entry['id']}/delete-permanently",
                       headers=headers, json={"confirmation": "yes"}
                       ).status_code == 400

    make_user(client, headers, "editor", "ADMIN")
    assert client.post(f"/api/announcements/history/{entry['id']}/delete-permanently",
                       headers=sign_in(client, "editor"),
                       json={"confirmation": "DELETE"}).status_code == 403

    assert client.post(f"/api/announcements/history/{entry['id']}/delete-permanently",
                       headers=headers, json={"confirmation": "DELETE"}
                       ).status_code == 200
    assert client.get("/api/announcements/history?include_archived=true",
                      headers=headers).json()["total"] == 0


def test_a_template_can_be_deleted_permanently(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    response = client.post(
        f"/api/announcements/templates/{template['id']}/delete-permanently",
        headers=headers, json={"confirmation": "DELETE"})
    assert response.status_code == 200, response.text
    assert client.get("/api/announcements/templates?status=all",
                      headers=headers).json()["items"] == []


def test_deleting_a_template_stops_the_shops_playing_it_and_says_so(client):
    """Deleting the plan while shops run it would leave them playing something
    with no name - and no row in the list to press Pause on."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    response = client.post(
        f"/api/announcements/templates/{template['id']}/delete-permanently",
        headers=headers, json={"confirmation": "DELETE"})
    assert response.status_code == 200, response.text
    assert response.json()["stopped_stores"] == [store_id]
    assert "have been stopped" in response.json()["note"]

    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "STOPPED"


def test_deleting_a_template_leaves_the_history_readable(client):
    """History carries its own copy of the name, so what a shop played last
    week survives the plan being deleted."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    client.post(f"/api/announcements/templates/{template['id']}/delete-permanently",
                headers=headers, json={"confirmation": "DELETE"})

    history = client.get("/api/announcements/history", headers=headers).json()
    assert history["items"][0]["template_name"] == "Festival"


def test_deleting_a_template_needs_the_word_and_the_right(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    assert client.post(
        f"/api/announcements/templates/{template['id']}/delete-permanently",
        headers=headers, json={"confirmation": "sure"}).status_code == 400

    make_user(client, headers, "editor", "ADMIN")
    assert client.post(
        f"/api/announcements/templates/{template['id']}/delete-permanently",
        headers=sign_in(client, "editor"),
        json={"confirmation": "DELETE"}).status_code == 403


# ===========================================================================
# Bulk selection
#
# Deleting one row at a time is not a safety feature when there are two
# hundred of them - it is a person clicking through the same dialog until
# they stop reading it.
# ===========================================================================

def played_rows(client, headers, count=3):
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    for _ in range(count):
        client.post(f"/api/announcements/templates/{template['id']}/play",
                    headers=headers)
        client.post("/api/announcements/pause-all", headers=headers)
    return client.get("/api/announcements/history?page_size=100",
                      headers=headers).json()["items"]


def test_selected_rows_can_be_archived_together(client):
    headers = sign_in(client)
    rows = played_rows(client, headers)
    ids = [row["id"] for row in rows[:2]]

    response = client.post("/api/announcements/history/archive", headers=headers,
                           json={"mode": "ids", "ids": ids})
    assert response.status_code == 200, response.text
    assert response.json()["affected"] == 2
    assert client.get("/api/announcements/history?page_size=100",
                      headers=headers).json()["total"] == len(rows) - 2


def test_select_all_filtered_means_every_match_not_just_the_page(client):
    """The browser only ever holds one page, so a selection built there could
    act on fifty rows while claiming to act on all of them."""
    headers = sign_in(client)
    played_rows(client, headers, count=5)

    response = client.post("/api/announcements/history/archive", headers=headers,
                           json={"mode": "filtered", "filters": {"zone": "NORTH"}})
    assert response.status_code == 200, response.text
    assert response.json()["affected"] == 5
    assert client.get("/api/announcements/history",
                      headers=headers).json()["total"] == 0


def test_a_filtered_bulk_action_touches_only_what_the_filter_matched(client):
    headers = sign_in(client)
    played_rows(client, headers, count=2)
    make_store(client, headers, "SA", region="SOUTH")
    audio = client.get("/api/announcements/audio", headers=headers).json()["items"][0]
    southern = make_template(client, headers, audio_id=audio["id"], name="South",
                             items=[{"audio_id": audio["id"], "zone": "SOUTH"}])
    client.post(f"/api/announcements/templates/{southern['id']}/play",
                headers=headers)

    client.post("/api/announcements/history/archive", headers=headers,
                json={"mode": "filtered", "filters": {"zone": "NORTH"}})
    remaining = client.get("/api/announcements/history", headers=headers).json()
    assert [row["zone"] for row in remaining["items"]] == ["SOUTH"]


def test_bulk_deletion_asks_for_the_word_once_for_the_whole_selection(client):
    """Asking two hundred times is not two hundred times the protection."""
    headers = sign_in(client)
    rows = played_rows(client, headers)
    ids = [row["id"] for row in rows]

    refused = client.post("/api/announcements/history/delete", headers=headers,
                          json={"mode": "ids", "ids": ids})
    assert refused.status_code == 400
    assert client.get("/api/announcements/history?page_size=100",
                      headers=headers).json()["total"] == len(rows)

    done = client.post("/api/announcements/history/delete", headers=headers,
                       json={"mode": "ids", "ids": ids, "confirm": "DELETE"})
    assert done.status_code == 200, done.text
    assert done.json()["affected"] == len(rows)


def test_bulk_deletion_is_not_granted_by_the_tidy_up_right(client):
    headers = sign_in(client)
    rows = played_rows(client, headers)
    make_user(client, headers, "editor", "ADMIN")
    admin = sign_in(client, "editor")

    assert client.post("/api/announcements/history/archive", headers=admin,
                       json={"mode": "ids", "ids": [rows[0]["id"]]}
                       ).status_code == 200
    assert client.post("/api/announcements/history/delete", headers=admin,
                       json={"mode": "ids", "ids": [rows[0]["id"]],
                             "confirm": "DELETE"}).status_code == 403


def test_history_names_the_account_rather_than_saying_a_person(client):
    """"Paused by a person" was true and useless: the whole reason the column
    exists is to answer "who did that"."""
    headers = sign_in(client)
    rows = played_rows(client, headers, count=1)
    assert rows[0]["started_by_username"] == "founder"
    assert rows[0]["ended_by_username"] == "founder"


def test_archiving_a_template_stops_the_shops_running_it(client):
    """A template was archived, vanished from the list, and a Store went on
    reporting it as the thing it was playing - with no row left to press Pause
    on. Archiving a plan and leaving shops running it is not a smaller version
    of withdrawing it."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    archived = client.delete(f"/api/announcements/templates/{template['id']}",
                             headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["stopped_stores"] == [store_id]

    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "STOPPED"
    assert row["template_name"] is None, (
        "the Store still names a template nobody can open")
    assert row["audio_title"] is None


def test_recordings_can_be_archived_and_deleted_in_bulk(client):
    headers = sign_in(client)
    first = upload(client, headers, title="One")
    second = upload(client, headers, title="Two")

    archived = client.post("/api/announcements/audio/archive", headers=headers,
                           json={"mode": "ids", "ids": [first["id"]]})
    assert archived.json()["affected"] == 1

    deleted = client.post("/api/announcements/audio/delete", headers=headers,
                          json={"mode": "ids", "ids": [second["id"]],
                                "confirm": "DELETE"})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["affected"] == 1


def test_a_bulk_recording_delete_skips_the_ones_in_use_and_names_them(client):
    """Failing everything because one of forty is in use would leave the
    operator selecting the other thirty-nine by hand."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    used = upload(client, headers, title="Used")
    spare = upload(client, headers, title="Spare")
    make_template(client, headers, audio_id=used["id"], name="Festival")

    response = client.post("/api/announcements/audio/delete", headers=headers,
                           json={"mode": "ids", "ids": [used["id"], spare["id"]],
                                 "confirm": "DELETE"})
    assert response.status_code == 200, response.text
    assert response.json()["affected"] == 1
    assert response.json()["skipped"] == [{"id": used["id"], "used_by": "Festival"}]
    assert "still uses them" in response.json()["note"]


def test_templates_can_be_deleted_in_bulk_and_the_shops_stop(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    first = make_template(client, headers, audio_id=audio["id"], name="One")
    second = make_template(client, headers, audio_id=audio["id"], name="Two")
    client.post(f"/api/announcements/templates/{first['id']}/play", headers=headers)

    response = client.post("/api/announcements/templates/delete", headers=headers,
                           json={"mode": "ids",
                                 "ids": [first["id"], second["id"]],
                                 "confirm": "DELETE"})
    assert response.status_code == 200, response.text
    assert response.json()["affected"] == 2
    assert response.json()["stopped_stores"] == [store_id]
    assert client.get("/api/announcements/templates?status=all",
                      headers=headers).json()["items"] == []


def test_a_store_left_pointing_at_an_archived_template_is_reconciled(client):
    """Archiving used to leave the shops running it exactly where they were,
    and one estate carries the result: a Store still reporting a template that
    cannot be opened, with no row in the list to press Pause on. Fixing the
    archive path does nothing for the rows already like that."""
    import announcement_service
    engine = client.server_module.engine

    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    # Exactly what the old archive path left behind: template archived, Store
    # still PLAYING and still pointing at it.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE announcement_templates SET status = 'archived' WHERE id = "
            + str(template["id"]))

    assert announcement_service.reconcile_playback(engine) == [store_id]

    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "STOPPED"
    assert row["template_name"] is None


def test_reconciliation_leaves_a_healthy_store_alone(client):
    import announcement_service
    engine = client.server_module.engine

    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    assert announcement_service.reconcile_playback(engine) == []
    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "PLAYING"


def test_reconciliation_is_safe_to_run_twice(client):
    """It runs on every boot."""
    import announcement_service
    engine = client.server_module.engine
    assert announcement_service.reconcile_playback(engine) == []
    assert announcement_service.reconcile_playback(engine) == []


# ===========================================================================
# An announcement belongs to the estate, not to whoever pressed play
#
# Two properties, and they are the same property seen from two sides: nothing
# about a playing announcement depends on the person who started it still
# being there.
# ===========================================================================

def test_a_colleague_with_the_right_can_pause_what_somebody_else_started(client):
    """A jingle annoying customers at 4pm must be stoppable by whoever is at a
    desk, not only by the person who happened to start it that morning."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    make_user(client, headers, "colleague", "BROADCASTER")
    colleague = sign_in(client, "colleague")

    paused = client.post(f"/api/announcements/stores/{store_id}/pause",
                         headers=colleague)
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "PAUSED"

    # And can start it again.
    resumed = client.post(f"/api/announcements/stores/{store_id}/play",
                          headers=colleague)
    assert resumed.json()["state"] == "PLAYING"


def test_an_announcement_keeps_playing_after_the_starter_signs_out(client):
    """The state lives in the estate's database and the Store's own player.
    Nothing about it is attached to a session, so signing out - or a laptop
    closing, or a token expiring - changes nothing in the shop."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    make_user(client, headers, "starter", "BROADCASTER")
    starter = sign_in(client, "starter")
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=starter)

    logout = client.post("/api/auth/logout", headers=starter)
    assert logout.status_code in (200, 204, 404), logout.text

    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "PLAYING", (
        "an announcement stopped because the person who started it signed out")


def test_a_disabled_account_does_not_silence_what_it_started(client):
    """Stronger than signing out: the account is gone entirely and the shop
    carries on, because the shop was never depending on it."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    starter = make_user(client, headers, "temp", "BROADCASTER")
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=sign_in(client, "temp"))

    disabled = client.post(f"/api/users/{starter['id']}/disable", headers=headers)
    assert disabled.status_code in (200, 204), disabled.text

    rows = client.get("/api/announcements/status?q=NA", headers=headers).json()
    row = next(r for r in rows["items"] if r["store_id"] == store_id)
    assert row["state"] == "PLAYING"


# ===========================================================================
# A filter may name more than one value
#
# A dropdown that admits one Store answers "how is Nehru Place doing". It
# cannot answer "how are these six shops doing", which is the question people
# actually bring - a zone with an exception in it, a handful in one market,
# the three that were complaining this morning. Running the search six times
# and comparing six screens is arithmetic done by the reader.
# ===========================================================================

def test_the_status_page_can_be_filtered_to_several_zones(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")
    make_store(client, headers, "EA", region="EAST")

    both = client.get("/api/announcements/status?zone=NORTH,SOUTH",
                      headers=headers).json()
    assert sorted(row["store_code"] for row in both["items"]) == ["NA", "SA"]

    # One value still means exactly what it always meant.
    one = client.get("/api/announcements/status?zone=NORTH", headers=headers).json()
    assert [row["store_code"] for row in one["items"]] == ["NA"]


def test_the_status_page_can_be_filtered_to_several_stores(client):
    headers = sign_in(client)
    first = make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "NB", region="NORTH")
    third = make_store(client, headers, "NC", region="NORTH")

    chosen = client.get(f"/api/announcements/status?store_id={first},{third}",
                        headers=headers).json()
    assert sorted(row["store_code"] for row in chosen["items"]) == ["NA", "NC"]


def test_templates_can_be_filtered_by_store_and_by_several_of_them(client):
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    other = make_store(client, headers, "EA", region="EAST")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"], name="North one",
                  items=[{"audio_id": audio["id"], "store_id": north}])
    make_template(client, headers, audio_id=audio["id"], name="South one",
                  items=[{"audio_id": audio["id"], "store_id": south}])
    make_template(client, headers, audio_id=audio["id"], name="Elsewhere",
                  items=[{"audio_id": audio["id"], "store_id": other}])

    one = client.get(f"/api/announcements/templates?store_id={north}",
                     headers=headers).json()
    assert [row["name"] for row in one["items"]] == ["North one"]

    several = client.get(f"/api/announcements/templates?store_id={north},{south}",
                         headers=headers).json()
    assert sorted(row["name"] for row in several["items"]) == ["North one", "South one"]


def test_a_template_matches_if_any_of_its_lines_matches(client):
    """"Show me the templates touching these six shops" is the question.
    Requiring every line to match would answer a different one nobody asks."""
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"], name="Both",
                  items=[{"audio_id": audio["id"], "store_id": north},
                         {"audio_id": audio["id"], "store_id": south}])

    found = client.get(f"/api/announcements/templates?store_id={north}",
                       headers=headers).json()
    assert [row["name"] for row in found["items"]] == ["Both"]


def test_history_accepts_several_reasons_and_several_zones(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    client.post("/api/announcements/pause-all", headers=headers)
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    # "open" and "paused" together: still playing OR paused by a person.
    both = client.get("/api/announcements/history?reason=open,paused",
                      headers=headers).json()
    assert both["total"] == 2

    zoned = client.get("/api/announcements/history?zone=NORTH,SOUTH",
                       headers=headers).json()
    assert zoned["total"] == 2


def test_an_empty_filter_still_means_everything(client):
    """The alternative is that clearing a filter hides every row, which reads
    as the page being broken."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    everything = client.get("/api/announcements/status?zone=&state=&store_id=",
                            headers=headers).json()
    assert everything["total"] >= 1


# ===========================================================================
# Stop, which is not pause
# ===========================================================================

def test_stop_lets_go_of_what_was_chosen(client):
    """Pause keeps the choice so it can carry on; stop discards it.

    A stop that kept the campaign would leave a finished promotion one
    accidental click from coming back on in front of customers.
    """
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    stopped = client.post(f"/api/announcements/stores/{store}/stop",
                          headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "STOPPED"

    # The ASSIGNMENT survives. Which template a shop belongs to was decided on
    # the Templates page, and a transport button does not undo it - the first
    # version cleared it, and the console then said "nothing chosen" for shops
    # that were still part of a live campaign.
    assert stopped.json()["template_id"] == template["id"]

    # And Play starts it again rather than refusing.
    resumed = client.post(f"/api/announcements/stores/{store}/play",
                          headers=headers)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "PLAYING"


def test_pause_keeps_the_choice_so_play_can_carry_on(client):
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    paused = client.post(f"/api/announcements/stores/{store}/pause",
                         headers=headers).json()
    assert paused["state"] == "PAUSED"
    assert paused["template_id"] == template["id"]
    assert client.post(f"/api/announcements/stores/{store}/play",
                       headers=headers).json()["state"] == "PLAYING"


def test_stop_all_clears_every_shop(client):
    headers = sign_in(client)
    first = make_store(client, headers, "NA", region="NORTH")
    second = make_store(client, headers, "NB", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    stopped = client.post("/api/announcements/stop-all", headers=headers)
    assert stopped.status_code == 200, stopped.text
    assert set(stopped.json()["stopped"]) >= {first, second}

    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert all(row["state"] == "STOPPED" for row in rows)
    # Silent everywhere, and every shop the campaign reached still holds it.
    # Shops it never reached keep their own nothing - stop-all is not a way of
    # assigning a template to the estate.
    reached = [row for row in rows if row["store_id"] in (first, second)]
    assert reached and all(row["template_id"] == template["id"] for row in reached)


def test_stopping_the_whole_estate_is_its_own_right(client):
    """One shop is a local decision; every shop at once has the reach of an
    emergency stop."""
    headers = sign_in(client)
    client.post("/api/users", headers=headers, json={
        "username": "shopfloor", "password": PASSWORD,
        "display_name": "shopfloor", "role": "BROADCASTER"})
    theirs = sign_in(client, "shopfloor")
    assert client.post("/api/announcements/stop-all",
                       headers=theirs).status_code == 403


def test_stopping_one_shop_needs_the_control_right(client):
    """Stop is not a lighter action than pause - it discards the choice - so it
    sits behind the same right, and is refused without it."""
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    client.post("/api/users", headers=headers, json={
        "username": "watcher", "password": PASSWORD, "display_name": "watcher",
        "role": "VIEWER"})
    theirs = sign_in(client, "watcher")
    assert client.post(f"/api/announcements/stores/{store}/stop",
                       headers=theirs).status_code == 403


# ===========================================================================
# Editing what was already decided
# ===========================================================================

def test_a_template_can_be_edited_in_place(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    other = upload(client, headers, title="Second")
    template = make_template(client, headers, audio_id=audio["id"])

    edited = client.put(f"/api/announcements/templates/{template['id']}",
                        headers=headers, json={
                            "name": "Festival - revised",
                            "description": "now the other recording",
                            "items": [{"audio_id": other["id"], "zone": "SOUTH"}]})
    assert edited.status_code == 200, edited.text
    assert edited.json()["name"] == "Festival - revised"
    # The lines are REPLACED, not added to. Editing "this recording, in these
    # places" one row at a time would need ids for rows nobody refers to.
    assert [item["audio_id"] for item in edited.json()["items"]] == [other["id"]]
    assert [item["zone"] for item in edited.json()["items"]] == ["SOUTH"]


def test_editing_a_name_does_not_drop_the_end_date(client):
    """Absent means "leave it"; empty means "no limit". Collapsing the two
    would quietly un-expire a campaign somebody only renamed."""
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             expires_at="2030-01-01T00:00:00+00:00")

    edited = client.put(f"/api/announcements/templates/{template['id']}",
                        headers=headers, json={"name": "Renamed"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["expires_at"] == "2030-01-01T00:00:00+00:00"

    cleared = client.put(f"/api/announcements/templates/{template['id']}",
                         headers=headers,
                         json={"name": "Renamed", "expires_at": ""})
    assert cleared.json()["expires_at"] is None


def test_an_empty_edit_is_refused_rather_than_emptying_the_template(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    refused = client.put(f"/api/announcements/templates/{template['id']}",
                         headers=headers, json={"name": "X", "items": []})
    assert refused.status_code == 400
    assert "plays nothing" in refused.json()["detail"]


def test_a_recording_can_be_renamed_but_not_swapped(client):
    headers = sign_in(client)
    audio = upload(client, headers)

    renamed = client.put(f"/api/announcements/audio/{audio['id']}",
                         headers=headers, json={"title": "Diwali - final cut"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Diwali - final cut"
    # The stored file is untouched: history and every Store's cache point at
    # this recording by id and by content hash.
    assert renamed.json()["sha256"] == audio["sha256"]
    assert renamed.json()["storage_name"] == audio["storage_name"]


def test_editing_is_gated_on_the_rights_that_own_each_thing(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post("/api/users", headers=headers, json={
        "username": "reader", "password": PASSWORD, "display_name": "reader",
        "role": "VIEWER"})
    theirs = sign_in(client, "reader")

    assert client.put(f"/api/announcements/templates/{template['id']}",
                      headers=theirs, json={"name": "no"}).status_code == 403
    assert client.put(f"/api/announcements/audio/{audio['id']}",
                      headers=theirs, json={"title": "no"}).status_code == 403


def test_emergency_stop_silences_announcements_as_well(client):
    """Whoever presses this is not distinguishing between kinds of audio -
    they are stopping the sound in their shops. A version that killed live
    microphones and left a jingle playing would answer a question nobody
    asked."""
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    stopped = client.post("/api/broadcast/emergency-stop", headers=headers)
    assert stopped.status_code == 200, stopped.text

    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    playing = [row for row in rows if row["state"] != "STOPPED"]
    assert playing == []
    # And the assignment survives, exactly as an ordinary Stop leaves it: the
    # shop is silent, not un-targeted.
    assert any(row["template_id"] == template["id"] for row in rows)


def test_history_does_not_claim_a_disconnected_shop_is_still_playing(client):
    """An open row for an unreachable shop is not "still playing".

    Nothing confirmed it started, and with no Receiver connected nothing will
    ever close it - so the row would go on claiming sound in a shop for as
    long as anybody looked at it. This is the same lie the live status and the
    dashboard used to tell, in the one place people go to check afterwards.
    """
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    # The shop drops off AFTER the run began: the row is open, and there is
    # now nothing to close it.
    client.server_module.manager.receivers.clear()
    client.server_module.manager.receiver_snapshots.clear()

    rows = client.get("/api/announcements/history", headers=headers).json()["items"]
    open_rows = [row for row in rows if not row.get("ended_at")]
    assert open_rows, "the play should have opened a history row"
    assert all(row["reachable"] is False for row in open_rows)


def test_a_shop_with_no_receiver_writes_no_history_at_all(client):
    """History is what PLAYED, not what was sent.

    Opening a row for a disconnected shop put minutes - then hours - of
    "playing" into the record for shops that were silent throughout, and every
    report built on this table drifted further from the truth the longer the
    shop stayed offline. The state is still recorded and the shop still picks
    it up when it reconnects; that reconnection is when a run begins.
    """
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH", connected=False)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    # No Receiver is connected for this shop, and none is pretended.
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    rows = client.get("/api/announcements/history", headers=headers).json()["items"]
    assert [row for row in rows if row["store_id"] == store] == []

    # The Store is still holding the campaign, and still says so.
    status = client.get("/api/announcements/status", headers=headers).json()["items"]
    mine = [row for row in status if row["store_id"] == store][0]
    assert mine["state"] == "PLAYING"
    assert mine["reachable"] is False


# ===========================================================================
# The daily window, applied
# ===========================================================================

def scheduler_tick(client, when):
    """Run one scheduler pass at a chosen moment."""
    import asyncio
    from datetime import datetime

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        client.server_module._apply_announcement_schedules(
            now=datetime.strptime(when, "%Y-%m-%d %H:%M")))


def test_a_daily_window_starts_and_stops_the_campaign_by_itself(client):
    """Ten to ten: nobody presses anything."""
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             daily_start="10:00", daily_end="22:00")

    def state():
        rows = client.get("/api/announcements/status", headers=headers).json()["items"]
        return [row for row in rows if row["store_id"] == store][0]["state"]

    assert state() == "STOPPED"

    scheduler_tick(client, "2026-08-14 10:00")
    assert state() == "PLAYING"

    # And it puts itself away. The stop time is exclusive, so 22:00 is silent.
    scheduler_tick(client, "2026-08-14 22:00")
    assert state() == "STOPPED"


def test_the_window_does_not_overrule_a_person(client):
    """A pause a machine undoes thirty seconds later is not a pause.

    Somebody silenced a shop at eleven for a reason; the window must not argue
    with them at noon.
    """
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             daily_start="10:00", daily_end="22:00")

    scheduler_tick(client, "2026-08-14 10:00")
    client.post(f"/api/announcements/stores/{store}/pause", headers=headers)

    scheduler_tick(client, "2026-08-14 12:00")
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [row for row in rows if row["store_id"] == store][0]["state"] == "PAUSED"


def test_an_expired_campaign_is_not_started_by_the_clock(client):
    """The daily window says WHEN in the day, not whether the campaign is
    still running at all."""
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"],
                  daily_start="10:00", daily_end="22:00",
                  expires_at="2020-01-01T00:00:00+00:00")

    scheduler_tick(client, "2026-08-14 10:30")
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [row for row in rows if row["store_id"] == store][0]["state"] == "STOPPED"


def test_half_a_window_is_refused_by_the_api(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    refused = client.post("/api/announcements/templates", headers=headers, json={
        "name": "Half", "daily_start": "10:00",
        "items": [{"audio_id": audio["id"], "zone": "NORTH"}]})
    assert refused.status_code == 400
    assert "both a start and a stop" in refused.json()["detail"]


def test_the_window_is_shown_in_words_on_the_template(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             daily_start="10:00", daily_end="22:00")
    assert "10:00 to 22:00" in template["window"]


def test_editing_a_name_keeps_the_daily_window(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             daily_start="10:00", daily_end="22:00")
    edited = client.put(f"/api/announcements/templates/{template['id']}",
                        headers=headers, json={"name": "Renamed"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["daily_start"] == "10:00"
    assert edited.json()["daily_end"] == "22:00"

    cleared = client.put(f"/api/announcements/templates/{template['id']}",
                         headers=headers,
                         json={"name": "Renamed", "daily_start": "",
                               "daily_end": ""})
    assert cleared.json()["daily_start"] == ""


def test_the_clock_does_not_stop_what_a_person_started(client):
    """Reported from the estate: somebody pressed Play, and twelve seconds
    later the scheduler stopped it, because the template's daily window had
    already closed.

    That is the window overruling a person - the thing this scheduler is
    written not to do at the opening edge, and it was doing it at the closing
    one. A play started by hand carries the account that started it; one the
    clock started carries nobody, so the clock only closes its own.
    """
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"],
                             daily_start="10:00", daily_end="12:00")

    # Well outside the window, and started by a person anyway.
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    scheduler_tick(client, "2026-08-14 18:00")

    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    mine = [row for row in rows if row["store_id"] == store][0]
    assert mine["state"] == "PLAYING", "the clock stopped somebody else's play"


def test_the_clock_does_put_away_its_own(client):
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    make_template(client, headers, audio_id=audio["id"],
                  daily_start="10:00", daily_end="12:00")

    scheduler_tick(client, "2026-08-14 10:30")
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [r for r in rows if r["store_id"] == store][0]["state"] == "PLAYING"

    scheduler_tick(client, "2026-08-14 12:00")
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [r for r in rows if r["store_id"] == store][0]["state"] == "STOPPED"


def test_a_receiver_cannot_download_a_recording_its_store_never_plays(client):
    """The credential sits on a shop-floor desktop.

    Without a scope check one Receiver could walk the numbers and pull the
    estate's whole announcement library, including campaigns for regions it
    has no part in - while the route's own docstring claimed a narrower
    guarantee than the code delivered. Found by an audit, not by a test.
    """
    headers = sign_in(client)
    ours = make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SB", region="SOUTH")
    mine = upload(client, headers, title="North promo")
    theirs = upload(client, headers, title="South promo")
    make_template(client, headers, audio_id=mine["id"], name="North",
                  items=[{"audio_id": mine["id"], "zone": "NORTH"}])
    make_template(client, headers, audio_id=theirs["id"], name="South",
                  items=[{"audio_id": theirs["id"], "zone": "SOUTH"}])

    class Identity:
        store_id = ours
        device_public_id = "dev-north"

    class Authenticator:
        def authenticate(self, **_kwargs):
            return Identity()

    client.server_module.app.state.receiver_runtime_authenticator = Authenticator()
    try:
        allowed = client.get(f"/api/receiver/announcements/{mine['id']}/download",
                             headers={"Authorization": "Bearer device-credential"})
        assert allowed.status_code == 200, allowed.text

        refused = client.get(f"/api/receiver/announcements/{theirs['id']}/download",
                             headers={"Authorization": "Bearer device-credential"})
        assert refused.status_code == 404
        assert "not part of anything this Store plays" in refused.json()["detail"]
    finally:
        client.server_module.app.state.receiver_runtime_authenticator = None


def test_hq_records_what_the_store_said_rather_than_what_it_sent(client):
    """The Receiver has always answered. Nothing read the answers.

    announcement_playing and announcement_failed carry no session, no command
    id and no sequence - an announcement runs for days with no broadcast near
    it - so they fell through to the session-contract parser and were dropped.
    A shop whose decoder could not start, or whose speaker was not open,
    therefore appeared on the console as PLAYING for as long as anybody looked
    at it. Two separate real faults hid behind that for a day.
    """
    import announcement_service

    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    engine = client.server_module.engine

    def row_for(store_id):
        rows = client.get("/api/announcements/status", headers=headers).json()["items"]
        return [row for row in rows if row["store_id"] == store_id][0]

    # Sent, and nothing has answered yet.
    assert row_for(store)["confirmed"] is False

    announcement_service.record_acknowledgement(
        engine, store_id=store, kind="announcement_playing")
    assert row_for(store)["confirmed"] is True
    assert row_for(store)["confirm_error"] == ""

    # And a refusal is kept in the shop's own words, because "it did not play"
    # without a reason sends somebody to the wrong computer.
    announcement_service.record_acknowledgement(
        engine, store_id=store, kind="announcement_failed",
        error="ffmpeg is not installed, so the announcement cannot be decoded")
    after = row_for(store)
    assert after["confirmed"] is False
    assert "ffmpeg" in after["confirm_error"]


def test_both_consoles_read_one_answer_about_a_shop_volume(client):
    """A broadcast reports the shop's level inside its session; a Receiver
    reports it outside one. The Announcements console and the Broadcast
    Console must never disagree about what the speaker is set to, so both
    readings land in the same place.
    """
    import server as server_module

    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")

    server_module.STORE_MASTER_VOLUME[store] = {"volume_percent": 69,
                                                "muted": False}
    try:
        rows = client.get("/api/announcements/status", headers=headers).json()["items"]
        mine = [row for row in rows if row["store_id"] == store][0]
        assert mine["store_volume_percent"] == 69
        assert mine["store_muted"] is False
    finally:
        server_module.STORE_MASTER_VOLUME.pop(store, None)


def test_a_shop_that_has_said_nothing_reports_no_level_rather_than_a_guess(client):
    headers = sign_in(client)
    store = make_store(client, headers, "NB", region="NORTH")

    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    mine = [row for row in rows if row["store_id"] == store][0]
    assert mine["store_volume_percent"] is None


def test_an_expired_campaign_stops_itself_and_lets_the_shops_go(client):
    """Reported from the estate: an expired template was still playing.

    Playing one was already refused once its end date had passed, and the
    daily window would not start it - but nothing stopped a shop that was
    ALREADY playing when the date arrived. It carried on, and the console went
    on naming a finished promotion as the thing that shop was playing.

    Expiry clears the assignment as well as the sound, unlike Stop: the end
    date was decided in advance by the person who set it.
    """
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])

    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [r for r in rows if r["store_id"] == store][0]["state"] == "PLAYING"

    # The end date arrives.
    client.put(f"/api/announcements/templates/{template['id']}",
               headers=headers,
               json={"name": "Festival", "expires_at": "2020-01-01T00:00:00+00:00"})
    scheduler_tick(client, "2026-08-16 14:00")

    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    mine = [row for row in rows if row["store_id"] == store][0]
    assert mine["state"] == "STOPPED"
    assert mine["template_id"] is None, (
        "the console would go on naming a finished promotion")


def test_expiry_is_the_only_thing_that_takes_a_template_off_a_shop(client):
    """Stop keeps the assignment - that was itself a correction from the
    estate - and only the clock, at a date somebody set in advance, clears
    it."""
    headers = sign_in(client)
    store = make_store(client, headers, "NA", region="NORTH")
    audio = upload(client, headers)
    template = make_template(client, headers, audio_id=audio["id"])
    client.post(f"/api/announcements/templates/{template['id']}/play",
                headers=headers)

    client.post(f"/api/announcements/stores/{store}/stop", headers=headers)
    rows = client.get("/api/announcements/status", headers=headers).json()["items"]
    assert [r for r in rows if r["store_id"] == store][0]["template_id"] == template["id"]
