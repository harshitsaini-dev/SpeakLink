"""True permanent deletion of a Store: the row goes, the Store Code is freed.

THE DEFECT THIS FILE EXISTS FOR

An operator permanently deleted the Store AYUSHK. It vanished from the Store
list, and then:

    Add Store -> store_code AYUSHK  ->  "store_code already exists"

The old design tombstoned the row, so the UNIQUE index kept the code for ever.
``store_deletion.py`` said so in its own docstring - the code was "never handed
out to a new Store afterward". A Store that still occupies the code namespace
has not been deleted; it has been hidden.

THE SECURITY PROPERTY THAT MATTERS MOST

A reused Store Code is NOT the old Store. The replacement must inherit no
Receiver Device, no credential, no primary assignment, no enrolment code, no
Store Scope and no lease - and critically, a Receiver holding the OLD Store's
credential must not be able to authenticate as the new one.

THE ID-REUSE TRAP

``stores.id`` had no AUTOINCREMENT, so SQLite reissues ``max(id) + 1``. In the
live database the tombstones were ids 58, 59 and 60 - and 60 was the maximum,
so deleting it and adding any Store would have handed the new Store id 60 plus
every history row still pointing there.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

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
    for module in [n for n in list(sys.modules) if n in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "admin_search",
            "store_lifecycle", "store_deletion", "store_permanent_delete",
            "user_permanent_delete", "sqlite_schema_surgery", "store_scope",
            "deletion_safety", "ws_manager", "broadcast_reservation")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sign_in(client, username="founder", password=PASSWORD):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def make_store(client, headers, code, name=None):
    r = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": name or f"{code} Shop",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    assert r.status_code == 201, r.text
    return r.json()


def delete_store(client, headers, store_id, confirm):
    return client.post(f"/api/stores/{store_id}/delete-permanently", headers=headers,
                       json={"confirm": confirm, "acknowledged": True})


def scalar(client, sql, **params):
    with client.server_module.engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def rows(client, sql, **params):
    with client.server_module.engine.connect() as c:
        return c.execute(text(sql), params).all()


def enrol_a_device(client, headers, store_id):
    """A Device with an active credential, a primary assignment, a pending
    enrolment code and a Receiver event.

    Written directly rather than through the enrolment API because enrolment
    needs an HMAC key container this test environment deliberately does not
    have - the container is real key custody, not a fixture. The rows written
    here are exactly the shape the API produces, which is what the deletion
    has to handle.
    """
    import uuid
    from datetime import datetime, timezone

    engine = client.server_module.engine
    device_public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        device_id = connection.execute(
            text("INSERT INTO receiver_devices "
                 "(public_id, store_id, display_name, status, enrolled_at, "
                 "created_at, updated_at) "
                 "VALUES (:pid, :sid, 'QA till', 'active', :now, :now, :now) "
                 "RETURNING id"),
            {"pid": device_public_id, "sid": store_id, "now": now}).scalar_one()
        connection.execute(
            text("INSERT INTO receiver_credentials "
                 "(public_id, device_id, credential_version, token_format, "
                 "token_hash, hash_key_version, status, issued_at, created_at) "
                 "VALUES (:cp, :did, 1, 'echocast_rcv', :hash, 1, 'active', :now, :now)"),
            # token_hash is UNIQUE, so it must differ per Device - two Devices
            # in one test otherwise collide on the second insert.
            {"cp": str(uuid.uuid4()), "did": device_id,
             "hash": uuid.uuid4().hex + uuid.uuid4().hex, "now": now})
        connection.execute(
            text("INSERT INTO receiver_store_primary_device "
                 "(store_id, device_id, promoted_at) VALUES (:sid, :did, :now)"),
            {"sid": store_id, "did": device_id, "now": now})
        connection.execute(
            text("INSERT INTO receiver_events (store_id, event_type, event_time, details) "
                 "VALUES (:sid, 'connected', :now, 'QA history')"),
            {"sid": store_id, "now": now})
        connection.execute(
            text("INSERT INTO receiver_enrollment_codes "
                 "(code_hash, store_id, created_by, created_at, expires_at_epoch) "
                 "VALUES (:hash, :sid, 1, :now, :exp)"),
            {"hash": uuid.uuid4().hex + uuid.uuid4().hex, "sid": store_id, "now": now,
             "exp": datetime.now(timezone.utc).timestamp() + 3600})
    return {"device_public_id": device_public_id, "device_id": device_id}


# ===========================================================================
# 1-3  ARCHIVE is unchanged
# ===========================================================================
def test_1_archive_keeps_the_store_row(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    assert client.post(f"/api/stores/{store['id']}/archive",
                       headers=owner).status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 1


def test_2_archive_still_reserves_the_store_code(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    client.post(f"/api/stores/{store['id']}/archive", headers=owner)

    clash = client.post("/api/stores", headers=owner, json={
        "store_code": "QADEL", "store_name": "Impostor",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    assert clash.status_code == 409, clash.text


def test_3_restore_returns_the_same_store_id(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    client.post(f"/api/stores/{store['id']}/archive", headers=owner)
    assert client.post(f"/api/stores/{store['id']}/restore",
                       headers=owner).status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 1


# ===========================================================================
# 4-9  PERMANENT DELETE is real
# ===========================================================================
def test_4_permanent_delete_physically_removes_the_row(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    assert delete_store(client, owner, store["id"], "QADEL").status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 0


def test_5_6_a_deleted_store_is_absent_from_every_listing(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    delete_store(client, owner, store["id"], "QADEL")

    listed = {s["id"] for s in client.get("/api/stores", headers=owner).json()}
    assert store["id"] not in listed

    for params in ({}, {"lifecycle": "deleted"}, {"include_deleted": True},
                   {"state": "deleted"}, {"q": "QADEL"}):
        found = client.get("/api/stores/search", headers=owner,
                           params={**params, "page_size": 200}).json()
        codes = {s["store_code"] for s in found.get("items", [])}
        assert "QADEL" not in codes, f"revealed by {params}"


def test_7_8_the_store_code_becomes_reusable_as_a_new_identity(client):
    owner = sign_in(client)
    old = make_store(client, owner, "QADEL")
    delete_store(client, owner, old["id"], "QADEL")

    created = client.post("/api/stores", headers=owner, json={
        "store_code": "QADEL", "store_name": "A Different Shop",
        "city": "TESTVILLE", "region": "TEST ZONE"})
    assert created.status_code == 201, created.text
    assert created.json()["id"] != old["id"], (
        "the replacement Store reused the deleted Store's id")


def test_9_store_ids_are_never_reissued(client):
    owner = sign_in(client)
    first = make_store(client, owner, "QADEL")
    delete_store(client, owner, first["id"], "QADEL")
    second = make_store(client, owner, "QAOTHER")
    assert second["id"] != first["id"], "a deleted Store id was handed straight back out"


# ===========================================================================
# 10-16  the replacement inherits nothing
# ===========================================================================
def test_10_16_a_replacement_store_inherits_no_receiver_identity(client):
    """Release-blocking. The whole point of separating code from identity."""
    owner = sign_in(client)
    old = make_store(client, owner, "QADEL")
    enrolled = enrol_a_device(client, owner, old["id"])
    assert scalar(client, "SELECT COUNT(*) FROM receiver_devices WHERE store_id = :i",
                  i=old["id"]) == 1

    delete_store(client, owner, old["id"], "QADEL")
    new = make_store(client, owner, "QADEL", "A Different Shop")

    for table in ("receiver_devices", "receiver_enrollment_codes",
                  "receiver_events", "broadcast_targets", "user_store_scope"):
        assert scalar(client, f"SELECT COUNT(*) FROM {table} WHERE store_id = :i",
                      i=new["id"]) == 0, f"{table} transferred to the new Store"

    # No live credential survives anywhere from the old Store's Devices.
    live = scalar(client,
                  "SELECT COUNT(*) FROM receiver_credentials r "
                  "JOIN receiver_devices d ON d.id = r.device_id "
                  "WHERE d.store_code_snapshot = 'QADEL' AND r.status = 'active'")
    assert live == 0, "an old Store credential is still active"

    # And the new Store has its own receiver_token, not the old one.
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i "
                          "AND receiver_token = :t", i=new["id"],
                  t=old.get("receiver_token") or "never-matches") == 0
    assert enrolled


def test_16b_an_old_device_cannot_authenticate_as_the_new_store(client):
    """The Device row survives as history, retired and detached, so nothing
    that pointed at the old Store can serve the new one."""
    owner = sign_in(client)
    old = make_store(client, owner, "QADEL")
    enrol_a_device(client, owner, old["id"])

    delete_store(client, owner, old["id"], "QADEL")
    new = make_store(client, owner, "QADEL", "A Different Shop")

    device = rows(client, "SELECT store_id, status, store_code_snapshot "
                          "FROM receiver_devices WHERE store_code_snapshot = 'QADEL'")[0]
    assert device.store_id is None, "the old Device still points at a Store"
    assert device.status == "retired"
    assert device.store_code_snapshot == "QADEL"
    assert scalar(client, "SELECT COUNT(*) FROM receiver_devices WHERE store_id = :i",
                  i=new["id"]) == 0


# ===========================================================================
# 17-20  history survives and stays with the OLD Store
# ===========================================================================
def test_17_18_old_history_does_not_bind_to_the_new_same_code_store(client):
    owner = sign_in(client)
    old = make_store(client, owner, "QADEL")
    session = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Autumn Sale", "target_mode": "selected",
        "store_ids": [old["id"]]})
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]

    delete_store(client, owner, old["id"], "QADEL")
    new = make_store(client, owner, "QADEL", "A Different Shop")
    assert new["id"] != old["id"]

    target = rows(client, "SELECT store_id, store_code_snapshot, store_name_snapshot "
                          "FROM broadcast_targets WHERE session_id = :i", i=session_id)[0]
    assert target.store_id is None, "the target still names a live Store"
    assert target.store_id != new["id"]
    assert target.store_code_snapshot == "QADEL"

    # And the API still renders it, from the snapshot rather than a lookup.
    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=owner)
    assert detail.status_code == 200, detail.text
    rendered = detail.json()["targets"][0]
    assert rendered["store_code"] == "QADEL"
    assert rendered["store_id"] is None

    bound = scalar(client, "SELECT COUNT(*) FROM broadcast_targets t "
                           "JOIN stores s ON s.id = t.store_id WHERE t.session_id = :i",
                   i=session_id)
    assert bound == 0


def test_19_receiver_history_remains_readable(client):
    owner = sign_in(client)
    old = make_store(client, owner, "QADEL")
    enrol_a_device(client, owner, old["id"])
    before = scalar(client, "SELECT COUNT(*) FROM receiver_events WHERE store_id = :i",
                    i=old["id"])

    delete_store(client, owner, old["id"], "QADEL")

    after = scalar(client, "SELECT COUNT(*) FROM receiver_events "
                           "WHERE store_code_snapshot = 'QADEL'")
    assert after == before, "Receiver history was destroyed rather than detached"


def test_20_the_deletion_audit_is_preserved(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    delete_store(client, owner, store["id"], "QADEL")

    audit = rows(client, "SELECT store_id, store_code, store_name "
                         "FROM store_deletion_events WHERE store_id = :i", i=store["id"])
    assert audit, "the deletion left no administrative audit record"
    assert audit[-1].store_code == "QADEL"
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 0


def test_20b_the_audit_carries_no_token_or_credential(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    token = scalar(client, "SELECT receiver_token FROM stores WHERE id = :i", i=store["id"])
    enrol_a_device(client, owner, store["id"])
    delete_store(client, owner, store["id"], "QADEL")

    with client.server_module.engine.connect() as c:
        blob = str(c.execute(text("SELECT * FROM store_deletion_events")).all())
    assert token not in blob
    for forbidden in ("echocast_rcv_v1", "password", "bearer"):
        assert forbidden not in blob.lower()


# ===========================================================================
# 21-25  integrity, isolation and the live-broadcast guard
# ===========================================================================
def test_21_22_the_database_stays_sound(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    enrol_a_device(client, owner, store["id"])
    delete_store(client, owner, store["id"], "QADEL")

    with client.server_module.engine.connect() as c:
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
        # Foreign keys stayed ON - the deletion did not work by disabling them.
        assert c.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_23_24_unrelated_stores_and_devices_are_untouched(client):
    owner = sign_in(client)
    keeper = make_store(client, owner, "QAKEEP")
    keeper_device = enrol_a_device(client, owner, keeper["id"])
    doomed = make_store(client, owner, "QADEL")
    enrol_a_device(client, owner, doomed["id"])

    delete_store(client, owner, doomed["id"], "QADEL")

    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=keeper["id"]) == 1
    assert scalar(client, "SELECT COUNT(*) FROM receiver_devices WHERE store_id = :i",
                  i=keeper["id"]) == 1
    still_active = scalar(client,
                          "SELECT COUNT(*) FROM receiver_credentials r "
                          "JOIN receiver_devices d ON d.id = r.device_id "
                          "WHERE d.store_id = :i AND r.status = 'active'", i=keeper["id"])
    assert still_active == 1, "an unrelated Store's credential was revoked"
    assert keeper_device


def test_25_a_live_broadcast_refuses_the_deletion(client):
    """Deleting a Store must not silence somebody else's announcement."""
    import asyncio
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    session = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "on air", "target_mode": "selected",
        "store_ids": [store["id"]]})
    session_id = session.json()["id"]

    from ws_manager import manager
    asyncio.run(manager.broadcasts.start(
        session_id=session_id, owner_user_id=1, target_store_ids={store["id"]}))
    try:
        refusal = delete_store(client, owner, store["id"], "QADEL")
        assert refusal.status_code == 409, refusal.text
        assert "on air" in refusal.json()["detail"].lower()
        assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i",
                      i=store["id"]) == 1
    finally:
        asyncio.run(manager.broadcasts.end(session_id))


# ===========================================================================
# 26-29  authorization, confirmation and rollback
# ===========================================================================
def test_26_the_wrong_permission_is_refused(client):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    client.post("/api/users", headers=owner, json={
        "username": "opsadmin", "display_name": "Ops", "role": "ADMIN",
        "password": PASSWORD})

    refusal = delete_store(client, sign_in(client, "opsadmin"), store["id"], "QADEL")
    assert refusal.status_code == 403
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 1


@pytest.mark.parametrize("body", [
    {"confirm": "wrong-code", "acknowledged": True},
    {"confirm": "QADEL", "acknowledged": False},
    {"acknowledged": True},
])
def test_28_malformed_confirmation_is_a_safe_4xx(client, body):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    resp = client.post(f"/api/stores/{store['id']}/delete-permanently",
                       headers=owner, json=body)
    assert 400 <= resp.status_code < 500, resp.text
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 1


def test_29_a_failure_midway_rolls_the_whole_thing_back(client, monkeypatch):
    owner = sign_in(client)
    store = make_store(client, owner, "QADEL")
    enrol_a_device(client, owner, store["id"])

    import store_permanent_delete as spd
    original = spd._neutralise_receiver_identity

    def explode(connection, store_id):
        original(connection, store_id)
        raise RuntimeError("simulated failure part-way through the transaction")

    monkeypatch.setattr(spd, "_neutralise_receiver_identity", explode)
    assert delete_store(client, owner, store["id"], "QADEL").status_code == 500
    monkeypatch.undo()

    # Nothing happened: the Store is there, its Device is not retired, and its
    # credential still works.
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=store["id"]) == 1
    device = rows(client, "SELECT store_id, status FROM receiver_devices "
                          "WHERE store_id = :i", i=store["id"])[0]
    assert device.status == "active"
    assert scalar(client, "SELECT COUNT(*) FROM receiver_credentials r "
                          "JOIN receiver_devices d ON d.id = r.device_id "
                          "WHERE d.store_id = :i AND r.status = 'active'",
                  i=store["id"]) == 1


# ===========================================================================
# 30-34  migration and the canonical catalog
# ===========================================================================
def _tombstone_directly(client, store_id):
    """Recreate the OLD design's state: row present, marked deleted."""
    with client.server_module.engine.begin() as c:
        c.execute(text("UPDATE stores SET lifecycle_state = 'deleted', "
                       "is_active = :inactive, deleted_at = '2026-01-01T00:00:00+00:00' "
                       "WHERE id = :i"), {"i": store_id, "inactive": False})


def test_30_31_migration_purges_only_legacy_tombstones(client):
    owner = sign_in(client)
    active = make_store(client, owner, "QAACTIVE")
    archived = make_store(client, owner, "QAARCH")
    client.post(f"/api/stores/{archived['id']}/archive", headers=owner)
    legacy = make_store(client, owner, "QAGHOST")
    _tombstone_directly(client, legacy["id"])

    import store_permanent_delete as spd
    result = spd.purge_legacy_store_tombstones(client.server_module.engine)

    assert result["purged"] == 1
    assert result["store_codes"] == ["QAGHOST"]
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=legacy["id"]) == 0
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=active["id"]) == 1
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE id = :i", i=archived["id"]) == 1
    assert scalar(client, "SELECT lifecycle_state FROM stores WHERE id = :i",
                  i=archived["id"]) == "archived"


def test_32_migration_is_idempotent(client):
    owner = sign_in(client)
    legacy = make_store(client, owner, "QAGHOST")
    _tombstone_directly(client, legacy["id"])

    import store_permanent_delete as spd
    engine = client.server_module.engine
    first = spd.purge_legacy_store_tombstones(engine)
    audits = scalar(client, "SELECT COUNT(*) FROM store_deletion_events")
    second = spd.purge_legacy_store_tombstones(engine)
    third = spd.purge_legacy_store_tombstones(engine)

    assert first["purged"] == 1
    assert second["purged"] == 0 and third["purged"] == 0
    assert scalar(client, "SELECT COUNT(*) FROM store_deletion_events") == audits


def test_33_an_ordinary_restart_does_not_resurrect_a_deleted_canonical_store(client):
    """The canonical catalog must not undo an operator's decision.

    seed_stores is a first-run bootstrap: it inserts only when the Store table
    is EMPTY. So a Store deleted from a populated database stays deleted across
    every ordinary restart, however many times HQ starts.
    """
    owner = sign_in(client)
    catalog = client.get("/api/stores", headers=owner).json()
    victim = catalog[0]
    total_before = len(catalog)

    delete_store(client, owner, victim["id"], victim["store_code"])
    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE store_code = :c",
                  c=victim["store_code"]) == 0

    # Re-run the seeding exactly as startup does, twice.
    import seed
    from db import SessionLocal
    for _ in range(2):
        session = SessionLocal()
        try:
            seed.seed_stores(session)
        finally:
            session.close()

    assert scalar(client, "SELECT COUNT(*) FROM stores WHERE store_code = :c",
                  c=victim["store_code"]) == 0, (
        "an ordinary restart recreated a Store the operator permanently deleted")
    assert scalar(client, "SELECT COUNT(*) FROM stores") == total_before - 1


def test_34_a_fresh_database_still_receives_the_canonical_catalog(client, tmp_path):
    """The other half: first boot must still seed."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import models
    import seed
    from store_catalog import CANONICAL_STORES

    fresh = tmp_path / "fresh.db"
    engine = create_engine(f"sqlite:///{fresh.as_posix()}")
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        seed.seed_stores(session)
        session.commit()
        count = session.execute(text("SELECT COUNT(*) FROM stores")).scalar_one()
    finally:
        session.close()

    assert count == len(CANONICAL_STORES), (
        "a brand-new database did not receive the canonical Store catalog")
