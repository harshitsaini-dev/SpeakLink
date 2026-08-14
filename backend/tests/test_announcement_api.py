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
