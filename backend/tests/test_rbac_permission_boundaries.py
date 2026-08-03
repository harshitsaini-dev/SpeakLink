"""Two permission boundaries an operator found broken, against the real API.

BUG 1 - Store MANAGEMENT visibility decided Broadcast TARGET visibility.

Broadcast Console built its target list from ``GET /api/stores``, guarded by
``menu.stores.view`` ("View Store Management"). So an operator allowed to
broadcast but not to administer Stores opened the Console and found an empty
table: the fetch behind it had already returned 403, and nothing on screen
said so. Managing the records and pointing a broadcast at them are different
kinds of trust, and one permission was deciding both.

BUG 2 - "Manage User Rights" did nothing.

``GET``/``PUT /api/users/{id}/permissions`` required ``require_super_admin`` -
a literal "is this account OWNER" test - rather than the permission whose UI
label is "Manage User Rights". An OWNER could grant ``users.permissions.manage``
to an ADMIN, the ADMIN's effective set genuinely contained it, and the ADMIN
still got 403. A granted right with no effect anywhere.

The tests below are written against the HTTP surface rather than the resolver,
because both defects lived in the gap between "the permission resolves" and
"the endpoint honours it" - a unit test of the resolver passed throughout.
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

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope")]:
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


def make_store(client, headers, code, name, city="TESTVILLE", region="TEST ZONE"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": name, "city": city, "region": region})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def set_override(client, headers, user_id, code, effect):
    return client.put(f"/api/users/{user_id}/permissions", headers=headers,
                      json={"changes": [{"code": code, "effect": effect}]})


def set_scope(client, headers, user_id, store_ids):
    return client.put(f"/api/users/{user_id}/store-scope", headers=headers,
                      json={"entries": [{"scope_type": "STORE", "store_id": sid}
                                        for sid in store_ids]})


def target_codes(client, headers):
    response = client.get("/api/broadcast/target-stores", headers=headers)
    assert response.status_code == 200, response.text
    return {s["store_code"] for s in response.json()["stores"]}


# ===========================================================================
# BUG 1 - Broadcast targets are not gated on Store Management
# ===========================================================================
def test_broadcaster_without_store_management_still_sees_broadcast_targets(client, owner):
    """The exact operator report: Broadcast ALLOW, Store Management DENY."""
    bp = make_store(client, owner, "QAB1", "QA Bindapur")
    rg = make_store(client, owner, "QAB2", "QA Rajgarh")
    make_store(client, owner, "QAB3", "QA Vasant Place")

    user_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, user_id, "menu.stores.view", "DENY").status_code == 200
    assert set_scope(client, owner, user_id, [bp, rg]).status_code == 200

    caster = sign_in(client, "caster")

    # The management surface is genuinely denied ...
    assert client.get("/api/stores", headers=caster).status_code == 403
    # ... and the broadcast targets are genuinely present.
    assert target_codes(client, caster) == {"QAB1", "QAB2"}


def test_store_scope_still_excludes_out_of_scope_stores_from_targets(client, owner):
    """Fixing the permission coupling must not become a way around scope."""
    bp = make_store(client, owner, "QAB1", "QA Bindapur")
    make_store(client, owner, "QAB3", "QA Vasant Place")

    user_id = make_user(client, owner, "scoped", "BROADCASTER")
    assert set_scope(client, owner, user_id, [bp]).status_code == 200

    scoped = sign_in(client, "scoped")
    codes = target_codes(client, scoped)
    assert codes == {"QAB1"}
    # Not merely hidden by the client: the id is absent from the response, so
    # a hand-crafted request cannot reveal it either.
    assert "QAB3" not in codes


def test_target_stores_requires_a_broadcast_permission(client, owner):
    """Store Management alone must not grant broadcast targeting."""
    make_store(client, owner, "QAB1", "QA Bindapur")
    user_id = make_user(client, owner, "storeadmin", "VIEWER")
    assert set_override(client, owner, user_id, "menu.stores.view", "ALLOW").status_code == 200
    assert set_override(client, owner, user_id,
                        "menu.broadcast.view", "DENY").status_code == 200

    viewer = sign_in(client, "storeadmin")
    assert client.get("/api/stores", headers=viewer).status_code == 200
    assert client.get("/api/broadcast/target-stores", headers=viewer).status_code == 403


def test_target_stores_never_exposes_secret_or_administrative_fields(client, owner):
    make_store(client, owner, "QAB1", "QA Bindapur")
    response = client.get("/api/broadcast/target-stores", headers=owner)
    assert response.status_code == 200
    body = response.json()
    assert body["stores"], "expected at least one target Store"
    allowed = {"id", "store_code", "store_name", "city", "region",
               "is_online_store", "status"}
    for store in body["stores"]:
        assert set(store) == allowed, set(store) - allowed
    # The whole serialised body, in case a field arrives nested later.
    raw = response.text.lower()
    for leak in ("receiver_token", "speaklink_rcv_v1", "credential",
                 "enrollment", "deleted_at", "deleted_by"):
        assert leak not in raw, leak


def test_archived_and_deleted_stores_are_never_broadcast_targets(client, owner):
    keep = make_store(client, owner, "QAB1", "QA Bindapur")
    archived = make_store(client, owner, "QAB4", "QA Archived Shop")
    assert client.post(f"/api/stores/{archived}/archive", headers=owner).status_code in (200, 204)

    codes = target_codes(client, owner)
    assert "QAB1" in codes
    assert "QAB4" not in codes
    assert keep is not None


def test_target_store_regions_and_cities_respect_store_scope(client, owner):
    """The old meta endpoint listed every region in the estate, unscoped."""
    bp = make_store(client, owner, "QAB1", "QA Bindapur", city="DELHI", region="NORTH")
    make_store(client, owner, "QAB3", "QA Vasant Place", city="CHENNAI", region="SOUTH")

    user_id = make_user(client, owner, "northonly", "BROADCASTER")
    assert set_scope(client, owner, user_id, [bp]).status_code == 200

    response = client.get("/api/broadcast/target-stores",
                          headers=sign_in(client, "northonly"))
    assert response.status_code == 200
    body = response.json()
    assert body["regions"] == ["NORTH"]
    assert body["cities"] == ["DELHI"]


# ===========================================================================
# BUG 2 - Manage User Rights actually works
# ===========================================================================
def test_admin_granted_manage_user_rights_can_read_and_write_them(client, owner):
    """The operator's report: OWNER grants it, ADMIN still cannot use it."""
    admin_id = make_user(client, owner, "boss", "ADMIN")
    target_id = make_user(client, owner, "caster", "BROADCASTER")

    admin = sign_in(client, "boss")
    # Before the grant: the role default excludes it, so 403.
    assert client.get(f"/api/users/{target_id}/permissions",
                      headers=admin).status_code == 403

    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200

    admin = sign_in(client, "boss")
    read = client.get(f"/api/users/{target_id}/permissions", headers=admin)
    assert read.status_code == 200, read.text
    assert read.json()["role"] == "BROADCASTER"

    written = set_override(client, admin, target_id, "menu.history.view", "DENY")
    assert written.status_code == 200, written.text

    # And it really took effect for the target.
    caster = sign_in(client, "caster")
    granted = set(client.get("/api/auth/permissions", headers=caster).json()["permissions"])
    assert "menu.history.view" not in granted


def test_admin_without_the_permission_is_refused(client, owner):
    make_user(client, owner, "boss", "ADMIN")
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    admin = sign_in(client, "boss")
    assert client.get(f"/api/users/{target_id}/permissions",
                      headers=admin).status_code == 403
    assert set_override(client, admin, target_id,
                        "menu.history.view", "DENY").status_code == 403


def test_explicit_deny_beats_an_allowing_role_default(client, owner):
    """OWNER holds it by role default; an explicit DENY still wins."""
    other_id = make_user(client, owner, "second", "ADMIN")
    assert set_override(client, owner, other_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    assert set_override(client, owner, other_id,
                        "users.permissions.manage", "DENY").status_code == 200
    blocked = sign_in(client, "second")
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    assert client.get(f"/api/users/{target_id}/permissions",
                      headers=blocked).status_code == 403


def test_admin_cannot_manage_an_owner_or_another_admin(client, owner):
    admin_id = make_user(client, owner, "boss", "ADMIN")
    peer_id = make_user(client, owner, "peer", "ADMIN")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    admin = sign_in(client, "boss")

    owner_id = client.get("/api/auth/me", headers=owner).json()["id"]
    # A crafted request against the OWNER, not merely a hidden button.
    assert client.get(f"/api/users/{owner_id}/permissions",
                      headers=admin).status_code == 403
    assert set_override(client, admin, owner_id,
                        "menu.users.view", "DENY").status_code == 403
    # And against a same-level ADMIN, matching the existing hierarchy.
    assert client.get(f"/api/users/{peer_id}/permissions",
                      headers=admin).status_code == 403


def test_admin_cannot_edit_its_own_rights(client, owner):
    admin_id = make_user(client, owner, "boss", "ADMIN")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    admin = sign_in(client, "boss")
    refused = set_override(client, admin, admin_id, "stores.delete_permanently", "ALLOW")
    assert refused.status_code == 403
    # Two independent guards refuse this - the role hierarchy (an ADMIN may not
    # manage an ADMIN, itself included) and SelfRightsEditRefused. Which one
    # answers first is an implementation detail; that it is refused is not.
    admin_after = sign_in(client, "boss")
    granted = set(client.get("/api/auth/permissions",
                             headers=admin_after).json()["permissions"])
    assert "stores.delete_permanently" not in granted


def test_admin_cannot_grant_a_permission_it_does_not_hold(client, owner):
    """Otherwise Manage User Rights would be a bypass for every restriction."""
    admin_id = make_user(client, owner, "boss", "ADMIN")
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    admin = sign_in(client, "boss")

    # ADMIN never holds stores.delete_permanently by role default.
    refused = set_override(client, admin, target_id, "stores.delete_permanently", "ALLOW")
    assert refused.status_code == 403
    assert "do not hold" in refused.json()["detail"].lower()

    # The target really did not receive it.
    caster = sign_in(client, "caster")
    granted = set(client.get("/api/auth/permissions", headers=caster).json()["permissions"])
    assert "stores.delete_permanently" not in granted


def test_admin_may_still_revoke_a_permission_it_does_not_hold(client, owner):
    """Taking authority away can never raise the actor's own."""
    admin_id = make_user(client, owner, "boss", "ADMIN")
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    admin = sign_in(client, "boss")
    assert set_override(client, admin, target_id,
                        "stores.delete_permanently", "DENY").status_code == 200


def test_owner_can_still_manage_rights_exactly_as_before(client, owner):
    """The permission replaced a role check; it must not narrow OWNER."""
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    assert client.get(f"/api/users/{target_id}/permissions",
                      headers=owner).status_code == 200
    assert set_override(client, owner, target_id,
                        "stores.delete_permanently", "ALLOW").status_code == 200
    # An OWNER target is still refused, by the pre-existing guard.
    owner_id = client.get("/api/auth/me", headers=owner).json()["id"]
    assert set_override(client, owner, owner_id,
                        "menu.users.view", "DENY").status_code in (403, 409)


def test_store_scope_routes_remain_owner_only(client, owner):
    """This fix deliberately did not touch Store Scope's reservation."""
    admin_id = make_user(client, owner, "boss", "ADMIN")
    target_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    admin = sign_in(client, "boss")
    assert client.get(f"/api/users/{target_id}/store-scope",
                      headers=admin).status_code == 403


# ===========================================================================
# Cross-bug regression matrix
# ===========================================================================
def test_user_a_broadcast_allow_management_deny_rights_deny(client, owner):
    bp = make_store(client, owner, "QAB1", "QA Bindapur")
    user_id = make_user(client, owner, "usera", "BROADCASTER")
    assert set_override(client, owner, user_id, "menu.stores.view", "DENY").status_code == 200
    assert set_scope(client, owner, user_id, [bp]).status_code == 200
    a = sign_in(client, "usera")

    assert target_codes(client, a) == {"QAB1"}
    assert client.get("/api/stores", headers=a).status_code == 403
    assert client.get(f"/api/users/{user_id}/permissions", headers=a).status_code == 403


def test_user_b_broadcast_deny_management_allow_rights_deny(client, owner):
    make_store(client, owner, "QAB1", "QA Bindapur")
    user_id = make_user(client, owner, "userb", "VIEWER")
    assert set_override(client, owner, user_id, "menu.stores.view", "ALLOW").status_code == 200
    assert set_override(client, owner, user_id, "menu.broadcast.view", "DENY").status_code == 200
    b = sign_in(client, "userb")

    assert client.get("/api/stores", headers=b).status_code == 200
    assert client.get("/api/broadcast/target-stores", headers=b).status_code == 403
    assert client.get(f"/api/users/{user_id}/permissions", headers=b).status_code == 403


def test_user_c_admin_with_rights_manages_eligible_but_not_protected(client, owner):
    admin_id = make_user(client, owner, "userc", "ADMIN")
    caster_id = make_user(client, owner, "caster", "BROADCASTER")
    assert set_override(client, owner, admin_id,
                        "users.permissions.manage", "ALLOW").status_code == 200
    c = sign_in(client, "userc")

    assert client.get(f"/api/users/{caster_id}/permissions", headers=c).status_code == 200
    owner_id = client.get("/api/auth/me", headers=owner).json()["id"]
    assert client.get(f"/api/users/{owner_id}/permissions", headers=c).status_code == 403
