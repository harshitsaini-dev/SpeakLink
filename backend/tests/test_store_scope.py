"""Per-user Store/City/Zone scope, layered on top of role-based permissions.

A permission answers "may this account edit a Store at all." Scope answers
"which one." An ADMIN or BROADCASTER can be limited to a single Store, a
city, or a Zone (region) - assigned scope narrows what they see and manage,
everywhere. An account with no scope rows is unrestricted, so nothing that
already worked stops working the moment this feature ships.
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
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one

    run_receiver_credential_phase_one(server_module.engine)

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_user(client, headers, username, role):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(), "role": role, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def set_scope(engine, *, user_id, entries):
    from store_scope import set_user_scope
    set_user_scope(engine, user_id=user_id, entries=entries)


def test_unscoped_admin_sees_every_store(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "alice", "ADMIN")
    headers = sign_in(client, "alice")

    all_stores = client.get("/api/stores", headers=owner).json()
    scoped_view = client.get("/api/stores", headers=headers).json()
    assert len(scoped_view) == len(all_stores)


def test_store_scoped_admin_sees_only_the_assigned_store(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "bob", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()
    target = stores[0]
    other = stores[1]

    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": target["id"]}])
    headers = sign_in(client, "bob")

    visible = client.get("/api/stores", headers=headers).json()
    assert {s["id"] for s in visible} == {target["id"]}
    assert other["id"] not in {s["id"] for s in visible}


def test_store_scoped_admin_cannot_edit_a_store_outside_scope(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "carol", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()
    target, other = stores[0], stores[1]

    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": target["id"]}])
    headers = sign_in(client, "carol")

    in_scope = client.put(f"/api/stores/{target['id']}", headers=headers,
                          json={"store_name": "Renamed In Scope"})
    assert in_scope.status_code == 200, in_scope.text

    out_of_scope = client.put(f"/api/stores/{other['id']}", headers=headers,
                             json={"store_name": "Should Be Refused"})
    assert out_of_scope.status_code == 403


def test_city_scoped_broadcaster_can_only_start_a_broadcast_at_the_assigned_city(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    target_city = stores[0]["city"]
    other_city_store = next(s for s in stores if s["city"] != target_city)

    user_id = make_user(client, owner, "dave", "BROADCASTER")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "CITY", "scope_value": target_city}])
    headers = sign_in(client, "dave")

    ok = client.post("/api/broadcast/sessions", headers=headers,
                     json={"campaign_name": "city test", "target_mode": "city", "city": target_city})
    assert ok.status_code == 201, ok.text
    assert ok.json()["selected_store_count"] >= 1
    client.post(f"/api/broadcast/sessions/{ok.json()['id']}/stop", headers=headers)

    refused = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "explicit out of scope", "target_mode": "selected",
        "store_ids": [other_city_store["id"]]})
    assert refused.status_code == 403


def test_zone_scoped_admin_all_mode_silently_narrows_to_the_assigned_zone(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    target_region = stores[0]["region"]
    expected_ids = {s["id"] for s in stores if s["region"] == target_region}

    user_id = make_user(client, owner, "erin", "ADMIN")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "REGION", "scope_value": target_region}])
    headers = sign_in(client, "erin")

    created = client.post("/api/broadcast/sessions", headers=headers,
                          json={"campaign_name": "all mode scoped", "target_mode": "all"})
    assert created.status_code == 201, created.text
    assert created.json()["selected_store_count"] == len(expected_ids)


def test_device_endpoints_are_scoped_by_store(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    target, other = stores[0], stores[1]

    user_id = make_user(client, owner, "frank", "ADMIN")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": target["id"]}])
    headers = sign_in(client, "frank")

    ok = client.get(f"/api/stores/{target['id']}/receiver-devices", headers=headers)
    assert ok.status_code == 200, ok.text

    refused = client.get(f"/api/stores/{other['id']}/receiver-devices", headers=headers)
    assert refused.status_code == 403


def test_owner_is_never_scoped_even_with_scope_rows(client):
    owner = sign_in(client)
    owner_me = client.get("/api/auth/me", headers=owner).json()
    stores = client.get("/api/stores", headers=owner).json()

    set_scope(client.server_module.engine, user_id=owner_me["id"],
              entries=[{"scope_type": "STORE", "store_id": stores[0]["id"]}])

    still_all = client.get("/api/stores", headers=owner).json()
    assert len(still_all) == len(stores)


def test_removing_all_scope_rows_returns_to_unrestricted(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "gina", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()

    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": stores[0]["id"]}])
    headers = sign_in(client, "gina")
    assert len(client.get("/api/stores", headers=headers).json()) == 1

    set_scope(client.server_module.engine, user_id=user_id, entries=[])
    headers = sign_in(client, "gina")
    assert len(client.get("/api/stores", headers=headers).json()) == len(stores)
