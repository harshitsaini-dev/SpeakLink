"""History-preserving permanent Store deletion over the real HTTP surface.

Different from ``delete_store_if_unused`` (deletion_safety.py), which only
ever erases a Store nothing has referenced. This is the harder, SUPER
ADMIN-only case: a Store WITH real Broadcast Targets, Receiver Devices,
enrollment codes and Receiver events, permanently removed from every
operational surface while every one of those historical rows stays exactly
as readable as it was - a tombstone, never a cascade delete.

Nothing here touches the protected database. Every test builds its own
temporary one.
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
                               "receiver_enrollment_api", "deletion_safety",
                               "store_deletion", "store_lifecycle", "store_scope")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one

    run_receiver_credential_phase_one(server_module.engine)

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        made.database_path = database
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


def _target_store(client, headers):
    stores = client.get("/api/stores", headers=headers).json()
    return stores[0]


def _give_store_full_history(client, *, store_id: int, owner_headers: dict) -> dict:
    """A Store with a Broadcast Target, a Receiver Device with an active
    credential, a pending enrollment code and a Receiver event - the exact
    shape the ordinary hard-delete-if-unused path already refuses."""
    engine = client.server_module.engine
    from sqlalchemy import text

    session = client.post("/api/broadcast/sessions", headers=owner_headers, json={
        "campaign_name": "history for tombstone test", "target_mode": "selected",
        "store_ids": [store_id]})
    assert session.status_code == 201, session.text
    # Left in its created 'pending' state deliberately - an unstarted draft
    # must not itself block a permanent delete (only a genuinely 'live'
    # session does), and its BroadcastTarget row already exists as history
    # the moment the session is created.

    device_public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        device_id = connection.execute(
            text(
                "INSERT INTO receiver_devices "
                "(public_id, store_id, display_name, status, enrolled_at, created_at, updated_at) "
                "VALUES (:pid, :sid, 'History Device', 'active', :now, :now, :now) RETURNING id"
            ),
            {"pid": device_public_id, "sid": store_id, "now": now},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO receiver_credentials "
                "(public_id, device_id, credential_version, token_format, token_hash, "
                "hash_key_version, status, issued_at, created_at) "
                "VALUES (:cred_pid, :did, 1, 'speaklink_rcv', :hash, 1, 'active', :now, :now)"
            ),
            {"cred_pid": str(uuid.uuid4()), "did": device_id, "hash": "b" * 64, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO receiver_store_primary_device (store_id, device_id, promoted_at) "
                "VALUES (:sid, :did, :now)"
            ),
            {"sid": store_id, "did": device_id, "now": now},
        )
        connection.execute(
            text("INSERT INTO receiver_events (store_id, event_type, event_time, details) "
                "VALUES (:sid, 'connected', :now, 'history event')"),
            {"sid": store_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO receiver_enrollment_codes "
                "(code_hash, store_id, created_by, created_at, expires_at_epoch) "
                "VALUES (:hash, :sid, 1, :now, :expires)"
            ),
            {"hash": uuid.uuid4().hex, "sid": store_id, "now": now,
             "expires": datetime.now(timezone.utc).timestamp() + 3600},
        )
    return {"session_id": session.json()["id"], "device_public_id": device_public_id,
            "device_id": device_id}


def _delete(client, headers, store_id, *, confirm, acknowledged=True):
    return client.post(f"/api/stores/{store_id}/delete-permanently", headers=headers,
                       json={"confirm": confirm, "acknowledged": acknowledged})


# ===========================================================================
# 1-4. Permission
# ===========================================================================
def test_super_admin_can_permanently_delete_a_store_with_history(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)

    resp = _delete(client, owner, store["id"], confirm=store["store_code"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["row_deleted"] is True
    assert body["store_code_released"] is True
    assert body["history_detached"]["broadcast_targets.store_id"] == 1
    assert body["devices_detached"] == 1


def test_admin_cannot_permanently_delete_a_store(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    make_user(client, owner, "opsadmin", "ADMIN")
    headers = sign_in(client, "opsadmin")

    resp = _delete(client, headers, store["id"], confirm=store["store_code"])
    assert resp.status_code == 403


def test_broadcaster_cannot_permanently_delete_a_store(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    make_user(client, owner, "castbot", "BROADCASTER")
    headers = sign_in(client, "castbot")

    resp = _delete(client, headers, store["id"], confirm=store["store_code"])
    assert resp.status_code == 403


def test_viewer_cannot_permanently_delete_a_store(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    make_user(client, owner, "watcher", "VIEWER")
    headers = sign_in(client, "watcher")

    resp = _delete(client, headers, store["id"], confirm=store["store_code"])
    assert resp.status_code == 403


# ===========================================================================
# 5-8. Disappears from every operational surface
# ===========================================================================
def test_store_disappears_from_the_normal_store_list(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    before = client.get("/api/stores", headers=owner).json()

    _delete(client, owner, store["id"], confirm=store["store_code"])

    after = client.get("/api/stores", headers=owner).json()
    assert store["id"] not in {s["id"] for s in after}
    assert len(after) == len(before) - 1
    # Not even with every "show me more" flag on - unlike archived, a
    # tombstoned Store has no flag that reveals it operationally.
    everything = client.get("/api/stores", headers=owner,
                            params={"include_inactive": True, "include_archived": True}).json()
    assert store["id"] not in {s["id"] for s in everything}


def test_store_disappears_from_broadcast_console_targeting(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "targeting a deleted store", "target_mode": "selected",
        "store_ids": [store["id"]]})
    assert created.status_code in (400, 403, 404) or created.json().get("selected_store_count") == 0


def test_store_disappears_from_scope_dropdown_source(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    stores_for_dropdown = client.get("/api/stores", headers=owner,
                                     params={"include_inactive": True, "include_archived": True}).json()
    assert store["id"] not in {s["id"] for s in stores_for_dropdown}


def test_store_excluded_from_city_zone_effective_scope_resolution(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    user_id = make_user(client, owner, "scoped", "ADMIN")

    from store_scope import set_user_scope
    engine = client.server_module.engine
    set_user_scope(engine, user_id=user_id, entries=[
        {"scope_type": "CITY", "scope_value": store["city"]},
        {"scope_type": "STORE", "store_id": store["id"]},
    ], actor_id=1)

    _delete(client, owner, store["id"], confirm=store["store_code"])

    headers = sign_in(client, "scoped")
    visible = client.get("/api/stores", headers=headers).json()
    assert store["id"] not in {s["id"] for s in visible}


# ===========================================================================
# 9-10. Direct operations refused; not restorable
# ===========================================================================
def test_direct_operations_against_a_deleted_store_are_refused(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    # 404 everywhere, not 409. There is no row left to refuse a transition on,
    # which is the honest answer once deletion is real - a 409 would imply the
    # Store still exists in some state.
    assert client.put(f"/api/stores/{store['id']}", headers=owner,
                      json={"store_name": "Should Not Work"}).status_code == 404
    assert client.post(f"/api/stores/{store['id']}/regenerate-token",
                       headers=owner).status_code == 404
    for action in ("archive", "disable", "enable"):
        assert client.post(f"/api/stores/{store['id']}/{action}",
                           headers=owner).status_code == 404, action


def test_a_deleted_store_can_never_be_restored(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    resp = client.post(f"/api/stores/{store['id']}/restore", headers=owner)
    assert resp.status_code == 404


def test_deleting_an_already_deleted_store_is_refused_not_reapplied(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    first = _delete(client, owner, store["id"], confirm=store["store_code"])
    assert first.status_code == 200

    second = _delete(client, owner, store["id"], confirm=store["store_code"])
    assert second.status_code == 409  # refusal: that Store no longer exists


# ===========================================================================
# 11-15. History remains readable
# ===========================================================================
def test_broadcast_history_remains_readable_and_shows_the_deleted_store(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    history_info = _give_store_full_history(client, store_id=store["id"], owner_headers=owner)

    _delete(client, owner, store["id"], confirm=store["store_code"])

    detail = client.get(f"/api/broadcast/sessions/{history_info['session_id']}", headers=owner)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    target = body["targets"][0]
    # The pointer is gone; the identity is not. Reading history must not
    # depend on a Store row that an operator deleted.
    assert target["store_id"] is None
    assert target["store_code"] == store["store_code"]

    history = client.get("/api/broadcast/history", headers=owner)
    assert history.status_code == 200
    assert any(s["id"] == history_info["session_id"] for s in history.json())


def test_broadcast_target_rows_remain_in_the_database(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        # The ROW stays - that is the history. The POINTER is nulled, because
        # stores has no AUTOINCREMENT and a dangling id would be reissued to
        # the next Store created.
        still_pointing = connection.execute(
            text("SELECT COUNT(*) FROM broadcast_targets WHERE store_id = :i"),
            {"i": store["id"]},
        ).scalar_one()
        kept = connection.execute(
            text("SELECT COUNT(*) FROM broadcast_targets "
                 "WHERE store_code_snapshot = :c"),
            {"c": store["store_code"]},
        ).scalar_one()
    assert still_pointing == 0
    assert kept == 1


def test_receiver_events_remain_in_the_database(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        still_pointing = connection.execute(
            text("SELECT COUNT(*) FROM receiver_events WHERE store_id = :i"),
            {"i": store["id"]},
        ).scalar_one()
        kept = connection.execute(
            text("SELECT COUNT(*) FROM receiver_events "
                 "WHERE store_code_snapshot = :c"),
            {"c": store["store_code"]},
        ).scalar_one()
    assert still_pointing == 0
    assert kept == 1


def test_device_history_remains_but_device_is_retired(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    history = _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        row = connection.execute(
            text("SELECT status FROM receiver_devices WHERE public_id = :pid"),
            {"pid": history["device_public_id"]},
        ).first()
    assert row is not None  # the row itself still exists
    assert row.status == "retired"


def test_store_deletion_audit_history_is_recorded_and_readable(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    events = client.get(f"/api/stores/{store['id']}/deletion-events", headers=owner)
    assert events.status_code == 200, events.text
    rows = events.json()["events"]
    assert len(rows) == 1
    assert rows[0]["store_code"] == store["store_code"]
    assert rows[0]["actor_user_id"] is not None
    for forbidden in ("password", "jwt", "bearer ", "hmac", "secret", "token"):
        assert forbidden not in events.text.lower()


# ===========================================================================
# 16-18. Credentials revoked, primary cleared, enrollment codes invalidated
# ===========================================================================
def test_active_receiver_credentials_are_revoked(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    resp = _delete(client, owner, store["id"], confirm=store["store_code"])
    assert resp.json()["credentials_revoked"] == 1

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM receiver_credentials")
        ).scalar_one()
    assert status == "revoked"


def test_primary_assignment_is_removed(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        count = connection.execute(
            text("SELECT COUNT(*) FROM receiver_store_primary_device WHERE store_id = :i"),
            {"i": store["id"]},
        ).scalar_one()
    assert count == 0


def test_pending_enrollment_codes_become_unusable(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    resp = _delete(client, owner, store["id"], confirm=store["store_code"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["history_detached"]["receiver_enrollment_codes.store_id"] == 1

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        # The row survives as evidence of what was issued, detached from the
        # Store, and backdated so it can never be redeemed.
        row = connection.execute(
            text("SELECT expires_at_epoch, store_id FROM receiver_enrollment_codes"),
        ).first()
    assert row.store_id is None
    assert row.expires_at_epoch <= datetime.now(timezone.utc).timestamp()


# ===========================================================================
# 19-20. Integrity
# ===========================================================================
def test_foreign_key_check_and_integrity_check_are_clean_after_deletion(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    _give_store_full_history(client, store_id=store["id"], owner_headers=owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    from sqlalchemy import text
    with client.server_module.engine.connect() as connection:
        fk_violations = connection.execute(text("PRAGMA foreign_key_check")).all()
        integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
    assert fk_violations == []
    assert integrity == "ok"


# ===========================================================================
# 21. Store code cannot silently be reused
# ===========================================================================
def test_a_deleted_stores_code_becomes_available_again(client):
    """The defect this feature exists to fix.

    The old tombstone kept the row, so the UNIQUE index kept the Store Code,
    and an operator who permanently deleted TESTSTORE could never create TESTSTORE
    again. Deleting means the code is free - and the Store that takes it is a
    DIFFERENT Store, with a different id.
    """
    owner = sign_in(client)
    store = _target_store(client, owner)
    _delete(client, owner, store["id"], confirm=store["store_code"])

    created = client.post("/api/stores", headers=owner, json={
        "store_code": store["store_code"], "store_name": "A Different Shop",
        "city": store["city"], "region": store["region"],
    })
    assert created.status_code == 201, created.text
    assert created.json()["id"] != store["id"], (
        "the replacement Store reused the deleted Store's id")


# ===========================================================================
# 22-23. Confirmation requirements enforced server-side
# ===========================================================================
def test_typed_confirmation_must_match_exactly(client):
    owner = sign_in(client)
    store = _target_store(client, owner)

    wrong = _delete(client, owner, store["id"], confirm="WRONG-CODE")
    assert wrong.status_code == 409

    still_there = client.get("/api/stores", headers=owner).json()
    assert store["id"] in {s["id"] for s in still_there}


def test_acknowledgement_flag_is_required(client):
    owner = sign_in(client)
    store = _target_store(client, owner)

    resp = _delete(client, owner, store["id"], confirm=store["store_code"], acknowledged=False)
    assert resp.status_code == 400

    still_there = client.get("/api/stores", headers=owner).json()
    assert store["id"] in {s["id"] for s in still_there}


def test_live_broadcast_target_refuses_permanent_deletion(client):
    owner = sign_in(client)
    store = _target_store(client, owner)
    session = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "live guard test", "target_mode": "selected",
        "store_ids": [store["id"]]})
    assert session.status_code == 201, session.text

    engine = client.server_module.engine
    import asyncio

    from ws_manager import manager

    # Live state is per session now, so the guard is driven through the real
    # runtime rather than by assigning two module fields.
    session_id = session.json()["id"]
    asyncio.run(manager.broadcasts.start(
        session_id=session_id, owner_user_id=1,
        target_store_ids={store["id"]}))
    try:
        resp = _delete(client, owner, store["id"], confirm=store["store_code"])
        assert resp.status_code == 409
    finally:
        asyncio.run(manager.broadcasts.end(session_id))
