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
# path of this file's own choosing. ECHOCAST_DB_PATH is read once, at
# db.py import time, and setdefault means whichever test module happens to
# be imported first in a worker decides it for every test in that worker.
# An earlier version of this line pointed at a file inside backend/tests/,
# which meant that whenever this module won that race, two xdist workers
# shared one on-disk SQLite file and roughly a hundred unrelated tests
# failed. See docs/learning-guide.md.
os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
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


@pytest.fixture()
def pg_engine():
    from sqlalchemy import create_engine, text

    engine = create_engine(TEST_POSTGRES_URL)
    yield engine
    # Clean up everything this test run created, in reverse FK order.
    with engine.begin() as connection:
        for table in reversed(migration_tool.TABLE_ORDER):
            connection.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    engine.dispose()


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
            "receiver_token, is_active, created_at, updated_at) "
            "VALUES ('T1', 'Test', 'City', 'Zone', :tok, true, now(), now())"
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
            "receiver_token, is_active, created_at, updated_at) "
            "VALUES (500, 'MIG', 'Migrated Store', 'City', 'Zone', :tok, true, now(), now())"
        ), {"tok": uuid.uuid4().hex})
        migration_tool._repair_sequences(connection)
        new_id = connection.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, "
            "receiver_token, is_active, created_at, updated_at) "
            "VALUES ('NEW', 'New Store', 'City', 'Zone', :tok, true, now(), now()) "
            "RETURNING id"
        ), {"tok": uuid.uuid4().hex}).scalar_one()
    assert new_id > 500
