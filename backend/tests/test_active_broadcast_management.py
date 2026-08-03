"""The Active Broadcasts supervision page, proved at its permission edges.

WHAT THIS FILE IS REALLY TESTING

Not "does the page load". Four capabilities that operators asked to be kept
apart, and the ways software usually fails to keep them apart:

    broadcast.active_view    open the page at all
    broadcast.view_ownership know WHO is broadcasting
    broadcast.view_targets   know WHICH Stores
    broadcast.stop_any       stop somebody else's broadcast

The failure mode that matters is not a missing 403. It is a field that WAS
serialized and merely hidden by the interface, or a search whose RESULT COUNT
answers a question the caller was not allowed to ask. So the assertions here
read the raw response body, and several of them assert on the shape of an
answer rather than its contents.

Every assertion about hidden data checks the SERIALIZED JSON. A test that
only checked what a component rendered would pass against a backend that
sends everything to everyone.
"""

from __future__ import annotations

import asyncio
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
LIST_URL = "/api/broadcast/active-management"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for name in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api",
            "store_scope", "ws_manager", "broadcast_runtime",
            "broadcast_reservation", "active_broadcast_management")]:
        sys.modules.pop(name, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        try:
            yield made
        finally:
            # A broadcast left live outlives this database on the module-level
            # manager, and a live target Store cannot be archived - which
            # surfaces as an unrelated lifecycle failure files away.
            for session_id in list(server_module.manager.broadcasts.active_session_ids()):
                asyncio.run(server_module.manager.broadcasts.end(session_id))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_user(client, headers, username, role="BROADCASTER"):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def set_rights(client, owner_headers, user_id, **codes):
    """{'broadcast.active_view': 'ALLOW'} as active_view='ALLOW'."""
    changes = [{"code": code.replace("__", "."), "effect": effect}
               for code, effect in codes.items()]
    r = client.put(f"/api/users/{user_id}/permissions", headers=owner_headers,
                   json={"changes": changes})
    assert r.status_code == 200, r.text


def grant(client, owner_headers, user_id, *codes):
    set_rights(client, owner_headers, user_id,
               **{code.replace(".", "__"): "ALLOW" for code in codes})


def deny(client, owner_headers, user_id, *codes):
    set_rights(client, owner_headers, user_id,
               **{code.replace(".", "__"): "DENY" for code in codes})


def scope_to_stores(client, owner_headers, user_id, store_ids):
    r = client.put(f"/api/users/{user_id}/store-scope", headers=owner_headers, json={
        "entries": [{"scope_type": "STORE", "store_id": sid} for sid in store_ids]})
    assert r.status_code == 200, r.text


def stores(client, headers):
    return client.get("/api/stores", headers=headers).json()


def start_broadcast(client, headers, name, store_ids):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": list(store_ids)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    started = client.post(f"/api/broadcast/sessions/{session_id}/start", headers=headers)
    assert started.status_code == 200, started.text
    return session_id


@pytest.fixture()
def estate(client):
    """One OWNER, three broadcasters live on distinct Stores.

    Alice -> catalog[0], Bob -> catalog[1..2], Carol -> catalog[3].
    """
    owner = sign_in(client)
    for name in ("alice", "bob", "carol"):
        make_user(client, owner, name)
    catalog = stores(client, owner)
    sessions = {
        "alice": start_broadcast(client, sign_in(client, "alice"), "Morning Offer",
                                 [catalog[0]["id"]]),
        "bob": start_broadcast(client, sign_in(client, "bob"), "Weekend Offer",
                               [catalog[1]["id"], catalog[2]["id"]]),
        "carol": start_broadcast(client, sign_in(client, "carol"), "Closing Time",
                                 [catalog[3]["id"]]),
    }
    return {"owner": owner, "catalog": catalog, "sessions": sessions}


# ===========================================================================
# PAGE PERMISSION (1-5)
# ===========================================================================
def test_1_list_refused_without_active_view(client, estate):
    """A BROADCASTER holds no supervision rights by default."""
    r = client.get(LIST_URL, headers=sign_in(client, "alice"))
    assert r.status_code == 403, r.text


def test_2_detail_refused_without_active_view(client, estate):
    sid = estate["sessions"]["bob"]
    r = client.get(f"{LIST_URL}/{sid}/stores", headers=sign_in(client, "alice"))
    assert r.status_code == 403


def test_3_active_view_returns_the_list(client, estate):
    r = client.get(LIST_URL, headers=estate["owner"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3


def test_4_per_user_deny_overrides_the_admin_default(client, estate):
    admin_id = make_user(client, estate["owner"], "supervisor", role="ADMIN")
    before = client.get(LIST_URL, headers=sign_in(client, "supervisor"))
    assert before.status_code == 200, "ADMIN holds active_view by default"

    deny(client, estate["owner"], admin_id, "broadcast.active_view")
    after = client.get(LIST_URL, headers=sign_in(client, "supervisor"))
    assert after.status_code == 403, "an explicit DENY must beat the role default"


def test_5_explicit_grant_opens_the_page_for_a_broadcaster(client, estate):
    alice_id = make_user(client, estate["owner"], "alice2")
    grant(client, estate["owner"], alice_id, "broadcast.active_view")
    r = client.get(LIST_URL, headers=sign_in(client, "alice2"))
    assert r.status_code == 200, r.text


# ===========================================================================
# OWNERSHIP (6-10)
# ===========================================================================
def _viewer(client, estate, *codes, username="viewer"):
    user_id = make_user(client, estate["owner"], username)
    grant(client, estate["owner"], user_id, "broadcast.active_view", *codes)
    return sign_in(client, username), user_id


def test_6_no_ownership_permission_means_no_owner_fields(client, estate):
    headers, _ = _viewer(client, estate)
    body = client.get(LIST_URL, headers=headers).json()
    for row in body["items"]:
        assert "owner_username" not in row
        assert "owner_user_id" not in row
        assert "owner_display_name" not in row


def test_7_ownership_permission_reveals_the_broadcaster(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_ownership")
    body = client.get(LIST_URL, headers=headers).json()
    names = {row["owner_username"] for row in body["items"]}
    assert names == {"alice", "bob", "carol"}


def test_8_owner_search_requires_ownership_permission(client, estate):
    """The leak this closes is the SHAPE of the answer.

    Searching "alice" without view_ownership must not return her broadcast,
    because a single result would disclose that Alice is broadcasting just as
    surely as an owner field would.
    """
    blind, _ = _viewer(client, estate, username="blind")
    seeing, _ = _viewer(client, estate, "broadcast.view_ownership", username="seeing")

    blind_body = client.get(LIST_URL, headers=blind, params={"q": "alice"}).json()
    seeing_body = client.get(LIST_URL, headers=seeing, params={"q": "alice"}).json()

    assert blind_body["total"] == 0, "owner name matched for an account that cannot see owners"
    assert seeing_body["total"] == 1


def test_9_owner_filter_is_refused_without_ownership_permission(client, estate):
    headers, _ = _viewer(client, estate)
    r = client.get(LIST_URL, headers=headers, params={"owner_user_id": 2})
    assert r.status_code == 403, "an unauthorized filter must be refused, not ignored"


def test_10_hidden_owner_data_is_absent_from_the_raw_body(client, estate):
    """Not "the frontend ignores it" - absent from the bytes on the wire."""
    headers, _ = _viewer(client, estate)
    raw = client.get(LIST_URL, headers=headers).text.lower()
    for leak in ("alice", "bob", "carol", "owner_user_id", "owner_username"):
        assert leak not in raw, f"{leak!r} was serialized to an account without view_ownership"


# ===========================================================================
# TARGET STORES (11-19)
# ===========================================================================
def test_11_no_target_permission_means_no_store_data(client, estate):
    headers, _ = _viewer(client, estate)
    body = client.get(LIST_URL, headers=headers).json()
    raw = client.get(LIST_URL, headers=headers).text
    for row in body["items"]:
        assert "target_store_ids" not in row
        assert "stores" not in row
    for store in estate["catalog"][:4]:
        assert store["store_name"] not in raw
        assert store["store_code"] not in raw


def test_12_target_permission_returns_exact_store_ids(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_targets")
    sid = estate["sessions"]["bob"]
    body = client.get(f"{LIST_URL}/{sid}/stores", headers=headers).json()
    returned = {s["store_id"] for s in body["stores"]}
    assert returned == {estate["catalog"][1]["id"], estate["catalog"][2]["id"]}


def test_13_store_short_names_are_returned(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_targets")
    sid = estate["sessions"]["bob"]
    body = client.get(f"{LIST_URL}/{sid}/stores", headers=headers).json()
    expected = {estate["catalog"][1]["store_code"], estate["catalog"][2]["store_code"]}
    assert {s["store_code"] for s in body["stores"]} == expected


def test_14_store_full_names_are_returned(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_targets")
    sid = estate["sessions"]["bob"]
    body = client.get(f"{LIST_URL}/{sid}/stores", headers=headers).json()
    expected = {estate["catalog"][1]["store_name"], estate["catalog"][2]["store_name"]}
    assert {s["store_name"] for s in body["stores"]} == expected


def test_15_store_search_requires_target_permission(client, estate):
    blind, _ = _viewer(client, estate, username="blind")
    seeing, _ = _viewer(client, estate, "broadcast.view_targets", username="seeing")
    needle = estate["catalog"][1]["store_name"]

    blind_body = client.get(LIST_URL, headers=blind, params={"q": needle}).json()
    seeing_body = client.get(LIST_URL, headers=seeing, params={"q": needle}).json()

    assert blind_body["total"] == 0, "a Store name matched for an account that cannot see Stores"
    assert seeing_body["total"] == 1


def test_16_store_filter_is_refused_without_target_permission(client, estate):
    headers, _ = _viewer(client, estate)
    r = client.get(LIST_URL, headers=headers,
                   params={"store_id": estate["catalog"][1]["id"]})
    assert r.status_code == 403


def test_17_detail_endpoint_refuses_without_target_permission(client, estate):
    headers, _ = _viewer(client, estate)
    sid = estate["sessions"]["bob"]
    r = client.get(f"{LIST_URL}/{sid}/stores", headers=headers)
    assert r.status_code == 403
    assert "bindapur" not in r.text.lower()


def test_18_detail_respects_store_scope(client, estate):
    """Scope narrows the Store list even for a full target-visibility holder."""
    headers, user_id = _viewer(client, estate, "broadcast.view_targets")
    # Scope to only ONE of Bob's two Stores.
    scope_to_stores(client, estate["owner"], user_id, [estate["catalog"][1]["id"]])
    sid = estate["sessions"]["bob"]
    body = client.get(f"{LIST_URL}/{sid}/stores", headers=headers).json()
    assert [s["store_id"] for s in body["stores"]] == [estate["catalog"][1]["id"]]


def test_19_target_count_reports_scope_survivors_only(client, estate):
    """A true total would let a scoped viewer measure what they cannot see."""
    headers, user_id = _viewer(client, estate, "broadcast.view_targets")
    scope_to_stores(client, estate["owner"], user_id, [estate["catalog"][1]["id"]])
    body = client.get(LIST_URL, headers=headers).json()
    bob_row = next(r for r in body["items"] if r["session_id"] == estate["sessions"]["bob"])
    assert bob_row["target_store_count"] == 1, "count leaked an out-of-scope Store"


# ===========================================================================
# STOP (20-31)
# ===========================================================================
def test_20_own_stop_works_with_ordinary_stop_permission(client, estate):
    """No supervision rights at all beyond opening the page."""
    alice_id = make_user(client, estate["owner"], "alice3")
    grant(client, estate["owner"], alice_id, "broadcast.active_view")
    headers = sign_in(client, "alice3")
    catalog = estate["catalog"]
    sid = start_broadcast(client, headers, "Own Campaign", [catalog[5]["id"]])

    r = client.post(f"{LIST_URL}/{sid}/stop", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ended"


def test_21_cross_owner_stop_refused_without_stop_any(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_ownership", "broadcast.view_targets")
    r = client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)
    assert r.status_code == 403


def test_22_cross_owner_stop_allowed_with_stop_any(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    r = client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)
    assert r.status_code == 200, r.text


def test_23_admin_explicit_deny_of_stop_any_is_enforced(client, estate):
    admin_id = make_user(client, estate["owner"], "supervisor", role="ADMIN")
    deny(client, estate["owner"], admin_id, "broadcast.stop_any")
    headers = sign_in(client, "supervisor")
    r = client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)
    assert r.status_code == 403, "DENY must beat the ADMIN default"
    # And the page itself still works - the DENY is narrow.
    assert client.get(LIST_URL, headers=headers).status_code == 200


def test_24_stop_any_does_not_reveal_store_names(client, estate):
    """The action without the disclosure. This is the whole point of keeping
    stop_any and view_targets independent."""
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    raw = client.get(LIST_URL, headers=headers).text
    for store in estate["catalog"][:4]:
        assert store["store_name"] not in raw
    assert client.get(f"{LIST_URL}/{estate['sessions']['bob']}/stores",
                      headers=headers).status_code == 403


def test_25_stop_any_does_not_reveal_owner_identity(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    raw = client.get(LIST_URL, headers=headers).text.lower()
    for name in ("alice", "bob", "carol"):
        assert name not in raw


def test_26_scope_blocks_stop_when_one_target_is_outside_it(client, estate):
    """Bob broadcasts to two Stores; the supervisor administers only one.

    Stop ends the WHOLE session, so allowing this would silence a Store the
    caller has no authority over - and one they cannot even see.
    """
    headers, user_id = _viewer(client, estate, "broadcast.stop_any")
    scope_to_stores(client, estate["owner"], user_id, [estate["catalog"][1]["id"]])

    r = client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)
    assert r.status_code == 403, r.text
    # The refusal counts the out-of-scope Stores but never names them.
    for store in estate["catalog"]:
        assert store["store_name"] not in r.text


def test_27_28_selected_stop_ends_only_that_session(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.stop_any", "broadcast.view_ownership")
    client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)

    body = client.get(LIST_URL, headers=headers).json()
    remaining = {row["owner_username"] for row in body["items"]}
    assert remaining == {"alice", "carol"}, "a selected Stop disturbed another session"


def test_29_30_selected_stop_releases_its_leases(client, estate):
    """The Store must become selectable again - a permanent BUSY Store is the
    failure this whole concurrency line of work keeps guarding against."""
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    freed = estate["catalog"][1]["id"]

    client.post(f"{LIST_URL}/{estate['sessions']['bob']}/stop", headers=headers)

    from broadcast_reservation import active_busy_store_ids
    server_module = client.server_module
    busy = active_busy_store_ids(server_module.engine)
    assert freed not in busy, "the lease survived the stop"

    # And a new broadcast can take it.
    dave_id = make_user(client, estate["owner"], "dave")
    assert dave_id
    reused = start_broadcast(client, sign_in(client, "dave"), "After Bob", [freed])
    assert reused


def test_31_cross_owner_stop_is_audited(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    sid = estate["sessions"]["bob"]
    client.post(f"{LIST_URL}/{sid}/stop", headers=headers)

    logs = client.get("/api/logs", headers=estate["owner"], params={"page_size": 200})
    assert logs.status_code == 200, logs.text
    text = logs.text
    assert "CROSS-OWNER STOP" in text
    assert f"session_id={sid}" in text
    # Ids and counts, never a secret.
    for forbidden in ("password", "Bearer ", "speaklink_rcv_v1"):
        assert forbidden not in text


# ===========================================================================
# EMERGENCY (32-35)
# ===========================================================================
def test_32_emergency_stop_permission_is_independent(client, estate):
    """stop_any must not confer Emergency Stop All."""
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    r = client.post("/api/broadcast/emergency-stop", headers=headers)
    assert r.status_code == 403, "stop_any granted estate-wide Emergency Stop"


def test_33_emergency_stop_still_stops_everything(client, estate):
    r = client.post("/api/broadcast/emergency-stop", headers=estate["owner"])
    assert r.status_code == 200, r.text
    assert len(r.json()["session_ids"]) == 3
    assert client.get(LIST_URL, headers=estate["owner"]).json()["total"] == 0


def test_34_stop_any_cannot_invoke_emergency_stop_all(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.stop_any")
    client.post("/api/broadcast/emergency-stop", headers=headers)
    # All three must still be live.
    assert client.get(LIST_URL, headers=estate["owner"]).json()["total"] == 3


def test_35_emergency_stop_does_not_grant_target_visibility(client, estate):
    user_id = make_user(client, estate["owner"], "emergency-only")
    grant(client, estate["owner"], user_id,
          "broadcast.active_view", "broadcast.emergency_stop")
    headers = sign_in(client, "emergency-only")
    assert client.get(f"{LIST_URL}/{estate['sessions']['bob']}/stores",
                      headers=headers).status_code == 403


# ===========================================================================
# SEARCH / PAGING (36-43)
# ===========================================================================
def test_36_37_pagination_is_server_side_with_a_safe_total(client, estate):
    headers = estate["owner"]
    page1 = client.get(LIST_URL, headers=headers, params={"page": 1, "page_size": 2}).json()
    page2 = client.get(LIST_URL, headers=headers, params={"page": 2, "page_size": 2}).json()

    assert len(page1["items"]) == 2, "page_size was not applied on the server"
    assert len(page2["items"]) == 1
    assert page1["total"] == 3 and page1["pages"] == 2
    assert page1["has_more"] is True and page2["has_more"] is False


def test_37b_total_counts_only_what_the_caller_may_know(client, estate):
    """A scoped viewer's total must not include broadcasts on Stores outside
    their scope - the number itself is a disclosure."""
    headers, user_id = _viewer(client, estate, "broadcast.view_targets")
    scope_to_stores(client, estate["owner"], user_id, [estate["catalog"][0]["id"]])
    body = client.get(LIST_URL, headers=headers,
                      params={"store_id": estate["catalog"][0]["id"]}).json()
    assert body["total"] == 1


def test_38_39_sorting_newest_and_oldest(client, estate):
    headers = estate["owner"]
    newest = client.get(LIST_URL, headers=headers, params={"sort": "newest"}).json()
    oldest = client.get(LIST_URL, headers=headers, params={"sort": "oldest"}).json()
    newest_ids = [r["session_id"] for r in newest["items"]]
    oldest_ids = [r["session_id"] for r in oldest["items"]]
    assert newest_ids == list(reversed(oldest_ids))
    assert newest_ids[0] == estate["sessions"]["carol"], "Carol started last"


def test_40_search_matches_a_store_short_name(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_targets")
    code = estate["catalog"][1]["store_code"]
    body = client.get(LIST_URL, headers=headers, params={"q": code}).json()
    assert [r["session_id"] for r in body["items"]] == [estate["sessions"]["bob"]]


def test_41_search_matches_a_store_full_name(client, estate):
    headers, _ = _viewer(client, estate, "broadcast.view_targets")
    name = estate["catalog"][3]["store_name"]
    body = client.get(LIST_URL, headers=headers, params={"q": name}).json()
    assert [r["session_id"] for r in body["items"]] == [estate["sessions"]["carol"]]


def test_42_search_matches_an_owner_only_with_permission(client, estate):
    seeing, _ = _viewer(client, estate, "broadcast.view_ownership", username="seeing")
    body = client.get(LIST_URL, headers=seeing, params={"q": "carol"}).json()
    assert [r["session_id"] for r in body["items"]] == [estate["sessions"]["carol"]]


def test_43_campaign_search_is_available_to_everyone_on_the_page(client, estate):
    """The safe field. Campaign names are already shown in the list, so
    searching them discloses nothing new - and this proves search still works
    for the most restricted account rather than being disabled wholesale."""
    headers, _ = _viewer(client, estate)
    body = client.get(LIST_URL, headers=headers, params={"q": "Weekend"}).json()
    assert [r["session_id"] for r in body["items"]] == [estate["sessions"]["bob"]]


def test_43b_a_query_cannot_leak_through_result_count(client, estate):
    """The subtle side channel, stated as its own test.

    An account with neither ownership nor target visibility searching a Store
    name or a broadcaster name must get NOTHING - not a redacted row, not a
    count. Otherwise "1 result" answers the question the permission refused.
    """
    headers, _ = _viewer(client, estate)
    for needle in ("alice", "carol", estate["catalog"][1]["store_name"],
                   estate["catalog"][1]["store_code"]):
        body = client.get(LIST_URL, headers=headers, params={"q": needle}).json()
        assert body["total"] == 0, f"{needle!r} leaked through the result count"


# ===========================================================================
# The console badge and the old panel
# ===========================================================================
def test_the_active_route_no_longer_leaks_targets_to_ownership_holders(client, estate):
    """A regression guard on the ORIGINAL endpoint.

    /api/broadcast/active used to send target_store_ids to anybody holding
    view_ownership, which made ownership visibility a back door to target
    visibility. The count stays; the ids need their own permission.
    """
    headers, _ = _viewer(client, estate, "broadcast.view_ownership")
    body = client.get("/api/broadcast/active", headers=headers).json()
    assert body["sessions"], "expected other operators' broadcasts to be listed"
    for row in body["sessions"]:
        assert "target_store_ids" not in row
        assert row["target_store_count"] >= 1


def test_the_console_count_is_withheld_without_active_view(client, estate):
    alice = sign_in(client, "alice")
    body = client.get("/api/broadcast/active", headers=alice).json()
    assert body["active_count"] is None
    assert body["may_manage_active"] is False


def test_the_console_count_is_present_with_active_view(client, estate):
    headers, _ = _viewer(client, estate)
    body = client.get("/api/broadcast/active", headers=headers).json()
    assert body["active_count"] == 3
    assert body["may_manage_active"] is True


# ===========================================================================
# The catalog itself
# ===========================================================================
def test_the_new_codes_are_assignable_through_the_rights_editor(client, estate):
    """An operator must be able to grant these without editing SQLite."""
    user_id = make_user(client, estate["owner"], "assignable")
    body = client.get(f"/api/users/{user_id}/permissions",
                      headers=estate["owner"]).json()
    codes = {row["code"]: row for row in body["permissions"]}
    for code in ("broadcast.active_view", "broadcast.view_ownership",
                 "broadcast.view_targets", "broadcast.stop_any"):
        assert code in codes, f"{code} is missing from the rights editor"
        assert codes[code]["group"] == "Broadcast"
        assert codes[code]["label"].startswith("Active Broadcasts")
        assert codes[code]["effective"] is False, "a BROADCASTER holds none by default"


def test_role_defaults_match_the_documented_matrix(client):
    from permission_catalog import DEFAULT_ROLE_PERMISSIONS
    from rbac import Role
    new_codes = {"broadcast.active_view", "broadcast.view_targets", "broadcast.stop_any"}
    assert new_codes <= DEFAULT_ROLE_PERMISSIONS[Role.OWNER]
    assert new_codes <= DEFAULT_ROLE_PERMISSIONS[Role.ADMIN]
    assert not (new_codes & DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER])
    assert not (new_codes & DEFAULT_ROLE_PERMISSIONS[Role.VIEWER])


def test_no_permission_implies_another(client, estate):
    """Each protects its own data. Explicitly asserted because implication is
    the easiest thing to introduce by accident later."""
    only_stop, _ = _viewer(client, estate, "broadcast.stop_any", username="only-stop")
    only_targets, _ = _viewer(client, estate, "broadcast.view_targets", username="only-targets")

    stop_body = client.get(LIST_URL, headers=only_stop).json()
    assert stop_body["meta"]["may_view_targets"] is False
    assert stop_body["meta"]["may_view_ownership"] is False

    targets_body = client.get(LIST_URL, headers=only_targets).json()
    assert targets_body["meta"]["may_view_ownership"] is False
    assert targets_body["meta"]["may_stop_any"] is False
