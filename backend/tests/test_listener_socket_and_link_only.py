"""The listener socket's contract, and what Only With Link really does.

Two things are being pinned down here.

First, that the listener socket is a one-way pipe with a doorbell. A listener
receives audio and may say only that it is still there and what its browser is
doing. Anything else - audio, a Store command, an unparseable frame - closes the
connection rather than being ignored, so a client cannot probe for a message
type that happens to be tolerated.

Second, that Only With Link genuinely has no physical destination: no target
rows, no Store lease, no PREPARE. Those are absences, and an absence is only
proved by looking for it.
"""

from __future__ import annotations

import json
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

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CAPTURE = FIXTURE_DIR / "mediarecorder-live.webm"
CHUNK_INDEX = FIXTURE_DIR / "mediarecorder-live.chunks.json"

requires_capture = pytest.mark.skipif(
    not CAPTURE.exists() or not CHUNK_INDEX.exists(),
    reason="run: SPEAKLINK_CAPTURE_WEBM=1 npx playwright test e2e/capture-fixture.spec.js",
)


def timeslice_chunks() -> list[bytes]:
    sizes = json.loads(CHUNK_INDEX.read_text())["chunkSizes"]
    data = CAPTURE.read_bytes()
    chunks, offset = [], 0
    for size in sizes:
        chunks.append(data[offset:offset + size])
        offset += size
    return chunks


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPEAKLINK_LAN_HTTP_LISTENERS", "1")

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "web_rooms",
                               "web_participant_runtime")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username: str = "founder"):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client)


def link_only_session(client, headers, campaign="Link only"):
    response = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "only_with_link"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def room_of(client, headers, sid):
    return client.get(f"/api/broadcast/sessions/{sid}/web-room",
                      headers=headers).json()


def go_live(client, headers, sid):
    started = client.post(f"/api/broadcast/sessions/{sid}/start", headers=headers)
    assert started.status_code == 200, started.text
    return started.json()


def admit(client, room):
    joined = client.post(f"/api/listen/rooms/{room['public_code']}/join",
                         json={"display_name": "Harshit",
                               "password": room["password"]})
    assert joined.status_code == 200, joined.text
    return joined


def feed_audio(client, sid, chunk_count=8):
    """Push real audio through the session's relay, as the broadcaster would."""
    relay = client.server_module.manager.broadcasts.web_relay(sid)
    assert relay is not None, "a live Broadcast owns a relay"
    for chunk in timeslice_chunks()[:chunk_count]:
        relay.offer(chunk)
    return relay


# ===========================================================================
# Only With Link: the absences
# ===========================================================================

def test_link_only_creates_a_session_with_no_physical_destination(client, owner):
    sid = link_only_session(client, owner)
    server = client.server_module

    with server.SessionLocal() as db:
        targets = db.query(server.BroadcastTarget).filter(
            server.BroadcastTarget.session_id == sid).all()
    assert targets == [], "Only With Link creates ZERO target rows"

    session = client.get(f"/api/broadcast/sessions/{sid}", headers=owner)
    if session.status_code == 200:
        assert session.json()["selected_store_count"] == 0


def test_starting_link_only_claims_no_store_and_sends_no_prepare(client, owner):
    """The absence of a lease and of a PREPARE is the whole point of this mode."""
    sid = link_only_session(client, owner)
    server = client.server_module

    sent: list[tuple] = []
    original = server.manager.send_to_receiver

    async def record(store_id, message):
        sent.append((store_id, message))
        return await original(store_id, message)

    server.manager.send_to_receiver = record
    try:
        go_live(client, owner, sid)
    finally:
        server.manager.send_to_receiver = original

    assert sent == [], "no Receiver was contacted"

    with server.engine.connect() as connection:
        leases = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM broadcast_store_leases WHERE session_id = ?",
            (sid,)).fetchone()
    assert leases[0] == 0, "no Store lease was claimed"

    # And it is genuinely live, not a half-started session.
    assert client.get("/api/broadcast/current",
                      headers=owner).json()["live"] is True


def test_a_live_link_only_broadcast_has_no_store_audio_destinations(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    live = client.server_module.manager.broadcasts.get(sid)
    assert live is not None
    assert set(live.target_store_ids) == set(), "no Store receives this audio"
    assert live.web_relay is not None, "but the web room is fully wired"


def test_link_only_records_and_appears_in_history(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    feed_audio(client, sid)

    stopped = client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)
    assert stopped.status_code == 200, stopped.text

    history = client.get("/api/broadcast/history", headers=owner)
    assert history.status_code == 200
    rows = history.json()
    items = rows["items"] if isinstance(rows, dict) else rows
    mine = next(row for row in items if row["id"] == sid)
    # Truthfully zero Stores, and NOT a failed Broadcast.
    assert mine["selected_store_count"] == 0
    assert mine["status"] in ("ended", "completed")
    assert mine["target_mode"] == "only_with_link"


def test_a_broadcaster_without_physical_delivery_can_run_link_only(client, owner):
    """The whole reason the permission was split."""
    created = client.post("/api/users", headers=owner, json={
        "username": "linkonly", "display_name": "Link Only",
        "role": "BROADCASTER", "password": PASSWORD})
    user_id = created.json()["id"]
    assert client.put(f"/api/users/{user_id}/permissions", headers=owner, json={
        "changes": [{"code": "broadcast.store_delivery", "effect": "DENY"}]
    }).status_code == 200
    headers = sign_in(client, "linkonly")

    sid = link_only_session(client, headers, "Their link only")
    go_live(client, headers, sid)
    room = room_of(client, headers, sid)
    assert room["public_code"]

    # ...and still cannot reach a Store by crafting a request.
    assert client.get("/api/broadcast/target-stores",
                      headers=headers).status_code == 403
    assert client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "crafted", "target_mode": "all"}).status_code == 403


# ===========================================================================
# The listener socket
# ===========================================================================

@requires_capture
def test_an_admitted_listener_receives_the_bootstrap_then_live_audio(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    with client.websocket_connect("/api/listen/ws") as socket:
        opening = json.loads(socket.receive_text())
        assert opening["type"] == "bootstrap"
        assert opening["mime"] == "audio/webm;codecs=opus"

        init = socket.receive_bytes()
        # The initialization segment, and definitely not a Cluster.
        assert init.startswith(bytes([0x1A, 0x45, 0xDF, 0xA3]))
        for _ in range(opening["clusters"]):
            cluster = socket.receive_bytes()
            assert cluster.startswith(bytes([0x1F, 0x43, 0xB6, 0x75]))


@requires_capture
def test_a_listener_may_heartbeat_and_nothing_else(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    with client.websocket_connect("/api/listen/ws") as socket:
        opening = json.loads(socket.receive_text())
        socket.receive_bytes()
        for _ in range(opening["clusters"]):
            socket.receive_bytes()

        socket.send_text(json.dumps({"type": "heartbeat",
                                     "playback_state": "LISTENING"}))
        # Accepted: the console now shows this listener as listening.
        state = client.get(f"/api/broadcast/sessions/{sid}/web-room", headers=owner)
        assert state.json()["counts"]["listening"] == 1


@requires_capture
@pytest.mark.parametrize("hostile", [
    json.dumps({"type": "stop"}),
    json.dumps({"type": "set_audio_control", "volume_percent": 100}),
    json.dumps({"type": "approve", "participant_id": 1}),
    json.dumps(["not", "an", "object"]),
    "not json at all",
    "x" * 4096,
])
def test_any_other_message_closes_the_socket(client, owner, hostile):
    """Ignoring an unknown frame would let a client probe for a tolerated one."""
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            opening = json.loads(socket.receive_text())
            socket.receive_bytes()
            for _ in range(opening["clusters"]):
                socket.receive_bytes()
            socket.send_text(hostile)
            socket.receive_bytes()          # the close arrives here


@requires_capture
def test_a_listener_cannot_publish_audio(client, owner):
    """There is no listener microphone, and trying to be one ends the call."""
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            opening = json.loads(socket.receive_text())
            socket.receive_bytes()
            for _ in range(opening["clusters"]):
                socket.receive_bytes()
            socket.send_bytes(b"\x1a\x45\xdf\xa3 pretending to be a broadcaster")
            socket.receive_bytes()


def test_a_socket_without_a_listener_session_is_refused(client, owner):
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            socket.receive_text()


def test_a_waiting_participant_has_no_socket(client, owner):
    """No audio before admission, however long they wait."""
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    client.post(f"/api/listen/rooms/{room['public_code']}/request-access",
                json={"display_name": "Aman"})

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            socket.receive_text()


@requires_capture
def test_a_kicked_listener_cannot_reconnect(client, owner):
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)
    pid = room_of(client, owner, sid)["listeners"][0]["id"]

    assert client.post(
        f"/api/broadcast/sessions/{sid}/web-participants/{pid}/kick",
        headers=owner).status_code == 200

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            socket.receive_text()


@requires_capture
def test_a_listener_of_an_ended_broadcast_cannot_connect(client, owner):
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)
    assert client.post(f"/api/broadcast/sessions/{sid}/stop",
                       headers=owner).status_code == 200

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            socket.receive_text()


def test_a_listener_cannot_connect_before_the_broadcast_is_live(client, owner):
    """The link can be shared early. The audio does not leak early."""
    from starlette.websockets import WebSocketDisconnect

    sid = link_only_session(client, owner)
    room = room_of(client, owner, sid)
    admit(client, room)          # admitted while the session is still pending

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/listen/ws") as socket:
            socket.receive_text()


@requires_capture
def test_stopping_the_broadcast_ends_the_room(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)

    ended = room_of(client, owner, sid)
    assert ended["status"] == "ENDED"
    # And the public code stops resolving, so a shared link does not linger.
    assert client.get(f"/api/listen/rooms/{room['public_code']}").status_code == 404


@requires_capture
def test_emergency_stop_ends_the_room_too(client, owner):
    sid = link_only_session(client, owner)
    go_live(client, owner, sid)
    room = room_of(client, owner, sid)
    admit(client, room)
    feed_audio(client, sid)

    emergency = client.post("/api/broadcast/emergency-stop", headers=owner)
    assert emergency.status_code == 200, emergency.text
    assert room_of(client, owner, sid)["status"] == "ENDED"
