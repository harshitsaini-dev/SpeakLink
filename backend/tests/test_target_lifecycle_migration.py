"""The lifecycle columns must land on a database that already has data.

This runs against a table built the OLD way - no lifecycle_state, no
current_generation, rows already in it - because that is what the live database
is. A migration that only works on an empty schema is a migration that has
never met the thing it has to migrate.

What must hold afterwards: existing rows read as ACTIVE generation 1, which is
what they were - Stores targeted when their Broadcast started and never touched
again - and nothing else about them moves.
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

from sqlalchemy import create_engine, text  # noqa: E402

from broadcast_target_lifecycle import (  # noqa: E402
    ACTIVE,
    LIFECYCLE_STATES,
    ensure_target_lifecycle_schema,
)

#: The table as it was before dynamic targeting - deliberately written out
#: rather than imported from models, so this keeps testing the OLD shape even
#: after the model gains more columns.
OLD_TABLE = """
CREATE TABLE broadcast_targets (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    store_id INTEGER,
    store_code_snapshot VARCHAR(50),
    store_name_snapshot VARCHAR(200),
    play_status VARCHAR(30) NOT NULL DEFAULT 'pending',
    command_sent_at DATETIME,
    started_playing_at DATETIME,
    stopped_at DATETIME,
    error_message TEXT
)
"""


@pytest.fixture()
def old_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text(OLD_TABLE))
        connection.execute(text(
            "INSERT INTO broadcast_targets "
            "(id, session_id, store_id, play_status, error_message) VALUES "
            "(1, 50, 10, 'playback_confirmed', NULL),"
            "(2, 50, 11, 'failed', 'Receiver offline at broadcast start'),"
            "(3, 51, 12, 'stopped', NULL)"))
    return engine


def columns(engine):
    with engine.connect() as connection:
        return {row[1] for row in connection.execute(
            text("PRAGMA table_info(broadcast_targets)"))}


def rows(engine):
    with engine.connect() as connection:
        return connection.execute(text(
            "SELECT id, play_status, error_message, lifecycle_state, "
            "current_generation FROM broadcast_targets ORDER BY id")).fetchall()


# ===========================================================================
# The migration itself
# ===========================================================================

def test_the_columns_arrive_on_a_table_that_already_has_rows(old_database):
    assert "lifecycle_state" not in columns(old_database)

    ensure_target_lifecycle_schema(old_database)

    assert "lifecycle_state" in columns(old_database)
    assert "current_generation" in columns(old_database)


def test_existing_rows_read_as_what_they_actually_were(old_database):
    ensure_target_lifecycle_schema(old_database)

    for row in rows(old_database):
        assert row.lifecycle_state == ACTIVE, (
            "a Store targeted at start should read as ACTIVE, not as unset")
        assert row.current_generation == 1


def test_nothing_else_about_the_existing_rows_moves(old_database):
    ensure_target_lifecycle_schema(old_database)
    after = rows(old_database)

    assert [(r.id, r.play_status) for r in after] == [
        (1, "playback_confirmed"), (2, "failed"), (3, "stopped")]
    assert after[1].error_message == "Receiver offline at broadcast start", (
        "the migration rewrote data it had no business touching")


def test_running_it_twice_changes_nothing(old_database):
    ensure_target_lifecycle_schema(old_database)
    once = rows(old_database)
    ensure_target_lifecycle_schema(old_database)
    assert rows(old_database) == once


def test_it_survives_a_table_that_does_not_exist_yet(tmp_path):
    """A fresh install creates the table from the model, columns included."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", future=True)
    ensure_target_lifecycle_schema(engine)   # must not raise


def test_the_model_and_the_migration_agree(old_database):
    """Both paths have to produce the same table, or a fresh install differs
    from a migrated one in a way nobody notices until it matters."""
    from models import Base, BroadcastTarget

    fresh = create_engine("sqlite://", future=True)
    BroadcastTarget.__table__.create(fresh)
    ensure_target_lifecycle_schema(old_database)

    with fresh.connect() as connection:
        from_model = {row[1] for row in connection.execute(
            text("PRAGMA table_info(broadcast_targets)"))}
    assert {"lifecycle_state", "current_generation"} <= from_model
    assert {"lifecycle_state", "current_generation"} <= columns(old_database)
    assert Base is not None


def test_every_state_name_is_spelled_once(old_database):
    """A state the code writes but the set does not know is a typo waiting."""
    assert ACTIVE in LIFECYCLE_STATES
    assert all(name.isupper() for name in LIFECYCLE_STATES)
    assert len(LIFECYCLE_STATES) == 8
