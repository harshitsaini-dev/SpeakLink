"""Migration safety for the permission/scope/device-archive schema additions.

All work here is against a temporary SQLite clone, built fresh by the app's
own startup_event - never the protected live database. Proves: existing
data survives, an unscoped existing account stays unrestricted, every new
migration is idempotent, and no migration ever deletes a database or leaves
a dangling foreign key.
"""

from __future__ import annotations

import os
import secrets
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh, isolated database - never the protected one - built the same
    way a real HQ boots: startup_event() runs every migration in order."""
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "receiver_enrollment_api")]:
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


def test_existing_users_roles_and_stores_are_preserved(client):
    owner = sign_in(client)
    users = client.get("/api/users", headers=owner).json()
    assert any(u["username"] == "founder" and u["role"] == "OWNER" for u in users)

    stores = client.get("/api/stores", headers=owner).json()
    assert len(stores) == 44


def test_an_existing_unscoped_admin_remains_unrestricted_after_migration(client):
    owner = sign_in(client)
    created = client.post("/api/users", headers=owner, json={
        "username": "existing_admin", "display_name": "Existing Admin",
        "role": "ADMIN", "password": PASSWORD})
    assert created.status_code == 201, created.text

    # Re-run every additive migration a second time, exactly as a restart
    # would - nothing about this account's scope was ever touched.
    from permission_catalog import ensure_permission_schema
    from store_scope import ensure_store_scope_schema
    from receiver_enrollment_api import ensure_device_archive_schema
    engine = client.server_module.engine
    ensure_permission_schema(engine)
    ensure_store_scope_schema(engine)
    ensure_device_archive_schema(engine)

    headers = sign_in(client, "existing_admin")
    visible_stores = client.get("/api/stores", headers=headers).json()
    all_stores = client.get("/api/stores", headers=owner).json()
    assert len(visible_stores) == len(all_stores)


def test_migrations_are_idempotent_and_change_nothing_on_rerun(client):
    engine = client.server_module.engine
    from permission_catalog import ensure_permission_schema
    from store_scope import ensure_store_scope_schema
    from receiver_enrollment_api import ensure_device_archive_schema

    from sqlalchemy import text
    with engine.connect() as connection:
        before_permissions = connection.execute(text("SELECT COUNT(*) FROM permissions")).scalar_one()
        before_role_perms = connection.execute(text("SELECT COUNT(*) FROM role_permissions")).scalar_one()
        before_devices = connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar_one()
        before_stores = connection.execute(text("SELECT COUNT(*) FROM stores")).scalar_one()

    for _ in range(3):
        ensure_permission_schema(engine)
        ensure_store_scope_schema(engine)
        ensure_device_archive_schema(engine)

    with engine.connect() as connection:
        after_permissions = connection.execute(text("SELECT COUNT(*) FROM permissions")).scalar_one()
        after_role_perms = connection.execute(text("SELECT COUNT(*) FROM role_permissions")).scalar_one()
        after_devices = connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar_one()
        after_stores = connection.execute(text("SELECT COUNT(*) FROM stores")).scalar_one()
        integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
        fk_violations = connection.execute(text("PRAGMA foreign_key_check")).all()

    assert after_permissions == before_permissions
    assert after_role_perms == before_role_perms
    assert after_devices == before_devices
    assert after_stores == before_stores
    assert integrity == "ok"
    assert fk_violations == []


def test_archive_column_migration_is_idempotent(client):
    from receiver_enrollment_api import ensure_device_archive_schema
    engine = client.server_module.engine

    for _ in range(3):
        ensure_device_archive_schema(engine)

    from sqlalchemy import text
    with engine.connect() as connection:
        columns = [c[1] for c in connection.exec_driver_sql("PRAGMA table_info(receiver_devices)")]
        assert columns.count("archived_at") == 1


def test_no_database_file_is_ever_deleted_by_a_migration(client):
    database_path = client.database_path
    assert Path(database_path).exists()
    size_before = Path(database_path).stat().st_size

    from permission_catalog import ensure_permission_schema
    from store_scope import ensure_store_scope_schema
    from receiver_enrollment_api import ensure_device_archive_schema
    engine = client.server_module.engine
    ensure_permission_schema(engine)
    ensure_store_scope_schema(engine)
    ensure_device_archive_schema(engine)

    assert Path(database_path).exists()
    # Additive schema can grow the file; it must never shrink to zero or
    # vanish, which is what a destructive "reset" migration would look like.
    assert Path(database_path).stat().st_size >= size_before


def test_foreign_key_check_is_clean_after_a_real_device_lifecycle(client):
    """Exercise archive -> restore -> revoke -> permanent-delete on a real
    clone and prove no dangling reference is left behind."""
    owner = sign_in(client)
    stores = client.get("/api/stores", headers=owner).json()
    store_id = stores[0]["id"]

    from sqlalchemy import text
    import uuid
    from datetime import datetime, timezone
    engine = client.server_module.engine
    public_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO receiver_devices "
                "(public_id, store_id, display_name, status, enrolled_at, created_at, updated_at) "
                "VALUES (:pid, :sid, 'Migration Safety Device', 'active', :now, :now, :now)"
            ),
            {"pid": public_id, "sid": store_id, "now": now},
        )

    assert client.post(f"/api/receiver-devices/{public_id}/archive", headers=owner).status_code == 200
    assert client.post(f"/api/receiver-devices/{public_id}/restore", headers=owner).status_code == 200
    assert client.post(f"/api/receiver-devices/{public_id}/archive", headers=owner).status_code == 200
    deleted = client.delete(f"/api/receiver-devices/{public_id}/permanently",
                           headers=owner, params={"confirm": public_id})
    assert deleted.status_code == 200, deleted.text

    with engine.connect() as connection:
        fk_violations = connection.execute(text("PRAGMA foreign_key_check")).all()
        integrity = connection.execute(text("PRAGMA integrity_check")).scalar_one()
    assert fk_violations == []
    assert integrity == "ok"
