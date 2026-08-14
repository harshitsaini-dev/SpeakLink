"""Changing a Store's speaker from HQ.

Every test here exists because of one fact: nobody at HQ can hear the result.
A wrong selection produces silence, and silence is the failure this system
cannot detect on its own - no error, no disconnection, no failed command.

So what is being held in place is not "the setting saves". It is:

  * HQ can only offer what the Store itself reported;
  * a speaker that was unplugged disappears from that list rather than
    remaining selectable for ever;
  * the row never claims a change the Store has not confirmed;
  * the answer says what is TRUE, not what was asked for.
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

REALTEK = {"index": 3, "name": "Speakers (Realtek(R) Audio)",
           "selector": "index:3", "verified_selector":
               "index:3@Speakers (Realtek(R) Audio)",
           "is_default": True, "looks_wireless": False}
HEADSET = {"index": 5, "name": "Bluetooth Headset", "selector": "index:5",
           "verified_selector": "index:5@Bluetooth Headset",
           "is_default": False, "looks_wireless": True}


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
                               "store_scope", "receiver_output_device")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    # The connection manager lives in ws_manager, which is NOT reloaded between
    # tests - so a Store left "connected" by an earlier test was still
    # connected here, and every store id restarts at the same number on a fresh
    # database. Two tests about an OFFLINE Store were quietly running against
    # an online one.
    server_module.manager.receivers.clear()
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_store(client, headers, code="OUT1"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": "NORTH"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD, "display_name": username,
        "role": role})
    assert response.status_code in (200, 201), response.text
    return response.json()


def report_devices(client, store_id, devices):
    """What the Store told HQ it has."""
    import receiver_output_device
    receiver_output_device.record_reported_devices(
        client.server_module.engine, store_id=store_id, devices=devices)


class FakeReceiverSocket:
    """A connected Receiver that records what HQ sent it.

    A bare object() was not enough and the difference mattered: the first send
    raised, the manager correctly disconnected the "dead" Receiver, and the
    NEXT request was refused as offline - a test failure that had nothing to
    do with what was being tested. Recording the messages also lets these
    tests assert what was actually sent, which is the more useful half.
    """

    def __init__(self):
        self.sent = []

    async def send_text(self, payload):
        import json
        self.sent.append(json.loads(payload))


def pretend_online(client, store_id):
    """Put a connected Receiver on the real structure the routes consult, so a
    change to how presence is tracked breaks this test rather than silently
    bypassing it."""
    socket = FakeReceiverSocket()
    client.server_module.manager.receivers[store_id] = socket
    return socket


# ===========================================================================
# What HQ will offer
# ===========================================================================

def test_a_store_that_has_never_reported_offers_nothing(client):
    headers = sign_in(client)
    store_id = make_store(client, headers)
    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert state["devices"] == []
    assert state["summary"] == "not reported yet"


def test_only_what_the_store_reported_can_be_selected(client):
    """There is no free-text field, and this is why: a selector typed by
    somebody who cannot hear the result may resolve to nothing - or, worse, to
    a different device that does exist."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK])
    pretend_online(client, store_id)

    invented = client.post(f"/api/stores/{store_id}/audio-output",
                           headers=headers, json={"selector": "index:9"})
    assert invented.status_code == 400
    assert "did not report" in invented.json()["detail"]
    assert "Realtek" in invented.json()["detail"], (
        "the refusal must say what the Store actually has")


def test_an_unplugged_speaker_disappears_rather_than_staying_selectable(client):
    """Merging reports would leave a removed endpoint selectable for ever, and
    selecting it is precisely the mistake that makes a shop silent."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK, HEADSET])
    report_devices(client, store_id, [REALTEK])
    pretend_online(client, store_id)

    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert [device["name"] for device in state["devices"]] == [REALTEK["name"]]

    gone = client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                       json={"selector": HEADSET["verified_selector"]})
    assert gone.status_code == 400


def test_choosing_nothing_is_refused(client):
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK])
    pretend_online(client, store_id)
    response = client.post(f"/api/stores/{store_id}/audio-output",
                           headers=headers, json={"selector": ""})
    assert response.status_code == 400


# ===========================================================================
# HQ never claims a change the Store has not confirmed
# ===========================================================================

def test_sending_a_change_does_not_record_it_as_applied(client):
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK, HEADSET])
    pretend_online(client, store_id)

    socket = client.server_module.manager.receivers[store_id]
    sent = client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                       json={"selector": HEADSET["verified_selector"]})
    assert sent.status_code == 200, sent.text
    assert socket.sent == [{"type": "set_output_device",
                            "selector": HEADSET["verified_selector"]}], (
        "the Store was not actually told to change its speaker")
    body = sent.json()
    assert body["requested_selector"] == HEADSET["verified_selector"]
    assert body["applied_selector"] is None, (
        "HQ recorded a change the shop has not confirmed")
    assert "has not answered yet" in body["summary"]
    assert "nobody at HQ can hear" in body["note"]


def test_the_store_confirming_records_what_it_actually_ended_up_on(client):
    """"HQ sent index:5" and "the Store is playing through Bluetooth Headset"
    are different facts, and the operator needs the second one."""
    import receiver_output_device
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK, HEADSET])
    pretend_online(client, store_id)
    client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                json={"selector": HEADSET["verified_selector"]})

    client.server_module._handle_output_device_report(store_id, {
        "type": "output_device_result", "result": "applied",
        "applied_selector": HEADSET["verified_selector"],
        "applied_device_name": HEADSET["name"]})

    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert state["applied_device_name"] == HEADSET["name"]
    assert state["summary"] == f"playing through {HEADSET['name']}"


def test_a_refusal_keeps_naming_the_speaker_the_shop_is_really_on(client):
    """The question somebody has when a shop reports silence is not "what did
    we ask for" - it is "what is it actually playing through"."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK, HEADSET])
    pretend_online(client, store_id)

    client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                json={"selector": REALTEK["verified_selector"]})
    client.server_module._handle_output_device_report(store_id, {
        "type": "output_device_result", "result": "applied",
        "applied_selector": REALTEK["verified_selector"],
        "applied_device_name": REALTEK["name"]})

    client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                json={"selector": HEADSET["verified_selector"]})
    client.server_module._handle_output_device_report(store_id, {
        "type": "output_device_result", "result": "refused",
        "error": "that endpoint is no longer present"})

    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert state["last_result"] == "refused"
    assert REALTEK["name"] in state["summary"], (
        "the operator cannot see which speaker the shop is on")
    assert "no longer present" in state["summary"]


def test_the_previous_speaker_is_recorded_so_somebody_else_can_undo_it(client):
    """Given nobody can hear the result, whoever undoes a change is usually
    not the person who made it."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK, HEADSET])
    pretend_online(client, store_id)

    client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                json={"selector": REALTEK["verified_selector"]})
    client.server_module._handle_output_device_report(store_id, {
        "type": "output_device_result", "result": "applied",
        "applied_selector": REALTEK["verified_selector"],
        "applied_device_name": REALTEK["name"]})

    second = client.post(f"/api/stores/{store_id}/audio-output", headers=headers,
                         json={"selector": HEADSET["verified_selector"]})
    assert second.status_code == 200, second.text
    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert state["previous_selector"] == REALTEK["verified_selector"]


def test_an_offline_store_is_refused_rather_than_queued(client):
    """Queueing would leave HQ showing a change nobody has confirmed, for as
    long as the shop stays off - and the row would outlive the reason."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK])

    response = client.post(f"/api/stores/{store_id}/audio-output",
                           headers=headers,
                           json={"selector": REALTEK["verified_selector"]})
    assert response.status_code == 409
    assert "offline" in response.json()["detail"]


def test_refreshing_an_offline_store_says_the_list_is_old(client):
    headers = sign_in(client)
    store_id = make_store(client, headers)
    response = client.post(f"/api/stores/{store_id}/audio-output/refresh",
                           headers=headers)
    assert response.status_code == 409
    assert "last time it was connected" in response.json()["detail"]


def test_a_malformed_report_does_not_break_the_store(client):
    """The same socket is carrying that Store's broadcast audio."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    for rubbish in ({"type": "output_devices", "devices": "not a list"},
                    {"type": "output_devices", "devices": [None, 7]},
                    {"type": "output_device_result", "result": "made up"},
                    {"type": "output_device_result"}):
        client.server_module._handle_output_device_report(store_id, rubbish)
    state = client.get(f"/api/stores/{store_id}/audio-output",
                       headers=headers).json()
    assert state["devices"] == []


# ===========================================================================
# Authorization
# ===========================================================================

def test_changing_a_speaker_is_its_own_permission(client):
    """It was only ever possible standing at the Store PC, where whoever could
    get it wrong could also hear the result. That protection is gone, so this
    is not bundled into managing Devices."""
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK])
    pretend_online(client, store_id)
    make_user(client, headers, "voice", "BROADCASTER")
    broadcaster = sign_in(client, "voice")

    # A broadcaster may look - Receiver Status is part of the job.
    assert client.get(f"/api/stores/{store_id}/audio-output",
                      headers=broadcaster).status_code == 200
    # But not change it.
    assert client.post(f"/api/stores/{store_id}/audio-output",
                       headers=broadcaster,
                       json={"selector": REALTEK["verified_selector"]}
                       ).status_code == 403
    assert client.post(f"/api/stores/{store_id}/audio-output/refresh",
                       headers=broadcaster).status_code == 403


def test_a_viewer_cannot_change_a_speaker(client):
    headers = sign_in(client)
    store_id = make_store(client, headers)
    report_devices(client, store_id, [REALTEK])
    pretend_online(client, store_id)
    make_user(client, headers, "watcher", "VIEWER")
    viewer = sign_in(client, "watcher")

    assert client.post(f"/api/stores/{store_id}/audio-output", headers=viewer,
                       json={"selector": REALTEK["verified_selector"]}
                       ).status_code == 403
