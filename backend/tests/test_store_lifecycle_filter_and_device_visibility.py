"""Two review defects: deleted Devices on screen, and an ambiguous lifecycle filter.

BUG 1. ``describe_store_devices`` selected every row for a Store with no
exclusion at all, so a permanently deleted Receiver Device kept appearing in
the per-Store Device list. ``status`` cannot identify one - an ordinarily
retired Device is also ``retired`` - so the marker is ``deleted_at``.

BUG 2. Store Management had no lifecycle control, and the Receiver Devices
screen had two controls for one concern whose flags latched on and never
cleared. One explicit lifecycle selection now replaces the previous one.

The invariant both share: **a permanently deleted row is operationally gone
and historically preserved.** Nothing here deletes history, and nothing
invents a restore path.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
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
                               "deletion_safety", "user_deletion", "store_deletion",
                               "device_deletion", "receiver_primary_device")]:
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


def make_user(client, headers, username, role="ADMIN"):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def stores(client, headers, **params):
    r = client.get("/api/stores/search", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def add_device(client, store_id, *, status="active", archived=False, deleted=False):
    """Insert a Device directly. Enrolment needs a code and a key ring; this
    test is about visibility, not about how a Device came to exist."""
    from sqlalchemy import text
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    public_id = str(uuid.uuid4())
    engine = client.server_module.engine
    with engine.begin() as c:
        device_id = c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, created_at, updated_at, disabled_at, archived_at, deleted_at) "
            "VALUES (:p, :s, :n, :st, :now, :now, :now, :dis, :arc, :del)"),
            {"p": public_id, "s": store_id, "n": f"Till {public_id[:4]}",
             "st": status, "now": now,
             "dis": now if status != "active" else None,
             "arc": now if archived else None,
             "del": now if deleted else None}).lastrowid
    return {"id": device_id, "public_id": public_id}


def devices_of(client, headers, store_id):
    r = client.get(f"/api/stores/{store_id}/receiver-devices/roles", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ===========================================================================
# BUG 1 - permanently deleted Receiver Devices must be operationally gone
# ===========================================================================
def test_an_active_device_appears(client):
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    made = add_device(client, store["id"])
    shown = {d["public_id"] for d in devices_of(client, owner, store["id"])}
    assert made["public_id"] in shown


def test_a_retired_device_still_appears_because_retiring_is_not_deleting(client):
    """Retired is an operational state an administrator must still see -
    that is how somebody notices a Device that needs replacing."""
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    made = add_device(client, store["id"], status="retired")
    shown = {d["public_id"] for d in devices_of(client, owner, store["id"])}
    assert made["public_id"] in shown


def test_an_archived_device_still_appears_because_archiving_is_reversible(client):
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    made = add_device(client, store["id"], status="disabled", archived=True)
    shown = {d["public_id"] for d in devices_of(client, owner, store["id"])}
    assert made["public_id"] in shown


def test_a_permanently_deleted_device_never_appears(client):
    """The defect. deleted_at is the marker - status cannot be, because a
    permanently deleted Device is 'retired' and so is an ordinary one."""
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    kept = add_device(client, store["id"])
    gone = add_device(client, store["id"], status="retired", deleted=True)

    shown = {d["public_id"] for d in devices_of(client, owner, store["id"])}
    assert kept["public_id"] in shown
    assert gone["public_id"] not in shown, (
        "a permanently deleted Device must be operationally gone")


def test_a_permanently_deleted_device_is_absent_from_the_plain_device_list_too(client):
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    gone = add_device(client, store["id"], status="retired", deleted=True)
    response = client.get(f"/api/stores/{store['id']}/receiver-devices", headers=owner)
    assert response.status_code == 200, response.text
    assert gone["public_id"] not in {d["public_id"] for d in response.json()}


def test_a_deleted_primary_device_cannot_leak_through_the_primary_join(client):
    """describe_store_devices LEFT JOINs the primary table. A deleted Device
    that was primary must not reappear through that join."""
    from sqlalchemy import text

    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    gone = add_device(client, store["id"], status="retired", deleted=True)
    with client.server_module.engine.begin() as c:
        c.execute(text("INSERT INTO receiver_store_primary_device "
                       "(store_id, device_id, promoted_at) VALUES (:s, :d, :n)"),
                  {"s": store["id"], "d": gone["id"], "n": "2026-08-02T00:00:00+00:00"})

    shown = {d["public_id"] for d in devices_of(client, owner, store["id"])}
    assert gone["public_id"] not in shown


def test_the_deleted_device_row_and_its_history_are_preserved(client):
    """Operationally gone, historically intact. The row still exists so every
    credential event and broadcast record that names it stays readable."""
    from sqlalchemy import text

    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    gone = add_device(client, store["id"], status="retired", deleted=True)

    with client.server_module.engine.connect() as c:
        row = c.execute(text(
            "SELECT public_id, deleted_at FROM receiver_devices WHERE public_id = :p"),
            {"p": gone["public_id"]}).one_or_none()
    assert row is not None, "the row must NOT be physically deleted"
    assert row.deleted_at is not None


def test_device_visibility_still_requires_the_receiver_permission(client):
    owner = sign_in(client)
    store = stores(client, owner)["items"][0]
    make_user(client, owner, "broadcaster", "BROADCASTER")
    headers = sign_in(client, "broadcaster")
    # BROADCASTER holds menu.receivers.view; the point is that nothing about
    # this fix changed who may look.
    assert client.get(f"/api/stores/{store['id']}/receiver-devices/roles",
                      headers=headers).status_code == 200


# ===========================================================================
# BUG 2 - one lifecycle selection, and it REPLACES the previous one
# ===========================================================================
def _codes(body):
    return {row["store_code"] for row in body["items"]}


def test_the_default_store_view_is_active_only(client):
    owner = sign_in(client)
    every = stores(client, owner, lifecycle="all_current")["items"]
    target = every[0]
    client.post(f"/api/stores/{target['id']}/disable", headers=owner)

    default = stores(client, owner)
    assert target["store_code"] not in _codes(default)
    assert all(r["lifecycle_state"] == "active" for r in default["items"])


def test_lifecycle_active_returns_only_active(client):
    owner = sign_in(client)
    target = stores(client, owner)["items"][0]
    client.post(f"/api/stores/{target['id']}/disable", headers=owner)
    body = stores(client, owner, lifecycle="active")
    assert {r["lifecycle_state"] for r in body["items"]} == {"active"}


def test_lifecycle_disabled_returns_only_disabled(client):
    owner = sign_in(client)
    target = stores(client, owner)["items"][0]
    client.post(f"/api/stores/{target['id']}/disable", headers=owner)
    body = stores(client, owner, lifecycle="disabled")
    assert {r["lifecycle_state"] for r in body["items"]} == {"disabled"}
    assert body["total"] == 1


def test_lifecycle_archived_returns_only_archived(client):
    owner = sign_in(client)
    target = stores(client, owner)["items"][0]
    client.post(f"/api/stores/{target['id']}/archive", headers=owner)
    body = stores(client, owner, lifecycle="archived")
    assert {r["lifecycle_state"] for r in body["items"]} == {"archived"}
    assert body["total"] == 1


def test_all_current_includes_every_state_except_deleted(client):
    owner = sign_in(client)
    rows = stores(client, owner)["items"]
    disabled, archived = rows[0], rows[1]
    client.post(f"/api/stores/{disabled['id']}/disable", headers=owner)
    client.post(f"/api/stores/{archived['id']}/archive", headers=owner)

    body = stores(client, owner, lifecycle="all_current")
    states = {r["lifecycle_state"] for r in body["items"]}
    assert {"active", "disabled", "archived"} <= states
    assert "deleted" not in states


def test_one_lifecycle_replaces_another_rather_than_adding_to_it(client):
    """The reported symptom: choosing Archived then Active showed both."""
    owner = sign_in(client)
    rows = stores(client, owner)["items"]
    archived = rows[0]
    client.post(f"/api/stores/{archived['id']}/archive", headers=owner)

    only_archived = stores(client, owner, lifecycle="archived")
    assert _codes(only_archived) == {archived["store_code"]}

    only_active = stores(client, owner, lifecycle="active")
    assert archived["store_code"] not in _codes(only_active), (
        "selecting Active must not still show the previously selected Archived")


def test_deleted_cannot_be_requested_through_the_normal_store_endpoint(client):
    owner = sign_in(client)
    response = client.get("/api/stores/search", headers=owner,
                          params={"lifecycle": "deleted"})
    assert response.status_code == 400
    assert "deleted" in response.json()["detail"].lower()


def test_a_deleted_store_is_absent_from_every_lifecycle_selection(client):
    owner = sign_in(client)
    target = stores(client, owner)["items"][2]
    response = client.post(f"/api/stores/{target['id']}/delete-permanently", headers=owner,
                           json={"confirm": target["store_code"], "acknowledged": True})
    assert response.status_code == 200, response.text

    for selection in ("all_current", "active", "disabled", "archived"):
        body = stores(client, owner, lifecycle=selection)
        assert target["store_code"] not in _codes(body), f"leaked via {selection}"


def test_an_unknown_lifecycle_is_refused_rather_than_ignored(client):
    """Ignoring it would silently return the default set, which reads as a
    working filter that does nothing."""
    owner = sign_in(client)
    response = client.get("/api/stores/search", headers=owner,
                          params={"lifecycle": "nonsense"})
    assert response.status_code == 400


def test_search_zone_city_and_lifecycle_combine_with_and_semantics(client):
    owner = sign_in(client)
    rows = stores(client, owner, lifecycle="all_current")["items"]
    target = rows[0]
    body = stores(client, owner, lifecycle="active", region=target["region"],
                  city=target["city"], q=target["store_code"])
    assert body["total"] >= 1
    for row in body["items"]:
        assert row["region"] == target["region"]
        assert row["city"] == target["city"]
        assert row["lifecycle_state"] == "active"


def test_store_scope_still_applies_to_every_lifecycle_selection(client):
    from store_scope import set_user_scope

    owner = sign_in(client)
    rows = stores(client, owner)["items"]
    mine = rows[0]
    user_id = make_user(client, owner, "scoped", "ADMIN")
    set_user_scope(client.server_module.engine, user_id=user_id,
                   entries=[{"scope_type": "STORE", "store_id": mine["id"]}], actor_id=1)
    scoped = sign_in(client, "scoped")

    for selection in ("all_current", "active"):
        body = stores(client, scoped, lifecycle=selection)
        assert body["total"] <= 1
        assert _codes(body) <= {mine["store_code"]}


def test_the_total_matches_the_filtered_result(client):
    owner = sign_in(client)
    target = stores(client, owner)["items"][0]
    client.post(f"/api/stores/{target['id']}/archive", headers=owner)
    body = stores(client, owner, lifecycle="archived", page_size=200)
    assert body["total"] == len(body["items"]) == 1
