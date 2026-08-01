"""Receiver Device archive/restore/permanent-delete over the real HTTP surface.

Revoke already existed and is permanent-by-design ("retire this Device
permanently"). What was missing is the Store/User-shaped pair: a reversible
archive/restore, and a permanent delete that is refused the moment any
history depends on the row - exactly the deletion_safety.py rule already
applied to Stores and Users.
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "receiver_enrollment_api", "deletion_safety")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one

    run_receiver_credential_phase_one(server_module.engine)

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client):
    response = client.post("/api/auth/login",
                            json={"username": "founder", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _insert_bare_device(engine, *, store_id: int, public_id: str) -> None:
    """A Device row with zero credentials - the one shape a real enrolment
    never produces, and exactly the shape a permanent delete must accept."""
    from sqlalchemy import text
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO receiver_devices "
                "(public_id, store_id, display_name, status, enrolled_at, "
                "created_at, updated_at) "
                "VALUES (:pid, :sid, 'Test Device', 'active', :now, :now, :now)"
            ),
            {"pid": public_id, "sid": store_id, "now": now},
        )


def _insert_device_with_credential(engine, *, store_id: int, public_id: str) -> None:
    from sqlalchemy import text
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        device_id = connection.execute(
            text(
                "INSERT INTO receiver_devices "
                "(public_id, store_id, display_name, status, enrolled_at, "
                "created_at, updated_at) "
                "VALUES (:pid, :sid, 'Enrolled Device', 'active', :now, :now, :now) "
                "RETURNING id"
            ),
            {"pid": public_id, "sid": store_id, "now": now},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO receiver_credentials "
                "(public_id, device_id, credential_version, token_format, token_hash, "
                "hash_key_version, status, issued_at, created_at) "
                "VALUES (:cred_pid, :did, 1, 'echocast_rcv', :hash, 1, 'active', :now, :now)"
            ),
            {"cred_pid": str(uuid.uuid4()), "did": device_id, "hash": "a" * 64,
             "now": now},
        )


def _first_store_id(client) -> int:
    stores = client.get("/api/stores", headers=sign_in(client)).json()
    return stores[0]["id"]


def test_archive_disables_and_stamps_archived_at_then_restore_reverts_to_disabled(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)

    archived = client.post(f"/api/receiver-devices/{device_id}/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    body = archived.json()
    assert body["status"] == "disabled"
    assert body["archived_at"] is not None

    restored = client.post(f"/api/receiver-devices/{device_id}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    restored_body = restored.json()
    assert restored_body["archived_at"] is None
    # Restore returns to DISABLED, never straight back to active.
    assert restored_body["status"] == "disabled"


def test_archive_clears_the_primary_assignment(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)
    promote = client.post(f"/api/receiver-devices/{device_id}/promote", headers=headers)
    assert promote.status_code == 200, promote.text

    roles_before = client.get(f"/api/stores/{store_id}/receiver-devices/roles", headers=headers)
    assert any(r["public_id"] == device_id and str(r["role"]).upper().endswith("PRIMARY")
               for r in roles_before.json())

    client.post(f"/api/receiver-devices/{device_id}/archive", headers=headers)
    roles_after = client.get(f"/api/stores/{store_id}/receiver-devices/roles", headers=headers)
    assert not any(r["public_id"] == device_id and str(r["role"]).upper().endswith("PRIMARY")
                   for r in roles_after.json())


def test_permanent_delete_is_refused_for_a_device_with_an_issued_credential(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_device_with_credential(client.server_module.engine, store_id=store_id,
                                   public_id=device_id)

    dependencies = client.get(f"/api/receiver-devices/{device_id}/dependencies",
                              headers=headers)
    assert dependencies.status_code == 200, dependencies.text
    assert dependencies.json()["deletable"] is False
    assert dependencies.json()["counts"]["receiver_credentials"] == 1

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 409, deleted.text

    still_there = client.get(f"/api/receiver-devices/{device_id}", headers=headers)
    assert still_there.status_code == 200


def test_permanent_delete_refuses_an_active_never_used_device(client):
    """ACTIVE is still ordinary rotation. A Device is never permanently
    deleted while active, no matter how unused it is."""
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 409, deleted.text

    still_there = client.get(f"/api/receiver-devices/{device_id}", headers=headers)
    assert still_there.status_code == 200


def test_permanent_delete_refuses_a_disabled_but_not_archived_device(client):
    """DISABLED alone does not mean retired - it can be re-enabled with no
    ceremony, so it is refused the same as ACTIVE."""
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)
    disable = client.post(f"/api/receiver-devices/{device_id}/disable", headers=headers)
    assert disable.status_code == 200, disable.text
    assert disable.json()["archived_at"] is None

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 409, deleted.text


def test_permanent_delete_succeeds_for_an_archived_never_used_device(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)
    archive = client.post(f"/api/receiver-devices/{device_id}/archive", headers=headers)
    assert archive.status_code == 200, archive.text

    dependencies = client.get(f"/api/receiver-devices/{device_id}/dependencies", headers=headers)
    assert dependencies.json()["deletable"] is True

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 200, deleted.text

    gone = client.get(f"/api/receiver-devices/{device_id}", headers=headers)
    assert gone.status_code == 404


def test_permanent_delete_succeeds_for_a_revoked_never_used_device(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)
    revoke = client.post(f"/api/receiver-devices/{device_id}/revoke", headers=headers)
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "retired"

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 200, deleted.text

    gone = client.get(f"/api/receiver-devices/{device_id}", headers=headers)
    assert gone.status_code == 404


def test_permanent_delete_refuses_an_archived_device_that_is_still_primary(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)
    promote = client.post(f"/api/receiver-devices/{device_id}/promote", headers=headers)
    assert promote.status_code == 200, promote.text

    # Force an inconsistent state directly: archived, but the primary row was
    # never cleared. archive_device() always clears it - this proves the
    # permanent-delete check is a real, independent guard, not a side effect
    # of archiving always happening to clear it first.
    from sqlalchemy import text
    with client.server_module.engine.begin() as connection:
        device_row_id = connection.execute(
            text("SELECT id FROM receiver_devices WHERE public_id = :pid"),
            {"pid": device_id}).scalar_one()
        connection.execute(
            text("UPDATE receiver_devices SET archived_at = :now WHERE public_id = :pid"),
            {"now": datetime.now(timezone.utc).isoformat(), "pid": device_id})
        connection.execute(
            text(
                "INSERT INTO receiver_store_primary_device "
                "(store_id, device_id, promoted_at, promoted_by) "
                "VALUES (:sid, :did, :now, NULL) "
                "ON CONFLICT(store_id) DO UPDATE SET device_id = excluded.device_id"
            ),
            {"sid": store_id, "did": device_row_id,
             "now": datetime.now(timezone.utc).isoformat()},
        )

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": device_id})
    assert deleted.status_code == 409, deleted.text


def test_permanent_delete_refuses_a_wrong_typed_confirmation(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)

    deleted = client.delete(f"/api/receiver-devices/{device_id}/permanently",
                           headers=headers, params={"confirm": "not-the-right-id"})
    assert deleted.status_code == 409

    still_there = client.get(f"/api/receiver-devices/{device_id}", headers=headers)
    assert still_there.status_code == 200


def test_archive_and_permanent_delete_are_gated_by_their_own_permissions(client):
    headers = sign_in(client)
    store_id = _first_store_id(client)
    device_id = str(uuid.uuid4())
    _insert_bare_device(client.server_module.engine, store_id=store_id, public_id=device_id)

    viewer_resp = client.post("/api/users", headers=headers, json={
        "username": "viewbot", "display_name": "View Bot", "role": "VIEWER", "password": PASSWORD})
    assert viewer_resp.status_code == 201, viewer_resp.text
    login = client.post("/api/auth/login", json={"username": "viewbot", "password": PASSWORD})
    viewer_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert client.post(f"/api/receiver-devices/{device_id}/archive",
                       headers=viewer_headers).status_code == 403
    assert client.delete(f"/api/receiver-devices/{device_id}/permanently",
                         headers=viewer_headers,
                         params={"confirm": device_id}).status_code == 403
