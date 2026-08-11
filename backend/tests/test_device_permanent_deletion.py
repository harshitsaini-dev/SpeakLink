"""History-preserving irreversible permanent deletion of a Receiver Device.

``deletion_safety.delete_device_if_unused`` refuses any Device that has ever
been enrolled, because enrolment itself creates a credential and a credential
event - which means it refuses every real Device. This is the case a SUPER
ADMIN actually needs: a Device WITH credential and event history, removed
permanently from operation while all of that evidence stays readable.

The row is tombstoned rather than deleted: receiver_credentials references
receiver_devices with ON DELETE RESTRICT, and receiver_events/receiver_
credential_events are the only record of what a Store's Receiver actually
did.

Nothing here touches the protected database.
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
    for module in [n for n in list(sys.modules) if n in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "receiver_enrollment_api",
            "deletion_safety", "device_deletion", "user_deletion")]:
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


def store_id_of(client, headers):
    return client.get("/api/stores", headers=headers).json()[0]["id"]


def enrolled_device(engine, store_id, *, primary=True):
    """A Device shaped exactly like a real enrolment: credential, credential
    event, receiver events, and (optionally) the Store's primary."""
    from sqlalchemy import text
    public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=ON")
        device_id = c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, created_at, updated_at) "
            "VALUES (:p, :s, 'Store PC', 'active', :now, :now, :now) RETURNING id"),
            {"p": public_id, "s": store_id, "now": now}).scalar_one()
        c.execute(text(
            "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
            "token_format, token_hash, hash_key_version, status, issued_at, created_at) "
            "VALUES (:cp, :d, 1, 'speaklink_rcv', :h, 1, 'active', :now, :now)"),
            {"cp": str(uuid.uuid4()), "d": device_id, "h": uuid.uuid4().hex * 2, "now": now})
        c.execute(text(
            "INSERT INTO receiver_credential_events (public_id, event_type, outcome, "
            "store_id, device_id, event_at) "
            "VALUES (:ep, 'device_enrolled', 'success', :s, :d, :now)"),
            {"ep": str(uuid.uuid4()), "s": store_id, "d": device_id, "now": now})
        c.execute(text(
            "INSERT INTO receiver_events (store_id, event_type, event_time) "
            "VALUES (:s, 'connected', :now)"), {"s": store_id, "now": now})
        if primary:
            c.execute(text(
                "INSERT INTO receiver_store_primary_device (store_id, device_id, promoted_at) "
                "VALUES (:s, :d, :now)"), {"s": store_id, "d": device_id, "now": now})
    return device_id, public_id


def delete_device(client, headers, public_id, *, confirm, acknowledged=True):
    return client.post(f"/api/receiver-devices/{public_id}/delete-permanently",
                       headers=headers,
                       json={"confirm": confirm, "acknowledged": acknowledged})


# ===========================================================================
# History no longer blocks the delete
# ===========================================================================
def test_a_device_with_credential_history_can_be_permanently_deleted(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)

    resp = delete_device(client, owner, public_id, confirm=public_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["public_id"] == public_id


def test_the_old_delete_still_refuses_it_proving_this_is_the_new_path(client):
    """delete_device_if_unused is unchanged and still refuses an enrolled
    Device - the new endpoint is an addition, not a loosening of the old one."""
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)
    resp = client.delete(f"/api/receiver-devices/{public_id}/permanently",
                         headers=owner, params={"confirm": public_id})
    assert resp.status_code == 409


def test_credential_and_event_history_remain_readable(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    device_id, public_id = enrolled_device(client.server_module.engine, store)
    delete_device(client, owner, public_id, confirm=public_id)

    from sqlalchemy import text
    with client.server_module.engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM receiver_credentials WHERE device_id=:d"),
                         {"d": device_id}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM receiver_credential_events WHERE device_id=:d"),
                         {"d": device_id}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM receiver_events WHERE store_id=:s"),
                         {"s": store}).scalar_one() >= 1
        assert c.execute(text("SELECT COUNT(*) FROM receiver_devices WHERE id=:d"),
                         {"d": device_id}).scalar_one() == 1, "the Device row itself must survive"


# ===========================================================================
# Operationally gone and unable to authenticate
# ===========================================================================
def test_credentials_are_revoked_and_primary_is_cleared(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    device_id, public_id = enrolled_device(client.server_module.engine, store)
    resp = delete_device(client, owner, public_id, confirm=public_id)
    assert resp.json()["credentials_revoked"] == 1

    from sqlalchemy import text
    with client.server_module.engine.connect() as c:
        assert c.execute(text("SELECT status FROM receiver_credentials WHERE device_id=:d"),
                         {"d": device_id}).scalar_one() == "revoked"
        assert c.execute(text("SELECT COUNT(*) FROM receiver_store_primary_device "
                              "WHERE device_id=:d"), {"d": device_id}).scalar_one() == 0


def test_the_device_becomes_permanently_non_operational(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    device_id, public_id = enrolled_device(client.server_module.engine, store)
    delete_device(client, owner, public_id, confirm=public_id)

    from sqlalchemy import text
    with client.server_module.engine.connect() as c:
        row = c.execute(text("SELECT status, deleted_at FROM receiver_devices WHERE id=:d"),
                        {"d": device_id}).first()
    # 'retired' is what the Receiver authentication path already refuses, so
    # the Device cannot reconnect without any new check being added.
    assert row.status == "retired"
    assert row.deleted_at is not None


def test_a_deleted_device_disappears_from_the_operational_device_list(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)
    before = client.get(f"/api/stores/{store}/receiver-devices", headers=owner).json()
    assert public_id in {d["public_id"] for d in before}

    delete_device(client, owner, public_id, confirm=public_id)

    after = client.get(f"/api/stores/{store}/receiver-devices", headers=owner).json()
    assert public_id not in {d["public_id"] for d in after}


def test_a_deleted_device_cannot_be_restored_or_re_enabled(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)
    delete_device(client, owner, public_id, confirm=public_id)

    assert client.post(f"/api/receiver-devices/{public_id}/restore",
                       headers=owner).status_code == 409


def test_the_public_id_cannot_be_reused(client):
    """The row survives, so the UNIQUE index still holds the identifier."""
    owner = sign_in(client)
    store = store_id_of(client, owner)
    device_id, public_id = enrolled_device(client.server_module.engine, store)
    delete_device(client, owner, public_id, confirm=public_id)

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    now = datetime.now(timezone.utc).isoformat()
    with pytest.raises(IntegrityError):
        with client.server_module.engine.begin() as c:
            c.execute(text(
                "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
                "enrolled_at, created_at, updated_at) "
                "VALUES (:p, :s, 'Impostor', 'active', :now, :now, :now)"),
                {"p": public_id, "s": store, "now": now})


# ===========================================================================
# Confirmation, permissions, audit
# ===========================================================================
def test_typed_confirmation_and_acknowledgement_are_required(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)

    assert delete_device(client, owner, public_id, confirm="wrong").status_code == 409
    assert delete_device(client, owner, public_id, confirm=public_id,
                         acknowledged=False).status_code == 400
    devices = client.get(f"/api/stores/{store}/receiver-devices", headers=owner).json()
    assert public_id in {d["public_id"] for d in devices}


@pytest.mark.parametrize("role", ["ADMIN", "BROADCASTER", "VIEWER"])
def test_only_super_admin_may_permanently_delete_a_device(client, role):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)
    make_user(client, owner, f"actor{role.lower()}", role)
    headers = sign_in(client, f"actor{role.lower()}")

    assert delete_device(client, headers, public_id, confirm=public_id).status_code == 403


def test_the_deletion_is_audited_without_leaking_credentials(client):
    owner = sign_in(client)
    store = store_id_of(client, owner)
    _, public_id = enrolled_device(client.server_module.engine, store)
    delete_device(client, owner, public_id, confirm=public_id)

    events = client.get(f"/api/receiver-devices/{public_id}/deletion-events", headers=owner)
    assert events.status_code == 200, events.text
    rows = events.json()["events"]
    assert len(rows) == 1
    assert rows[0]["public_id"] == public_id
    assert rows[0]["actor_user_id"] is not None
    for forbidden in ("token_hash", "password", "bearer ", "hmac", "secret"):
        assert forbidden not in events.text.lower()
