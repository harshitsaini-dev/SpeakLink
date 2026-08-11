"""History-preserving irreversible permanent deletion of an HQ User.

Different from ``delete_user_if_unused`` (deletion_safety.py), which only
ever removes an account that never did anything. This is the harder case a
SUPER ADMIN genuinely needs: an account that STARTED BROADCASTS and appears
as the actor in audit history, removed permanently from operation while
every one of those historical rows stays exactly as readable as it was.

The model USED to mirror the Store tombstone - the row stayed and was
marked deleted - and that kept the username reserved for ever, which an
operator eventually hit as "the username 'admin' is already in use" for an
account they had deleted months earlier.

The row is now really deleted. History survives because each broadcast keeps
an immutable snapshot of who ran it and the pointer is nulled, rather than
because a hidden row is propping the reference up.

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

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "receiver_enrollment_api",
                               "deletion_safety", "user_deletion")]:
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


def give_user_history(client, headers, store_id):
    """Start and stop a broadcast so this account is the actor in history."""
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": "history maker", "target_mode": "selected",
        "store_ids": [store_id]})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def delete_user(client, headers, user_id, *, confirm, acknowledged=True):
    return client.post(f"/api/users/{user_id}/delete-permanently", headers=headers,
                       json={"confirm": confirm, "acknowledged": acknowledged})


def first_store_id(client, headers):
    return client.get("/api/stores", headers=headers).json()[0]["id"]


# ===========================================================================
# History no longer blocks the delete
# ===========================================================================
def test_a_user_with_broadcast_history_can_be_permanently_deleted(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    user_id = make_user(client, owner, "caster", "BROADCASTER")
    caster = sign_in(client, "caster")
    give_user_history(client, caster, store_id)

    resp = delete_user(client, owner, user_id, confirm="caster")
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "caster"


def test_broadcast_history_started_by_a_deleted_user_remains_readable(client):
    owner = sign_in(client)
    store_id = first_store_id(client, owner)
    user_id = make_user(client, owner, "caster", "BROADCASTER")
    caster = sign_in(client, "caster")
    session_id = give_user_history(client, caster, store_id)

    delete_user(client, owner, user_id, confirm="caster")

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=owner)
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == session_id

    from sqlalchemy import text
    with client.server_module.engine.connect() as c:
        row = c.execute(
            text("SELECT started_by, started_by_username FROM broadcast_sessions "
                 "WHERE id = :i"), {"i": session_id}).first()
    # The POINTER is cleared - a dangling id would be reissued to the next
    # account created, handing them somebody else's broadcast.
    assert row.started_by is None
    # The IDENTITY survives as a snapshot, which is what keeps history readable.
    assert row.started_by_username == "caster"


# ===========================================================================
# Operational disappearance and login refusal
# ===========================================================================
def test_a_deleted_user_is_hidden_from_the_normal_user_list(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "gone")
    before = client.get("/api/users", headers=owner).json()
    assert user_id in {u["id"] for u in before}

    delete_user(client, owner, user_id, confirm="gone")

    after = client.get("/api/users", headers=owner).json()
    assert user_id not in {u["id"] for u in after}


def test_a_deleted_user_cannot_sign_in(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "locked")
    delete_user(client, owner, user_id, confirm="locked")

    resp = client.post("/api/auth/login",
                       json={"username": "locked", "password": PASSWORD})
    assert resp.status_code in (401, 403)


def test_an_existing_token_stops_working_immediately(client):
    """session_version is compared on every request, so deletion must end
    live sessions rather than waiting for the token to expire."""
    owner = sign_in(client)
    user_id = make_user(client, owner, "livesession")
    victim = sign_in(client, "livesession")
    assert client.get("/api/auth/me", headers=victim).status_code == 200

    delete_user(client, owner, user_id, confirm="livesession")

    assert client.get("/api/auth/me", headers=victim).status_code == 401


def test_a_deleted_user_cannot_be_restored(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "norestore")
    delete_user(client, owner, user_id, confirm="norestore")

    # 404 rather than 409: there is no row left to refuse a transition on.
    # That is the honest answer once deletion is real.
    assert client.post(f"/api/users/{user_id}/restore", headers=owner).status_code == 404
    assert client.post(f"/api/users/{user_id}/enable", headers=owner).status_code == 404


def test_a_deleted_username_becomes_reusable(client):
    """The defect this feature exists to fix.

    The old tombstone kept the row, so the UNIQUE index kept the name, so an
    account an operator had permanently deleted made its username unusable for
    ever. Deleting means the name is free again.
    """
    owner = sign_in(client)
    user_id = make_user(client, owner, "takenname")
    delete_user(client, owner, user_id, confirm="takenname")

    resp = client.post("/api/users", headers=owner, json={
        "username": "takenname", "display_name": "A Different Person",
        "role": "ADMIN", "password": PASSWORD})
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] != user_id, "the new account must be a new identity"


# ===========================================================================
# Safety rules
# ===========================================================================
def test_you_cannot_permanently_delete_your_own_account(client):
    owner = sign_in(client)
    me = client.get("/api/auth/me", headers=owner).json()
    resp = delete_user(client, owner, me["id"], confirm=me["username"])
    assert resp.status_code == 409


def test_the_last_owner_cannot_be_permanently_deleted(client):
    owner = sign_in(client)
    me = client.get("/api/auth/me", headers=owner).json()
    second = make_user(client, owner, "secondowner", "OWNER")
    second_headers = sign_in(client, "secondowner")

    # Deleting the founder while another OWNER exists is allowed...
    assert delete_user(client, second_headers, me["id"],
                       confirm=me["username"]).status_code == 200
    # ...but the survivor is now the last one and is protected.
    resp = delete_user(client, second_headers, second, confirm="secondowner")
    assert resp.status_code == 409


def test_typed_confirmation_must_match_and_acknowledgement_is_required(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "careful")

    assert delete_user(client, owner, user_id, confirm="WRONG").status_code == 409
    assert delete_user(client, owner, user_id, confirm="careful",
                       acknowledged=False).status_code == 400
    assert user_id in {u["id"] for u in client.get("/api/users", headers=owner).json()}


# ===========================================================================
# Permission matrix
# ===========================================================================
@pytest.mark.parametrize("role", ["ADMIN", "BROADCASTER", "VIEWER"])
def test_only_super_admin_may_permanently_delete_a_user(client, role):
    owner = sign_in(client)
    victim = make_user(client, owner, "victim")
    actor = make_user(client, owner, f"actor{role.lower()}", role)
    headers = sign_in(client, f"actor{role.lower()}")

    assert delete_user(client, headers, victim, confirm="victim").status_code == 403


# ===========================================================================
# Audit
# ===========================================================================
def test_the_deletion_is_audited_without_leaking_secrets(client):
    owner = sign_in(client)
    user_id = make_user(client, owner, "audited")
    delete_user(client, owner, user_id, confirm="audited")

    events = client.get(f"/api/users/{user_id}/deletion-events", headers=owner)
    assert events.status_code == 200, events.text
    rows = events.json()["events"]
    assert len(rows) == 1
    assert rows[0]["username"] == "audited"
    assert rows[0]["actor_user_id"] is not None
    for forbidden in ("password", "hash", "jwt", "bearer ", "secret"):
        assert forbidden not in events.text.lower()


# ===========================================================================
# Migration ordering
# ===========================================================================
def test_a_legacy_hq_users_table_can_still_be_upgraded(tmp_path):
    """Regression test for a real bug this feature caused.

    user_schema.ensure_user_auth_schema runs in a documented order, and step
    3 (migrate_legacy_roles) reads hq_users THROUGH THE ORM - so it SELECTs
    every column declared on the HQUser model. Declaring deleted_at and
    deleted_by on the model while creating them only in
    user_deletion.ensure_user_deletion_schema meant that on a legacy table
    the ORM asked for a column no earlier step had added, and the whole
    sequence failed with an OperationalError. Twenty-five tests went red at
    once, none of them in the code that had changed.

    The columns are now added by ensure_user_lifecycle_schema (step 2),
    before anything reads a row through the ORM. Any future column on the
    HQUser model has to do the same.
    """
    from sqlalchemy import create_engine
    from user_lifecycle import ensure_user_lifecycle_schema
    from user_schema import ensure_user_auth_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with engine.begin() as connection:
        # Exactly the shape a pre-lifecycle install has on disk.
        connection.exec_driver_sql(
            "CREATE TABLE hq_users ("
            " id INTEGER PRIMARY KEY,"
            " username VARCHAR(100) NOT NULL UNIQUE,"
            " password_hash VARCHAR(255) NOT NULL,"
            " role VARCHAR(50) NOT NULL DEFAULT 'admin',"
            " is_active BOOLEAN NOT NULL DEFAULT 1,"
            " created_at DATETIME)")
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active) "
            "VALUES ('admin', '$2b$12$notarealhashvalue', 'admin', 1)")

    ensure_user_lifecycle_schema(engine)
    ensure_user_auth_schema(engine)   # must not raise

    from sqlalchemy import inspect
    columns = {c["name"] for c in inspect(engine).get_columns("hq_users")}
    assert {"deleted_at", "deleted_by"} <= columns
    assert {"lifecycle_state", "display_name", "session_version"} <= columns
