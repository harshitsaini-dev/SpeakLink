"""Server-side search, filtering and pagination for the admin screens.

The property that matters most here is that filtering happens in SQL, not in
React: System Logs and Broadcast History are the two tables that grow without
bound, and a client-side filter degrades silently into a slow page rather
than failing loudly.

The second property is Select All Filtered. "All filtered" must mean every
row matching the current server-side filter - not merely the rows currently
on screen - so the total in the response is what a bulk action operates on,
and these tests pin that relationship.
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
    for module in [n for n in list(sys.modules) if n in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api")]:
        sys.modules.pop(module, None)
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


def stores(client, headers):
    return client.get("/api/stores", headers=headers).json()


def make_session(client, headers, store_id, name):
    r = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": [store_id]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def search_history(client, headers, **params):
    r = client.get("/api/broadcast/history/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def search_logs(client, headers, **params):
    r = client.get("/api/logs/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# Shape and pagination boundaries
# ===========================================================================
def test_the_response_carries_total_page_and_has_more(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    for index in range(5):
        make_session(client, owner, store, f"campaign {index}")

    body = search_history(client, owner, page=1, page_size=2)
    assert body["total"] == 5
    assert len(body["items"]) == 2
    assert body["page"] == 1 and body["page_size"] == 2
    assert body["pages"] == 3
    assert body["has_more"] is True


def test_the_last_page_reports_no_more_and_may_be_short(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    for index in range(5):
        make_session(client, owner, store, f"campaign {index}")

    body = search_history(client, owner, page=3, page_size=2)
    assert len(body["items"]) == 1
    assert body["has_more"] is False


def test_a_page_beyond_the_end_is_empty_rather_than_an_error(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    make_session(client, owner, store, "only one")
    body = search_history(client, owner, page=99, page_size=20)
    assert body["items"] == []
    assert body["total"] == 1


def test_page_size_is_bounded_so_a_hand_edited_url_cannot_scan_everything(client):
    owner = sign_in(client)
    body = search_history(client, owner, page_size=100000)
    assert body["page_size"] <= 200


def test_paging_never_repeats_or_drops_a_row(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    expected = {make_session(client, owner, store, f"c{i}") for i in range(7)}

    seen = []
    for page in (1, 2, 3, 4):
        seen.extend(item["id"] for item in
                    search_history(client, owner, page=page, page_size=2)["items"])
    assert len(seen) == len(set(seen)), "a row appeared on two pages"
    assert set(seen) == expected


# ===========================================================================
# Broadcast History filters
# ===========================================================================
def test_text_search_matches_the_campaign_name(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    make_session(client, owner, store, "Diwali sale")
    make_session(client, owner, store, "Fire drill")

    body = search_history(client, owner, q="diwali")
    assert body["total"] == 1
    assert body["items"][0]["campaign_name"] == "Diwali sale"


def test_search_wildcards_are_escaped_not_interpreted(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    make_session(client, owner, store, "real campaign")

    # A bare % must not behave as "match everything".
    body = search_history(client, owner, q="%")
    assert body["total"] == 0


def test_status_and_started_by_filters(client):
    owner = sign_in(client)
    me = client.get("/api/auth/me", headers=owner).json()
    store = stores(client, owner)[0]["id"]
    make_session(client, owner, store, "pending one")

    assert search_history(client, owner, status="pending")["total"] == 1
    assert search_history(client, owner, status="ended")["total"] == 0
    assert search_history(client, owner, started_by=me["id"])["total"] == 1
    assert search_history(client, owner, started_by=999999)["total"] == 0


def test_a_multi_target_session_matches_a_zone_filter_exactly_once(client):
    """The whole reason the Store/City/Zone filter is an EXISTS subquery: a
    session targeting six Stores in a Zone must come back once, not six times."""
    owner = sign_in(client)
    all_stores = stores(client, owner)
    zone = all_stores[0]["region"]
    in_zone = [s["id"] for s in all_stores if s["region"] == zone][:6]
    assert len(in_zone) >= 2, "fixture needs several Stores sharing a Zone"

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "zone wide", "target_mode": "selected", "store_ids": in_zone})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    body = search_history(client, owner, region=zone)
    matching = [item for item in body["items"] if item["id"] == session_id]
    assert len(matching) == 1, "the session was returned once per target"
    assert body["total"] == len([i for i in body["items"]])


def test_store_and_city_filters_narrow_correctly(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    first, other = all_stores[0], next(s for s in all_stores if s["id"] != all_stores[0]["id"])
    make_session(client, owner, first["id"], "first store")
    make_session(client, owner, other["id"], "other store")

    body = search_history(client, owner, store_id=first["id"])
    assert body["total"] == 1
    assert body["items"][0]["campaign_name"] == "first store"

    by_city = search_history(client, owner, city=first["city"])
    assert by_city["total"] >= 1


def test_archived_sessions_are_excluded_until_asked_for(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    kept = make_session(client, owner, store, "kept")
    hidden = make_session(client, owner, store, "hidden")
    client.post("/api/broadcast/history/archive", headers=owner, json={"ids": [hidden]})

    default = search_history(client, owner)
    assert {i["id"] for i in default["items"]} == {kept}

    both = search_history(client, owner, include_archived=True)
    assert {i["id"] for i in both["items"]} == {kept, hidden}

    only = search_history(client, owner, archived_only=True)
    assert {i["id"] for i in only["items"]} == {hidden}


def test_a_session_row_says_whether_it_is_archived(client):
    """The archived filter alone is not enough for the UI.

    With include_archived=True the operator sees archived and unarchived rows
    side by side, and has to be able to tell which is which - both to read the
    list honestly and to know whether Archive or Unarchive is the action that
    applies. That means archived_at has to travel on the row itself.
    """
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    kept = make_session(client, owner, store, "kept")
    hidden = make_session(client, owner, store, "hidden")
    client.post("/api/broadcast/history/archive", headers=owner, json={"ids": [hidden]})

    rows = {i["id"]: i for i in search_history(client, owner, include_archived=True)["items"]}
    assert rows[kept]["archived_at"] is None
    assert rows[hidden]["archived_at"] is not None


def test_a_malformed_date_is_refused_rather_than_silently_ignored(client):
    owner = sign_in(client)
    r = client.get("/api/broadcast/history/search", headers=owner,
                   params={"date_from": "not-a-date"})
    assert r.status_code == 400
    assert "date" in r.json()["detail"].lower()


# ===========================================================================
# Select All Filtered
# ===========================================================================
def test_select_all_filtered_means_every_match_not_just_the_visible_page(client):
    """total is what a Select All Filtered bulk action operates on, and it is
    deliberately larger than the page the operator can see."""
    owner = sign_in(client)
    store = stores(client, owner)[0]["id"]
    for index in range(6):
        make_session(client, owner, store, f"purge me {index}")
    make_session(client, owner, store, "keep me")

    filtered = search_history(client, owner, q="purge me", page=1, page_size=2)
    assert filtered["total"] == 6, "the filter must count every match"
    assert len(filtered["items"]) == 2, "only one page is visible"

    every_id = [item["id"] for item in
                search_history(client, owner, q="purge me", page_size=200)["items"]]
    assert len(every_id) == 6

    result = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                         json={"ids": every_id, "confirm": "DELETE",
                               "acknowledged": True,
                               "filters": {"q": "purge me"}}).json()
    assert (result["requested"], result["affected"],
            result["skipped"], result["failed"]) == (6, 6, 0, 0)

    remaining = search_history(client, owner)
    assert {i["campaign_name"] for i in remaining["items"]} == {"keep me"}


# ===========================================================================
# System Logs
# ===========================================================================
def test_log_text_level_and_archived_filters(client):
    owner = sign_in(client)
    body = search_logs(client, owner, level="info")
    assert all(item["level"] == "info" for item in body["items"])

    warn = search_logs(client, owner, level="warn")
    assert all(item["level"] == "warn" for item in warn["items"])

    target = search_logs(client, owner, page_size=1)["items"][0]["id"]
    client.post("/api/logs/archive", headers=owner, json={"ids": [target]})
    assert target not in {i["id"] for i in search_logs(client, owner, page_size=200)["items"]}
    assert target in {i["id"] for i in
                      search_logs(client, owner, archived_only=True, page_size=200)["items"]}


def test_log_search_reports_honestly_how_far_entity_filters_reach(client):
    """The entity columns are populated for new rows only. The response says
    so, so a filter that legitimately matches nothing is distinguishable from
    a broken one."""
    owner = sign_in(client)
    body = search_logs(client, owner)
    coverage = body["meta"]["entity_filter_coverage"]
    assert "rows_with_structured_entities" in coverage
    assert "since those fields existed" in coverage["note"]


# ===========================================================================
# Users
# ===========================================================================
def test_user_search_by_name_role_and_state(client):
    owner = sign_in(client)
    for name, role in (("alice", "ADMIN"), ("bob", "BROADCASTER"), ("carol", "VIEWER")):
        client.post("/api/users", headers=owner, json={
            "username": name, "display_name": name.title(),
            "role": role, "password": PASSWORD})

    r = client.get("/api/users/search", headers=owner, params={"q": "ali"})
    assert r.status_code == 200, r.text
    assert {u["username"] for u in r.json()["items"]} == {"alice"}

    by_role = client.get("/api/users/search", headers=owner,
                         params={"role": "BROADCASTER"}).json()
    assert {u["username"] for u in by_role["items"]} == {"bob"}


def test_a_permanently_deleted_user_is_absent_from_search_by_default(client):
    owner = sign_in(client)
    created = client.post("/api/users", headers=owner, json={
        "username": "ghost", "display_name": "Ghost",
        "role": "ADMIN", "password": PASSWORD}).json()
    client.post(f"/api/users/{created['id']}/delete-permanently", headers=owner,
                json={"confirm": "ghost", "acknowledged": True})

    default = client.get("/api/users/search", headers=owner).json()
    assert "ghost" not in {u["username"] for u in default["items"]}

    with_deleted = client.get("/api/users/search", headers=owner,
                              params={"include_deleted": True}).json()
    assert "ghost" in {u["username"] for u in with_deleted["items"]}


# ===========================================================================
# Scope stays authoritative
# ===========================================================================
def test_history_search_still_respects_store_scope(client):
    owner = sign_in(client)
    all_stores = stores(client, owner)
    mine, theirs = all_stores[0], all_stores[1]
    make_session(client, owner, mine["id"], "mine")
    make_session(client, owner, theirs["id"], "theirs")

    created = client.post("/api/users", headers=owner, json={
        "username": "scoped", "display_name": "Scoped",
        "role": "ADMIN", "password": PASSWORD}).json()
    from store_scope import set_user_scope
    set_user_scope(client.server_module.engine, user_id=created["id"],
                   entries=[{"scope_type": "STORE", "store_id": mine["id"]}],
                   actor_id=1)

    scoped = sign_in(client, "scoped")
    body = search_history(client, scoped)
    assert {i["campaign_name"] for i in body["items"]} == {"mine"}
    assert body["total"] == 1
