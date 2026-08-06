"""The Master Volume panel: what it shows, and what it refuses to claim.

THE FEATURE

An operator needs to see and set every shop's Windows master volume without
starting a broadcast. Setting a Store's volume is done before opening, after a
complaint, or when somebody notices a shop has been left muted since Friday -
almost never with an announcement on air.

WHAT THESE TESTS ARE REALLY GUARDING

Truthfulness, mostly. The panel deals in three genuinely different things that
all look like "the volume" on screen:

    what the Store IS      - a live reading, only while it is connected
    what it WAS            - a memory, once it goes offline
    what we WANT it to be  - a pending change that has not happened yet

Nearly every test below exists because collapsing any two of those would
produce a confident, plausible, wrong number in front of an operator.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import asyncio

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
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "store_audio_control",
                               "store_master_audio", "store_audio_pending",
                               "master_volume_api")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made

    # These registries are process-wide singletons on purpose - a Store's
    # connection state and its mixer are facts about the machine, not about a
    # request. That makes them leak between tests unless each one puts them
    # back, and a leaked "online" Store silently turns an offline assertion
    # into a passing one for the wrong reason.
    server_module.manager.receivers.clear()
    server_module.manager.receiver_snapshots.clear()
    # Several tests replace a manager method to observe what would have been
    # sent. Assigning to the instance SHADOWS the class method, and the manager
    # outlives the test - so a stub left behind silences the next test's real
    # sends without failing anything.
    for stubbed in ("send_to_receiver", "get_receiver_snapshot"):
        server_module.manager.__dict__.pop(stubbed, None)
    import store_master_audio
    store_master_audio.registry._data.states.clear()
    from store_audio_control import registry as session_registry
    for session_id in list(session_registry.active_session_ids()):
        session_registry.end_session(session_id)


def sign_in(client, username: str, password: str = PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client, "founder")


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def store_ids(client, headers, count=3):
    rows = client.get("/api/stores", headers=headers).json()
    return [row["id"] for row in rows[:count]]


def install_receiver(client, store_id, *, status="active", primary=True,
                     device_id=None):
    """Give a Store a Receiver Device and make it the primary.

    Written against the tables directly rather than by driving enrolment: what
    is under test is which Devices the panel treats as installed, and a real
    enrolment would need codes, credentials and a socket, none of which this
    behaviour depends on.
    """
    from db import engine
    from sqlalchemy import text
    import receiver_primary_device

    receiver_primary_device.ensure_primary_device_schema(engine)
    with engine.begin() as connection:
        row = connection.execute(text(
            "INSERT INTO receiver_devices "
            "(public_id, store_id, display_name, status, enrolled_at, "
            " disabled_at, created_at, updated_at) "
            "VALUES (:public_id, :store_id, 'TEST-PC', :status, :now, "
            "        :disabled_at, :now, :now) "
            "RETURNING id"),
            # A real UUID: the table CHECKs the shape, and rightly so - a
            # Device's public id is quoted in support conversations.
            {"public_id": str(uuid4()),
             "store_id": store_id, "status": status,
             # The table refuses a retired Device with no disabled_at, and
             # rightly so: "retired since when" is part of what retired means.
             "disabled_at": (None if status == "active"
                             else "2026-08-06T00:00:00+00:00"),
             "now": "2026-08-06T00:00:00+00:00"}).fetchone()
        made = row[0]
        if primary:
            connection.execute(text(
                "INSERT INTO receiver_store_primary_device "
                "(store_id, device_id, promoted_at) "
                "VALUES (:store_id, :device_id, :now) "
                "ON CONFLICT(store_id) DO UPDATE SET device_id = excluded.device_id"),
                {"store_id": store_id, "device_id": made,
                 "now": "2026-08-06T00:00:00+00:00"})
    return made


def go_online(client, store_id, *, endpoint_status="ready"):
    """Mark a Store connected in the runtime registries the panel reads.

    ``is_receiver_online`` deliberately wants BOTH a socket and a CONNECTED
    snapshot - a socket alone is not evidence a Receiver is alive - so the
    fixture has to supply both rather than only the easy one.
    """
    import store_master_audio
    from receiver_contract import (
        ConnectionState, PlaybackState, ReadinessState, ReceiverSnapshot,
    )

    store_master_audio.registry.note_online(
        store_id=store_id, endpoint_status=endpoint_status)
    manager = client.server_module.manager
    manager.receivers[store_id] = object()
    manager.receiver_snapshots[store_id] = ReceiverSnapshot(
        connection=ConnectionState.CONNECTED,
        readiness=ReadinessState.UNKNOWN,
        playback=PlaybackState.STOPPED,
    )
    return store_master_audio.registry.state_for(store_id)


def go_offline(client, store_id):
    import store_master_audio
    store_master_audio.registry.note_offline(store_id=store_id)
    client.server_module.manager.receivers.pop(store_id, None)
    client.server_module.manager.receiver_snapshots.pop(store_id, None)


def observe(store_id, *, volume, muted=False, sequence=1):
    import store_master_audio
    return store_master_audio.registry.observe(
        store_id=store_id, state_sequence=sequence,
        volume_percent=volume, muted=muted)


def panel(client, headers):
    response = client.get("/api/store-audio/master", headers=headers)
    assert response.status_code == 200, response.text
    return {row["store_id"]: row for row in response.json()["stores"]}


def set_scope(client, headers, user_id, ids):
    return client.put(f"/api/users/{user_id}/store-scope", headers=headers,
                      json={"entries": [{"scope_type": "STORE", "store_id": sid}
                                        for sid in ids]})


def set_override(client, headers, user_id, code, effect):
    return client.put(f"/api/users/{user_id}/permissions", headers=headers,
                      json={"changes": [{"code": code, "effect": effect}]})


# ===========================================================================
# 1-3  Which Stores appear at all
# ===========================================================================
def test_an_installed_online_store_appears(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    assert store in panel(client, owner)


def test_an_installed_offline_store_still_appears(client, owner):
    """The whole point. A shop whose PC is off is the one worth looking at."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    row = panel(client, owner)[store]
    assert row["online"] is False
    assert row["control_status"] == "OFFLINE"


def test_a_store_whose_receiver_was_retired_does_not_appear(client, owner):
    """A replaced machine is not an installed Receiver.

    Sending a mixer command to a historical Device id would target something
    that is not in the shop any more.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store, status="retired")
    assert store not in panel(client, owner)


def test_a_store_with_no_receiver_at_all_does_not_appear(client, owner):
    store = store_ids(client, owner)[0]
    assert store not in panel(client, owner)


# ===========================================================================
# 4-7  Live state, and Store-local changes
# ===========================================================================
def test_an_online_store_shows_its_actual_volume(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35)

    row = panel(client, owner)[store]
    assert row["volume_percent"] == 35
    assert row["stale"] is False
    assert row["control_status"] == "ONLINE"


def test_an_online_store_shows_its_actual_mute(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35, muted=True)
    assert panel(client, owner)[store]["muted"] is True


def test_a_change_made_at_the_till_moves_the_panel(client, owner):
    """No broadcast, no HQ interaction, no refresh - the shop changed."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=70, sequence=1)
    assert panel(client, owner)[store]["volume_percent"] == 70

    observe(store, volume=20, sequence=2)
    assert panel(client, owner)[store]["volume_percent"] == 20


def test_a_mute_made_at_the_till_moves_the_panel(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=70, muted=False, sequence=1)
    observe(store, volume=70, muted=True, sequence=2)
    assert panel(client, owner)[store]["muted"] is True


# ===========================================================================
# 8-10  HQ control, and the readback rule
# ===========================================================================
def test_a_sent_command_does_not_by_itself_change_the_displayed_volume(client, owner):
    """"Currently 70%" may only ever come from the Receiver's readback.

    A command that has been sent is not a fact about a mixer. Echoing the
    request back would make a Store that is ignoring HQ look obedient.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35)

    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"volume_percent": 70})
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["volume_percent"] == 35, "the last READING, not the request"


def test_the_readback_is_what_updates_the_panel(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35, sequence=1)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    # The Receiver applied it and reported what Windows really says.
    observe(store, volume=70, sequence=2)
    assert panel(client, owner)[store]["volume_percent"] == 70


def test_a_mute_command_is_also_only_true_once_reported(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=50, muted=False, sequence=1)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"muted": True})
    assert panel(client, owner)[store]["muted"] is False
    observe(store, volume=50, muted=True, sequence=2)
    assert panel(client, owner)[store]["muted"] is True


def test_reading_the_panel_sends_no_command(client, owner):
    """No feedback loop: HQ must not answer its own telemetry.

    If reading the panel produced a command, HQ would hear the resulting
    reading, answer that, and never stop.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=42)

    sent = []
    manager = client.server_module.manager

    async def record(store_id, message):
        sent.append((store_id, message))
    manager.send_to_receiver = record

    for _ in range(5):
        panel(client, owner)
    assert sent == []


# ===========================================================================
# 11-15  Offline: last known, and pending on reconnect
# ===========================================================================
def test_an_offline_store_reports_its_reading_as_stale(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35)
    go_offline(client, store)

    row = panel(client, owner)[store]
    # The number is KEPT - knowing a shop was left at 35% is useful - but it is
    # flagged, so no client can render it as "currently".
    assert row["volume_percent"] == 35
    assert row["stale"] is True
    assert row["online"] is False
    assert row["control_status"] == "OFFLINE"


def test_an_offline_change_is_pending_and_never_applied(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)

    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"volume_percent": 70})
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["pending_volume_percent"] == 70
    assert row["pending_status"] == "pending"
    # It must NOT have become the displayed current value.
    assert row["volume_percent"] is None
    assert row["control_status"] == "OFFLINE"


def test_a_pending_change_is_stored_with_no_secret(client, owner):
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    import store_audio_pending
    from db import engine
    waiting = store_audio_pending.get_pending(engine, store_id=store)
    assert waiting.device_id == device_id
    assert waiting.created_by is not None
    body = str(waiting.as_dict()).lower()
    for leak in ("password", "token", "secret", "credential", "bearer", "jwt"):
        assert leak not in body, leak


def test_the_latest_offline_instruction_wins(client, owner):
    """Three requests are one wish, not a queue to replay at the shop."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    for level in (30, 50, 70):
        client.post(f"/api/store-audio/master/{store}", headers=owner,
                    json={"volume_percent": level})

    import store_audio_pending
    from db import engine
    assert store_audio_pending.get_pending(engine, store_id=store).volume_percent == 70
    with engine.connect() as connection:
        from sqlalchemy import text
        count = connection.execute(text(
            "SELECT COUNT(*) FROM store_audio_pending_commands "
            "WHERE store_id = :store_id"), {"store_id": store}).scalar()
    assert count == 1, "a queue would have grown; latest-wins is a PRIMARY KEY"


def test_cancel_removes_the_pending_change(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    response = client.delete(f"/api/store-audio/master/{store}/pending",
                             headers=owner)
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["pending_volume_percent"] is None
    assert row["pending_status"] is None


def test_requesting_nothing_is_refused(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={})
    assert response.status_code == 400


# ===========================================================================
# 16-19  Reconnect
# ===========================================================================
def test_a_pending_change_survives_a_restart(client, owner):
    """Its whole purpose is to outlive the disconnection."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    import store_audio_pending
    from db import engine
    # Read through a fresh connection, as a restarted process would.
    assert store_audio_pending.all_pending(engine)[store].volume_percent == 70


def test_reconnect_applies_the_pending_change_only_when_ready(client, owner):
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = []

    async def record(store_id, message):
        sent.append(message)
    server.manager.send_to_receiver = record

    # Not ready yet: the Receiver has not said it has a controllable output.
    go_online(client, store, endpoint_status="needs_output_selection")
    asyncio.run(server._apply_pending_master_volume(store, device_id))
    assert sent == [], "nothing may be applied to an output that is not there"

    # Now it reports a ready endpoint, and its actual state.
    go_online(client, store, endpoint_status="ready")

    class Capabilities:
        output_volume = True
        output_control_status = "ready"

    class Snapshot:
        capabilities = Capabilities()

    server.manager.get_receiver_snapshot = lambda _sid: Snapshot()
    asyncio.run(server._apply_pending_master_volume(store, device_id))
    assert len(sent) == 1
    assert sent[0]["volume_percent"] == 70
    assert sent[0]["session_id"] is None, "no broadcast owns this Store"


def test_a_pending_change_for_a_replaced_device_is_not_retargeted(client, owner):
    """The machine changed. The instruction was not about this one."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = []

    async def record(store_id, message):
        sent.append(message)
    server.manager.send_to_receiver = record
    go_online(client, store, endpoint_status="ready")

    asyncio.run(server._apply_pending_master_volume(store, 999_999))
    assert sent == []

    import store_audio_pending
    from db import engine
    waiting = store_audio_pending.get_pending(engine, store_id=store)
    assert waiting.status == "failed"
    assert "Device changed" in waiting.last_error


def test_a_failed_apply_keeps_the_pending_change(client, owner):
    """Clearing on attempt would make failure look exactly like success."""
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module

    async def explode(store_id, message):
        raise RuntimeError("the socket went away")
    server.manager.send_to_receiver = explode
    go_online(client, store, endpoint_status="ready")

    class Capabilities:
        output_volume = True
        output_control_status = "ready"

    class Snapshot:
        capabilities = Capabilities()

    server.manager.get_receiver_snapshot = lambda _sid: Snapshot()
    asyncio.run(server._apply_pending_master_volume(store, device_id))

    import store_audio_pending
    from db import engine
    waiting = store_audio_pending.get_pending(engine, store_id=store)
    assert waiting is not None, "the operator's wish has not been granted"
    assert waiting.status == "failed"


# ===========================================================================
# 20-22  Permission and Store Scope
# ===========================================================================
def test_an_account_without_the_permission_cannot_read_the_panel(client, owner):
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, caster_id,
                        "store_audio.control", "DENY").status_code == 200
    caster = sign_in(client, "caster")
    assert client.get("/api/store-audio/master", headers=caster).status_code == 403


def test_an_account_without_the_permission_cannot_control(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    set_override(client, owner, caster_id, "store_audio.control", "DENY")
    caster = sign_in(client, "caster")
    response = client.post(f"/api/store-audio/master/{store}",
                           headers=caster, json={"volume_percent": 10})
    assert response.status_code == 403


def test_a_scoped_operator_sees_only_their_own_stores(client, owner):
    ids = store_ids(client, owner, count=3)
    for store in ids:
        install_receiver(client, store)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_scope(client, owner, caster_id, [ids[0]]).status_code == 200
    caster = sign_in(client, "caster")

    visible = panel(client, caster)
    assert ids[0] in visible
    assert ids[1] not in visible and ids[2] not in visible


def test_store_a_cannot_be_controlled_from_store_bs_scope(client, owner):
    ids = store_ids(client, owner, count=2)
    for store in ids:
        install_receiver(client, store)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    set_scope(client, owner, caster_id, [ids[0]])
    caster = sign_in(client, "caster")

    response = client.post(f"/api/store-audio/master/{ids[1]}",
                           headers=caster, json={"volume_percent": 10})
    # 404, not 403: distinguishing "out of scope" from "does not exist" would
    # turn this route into a way of enumerating the estate.
    assert response.status_code == 404


def test_a_scoped_operator_cannot_cancel_another_stores_pending(client, owner):
    ids = store_ids(client, owner, count=2)
    for store in ids:
        install_receiver(client, store)
    client.post(f"/api/store-audio/master/{ids[1]}", headers=owner,
                json={"volume_percent": 70})
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    set_scope(client, owner, caster_id, [ids[0]])
    caster = sign_in(client, "caster")

    assert client.delete(f"/api/store-audio/master/{ids[1]}/pending",
                         headers=caster).status_code == 404
    import store_audio_pending
    from db import engine
    assert store_audio_pending.get_pending(engine, store_id=ids[1]) is not None


# ===========================================================================
# 23-27  Staleness and endpoint honesty
# ===========================================================================
def test_an_older_reading_cannot_drag_the_panel_backwards(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=20, sequence=9)
    assert observe(store, volume=80, sequence=4) is None
    assert panel(client, owner)[store]["volume_percent"] == 20


def test_a_repeated_sequence_is_ignored(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=20, sequence=5)
    assert observe(store, volume=99, sequence=5) is None
    assert panel(client, owner)[store]["volume_percent"] == 20


def test_an_unavailable_endpoint_says_so(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="unavailable")
    row = panel(client, owner)[store]
    assert row["control_status"] == "OUTPUT_UNAVAILABLE"


def test_an_unconfigured_endpoint_asks_for_a_selection(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")
    assert panel(client, owner)[store]["control_status"] == "NEEDS_OUTPUT_SELECTION"


def test_an_uncontrollable_endpoint_cannot_be_commanded(client, owner):
    """No default-endpoint fallback. Refusing beats moving the wrong output."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")
    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"volume_percent": 50})
    assert response.status_code == 409


def test_offline_outranks_every_other_reason(client, owner):
    """A shop that is off cannot be fixed by re-selecting its audio output."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")
    go_offline(client, store)
    assert panel(client, owner)[store]["control_status"] == "OFFLINE"


# ===========================================================================
# 28  Active broadcast ownership
# ===========================================================================
def test_a_store_owned_by_a_broadcast_says_so(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)

    from store_audio_control import registry
    registry.start_session(session_id=4242, owner_user_id=1, store_ids=[store])
    try:
        assert panel(client, owner)[store]["control_status"] == "CONTROLLED_BY_BROADCAST"
    finally:
        registry.end_session(4242)


def test_another_operators_broadcast_refuses_idle_control(client, owner):
    """Two writers must never race one Windows endpoint."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    other_id = make_user(client, owner, "caster", "BROADCASTER")

    from store_audio_control import registry
    registry.start_session(session_id=4243, owner_user_id=other_id,
                           store_ids=[store])
    try:
        response = client.post(f"/api/store-audio/master/{store}",
                               headers=owner, json={"volume_percent": 50})
        assert response.status_code == 409
        assert "broadcast" in response.json()["detail"].lower()
    finally:
        registry.end_session(4243)


def test_the_broadcast_owner_controls_through_the_broadcasts_own_authority(client, owner):
    """Routed through the existing channel, not a second competing one."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    founder_id = client.get("/api/auth/me", headers=owner).json()["id"]

    from store_audio_control import registry
    registry.start_session(session_id=4244, owner_user_id=founder_id,
                           store_ids=[store])
    try:
        response = client.post(f"/api/store-audio/master/{store}",
                               headers=owner, json={"volume_percent": 55})
        assert response.status_code == 200, response.text
        # The BROADCAST's registry holds the request - there is no separate
        # idle state that could disagree with it.
        assert registry.state_for(4244, store).requested_volume_percent == 55
    finally:
        registry.end_session(4244)


# ===========================================================================
# 29-32  Restoration, and what is NOT written to the database
# ===========================================================================
def test_live_telemetry_writes_no_database_row(client, owner):
    """A slider drag must not be a database load generator."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)

    from db import engine
    from sqlalchemy import text

    def row_counts():
        with engine.connect() as connection:
            return {
                table: connection.execute(
                    text(f"SELECT COUNT(*) FROM {table}")).scalar()
                for table in ("receiver_events", "system_logs",
                              "store_audio_pending_commands")
            }

    before = row_counts()
    for step in range(1, 41):
        observe(store, volume=step, sequence=step)
    assert row_counts() == before


def test_a_restored_store_reports_its_original_state_to_the_panel(client, owner):
    """After a broadcast puts a shop back, the panel shows what it was put to.

    Restoration is a real change to the mixer. If the panel kept showing the
    announcement level it would be describing a state that no longer exists.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=25, muted=True, sequence=1)      # idle, before
    observe(store, volume=80, muted=False, sequence=2)     # broadcast level
    observe(store, volume=40, muted=False, sequence=3)     # somebody at the till
    assert panel(client, owner)[store]["volume_percent"] == 40

    observe(store, volume=25, muted=True, sequence=4)      # STOP restored it
    row = panel(client, owner)[store]
    assert row["volume_percent"] == 25
    assert row["muted"] is True


def test_the_summary_counts_only_live_stores_as_muted(client, owner):
    """An offline shop left muted is not evidence that it is muted now."""
    ids = store_ids(client, owner, count=2)
    for store in ids:
        install_receiver(client, store)
    go_online(client, ids[0])
    observe(ids[0], volume=50, muted=True, sequence=1)
    go_online(client, ids[1])
    observe(ids[1], volume=50, muted=True, sequence=1)
    go_offline(client, ids[1])

    summary = client.get("/api/store-audio/master/summary", headers=owner).json()
    assert summary["installed"] == 2
    assert summary["online"] == 1
    assert summary["offline"] == 1
    assert summary["muted_online"] == 1


def test_the_summary_counts_pending_changes(client, owner):
    ids = store_ids(client, owner, count=2)
    for store in ids:
        install_receiver(client, store)
    client.post(f"/api/store-audio/master/{ids[0]}", headers=owner,
                json={"volume_percent": 70})
    summary = client.get("/api/store-audio/master/summary", headers=owner).json()
    assert summary["pending_changes"] == 1


def test_the_numeric_percentage_is_always_present_beside_its_class(client, owner):
    """The label is a scanning aid. The number stays authoritative."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=12)
    row = panel(client, owner)[store]
    assert row["level_class"] == "low"
    assert row["volume_percent"] == 12


def test_no_pending_row_or_log_line_carries_a_secret(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    logs = client.get("/api/logs", headers=owner)
    body = logs.text.lower() if logs.status_code == 200 else ""
    for leak in ("password", "bearer ", "jwt", "secret", "credential"):
        assert leak not in body, leak
