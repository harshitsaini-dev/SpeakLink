"""/api/receiver-devices/search must never return a permanently deleted Device.

THE HOLE THIS CLOSES

RC18 removed the "Permanently deleted" option from the Receiver Devices
screen, so the UI stopped asking for tombstones. The QUERY PARAMETER that
served that option survived, and the endpoint still honoured it: a
hand-crafted ``?include_deleted=true`` returned every tombstone on a live
system. Removing a control from the UI is not the same as removing a
capability from an API - anyone who can reach the endpoint can type a URL.

The contract these tests pin is deliberately absolute, because a conditional
one is what failed: this endpoint NEVER returns a Device with deleted_at set,
under any combination of parameters. There is no opt-in, so there is nothing
to latch, mistype, or leave enabled.

WHAT DELIBERATELY STILL WORKS

A permanently deleted Device is operationally gone, not erased. Its row
stays, its deletion-event record stays readable, and the per-Device history
endpoints still resolve it. "Gone from the operational list" and "kept for
history" are the two halves of a tombstone and both are asserted here.

Archived and retired Devices are NOT tombstones and must keep appearing.
Retired is the trap: a permanently deleted Device is also status='retired',
so an implementation tempted to filter on status would hide the ordinary
retired Devices somebody still needs to see. That case has its own test.
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
SEARCH = "/api/receiver-devices/search"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for name in [m for m in list(sys.modules) if m in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "user_deletion", "device_deletion", "receiver_enrollment_api",
            "store_scope")]:
        sys.modules.pop(name, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login",
                    json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def stores(client, headers):
    return client.get("/api/stores", headers=headers).json()


def add_device(engine, store_id, *, status="active", name="Store PC"):
    from sqlalchemy import text
    public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        # ck_receiver_devices_disabled_state: an active Device has no
        # disabled_at; a disabled or retired one must have it.
        disabled_at = None if status == "active" else now
        c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, "
            "status, enrolled_at, disabled_at, created_at, updated_at) "
            "VALUES (:p,:s,:n,:st,:now,:dis,:now,:now)"),
            {"p": public_id, "s": store_id, "n": name, "st": status,
             "dis": disabled_at, "now": now})
    return public_id


def purge(client, headers, public_id):
    """Permanently delete through the real endpoint, not by writing SQL."""
    r = client.post(f"/api/receiver-devices/{public_id}/delete-permanently",
                    headers=headers, json={"confirm": public_id,
                                           "acknowledged": True})
    assert r.status_code == 200, r.text
    return r


def ids_returned(client, headers, **params):
    params.setdefault("page_size", 200)
    r = client.get(SEARCH, headers=headers, params=params)
    assert r.status_code == 200, r.text
    return {i["public_id"] for i in r.json()["items"]}


# ===========================================================================
# The tombstone is unreachable
# ===========================================================================
def test_a_default_query_hides_a_permanently_deleted_device(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    assert gone not in ids_returned(client, owner)


def test_include_deleted_true_cannot_expose_a_tombstone(client):
    """The exact hand-crafted request that worked on the live system."""
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    # Whether the parameter is rejected outright or silently carries no
    # meaning is an implementation choice. That the tombstone stays hidden
    # is not, so this asserts the property rather than the mechanism.
    r = client.get(SEARCH, headers=owner,
                   params={"include_deleted": True, "page_size": 200})
    if r.status_code == 200:
        assert gone not in {i["public_id"] for i in r.json()["items"]}
    else:
        assert r.status_code == 400, r.text


def test_include_deleted_combined_with_every_lifecycle_still_hides_it(client):
    """Belt and braces: the old defect was a latch that survived a change of
    lifecycle, so every combination is tried rather than the default one."""
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    for lifecycle in (None, "", "all_current", "active", "archived"):
        params = {"include_deleted": True, "page_size": 200}
        if lifecycle is not None:
            params["lifecycle"] = lifecycle
        r = client.get(SEARCH, headers=owner, params=params)
        if r.status_code == 200:
            leaked = {i["public_id"] for i in r.json()["items"]}
            assert gone not in leaked, f"leaked with lifecycle={lifecycle!r}"


def test_lifecycle_deleted_cannot_expose_a_tombstone(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    r = client.get(SEARCH, headers=owner,
                   params={"lifecycle": "deleted", "page_size": 200})
    if r.status_code == 200:
        assert gone not in {i["public_id"] for i in r.json()["items"]}
    else:
        assert r.status_code == 400, r.text


def test_a_tombstone_is_not_reachable_by_searching_for_its_own_id(client):
    """Search is the other way in. A free-text term that matches the tombstone
    exactly must still return nothing - filters and search are ANDed, and it
    would be easy to apply the exclusion to only one of them."""
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"],
                      name="Doomed Till")
    purge(client, owner, gone)

    assert gone not in ids_returned(client, owner, q=gone)
    assert gone not in ids_returned(client, owner, q="Doomed Till")


# ===========================================================================
# What must NOT be hidden
# ===========================================================================
def test_an_ordinary_retired_device_is_still_visible(client):
    """The trap. A permanently deleted Device is status='retired' too, so an
    implementation that filtered on status would hide this one as well."""
    owner = sign_in(client)
    store = stores(client, owner)[0]
    retired = add_device(client.server_module.engine, store["id"],
                         status="retired", name="Old Till")

    assert retired in ids_returned(client, owner)


def test_an_archived_device_remains_visible_where_intended(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    archived = add_device(client.server_module.engine, store["id"])
    r = client.post(f"/api/receiver-devices/{archived}/archive", headers=owner)
    assert r.status_code == 200, r.text

    assert archived in ids_returned(client, owner)
    assert archived in ids_returned(client, owner, lifecycle="archived")
    assert archived in ids_returned(client, owner, lifecycle="all_current")


def test_archived_and_deleted_remain_distinguishable(client):
    """Both are 'not ordinary', and conflating them is how a restorable
    Device gets treated as unrecoverable. The archived one is returned and
    labelled; the deleted one is simply absent."""
    owner = sign_in(client)
    store = stores(client, owner)[0]
    engine = client.server_module.engine
    archived = add_device(engine, store["id"])
    gone = add_device(engine, store["id"])
    client.post(f"/api/receiver-devices/{archived}/archive", headers=owner)
    purge(client, owner, gone)

    rows = {i["public_id"]: i for i in
            client.get(SEARCH, headers=owner,
                       params={"page_size": 200}).json()["items"]}
    assert rows[archived]["lifecycle"] == "archived"
    assert gone not in rows


def test_a_live_device_is_unaffected(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    engine = client.server_module.engine
    alive = add_device(engine, store["id"])
    gone = add_device(engine, store["id"])
    purge(client, owner, gone)

    returned = ids_returned(client, owner)
    assert alive in returned
    assert gone not in returned


# ===========================================================================
# History is preserved - the other half of a tombstone
# ===========================================================================
def test_the_row_still_exists_after_the_endpoint_stops_returning_it(client):
    """Operationally gone is not erased. If this ever fails, the fix has
    started deleting history instead of hiding it."""
    from sqlalchemy import text
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    with client.server_module.engine.begin() as c:
        row = c.execute(text("SELECT deleted_at FROM receiver_devices "
                             "WHERE public_id = :p"), {"p": gone}).fetchone()
    assert row is not None, "the tombstone row was destroyed"
    assert row[0] is not None, "deleted_at was not recorded"


def test_the_deletion_event_history_still_reads_the_tombstone(client):
    owner = sign_in(client)
    store = stores(client, owner)[0]
    gone = add_device(client.server_module.engine, store["id"])
    purge(client, owner, gone)

    r = client.get(f"/api/receiver-devices/{gone}/deletion-events", headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["events"], \
        "the deletion audit lost the event for a Device it deleted"


# ===========================================================================
# Scope is unchanged by this fix
# ===========================================================================
def test_store_scope_still_narrows_the_list(client):
    """The exclusion must be ANDed with Scope, not replace it. A fix that
    rebuilt the WHERE clause could easily drop the scope filter, and the
    result would look correct to an unrestricted account."""
    from store_scope import set_user_scope
    owner = sign_in(client)
    catalog = stores(client, owner)
    mine, theirs = catalog[0], catalog[1]
    engine = client.server_module.engine

    inside = add_device(engine, mine["id"], name="Mine")
    outside = add_device(engine, theirs["id"], name="Theirs")
    gone = add_device(engine, mine["id"])
    purge(client, owner, gone)

    created = client.post("/api/users", headers=owner, json={
        "username": "scoped", "display_name": "Scoped",
        "role": "ADMIN", "password": PASSWORD}).json()
    set_user_scope(engine, user_id=created["id"], actor_id=1,
                   entries=[{"scope_type": "STORE", "store_id": mine["id"]}])
    scoped = sign_in(client, "scoped")

    seen = ids_returned(client, scoped)
    assert inside in seen
    assert outside not in seen, "Scope was lost"
    assert gone not in seen
