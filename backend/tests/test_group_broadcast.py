"""More than one voice on one broadcast.

Two ways in, and the difference is a permission:

  * an account holding broadcast.group_join walks in. The estate has already
    decided this person may speak on air, and asking the host again would be a
    second approval for a decision already made.
  * everybody else asks. The host is the only person who can hear what is
    happening on that broadcast right now, so they are the only one who can
    judge whether a second voice is wanted this minute.

The test that matters most is the last section: the microphone socket admits
JOINED and nothing else. Everything above it is bookkeeping, and a REQUESTED
or DENIED account that could still push audio would make all of it decoration.
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
    monkeypatch.setenv("SPEAKLINK_KEY_PROTECTOR", "fake")
    monkeypatch.setenv("SPEAKLINK_KEY_CONTAINER",
                       str(tmp_path / "keys" / "receiver-hmac-keys.bin"))
    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "broadcast_group")]:
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


def make_user(client, headers, username, role="BROADCASTER"):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD, "display_name": username,
        "role": role})
    assert response.status_code in (200, 201), response.text
    return response.json()


def make_store(client, headers, code):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": "GROUPZONE"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def start_broadcast(client, headers, store_ids):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "Group test", "target_mode": "selected",
        "store_ids": list(store_ids)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    started = client.post(f"/api/broadcast/sessions/{session_id}/start",
                          headers=headers)
    assert started.status_code == 200, started.text
    return session_id


def set_override(client, headers, user_id, code, effect):
    """Grant or deny one fine-grained code on one account."""
    response = client.put(f"/api/users/{user_id}/permissions", headers=headers,
                          json={"changes": [{"code": code, "effect": effect}]})
    assert response.status_code in (200, 204), response.text


# ===========================================================================
# The two ways in
# ===========================================================================

def test_the_right_to_join_means_joining_without_asking(client):
    """Asking the host again would be a second approval for a decision the
    estate made when the right was granted."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "second-voice")
    set_override(client, headers, guest["id"], "broadcast.group_join", "ALLOW")
    session_id = start_broadcast(client, headers, [store_id])

    joined = client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                         headers=sign_in(client, "second-voice"))
    assert joined.status_code == 200, joined.text
    assert joined.json()["on_air"] is True
    assert joined.json()["participant"]["state"] == "JOINED"


def test_without_the_right_a_join_is_a_request_and_nothing_more(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "asker")
    set_override(client, headers, guest["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])

    asked = client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                        headers=sign_in(client, "asker"))
    assert asked.status_code == 200, asked.text
    assert asked.json()["on_air"] is False
    assert asked.json()["participant"]["state"] == "REQUESTED"
    assert "waiting for the host" in asked.json()["status"]


def test_the_host_can_approve_a_request(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "asker")
    set_override(client, headers, guest["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=sign_in(client, "asker"))

    approved = client.post(
        f"/api/broadcast/sessions/{session_id}/group/requests/{guest['id']}/approve",
        headers=headers)
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "JOINED"


def test_a_denial_is_remembered_rather_than_re_askable_with_one_click(client):
    """Otherwise the host answers the same question all through a broadcast
    they are trying to run."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "asker")
    set_override(client, headers, guest["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])
    guest_headers = sign_in(client, "asker")
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=guest_headers)
    client.post(
        f"/api/broadcast/sessions/{session_id}/group/requests/{guest['id']}/deny",
        headers=headers)

    again = client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                        headers=guest_headers)
    assert again.status_code == 409
    assert "already declined" in again.json()["detail"]


def test_only_the_host_answers_requests(client):
    """The host is the one person who can hear what is happening on that
    broadcast right now."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "asker")
    bystander = make_user(client, headers, "bystander", role="ADMIN")
    set_override(client, headers, guest["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=sign_in(client, "asker"))

    assert bystander is not None
    response = client.post(
        f"/api/broadcast/sessions/{session_id}/group/requests/{guest['id']}/approve",
        headers=sign_in(client, "bystander"))
    assert response.status_code == 403
    assert "who started this broadcast" in response.json()["detail"]


def test_a_viewer_cannot_join_or_ask(client):
    """This puts a voice on the loudspeakers of real shops. An account that may
    not broadcast at all must not reach that by asking somebody nicely."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    make_user(client, headers, "watcher", role="VIEWER")
    session_id = start_broadcast(client, headers, [store_id])

    response = client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                           headers=sign_in(client, "watcher"))
    assert response.status_code == 403


def test_the_host_is_not_a_guest_of_their_own_broadcast(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    session_id = start_broadcast(client, headers, [store_id])

    response = client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                           headers=headers)
    assert response.status_code == 400
    assert "already the host" in response.json()["detail"]


def test_who_else_asked_is_the_hosts_business_only(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    asker = make_user(client, headers, "asker")
    other = make_user(client, headers, "other")
    set_override(client, headers, asker["id"], "broadcast.group_join", "DENY")
    set_override(client, headers, other["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=sign_in(client, "asker"))
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=sign_in(client, "other"))

    as_host = client.get(f"/api/broadcast/sessions/{session_id}/group",
                         headers=headers).json()
    assert len(as_host["participants"]) == 2
    assert as_host["is_host"] is True

    as_guest = client.get(f"/api/broadcast/sessions/{session_id}/group",
                          headers=sign_in(client, "asker")).json()
    assert as_guest["is_host"] is False
    assert as_guest["participants"] == [], (
        "one guest can see who else asked to speak on somebody else's broadcast")
    assert as_guest["me"]["state"] == "REQUESTED"


def test_leaving_works_even_after_the_right_is_taken_away(client):
    """An account demoted since joining must still be able to take itself off
    air; refusing that leaves a voice on the loudspeakers with no way to remove
    it except stopping the whole broadcast."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "second-voice")
    set_override(client, headers, guest["id"], "broadcast.group_join", "ALLOW")
    session_id = start_broadcast(client, headers, [store_id])
    guest_headers = sign_in(client, "second-voice")
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=guest_headers)

    set_override(client, headers, guest["id"], "broadcast.start", "DENY")
    left = client.post(f"/api/broadcast/sessions/{session_id}/group/leave",
                       headers=sign_in(client, "second-voice"))
    assert left.status_code == 200, left.text
    assert left.json()["state"] == "LEFT"


def test_joining_a_broadcast_that_is_not_live_is_refused(client):
    headers = sign_in(client)
    make_store(client, headers, "G1")
    guest = make_user(client, headers, "second-voice")
    set_override(client, headers, guest["id"], "broadcast.group_join", "ALLOW")

    response = client.post("/api/broadcast/sessions/999999/group/join",
                           headers=sign_in(client, "second-voice"))
    assert response.status_code == 404


# ===========================================================================
# The line that is the whole feature
# ===========================================================================

def test_the_microphone_admits_joined_and_nothing_else(client):
    """A default that admitted anything but an explicit refusal would put a
    requester on air the moment they asked."""
    import broadcast_group
    engine = client.server_module.engine

    headers = sign_in(client)
    store_id = make_store(client, headers, "G1")
    guest = make_user(client, headers, "asker")
    set_override(client, headers, guest["id"], "broadcast.group_join", "DENY")
    session_id = start_broadcast(client, headers, [store_id])
    guest_headers = sign_in(client, "asker")

    # Asked, not answered: not on air.
    client.post(f"/api/broadcast/sessions/{session_id}/group/join",
                headers=guest_headers)
    assert broadcast_group.is_on_air(engine, session_id=session_id,
                                     user_id=guest["id"]) is False

    # Denied: still not on air.
    client.post(
        f"/api/broadcast/sessions/{session_id}/group/requests/{guest['id']}/deny",
        headers=headers)
    assert broadcast_group.is_on_air(engine, session_id=session_id,
                                     user_id=guest["id"]) is False

    # Approved: on air.
    broadcast_group._write(engine, session_id=session_id, user_id=guest["id"],
                           state=broadcast_group.STATE_REQUESTED,
                           requested_at=broadcast_group.utcnow())
    client.post(
        f"/api/broadcast/sessions/{session_id}/group/requests/{guest['id']}/approve",
        headers=headers)
    assert broadcast_group.is_on_air(engine, session_id=session_id,
                                     user_id=guest["id"]) is True

    # Left: off air again.
    client.post(f"/api/broadcast/sessions/{session_id}/group/leave",
                headers=guest_headers)
    assert broadcast_group.is_on_air(engine, session_id=session_id,
                                     user_id=guest["id"]) is False


def test_the_uplink_socket_checks_the_participant_table(client):
    """Read as source, because the socket cannot be handshaken here - but the
    check being present and being about JOINED is exactly what stops a URL
    edit putting a stranger's voice into somebody else's broadcast."""
    import inspect

    body = inspect.getsource(client.server_module.ws_broadcaster)
    assert "broadcast_group.is_on_air" in body
    assert "started_by == int(user_id)" in body
