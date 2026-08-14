"""Announcements stepping aside for a real broadcast, and coming back.

test_announcement_state proves the rule on the state machine. This proves it
survives the trip through a real broadcast: an actual session started over
HTTP, actual target rows, actual stop.

The failure being guarded against is not "the jingle does not pause". It is
the quieter one: a shop that starts talking on its own after a broadcast it
had nothing to do with.
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


def make_store(client, headers, code, *, region="DUCKZONE"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": region})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_announcement(client, headers, zone="DUCKZONE"):
    upload = client.post(
        "/api/announcements/audio", headers=headers,
        files={"file": ("j.mp3", io.BytesIO(b"ID3jingle"), "audio/mpeg")},
        data={"title": "Jingle"})
    assert upload.status_code == 201, upload.text
    template = client.post("/api/announcements/templates", headers=headers, json={
        "name": "Jingle everywhere",
        "items": [{"audio_id": upload.json()["id"], "zone": zone}]})
    assert template.status_code == 201, template.text
    played = client.post(
        f"/api/announcements/templates/{template.json()['id']}/play",
        headers=headers)
    assert played.status_code == 200, played.text
    return template.json()["id"], played.json()["started"]


def state_of(client, headers, store_id):
    rows = client.get("/api/announcements/status?page_size=500",
                      headers=headers).json()["items"]
    for row in rows:
        if row["store_id"] == store_id:
            return row["state"]
    raise AssertionError(f"Store {store_id} is not in the status list at all")


def run_broadcast(client, headers, store_ids, *, stop=True):
    """Start a broadcast over the real endpoints and optionally stop it."""
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "Ducking test", "target_mode": "selected",
        "store_ids": list(store_ids)})
    assert created.status_code in (200, 201), created.text
    session_id = created.json()["id"]
    started = client.post(f"/api/broadcast/sessions/{session_id}/start",
                          headers=headers)
    assert started.status_code == 200, started.text
    if stop:
        stopped = client.post(f"/api/broadcast/sessions/{session_id}/stop",
                              headers=headers)
        assert stopped.status_code == 200, stopped.text
    return session_id


# ===========================================================================

def test_a_broadcast_pauses_the_announcement_and_the_end_brings_it_back(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "D1")
    start_announcement(client, headers)
    assert state_of(client, headers, store_id) == "PLAYING"

    session_id = run_broadcast(client, headers, [store_id], stop=False)
    assert state_of(client, headers, store_id) == "DUCKED", (
        "the announcement did not step aside for the broadcast")

    client.post(f"/api/broadcast/sessions/{session_id}/stop", headers=headers)
    assert state_of(client, headers, store_id) == "PLAYING", (
        "the announcement did not come back when the broadcast ended")


def test_a_store_paused_during_the_broadcast_stays_silent_afterwards(client):
    """THE failure this design exists for.

    If pause and duck were one state, the broadcast ending would start an
    announcement an operator had deliberately silenced - the shop would begin
    talking on its own, and nobody could explain why.
    """
    headers = sign_in(client)
    store_id = make_store(client, headers, "D1")
    start_announcement(client, headers)
    session_id = run_broadcast(client, headers, [store_id], stop=False)
    assert state_of(client, headers, store_id) == "DUCKED"

    paused = client.post(f"/api/announcements/stores/{store_id}/pause",
                         headers=headers)
    assert paused.json()["state"] == "PAUSED"

    client.post(f"/api/broadcast/sessions/{session_id}/stop", headers=headers)
    assert state_of(client, headers, store_id) == "PAUSED", (
        "a Store somebody silenced started talking when an unrelated "
        "broadcast finished")


def test_a_store_that_was_not_playing_is_not_started_by_a_broadcast_ending(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "D1")
    # No announcement was ever started here.
    assert state_of(client, headers, store_id) == "STOPPED"

    run_broadcast(client, headers, [store_id])
    assert state_of(client, headers, store_id) == "STOPPED", (
        "a broadcast ending started an announcement that never existed")


def test_only_the_broadcast_stores_are_touched(client):
    headers = sign_in(client)
    inside = make_store(client, headers, "D1")
    outside = make_store(client, headers, "D2")
    start_announcement(client, headers)

    session_id = run_broadcast(client, headers, [inside], stop=False)
    assert state_of(client, headers, inside) == "DUCKED"
    assert state_of(client, headers, outside) == "PLAYING", (
        "a Store the broadcast never reached was silenced anyway")

    client.post(f"/api/broadcast/sessions/{session_id}/stop", headers=headers)
    assert state_of(client, headers, inside) == "PLAYING"
    assert state_of(client, headers, outside) == "PLAYING"


def test_pressing_play_during_a_broadcast_is_refused_rather_than_ignored(client):
    """Obeying would talk over the broadcast. Ignoring would leave somebody
    pressing a button that silently does nothing."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "D1")
    start_announcement(client, headers)
    run_broadcast(client, headers, [store_id], stop=False)

    response = client.post(f"/api/announcements/stores/{store_id}/play",
                           headers=headers)
    assert response.status_code == 409
    assert "broadcast" in response.json()["detail"].lower()
    assert state_of(client, headers, store_id) == "DUCKED"


def test_an_emergency_stop_does_not_resume_another_broadcasts_stores(client):
    """An emergency stop tells every connected Receiver to stop whatever it is
    doing. Resuming announcements across that wider set would start a jingle
    in a shop still standing aside for a different broadcast.

    Here that wider set is exercised by ending one session while a second,
    covering a different Store, is still on air.
    """
    headers = sign_in(client)
    first = make_store(client, headers, "D1")
    second = make_store(client, headers, "D2")
    start_announcement(client, headers)

    first_session = run_broadcast(client, headers, [first], stop=False)
    second_session = run_broadcast(client, headers, [second], stop=False)
    assert state_of(client, headers, first) == "DUCKED"
    assert state_of(client, headers, second) == "DUCKED"

    client.post(f"/api/broadcast/sessions/{first_session}/stop", headers=headers)
    assert state_of(client, headers, first) == "PLAYING"
    assert state_of(client, headers, second) == "DUCKED", (
        "ending one broadcast resumed an announcement in a Store that is "
        "still on air with another one")

    client.post(f"/api/broadcast/sessions/{second_session}/stop", headers=headers)
    assert state_of(client, headers, second) == "PLAYING"


def test_the_volume_survives_ducking(client):
    """A jingle turned down to 30% must come back at 30%, not at the default.
    Nobody would think to re-check it, and the shop would suddenly be loud."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "D1")
    start_announcement(client, headers)
    client.post(f"/api/announcements/stores/{store_id}/volume",
                headers=headers, json={"volume_percent": 30})

    session_id = run_broadcast(client, headers, [store_id], stop=False)
    client.post(f"/api/broadcast/sessions/{session_id}/stop", headers=headers)

    rows = client.get("/api/announcements/status?page_size=500",
                      headers=headers).json()["items"]
    row = next(r for r in rows if r["store_id"] == store_id)
    assert row["state"] == "PLAYING"
    assert row["volume_percent"] == 30
