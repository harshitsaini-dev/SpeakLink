"""Broadcast History and System Log archive, permanent deletion and bulk.

The distinction this file exists to protect: everywhere else in SpeakLink a
"permanent delete" is a tombstone, because the row is REFERENCED BY history.
Broadcast History and System Logs ARE the history, so here deletion is real -
and the guard rails move accordingly:

* deleting a session removes its broadcast_targets too (the only table that
  references it, and meaningless without it) and touches nothing else - never
  a Store, a User or a Receiver Device;
* deleting logs removes only system_logs rows;
* every destructive action writes one row to a SEPARATE audit table, which a
  log purge therefore cannot erase.

Nothing here touches the protected database.
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
            "schemas", "permission_catalog", "admin_records", "user_deletion",
            "device_deletion", "receiver_enrollment_api")]:
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


def make_session(client, headers, store_id, name="campaign"):
    r = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": [store_id]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def store_ids(client, headers):
    return [s["id"] for s in client.get("/api/stores", headers=headers).json()]


def log_ids(client, headers, limit=500):
    return [row["id"] for row in
            client.get("/api/logs", headers=headers, params={"limit": limit}).json()]


DELETE_BODY = {"confirm": "DELETE", "acknowledged": True}


# ===========================================================================
# Broadcast History - archive
# ===========================================================================
def test_archiving_a_session_hides_it_from_normal_history(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)

    assert session_id in {s["id"] for s in
                          client.get("/api/broadcast/history", headers=owner).json()}

    r = client.post("/api/broadcast/history/archive", headers=owner,
                    json={"ids": [session_id]})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 1

    visible = {s["id"] for s in client.get("/api/broadcast/history", headers=owner).json()}
    assert session_id not in visible


def test_an_archived_session_is_visible_with_show_archived(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)
    client.post("/api/broadcast/history/archive", headers=owner, json={"ids": [session_id]})

    shown = client.get("/api/broadcast/history", headers=owner,
                       params={"include_archived": True}).json()
    assert session_id in {s["id"] for s in shown}


def test_archiving_is_reversible(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)
    client.post("/api/broadcast/history/archive", headers=owner, json={"ids": [session_id]})
    r = client.post("/api/broadcast/history/unarchive", headers=owner,
                    json={"ids": [session_id]})
    assert r.json()["affected"] == 1
    assert session_id in {s["id"] for s in
                          client.get("/api/broadcast/history", headers=owner).json()}


def test_archiving_an_already_archived_session_is_skipped_not_double_counted(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)
    client.post("/api/broadcast/history/archive", headers=owner, json={"ids": [session_id]})
    again = client.post("/api/broadcast/history/archive", headers=owner,
                        json={"ids": [session_id]}).json()
    assert again["affected"] == 0 and again["skipped"] == 1


# ===========================================================================
# Broadcast History - permanent deletion
# ===========================================================================
def test_permanent_delete_removes_the_session_and_its_targets_only(client):
    owner = sign_in(client)
    stores = store_ids(client, owner)
    doomed = make_session(client, owner, stores[0], "doomed")
    survivor = make_session(client, owner, stores[1], "survivor")

    from sqlalchemy import text
    engine = client.server_module.engine
    with engine.connect() as c:
        store_count_before = c.execute(text("SELECT COUNT(*) FROM stores")).scalar_one()
        user_count_before = c.execute(text("SELECT COUNT(*) FROM hq_users")).scalar_one()

    r = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                    json={"ids": [doomed], **DELETE_BODY})
    assert r.status_code == 200, r.text
    # `matched` was added alongside these when filtered-bulk mode shipped, so
    # assert the counts that matter rather than exact dict equality.
    assert r.json()["requested"] == 1 and r.json()["affected"] == 1
    assert r.json()["skipped"] == 0 and r.json()["failed"] == 0

    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": doomed}).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_targets WHERE session_id=:i"),
                         {"i": doomed}).scalar_one() == 0
        # Everything else is untouched.
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": survivor}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_targets WHERE session_id=:i"),
                         {"i": survivor}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM stores")).scalar_one() == store_count_before
        assert c.execute(text("SELECT COUNT(*) FROM hq_users")).scalar_one() == user_count_before


def test_permanent_delete_requires_typed_confirmation_and_acknowledgement(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)

    assert client.post("/api/broadcast/history/delete-permanently", headers=owner,
                       json={"ids": [session_id], "confirm": "delete",
                             "acknowledged": True}).status_code == 409
    assert client.post("/api/broadcast/history/delete-permanently", headers=owner,
                       json={"ids": [session_id], "confirm": "DELETE",
                             "acknowledged": False}).status_code == 400
    assert session_id in {s["id"] for s in
                          client.get("/api/broadcast/history", headers=owner).json()}


def test_bulk_delete_reports_requested_affected_and_skipped_exactly(client):
    owner = sign_in(client)
    stores = store_ids(client, owner)
    real = [make_session(client, owner, stores[i], f"c{i}") for i in range(3)]
    missing = 999_999

    r = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                    json={"ids": real + [missing], **DELETE_BODY}).json()
    assert (r["requested"], r["affected"], r["skipped"], r["failed"]) == (4, 3, 1, 0)


# ===========================================================================
# System Logs
# ===========================================================================
def test_archiving_a_log_hides_it_and_show_archived_reveals_it(client):
    owner = sign_in(client)
    target = log_ids(client, owner)[0]

    assert client.post("/api/logs/archive", headers=owner,
                       json={"ids": [target]}).json()["affected"] == 1
    assert target not in log_ids(client, owner)

    shown = client.get("/api/logs", headers=owner,
                       params={"include_archived": True, "limit": 500}).json()
    assert target in {row["id"] for row in shown}


def seed_logs(client, headers, store_id, count=3):
    """Generate real log lines through a real action rather than depending on
    however many incidental startup logs happen to exist."""
    for index in range(count):
        client.post("/api/broadcast/sessions", headers=headers, json={
            "campaign_name": f"log seed {index}", "target_mode": "selected",
            "store_ids": [store_id]})
    return log_ids(client, headers, limit=500)


def test_permanent_log_delete_removes_only_those_rows(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    ids = seed_logs(client, owner, store)
    assert len(ids) >= 2, "the fixture did not produce enough log rows"
    # Deliberately NOT the highest id. system_logs.id is a plain SQLite
    # INTEGER PRIMARY KEY (a rowid, no AUTOINCREMENT), so deleting the
    # current maximum frees that number - and this endpoint writes its own
    # audit log line immediately afterwards, which would then be handed the
    # very id just deleted. The row really was removed; only the identifier
    # is recycled. Targeting an older row keeps the assertion about deletion
    # rather than about id allocation.
    doomed, keep = ids[-1], ids[0]

    r = client.post("/api/logs/delete-permanently", headers=owner,
                    json={"ids": [doomed], **DELETE_BODY})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 1

    remaining = log_ids(client, owner, limit=500)
    assert doomed not in remaining
    assert keep in remaining


def test_a_log_purge_cannot_erase_the_record_of_the_purge(client):
    """The administrative deletion audit lives in its own table precisely so
    deleting every system_logs row cannot destroy the evidence."""
    owner = sign_in(client)
    first_target = log_ids(client, owner)[0]
    client.post("/api/logs/delete-permanently", headers=owner,
                json={"ids": [first_target], **DELETE_BODY})

    audit_before = client.get("/api/admin/deletion-events", headers=owner,
                              params={"record_type": "system_log"}).json()["events"]
    assert len(audit_before) >= 1

    # Now purge EVERY remaining log row.
    everything = log_ids(client, owner, limit=1000)
    client.post("/api/logs/delete-permanently", headers=owner,
                json={"ids": everything, **DELETE_BODY})

    audit_after = client.get("/api/admin/deletion-events", headers=owner,
                             params={"record_type": "system_log"}).json()["events"]
    assert len(audit_after) >= len(audit_before), "the purge erased its own audit"


def test_the_audit_records_actor_count_and_filter_but_not_content(client):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store, "Secret Campaign Name")

    client.post("/api/broadcast/history/delete-permanently", headers=owner,
                json={"ids": [session_id], **DELETE_BODY,
                      "filters": {"status": "ended", "search": "Secret"}})

    events = client.get("/api/admin/deletion-events", headers=owner,
                        params={"record_type": "broadcast_session"}).json()["events"]
    deleted = [e for e in events if e["action"] == "deleted"]
    assert deleted, "no deletion audit row was written"
    row = deleted[0]
    assert row["actor_user_id"] is not None
    assert row["affected_count"] == 1
    assert row["filters"]["status"] == "ended"
    # The audit explains WHO removed HOW MUCH by WHAT filter - never what it
    # said, which would defeat the deletion the operator asked for.
    assert "Secret Campaign Name" not in str(row)


# ===========================================================================
# Permissions
# ===========================================================================
@pytest.mark.parametrize("role", ["ADMIN", "BROADCASTER", "VIEWER"])
def test_only_super_admin_may_permanently_delete_history_or_logs(client, role):
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)
    make_user(client, owner, f"a{role.lower()}", role)
    headers = sign_in(client, f"a{role.lower()}")

    assert client.post("/api/broadcast/history/delete-permanently", headers=headers,
                       json={"ids": [session_id], **DELETE_BODY}).status_code == 403
    assert client.post("/api/logs/delete-permanently", headers=headers,
                       json={"ids": [1], **DELETE_BODY}).status_code == 403


def test_admin_may_archive_but_not_permanently_delete(client):
    """Archiving is reversible and is the intended everyday tool, so ADMIN
    keeps it - only irreversible destruction is reserved."""
    owner = sign_in(client)
    store = store_ids(client, owner)[0]
    session_id = make_session(client, owner, store)
    make_user(client, owner, "opsadmin", "ADMIN")
    headers = sign_in(client, "opsadmin")

    assert client.post("/api/broadcast/history/archive", headers=headers,
                       json={"ids": [session_id]}).status_code == 200
    assert client.post("/api/broadcast/history/delete-permanently", headers=headers,
                       json={"ids": [session_id], **DELETE_BODY}).status_code == 403
