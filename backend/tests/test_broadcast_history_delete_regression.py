"""Permanent deletion of Broadcast History that has actually been broadcast.

THE DEFECT THIS FILE EXISTS FOR

An operator selected completed sessions in Broadcast History, chose Permanently
Delete, and the browser said:

    No 'Access-Control-Allow-Origin' header is present on the requested
    resource.

which reads as a CORS misconfiguration and is not one. The real fault:
``broadcast_store_leases`` arrived with concurrent broadcasts carrying a
foreign key to ``broadcast_sessions``, and ``delete_sessions_permanently`` was
never taught about it. With ``PRAGMA foreign_keys=ON`` the DELETE raised
IntegrityError, the exception escaped the route, and Starlette's
ServerErrorMiddleware - which sits OUTSIDE CORSMiddleware - returned a 500
with no CORS headers on it.

WHY THE EXISTING TESTS MISSED IT

Every earlier test CREATED a session and deleted it. A session that was never
STARTED holds no lease, so it deletes cleanly. The bug needed a session that
had actually been on air, which is exactly what the operator had and what the
tests did not. Every deletion test here starts and stops its sessions first.

Two independent things are therefore asserted: that the delete works, and
that a server fault can never again arrive dressed as a transport problem.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text

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
URL = "/api/broadcast/history/delete-permanently"
DELETE_BODY = {"confirm": "DELETE", "acknowledged": True}
DEV_ORIGIN = "http://localhost:3000"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    # receiver_enrollment_api is deliberately NOT reloaded - doing so swaps its
    # EnrollmentRefused class and breaks the enrolment suite's except clauses.
    for module in [n for n in list(sys.modules) if n in (
            "server", "db", "models", "seed", "auth", "rbac", "user_lifecycle",
            "schemas", "permission_catalog", "admin_records", "user_deletion",
            "device_deletion", "broadcast_runtime", "broadcast_reservation",
            "active_broadcast_management")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        try:
            yield made
        finally:
            for sid in list(server_module.manager.broadcasts.active_session_ids()):
                asyncio.run(server_module.manager.broadcasts.end(sid))


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


def store_ids(client, headers):
    return [s["id"] for s in client.get("/api/stores", headers=headers).json()]


def broadcast_and_finish(client, headers, store_id, name="campaign"):
    """A session that was really ON AIR, so it holds lease rows.

    This is the whole point of the file: a created-but-never-started session
    does not reproduce the defect.
    """
    made = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected", "store_ids": [store_id]})
    assert made.status_code == 201, made.text
    sid = made.json()["id"]
    assert client.post(f"/api/broadcast/sessions/{sid}/start",
                       headers=headers).status_code == 200
    assert client.post(f"/api/broadcast/sessions/{sid}/stop",
                       headers=headers).status_code == 200
    return sid


def lease_count(client, session_id):
    with client.server_module.engine.connect() as c:
        return c.execute(
            text("SELECT COUNT(*) FROM broadcast_store_leases WHERE session_id=:i"),
            {"i": session_id}).scalar_one()


# ===========================================================================
# 1-4, 9, 10 - the delete itself
# ===========================================================================
def test_the_defect_reproduces_without_the_fix_conditions(client):
    """A guard on the PRECONDITION, so this suite cannot quietly stop testing
    the real thing. If a finished session ever stops holding leases, the rest
    of this file would pass for the wrong reason."""
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])
    assert lease_count(client, sid) >= 1, (
        "a finished session no longer holds lease rows - these tests would "
        "no longer reproduce the original defect")


def test_1_owner_may_permanently_delete_broadcast_sessions(client):
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0], "doomed")

    r = client.post(URL, headers=owner, json={"ids": [sid], **DELETE_BODY})

    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 1
    assert r.json()["failed"] == 0


def test_2_deleted_sessions_disappear_from_history(client):
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])

    client.post(URL, headers=owner, json={"ids": [sid], **DELETE_BODY})

    remaining = {s["id"] for s in client.get("/api/broadcast/history", headers=owner).json()}
    assert sid not in remaining


def test_3_the_administrative_audit_record_survives(client):
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])

    client.post(URL, headers=owner, json={"ids": [sid], **DELETE_BODY})

    with client.server_module.engine.connect() as c:
        rows = c.execute(text(
            "SELECT record_type, action, affected_count FROM admin_deletion_events "
            "WHERE record_type='broadcast_session' AND action='deleted'")).all()
    assert rows, "the deletion left no administrative audit record"
    assert rows[-1].affected_count == 1


def test_4_the_sessions_own_rows_go_with_it_and_nothing_else(client):
    """targets AND leases belong to the session. Stores and Users do not."""
    owner = sign_in(client)
    stores = store_ids(client, owner)
    doomed = broadcast_and_finish(client, owner, stores[0], "doomed")
    survivor = broadcast_and_finish(client, owner, stores[1], "survivor")

    engine = client.server_module.engine
    with engine.connect() as c:
        stores_before = c.execute(text("SELECT COUNT(*) FROM stores")).scalar_one()
        users_before = c.execute(text("SELECT COUNT(*) FROM hq_users")).scalar_one()

    client.post(URL, headers=owner, json={"ids": [doomed], **DELETE_BODY})

    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": doomed}).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_targets WHERE session_id=:i"),
                         {"i": doomed}).scalar_one() == 0
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_store_leases WHERE session_id=:i"),
                         {"i": doomed}).scalar_one() == 0
        # 10 - the other session is completely untouched, leases included.
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_sessions WHERE id=:i"),
                         {"i": survivor}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_targets WHERE session_id=:i"),
                         {"i": survivor}).scalar_one() == 1
        assert c.execute(text("SELECT COUNT(*) FROM broadcast_store_leases WHERE session_id=:i"),
                         {"i": survivor}).scalar_one() >= 1
        assert c.execute(text("SELECT COUNT(*) FROM stores")).scalar_one() == stores_before
        assert c.execute(text("SELECT COUNT(*) FROM hq_users")).scalar_one() == users_before


def test_9_the_database_is_still_sound_afterwards(client):
    owner = sign_in(client)
    stores = store_ids(client, owner)
    ids = [broadcast_and_finish(client, owner, stores[i], f"c{i}") for i in range(3)]

    r = client.post(URL, headers=owner, json={"ids": ids, **DELETE_BODY})
    assert r.status_code == 200, r.text
    assert r.json()["affected"] == 3

    with client.server_module.engine.connect() as c:
        assert c.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert c.execute(text("PRAGMA foreign_key_check")).all() == []
        # No orphaned lease rows anywhere.
        orphans = c.execute(text(
            "SELECT COUNT(*) FROM broadcast_store_leases l "
            "LEFT JOIN broadcast_sessions s ON s.id = l.session_id "
            "WHERE s.id IS NULL")).scalar_one()
    assert orphans == 0


def test_10_deleting_one_session_leaves_the_rest_of_history_alone(client):
    owner = sign_in(client)
    stores = store_ids(client, owner)
    keep_a = broadcast_and_finish(client, owner, stores[0], "keep-a")
    doomed = broadcast_and_finish(client, owner, stores[1], "doomed")
    keep_b = broadcast_and_finish(client, owner, stores[2], "keep-b")

    client.post(URL, headers=owner, json={"ids": [doomed], **DELETE_BODY})

    remaining = {s["id"] for s in client.get("/api/broadcast/history", headers=owner).json()}
    assert keep_a in remaining and keep_b in remaining
    assert doomed not in remaining


# ===========================================================================
# 5, 6 - authorization and input
# ===========================================================================
def test_5_the_wrong_permission_is_refused_with_403(client):
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])
    make_user(client, owner, "supervisor", role="ADMIN")   # holds archive, not delete

    r = client.post(URL, headers=sign_in(client, "supervisor"),
                    json={"ids": [sid], **DELETE_BODY})

    assert r.status_code == 403
    assert sid in {s["id"] for s in
                   client.get("/api/broadcast/history", headers=owner).json()}


@pytest.mark.parametrize("body", [
    {"ids": "not-a-list", **DELETE_BODY},
    {"ids": [{"nested": 1}], **DELETE_BODY},
    {"ids": ["abc"], **DELETE_BODY},
    {"mode": "filtered", "filters": "not-a-dict", **DELETE_BODY},
])
def test_6_malformed_input_is_a_safe_4xx_never_an_unhandled_500(client, body):
    owner = sign_in(client)
    r = client.post(URL, headers=owner, json=body)
    assert 400 <= r.status_code < 500, f"got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500


def test_6b_a_nonexistent_id_is_skipped_rather_than_failing(client):
    owner = sign_in(client)
    r = client.post(URL, headers=owner, json={"ids": [99999], **DELETE_BODY})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == 1 and r.json()["affected"] == 0


# ===========================================================================
# 7, 8 - the transport, and the symptom that misled the operator
# ===========================================================================
def test_7_a_valid_dev_origin_gets_cors_headers_on_a_real_delete(client):
    """LEGACY two-port development mode: React on 3000, API on 8000."""
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])

    r = client.post(URL, headers={**owner, "Origin": DEV_ORIGIN},
                    json={"ids": [sid], **DELETE_BODY})

    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN
    assert r.headers.get("access-control-allow-credentials") == "true"


def test_7b_the_preflight_is_answered(client):
    r = client.options(URL, headers={
        "Origin": DEV_ORIGIN,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert r.status_code in (200, 204), r.text
    assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN


def test_7c_an_unknown_origin_is_not_granted_access(client):
    """The fix must not have been "allow everything"."""
    r = client.options(URL, headers={
        "Origin": "http://evil.example",
        "Access-Control-Request-Method": "POST",
    })
    assert r.headers.get("access-control-allow-origin") != "http://evil.example"
    assert r.headers.get("access-control-allow-origin") != "*"


def test_8_an_endpoint_exception_becomes_a_json_500_with_cors_headers(client, monkeypatch):
    """The heart of the misdiagnosis.

    A route that raises must produce a JSON error the browser can read,
    carrying CORS headers, so the operator is told the server failed rather
    than being sent to investigate a transport problem that does not exist.
    """
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])

    import admin_records
    def explode(*_args, **_kwargs):
        raise RuntimeError("simulated internal failure with /a/path and a value")
    monkeypatch.setattr(admin_records, "delete_sessions_permanently", explode)
    monkeypatch.setattr(client.server_module, "delete_sessions_permanently", explode)

    r = client.post(URL, headers={**owner, "Origin": DEV_ORIGIN},
                    json={"ids": [sid], **DELETE_BODY})

    assert r.status_code == 500
    # The browser can read it, which is what makes the message honest.
    assert r.headers.get("access-control-allow-origin") == DEV_ORIGIN
    assert r.json()["detail"]["code"] == "INTERNAL_ERROR"
    # And it leaks nothing: no exception text, no path, no SQL.
    body = r.text.lower()
    for leak in ("simulated internal failure", "/a/path", "traceback",
                 "runtimeerror", "select ", "delete from"):
        assert leak not in body, f"{leak!r} reached the browser"


def test_8b_the_session_survives_a_failed_delete(client):
    """A failure must not half-delete. Nothing is reported as removed and
    nothing is.

    The substitution is restored by hand rather than with monkeypatch.undo():
    the `client` fixture takes the SAME function-scoped monkeypatch object, so
    undo() would also roll back SPEAKLINK_DB_PATH and point the next request at
    a different database.
    """
    owner = sign_in(client)
    sid = broadcast_and_finish(client, owner, store_ids(client, owner)[0])

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    original = client.server_module.delete_sessions_permanently
    client.server_module.delete_sessions_permanently = explode
    try:
        assert client.post(URL, headers=owner,
                           json={"ids": [sid], **DELETE_BODY}).status_code == 500
    finally:
        client.server_module.delete_sessions_permanently = original

    assert sid in {s["id"] for s in
                   client.get("/api/broadcast/history", headers=owner).json()}
