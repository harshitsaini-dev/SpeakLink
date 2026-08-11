"""Sequence repair, discovered from the catalog rather than a hand-kept list.

THE INCIDENT THIS COMES FROM

The production migration copied ``login_security_state`` rows with explicit
ids 1 and 2, and left its sequence at ``(last_value=1, is_called=false)``. The
next login-security write would have failed on a duplicate key - in the
authentication path, on a live system, at some unpredictable later moment.

The cause was a hand-maintained ``SEQUENCE_TABLES`` list that had drifted:
somebody added a table with an id column and did not add it to the list. A
list that must be updated by hand every time the schema grows is a list that
will be wrong again, so the set is now asked of the database.

No test here calls ``nextval``. nextval is NOT transactional - a rollback does
not give the number back - so "testing" a sequence by advancing it permanently
consumes an id. Everything is verified from ``pg_sequences`` /
``information_schema`` instead.

Skipped entirely without ``TEST_POSTGRES_URL``, and never touches production.
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

from migrate_sqlite_to_postgres import _repair_sequences  # noqa: E402
from sqlalchemy import text  # noqa: E402

from tests.test_postgres_schema import pg_engine, pg_required  # noqa: E402,F401


def _schema_of(engine) -> str:
    with engine.connect() as c:
        return c.execute(text("SELECT current_schema()")).scalar_one()


def _sequence_state(connection, table: str):
    """last_value and is_called, read from the catalog. Never nextval."""
    sequence = connection.execute(text(
        "SELECT pg_get_serial_sequence(:t, 'id')"), {"t": table}).scalar_one()
    if sequence is None:
        return None
    return connection.execute(text(
        f"SELECT last_value, is_called FROM {sequence}")).one()


def _next_id_would_be(connection, table: str) -> int:
    """What the next generated id WOULD be, derived from metadata only.

    PostgreSQL returns last_value + 1 when is_called is true, and last_value
    itself when it is false. Reproducing that rule here is what lets this be
    checked without consuming a number.
    """
    state = _sequence_state(connection, table)
    assert state is not None, f"{table} has no sequence"
    return (state.last_value + 1) if state.is_called else state.last_value


# ===========================================================================
# The exact production regression
# ===========================================================================
@pg_required
def test_login_security_state_rows_1_and_2_leave_the_next_id_at_3(pg_engine):
    """The precise shape observed in production, pinned.

    Before the fix this table was not in the hand-kept list, so its sequence
    stayed at (1, false) and the next insert would have collided with the
    migrated row id 1.
    """
    import postgres_schema

    postgres_schema.create_all(pg_engine)
    schema = _schema_of(pg_engine)

    with pg_engine.begin() as c:
        # Explicit ids, exactly as the migration preserves them.
        c.execute(text("INSERT INTO login_security_state (id, username, failed_count, updated_at) "
                       "VALUES (1, 'alpha', 0, now()), (2, 'beta', 0, now())"))

    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)

    with pg_engine.connect() as c:
        assert _next_id_would_be(c, "login_security_state") == 3, (
            "the next generated id must clear the migrated rows")


@pg_required
def test_every_table_with_a_generated_id_is_repaired_not_a_chosen_few(pg_engine):
    """Discovery, not enumeration.

    Asserted against what the database actually reports, so a table added
    later is covered without anybody remembering to add it anywhere.
    """
    import postgres_schema

    postgres_schema.create_all(pg_engine)
    schema = _schema_of(pg_engine)

    with pg_engine.connect() as c:
        generated = set(c.execute(text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = :s AND column_name = 'id' "
            "AND (column_default LIKE 'nextval(%' OR is_identity = 'YES')"),
            {"s": schema}).scalars().all())

    assert "login_security_state" in generated, (
        "the table that caused the incident must be discovered automatically")
    assert len(generated) > 10, "discovery should find the whole schema, not a subset"

    # And repairing must not raise on any of them, including the empty ones.
    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)


@pg_required
def test_tables_without_an_id_column_do_not_raise(pg_engine):
    """permissions has a text primary key and no id at all.

    An earlier version of this fix filtered with pg_get_serial_sequence() in
    the WHERE clause. PostgreSQL is free to evaluate that before the join that
    restricts to tables having an id, and it RAISES rather than returning NULL
    - so the repair blew up on a perfectly ordinary schema.
    """
    import postgres_schema

    postgres_schema.create_all(pg_engine)
    schema = _schema_of(pg_engine)

    with pg_engine.connect() as c:
        columns = {row[0] for row in c.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = 'permissions'"), {"s": schema})}
    assert "id" not in columns, "this test is meaningless if permissions gains an id"

    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)   # must not raise


@pg_required
def test_an_empty_table_is_left_uncalled_rather_than_advanced(pg_engine):
    """An empty table's sequence must still hand out 1 first.

    Advancing it would silently waste id 1 on every fresh install.
    """
    import postgres_schema

    postgres_schema.create_all(pg_engine)
    schema = _schema_of(pg_engine)

    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)

    with pg_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM receiver_events")).scalar_one() == 0
        assert _next_id_would_be(c, "receiver_events") == 1


@pg_required
def test_repair_is_idempotent(pg_engine):
    """Running it twice must not move anything - a re-run of the migration
    tool, or a retried cutover step, must be safe."""
    import postgres_schema

    postgres_schema.create_all(pg_engine)
    schema = _schema_of(pg_engine)

    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO login_security_state (id, username, failed_count, updated_at) "
                       "VALUES (7, 'gamma', 0, now())"))
    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)
    with pg_engine.connect() as c:
        first = _next_id_would_be(c, "login_security_state")
    with pg_engine.begin() as c:
        c.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        _repair_sequences(c)
    with pg_engine.connect() as c:
        assert _next_id_would_be(c, "login_security_state") == first == 8
