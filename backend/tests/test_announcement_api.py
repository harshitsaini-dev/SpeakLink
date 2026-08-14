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


def make_store(client, headers, code, *, region="NORTH", city="DELHI"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": city, "region": region})
    assert response.status_code == 201, response.text
    return response.json()["id"]


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
