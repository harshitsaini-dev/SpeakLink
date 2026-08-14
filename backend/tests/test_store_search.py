"""Server-side search, filter and paging for Store Management.

Store Management was the last admin screen still loading its whole catalog and
filtering nothing. This adds ``GET /api/stores/search`` in the same shape as
the other five, with the same two properties that matter:

* **Store Scope narrows the results, the totals AND the filter options.** A
  scoped account must not learn that a Zone exists by opening a dropdown, and
  must not learn how many Stores it cannot see by reading a count.
* **A permanently deleted Store never appears**, under any flag. Its history
  stays reachable through the rows that reference it, never through this list.

Nothing here changes ``GET /api/stores``: the Broadcast Console, the Playwright
mocks and the tooling all depend on its bare-array shape.
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
                               "store_scope", "receiver_enrollment_api",
                               "deletion_safety", "user_deletion", "store_deletion")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one

    # Permanent Store deletion inspects receiver_devices, which phase one
    # creates. Without it the tombstone tests fail on a missing table rather
    # than on the behaviour they are about.
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


def set_scope(client, *, user_id, entries):
    from store_scope import set_user_scope
    return set_user_scope(client.server_module.engine, user_id=user_id,
                          entries=entries, actor_id=1)


def search(client, headers, **params):
    r = client.get("/api/stores/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# Shape
# ===========================================================================
def test_the_response_carries_the_same_envelope_as_every_other_admin_search(client):
    owner = sign_in(client)
    body = search(client, owner)
    for key in ("items", "total", "page", "page_size", "pages", "has_more"):
        assert key in body, f"missing {key}"
    assert isinstance(body["items"], list)
    assert body["total"] >= 1


def test_a_row_carries_what_store_management_renders(client):
    owner = sign_in(client)
    row = search(client, owner)["items"][0]
    for key in ("id", "store_code", "store_name", "city", "region",
                "lifecycle_state", "is_active"):
        assert key in row, f"missing {key}"


# ===========================================================================
# Search
# ===========================================================================
def test_search_matches_a_store_code(client):
    owner = sign_in(client)
    first = search(client, owner)["items"][0]
    body = search(client, owner, q=first["store_code"])
    assert body["total"] >= 1
    assert any(r["store_code"] == first["store_code"] for r in body["items"])


def test_search_matches_a_store_name_case_insensitively(client):
    owner = sign_in(client)
    first = search(client, owner)["items"][0]
    body = search(client, owner, q=first["store_name"].lower())
    assert any(r["id"] == first["id"] for r in body["items"])


def test_search_matches_nothing_rather_than_everything_when_it_matches_nothing(client):
    owner = sign_in(client)
    body = search(client, owner, q="no-such-store-anywhere-zzz")
    assert body["total"] == 0 and body["items"] == []


def test_a_percent_sign_is_escaped_rather_than_matching_everything(client):
    """'%' is the LIKE wildcard. Passed through unescaped it turns a search
    into 'match every Store', which reads as a working search returning the
    whole catalog - the least obvious kind of wrong."""
    owner = sign_in(client)
    everything = search(client, owner)["total"]
    body = search(client, owner, q="%")
    assert body["total"] < everything or everything == 0, (
        "a literal % must not behave as a wildcard")


def test_an_underscore_is_escaped_too(client):
    owner = sign_in(client)
    body = search(client, owner, q="_")
    everything = search(client, owner)["total"]
    assert body["total"] < everything or everything == 0


# ===========================================================================
# Filters
# ===========================================================================
def test_filtering_by_zone_narrows_the_result_and_the_total(client):
    owner = sign_in(client)
    first = search(client, owner)["items"][0]
    body = search(client, owner, region=first["region"])
    assert body["total"] >= 1
    assert {r["region"] for r in body["items"]} == {first["region"]}


def test_filtering_by_city_narrows_the_result(client):
    owner = sign_in(client)
    first = search(client, owner)["items"][0]
    body = search(client, owner, city=first["city"])
    assert {r["city"] for r in body["items"]} == {first["city"]}


def test_zone_and_search_combine_rather_than_replace_each_other(client):
    owner = sign_in(client)
    first = search(client, owner)["items"][0]
    body = search(client, owner, region=first["region"], q=first["store_code"])
    assert body["total"] >= 1
    for row in body["items"]:
        assert row["region"] == first["region"]


def test_a_disabled_store_is_hidden_from_the_default_view_and_found_by_its_own_selection(client):
    """One control, not two overlapping flags - see the lifecycle tests in
    test_store_lifecycle_filter_and_device_visibility.py."""
    owner = sign_in(client)
    target = search(client, owner)["items"][0]
    client.post(f"/api/stores/{target['id']}/disable", headers=owner)

    assert target["id"] not in {r["id"] for r in search(client, owner)["items"]}
    shown = search(client, owner, lifecycle="disabled")
    assert target["id"] in {r["id"] for r in shown["items"]}


def test_an_archived_store_is_hidden_from_the_default_view_and_found_by_its_own_selection(client):
    owner = sign_in(client)
    target = search(client, owner)["items"][1]
    client.post(f"/api/stores/{target['id']}/archive", headers=owner)

    assert target["id"] not in {r["id"] for r in search(client, owner)["items"]}
    shown = search(client, owner, lifecycle="archived")
    assert target["id"] in {r["id"] for r in shown["items"]}


# ===========================================================================
# The tombstone rule
# ===========================================================================
def test_a_permanently_deleted_store_is_hidden_under_every_selection(client):
    """Unlike archived, there is no selection that reveals it. That asymmetry
    is the design: archiving is reversible and deletion is not."""
    owner = sign_in(client)
    target = search(client, owner, lifecycle="all_current")["items"][2]
    response = client.post(f"/api/stores/{target['id']}/delete-permanently",
                           headers=owner,
                           json={"confirm": target["store_code"], "acknowledged": True})
    assert response.status_code == 200, response.text

    for extra in ({}, {"lifecycle": "all_current"}, {"lifecycle": "active"},
                  {"lifecycle": "disabled"}, {"lifecycle": "archived"}):
        body = search(client, owner, **extra)
        assert target["id"] not in {r["id"] for r in body["items"]}, (
            f"a deleted Store appeared with {extra}")


# ===========================================================================
# Paging
# ===========================================================================
def test_paging_splits_the_catalog_and_reports_an_honest_total(client):
    owner = sign_in(client)
    everything = search(client, owner)["total"]
    assert everything > 3, "the seeded catalog should be big enough to page"

    first = search(client, owner, page=1, page_size=2)
    second = search(client, owner, page=2, page_size=2)
    assert first["total"] == second["total"] == everything
    assert len(first["items"]) == 2
    assert not ({r["id"] for r in first["items"]} & {r["id"] for r in second["items"]})


def test_page_size_is_bounded(client):
    owner = sign_in(client)
    body = search(client, owner, page_size=100000)
    assert body["page_size"] <= 200


# ===========================================================================
# Store Scope - results, totals AND filter options
# ===========================================================================
def test_a_scoped_account_sees_only_its_own_stores_and_an_honest_total(client):
    owner = sign_in(client)
    everything = search(client, owner)
    mine = everything["items"][0]

    user_id = make_user(client, owner, "scoped", "ADMIN")
    set_scope(client, user_id=user_id, entries=[{"scope_type": "STORE", "store_id": mine["id"]}])
    scoped = sign_in(client, "scoped")

    body = search(client, scoped)
    assert body["total"] == 1, "the total must not count Stores this account cannot see"
    assert [r["id"] for r in body["items"]] == [mine["id"]]


def test_a_scoped_account_cannot_reach_another_store_by_searching_for_it(client):
    owner = sign_in(client)
    rows = search(client, owner)["items"]
    mine, theirs = rows[0], rows[1]

    user_id = make_user(client, owner, "narrow", "ADMIN")
    set_scope(client, user_id=user_id, entries=[{"scope_type": "STORE", "store_id": mine["id"]}])
    scoped = sign_in(client, "narrow")

    body = search(client, scoped, q=theirs["store_code"])
    assert body["total"] == 0
    assert body["items"] == []


def test_scope_filter_options_never_name_an_out_of_scope_zone_or_city(client):
    """Opening a dropdown must not be a way to enumerate the estate."""
    owner = sign_in(client)
    rows = search(client, owner)["items"]
    mine = rows[0]
    other_regions = {r["region"] for r in rows if r["region"] != mine["region"]}
    assert other_regions, "the seeded catalog needs more than one Zone for this test"

    user_id = make_user(client, owner, "dropdown", "ADMIN")
    set_scope(client, user_id=user_id, entries=[{"scope_type": "STORE", "store_id": mine["id"]}])
    scoped = sign_in(client, "dropdown")

    response = client.get("/api/stores/filter-options", headers=scoped)
    assert response.status_code == 200, response.text
    options = response.json()
    assert options["regions"] == [mine["region"]]
    assert options["cities"] == [mine["city"]]
    for leaked in other_regions:
        assert leaked not in options["regions"]


def test_filter_options_exclude_a_permanently_deleted_store(client):
    owner = sign_in(client)
    rows = search(client, owner)["items"]
    # A Store whose Zone nothing else uses, so its removal is observable.
    by_region = {}
    for row in rows:
        by_region.setdefault(row["region"], []).append(row)
    unique = [rs[0] for rs in by_region.values() if len(rs) == 1]
    if not unique:
        pytest.skip("the seeded catalog has no Zone with exactly one Store")
    target = unique[0]

    client.post(f"/api/stores/{target['id']}/delete-permanently", headers=owner,
                json={"confirm": target["store_code"], "acknowledged": True})

    options = client.get("/api/stores/filter-options", headers=owner).json()
    assert target["region"] not in options["regions"]


# ===========================================================================
# Permissions unchanged
# ===========================================================================
def test_the_search_needs_the_same_menu_permission_as_the_list(client):
    owner = sign_in(client)
    make_user(client, owner, "viewer", "VIEWER")
    viewer = sign_in(client, "viewer")
    # VIEWER holds menu.stores.view, exactly as it does for GET /stores.
    assert client.get("/api/stores", headers=viewer).status_code == 200
    assert client.get("/api/stores/search", headers=viewer).status_code == 200


def test_the_existing_list_endpoint_still_returns_a_bare_array(client):
    """The Broadcast Console and the Playwright mocks depend on this shape."""
    owner = sign_in(client)
    body = client.get("/api/stores", headers=owner).json()
    assert isinstance(body, list)


# ===========================================================================
# A filter that names several values
#
# The lifecycle parameter was validated as a WHOLE against the allowed set,
# and adding multi-value filters broke it for the one caller that matters: the
# page sends "active,archived" the moment somebody ticks two boxes, and the
# entire list came back as a 400 the operator could do nothing about.
# ===========================================================================

def test_two_lifecycles_can_be_asked_for_at_once(client):
    owner = sign_in(client)
    one = search(client, owner, page_size=1)["items"][0]
    assert client.post(f"/api/stores/{one['id']}/archive", headers=owner
                       ).status_code in (200, 204)

    active_only = search(client, owner, lifecycle="active", page_size=200)
    archived_only = search(client, owner, lifecycle="archived", page_size=200)
    both = search(client, owner, lifecycle="active,archived", page_size=200)

    assert both["total"] == active_only["total"] + archived_only["total"]
    codes = {row["store_code"] for row in both["items"]}
    assert one["store_code"] in codes


def test_an_unknown_lifecycle_is_still_refused_by_name(client):
    response = client.get("/api/stores/search?lifecycle=active,nonsense",
                          headers=sign_in(client))
    assert response.status_code == 400
    assert "nonsense" in response.json()["detail"]


def test_deleted_is_still_refused_even_beside_a_valid_value(client):
    response = client.get("/api/stores/search?lifecycle=active,deleted",
                          headers=sign_in(client))
    assert response.status_code == 400
    assert "deletion-event records" in response.json()["detail"]


def test_stores_can_be_sorted_by_a_named_column(client):
    owner = sign_in(client)
    ascending = search(client, owner, sort="store_name", dir="asc",
                       page_size=200)["items"]
    names = [row["store_name"] for row in ascending]
    assert names == sorted(names, key=str.lower)

    descending = search(client, owner, sort="store_name", dir="desc",
                        page_size=200)["items"]
    assert [row["store_name"] for row in descending] == list(reversed(names))


def test_an_unknown_sort_column_is_ignored_rather_than_failing_the_page(client):
    """A typo in a sort parameter must not cost somebody the whole list."""
    owner = sign_in(client)
    assert search(client, owner, sort="nonsense")["total"] > 0
