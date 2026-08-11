"""The guarded destination reset, tested for the refusals rather than the deletes.

A tool whose job is to delete production rows is judged by what it REFUSES to
do. The happy path is one statement per table; the value is entirely in the
gates in front of it. So most of this file is about the tool declining.

The reset itself is exercised against real PostgreSQL, inside the same
generated ``speaklink_test_*`` schema every other PostgreSQL test uses, and is
skipped without ``TEST_POSTGRES_URL``.
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
TOOLS_ROOT = REPOSITORY_ROOT / "tools"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT, TOOLS_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import reset_postgres_destination as reset_tool  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.test_postgres_schema import pg_engine, pg_required  # noqa: E402,F401


def _schema_of(engine) -> str:
    """The generated schema this engine is confined to.

    The tool is schema-qualified rather than search_path-dependent - that is
    the whole point of it - so the tests must name the schema too.
    """
    with engine.connect() as c:
        return c.execute(text("SELECT current_schema()")).scalar_one()


# ===========================================================================
# Fingerprint: the one check that cannot be automated away
# ===========================================================================
def test_the_fingerprint_hashes_the_full_username_not_the_bare_ref():
    """Pinned because getting this wrong reads as a mismatch on the RIGHT
    project, which would either stop a legitimate cutover or - far worse -
    tempt somebody to bypass the check."""
    import hashlib

    url = "postgresql://postgres.abcdefghijklmnop:pw@host.pooler.supabase.com:5432/postgres"
    expected = hashlib.sha256(b"postgres.abcdefghijklmnop").hexdigest()[:16]
    assert reset_tool.fingerprint_of(url) == expected

    bare_ref = hashlib.sha256(b"abcdefghijklmnop").hexdigest()[:16]
    assert reset_tool.fingerprint_of(url) != bare_ref


def test_a_fingerprint_mismatch_refuses_before_reading_anything(monkeypatch, capsys):
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.someproject:pw@host.pooler.supabase.com:5432/postgres")
    code = reset_tool.main(["--expect-fingerprint", "0000000000000000",
                            "--confirm", "RESET",
                            "--i-understand-this-deletes-rows"])
    assert code == 2
    out = capsys.readouterr().out
    assert "not the project you named" in out
    assert "Nothing was read or changed" in out


def test_a_missing_database_url_is_refused_rather_than_guessed(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert reset_tool.main(["--expect-fingerprint", "abcabcabcabcabca"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().out


def test_no_secret_is_ever_printed(monkeypatch, capsys):
    """The password is in the URL this tool is handed. It must not come out."""
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://postgres.someproject:sup3rs3cr3t@host.pooler.supabase.com:5432/postgres")
    reset_tool.main(["--expect-fingerprint", "0000000000000000"])
    out = capsys.readouterr().out
    assert "sup3rs3cr3t" not in out
    assert "someproject" not in out
    assert "pooler.supabase.com" not in out


# ===========================================================================
# The inventory is the safety boundary
# ===========================================================================
def test_the_tool_knows_exactly_the_migration_tools_table_inventory():
    """One inventory, imported - not a second copy that can drift.

    If these two ever disagreed, the reset would either leave rows behind
    (migration then fails on a PK collision) or delete a table the migration
    does not repopulate (silent data loss)."""
    from migrate_sqlite_to_postgres import TABLE_ORDER

    assert reset_tool._speaklink_tables() == list(TABLE_ORDER)


def test_deletion_order_is_the_reverse_of_the_fk_safe_creation_order():
    """Children before parents, so no row is orphaned even between statements."""
    order = reset_tool._speaklink_tables()
    assert order.index("stores") < order.index("broadcast_targets")
    assert order.index("hq_users") < order.index("broadcast_sessions")
    assert order.index("receiver_devices") < order.index("receiver_credentials")
    # The tool deletes in reverse, which puts every child first.
    deletion = list(reversed(order))
    assert deletion.index("broadcast_targets") < deletion.index("stores")
    assert deletion.index("receiver_credentials") < deletion.index("receiver_devices")


def test_every_supabase_managed_schema_is_named_as_protected():
    for schema in ("auth", "storage", "realtime", "vault", "extensions",
                   "graphql", "supabase_migrations"):
        assert schema in reset_tool.PROTECTED_SCHEMAS


def test_no_speaklink_table_is_also_a_protected_schema_name():
    assert not (set(reset_tool._speaklink_tables()) & reset_tool.PROTECTED_SCHEMAS)


# ===========================================================================
# Against real PostgreSQL
# ===========================================================================
def _seed(engine):
    """A destination that looks like the stale snapshot: parents and children."""
    with engine.begin() as c:
        store = c.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, is_online_store, "
            "receiver_token, is_active, lifecycle_state, status, created_at, updated_at) "
            "VALUES ('BP','Bindapur','DELHI','NORTH', false, :t, true, 'active', "
            "'offline', now(), now()) RETURNING id"), {"t": uuid.uuid4().hex}).scalar_one()
        user = c.execute(text(
            "INSERT INTO hq_users (username, password_hash, role, is_active, "
            "session_version, created_at, lifecycle_state) "
            "VALUES ('founder','x','OWNER', true, 1, now(), 'active') RETURNING id"
        )).scalar_one()
        session = c.execute(text(
            "INSERT INTO broadcast_sessions (campaign_name, started_by, status, "
            "target_mode, selected_store_count, online_store_count, offline_store_count, "
            "created_at) VALUES ('old snapshot', :u, 'completed', 'selected', 1, 1, 0, now()) "
            "RETURNING id"), {"u": user}).scalar_one()
        c.execute(text("INSERT INTO broadcast_targets (session_id, store_id, play_status) "
                       "VALUES (:s, :st, 'pending')"), {"s": session, "st": store})
        c.execute(text("INSERT INTO system_logs (level, message, created_at) "
                       "VALUES ('info','old entry', now())"))


@pg_required
def test_inspecting_reports_the_rows_and_deletes_nothing(pg_engine):
    import postgres_schema
    postgres_schema.create_all(pg_engine)
    _seed(pg_engine)

    schema = _schema_of(pg_engine)
    counts, missing, unexpected = reset_tool.inspect_destination(pg_engine, schema=schema)
    assert unexpected == []
    assert counts["stores"] == 1 and counts["broadcast_targets"] == 1

    # Reporting must not delete: inspect_destination is read-only.
    with pg_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM broadcast_targets")).scalar_one() == 1


@pg_required
def test_the_real_reset_clears_children_and_parents_and_keeps_the_tables(pg_engine):
    import postgres_schema
    from sqlalchemy import inspect

    postgres_schema.create_all(pg_engine)
    _seed(pg_engine)

    schema = _schema_of(pg_engine)
    counts, _, _ = reset_tool.inspect_destination(pg_engine, schema=schema)
    tables_before = set(inspect(pg_engine).get_table_names())

    reset_tool.reset(pg_engine, counts=counts, schema=schema)

    with pg_engine.connect() as c:
        for table in ("broadcast_targets", "broadcast_sessions", "stores",
                      "hq_users", "system_logs"):
            assert c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table

    # DELETE, never DROP: the destination must stay ready for the migration
    # tool without re-running any DDL.
    assert set(inspect(pg_engine).get_table_names()) == tables_before


@pg_required
def test_the_reset_is_one_transaction_so_a_failure_deletes_nothing(pg_engine, monkeypatch):
    """If any statement fails, the destination must be exactly as it was.

    A half-reset destination is the worst possible outcome: the migration
    would then fail on the surviving rows, and the operator would be holding
    a database that is neither the old snapshot nor the new one.
    """
    import postgres_schema
    postgres_schema.create_all(pg_engine)
    _seed(pg_engine)

    schema = _schema_of(pg_engine)
    counts, _, _ = reset_tool.inspect_destination(pg_engine, schema=schema)

    # Force a genuine mid-transaction failure. The bogus name must be in BOTH
    # the iteration order and the counts, or reset() simply skips it and the
    # test proves nothing - which is exactly how the first version of this
    # test passed while asserting nothing at all.
    real_order = reset_tool._speaklink_tables()
    poisoned_order = ["table_that_does_not_exist"] + real_order
    poisoned_counts = dict(counts)
    poisoned_counts["table_that_does_not_exist"] = 1
    monkeypatch.setattr(reset_tool, "_speaklink_tables", lambda: poisoned_order)

    with pytest.raises(Exception):
        reset_tool.reset(pg_engine, counts=poisoned_counts, schema=schema)

    with pg_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM stores")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM broadcast_targets")).scalar_one() == 1


@pg_required
def test_an_unrecognised_public_table_is_reported_as_unexpected(pg_engine):
    """Fail closed. An unexpected table means the database is not what this
    tool assumes, and guessing would be how a reset destroys something it was
    never told about."""
    import postgres_schema
    postgres_schema.create_all(pg_engine)
    with pg_engine.begin() as c:
        c.execute(text("CREATE TABLE somebody_elses_table (id integer)"))

    _, _, unexpected = reset_tool.inspect_destination(
        pg_engine, schema=_schema_of(pg_engine))
    assert "somebody_elses_table" in unexpected


@pg_required
def test_the_managed_schema_snapshot_reports_supabase_schemas(pg_engine):
    snapshot = reset_tool.managed_schema_snapshot(pg_engine)
    # On a real Supabase project at least auth and storage exist and are
    # non-empty; the point is that the tool can see them in order to prove it
    # left them alone.
    assert isinstance(snapshot, dict)
    for schema, count in snapshot.items():
        assert schema in reset_tool.PROTECTED_SCHEMAS
        assert isinstance(count, int)
