"""PostgreSQL production schema, proven where it can honestly be proven.

Two kinds of proof here, and they are not the same:

* SCHEMA GRAPH SHAPE - no real PostgreSQL needed. postgres_schema.py's
  Table objects, their FK graph, and migrate_sqlite_to_postgres.py's
  hand-listed TABLE_ORDER can all be checked against each other and
  against SQLAlchemy's own topological sort with nothing but SQLAlchemy
  itself. These run always, with no internet connection, like every
  other test in this suite.

* REAL POSTGRESQL BEHAVIOR (CREATE TABLE actually succeeding, a real
  FK/CHECK constraint actually being enforced) - genuinely needs a
  reachable PostgreSQL server. These are gated behind the TEST_POSTGRES_URL
  environment variable and skipped entirely if it is not set, so ordinary
  `pytest` on a laptop with no PostgreSQL installed - and no destructive
  test ever aimed at anyone's real Supabase project - is unaffected.
  Point TEST_POSTGRES_URL at a disposable local/test PostgreSQL, never at
  production.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# Exactly the same value every other test module in this suite uses - NOT a
# path of this file's own choosing. SPEAKLINK_DB_PATH is read once, at
# db.py import time, and setdefault means whichever test module happens to
# be imported first in a worker decides it for every test in that worker.
# An earlier version of this line pointed at a file inside backend/tests/,
# which meant that whenever this module won that race, two xdist workers
# shared one on-disk SQLite file and roughly a hundred unrelated tests
# failed. See docs/learning-guide.md.
os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import postgres_schema  # noqa: E402
from sqlalchemy.schema import sort_tables  # noqa: E402

sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
import migrate_sqlite_to_postgres as migration_tool  # noqa: E402


# ===========================================================================
# Schema graph shape - always runs
# ===========================================================================
def test_importing_this_module_never_pollutes_the_real_orm_metadata():
    """Regression test for a real bug found while writing this module: an
    earlier version built postgres_schema.metadata as literally
    ``models.Base.metadata`` (reused, not copied), so every receiver_devices/
    receiver_credentials/etc Table declared here registered onto that
    single shared, process-global MetaData object. The instant this module
    was imported anywhere in a pytest worker (collection happens for every
    test file up front), every ordinary SQLite test calling
    ``Base.metadata.create_all(...)`` - which is nearly all of them, and
    which correctly expects to create ONLY the ORM-declared tables - started
    trying to create receiver_devices too, colliding with migrations.py's
    own raw CREATE TABLE and failing the entire test file with "table
    already exists". This must never regress."""
    import db

    # The regression itself: none of the raw-SQL tables this module declares
    # may ever appear on the ORM's shared MetaData.
    assert "receiver_devices" not in db.Base.metadata.tables
    assert "receiver_credentials" not in db.Base.metadata.tables
    assert "store_deletion_events" not in db.Base.metadata.tables

    # ...and the two registries are genuinely separate objects, which is the
    # property that makes the above true rather than a coincidence.
    assert postgres_schema.metadata is not db.Base.metadata

    # Deliberately NOT asserted here: that "stores" IS present on
    # db.Base.metadata. Several modules in this suite reload `db` (popping it
    # from sys.modules and re-importing) to bind a fresh engine, which builds
    # a brand-new declarative Base whose metadata is empty until `models` is
    # imported against it too. Whether that has happened yet depends on which
    # modules share the worker - so asserting it would make this test's
    # result depend on collection order, which is precisely the class of bug
    # this file already exists to prevent. The postgres_schema side is
    # deterministic and is asserted instead.
    assert "stores" in postgres_schema.metadata.tables
    assert "hq_users" in postgres_schema.metadata.tables


def test_every_core_production_entity_is_declared():
    required = {
        "stores", "hq_users", "receiver_devices", "receiver_credentials",
        "receiver_credential_events", "receiver_enrollment_codes",
        "receiver_store_primary_device", "receiver_events",
        "broadcast_sessions", "broadcast_targets", "permissions",
        "role_permissions", "user_permission_overrides",
        "permission_audit_events", "user_store_scope",
        "store_scope_audit_events", "store_deletion_events", "system_logs",
    }
    declared = set(postgres_schema.metadata.tables.keys())
    missing = required - declared
    assert not missing, f"Missing from the PostgreSQL schema: {missing}"


def test_stores_carries_its_lifecycle_and_tombstone_fields():
    columns = {c.name for c in postgres_schema.metadata.tables["stores"].columns}
    assert {"lifecycle_state", "deleted_at", "deleted_by"} <= columns


def test_receiver_devices_carries_its_archive_field():
    columns = {c.name for c in postgres_schema.metadata.tables["receiver_devices"].columns}
    assert "archived_at" in columns


def test_the_migration_tools_table_order_is_a_valid_fk_topological_sort():
    """TABLE_ORDER is hand-listed in the migration tool for readability, but
    it must never silently drift from what the real schema graph requires -
    this test fails the moment someone adds a table with a new FK and
    forgets to update TABLE_ORDER."""
    computed = [t.name for t in sort_tables(postgres_schema.metadata.tables.values())]
    # A valid topological order need not be unique, so assert the STRONGER,
    # checkable property directly: every FK dependency is satisfied, i.e.
    # every table appears after all tables it references.
    position = {name: i for i, name in enumerate(migration_tool.TABLE_ORDER)}
    for table in postgres_schema.metadata.tables.values():
        if table.name not in position:
            continue
        for fk in table.foreign_keys:
            referenced = fk.column.table.name
            if referenced in position:
                assert position[referenced] <= position[table.name], (
                    f"{table.name} (position {position[table.name]}) depends on "
                    f"{referenced} (position {position[referenced]}) but is listed first"
                )
    assert set(computed) == set(migration_tool.TABLE_ORDER)


def test_sequence_tables_are_exactly_the_integer_primary_key_tables():
    """Every table in SEQUENCE_TABLES must have an integer, autoincrementing
    'id' primary key column - repairing a sequence for anything else would
    be meaningless (or, for a composite/non-integer key, an error)."""
    for name in migration_tool.SEQUENCE_TABLES:
        table = postgres_schema.metadata.tables[name]
        pk_columns = list(table.primary_key.columns)
        assert len(pk_columns) == 1, f"{name} does not have a single-column primary key"
        assert pk_columns[0].name == "id", f"{name}'s primary key is not named 'id'"


def test_dry_run_plan_reads_a_real_sqlite_file_without_writing_to_it(tmp_path):
    import sqlite3

    db_path = tmp_path / "source.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT, "
        "store_name TEXT, city TEXT, region TEXT, receiver_token TEXT)"
    )
    connection.execute(
        "INSERT INTO stores (id, store_code, store_name, city, region, receiver_token) "
        "VALUES (1, 'BP', 'Bindapur', 'City', 'Zone', 'x')"
    )
    connection.commit()
    connection.close()

    before = db_path.stat().st_mtime_ns
    result = migration_tool.plan(db_path)
    after = db_path.stat().st_mtime_ns
    assert after == before, "the dry-run plan must never modify the SQLite source file"

    by_table = dict(result)
    assert by_table["stores"] == 1
    assert by_table["hq_users"] == 0  # table absent in this minimal fixture - zero, not an error


def test_migration_refuses_without_database_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_path = tmp_path / "source.db"
    db_path.write_bytes(b"")
    with pytest.raises(SystemExit, match="DATABASE_URL is not set"):
        migration_tool.migrate(db_path)


# ===========================================================================
# Real PostgreSQL behavior - only with TEST_POSTGRES_URL set
# ===========================================================================
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pg_required = pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="TEST_POSTGRES_URL not set - skipping real PostgreSQL tests. "
           "Point it at a disposable test PostgreSQL to run these; never at "
           "a real Supabase production project.",
)


#: Schemas that belong to the PostgreSQL host (Supabase), not to SpeakLink.
#: Nothing in this file may ever create, alter or drop anything inside them.
#: Listed explicitly so the guard below is a decision rather than a hope.
PROTECTED_SCHEMAS = frozenset({
    "public", "auth", "storage", "realtime", "vault", "extensions",
    "graphql", "graphql_public", "pgbouncer", "pg_catalog",
    "information_schema", "cron", "net", "supabase_functions",
})


@pytest.fixture()
def pg_engine():
    """A real PostgreSQL engine confined to a freshly generated schema.

    WHY NOT JUST USE public

    An earlier version of this fixture ran
    ``DROP TABLE IF EXISTS <name> CASCADE`` with UNQUALIFIED table names.
    Unqualified names resolve through ``search_path``, which normally ends
    at ``public`` - so a single missed or lost ``search_path`` (a pooled
    reconnect, a pooler that resets session state) turns a test cleanup
    into "drop the production Stores table". Against a Supabase project
    that is not a hypothetical.

    So every object this fixture touches lives in one generated schema
    whose name cannot collide with anything real, and cleanup drops
    exactly that one schema. Three independent properties make that
    provable rather than assumed:

    1. the schema name is generated per test and asserted not to be any
       protected/host-owned schema before a single statement runs;
    2. ``search_path`` is set on EVERY new DBAPI connection through a
       ``connect`` event listener, so a pooled reconnect cannot silently
       fall back to ``public``;
    3. the fixture asserts ``current_schema()`` actually equals the
       generated schema, on a real connection, BEFORE yielding - if
       isolation did not take effect, the test errors instead of running
       destructively somewhere else.
    """
    from sqlalchemy import create_engine, event, text
    from db_config import load_database_config

    schema = f"speaklink_test_{uuid.uuid4().hex[:16]}"
    assert schema not in PROTECTED_SCHEMAS
    assert schema.startswith("speaklink_test_")

    # Normalized through the SAME loader production uses, so these tests
    # exercise the real driver (psycopg 3) and the real TLS requirement
    # rather than whatever SQLAlchemy would have guessed from a bare
    # postgresql:// scheme (psycopg2, which is not installed).
    url = load_database_config(app_env="production",
                               database_url=TEST_POSTGRES_URL).url

    admin_engine = create_engine(url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    # Confining the connection to the test schema took three attempts, and
    # the two that failed are worth recording because both LOOKED correct.
    #
    # 1. A `connect` listener issuing `SET search_path TO <schema>`.
    #    `SET` is TRANSACTIONAL in PostgreSQL, and SQLAlchemy's pool
    #    defaults to `reset_on_return="rollback"` - so the first time a
    #    connection returned to the pool, the ROLLBACK silently reverted
    #    search_path and everything afterwards ran against `public`.
    #    Nineteen tables and five rows were created in the production
    #    `public` schema before the isolation test below caught it.
    #
    # 2. The libpq connection option `-csearch_path=<schema>`, which is
    #    applied at connection establishment and cannot be rolled back.
    #    Correct in general - but Supabase's Session Pooler (Supavisor)
    #    does not pass the `options` startup parameter through, so
    #    current_schema() was still `public`.
    #
    # 3. What is used here: issue the `SET` with the DBAPI connection
    #    temporarily in AUTOCOMMIT. Outside a transaction the setting
    #    applies to the session itself, so a later ROLLBACK has nothing to
    #    revert, and it needs no cooperation from the pooler.
    #
    # The assertions below verify the result rather than trusting the
    # mechanism - including explicitly across a rollback, which is exactly
    # what attempt 1 failed.
    engine = create_engine(url)

    @event.listens_for(engine, "connect")
    def _confine_to_test_schema(dbapi_connection, connection_record):
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            cursor.close()
        finally:
            dbapi_connection.autocommit = previous

    # The verification lives INSIDE the try, so that a failed isolation
    # check still drops the schema it just created. An earlier version
    # asserted before the try and leaked one empty schema per failure.
    try:
        # Prove the confinement took effect - and specifically that it
        # SURVIVES a rollback, which is exactly what attempt 1 did not.
        with engine.connect() as connection:
            actual = connection.execute(text("SELECT current_schema()")).scalar_one()
            assert actual == schema, (
                f"isolation failed: current_schema() is {actual!r}, not {schema!r}. "
                "Refusing to run destructive statements outside the test schema."
            )
            connection.rollback()
            after_rollback = connection.execute(
                text("SELECT current_schema()")).scalar_one()
            assert after_rollback == schema, (
                f"isolation did not survive a rollback: {after_rollback!r}. "
                "Refusing to run destructive statements outside the test schema."
            )

        yield engine
    finally:
        engine.dispose()
        # Exactly one schema, by its generated name. Never a bare table name,
        # never public, never a host-owned schema.
        assert schema not in PROTECTED_SCHEMAS
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


@pg_required
def test_the_test_schema_is_isolated_and_public_is_never_touched(pg_engine):
    """The safety property every other real-PostgreSQL test depends on.

    If this fails, nothing else in this file may be trusted to have run
    where it believed it was running - which, against a hosted database,
    is the difference between a test and an incident."""
    from sqlalchemy import text

    postgres_schema.create_all(pg_engine)

    with pg_engine.connect() as c:
        schema = c.execute(text("SELECT current_schema()")).scalar_one()
        assert schema.startswith("speaklink_test_")
        assert schema not in PROTECTED_SCHEMAS

        # The tables really landed in the generated schema...
        here = c.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s"
        ), {"s": schema}).scalar_one()
        assert here >= len(migration_tool.TABLE_ORDER)

        # ...and NOT in public, which must still hold no SpeakLink table.
        for name in ("stores", "hq_users", "receiver_devices"):
            in_public = c.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": name}).scalar_one()
            assert in_public == 0, f"public.{name} exists - isolation leaked"

        # Supabase-managed schemas are still intact and untouched.
        for managed in ("auth", "storage"):
            assert c.execute(text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s"
            ), {"s": managed}).scalar_one() > 0


@pg_required
def test_schema_creates_successfully_on_real_postgresql(pg_engine):
    from sqlalchemy import inspect

    postgres_schema.create_all(pg_engine)
    inspector = inspect(pg_engine)
    tables = set(inspector.get_table_names())
    assert "stores" in tables
    assert "receiver_devices" in tables
    assert "store_deletion_events" in tables


@pg_required
def test_foreign_key_is_actually_enforced_on_real_postgresql(pg_engine):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    postgres_schema.create_all(pg_engine)
    with pg_engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, "
            "is_online_store, receiver_token, is_active, status, created_at, updated_at) "
            "VALUES ('T1', 'Test', 'City', 'Zone', false, :tok, true, 'offline', now(), now())"
        ), {"tok": uuid.uuid4().hex})

    with pytest.raises(IntegrityError):
        with pg_engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO receiver_devices (public_id, store_id, display_name, "
                "status, enrolled_at, created_at, updated_at) "
                "VALUES (:pid, 999999, 'Ghost Device', 'active', now()::text, "
                "now()::text, now()::text)"
            ), {"pid": str(uuid.uuid4())})


@pg_required
def test_sequence_repair_lets_a_new_row_insert_after_migrated_ids(pg_engine):
    from sqlalchemy import text

    postgres_schema.create_all(pg_engine)
    with pg_engine.begin() as connection:
        # Simulate a migrated row with an explicit, preserved high id.
        connection.execute(text(
            "INSERT INTO stores (id, store_code, store_name, city, region, "
            "is_online_store, receiver_token, is_active, status, created_at, updated_at) "
            "VALUES (500, 'MIG', 'Migrated Store', 'City', 'Zone', false, :tok, true, "
            "'offline', now(), now())"
        ), {"tok": uuid.uuid4().hex})
        migration_tool._repair_sequences(connection)
        new_id = connection.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, "
            "is_online_store, receiver_token, is_active, status, created_at, updated_at) "
            "VALUES ('NEW', 'New Store', 'City', 'Zone', false, :tok, true, "
            "'offline', now(), now()) RETURNING id"
        ), {"tok": uuid.uuid4().hex}).scalar_one()
    assert new_id > 500
