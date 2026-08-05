"""Store output volume over the real HTTP surface: who may, and who may not.

The registry tests next door prove the state machine. These prove the parts
that only exist once a request has an identity attached: the permission, the
ownership rule, Store Scope, and the fact that a 200 here means "sent" rather
than "the shop is quieter".
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "store_audio_control")]:
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


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def some_store_ids(client, headers, count=3):
    rows = client.get("/api/stores", headers=headers).json()
    return [row["id"] for row in rows[:count]]


def open_session(client, *, owner_user_id, store_ids, session_id=901):
    """Register live control state directly.

    Deliberately not by driving a real broadcast: starting one needs a
    microphone WebSocket and online Receivers, none of which this behaviour
    depends on. What it does depend on is the session existing with an owner
    and a target list, which is exactly what this creates.
    """
    from store_audio_control import registry
    registry.start_session(session_id=session_id, owner_user_id=owner_user_id,
                           store_ids=store_ids)
    return session_id


def set_override(client, headers, user_id, code, effect):
    return client.put(f"/api/users/{user_id}/permissions", headers=headers,
                      json={"changes": [{"code": code, "effect": effect}]})


def set_scope(client, headers, user_id, store_ids):
    return client.put(f"/api/users/{user_id}/store-scope", headers=headers,
                      json={"entries": [{"scope_type": "STORE", "store_id": sid}
                                        for sid in store_ids]})


# ===========================================================================
# Permission and ownership
# ===========================================================================
def test_the_broadcast_owner_can_set_a_store_volume(client, owner):
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")

    response = client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                           headers=caster,
                           json={"store_id": stores[0], "volume_percent": 30})
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["stores"] if r["store_id"] == stores[0])
    assert row["requested_volume_percent"] == 30
    # A 200 means SENT. The Store has said nothing yet, and the response is
    # explicit about that rather than echoing the request back as applied.
    assert row["applied_volume_percent"] is None
    assert row["pending"] is True


def test_an_account_without_the_permission_is_refused(client, owner):
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, caster_id,
                        "store_audio.control", "DENY").status_code == 200
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")

    assert client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                       headers=caster,
                       json={"store_id": stores[0], "volume_percent": 30}
                       ).status_code == 403


def test_another_broadcaster_cannot_touch_a_session_they_do_not_own(client, owner):
    """Bob holds the permission and guesses Alice's session and Store ids."""
    stores = some_store_ids(client, owner)
    alice_id = make_user(client, owner, "alice", "BROADCASTER")
    make_user(client, owner, "bob", "BROADCASTER")
    session_id = open_session(client, owner_user_id=alice_id, store_ids=stores)
    bob = sign_in(client, "bob")

    refused = client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                          headers=bob,
                          json={"store_id": stores[0], "volume_percent": 10})
    assert refused.status_code == 403
    assert client.get(f"/api/broadcast/sessions/{session_id}/audio-control",
                      headers=bob).status_code == 403


def test_supervision_permissions_do_not_grant_output_control(client, owner):
    """stop_any and active_view are supervision, not a remote mixer."""
    stores = some_store_ids(client, owner)
    alice_id = make_user(client, owner, "alice", "BROADCASTER")
    supervisor_id = make_user(client, owner, "supervisor", "BROADCASTER")
    for code in ("broadcast.active_view", "broadcast.stop_any",
                 "broadcast.view_targets"):
        assert set_override(client, owner, supervisor_id, code,
                            "ALLOW").status_code == 200
    session_id = open_session(client, owner_user_id=alice_id, store_ids=stores)

    supervisor = sign_in(client, "supervisor")
    assert client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                       headers=supervisor,
                       json={"store_id": stores[0], "volume_percent": 10}
                       ).status_code == 403


def test_store_scope_is_enforced_server_side(client, owner):
    stores = some_store_ids(client, owner, count=3)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_scope(client, owner, caster_id, [stores[0]]).status_code == 200
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")

    assert client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                       headers=caster,
                       json={"store_id": stores[0], "volume_percent": 40}
                       ).status_code == 200
    # In the broadcast, but not in this operator's scope.
    refused = client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                          headers=caster,
                          json={"store_id": stores[2], "volume_percent": 40})
    assert refused.status_code == 403
    assert "Scope" in refused.json()["detail"]


# ===========================================================================
# Session and Store boundaries
# ===========================================================================
def test_a_finished_session_is_refused(client, owner):
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    from store_audio_control import registry
    registry.end_session(session_id)
    caster = sign_in(client, "caster")

    refused = client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                          headers=caster,
                          json={"store_id": stores[0], "volume_percent": 40})
    assert refused.status_code == 409
    assert "no longer active" in refused.json()["detail"].lower()


def test_a_store_outside_the_session_is_refused(client, owner):
    stores = some_store_ids(client, owner, count=4)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id,
                              store_ids=stores[:2])
    caster = sign_in(client, "caster")

    assert client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                       headers=caster,
                       json={"store_id": stores[3], "volume_percent": 40}
                       ).status_code == 404


@pytest.mark.parametrize("volume,expected", [(0, 200), (100, 200),
                                             (-1, 422), (101, 422)])
def test_the_range_contract_is_enforced_at_the_edge(client, owner, volume, expected):
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")

    response = client.post(f"/api/broadcast/sessions/{session_id}/audio-control",
                           headers=caster,
                           json={"store_id": stores[0], "volume_percent": volume})
    assert response.status_code == expected


def test_mute_then_unmute_restores_the_chosen_level_over_http(client, owner):
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")
    url = f"/api/broadcast/sessions/{session_id}/audio-control"

    client.post(url, headers=caster, json={"store_id": stores[0], "volume_percent": 65})
    muted = client.post(url, headers=caster, json={"store_id": stores[0], "muted": True})
    row = next(r for r in muted.json()["stores"] if r["store_id"] == stores[0])
    assert row["requested_muted"] is True
    assert row["requested_volume_percent"] == 65

    unmuted = client.post(url, headers=caster,
                          json={"store_id": stores[0], "muted": False})
    row = next(r for r in unmuted.json()["stores"] if r["store_id"] == stores[0])
    assert row["requested_muted"] is False
    assert row["requested_volume_percent"] == 65


def test_one_store_does_not_affect_another(client, owner):
    stores = some_store_ids(client, owner, count=3)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")
    url = f"/api/broadcast/sessions/{session_id}/audio-control"

    client.post(url, headers=caster, json={"store_id": stores[0], "volume_percent": 80})
    client.post(url, headers=caster, json={"store_id": stores[1], "volume_percent": 55})
    final = client.post(url, headers=caster,
                        json={"store_id": stores[2], "muted": True})
    rows = {r["store_id"]: r for r in final.json()["stores"]}
    assert rows[stores[0]]["requested_volume_percent"] == 80
    assert rows[stores[1]]["requested_volume_percent"] == 55
    assert rows[stores[2]]["requested_muted"] is True
    assert rows[stores[0]]["requested_muted"] is False


# ===========================================================================
# Receiver capability and persistence
# ===========================================================================
def test_a_store_with_no_reporting_receiver_is_not_controllable(client, owner):
    """Old Receivers and offline ones are both surfaced, and differently."""
    stores = some_store_ids(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")

    rows = client.get(f"/api/broadcast/sessions/{session_id}/audio-control",
                      headers=caster).json()["stores"]
    for row in rows:
        # No Receiver has connected in this test, so nothing claims support.
        assert row["supported"] is False
        assert row["online"] is False


def test_slider_movement_writes_no_database_rows(client, owner):
    """Twenty movements across three Stores must not touch SQLite."""
    import sqlite3

    stores = some_store_ids(client, owner, count=3)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = open_session(client, owner_user_id=caster_id, store_ids=stores)
    caster = sign_in(client, "caster")
    url = f"/api/broadcast/sessions/{session_id}/audio-control"

    database = os.environ["ECHOCAST_DB_PATH"]

    def row_census():
        with sqlite3.connect(database) as connection:
            tables = [name for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            return {t: connection.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
                    for t in tables}

    before = row_census()
    for step in range(20):
        for store_id in stores:
            client.post(url, headers=caster,
                        json={"store_id": store_id, "volume_percent": 40 + step})
    after = row_census()

    assert after == before, {
        table: (before.get(table), after.get(table))
        for table in set(before) | set(after)
        if before.get(table) != after.get(table)
    }
