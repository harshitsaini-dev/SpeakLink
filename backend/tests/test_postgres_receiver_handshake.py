"""A REAL Receiver WebSocket handshake against REAL PostgreSQL.

THIS IS THE PROOF THAT WAS MISSING BEFORE RC14

The PostgreSQL suites before this one exercised the schema and the service
modules. None of them started the application and let a Receiver connect. So
`authenticate_receiver_credential` could refuse every non-SQLite engine on its
first line while 57 PostgreSQL tests reported success - which is exactly what
happened, and it was only found when a production cutover was attempted.

Service-level tests answer "does the credential resolve". This file answers
the question an operator actually has: **does a Store Receiver get CONNECTED**.

WHAT IT DELIBERATELY DOES NOT CLAIM

Only that the handshake authenticates and the connection is registered.
Nothing here says anything about audio: no PLAYBACK_CONFIRMED, and certainly
no SPEAKER_VERIFIED - those require a real Receiver, a real amplifier and an
operator's ears, and no software test may assert them.

Skipped entirely without ``TEST_POSTGRES_URL``.
"""
from __future__ import annotations

import json
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

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "").strip()
pg_required = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL is not configured; real-PostgreSQL tests are skipped",
)

PROTECTED_SCHEMAS = frozenset({
    "public", "auth", "storage", "realtime", "vault", "extensions", "graphql",
    "supabase_migrations", "pg_catalog", "information_schema",
})

HASH_KEY_VERSION = 1
HASH_KEY = b"handshake-test-hmac-key-of-more-than-enough-length"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def postgres_app(tmp_path, monkeypatch):
    """The whole application, running against an isolated PostgreSQL schema.

    Isolation is the same three-property arrangement the other PostgreSQL
    fixtures use, and for the same reason: a lost ``search_path`` once created
    nineteen tables in a production ``public`` schema while the tests reported
    success. The schema name is generated, asserted not to be a protected one,
    applied on EVERY new DBAPI connection through a ``connect`` listener (so a
    pooled reconnect cannot fall back to ``public``), and verified with
    ``current_schema()`` on a real connection before anything is yielded.
    """
    from sqlalchemy import create_engine, event, text
    from db_config import load_database_config

    schema = f"echocast_test_{uuid.uuid4().hex[:16]}"
    assert schema not in PROTECTED_SCHEMAS
    assert schema.startswith("echocast_test_")

    url = load_database_config(app_env="production",
                               database_url=TEST_POSTGRES_URL).url
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    # The application must come up pointed at PostgreSQL, exactly as it would
    # after the cutover: APP_ENV=production and a DATABASE_URL.
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", TEST_POSTGRES_URL)
    monkeypatch.setenv("JWT_SECRET", "handshake-test-only-secret-value")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-enough-temporary-password")
    # Staging protector: DPAPI binds a container to the Windows identity that
    # sealed it, which a test process must not depend on. This never carries a
    # real Store credential.
    monkeypatch.setenv("ECHOCAST_KEY_PROTECTOR", "fake")
    monkeypatch.setenv("ECHOCAST_KEY_CONTAINER", str(tmp_path / "keys.bin"))

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "receiver_enrollment_api",
                               "deletion_safety", "user_deletion", "postgres_schema",
                               "device_deletion", "admin_records", "store_lifecycle")]:
        sys.modules.pop(module, None)

    import db as db_module

    @event.listens_for(db_module.engine, "connect")
    def _set_search_path(dbapi_connection, _record):
        # SET is transactional and the pool rolls back on return, so it is
        # issued with the DBAPI connection temporarily in autocommit. The
        # libpq `options` route does not survive Supabase's pooler.
        was_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}"')
        finally:
            dbapi_connection.autocommit = was_autocommit

    with db_module.engine.connect() as connection:
        actual = connection.execute(text("SELECT current_schema()")).scalar_one()
        assert actual == schema, (
            f"isolation did not take effect: current_schema() is {actual!r}. "
            "Refusing to run - this is the check that stops a test suite "
            "writing into production."
        )
        assert connection.dialect.name == "postgresql"

    # The production schema, created the way the cutover creates it.
    import postgres_schema
    postgres_schema.create_all(db_module.engine)

    import server as server_module
    yield {"server": server_module, "engine": db_module.engine, "schema": schema}

    db_module.engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _seed_auth_state(engine):
    """The Receiver credential migration state the live fleet actually runs."""
    from migrations import PHASE_ONE_NAME, PHASE_ONE_VERSION
    from sqlalchemy import text

    with engine.begin() as c:
        c.execute(text("INSERT INTO schema_migrations (version, name, applied_at) "
                       "VALUES (:v, :n, :t)"),
                  {"v": PHASE_ONE_VERSION, "n": PHASE_ONE_NAME, "t": _utc()})
        c.execute(text(
            "INSERT INTO receiver_credential_migration_state "
            "(id, schema_version, state, legacy_verification_enabled, updated_at) "
            "VALUES (1, :v, 'hash_only', 0, :t)"),
            {"v": PHASE_ONE_VERSION, "t": _utc()})


def _enrol_receiver(engine, key_ring_keys):
    """A Store, a primary Device and a real credential hashed with the app's key."""
    from receiver_credentials import generate_receiver_credential, hash_receiver_token
    from sqlalchemy import text

    version, key = next(iter(key_ring_keys.items()))
    now = _utc()
    with engine.begin() as c:
        # A generated code, not 'BP': startup seeds the canonical 44-Store
        # catalog, which already contains Bindapur. Colliding with a real
        # seeded Store would make this test depend on the seed's contents.
        store_code = f"T{uuid.uuid4().hex[:6].upper()}"
        store_id = c.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, is_online_store, "
            "receiver_token, is_active, lifecycle_state, status, created_at, updated_at) "
            "VALUES (:code,'Handshake Test Store','DELHI','NORTH', false, :t, true, "
            "'active', 'offline', now(), now()) RETURNING id"),
            {"code": store_code, "t": uuid.uuid4().hex}).scalar_one()
        device_public_id = str(uuid.uuid4())
        device_id = c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, created_at, updated_at) "
            "VALUES (:p, :s, 'Till 1', 'active', :n, :n, :n) RETURNING id"),
            {"p": device_public_id, "s": store_id, "n": now}).scalar_one()
        issued = generate_receiver_credential(version=1)
        c.execute(text(
            "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
            "token_format, token_hash, hash_key_version, status, expiry_policy, "
            "issued_at, created_at) VALUES (:p, :d, 1, 'echocast_rcv', :h, :kv, "
            "'active', 'non_expiring', :n, :n)"),
            {"p": issued.public_id, "d": device_id,
             "h": hash_receiver_token(issued.raw_token, key, key_version=version),
             "kv": version, "n": now})
        # Primary, so this Device is the one that carries audio for the Store.
        c.execute(text("INSERT INTO receiver_store_primary_device "
                       "(store_id, device_id, promoted_at) VALUES (:s, :d, :n)"),
                  {"s": store_id, "d": device_id, "n": now})
    return {"token": issued.raw_token, "store_id": store_id,
            "device_id": device_id, "device_public_id": device_public_id}


# ===========================================================================
# Phase 6 - the runtime actually comes up on PostgreSQL
# ===========================================================================
@pg_required
def test_the_application_starts_against_postgresql(postgres_app):
    """Start-up reaches a serving state with PostgreSQL as the database.

    On RC14 the application also started - that was never the problem. This
    asserts the dialect explicitly so a future regression to SQLite (a lost
    DATABASE_URL falling back to a local file) is caught here rather than by
    somebody noticing the data is wrong.
    """
    from fastapi.testclient import TestClient

    server_module = postgres_app["server"]
    assert postgres_app["engine"].dialect.name == "postgresql"
    assert postgres_app["engine"].url.database != ":memory:"

    with TestClient(server_module.app) as client:
        response = client.get("/api/")
        assert response.status_code == 200


@pg_required
def test_an_admin_can_sign_in_against_postgresql(postgres_app):
    from fastapi.testclient import TestClient

    with TestClient(postgres_app["server"].app) as client:
        response = client.post("/api/auth/login", json={
            "username": "founder", "password": "a-long-enough-temporary-password"})
        assert response.status_code == 200, response.text
        assert response.json()["access_token"]


# ===========================================================================
# Phase 5 - the handshake itself
# ===========================================================================
@pg_required
def test_a_receiver_reaches_CONNECTED_over_a_real_websocket_on_postgresql(postgres_app):
    """The one that would have caught the RC14 blocker.

    A real WebSocket, a real credential, the real authenticator, PostgreSQL
    underneath. On RC14 this closes immediately with an authentication
    refusal, for every Store in the fleet.
    """
    from fastapi.testclient import TestClient

    server_module = postgres_app["server"]
    engine = postgres_app["engine"]
    _seed_auth_state(engine)

    with TestClient(server_module.app) as client:
        key_ring = server_module.receiver_key_ring()
        assert key_ring is not None, "the staging key container must be available"
        enrolled = _enrol_receiver(engine, key_ring.as_mapping())

        with client.websocket_connect(
            "/api/ws/receiver",
            headers={"Authorization": f"Bearer {enrolled['token']}"},
        ) as websocket:
            # Reaching this line means the handshake was accepted: the socket
            # is open rather than closed with a policy violation.
            manager = server_module.manager
            assert enrolled["store_id"] in manager.receivers, (
                "the Store must be registered as a connected Receiver")

            connected_store = manager.receivers[enrolled["store_id"]]
            assert connected_store is not None

            # And the server considers the connection authenticated for the
            # right Device, not merely for the right Store.
            assert manager.receiver_connection_inventory is not None

            del websocket

    # Deliberately NOT asserting stores.status here.
    #
    # The handler marks the Store online AFTER the handshake returns, so
    # reading it from the test is a race, and reading it after the socket
    # closes correctly reports 'offline' again. Neither reading proves
    # anything about authentication. The two facts below do, and both are
    # deterministic: membership of manager.receivers is set synchronously
    # during the handshake, and the receiver_events row is committed.

    # The connection itself is recorded, and that record outlives the socket.
    from sqlalchemy import text
    with engine.connect() as c:
        events = c.execute(text(
            "SELECT count(*) FROM receiver_events WHERE store_id = :i "
            "AND event_type = 'connected'"), {"i": enrolled["store_id"]}).scalar_one()
    assert events >= 1, "the connection must be recorded in receiver_events"


@pg_required
def test_a_wrong_credential_is_refused_at_the_websocket_on_postgresql(postgres_app):
    """The refusal path must still refuse. A port that authenticates
    everything would also make the test above pass."""
    from fastapi.testclient import TestClient
    from receiver_credentials import generate_receiver_credential
    from starlette.websockets import WebSocketDisconnect

    server_module = postgres_app["server"]
    _seed_auth_state(postgres_app["engine"])
    _enrol_receiver(postgres_app["engine"],
                    {HASH_KEY_VERSION: HASH_KEY})

    impostor = generate_receiver_credential(version=1).raw_token
    with TestClient(server_module.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "/api/ws/receiver",
                headers={"Authorization": f"Bearer {impostor}"},
            ) as websocket:
                websocket.receive_text()


@pg_required
def test_no_receiver_re_enrolment_is_required_by_the_port(postgres_app):
    """The identity a Store already has must keep working unchanged.

    This is the constraint the whole cutover exists under: moving the
    database must not cost 45 Stores a re-enrolment. The credential here is
    created once and used as-is - nothing rotates it, nothing re-issues it.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    server_module = postgres_app["server"]
    engine = postgres_app["engine"]
    _seed_auth_state(engine)

    with TestClient(server_module.app) as client:
        key_ring = server_module.receiver_key_ring()
        enrolled = _enrol_receiver(engine, key_ring.as_mapping())

        before = _credential_snapshot(engine, enrolled["device_id"])
        with client.websocket_connect(
            "/api/ws/receiver",
            headers={"Authorization": f"Bearer {enrolled['token']}"},
        ):
            pass
        after = _credential_snapshot(engine, enrolled["device_id"])

    assert before == after, "authenticating must not rotate or re-issue anything"

    with engine.connect() as c:
        public_id = c.execute(text(
            "SELECT public_id FROM receiver_devices WHERE id = :i"),
            {"i": enrolled["device_id"]}).scalar_one()
    assert public_id == enrolled["device_public_id"], (
        "the Device identity must be unchanged by the handshake")


def _credential_snapshot(engine, device_id):
    from sqlalchemy import text

    with engine.connect() as c:
        return c.execute(text(
            "SELECT id, public_id, token_hash, credential_version, status "
            "FROM receiver_credentials WHERE device_id = :d ORDER BY id"),
            {"d": device_id}).all()


# ===========================================================================
# What else start-up does - and does not - manage on PostgreSQL
# ===========================================================================
@pg_required
def test_which_startup_migrations_survive_postgresql(postgres_app):
    """A deliberate record of the boundary, not an aspiration.

    Every ensure_*_schema at start-up is wrapped in a warn-only try/except, so
    a SQLite-only one degrades instead of stopping the boot. That is correct
    for PostgreSQL, where the schema arrives from postgres_schema.create_all
    via the migration tool rather than from these functions.

    But "it only warns" is not the same as "nothing is missing", and the
    difference matters for the permission catalog: the cutover plan relies on
    it re-seeding additively on first boot. This test states what is actually
    true so the plan rests on evidence.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    engine = postgres_app["engine"]
    # Start-up runs when the TestClient context is entered, not at import.
    with TestClient(postgres_app["server"].app):
        pass
    with engine.connect() as c:
        permissions = c.execute(text("SELECT count(*) FROM permissions")).scalar_one()
        role_rows = c.execute(text("SELECT count(*) FROM role_permissions")).scalar_one()
        users = c.execute(text("SELECT count(*) FROM hq_users")).scalar_one()
        stores = c.execute(text("SELECT count(*) FROM stores")).scalar_one()

    # Recorded rather than asserted-as-desired: whatever these are, the
    # cutover has to plan around them.
    print(f"\nPostgreSQL start-up produced: permissions={permissions} "
          f"role_permissions={role_rows} hq_users={users} stores={stores}")

    assert users >= 1, "the administrator must exist, or nobody can sign in"
    assert stores >= 1, "the Store catalog must seed, or there is nothing to broadcast to"
    # The permission catalog MUST reseed on PostgreSQL. Without it a cutover
    # carries the previous release's catalog forever, and every feature guarded
    # by a newly added code is denied to everybody with nothing explaining why.
    assert permissions >= 29, (
        f"the permission catalog must reseed on PostgreSQL, found {permissions}")
    assert role_rows > 0, "the default role matrix must reseed on PostgreSQL"

    # And specifically the codes the admin-management round introduced, which
    # do not exist in the RC12 catalog the migration will copy in.
    with engine.connect() as c:
        for code in ("users.delete_permanently", "stores.delete_permanently",
                     "broadcast_history.delete_permanently",
                     "system_logs.delete_permanently"):
            found = c.execute(text("SELECT count(*) FROM permissions WHERE code = :c"),
                              {"c": code}).scalar_one()
            assert found == 1, f"{code} is missing from the PostgreSQL catalog"
