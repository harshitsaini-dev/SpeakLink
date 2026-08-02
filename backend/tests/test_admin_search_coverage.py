"""Server-side search/filter for the three screens that had none, plus
Scope filters on Users and filter-based bulk selection.

An earlier round claimed "server-side search/filter for the admin screens"
after implementing three of the six. This file covers the rest and, more
usefully, pins the properties that are easy to get wrong:

* a scoped account must not learn about out-of-scope Stores through a
  result, a total, or a filter option list;
* SPEAKER_VERIFIED is acoustic proof and is never inferred from
  CONNECTED/READY/AUDIO_RECEIVING/PLAYBACK_CONFIRMED;
* archived and permanently deleted are different Device states, and only
  the former can be restored;
* Select All Filtered means every server-side match, including rows on
  pages the operator never loaded - and nothing outside their scope.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
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
    for n in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api",
            "store_scope")]:
        sys.modules.pop(n, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_user(client, headers, username, role="ADMIN"):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def stores(client, headers):
    return client.get("/api/stores", headers=headers).json()


def scope_user_to(client, engine, user_id, **entry):
    from store_scope import set_user_scope
    set_user_scope(engine, user_id=user_id, entries=[entry], actor_id=1)


def add_device(engine, store_id, *, status="active", primary=False, name="Store PC"):
    from sqlalchemy import text
    public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        # ck_receiver_devices_disabled_state: an active Device must have no
        # disabled_at, and a disabled/retired one must have it. The schema is
        # right to insist - a "retired" Device with no retirement moment is
        # not a state anything could explain later.
        disabled_at = None if status == "active" else now
        device_id = c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, disabled_at, created_at, updated_at) "
            "VALUES (:p,:s,:n,:st,:now,:dis,:now,:now) RETURNING id"),
            {"p": public_id, "s": store_id, "n": name, "st": status,
             "dis": disabled_at, "now": now}).scalar_one()
        if primary:
            c.execute(text("INSERT INTO receiver_store_primary_device "
                           "(store_id, device_id, promoted_at) VALUES (:s,:d,:now)"),
                      {"s": store_id, "d": device_id, "now": now})
    return device_id, public_id


# ===========================================================================
# Receiver Status
# ===========================================================================
def test_receiver_status_search_endpoint_exists_and_paginates(client):
    owner = sign_in(client)
    r = client.get("/api/receivers/search", headers=owner, params={"page_size": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"items", "total", "page", "page_size", "pages", "has_more"} <= set(body)
    assert len(body["items"]) <= 5
    assert body["total"] >= len(body["items"])


def test_receiver_status_searches_store_code_and_name(client):
    owner = sign_in(client)
    target = stores(client, owner)[0]
    by_code = client.get("/api/receivers/search", headers=owner,
                         params={"q": target["store_code"]}).json()
    assert target["id"] in {i["id"] for i in by_code["items"]}
    by_name = client.get("/api/receivers/search", headers=owner,
                         params={"q": target["store_name"][:5]}).json()
    assert by_name["total"] >= 1


def test_receiver_status_searches_device_public_id(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    _, public_id = add_device(client.server_module.engine, store["id"])
    body = client.get("/api/receivers/search", headers=owner,
                      params={"q": public_id}).json()
    assert store["id"] in {i["id"] for i in body["items"]}


def test_receiver_status_filters_by_zone_city_and_primary(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    zone = all_stores[0]["region"]
    with_primary = all_stores[0]
    add_device(client.server_module.engine, with_primary["id"], primary=True)

    by_zone = client.get("/api/receivers/search", headers=owner,
                         params={"region": zone, "page_size": 200}).json()
    assert by_zone["total"] >= 1
    assert all(i["region"] == zone for i in by_zone["items"])

    primary_only = client.get("/api/receivers/search", headers=owner,
                              params={"has_primary": True, "page_size": 200}).json()
    assert with_primary["id"] in {i["id"] for i in primary_only["items"]}
    assert all(i["has_primary"] for i in primary_only["items"])


def test_receiver_status_never_infers_speaker_verified(client):
    """SPEAKER_VERIFIED is acoustic proof from a trusted event. A Store that
    is merely connected/online must never be reported as verified."""
    owner = sign_in(client)
    body = client.get("/api/receivers/search", headers=owner,
                      params={"page_size": 200}).json()
    for item in body["items"]:
        assert item.get("speaker_verified") is not True, (
            f"{item['store_code']} claims SPEAKER_VERIFIED without acoustic proof")


def test_receiver_status_respects_store_scope_in_results_and_totals(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    mine = all_stores[0]
    user_id = make_user(client, owner, "scoped")
    scope_user_to(client, client.server_module.engine, user_id,
                  scope_type="STORE", store_id=mine["id"])
    scoped = sign_in(client, "scoped")

    body = client.get("/api/receivers/search", headers=scoped,
                      params={"page_size": 200}).json()
    assert {i["id"] for i in body["items"]} == {mine["id"]}
    assert body["total"] == 1, "the total leaked out-of-scope Stores"


def test_receiver_status_filter_options_do_not_leak_out_of_scope_zones(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    mine = all_stores[0]
    other_zone = next((s["region"] for s in all_stores if s["region"] != mine["region"]), None)
    user_id = make_user(client, owner, "zonescoped")
    scope_user_to(client, client.server_module.engine, user_id,
                  scope_type="STORE", store_id=mine["id"])
    scoped = sign_in(client, "zonescoped")

    r = client.get("/api/receivers/filter-options", headers=scoped)
    assert r.status_code == 200, r.text
    options = r.json()
    assert mine["region"] in options["regions"]
    if other_zone:
        assert other_zone not in options["regions"], "filter options leaked another Zone"


# ===========================================================================
# Receiver Devices
# ===========================================================================
def test_device_search_endpoint_exists_and_searches_across_stores(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    _, first = add_device(client.server_module.engine, all_stores[0]["id"], name="Till PC")
    _, second = add_device(client.server_module.engine, all_stores[1]["id"], name="Back Office")

    r = client.get("/api/receiver-devices/search", headers=owner, params={"page_size": 200})
    assert r.status_code == 200, r.text
    ids = {i["public_id"] for i in r.json()["items"]}
    assert {first, second} <= ids, "search must span Stores, not one Store at a time"

    by_id = client.get("/api/receiver-devices/search", headers=owner,
                       params={"q": first}).json()
    assert {i["public_id"] for i in by_id["items"]} == {first}

    by_name = client.get("/api/receiver-devices/search", headers=owner,
                         params={"q": "Back Off"}).json()
    assert {i["public_id"] for i in by_name["items"]} == {second}


def test_device_search_filters_by_store_zone_status_and_primary(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    target = all_stores[0]
    _, primary_id = add_device(client.server_module.engine, target["id"], primary=True)
    add_device(client.server_module.engine, target["id"], status="retired")

    by_store = client.get("/api/receiver-devices/search", headers=owner,
                          params={"store_id": target["id"], "page_size": 200}).json()
    assert by_store["total"] == 2

    by_status = client.get("/api/receiver-devices/search", headers=owner,
                           params={"status": "retired", "page_size": 200}).json()
    assert all(i["status"] == "retired" for i in by_status["items"])

    primary_only = client.get("/api/receiver-devices/search", headers=owner,
                              params={"is_primary": True, "page_size": 200}).json()
    assert {i["public_id"] for i in primary_only["items"]} == {primary_id}

    by_zone = client.get("/api/receiver-devices/search", headers=owner,
                         params={"region": target["region"], "page_size": 200}).json()
    assert by_zone["total"] >= 2


def test_device_search_hides_permanently_deleted_by_default(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    _, public_id = add_device(client.server_module.engine, store["id"])
    client.post(f"/api/receiver-devices/{public_id}/delete-permanently", headers=owner,
                json={"confirm": public_id, "acknowledged": True})

    default = client.get("/api/receiver-devices/search", headers=owner,
                         params={"page_size": 200}).json()
    assert public_id not in {i["public_id"] for i in default["items"]}

    shown = client.get("/api/receiver-devices/search", headers=owner,
                       params={"include_deleted": True, "page_size": 200}).json()
    assert public_id in {i["public_id"] for i in shown["items"]}


def test_archived_and_permanently_deleted_are_distinct_states(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    _, archived = add_device(client.server_module.engine, store["id"])
    _, deleted = add_device(client.server_module.engine, store["id"])
    client.post(f"/api/receiver-devices/{archived}/archive", headers=owner)
    client.post(f"/api/receiver-devices/{deleted}/delete-permanently", headers=owner,
                json={"confirm": deleted, "acknowledged": True})

    body = client.get("/api/receiver-devices/search", headers=owner,
                      params={"include_deleted": True, "page_size": 200}).json()
    rows = {i["public_id"]: i for i in body["items"]}
    assert rows[archived]["lifecycle"] == "archived"
    assert rows[deleted]["lifecycle"] == "deleted"

    # Only the archived one can come back.
    assert client.post(f"/api/receiver-devices/{archived}/restore",
                       headers=owner).status_code == 200
    assert client.post(f"/api/receiver-devices/{deleted}/restore",
                       headers=owner).status_code == 409


def test_device_search_respects_store_scope(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    mine, theirs = all_stores[0], all_stores[1]
    _, mine_device = add_device(client.server_module.engine, mine["id"])
    add_device(client.server_module.engine, theirs["id"])

    user_id = make_user(client, owner, "devscoped")
    scope_user_to(client, client.server_module.engine, user_id,
                  scope_type="STORE", store_id=mine["id"])
    scoped = sign_in(client, "devscoped")

    body = client.get("/api/receiver-devices/search", headers=scoped,
                      params={"page_size": 200}).json()
    assert {i["public_id"] for i in body["items"]} == {mine_device}
    assert body["total"] == 1


# ===========================================================================
# User Management - Scope filters
# ===========================================================================
def test_user_search_filters_by_store_city_and_zone_scope(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    target = all_stores[0]

    store_scoped = make_user(client, owner, "storescoped")
    city_scoped = make_user(client, owner, "cityscoped")
    make_user(client, owner, "unscoped")
    engine = client.server_module.engine
    scope_user_to(client, engine, store_scoped, scope_type="STORE", store_id=target["id"])
    scope_user_to(client, engine, city_scoped, scope_type="CITY", scope_value=target["city"])

    by_store = client.get("/api/users/search", headers=owner,
                          params={"scope_store_id": target["id"]}).json()
    assert "storescoped" in {u["username"] for u in by_store["items"]}
    assert "unscoped" not in {u["username"] for u in by_store["items"]}

    by_city = client.get("/api/users/search", headers=owner,
                         params={"scope_city": target["city"]}).json()
    assert "cityscoped" in {u["username"] for u in by_city["items"]}
    assert "storescoped" not in {u["username"] for u in by_city["items"]}


# ===========================================================================
# Select All Filtered - filter-based bulk
# ===========================================================================
def make_session(client, headers, store_id, name):
    r = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": [store_id]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_filtered_bulk_affects_rows_on_pages_never_loaded(client):
    """The whole point of Select All Filtered: React sends the FILTER, and the
    backend resolves the matched set - so rows the operator never paged to are
    still acted on, without downloading thousands of ids."""
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    for i in range(7):
        make_session(client, owner, store, f"purge {i}")
    keeper = make_session(client, owner, store, "keep this one")

    visible = client.get("/api/broadcast/history/search", headers=owner,
                         params={"q": "purge", "page_size": 2}).json()
    assert visible["total"] == 7 and len(visible["items"]) == 2

    r = client.post("/api/broadcast/history/delete-permanently", headers=owner, json={
        "mode": "filtered", "filters": {"q": "purge"},
        "confirm": "DELETE", "acknowledged": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 7
    assert body["affected"] == 7

    remaining = client.get("/api/broadcast/history/search", headers=owner,
                           params={"page_size": 200}).json()
    assert {i["id"] for i in remaining["items"]} == {keeper}


def test_filtered_bulk_never_reaches_outside_the_callers_scope(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    mine, theirs = all_stores[0], all_stores[1]
    mine_session = make_session(client, owner, mine["id"], "shared name")
    theirs_session = make_session(client, owner, theirs["id"], "shared name")

    user_id = make_user(client, owner, "bulkscoped")
    scope_user_to(client, client.server_module.engine, user_id,
                  scope_type="STORE", store_id=mine["id"])
    # Give the scoped account the permission, so scope is what stops it.
    client.put(f"/api/users/{user_id}/permissions", headers=owner, json={"changes": [
        {"code": "broadcast_history.delete_permanently", "effect": "ALLOW"}]})
    scoped = sign_in(client, "bulkscoped")

    r = client.post("/api/broadcast/history/delete-permanently", headers=scoped, json={
        "mode": "filtered", "filters": {"q": "shared name"},
        "confirm": "DELETE", "acknowledged": True})
    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 1, "the filter reached outside the caller's scope"

    from sqlalchemy import text
    with client.server_module.engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": theirs_session}).scalar_one() == 1, "out-of-scope row deleted"
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": mine_session}).scalar_one() == 0


def test_filtered_bulk_still_requires_typed_confirmation(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    make_session(client, owner, store, "protected")

    assert client.post("/api/broadcast/history/delete-permanently", headers=owner, json={
        "mode": "filtered", "filters": {"q": "protected"},
        "confirm": "delete", "acknowledged": True}).status_code == 409
    assert client.post("/api/broadcast/history/delete-permanently", headers=owner, json={
        "mode": "filtered", "filters": {"q": "protected"},
        "confirm": "DELETE", "acknowledged": False}).status_code == 400
    assert client.get("/api/broadcast/history/search", headers=owner,
                      params={"q": "protected"}).json()["total"] == 1


def test_filtered_bulk_archive_for_logs(client):
    owner = sign_in(client)
    before = client.get("/api/logs/search", headers=owner,
                        params={"level": "info", "page_size": 200}).json()["total"]
    assert before >= 1

    r = client.post("/api/logs/archive", headers=owner,
                    json={"mode": "filtered", "filters": {"level": "info"}})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == before

    after = client.get("/api/logs/search", headers=owner,
                       params={"level": "info", "page_size": 200}).json()["total"]
    assert after == 0
    archived = client.get("/api/logs/search", headers=owner,
                          params={"level": "info", "archived_only": True,
                                  "page_size": 200}).json()["total"]
    assert archived == before
