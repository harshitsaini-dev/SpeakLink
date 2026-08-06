"""Serving a recording: who may hear it, and what the route refuses to leak.

A recording is the audio of a real announcement in a real shop. The recordings
directory is therefore NOT a static mount - every byte leaves through this
route, which applies the same permission and the same Store Scope as Broadcast
History itself.

Byte-range support is tested because a browser audio element asks for one.
Without it, seeking in a WebM means refetching the whole file and some
browsers simply refuse to seek at all.
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
AUDIO = b"0123456789" * 200          # 2000 bytes, easy to reason about


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    # A recordings directory of this test's own. The real one is never touched.
    monkeypatch.setenv("ECHOCAST_DATA_DIR", str(tmp_path / "data"))

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "store_audio_control",
                                                              "master_volume_api", "broadcast_recording")]:
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


def make_session(client, *, session_id=1, store_ids=()):
    """A finished broadcast, written directly.

    Driving a real one would need a microphone socket and online Receivers,
    none of which the playback route depends on.
    """
    from db import engine
    from sqlalchemy import text

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO broadcast_sessions "
            "(id, campaign_name, target_mode, status, selected_store_count, "
            " online_store_count, offline_store_count, created_at) "
            "VALUES (:id, 'Morning announcement', 'selected', 'ended', "
            "        :count, 0, 0, :now)"),
            {"id": session_id, "count": len(store_ids),
             "now": "2026-08-06 10:00:00"})
        for store_id in store_ids:
            connection.execute(text(
                "INSERT INTO broadcast_targets (session_id, store_id, play_status) "
                "VALUES (:session_id, :store_id, 'stopped')"),
                {"session_id": session_id, "store_id": store_id})
    return session_id


def make_recording(client, session_id, *, status="available", audio=AUDIO,
                   write_file=True):
    import broadcast_recording
    from db import engine

    directory = broadcast_recording.recordings_directory()
    directory.mkdir(parents=True, exist_ok=True)
    file_name = broadcast_recording.recording_filename(session_id)
    if write_file:
        (directory / file_name).write_bytes(audio)
    broadcast_recording.start_record(engine, session_id=session_id,
                                     file_name=file_name)
    broadcast_recording.finish_record(
        engine, session_id=session_id, status=status,
        container="webm", codec="opus",
        byte_size=len(audio) if write_file else None,
        duration_seconds=64.0, chunks_written=10)
    return file_name


def store_ids(client, headers, count=2):
    rows = client.get("/api/stores", headers=headers).json()
    return [row["id"] for row in rows[:count]]


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ===========================================================================
# Metadata
# ===========================================================================
def test_history_carries_each_broadcasts_recording_status(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    rows = client.get("/api/broadcast/history", headers=owner).json()
    row = next(r for r in rows if r["id"] == 1)
    assert row["recording"]["status"] == "available"
    assert row["recording"]["duration_seconds"] == 64.0


def test_a_broadcast_with_no_recording_says_null_not_failed(client, owner):
    """Absent and failed are different, and History must not conflate them."""
    make_session(client, session_id=1)
    rows = client.get("/api/broadcast/history", headers=owner).json()
    assert next(r for r in rows if r["id"] == 1)["recording"] is None


def test_the_metadata_response_carries_no_path_or_filename(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    body = client.get("/api/broadcast/sessions/1/recording", headers=owner).text
    assert "broadcast-000001" not in body
    assert "recordings" not in body
    assert ".webm" not in body


# ===========================================================================
# Playback and authorization
# ===========================================================================
def test_an_authorized_operator_can_play_a_recording(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers=owner)
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-type"].startswith("audio/webm")


def test_an_unauthenticated_request_gets_no_audio(client):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio")
    assert response.status_code in (401, 403)
    assert AUDIO not in response.content


def test_an_account_without_history_permission_is_refused(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    user_id = make_user(client, owner, "viewer", "VIEWER")
    client.put(f"/api/users/{user_id}/permissions", headers=owner,
               json={"changes": [{"code": "menu.history.view", "effect": "DENY"}]})
    viewer = sign_in(client, "viewer")

    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers=viewer)
    assert response.status_code == 403
    assert AUDIO not in response.content


def test_a_scoped_operator_cannot_hear_another_regions_broadcast(client, owner):
    """Store Scope already governs History; a recording must not evade it."""
    ids = store_ids(client, owner, count=2)
    make_session(client, session_id=1, store_ids=[ids[1]])
    make_recording(client, 1)

    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    client.put(f"/api/users/{caster_id}/store-scope", headers=owner,
               json={"entries": [{"scope_type": "STORE", "store_id": ids[0]}]})
    caster = sign_in(client, "caster")

    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers=caster)
    # 404 rather than 403, matching the rest of History: a 403 would confirm
    # the broadcast exists and let an out-of-scope account enumerate it.
    assert response.status_code == 404
    assert AUDIO not in response.content


def test_a_scoped_operator_can_hear_their_own_broadcast(client, owner):
    ids = store_ids(client, owner, count=2)
    make_session(client, session_id=1, store_ids=[ids[0]])
    make_recording(client, 1)

    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    client.put(f"/api/users/{caster_id}/store-scope", headers=owner,
               json={"entries": [{"scope_type": "STORE", "store_id": ids[0]}]})
    caster = sign_in(client, "caster")

    assert client.get("/api/broadcast/sessions/1/recording/audio",
                      headers=caster).status_code == 200


# ===========================================================================
# Honest failures
# ===========================================================================
def test_a_failed_recording_is_not_playable(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1, status="failed", write_file=False)
    assert client.get("/api/broadcast/sessions/1/recording/audio",
                      headers=owner).status_code == 404


def test_a_partial_recording_IS_playable(client, owner):
    """A recording with a gap is still the best evidence of what went out."""
    make_session(client, session_id=1)
    make_recording(client, 1, status="partial")
    assert client.get("/api/broadcast/sessions/1/recording/audio",
                      headers=owner).status_code == 200


def test_a_vanished_file_is_recorded_as_missing_rather_than_500(client, owner):
    """The row said there was audio and there is not - say so, and remember."""
    make_session(client, session_id=1)
    file_name = make_recording(client, 1)
    import broadcast_recording
    from db import engine
    (broadcast_recording.recordings_directory() / file_name).unlink()

    assert client.get("/api/broadcast/sessions/1/recording/audio",
                      headers=owner).status_code == 404
    # And the next History read stops offering a Play button for it.
    assert broadcast_recording.get_recording(engine, session_id=1).status == "missing"


def test_an_unknown_session_is_a_404(client, owner):
    assert client.get("/api/broadcast/sessions/999999/recording/audio",
                      headers=owner).status_code == 404


# ===========================================================================
# Range requests, so a browser can seek
# ===========================================================================
def test_a_range_request_returns_only_that_range(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers={**owner, "Range": "bytes=0-99"})
    assert response.status_code == 206
    assert response.content == AUDIO[:100]
    assert response.headers["content-range"] == f"bytes 0-99/{len(AUDIO)}"
    assert response.headers["content-length"] == "100"


def test_an_open_ended_range_runs_to_the_end(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers={**owner, "Range": "bytes=1900-"})
    assert response.status_code == 206
    assert response.content == AUDIO[1900:]


def test_a_suffix_range_returns_the_tail(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers={**owner, "Range": "bytes=-50"})
    assert response.status_code == 206
    assert response.content == AUDIO[-50:]


def test_a_range_beyond_the_file_is_clamped_rather_than_crashing(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers={**owner, "Range": "bytes=0-999999"})
    assert response.status_code == 206
    assert response.content == AUDIO


def test_the_response_advertises_range_support(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/audio",
                          headers=owner)
    assert response.headers["accept-ranges"] == "bytes"


def test_the_download_name_carries_nothing_private(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    disposition = client.get("/api/broadcast/sessions/1/recording/audio",
                             headers=owner).headers["content-disposition"]
    assert "broadcast-000001.webm" in disposition
    for leak in ("founder", "Morning announcement", "token", "password"):
        assert leak not in disposition


# ===========================================================================
# Deletion
# ===========================================================================
def test_deleting_history_removes_the_recording_and_its_file(client, owner):
    import broadcast_recording
    from db import engine

    make_session(client, session_id=1)
    make_session(client, session_id=2)
    file_one = make_recording(client, 1)
    file_two = make_recording(client, 2)
    directory = broadcast_recording.recordings_directory()

    response = client.post("/api/broadcast/history/delete-permanently",
                           headers=owner,
                           json={"ids": [1], "confirm": "DELETE", "acknowledged": True})
    assert response.status_code == 200, response.text

    assert not (directory / file_one).exists()
    assert broadcast_recording.get_recording(engine, session_id=1) is None
    # The other broadcast is untouched, and so is the directory itself.
    assert (directory / file_two).exists()
    assert broadcast_recording.get_recording(engine, session_id=2) is not None
    assert directory.exists()


# ===========================================================================
# Download
# ===========================================================================
def test_an_authorized_operator_can_download_a_recording(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/download",
                          headers=owner)
    assert response.status_code == 200
    assert response.content == AUDIO
    assert response.headers["content-disposition"].startswith("attachment;")


def test_playback_stays_inline_and_download_is_an_attachment(client, owner):
    """The same bytes, and the only difference is how the browser treats them."""
    make_session(client, session_id=1)
    make_recording(client, 1)
    play = client.get("/api/broadcast/sessions/1/recording/audio", headers=owner)
    save = client.get("/api/broadcast/sessions/1/recording/download", headers=owner)
    assert play.content == save.content
    assert play.headers["content-disposition"].startswith("inline;")
    assert save.headers["content-disposition"].startswith("attachment;")


def test_an_unauthenticated_download_gets_no_audio(client):
    make_session(client, session_id=1)
    make_recording(client, 1)
    response = client.get("/api/broadcast/sessions/1/recording/download")
    assert response.status_code in (401, 403)
    assert AUDIO not in response.content


def test_an_account_without_history_permission_cannot_download(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    user_id = make_user(client, owner, "viewer", "VIEWER")
    client.put(f"/api/users/{user_id}/permissions", headers=owner,
               json={"changes": [{"code": "menu.history.view", "effect": "DENY"}]})
    viewer = sign_in(client, "viewer")

    response = client.get("/api/broadcast/sessions/1/recording/download",
                          headers=viewer)
    assert response.status_code == 403
    assert AUDIO not in response.content


def test_a_scoped_operator_cannot_download_another_regions_broadcast(client, owner):
    """Downloading is not a way around Store Scope."""
    ids = store_ids(client, owner, count=2)
    make_session(client, session_id=1, store_ids=[ids[1]])
    make_recording(client, 1)

    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    client.put(f"/api/users/{caster_id}/store-scope", headers=owner,
               json={"entries": [{"scope_type": "STORE", "store_id": ids[0]}]})
    caster = sign_in(client, "caster")

    response = client.get("/api/broadcast/sessions/1/recording/download",
                          headers=caster)
    assert response.status_code == 404
    assert AUDIO not in response.content


def test_the_download_name_carries_nothing_private(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1)
    disposition = client.get("/api/broadcast/sessions/1/recording/download",
                             headers=owner).headers["content-disposition"]
    assert 'filename="broadcast-000001.webm"' in disposition
    for leak in ("founder", "Morning announcement", "token", "password"):
        assert leak not in disposition


def test_a_failed_recording_cannot_be_downloaded(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1, status="failed", write_file=False)
    assert client.get("/api/broadcast/sessions/1/recording/download",
                      headers=owner).status_code == 404


def test_a_partial_recording_can_be_downloaded(client, owner):
    """Incomplete is still the best evidence of what went out."""
    make_session(client, session_id=1)
    make_recording(client, 1, status="partial")
    assert client.get("/api/broadcast/sessions/1/recording/download",
                      headers=owner).status_code == 200


def test_no_recording_url_carries_a_token(client, owner):
    """The credential travels in a header, never in something bookmarkable."""
    make_session(client, session_id=1)
    make_recording(client, 1)
    for path in ("/api/broadcast/sessions/1/recording",
                 "/api/broadcast/sessions/1/recording/audio",
                 "/api/broadcast/sessions/1/recording/download"):
        response = client.get(path, headers=owner)
        assert response.status_code == 200
        assert "?" not in str(response.url) or "token" not in str(response.url)


# ===========================================================================
# Deleting history deletes exactly one recording
# ===========================================================================
def test_deleting_one_broadcast_never_touches_another_recording(client, owner):
    import broadcast_recording
    from db import engine

    for session_id in (1, 2, 3):
        make_session(client, session_id=session_id)
        make_recording(client, session_id)
    directory = broadcast_recording.recordings_directory()

    response = client.post("/api/broadcast/history/delete-permanently",
                           headers=owner,
                           json={"ids": [2], "confirm": "DELETE",
                                 "acknowledged": True})
    assert response.status_code == 200, response.text

    assert not (directory / "broadcast-000002.webm").exists()
    assert broadcast_recording.get_recording(engine, session_id=2) is None
    for survivor in (1, 3):
        assert (directory / f"broadcast-{survivor:06d}.webm").exists()
        assert broadcast_recording.get_recording(engine, session_id=survivor)
    assert directory.exists(), "the directory itself is never removed"


def test_deleting_a_broadcast_whose_file_already_vanished_still_succeeds(client, owner):
    """A missing file must not block an operator from clearing history."""
    import broadcast_recording
    from db import engine

    make_session(client, session_id=1)
    file_name = make_recording(client, 1)
    (broadcast_recording.recordings_directory() / file_name).unlink()

    response = client.post("/api/broadcast/history/delete-permanently",
                           headers=owner,
                           json={"ids": [1], "confirm": "DELETE",
                                 "acknowledged": True})
    assert response.status_code == 200, response.text
    assert broadcast_recording.get_recording(engine, session_id=1) is None


def test_an_unauthorized_account_cannot_delete_a_recording(client, owner):
    import broadcast_recording
    from db import engine

    make_session(client, session_id=1)
    make_recording(client, 1)
    user_id = make_user(client, owner, "viewer", "VIEWER")
    viewer = sign_in(client, "viewer")

    response = client.post("/api/broadcast/history/delete-permanently",
                           headers=viewer,
                           json={"ids": [1], "confirm": "DELETE",
                                 "acknowledged": True})
    assert response.status_code == 403
    # The audio and its metadata are both still there.
    assert broadcast_recording.get_recording(engine, session_id=1) is not None
    assert (broadcast_recording.recordings_directory()
            / "broadcast-000001.webm").exists()


# ===========================================================================
# The paginated endpoint the History page actually reads
# ===========================================================================
def test_the_search_endpoint_attaches_recordings(client, owner):
    """The bug this file exists to stop coming back.

    Broadcast History in the browser reads /broadcast/history/SEARCH - the
    paginated endpoint - not /broadcast/history. The recording metadata was
    attached only to the second, so seven completed broadcasts whose audio was
    on disk and whose rows said AVAILABLE all rendered as "No recording".

    Two endpoints returning the same shape had drifted, and only one of them
    was ever looked at.
    """
    make_session(client, session_id=1)
    make_recording(client, 1)

    response = client.get("/api/broadcast/history/search",
                          headers=owner, params={"page": 1, "page_size": 10})
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["items"] if r["id"] == 1)
    assert row["recording"] is not None, "the page would say No recording"
    assert row["recording"]["status"] == "available"
    assert row["recording"]["byte_size"] == len(AUDIO)


def test_both_history_endpoints_agree_about_recordings(client, owner):
    """They describe the same broadcasts and must not disagree."""
    for session_id in (1, 2):
        make_session(client, session_id=session_id)
    make_recording(client, 1)
    make_recording(client, 2, status="partial")

    listed = {r["id"]: r["recording"]
              for r in client.get("/api/broadcast/history", headers=owner).json()}
    searched = {r["id"]: r["recording"]
                for r in client.get("/api/broadcast/history/search", headers=owner,
                                    params={"page": 1, "page_size": 10}).json()["items"]}
    assert listed == searched


def test_the_search_endpoint_says_nothing_for_a_broadcast_with_no_recording(
        client, owner):
    """Absent must stay absent - the fix must not invent a recording."""
    make_session(client, session_id=1)
    response = client.get("/api/broadcast/history/search", headers=owner,
                          params={"page": 1, "page_size": 10})
    row = next(r for r in response.json()["items"] if r["id"] == 1)
    assert row["recording"] is None


def test_the_search_endpoint_reports_a_failed_recording_honestly(client, owner):
    make_session(client, session_id=1)
    make_recording(client, 1, status="failed", write_file=False)
    response = client.get("/api/broadcast/history/search", headers=owner,
                          params={"page": 1, "page_size": 10})
    row = next(r for r in response.json()["items"] if r["id"] == 1)
    assert row["recording"]["status"] == "failed"


def test_the_search_endpoint_still_applies_store_scope(client, owner):
    """Attaching recordings must not widen who can see a broadcast."""
    ids = store_ids(client, owner, count=2)
    make_session(client, session_id=1, store_ids=[ids[1]])
    make_recording(client, 1)

    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    client.put(f"/api/users/{caster_id}/store-scope", headers=owner,
               json={"entries": [{"scope_type": "STORE", "store_id": ids[0]}]})
    caster = sign_in(client, "caster")

    response = client.get("/api/broadcast/history/search", headers=caster,
                          params={"page": 1, "page_size": 10})
    assert all(r["id"] != 1 for r in response.json()["items"])
