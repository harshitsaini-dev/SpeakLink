"""What HQ actually sends a Store, and who may fetch a recording.

Two things are worth holding in place here and neither is about the wire
format for its own sake:

  * the volume travels WITH the play command. Sent separately, a Store plays
    the first half-second at whatever level it was holding - which, after a
    broadcast that turned it down, is wrong in the direction people notice.
  * a recording is fetched with the RECEIVER'S own credential. A download link
    that worked without one would let anybody who saw a single command pull
    every recording the estate plays.
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

import announcement_protocol  # noqa: E402

PASSWORD = "a-long-enough-temporary-password"


# ===========================================================================
# The messages themselves
# ===========================================================================

def test_the_volume_travels_with_the_play_command():
    """Not as a message that follows it."""
    command = announcement_protocol.play_command(
        audio_id=7, sha256="abc", download_path="/x", volume_percent=35)
    assert command["type"] == "announcement_play"
    assert command["volume_percent"] == 35


def test_the_play_command_identifies_the_audio_by_content_hash():
    """A filename or an id would let a re-uploaded recording be served from a
    stale cache - half the estate playing last year's Diwali offer."""
    command = announcement_protocol.play_command(
        audio_id=7, sha256="deadbeef", download_path="/x", volume_percent=80)
    assert command["sha256"] == "deadbeef"


def test_pause_says_whether_a_person_or_a_broadcast_caused_it():
    """Identical at the speaker, and a Store log that cannot tell them apart
    cannot answer "why did it go quiet at 4pm"."""
    assert announcement_protocol.pause_command(reason="hq")["reason"] == "hq"
    assert announcement_protocol.pause_command(
        reason="broadcast")["reason"] == "broadcast"


def test_setting_the_volume_is_not_a_play_command():
    """Restating what is playing would restart the recording - the jingle
    would jump back to its first word every time somebody nudged the slider."""
    command = announcement_protocol.set_volume_command(volume_percent=20)
    assert command["type"] == "announcement_set_volume"
    assert "download_path" not in command
    assert "sha256" not in command


def test_announcement_verbs_are_distinct_from_the_broadcast_ones():
    """A Receiver built before this feature must ignore what it does not
    understand rather than mistake an announcement for a broadcast."""
    for command in announcement_protocol.COMMANDS:
        assert command.startswith("announcement_")
    assert "play" not in announcement_protocol.COMMANDS
    assert "stop" not in announcement_protocol.COMMANDS


# ===========================================================================
# The download
# ===========================================================================

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


def sign_in(client):
    response = client.post("/api/auth/login",
                           json={"username": "founder", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def upload(client, headers):
    response = client.post(
        "/api/announcements/audio", headers=headers,
        files={"file": ("d.mp3", io.BytesIO(b"ID3the-audio"), "audio/mpeg")},
        data={"title": "Diwali"})
    assert response.status_code == 201, response.text
    return response.json()


def test_a_receiver_with_a_store_token_may_download(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    stores = client.get("/api/stores", headers=headers).json()
    store = stores["items"][0] if isinstance(stores, dict) else stores[0]
    token = client.get(f"/api/stores/{store['id']}", headers=headers).json().get(
        "receiver_token")
    if not token:
        pytest.skip("this HQ does not expose a legacy Store token to read")

    response = client.get(
        announcement_protocol.download_path(audio["id"]),
        headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert response.content == b"ID3the-audio"


def test_an_unauthenticated_download_is_refused(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    response = client.get(announcement_protocol.download_path(audio["id"]))
    assert response.status_code == 401


def test_a_made_up_credential_is_refused(client):
    headers = sign_in(client)
    audio = upload(client, headers)
    response = client.get(
        announcement_protocol.download_path(audio["id"]),
        headers={"Authorization": "Bearer not-a-real-credential"})
    assert response.status_code == 401


def test_an_hq_account_token_is_not_a_receiver_credential(client):
    """The two authentication systems must not be interchangeable. An HQ
    session token reaching a Receiver route would mean a Receiver credential
    could eventually reach an HQ one."""
    headers = sign_in(client)
    audio = upload(client, headers)
    response = client.get(announcement_protocol.download_path(audio["id"]),
                          headers=headers)
    assert response.status_code == 401
