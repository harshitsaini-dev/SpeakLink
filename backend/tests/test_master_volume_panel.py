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
                               "store_master_audio", "store_audio_target",
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
    import target_enforcement
    target_enforcement.policy._stores.clear()
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


def _record_sends(server):
    """Capture what would have gone to a Receiver.

    Needed wherever a test drives an ONLINE Store: the real send would write to
    the fixture's stand-in socket, fail, and drop the Receiver - so the Store
    would quietly go offline mid-test and every later assertion would be about
    the wrong situation.
    """
    sent = []

    async def record(store_id, message):
        sent.append(message)

    server.manager.send_to_receiver = record
    return sent


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
# TARGET state: always settable, online or offline
# ===========================================================================
def test_an_offline_store_can_still_be_given_a_desired_volume(client, owner):
    """The defect this whole rework exists to fix.

    A manager deciding a shop should be at 70% is not blocked by that shop's PC
    being switched off. The instruction is real; only its arrival is pending.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)          # never brought online

    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"volume_percent": 70})
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["target_volume_percent"] == 70
    assert row["sync_state"] == "WAITING_FOR_SYNC"


def test_an_offline_store_can_still_be_muted(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"muted": True})
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["target_muted"] is True
    assert row["sync_state"] == "WAITING_FOR_SYNC"


def test_an_offline_instruction_never_claims_to_be_applied(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35)
    go_offline(client, store)

    body = client.post(f"/api/store-audio/master/{store}", headers=owner,
                       json={"volume_percent": 70}).text
    assert "applied" not in body.lower()
    row = panel(client, owner)[store]
    # TARGET moved. ACTUAL did not - nothing reached the machine.
    assert row["target_volume_percent"] == 70
    assert row["volume_percent"] == 35
    assert row["sync_state"] == "WAITING_FOR_SYNC"


def test_a_store_that_never_reported_can_still_be_given_a_setting(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    row = panel(client, owner)[store]
    assert row["target_volume_percent"] == 70
    # And ACTUAL stays honestly unknown rather than inheriting the intention.
    assert row["volume_percent"] is None
    assert row["muted"] is None


def test_the_latest_instruction_wins_and_no_queue_is_built(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    for level in (20, 40, 50, 70):
        client.post(f"/api/store-audio/master/{store}", headers=owner,
                    json={"volume_percent": level})

    import store_audio_target
    from db import engine
    from sqlalchemy import text

    assert store_audio_target.get_target(
        engine, store_id=store).volume_percent == 70
    with engine.connect() as connection:
        count = connection.execute(text(
            f"SELECT COUNT(*) FROM {store_audio_target.TARGET_TABLE} "
            "WHERE store_id = :store_id"), {"store_id": store}).scalar()
    assert count == 1, "a queue would have grown; latest-wins is a PRIMARY KEY"


def test_a_partial_instruction_keeps_the_rest_of_the_intention(client, owner):
    """Pressing Mute says nothing about the level the operator chose earlier."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"muted": True})
    row = panel(client, owner)[store]
    assert row["target_volume_percent"] == 70
    assert row["target_muted"] is True


def test_the_desired_state_survives_a_restart(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    import store_audio_target
    from db import engine
    # Read through a fresh connection, as a restarted process would.
    assert store_audio_target.all_target(engine)[store].volume_percent == 70


def test_the_desired_state_carries_no_secret(client, owner):
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    import store_audio_target
    from db import engine
    desired = store_audio_target.get_target(engine, store_id=store)
    assert desired.device_id == device_id
    assert desired.created_by is not None
    body = str(desired.as_dict()).lower()
    for leak in ("password", "token", "secret", "credential", "bearer", "jwt"):
        assert leak not in body, leak


def test_clearing_the_setting_removes_hqs_opinion(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    response = client.delete(f"/api/store-audio/master/{store}/pending",
                             headers=owner)
    assert response.status_code == 200, response.text
    row = {r["store_id"]: r for r in response.json()["stores"]}[store]
    assert row["target_volume_percent"] is None
    assert row["sync_state"] == "NO_TARGET_STATE"


def test_requesting_nothing_is_refused(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    assert client.post(f"/api/store-audio/master/{store}",
                       headers=owner, json={}).status_code == 400


# ===========================================================================
# Sync state: the relationship between desired and actual
# ===========================================================================
def test_matching_desired_and_actual_is_synced(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=70, sequence=1)     # the Receiver's readback
    assert panel(client, owner)[store]["sync_state"] == "SYNCED"


def test_a_store_local_change_reads_as_out_of_sync_not_applying(client, owner):
    """Nothing is being applied - a member of staff moved the slider.

    Calling this APPLYING would promise the operator that HQ was about to do
    something about it, and HQ deliberately does not fight Store staff.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    # The Receiver's readback matching the target state is what proves the
    # command landed, exactly as the server treats it.
    observe(store, volume=70, sequence=1)
    client.server_module._IN_FLIGHT_COMMANDS.pop(store, None)
    observe(store, volume=25, sequence=2)     # somebody at the till

    row = panel(client, owner)[store]
    assert row["volume_percent"] == 25
    assert row["target_volume_percent"] == 70
    assert row["sync_state"] == "OUT_OF_SYNC"


def test_a_store_local_change_never_provokes_a_corrective_command(client, owner):
    """No feedback fight with the people in the shop."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    sent = []
    manager = client.server_module.manager

    async def record(store_id, message):
        sent.append(message)
    manager.send_to_receiver = record

    for step, level in enumerate((60, 50, 40, 25), start=2):
        observe(store, volume=level, sequence=step)
        panel(client, owner)
    assert sent == []


def test_store_local_telemetry_never_changes_the_desired_state(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=25, sequence=2)
    assert panel(client, owner)[store]["target_volume_percent"] == 70


def test_a_difference_on_an_offline_store_is_waiting_not_out_of_sync(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    observe(store, volume=35, sequence=1)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    go_offline(client, store)
    assert panel(client, owner)[store]["sync_state"] == "WAITING_FOR_SYNC"


def test_a_store_with_no_hq_setting_says_so(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=35)
    assert panel(client, owner)[store]["sync_state"] == "NO_TARGET_STATE"


# ===========================================================================
# Reconnect
# ===========================================================================
class _ReadyCapabilities:
    output_volume = True
    output_control_status = "ready"


class _ReadySnapshot:
    capabilities = _ReadyCapabilities()


def test_reconnect_applies_the_desired_state_when_it_differs(client, owner):
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = _record_sends(server)

    # Not controllable yet: nothing may be applied to an output that is not there.
    go_online(client, store, endpoint_status="needs_output_selection")
    asyncio.run(server._apply_target_master_volume(store, device_id))
    assert sent == []

    server.manager.get_receiver_snapshot = lambda _sid: _ReadySnapshot()
    go_online(client, store, endpoint_status="ready")
    # The Receiver reports what Windows ACTUALLY is, first.
    observe(store, volume=35, sequence=1)

    asyncio.run(server._apply_target_master_volume(store, device_id))
    assert len(sent) == 1
    assert sent[0]["volume_percent"] == 70
    assert sent[0]["session_id"] is None


def test_reconnect_sends_nothing_when_the_store_is_already_right(client, owner):
    """No pointless traffic, and no misleading flash of APPLYING."""
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = _record_sends(server)
    server.manager.get_receiver_snapshot = lambda _sid: _ReadySnapshot()
    go_online(client, store, endpoint_status="ready")
    observe(store, volume=70, sequence=1)     # already where HQ wants it

    asyncio.run(server._apply_target_master_volume(store, device_id))
    assert sent == []
    assert panel(client, owner)[store]["sync_state"] == "SYNCED"


def test_reconnect_does_not_mark_desired_as_actual(client, owner):
    """Sending is not applying, and applying is not reading Windows back."""
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    _record_sends(server)
    server.manager.get_receiver_snapshot = lambda _sid: _ReadySnapshot()
    go_online(client, store, endpoint_status="ready")
    observe(store, volume=35, sequence=1)
    asyncio.run(server._apply_target_master_volume(store, device_id))

    row = panel(client, owner)[store]
    assert row["volume_percent"] == 35, "ACTUAL only ever comes from a readback"
    assert row["sync_state"] == "APPLYING"


def test_a_replaced_device_does_not_inherit_the_old_setting(client, owner):
    """The machine changed; the instruction was not about this one."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = _record_sends(server)
    go_online(client, store, endpoint_status="ready")

    asyncio.run(server._apply_target_master_volume(store, 999_999))
    assert sent == []

    import store_audio_target
    from db import engine
    desired = store_audio_target.get_target(engine, store_id=store)
    # The intention SURVIVES - it is still what the operator wants - but it is
    # honestly marked as not having reached anything.
    assert desired is not None
    assert desired.volume_percent == 70
    assert "Device changed" in desired.last_error
    assert panel(client, owner)[store]["sync_state"] == "SYNC_FAILED"


def test_a_failed_apply_is_reported_honestly_and_keeps_the_setting(client, owner):
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module

    async def explode(store_id, message):
        raise RuntimeError("the socket went away")

    server.manager.send_to_receiver = explode
    server.manager.get_receiver_snapshot = lambda _sid: _ReadySnapshot()
    go_online(client, store, endpoint_status="ready")
    observe(store, volume=35, sequence=1)
    asyncio.run(server._apply_target_master_volume(store, device_id))

    row = panel(client, owner)[store]
    assert row["sync_state"] == "SYNC_FAILED"
    assert row["target_volume_percent"] == 70, "the operator still wants this"
    assert row["volume_percent"] == 35, "and the shop is still where it was"


# ===========================================================================
# Permission and Store Scope
# ===========================================================================
def test_an_account_without_the_permission_cannot_read_the_panel(client, owner):
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, caster_id,
                        "store_audio.control", "DENY").status_code == 200
    caster = sign_in(client, "caster")
    assert client.get("/api/store-audio/master", headers=caster).status_code == 403


def test_an_account_without_the_permission_cannot_set_a_desired_state(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    set_override(client, owner, caster_id, "store_audio.control", "DENY")
    caster = sign_in(client, "caster")
    assert client.post(f"/api/store-audio/master/{store}", headers=caster,
                       json={"volume_percent": 10}).status_code == 403


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


def test_store_a_cannot_be_set_from_store_bs_scope(client, owner):
    ids = store_ids(client, owner, count=2)
    for store in ids:
        install_receiver(client, store)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    set_scope(client, owner, caster_id, [ids[0]])
    caster = sign_in(client, "caster")

    # 404, not 403: distinguishing the two would let this route enumerate the
    # estate from outside the caller's own scope.
    assert client.post(f"/api/store-audio/master/{ids[1]}", headers=caster,
                       json={"volume_percent": 10}).status_code == 404
    import store_audio_target
    from db import engine
    assert store_audio_target.get_target(engine, store_id=ids[1]) is None


# ===========================================================================
# Endpoint honesty and staleness
# ===========================================================================
def test_an_older_reading_cannot_drag_the_panel_backwards(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    observe(store, volume=20, sequence=9)
    assert observe(store, volume=80, sequence=4) is None
    assert panel(client, owner)[store]["volume_percent"] == 20


def test_an_unavailable_endpoint_says_so(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="unavailable")
    assert panel(client, owner)[store]["control_status"] == "OUTPUT_UNAVAILABLE"


def test_an_unconfigured_endpoint_asks_for_a_selection(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")
    assert panel(client, owner)[store]["control_status"] == "NEEDS_OUTPUT_SELECTION"


def test_a_desired_state_can_be_set_even_with_no_usable_output(client, owner):
    """The operator's intention is still recorded and still honest.

    The endpoint being unusable is a Store problem to fix, not a reason to
    refuse to remember what the shop should be set to.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")

    sent = _record_sends(client.server_module)

    response = client.post(f"/api/store-audio/master/{store}",
                           headers=owner, json={"volume_percent": 50})
    assert response.status_code == 200, response.text
    assert panel(client, owner)[store]["target_volume_percent"] == 50
    # Nothing was sent to an output that cannot be controlled.
    assert sent == []


def test_offline_outranks_every_other_reason(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store, endpoint_status="needs_output_selection")
    go_offline(client, store)
    assert panel(client, owner)[store]["control_status"] == "OFFLINE"


# ===========================================================================
# Broadcast ownership
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


def test_another_operators_broadcast_refuses_and_records_nothing(client, owner):
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
        # And no target state was quietly banked to be applied the moment the
        # broadcast ends - that is the operator's decision to make afterwards.
        import store_audio_target
        from db import engine
        assert store_audio_target.get_target(engine, store_id=store) is None
    finally:
        registry.end_session(4243)


def test_the_broadcast_owner_controls_through_the_broadcasts_own_authority(client, owner):
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
        assert registry.state_for(4244, store).requested_volume_percent == 55
    finally:
        registry.end_session(4244)


def test_a_broadcast_does_not_apply_the_desired_state_on_reconnect(client, owner):
    """The single-writer rule outranks HQ's standing intention."""
    store = store_ids(client, owner)[0]
    device_id = install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    server = client.server_module
    sent = _record_sends(server)
    go_online(client, store, endpoint_status="ready")

    from store_audio_control import registry
    registry.start_session(session_id=4245, owner_user_id=1, store_ids=[store])
    try:
        asyncio.run(server._apply_target_master_volume(store, device_id))
        assert sent == []
    finally:
        registry.end_session(4245)


def test_restoration_is_not_overwritten_by_the_desired_state(client, owner):
    """STOP restores the ORIGINAL. Nothing here may overwrite it.

    Idle 25% muted, HQ wants 70, broadcast runs at 80, STOP puts back 25%
    muted. The target state is still 70 and the panel says so - but the shop
    is at 25% and is reported at 25%, because restoration is authoritative and
    the target state is not applied in the same cleanup path.
    """
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    observe(store, volume=25, muted=True, sequence=1)          # idle original
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    client.server_module._IN_FLIGHT_COMMANDS.pop(store, None)
    observe(store, volume=80, muted=False, sequence=2)         # broadcast level
    observe(store, volume=25, muted=True, sequence=3)          # STOP restored it

    row = panel(client, owner)[store]
    assert row["volume_percent"] == 25
    assert row["muted"] is True
    assert row["target_volume_percent"] == 70
    # Honest, and NOT a promise that anything is about to change it back.
    assert row["sync_state"] == "OUT_OF_SYNC"


# ===========================================================================
# Runtime discipline, and what is never claimed
# ===========================================================================
def test_live_telemetry_writes_no_database_row(client, owner):
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
                              "store_audio_target_state")
            }

    before = row_counts()
    for step in range(1, 41):
        observe(store, volume=step, sequence=step)
    assert row_counts() == before


def test_the_summary_separates_intention_from_reality(client, owner):
    ids = store_ids(client, owner, count=3)
    for store in ids:
        install_receiver(client, store)
    _record_sends(client.server_module)

    go_online(client, ids[0])
    client.post(f"/api/store-audio/master/{ids[0]}", headers=owner,
                json={"volume_percent": 70})
    observe(ids[0], volume=70, sequence=1)                 # synced

    go_online(client, ids[1])
    client.post(f"/api/store-audio/master/{ids[1]}", headers=owner,
                json={"volume_percent": 70})
    observe(ids[1], volume=70, sequence=1)
    go_offline(client, ids[1])
    observe(ids[1], volume=30, sequence=2)                 # drifted while away

    client.post(f"/api/store-audio/master/{ids[2]}", headers=owner,
                json={"muted": True})                      # offline, desired mute

    summary = client.get("/api/store-audio/master/summary", headers=owner).json()
    assert summary["installed"] == 3
    assert summary["online"] == 1
    assert summary["synced"] == 1
    assert summary["waiting_for_sync"] == 2
    assert summary["target_muted"] == 1


def test_no_mixer_state_ever_claims_the_speakers_were_heard(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    go_online(client, store)
    _record_sends(client.server_module)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=70, sequence=1)

    body = client.get("/api/store-audio/master", headers=owner).text.lower()
    for claim in ("playback_confirmed", "speaker_verified", "audible", "verified"):
        assert claim not in body, claim


# ===========================================================================
# Target enforcement, through the server
# ===========================================================================
def _ready(client, store):
    """An online Store with a controllable output, as the sweep requires."""
    go_online(client, store, endpoint_status="ready")
    client.server_module.manager.get_receiver_snapshot = \
        lambda _sid: _ReadySnapshot()


def test_the_sweep_puts_a_drifted_store_back_to_its_target(client, owner):
    import target_enforcement

    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    sent = _record_sends(server)
    _ready(client, store)

    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=70, sequence=1)             # synced
    sent.clear()

    # Somebody at the till turns it down, and leaves it alone.
    observe(store, volume=25, sequence=2)
    target_enforcement.policy.note_reading(
        store_id=store, matches_target=False, now=0.0)
    server.target_enforcement.policy._entry(store).last_drift_at = 0.0

    import time as _time
    original = _time.monotonic
    try:
        _time.monotonic = lambda: 1000.0          # well past the debounce
        outcomes = asyncio.run(server._enforce_targets_once())
    finally:
        _time.monotonic = original

    assert any(o["store_id"] == store and o["enforced"] for o in outcomes)
    assert len(sent) == 1
    assert sent[0]["volume_percent"] == 70
    assert sent[0]["session_id"] is None


def test_the_sweep_leaves_a_synced_store_alone(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    sent = _record_sends(server)
    _ready(client, store)

    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=70, sequence=1)
    sent.clear()

    outcomes = asyncio.run(server._enforce_targets_once())
    assert sent == []
    assert [o["reason"] for o in outcomes if o["store_id"] == store] == \
        ["already at target"]


def test_the_sweep_never_touches_an_offline_store(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    sent = _record_sends(server)

    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    outcomes = asyncio.run(server._enforce_targets_once())
    assert sent == []
    assert [o["reason"] for o in outcomes if o["store_id"] == store] == \
        ["receiver offline"]


def test_the_sweep_never_touches_a_store_a_broadcast_owns(client, owner):
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    sent = _record_sends(server)
    _ready(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=25, sequence=1)
    sent.clear()

    from store_audio_control import registry
    registry.start_session(session_id=7001, owner_user_id=1, store_ids=[store])
    try:
        outcomes = asyncio.run(server._enforce_targets_once())
        assert sent == []
        assert [o["reason"] for o in outcomes if o["store_id"] == store] == \
            ["a broadcast owns this Store"]
    finally:
        registry.end_session(7001)


def test_a_store_that_will_not_stay_put_is_reported_as_suspended(client, owner):
    """An honest end to a disagreement, visible on the panel."""
    import target_enforcement

    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    _record_sends(server)
    _ready(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=25, sequence=1)

    for moment in (100.0, 200.0, 300.0):
        target_enforcement.policy.note_enforced(store_id=store, now=moment)

    assert panel(client, owner)[store]["sync_state"] == "ENFORCEMENT_SUSPENDED"


def test_setting_a_new_target_resumes_enforcement(client, owner):
    import target_enforcement

    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    _record_sends(server)
    _ready(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    for moment in (100.0, 200.0, 300.0):
        target_enforcement.policy.note_enforced(store_id=store, now=moment)
    assert target_enforcement.policy.is_suspended(store) is True

    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 55})
    assert target_enforcement.policy.is_suspended(store) is False


def test_enforcement_is_held_off_after_a_broadcast_restores_a_store(client, owner):
    """STOP must be seen to restore. Nothing may drag it away immediately."""
    import target_enforcement

    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    server = client.server_module
    sent = _record_sends(server)
    _ready(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})
    observe(store, volume=25, muted=True, sequence=1)      # restored original
    sent.clear()

    import time as _time
    target_enforcement.policy.note_broadcast_restored(
        store_id=store, now=_time.monotonic())

    outcomes = asyncio.run(server._enforce_targets_once())
    assert sent == []
    assert [o["reason"] for o in outcomes if o["store_id"] == store] == \
        ["restoring after a broadcast"]


def test_a_retired_device_is_never_sent_a_target(client, owner):
    """A replaced machine is not this Store's Receiver."""
    store = store_ids(client, owner)[0]
    install_receiver(client, store)
    client.post(f"/api/store-audio/master/{store}", headers=owner,
                json={"volume_percent": 70})

    # The Device is retired, so the Store stops being an installed one.
    from db import engine
    from sqlalchemy import text
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE receiver_devices SET status = 'retired', "
            "disabled_at = :now WHERE store_id = :store_id"),
            {"now": "2026-08-06T00:00:00+00:00", "store_id": store})

    assert store not in panel(client, owner)
    server = client.server_module
    sent = _record_sends(server)
    asyncio.run(server._enforce_targets_once())
    assert sent == []
