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


def set_scope(engine, *, user_id, entries, actor_id=1):
    from store_scope import set_user_scope
    return set_user_scope(engine, user_id=user_id, entries=entries, actor_id=actor_id)


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


# ===========================================================================
# Scope audit
# ===========================================================================
def test_scope_change_over_the_api_is_recorded_in_the_audit_table(client):
    owner = sign_in(client)
    owner_me = client.get("/api/auth/me", headers=owner).json()
    user_id = make_user(client, owner, "henry", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": [{"scope_type": "STORE", "store_id": stores[0]["id"]}]})
    assert resp.status_code == 200, resp.text

    from store_scope import list_scope_audit_events
    events = list_scope_audit_events(client.server_module.engine, user_id=user_id)
    assert len(events) == 1
    assert events[0]["action"] == "ADDED"
    assert events[0]["scope_type"] == "STORE"
    assert events[0]["actor_user_id"] == owner_me["id"]
    assert events[0]["target_user_id"] == user_id
    assert stores[0]["store_code"] in events[0]["scope_label"]


def test_removing_a_scope_entry_is_recorded_as_removed(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "iris", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()

    client.put(f"/api/users/{user_id}/store-scope", headers=owner,
              json={"entries": [{"scope_type": "CITY", "scope_value": stores[0]["city"]}]})
    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner, json={"entries": []})
    assert resp.status_code == 200, resp.text

    from store_scope import list_scope_audit_events
    events = list_scope_audit_events(client.server_module.engine, user_id=user_id)
    actions = [e["action"] for e in events]
    assert actions == ["ADDED", "REMOVED"]
    assert events[1]["scope_label"] == stores[0]["city"]


def test_scope_audit_contains_no_secrets(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "jack", "ADMIN")
    stores = client.get("/api/stores", headers=owner).json()

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": [{"scope_type": "STORE", "store_id": stores[0]["id"]}]})
    payload = resp.text.lower()
    for forbidden in ("password", "jwt", "bearer ", "hmac", "secret", "token"):
        assert forbidden not in payload

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM store_scope_audit_events")).all()
    assert rows
    for row in rows:
        for value in row:
            text_value = str(value).lower()
            for forbidden in ("password", "jwt", "bearer ", "hmac"):
                assert forbidden not in text_value


def test_broadcast_history_hides_sessions_outside_scope_and_recomputes_counts(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    in_scope_store, out_of_scope_store = stores[0], stores[1]

    # A session targeting BOTH an in-scope and an out-of-scope Store.
    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "mixed scope", "target_mode": "selected",
        "store_ids": [in_scope_store["id"], out_of_scope_store["id"]]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert created.json()["selected_store_count"] == 2

    user_id = make_user(client, owner, "liam", "ADMIN")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": in_scope_store["id"]}])
    headers = sign_in(client, "liam")

    history = client.get("/api/broadcast/history", headers=headers)
    assert history.status_code == 200, history.text
    visible = [s for s in history.json() if s["id"] == session_id]
    assert len(visible) == 1
    # Recomputed to the scoped subset - never the real total of 2.
    assert visible[0]["selected_store_count"] == 1

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["selected_store_count"] == 1
    assert {t["store_id"] for t in detail_body["targets"]} == {in_scope_store["id"]}


def test_session_detail_is_404_for_a_session_with_no_in_scope_targets(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    out_of_scope_store = stores[1]

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "entirely out of scope", "target_mode": "selected",
        "store_ids": [out_of_scope_store["id"]]})
    session_id = created.json()["id"]

    user_id = make_user(client, owner, "mona", "ADMIN")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": stores[0]["id"]}])
    headers = sign_in(client, "mona")

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=headers)
    assert detail.status_code == 404

    history = client.get("/api/broadcast/history", headers=headers)
    assert session_id not in {s["id"] for s in history.json()}


def test_system_logs_are_not_store_scoped_by_design(client):
    """system_logs has no store_id column - a log line is free text that MAY
    mention a Store, but is not structurally associated with one. Fabricating
    a Store scope over it would mean guessing at unstructured text, so it
    stays governed only by menu.logs.view, exactly like before scope shipped.
    """
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    user_id = make_user(client, owner, "noah", "ADMIN")
    set_scope(client.server_module.engine, user_id=user_id,
              entries=[{"scope_type": "STORE", "store_id": stores[0]["id"]}])
    headers = sign_in(client, "noah")

    scoped_logs = client.get("/api/logs", headers=headers)
    unscoped_logs = client.get("/api/logs", headers=owner)
    assert scoped_logs.status_code == 200, scoped_logs.text
    # Same count as OWNER's - logs are not filtered by Store scope.
    assert len(scoped_logs.json()) == len(unscoped_logs.json())


# ===========================================================================
# Live defect: a BROADCASTER with REGION + CITY + STORE scope must see the
# UNION of all three, and Receiver Status must never render blank because
# the account genuinely lacks permission to call the endpoint it needs.
# ===========================================================================
def test_a_fresh_broadcaster_can_load_stores_by_role_default_alone(client):
    """Reproduces the real defect: Role.BROADCASTER's default permissions did
    not include menu.stores.view, so GET /api/stores - the only endpoint
    Receiver Status calls - 403'd for every BROADCASTER that had no manually
    added per-user override. ReceiverStatus.jsx has no error handling, so
    that 403 rendered as a silently blank page, not an explanation."""
    owner = sign_in(client)
    user_id = make_user(client, owner, "quinn", "BROADCASTER")
    headers = sign_in(client, "quinn")

    resp = client.get("/api/stores", headers=headers)
    assert resp.status_code == 200, resp.text


def test_broadcaster_with_region_city_and_store_scope_sees_the_full_union(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    target_region = stores[0]["region"]
    target_city = stores[0]["city"]
    explicit_store = next(s for s in stores if s["city"] != target_city or s["region"] != target_region)
    if explicit_store is None:  # pragma: no cover - defensive, catalog dependent
        pytest.skip("catalog has no Store outside the target city/region to use as the explicit case")

    expected_ids = {explicit_store["id"]} | {
        s["id"] for s in stores if s["city"] == target_city or s["region"] == target_region
    }

    user_id = make_user(client, owner, "priya", "BROADCASTER")
    set_scope(client.server_module.engine, user_id=user_id, entries=[
        {"scope_type": "REGION", "scope_value": target_region},
        {"scope_type": "CITY", "scope_value": target_city},
        {"scope_type": "STORE", "store_id": explicit_store["id"]},
    ])
    headers = sign_in(client, "priya")

    visible = client.get("/api/stores", headers=headers).json()
    assert visible != []
    assert {s["id"] for s in visible} == expected_ids


def test_explicit_store_assignment_survives_even_if_city_and_zone_resolve_to_nothing(client):
    """A CITY/REGION value that (hypothetically, pre-validation) matched zero
    Stores must never cancel a valid explicit STORE assignment - UNION, not
    intersection, and never a silent all-or-nothing collapse."""
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    target = stores[0]

    user_id = make_user(client, owner, "ravi", "BROADCASTER")
    engine = client.server_module.engine
    # Bypass API-level validation to simulate a pre-existing zero-match
    # CITY/REGION row, exactly as Phase 5 validation is meant to prevent
    # going forward - a row like this must not already exist for new saves,
    # but old data must still degrade safely.
    from sqlalchemy import text
    with engine.begin() as connection:
        now = "2026-08-01T00:00:00+00:00"
        connection.execute(text(
            "INSERT INTO user_store_scope (user_id, scope_type, store_id, scope_value, created_at) "
            "VALUES (:uid, 'CITY', NULL, 'Nonexistent City', :now)"
        ), {"uid": user_id, "now": now})
        connection.execute(text(
            "INSERT INTO user_store_scope (user_id, scope_type, store_id, scope_value, created_at) "
            "VALUES (:uid, 'STORE', :sid, NULL, :now)"
        ), {"uid": user_id, "sid": target["id"], "now": now})

    headers = sign_in(client, "ravi")
    visible = client.get("/api/stores", headers=headers).json()
    assert target["id"] in {s["id"] for s in visible}


def test_saving_an_unknown_city_is_rejected(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "sam", "ADMIN")

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": [{"scope_type": "CITY", "scope_value": "UN ZNOE"}]})
    assert resp.status_code == 400, resp.text


def test_saving_an_unknown_zone_is_rejected(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "tara", "ADMIN")

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": [{"scope_type": "REGION", "scope_value": "Nowhere Zone"}]})
    assert resp.status_code == 400, resp.text


def test_saving_a_known_city_still_succeeds(client):
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    user_id = make_user(client, owner, "uma", "ADMIN")

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": [{"scope_type": "CITY", "scope_value": stores[0]["city"]}]})
    assert resp.status_code == 200, resp.text


def test_only_owner_can_change_store_scope(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "kelly", "ADMIN")
    admin_headers = sign_in(client, "kelly")

    resp = client.put(f"/api/users/{user_id}/store-scope", headers=admin_headers,
                      json={"entries": []})
    assert resp.status_code == 403

    read_resp = client.get(f"/api/users/{user_id}/store-scope", headers=admin_headers)
    assert read_resp.status_code == 403
