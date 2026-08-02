"""Fine-grained, per-user permissions on top of the four HQ roles.

Covers the default role matrix, the ALLOW/DENY override priority rule, menu
vs. action independence, the two Owner-safety refusals, and the audit trail -
each against the real HTTP surface (`server.app`) over an isolated per-test
SQLite database, the same way ``test_user_admin_endpoints.py`` does.
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from permission_catalog import (  # noqa: E402
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_CODES,
)
from rbac import Role  # noqa: E402


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
                               "user_lifecycle", "schemas", "permission_catalog")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username: str, password: str = PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client, "founder")


def make_user(client, headers, username, role, password=PASSWORD):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def permissions_for(client, headers) -> set[str]:
    response = client.get("/api/auth/permissions", headers=headers)
    assert response.status_code == 200, response.text
    return set(response.json()["permissions"])


def set_override(client, headers, user_id, code, effect):
    return client.put(f"/api/users/{user_id}/permissions", headers=headers,
                      json={"changes": [{"code": code, "effect": effect}]})


# ===========================================================================
# 1-4. Default role matrices (pure, no DB - the code IS the source of truth)
# ===========================================================================
def test_owner_default_permissions_are_the_entire_catalog():
    assert DEFAULT_ROLE_PERMISSIONS[Role.OWNER] == PERMISSION_CODES


def test_admin_default_permissions_exclude_permissions_manage_and_permanent_delete():
    admin = DEFAULT_ROLE_PERMISSIONS[Role.ADMIN]
    assert "users.permissions.manage" not in admin
    assert "devices.delete_permanently" not in admin
    assert "stores.delete_permanently" not in admin
    # ADMIN may still archive a Store or a Device - only permanent deletion
    # is reserved.
    assert "devices.archive" in admin
    assert "stores.archive" in admin
    # Every irreversibly destructive code is reserved, plus rights management.
    from permission_catalog import DESTRUCTIVE_CODES
    assert admin == PERMISSION_CODES - {"users.permissions.manage"} - DESTRUCTIVE_CODES
    for destructive in DESTRUCTIVE_CODES:
        assert destructive not in admin, destructive


def test_devices_delete_permanently_defaults_to_super_admin_only():
    for role, codes in DEFAULT_ROLE_PERMISSIONS.items():
        if role is Role.OWNER:
            assert "devices.delete_permanently" in codes
        else:
            assert "devices.delete_permanently" not in codes


def test_stores_delete_permanently_defaults_to_super_admin_only():
    for role, codes in DEFAULT_ROLE_PERMISSIONS.items():
        if role is Role.OWNER:
            assert "stores.delete_permanently" in codes
        else:
            assert "stores.delete_permanently" not in codes


def test_devices_archive_is_allowed_for_owner_and_admin_only():
    assert "devices.archive" in DEFAULT_ROLE_PERMISSIONS[Role.OWNER]
    assert "devices.archive" in DEFAULT_ROLE_PERMISSIONS[Role.ADMIN]
    assert "devices.archive" not in DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    assert "devices.archive" not in DEFAULT_ROLE_PERMISSIONS[Role.VIEWER]


def test_broadcaster_default_permissions_are_exactly_broadcast_and_read_only():
    # menu.stores.view is included: Receiver Status (GET /api/stores) requires
    # it, and a BROADCASTER without it got a 403 there - the live defect this
    # round fixes. It is still read-only: no stores.create/update/archive.
    broadcaster = DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    # broadcast.emergency_stop and broadcast.view_ownership are deliberately
    # absent: one stops every other operator's broadcast, the other reveals
    # whose broadcast is using a Store. Both are ADMIN/OWNER by default and
    # reachable per user through an override.
    assert broadcaster == {
        "menu.broadcast.view", "broadcast.start", "broadcast.stop",
        "menu.history.view", "menu.receivers.view", "menu.stores.view",
    }
    # No Store modification, no Device security changes, no User management.
    assert not any(code.startswith("stores.") for code in broadcaster)
    assert not any(code.startswith("devices.") and code != "menu.receivers.view"
                   for code in broadcaster)
    assert not any(code.startswith("users.") or code == "menu.users.view"
                   for code in broadcaster)


def test_viewer_default_permissions_are_read_only_and_exclude_users():
    viewer = DEFAULT_ROLE_PERMISSIONS[Role.VIEWER]
    assert viewer == {
        "menu.broadcast.view", "menu.stores.view", "menu.receivers.view",
        "menu.history.view", "menu.logs.view",
    }
    assert "menu.users.view" not in viewer
    assert not any(code.endswith((".create", ".update", ".archive", ".disable",
                                  ".revoke", ".rotate", ".assign", ".manage"))
                   or code in ("broadcast.start", "broadcast.stop", "broadcast.emergency_stop")
                   for code in viewer)


# ===========================================================================
# 8. Default deny for an unknown/unrecognised permission code
# ===========================================================================
def test_unknown_permission_code_is_not_in_any_role_default():
    for codes in DEFAULT_ROLE_PERMISSIONS.values():
        assert "not.a.real.permission" not in codes


def test_owner_permissions_endpoint_never_lists_an_unknown_code(client, owner):
    granted = permissions_for(client, owner)
    assert granted <= PERMISSION_CODES
    assert "not.a.real.permission" not in granted


# ===========================================================================
# 5-7. Override priority: explicit DENY/ALLOW beat role default; INHERIT reverts
# ===========================================================================
def test_explicit_allow_overrides_a_role_default_deny(client, owner):
    """BROADCASTER never gets stores.update by default; an ALLOW override grants it."""
    user_id = make_user(client, owner, "bob", Role.BROADCASTER.value)
    bob = sign_in(client, "bob")
    assert "stores.update" not in permissions_for(client, bob)

    resp = set_override(client, owner, user_id, "stores.update", "ALLOW")
    assert resp.status_code == 200, resp.text

    bob = sign_in(client, "bob")
    assert "stores.update" in permissions_for(client, bob)


def test_explicit_deny_overrides_a_role_default_allow(client, owner):
    """ADMIN gets stores.update by default; a DENY override removes it."""
    user_id = make_user(client, owner, "carol", Role.ADMIN.value)
    carol = sign_in(client, "carol")
    assert "stores.update" in permissions_for(client, carol)

    resp = set_override(client, owner, user_id, "stores.update", "DENY")
    assert resp.status_code == 200, resp.text

    carol = sign_in(client, "carol")
    assert "stores.update" not in permissions_for(client, carol)


def test_inherit_removes_the_override_and_reverts_to_role_default(client, owner):
    user_id = make_user(client, owner, "dave", Role.ADMIN.value)
    set_override(client, owner, user_id, "stores.update", "DENY")
    dave = sign_in(client, "dave")
    assert "stores.update" not in permissions_for(client, dave)

    resp = set_override(client, owner, user_id, "stores.update", "INHERIT")
    assert resp.status_code == 200, resp.text
    dave = sign_in(client, "dave")
    assert "stores.update" in permissions_for(client, dave)

    row = next(p for p in resp.json()["permissions"] if p["code"] == "stores.update")
    assert row["override"] == "INHERIT"


# ===========================================================================
# 10-13. Menu view and action rights are independently enforced by the backend
# ===========================================================================
def test_view_store_without_edit_right_can_list_but_not_update(client, owner):
    user_id = make_user(client, owner, "erin", Role.ADMIN.value)
    set_override(client, owner, user_id, "stores.update", "DENY")
    erin = sign_in(client, "erin")

    assert client.get("/api/stores", headers=erin).status_code == 200
    stores = client.get("/api/stores", headers=erin).json()
    store_id = stores[0]["id"]
    resp = client.put(f"/api/stores/{store_id}", headers=erin,
                      json={"store_name": "Renamed"})
    assert resp.status_code == 403


def test_view_broadcast_console_without_start_right_cannot_start(client, owner):
    user_id = make_user(client, owner, "frank", Role.BROADCASTER.value)
    assert "menu.broadcast.view" in DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    set_override(client, owner, user_id, "broadcast.start", "DENY")
    frank = sign_in(client, "frank")

    assert client.get("/api/broadcast/current", headers=frank).status_code == 200
    resp = client.post("/api/broadcast/sessions", headers=frank,
                       json={"campaign_name": "x", "target_mode": "all"})
    assert resp.status_code == 403


def test_emergency_stop_requires_its_own_permission_independent_of_start_stop(client, owner):
    user_id = make_user(client, owner, "gina", Role.BROADCASTER.value)
    set_override(client, owner, user_id, "broadcast.emergency_stop", "DENY")
    gina = sign_in(client, "gina")

    assert client.post("/api/broadcast/emergency-stop", headers=gina).status_code == 403
    # start/stop are untouched by the emergency_stop-specific override.
    assert "broadcast.start" in permissions_for(client, gina)
    assert "broadcast.stop" in permissions_for(client, gina)


def test_device_rotate_revoke_and_enrollment_are_independent_permissions(client, owner):
    user_id = make_user(client, owner, "hank", Role.ADMIN.value)
    set_override(client, owner, user_id, "devices.rotate", "DENY")
    hank = sign_in(client, "hank")

    stores = client.get("/api/stores", headers=hank).json()
    store_id = stores[0]["id"]
    code_resp = client.post("/api/receiver-devices/enrollment-codes", headers=hank,
                            json={"store_id": store_id})
    assert code_resp.status_code == 200, code_resp.text  # enrollment.create still allowed

    granted = permissions_for(client, hank)
    assert "devices.rotate" not in granted
    assert "devices.revoke" in granted
    assert "devices.enrollment.create" in granted


# ===========================================================================
# 14-15. Only OWNER may change overrides; OWNER can never be locked out
# ===========================================================================
def test_only_owner_can_change_permission_overrides(client, owner):
    user_id = make_user(client, owner, "ivan", Role.ADMIN.value)
    ivan = sign_in(client, "ivan")

    resp = set_override(client, ivan, user_id, "stores.update", "DENY")
    assert resp.status_code == 403

    read_resp = client.get(f"/api/users/{user_id}/permissions", headers=ivan)
    assert read_resp.status_code == 403


def test_the_owner_account_itself_cannot_be_locked_out_by_an_override(client, owner):
    me = client.get("/api/auth/me", headers=owner).json()
    resp = set_override(client, owner, me["id"], "users.permissions.manage", "DENY")
    assert resp.status_code == 409

    # The refusal did not silently write anything.
    granted = permissions_for(client, owner)
    assert "users.permissions.manage" in granted


def test_a_second_owner_also_cannot_be_overridden(client, owner):
    """Not just "the last one" - OWNER's rights are never narrowed at all."""
    second_owner_id = make_user(client, owner, "jane", Role.OWNER.value)
    resp = set_override(client, owner, second_owner_id, "stores.update", "DENY")
    assert resp.status_code == 409


# ===========================================================================
# 16. A disabled account has no effective permissions, whatever its role/overrides
# ===========================================================================
def test_disabled_user_has_no_effective_permissions(client, owner):
    user_id = make_user(client, owner, "karen", Role.OWNER.value)
    disable_resp = client.post(f"/api/users/{user_id}/disable", headers=owner)
    assert disable_resp.status_code == 200, disable_resp.text

    login = client.post("/api/auth/login",
                        json={"username": "karen", "password": PASSWORD})
    # A disabled account cannot even sign in to ask what it can do - which is
    # itself the enforcement: no token means no permission is ever granted.
    assert login.status_code in (401, 403)


# ===========================================================================
# 18-19. Every permission change is audited, and the audit carries no secrets
# ===========================================================================
def test_permission_change_is_recorded_in_the_audit_table(client, owner):
    user_id = make_user(client, owner, "leo", Role.ADMIN.value)
    resp = set_override(client, owner, user_id, "stores.update", "DENY")
    assert resp.status_code == 200

    engine = client.server_module.engine
    with engine.connect() as connection:
        from sqlalchemy import text
        rows = connection.execute(
            text(
                "SELECT actor_user_id, target_user_id, permission_code, "
                "old_value, new_value FROM permission_audit_events "
                "WHERE target_user_id = :uid"
            ),
            {"uid": user_id},
        ).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.permission_code == "stores.update"
    assert row.old_value == "INHERIT"
    assert row.new_value == "DENY"
    owner_me = client.get("/api/auth/me", headers=owner).json()
    assert row.actor_user_id == owner_me["id"]
    assert row.target_user_id == user_id


def test_permission_audit_and_response_carry_no_secrets(client, owner):
    user_id = make_user(client, owner, "mia", Role.ADMIN.value)
    resp = set_override(client, owner, user_id, "stores.update", "DENY")
    payload = resp.text.lower()
    for forbidden in ("password", "jwt", "bearer ", "hmac", "secret"):
        assert forbidden not in payload

    engine = client.server_module.engine
    with engine.connect() as connection:
        from sqlalchemy import text
        rows = connection.execute(text("SELECT * FROM permission_audit_events")).all()
    for row in rows:
        for value in row:
            text_value = str(value).lower()
            for forbidden in ("password", "jwt", "bearer ", "hmac"):
                assert forbidden not in text_value


def test_unknown_permission_code_is_refused_not_stored(client, owner):
    user_id = make_user(client, owner, "nina", Role.ADMIN.value)
    resp = set_override(client, owner, user_id, "not.a.real.permission", "ALLOW")
    assert resp.status_code == 400

    engine = client.server_module.engine
    with engine.connect() as connection:
        from sqlalchemy import text
        count = connection.execute(
            text("SELECT COUNT(*) FROM user_permission_overrides WHERE permission_code = :c"),
            {"c": "not.a.real.permission"},
        ).scalar_one()
    assert count == 0
