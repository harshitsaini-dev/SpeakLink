"""What "Online Stores Only" targets, and when it decides.

THE DEFECT THIS FILE EXISTS FOR

The mode filtered on ``Store.is_online_store`` - the column Store Management
edits with a checkbox labelled Online / Physical. That is an e-commerce
classification which defaults to False; it says nothing about whether a Receiver
is reachable. So the mode targeted the e-commerce stores and excluded every
physical shop whose Receiver was connected, and an operator looking at a console
that said BP ONLINE started a broadcast with zero targets.

Connectivity comes from the live Receiver connection inventory, the same source
the target list already uses to paint each row. These tests drive that inventory
directly, because it is the authority.
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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("ECHOCAST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "web_rooms", "web_participant_runtime",
                               "active_broadcast_management")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username: str = "founder"):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client)


def make_store(client, headers, code, name, *, is_online_store=False,
               city="TESTVILLE", region="TEST ZONE"):
    """Create a Store, or adopt the seeded one that already has this code.

    A fresh database is seeded with the canonical Store catalogue, which
    already contains BP. Reusing that row is closer to the real fleet than
    inventing a second Store with a code the product would refuse.
    """
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": name, "city": city, "region": region,
        "is_online_store": is_online_store})
    if response.status_code == 201:
        return response.json()["id"]
    assert response.status_code == 409, response.text

    server = client.server_module
    with server.SessionLocal() as db:
        existing = db.query(server.Store).filter(
            server.Store.store_code == code).first()
        assert existing is not None, code
        # Match what the caller asked for, so the classification under test is
        # the one this test set.
        existing.is_online_store = is_online_store
        existing.region = region
        existing.city = city
        db.commit()
        return existing.id


def other_store_ids(client, keep):
    """Every other authorised Store. Used to prove exclusions are real."""
    server = client.server_module
    with server.SessionLocal() as db:
        return {row.id for row in db.query(server.Store).filter(
            server.Store.is_active.is_(True)).all()} - set(keep)


def set_connected(client, store_ids):
    """Drive the live Receiver connection inventory - the online authority."""
    manager = client.server_module.manager
    manager.online_store_ids = lambda: set(store_ids)


def create_online_session(client, headers, campaign="Online only"):
    return client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": campaign, "target_mode": "online_only"})


def targets_of(client, sid):
    server = client.server_module
    with server.SessionLocal() as db:
        return {row.store_id for row in db.query(server.BroadcastTarget).filter(
            server.BroadcastTarget.session_id == sid).all()}


# ===========================================================================
# Resolution comes from connectivity
# ===========================================================================

def test_the_online_mode_targets_the_connected_store_not_the_ecommerce_one(client, owner):
    """The exact defect: BP is a PHYSICAL shop with a connected Receiver."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    # An e-commerce Store, which is what the old filter would have picked.
    web = make_store(client, owner, "WEB", "Web Store", is_online_store=True)
    set_connected(client, {bp})

    created = create_online_session(client, owner)
    assert created.status_code == 201, created.text
    sid = created.json()["id"]

    assert targets_of(client, sid) == {bp}
    assert rg not in targets_of(client, sid)
    assert web not in targets_of(client, sid), \
        "the Online / Physical flag is not connectivity"


def test_every_connected_store_is_targeted(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    make_store(client, owner, "DW", "Dwarka")
    set_connected(client, {bp, rg})

    sid = create_online_session(client, owner).json()["id"]
    assert targets_of(client, sid) == {bp, rg}


def test_an_offline_store_is_excluded(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp})
    sid = create_online_session(client, owner).json()["id"]
    assert targets_of(client, sid) == {bp}, "only the connected Store"


def test_zero_connected_stores_refuses_rather_than_pretending(client, owner):
    """Never a live physical broadcast with nothing on the other end."""
    make_store(client, owner, "BP", "Bindapur")
    set_connected(client, set())

    refused = create_online_session(client, owner)
    assert refused.status_code == 409, refused.text
    assert "currently online" in refused.json()["detail"].lower()
    # And emphatically not a silent fallback to every Store.
    assert "all" not in refused.json()["detail"].lower()


# ===========================================================================
# Scope and permission still apply
# ===========================================================================

def test_a_connected_store_outside_scope_is_excluded(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    created = client.post("/api/users", headers=owner, json={
        "username": "scoped", "display_name": "Scoped",
        "role": "BROADCASTER", "password": PASSWORD})
    user_id = created.json()["id"]
    assert client.put(f"/api/users/{user_id}/store-scope", headers=owner, json={
        "entries": [{"scope_type": "STORE", "store_id": bp}]}).status_code == 200
    headers = sign_in(client, "scoped")

    set_connected(client, {bp, rg})
    sid = create_online_session(client, headers).json()["id"]
    assert targets_of(client, sid) == {bp}, "the out-of-scope Store is not targeted"


def test_without_physical_delivery_the_online_mode_is_refused(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    created = client.post("/api/users", headers=owner, json={
        "username": "linkonly", "display_name": "Link Only",
        "role": "BROADCASTER", "password": PASSWORD})
    user_id = created.json()["id"]
    assert client.put(f"/api/users/{user_id}/permissions", headers=owner, json={
        "changes": [{"code": "broadcast.store_delivery", "effect": "DENY"}]
    }).status_code == 200
    headers = sign_in(client, "linkonly")

    set_connected(client, {bp})
    assert create_online_session(client, headers).status_code == 403


# ===========================================================================
# Manual selection cannot reach into the automatic mode
# ===========================================================================

def test_selected_store_ids_do_not_constrain_the_online_mode(client, owner):
    """A stale draft selection must not narrow an automatic mode."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp, rg})

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Online only", "target_mode": "online_only",
        "store_ids": [bp]})            # left over from Selected mode
    assert created.status_code == 201
    assert targets_of(client, created.json()["id"]) == {bp, rg}


def test_crafted_store_ids_cannot_expand_the_online_mode(client, owner):
    """Nor widen it to a Store that is not connected."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp})

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Crafted", "target_mode": "online_only",
        "store_ids": [bp, rg]})
    assert created.status_code == 201
    assert targets_of(client, created.json()["id"]) == {bp}


# ===========================================================================
# Start revalidates, then freezes
# ===========================================================================

def test_a_store_that_connects_before_start_is_included(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp})
    sid = create_online_session(client, owner).json()["id"]
    assert targets_of(client, sid) == {bp}

    # RG comes back between configuring and starting.
    set_connected(client, {bp, rg})
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=owner).status_code == 200
    assert targets_of(client, sid) == {bp, rg}, "Start re-resolved connectivity"


def test_a_store_that_drops_before_start_is_excluded(client, owner):
    """A stale browser must not start a broadcast to a Receiver that has gone."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp, rg})
    sid = create_online_session(client, owner).json()["id"]
    assert targets_of(client, sid) == {bp, rg}

    set_connected(client, {bp})
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=owner).status_code == 200
    assert targets_of(client, sid) == {bp}


def test_everything_dropping_before_start_refuses(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    set_connected(client, {bp})
    sid = create_online_session(client, owner).json()["id"]

    set_connected(client, set())
    refused = client.post(f"/api/broadcast/sessions/{sid}/start", headers=owner)
    assert refused.status_code == 409
    assert "currently online" in refused.json()["detail"].lower()


def test_a_store_connecting_after_start_is_not_added(client, owner):
    """The set is frozen at Start. Joining mid-announcement is a decision."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp})
    sid = create_online_session(client, owner).json()["id"]
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=owner).status_code == 200

    set_connected(client, {bp, rg})
    assert targets_of(client, sid) == {bp}, "a heartbeat does not add a target"
    live = client.server_module.manager.broadcasts.get(sid)
    assert set(live.target_store_ids) == {bp}


def test_the_resolved_set_governs_rows_leases_and_prepare(client, owner):
    """Target rows, reservations and PREPARE must all describe one set."""
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, {bp, rg})
    sid = create_online_session(client, owner).json()["id"]

    # RG drops in the moment before Start.
    set_connected(client, {bp})
    server = client.server_module
    prepared: list[int] = []
    original = server.manager.send_to_receiver

    async def record(store_id, message):
        if message.get("type") == "prepare":
            prepared.append(store_id)
        return await original(store_id, message)

    server.manager.send_to_receiver = record
    try:
        assert client.post(f"/api/broadcast/sessions/{sid}/start",
                           headers=owner).status_code == 200
    finally:
        server.manager.send_to_receiver = original

    assert targets_of(client, sid) == {bp}
    with server.engine.connect() as connection:
        leased = {row[0] for row in connection.exec_driver_sql(
            "SELECT store_id FROM broadcast_store_leases WHERE session_id = ?",
            (sid,)).fetchall()}
    assert leased == {bp}, "no lease for a Store that dropped"
    assert rg not in prepared, "no PREPARE to a Store that dropped"

    live = server.manager.broadcasts.get(sid)
    assert set(live.target_store_ids) == {bp}


# ===========================================================================
# The other modes are untouched
# ===========================================================================

def test_selected_mode_still_targets_exactly_what_was_named(client, owner):
    bp = make_store(client, owner, "BP", "Bindapur")
    rg = make_store(client, owner, "RG", "Rohini Gardens")
    set_connected(client, set())          # connectivity is irrelevant here

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Selected", "target_mode": "selected",
        "store_ids": [bp, rg]})
    assert created.status_code == 201
    assert targets_of(client, created.json()["id"]) == {bp, rg}


def test_zone_mode_still_targets_the_whole_zone(client, owner):
    north_a = make_store(client, owner, "N1", "North One", region="NORTH")
    north_b = make_store(client, owner, "N2", "North Two", region="NORTH")
    make_store(client, owner, "S1", "South One", region="SOUTH")
    set_connected(client, {north_a})      # offline Stores are still Zone targets

    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Zone", "target_mode": "region", "region": "NORTH"})
    assert created.status_code == 201
    assert targets_of(client, created.json()["id"]) == {north_a, north_b}


def test_only_with_link_still_has_no_physical_targets(client, owner):
    make_store(client, owner, "BP", "Bindapur")
    set_connected(client, set())
    created = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Link", "target_mode": "only_with_link"})
    assert created.status_code == 201
    assert targets_of(client, created.json()["id"]) == set()


def test_starting_link_only_is_not_refused_for_having_no_online_store(client, owner):
    """The zero-online refusal belongs to the online mode alone."""
    make_store(client, owner, "BP", "Bindapur")
    set_connected(client, set())
    sid = client.post("/api/broadcast/sessions", headers=owner, json={
        "campaign_name": "Link", "target_mode": "only_with_link"}).json()["id"]
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=owner).status_code == 200
