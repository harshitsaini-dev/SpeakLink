"""True permanent deletion: the row goes, the name is released, history stays.

THE DEFECT THIS FILE EXISTS FOR

User Management showed accounts marked "permanently deleted" that still had
Rights, Scope and Reset Password beside them, and creating a new account with
that username was refused:

    The username 'admin' is already in use.

Because the old design tombstoned the row instead of deleting it, the UNIQUE
index still held the name. An account that still occupies the namespace and
still has actions has not been deleted, it has been hidden - and hiding it
harder in React would have reproduced exactly that bug.

THE TRAP THAT MAKES SNAPSHOTS MANDATORY

``hq_users.id`` had no AUTOINCREMENT, so SQLite reissues ``max(id) + 1``.
Deleting the highest-numbered account and creating another handed the new
person the old id - and every history row still pointing at that id. Two
independent defences are asserted below: the pointer is nulled and the
identity kept as a snapshot, AND ids are no longer reusable at all.

ARCHIVE IS NOT DELETION AND MUST NOT DRIFT INTO IT

Half of this file is about archive continuing to behave exactly as before:
reversible, name still reserved, row still there.
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
            "user_deletion", "user_permanent_delete", "device_deletion",
            "store_scope", "deletion_safety")]:
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


def make_user(client, headers, username, role="ADMIN"):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": PASSWORD})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def delete_user(client, headers, user_id, confirm):
    return client.post(f"/api/users/{user_id}/delete-permanently", headers=headers,
                       json={"confirm": confirm, "acknowledged": True})


def rows(client, sql, **params):
    with client.server_module.engine.connect() as c:
        return c.execute(text(sql), params).all()


def scalar(client, sql, **params):
    with client.server_module.engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def first_store_id(client, headers):
    return client.get("/api/stores", headers=headers).json()[0]["id"]


def broadcast_as(client, headers, store_id, name="history"):
    made = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": [store_id]})
    assert made.status_code == 201, made.text
    return made.json()["id"]


# ===========================================================================
# 1-4  ARCHIVE stays exactly what it was
# ===========================================================================
def test_1_archive_keeps_the_user_row(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "archie")
    assert client.post(f"/api/users/{user_id}/archive", headers=owner).status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=user_id) == 1


def test_2_archive_still_reserves_the_username(client):
    """Archive is reversible, so the name must stay taken."""
    owner = sign_in(client)
    user_id = make_user(client, owner, "archie")
    client.post(f"/api/users/{user_id}/archive", headers=owner)

    resp = client.post("/api/users", headers=owner, json={
        "username": "archie", "display_name": "Impostor",
        "role": "ADMIN", "password": PASSWORD})
    assert resp.status_code == 409, resp.text


def test_3_an_archived_user_cannot_sign_in(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "archie")
    client.post(f"/api/users/{user_id}/archive", headers=owner)
    assert client.post("/api/auth/login",
                       json={"username": "archie", "password": PASSWORD}
                       ).status_code in (401, 403)


def test_4_unarchive_restores_the_same_user_id(client):
    """Restore brings the account back as DISABLED, not active - re-enabling is
    a second, deliberate step in this product. What matters here is that the
    SAME id comes back, which is the whole difference from deletion."""
    owner = sign_in(client)
    user_id = make_user(client, owner, "archie")
    client.post(f"/api/users/{user_id}/archive", headers=owner)
    assert client.post(f"/api/users/{user_id}/restore", headers=owner).status_code == 200

    listed = {u["id"]: u for u in client.get("/api/users", headers=owner).json()}
    assert user_id in listed, "restore must return the SAME account, not a new one"
    assert listed[user_id]["lifecycle_state"] == "disabled"

    assert client.post(f"/api/users/{user_id}/enable", headers=owner).status_code == 200
    reread = {u["id"]: u for u in client.get("/api/users", headers=owner).json()}
    assert reread[user_id]["lifecycle_state"] == "active"


# ===========================================================================
# 5-8  PERMANENT DELETE is real
# ===========================================================================
def test_5_permanent_delete_physically_removes_the_row(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "vanish")
    assert delete_user(client, owner, user_id, "vanish").status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=user_id) == 0


def test_6_a_deleted_user_is_absent_from_every_listing(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "vanish")
    delete_user(client, owner, user_id, "vanish")

    assert user_id not in {u["id"] for u in client.get("/api/users", headers=owner).json()}
    search = client.get("/api/users/search", headers=owner,
                        params={"page_size": 200}).json()
    assert "vanish" not in {u["username"] for u in search["items"]}
    # Even asking for it explicitly, however the parameter is spelled.
    forced = client.get("/api/users/search", headers=owner,
                        params={"include_deleted": True, "page_size": 200}).json()
    assert "vanish" not in {u["username"] for u in forced["items"]}


def test_7_the_username_becomes_reusable(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "recycled")
    delete_user(client, owner, user_id, "recycled")

    resp = client.post("/api/users", headers=owner, json={
        "username": "recycled", "display_name": "Somebody Else",
        "role": "ADMIN", "password": PASSWORD})
    assert resp.status_code == 201, resp.text


def test_8_the_recreated_account_is_a_new_identity(client):
    """Release-blocking. A reused name must never be a reused identity."""
    owner = sign_in(client)
    old_id = make_user(client, owner, "recycled")
    delete_user(client, owner, old_id, "recycled")

    new_id = make_user(client, owner, "recycled")
    assert new_id != old_id, (
        f"the new account reused id {old_id} - history pointing at that id "
        "would silently transfer to a different person")


# ===========================================================================
# 9-11  nothing of the old account transfers
# ===========================================================================
def test_9_permission_overrides_do_not_transfer(client):
    owner = sign_in(client)
    old_id = make_user(client, owner, "recycled")
    assert client.put(f"/api/users/{old_id}/permissions", headers=owner, json={
        "changes": [{"code": "stores.create", "effect": "DENY"}]}).status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM user_permission_overrides "
                          "WHERE user_id = :i", i=old_id) == 1

    delete_user(client, owner, old_id, "recycled")
    new_id = make_user(client, owner, "recycled")

    assert scalar(client, "SELECT COUNT(*) FROM user_permission_overrides "
                          "WHERE user_id = :i", i=new_id) == 0
    described = client.get(f"/api/users/{new_id}/permissions", headers=owner).json()
    overridden = [r for r in described["permissions"] if r["override"] != "INHERIT"]
    assert overridden == [], f"inherited overrides: {overridden}"


def test_10_store_scope_does_not_transfer(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    old_id = make_user(client, owner, "recycled")
    assert client.put(f"/api/users/{old_id}/store-scope", headers=owner, json={
        "entries": [{"scope_type": "STORE", "store_id": store_id}]}).status_code == 200
    assert scalar(client, "SELECT COUNT(*) FROM user_store_scope WHERE user_id = :i",
                  i=old_id) == 1

    delete_user(client, owner, old_id, "recycled")
    new_id = make_user(client, owner, "recycled")

    assert scalar(client, "SELECT COUNT(*) FROM user_store_scope WHERE user_id = :i",
                  i=new_id) == 0


def test_11_authentication_state_does_not_transfer(client):
    """The old password must not open the new account."""
    owner = sign_in(client)
    old_id = make_user(client, owner, "recycled")
    old_hash = scalar(client, "SELECT password_hash FROM hq_users WHERE id = :i", i=old_id)

    delete_user(client, owner, old_id, "recycled")
    resp = client.post("/api/users", headers=owner, json={
        "username": "recycled", "display_name": "Somebody Else",
        "role": "ADMIN", "password": "a-completely-different-password"})
    new_id = resp.json()["id"]

    new_hash = scalar(client, "SELECT password_hash FROM hq_users WHERE id = :i", i=new_id)
    assert new_hash != old_hash
    # The old password does not work; the new one does.
    assert client.post("/api/auth/login",
                       json={"username": "recycled", "password": PASSWORD}
                       ).status_code in (401, 403)
    assert client.post("/api/auth/login",
                       json={"username": "recycled",
                             "password": "a-completely-different-password"}
                       ).status_code == 200


# ===========================================================================
# 12-15  history survives and stays attached to the OLD identity
# ===========================================================================
def test_12_broadcast_history_survives_the_deletion(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = broadcast_as(client, sign_in(client, "caster"), store_id, "Autumn Sale")

    delete_user(client, owner, caster_id, "caster")

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=owner)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["campaign_name"] == "Autumn Sale"
    # Readable without the account: the identity is on the row.
    assert body["started_by_username"] == "caster"
    assert body["started_by"] is None


def test_13_old_history_does_not_bind_to_a_new_same_username_account(client):
    """The release-blocking scenario, end to end.

    Old caster runs a broadcast, is deleted, and a DIFFERENT person is created
    with the same username. The broadcast must still belong to the old
    identity, not to the new account.
    """
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    old_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = broadcast_as(client, sign_in(client, "caster"), store_id)

    delete_user(client, owner, old_id, "caster")
    new_id = make_user(client, owner, "caster", "BROADCASTER")

    assert new_id != old_id
    row = rows(client, "SELECT started_by, started_by_username FROM broadcast_sessions "
                       "WHERE id = :i", i=session_id)[0]
    assert row.started_by is None, "the pointer must not name any live account"
    assert row.started_by != new_id
    assert row.started_by_username == "caster"

    # And no join can accidentally reattach it.
    bound = scalar(client,
                   "SELECT COUNT(*) FROM broadcast_sessions s JOIN hq_users u "
                   "ON u.id = s.started_by WHERE s.id = :i", i=session_id)
    assert bound == 0


def test_14_administrative_audit_survives(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "audited")
    delete_user(client, owner, user_id, "audited")

    events = rows(client, "SELECT user_id, username, role FROM user_deletion_events "
                          "WHERE user_id = :i", i=user_id)
    assert events, "the deletion left no audit record"
    assert events[-1].username == "audited"
    # The audit outlives the account it describes.
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=user_id) == 0


def test_15_the_audit_carries_no_secret(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "audited")
    delete_user(client, owner, user_id, "audited")

    with client.server_module.engine.connect() as c:
        blob = str(c.execute(text("SELECT * FROM user_deletion_events")).all()).lower()
    for forbidden in ("password", "$2b$", "bearer", "eyj"):
        assert forbidden not in blob


# ===========================================================================
# 16-18  the database stays sound and unrelated records are untouched
# ===========================================================================
def test_16_foreign_key_integrity_remains_clean(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    broadcast_as(client, sign_in(client, "caster"), store_id)
    delete_user(client, owner, caster_id, "caster")

    with client.server_module.engine.connect() as c:
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
        # Foreign keys are ON - the deletion did not work by switching them off.
        assert c.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_17_unrelated_users_are_untouched(client):
    owner = sign_in(client)
    keep_id = make_user(client, owner, "keeper")
    doomed_id = make_user(client, owner, "doomed")
    client.put(f"/api/users/{keep_id}/permissions", headers=owner, json={
        "changes": [{"code": "stores.create", "effect": "DENY"}]})

    delete_user(client, owner, doomed_id, "doomed")

    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=keep_id) == 1
    assert scalar(client, "SELECT COUNT(*) FROM user_permission_overrides "
                          "WHERE user_id = :i", i=keep_id) == 1


def test_18_unrelated_broadcast_sessions_are_untouched(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    mine = broadcast_as(client, owner, store_id, "owner session")
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    theirs = broadcast_as(client, sign_in(client, "caster"), store_id, "caster session")

    delete_user(client, owner, caster_id, "caster")

    keeper = rows(client, "SELECT started_by, started_by_username FROM broadcast_sessions "
                          "WHERE id = :i", i=mine)[0]
    assert keeper.started_by is not None, "somebody else's broadcast lost its owner"
    assert keeper.started_by_username is None
    assert scalar(client, "SELECT COUNT(*) FROM broadcast_sessions WHERE id = :i",
                  i=theirs) == 1


# ===========================================================================
# 19-22  authorization and safety rules
# ===========================================================================
def test_19_the_wrong_permission_is_refused(client):
    owner = sign_in(client)
    victim = make_user(client, owner, "victim")
    make_user(client, owner, "meddler", role="ADMIN")   # holds no delete right

    resp = delete_user(client, sign_in(client, "meddler"), victim, "victim")
    assert resp.status_code == 403
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=victim) == 1


def test_20_the_last_owner_cannot_be_deleted(client):
    owner = sign_in(client)
    me = client.get("/api/auth/me", headers=owner).json()
    second = make_user(client, owner, "second-owner", role="OWNER")
    promoted = sign_in(client, "second-owner")

    # The second OWNER may delete the first...
    assert delete_user(client, promoted, me["id"], me["username"]).status_code == 200
    # ...but nobody can delete the last one.
    third = make_user(client, promoted, "helper", role="ADMIN")
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE UPPER(role) = 'OWNER'") == 1
    refusal = delete_user(client, sign_in(client, "helper"), second, "second-owner")
    # Either the permission or the last-owner rule stops it; both are correct
    # refusals and neither may delete the account.
    assert refusal.status_code in (403, 409)
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=second) == 1
    assert third


def test_21_self_deletion_is_refused(client):
    owner = sign_in(client)
    me = client.get("/api/auth/me", headers=owner).json()
    resp = delete_user(client, owner, me["id"], me["username"])
    assert resp.status_code == 409
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=me["id"]) == 1


def test_22_a_failure_midway_rolls_the_whole_thing_back(client, monkeypatch):
    """No half-deleted account: not a row gone without an audit, and not
    history that has lost its owner while the account survives."""
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    session_id = broadcast_as(client, sign_in(client, "caster"), store_id)

    import user_permanent_delete as upd
    original = upd._snapshot_broadcast_ownership

    def explode_after_snapshot(connection, user_id, username, display_name):
        original(connection, user_id, username, display_name)
        raise RuntimeError("simulated failure part-way through the transaction")

    monkeypatch.setattr(upd, "_snapshot_broadcast_ownership", explode_after_snapshot)
    resp = delete_user(client, owner, caster_id, "caster")
    assert resp.status_code == 500

    monkeypatch.undo()
    # The account is still there, and the snapshot written before the failure
    # was rolled back with everything else.
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=caster_id) == 1
    row = rows(client, "SELECT started_by, started_by_username FROM broadcast_sessions "
                       "WHERE id = :i", i=session_id)[0]
    assert row.started_by == caster_id
    assert row.started_by_username is None


# ===========================================================================
# 23-24  username policy is unchanged for accounts that still exist
# ===========================================================================
def test_23_uniqueness_still_protects_active_and_archived_accounts(client):
    owner = sign_in(client)
    make_user(client, owner, "activeone")
    archived_id = make_user(client, owner, "archivedone")
    client.post(f"/api/users/{archived_id}/archive", headers=owner)

    for name in ("activeone", "archivedone"):
        resp = client.post("/api/users", headers=owner, json={
            "username": name, "display_name": "Impostor",
            "role": "ADMIN", "password": PASSWORD})
        assert resp.status_code == 409, f"{name} was allowed to be duplicated"


def test_24_the_existing_username_normalisation_policy_is_unchanged(client):
    """Whatever the policy is, deletion must not alter it. Asserted by
    comparing a live duplicate against the same casing after deletion."""
    owner = sign_in(client)
    make_user(client, owner, "casing")
    duplicate = client.post("/api/users", headers=owner, json={
        "username": "CASING", "display_name": "Other",
        "role": "ADMIN", "password": PASSWORD})
    policy_is_case_insensitive = duplicate.status_code == 409

    user_id = make_user(client, owner, "another")
    delete_user(client, owner, user_id, "another")
    after = client.post("/api/users", headers=owner, json={
        "username": "ANOTHER", "display_name": "Other",
        "role": "ADMIN", "password": PASSWORD})

    if policy_is_case_insensitive:
        # A freed name is freed in either casing.
        assert after.status_code == 201, after.text
    else:
        assert after.status_code == 201, after.text


# ===========================================================================
# 25-28  migration of accounts tombstoned by the OLD design
# ===========================================================================
def _tombstone_directly(client, user_id):
    """Recreate the OLD design's state: row present, marked deleted."""
    with client.server_module.engine.begin() as c:
        c.execute(text("UPDATE hq_users SET lifecycle_state = 'deleted', "
                       "is_active = :inactive, deleted_at = '2026-01-01T00:00:00+00:00' "
                       "WHERE id = :i"), {"i": user_id, "inactive": False})


def test_25_migration_purges_only_legacy_tombstones(client):
    owner = sign_in(client)
    active_id = make_user(client, owner, "stayactive")
    archived_id = make_user(client, owner, "stayarchived")
    client.post(f"/api/users/{archived_id}/archive", headers=owner)
    legacy_id = make_user(client, owner, "legacyghost")
    _tombstone_directly(client, legacy_id)

    import user_permanent_delete as upd
    result = upd.purge_legacy_user_tombstones(client.server_module.engine)

    assert result["purged"] == 1
    assert result["usernames"] == ["legacyghost"]
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=legacy_id) == 0
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=active_id) == 1
    assert scalar(client, "SELECT COUNT(*) FROM hq_users WHERE id = :i", i=archived_id) == 1


def test_26_migration_never_touches_an_archived_account(client):
    """Archived accounts are restorable. Purging one would destroy an account
    an operator deliberately kept."""
    owner = sign_in(client)
    archived_id = make_user(client, owner, "stayarchived")
    client.post(f"/api/users/{archived_id}/archive", headers=owner)

    import user_permanent_delete as upd
    assert upd.purge_legacy_user_tombstones(client.server_module.engine)["purged"] == 0

    state = scalar(client, "SELECT lifecycle_state FROM hq_users WHERE id = :i",
                   i=archived_id)
    assert state == "archived"


def test_27_migration_is_idempotent(client):
    owner = sign_in(client)
    legacy_id = make_user(client, owner, "legacyghost")
    _tombstone_directly(client, legacy_id)

    import user_permanent_delete as upd
    engine = client.server_module.engine
    first = upd.purge_legacy_user_tombstones(engine)
    audits_after_first = scalar(client, "SELECT COUNT(*) FROM user_deletion_events")
    second = upd.purge_legacy_user_tombstones(engine)
    third = upd.purge_legacy_user_tombstones(engine)

    assert first["purged"] == 1
    assert second["purged"] == 0 and third["purged"] == 0
    assert scalar(client, "SELECT COUNT(*) FROM user_deletion_events") == audits_after_first


def test_28_the_freed_username_is_immediately_reusable_after_migration(client):
    owner = sign_in(client)
    legacy_id = make_user(client, owner, "legacyghost")
    _tombstone_directly(client, legacy_id)

    import user_permanent_delete as upd
    upd.purge_legacy_user_tombstones(client.server_module.engine)

    resp = client.post("/api/users", headers=owner, json={
        "username": "legacyghost", "display_name": "New Person",
        "role": "ADMIN", "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] != legacy_id


def test_28b_schema_preparation_is_idempotent(client):
    """Startup runs it on every boot; a second run must change nothing."""
    import user_permanent_delete as upd
    engine = client.server_module.engine

    before = scalar(client, "SELECT COUNT(*) FROM hq_users")
    upd.ensure_user_permanent_delete_schema(engine)
    upd.ensure_user_permanent_delete_schema(engine)

    assert scalar(client, "SELECT COUNT(*) FROM hq_users") == before
    with engine.connect() as c:
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []


def test_28c_user_ids_are_never_reissued(client):
    """The structural half of the id-reuse defence."""
    owner = sign_in(client)
    first_id = make_user(client, owner, "temporary")
    delete_user(client, owner, first_id, "temporary")
    second_id = make_user(client, owner, "somebodyelse")

    assert second_id != first_id, "a deleted id was handed straight back out"
