"""The physical delivery boundary, against the real HTTP surface.

The catalog tests next door prove the permission exists and that the upgrade
grants it to the right roles. They cannot prove the API honours it - a resolver
can be perfectly correct while an endpoint never asks it, which is precisely the
gap the existing RBAC boundary tests were written for.

So everything here goes through the API, as a signed-in account, including the
crafted requests a hidden button does not prevent.
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

PASSWORD = "a-long-enough-temporary-password"
CODE = "broadcast.store_delivery"


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


@pytest.fixture()
def link_only(client, owner):
    """A broadcaster with physical delivery removed and NO Store Scope.

    Blank Scope is the interesting case: it means unrestricted everywhere else
    in the product, so if the boundary were expressed through Scope this account
    would be able to reach every Store in the estate.
    """
    make_store(client, owner, "QAB1", "QA Bindapur")
    make_store(client, owner, "QAB2", "QA Rohini")
    user_id = make_user(client, owner, "linkonly", "BROADCASTER")
    assert set_override(client, owner, user_id, CODE, "DENY").status_code == 200
    return user_id, sign_in(client, "linkonly")


@pytest.fixture()
def physical(client, owner):
    """An ordinary broadcaster, unchanged by this feature."""
    user_id = make_user(client, owner, "physical", "BROADCASTER")
    return user_id, sign_in(client, "physical")


# ===========================================================================
# Refused, and refused for the right reason
# ===========================================================================

def test_a_link_only_broadcaster_cannot_list_physical_targets(client, link_only):
    """Store inventory for broadcasting IS physical delivery information."""
    _, headers = link_only
    assert client.get("/api/broadcast/target-stores", headers=headers).status_code == 403


def test_a_link_only_broadcaster_still_holds_ordinary_broadcast_rights(client, link_only):
    """The boundary must remove physical delivery and nothing else."""
    user_id, headers = link_only
    rights = client.get(f"/api/users/{user_id}/permissions",
                        headers=sign_in(client, "founder")).json()
    effective = {row["code"]: row for row in rights["permissions"]}
    assert effective[CODE]["effective"] is False
    assert effective["broadcast.start"]["effective"] is True
    assert effective["menu.broadcast.view"]["effective"] is True


@pytest.mark.parametrize("payload", [
    {"campaign_name": "crafted", "target_mode": "all"},
    {"campaign_name": "crafted", "target_mode": "online_only"},
    {"campaign_name": "crafted", "target_mode": "region", "region": "TEST ZONE"},
    {"campaign_name": "crafted", "target_mode": "city", "city": "TESTVILLE"},
])
def test_no_physical_target_mode_can_be_reached_by_a_crafted_request(client, link_only, payload):
    """Hiding the selector is not the control. This is."""
    _, headers = link_only
    response = client.post("/api/broadcast/sessions", headers=headers, json=payload)
    assert response.status_code == 403, response.text


def test_selecting_stores_by_id_is_refused(client, owner, link_only):
    _, headers = link_only
    stores = client.get("/api/stores", headers=owner).json()
    ids = [row["id"] for row in (stores if isinstance(stores, list) else stores["items"])]
    response = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "crafted", "target_mode": "selected", "store_ids": ids})
    assert response.status_code == 403, response.text


def test_an_omitted_selector_does_not_fall_through_to_every_store(client, link_only):
    """'all' with nothing named is the most dangerous shape of this request."""
    _, headers = link_only
    response = client.post("/api/broadcast/sessions", headers=headers,
                           json={"campaign_name": "crafted", "target_mode": "all"})
    assert response.status_code == 403


def test_a_blank_store_scope_does_not_bypass_the_missing_permission(client, owner, link_only):
    """Blank Scope means unrestricted. It must not mean 'unrestricted physically'."""
    user_id, headers = link_only
    # Explicitly clear any scope, so the account is unrestricted by scope alone.
    assert client.put(f"/api/users/{user_id}/store-scope", headers=owner,
                      json={"entries": []}).status_code == 200
    headers = sign_in(client, "linkonly")
    assert client.get("/api/broadcast/target-stores", headers=headers).status_code == 403
    assert client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "crafted", "target_mode": "all"}).status_code == 403


def test_the_refusal_leaks_no_store_names(client, link_only):
    """A 403 that lists what you cannot have is a directory."""
    _, headers = link_only
    body = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "crafted", "target_mode": "all"}).text
    assert "QAB1" not in body and "QA Bindapur" not in body
    assert "QAB2" not in body and "QA Rohini" not in body


# ===========================================================================
# The ordinary broadcaster is unchanged
# ===========================================================================

def test_a_physical_broadcaster_still_lists_targets_and_creates_a_session(client, owner, physical):
    """Existing operators must be exactly as capable as before the upgrade."""
    make_store(client, owner, "QAB9", "QA Dwarka")
    _, headers = physical
    listing = client.get("/api/broadcast/target-stores", headers=headers)
    assert listing.status_code == 200, listing.text
    assert {s["store_code"] for s in listing.json()["stores"]}

    created = client.post("/api/broadcast/sessions", headers=headers,
                          json={"campaign_name": "ordinary", "target_mode": "all"})
    assert created.status_code == 201, created.text


def test_store_scope_still_narrows_a_physical_broadcaster(client, owner, physical):
    """Physical permission answers WHETHER. Scope still answers WHICH."""
    allowed = make_store(client, owner, "QSC1", "QA Scoped")
    make_store(client, owner, "QSC2", "QA Unscoped")
    user_id, _ = physical
    assert set_scope(client, owner, user_id, [allowed]).status_code == 200
    headers = sign_in(client, "physical")

    codes = {s["store_code"] for s in
             client.get("/api/broadcast/target-stores", headers=headers).json()["stores"]}
    assert codes == {"QSC1"}, "the out-of-scope Store is not offered"


def test_a_scoped_broadcaster_cannot_name_an_out_of_scope_store(client, owner, physical):
    allowed = make_store(client, owner, "QSC1", "QA Scoped")
    forbidden = make_store(client, owner, "QSC2", "QA Unscoped")
    user_id, _ = physical
    assert set_scope(client, owner, user_id, [allowed]).status_code == 200
    headers = sign_in(client, "physical")

    response = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "crafted", "target_mode": "selected",
        "store_ids": [forbidden]})
    assert response.status_code == 403, response.text


def test_a_read_only_account_is_refused_as_it_always_was(client, owner):
    make_user(client, owner, "watcher", "VIEWER")
    headers = sign_in(client, "watcher")
    assert client.get("/api/broadcast/target-stores", headers=headers).status_code == 403
