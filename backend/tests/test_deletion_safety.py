"""Deleting a Store or a User, and refusing when history depends on it.

WHY DELETE IS NOT THE DEFAULT

A Store owns Receiver Devices, broadcast targets, sessions and Receiver events.
A User is the actor recorded in every audit line. Removing either row would
either orphan that history or cascade it away, and both destroy the only record
of what was announced where, and by whom.

So the default is archive. Hard deletion exists for exactly one case: a row
somebody created by mistake five minutes ago that nothing has ever referenced.

The rule this file enforces is that the check is **counted, not assumed** - a
dependency summary from the database, recomputed inside the transaction that
does the delete, because anything else is a race between the check and the act.

Nothing here touches the protected database.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from deletion_safety import (  # noqa: E402
    DeletionRefused,
    STORE_DEPENDENCY_TABLES,
    delete_store_if_unused,
    delete_user_if_unused,
    store_dependencies,
    user_dependencies,
)


SCHEMA = """
CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code VARCHAR(50) NOT NULL,
    store_name VARCHAR(200) NOT NULL, is_active BOOLEAN DEFAULT 1,
    lifecycle_state VARCHAR(20) DEFAULT 'active');
CREATE TABLE hq_users (id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL, role VARCHAR(50) NOT NULL,
    is_active BOOLEAN DEFAULT 1, lifecycle_state VARCHAR(20) DEFAULT 'active');
CREATE TABLE receiver_devices (id INTEGER PRIMARY KEY, public_id VARCHAR(64),
    store_id INTEGER NOT NULL, display_name VARCHAR(200), status VARCHAR(32));
CREATE TABLE broadcast_targets (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL);
CREATE TABLE receiver_events (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL);
CREATE TABLE receiver_enrollment_codes (id INTEGER PRIMARY KEY, store_id INTEGER NOT NULL);
CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY, started_by INTEGER);
CREATE TABLE system_logs (id INTEGER PRIMARY KEY, message TEXT);
"""


@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'hq.db'}", future=True)
    with made.begin() as connection:
        # One statement per call: exec_driver_sql refuses a multi-statement
        # script, and a fixture that fails to build is a test suite that reports
        # errors about itself rather than about the code.
        for statement in filter(None, (s.strip() for s in SCHEMA.split(";"))):
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql(
            "INSERT INTO stores (store_code, store_name) VALUES ('EMPTY','Never Used'),"
            " ('USED','Has A Device'), ('HIST','Has History')")
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role) VALUES"
            " ('spare','h','VIEWER'), ('busy','h','ADMIN'), ('owneradmin','h','OWNER')")
        # Store 2 has a Device; Store 3 has broadcast history.
        connection.exec_driver_sql(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status)"
            " VALUES ('abc', 2, 'till 1', 'active')")
        connection.exec_driver_sql("INSERT INTO broadcast_targets (store_id) VALUES (3)")
        # User 2 started a broadcast session.
        connection.exec_driver_sql("INSERT INTO broadcast_sessions (started_by) VALUES (2)")
    return made


# ===========================================================================
# The dependency summary
# ===========================================================================
def test_an_untouched_store_has_no_dependencies(engine):
    summary = store_dependencies(engine, store_id=1)
    assert summary.total == 0
    assert summary.deletable is True


def test_every_table_that_can_hold_a_store_is_counted(engine):
    """Named explicitly, so a table added later is a decision rather than a
    silent gap that lets somebody delete a Store still referenced by it."""
    summary = store_dependencies(engine, store_id=1)
    assert set(summary.counts) == set(STORE_DEPENDENCY_TABLES)


def test_a_store_with_a_device_is_not_deletable(engine):
    summary = store_dependencies(engine, store_id=2)
    assert summary.counts["receiver_devices"] == 1
    assert summary.deletable is False


def test_a_retired_device_still_blocks_deletion(engine):
    """Retired is not gone. It is the record that this till existed."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_devices SET status = 'retired' WHERE store_id = 2")
    assert store_dependencies(engine, store_id=2).deletable is False


def test_a_store_with_broadcast_history_is_not_deletable(engine):
    summary = store_dependencies(engine, store_id=3)
    assert summary.counts["broadcast_targets"] == 1
    assert summary.deletable is False


def test_the_summary_explains_itself_in_words(engine):
    summary = store_dependencies(engine, store_id=2)
    assert "Device" in summary.explain() or "device" in summary.explain()


def test_a_missing_table_is_treated_as_zero_not_as_an_error(tmp_path):
    """An older database may not have every table yet. That must not stop the
    summary being produced - but it must never be read as 'nothing depends on
    this' for a table that simply could not be checked."""
    made = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT, store_name TEXT)")
        connection.exec_driver_sql("INSERT INTO stores (store_code, store_name) VALUES ('X','X')")
    summary = store_dependencies(made, store_id=1)
    assert summary.unchecked, "tables that could not be read must be listed"
    assert summary.deletable is False, "unknown must never mean safe"


# ===========================================================================
# Deleting a Store
# ===========================================================================
def test_an_empty_store_can_be_deleted_with_the_right_confirmation(engine):
    deleted = delete_store_if_unused(engine, store_id=1, typed_confirmation="EMPTY")
    assert deleted["store_code"] == "EMPTY"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM stores")).scalar() == 2


def test_the_wrong_typed_confirmation_deletes_nothing(engine):
    with pytest.raises(DeletionRefused) as refusal:
        delete_store_if_unused(engine, store_id=1, typed_confirmation="empty ")
    assert "confirmation" in str(refusal.value).lower()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM stores")).scalar() == 3


def test_a_store_with_a_device_is_refused_and_nothing_changes(engine):
    with pytest.raises(DeletionRefused) as refusal:
        delete_store_if_unused(engine, store_id=2, typed_confirmation="USED")
    assert "archive" in str(refusal.value).lower()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM stores")).scalar() == 3
        assert connection.execute(text("SELECT COUNT(*) FROM receiver_devices")).scalar() == 1


def test_history_is_never_cascaded_away(engine):
    """The refusal must leave the history intact, not tidy it up on the way out.
    """
    with pytest.raises(DeletionRefused):
        delete_store_if_unused(engine, store_id=3, typed_confirmation="HIST")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM broadcast_targets")).scalar() == 1


def test_an_unknown_store_is_refused(engine):
    with pytest.raises(DeletionRefused):
        delete_store_if_unused(engine, store_id=999, typed_confirmation="X")


def test_dependencies_are_rechecked_inside_the_transaction(engine):
    """Checking and then deleting is a race. The count that matters is the one
    taken with the row already locked."""
    import inspect

    from deletion_safety import delete_store_if_unused as target

    source = inspect.getsource(target)
    assert "engine.begin()" in source
    # The recheck must happen on the same connection that performs the delete.
    assert source.index("_count_dependencies") < source.index("DELETE FROM stores")


# ===========================================================================
# Deleting a User
# ===========================================================================
def test_a_user_with_no_history_is_deletable(engine):
    assert user_dependencies(engine, user_id=1).deletable is True


def test_a_user_who_started_a_broadcast_is_not_deletable(engine):
    summary = user_dependencies(engine, user_id=2)
    assert summary.counts["broadcast_sessions"] == 1
    assert summary.deletable is False


def test_an_owner_is_never_hard_deletable(engine):
    """Not because of dependencies - because losing the last one cannot be
    undone from inside the product."""
    with pytest.raises(DeletionRefused) as refusal:
        delete_user_if_unused(engine, user_id=3, typed_confirmation="owneradmin",
                              actor_id=1)
    assert "owner" in str(refusal.value).lower()


def test_you_cannot_delete_the_account_you_are_signed_in_as(engine):
    with pytest.raises(DeletionRefused) as refusal:
        delete_user_if_unused(engine, user_id=1, typed_confirmation="spare", actor_id=1)
    assert "your own" in str(refusal.value).lower()


def test_a_spare_user_can_be_deleted(engine):
    deleted = delete_user_if_unused(engine, user_id=1, typed_confirmation="spare",
                                    actor_id=3)
    assert deleted["username"] == "spare"


def test_deleting_a_user_never_removes_audit_history(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql("INSERT INTO system_logs (message) VALUES ('spare did a thing')")
    delete_user_if_unused(engine, user_id=1, typed_confirmation="spare", actor_id=3)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM system_logs")).scalar() == 1


def test_a_refused_user_delete_changes_nothing(engine):
    with pytest.raises(DeletionRefused):
        delete_user_if_unused(engine, user_id=2, typed_confirmation="busy", actor_id=1)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM hq_users")).scalar() == 3


def test_no_refusal_message_carries_a_password_or_hash(engine):
    for call in (
        lambda: delete_user_if_unused(engine, user_id=3, typed_confirmation="owneradmin", actor_id=1),
        lambda: delete_user_if_unused(engine, user_id=2, typed_confirmation="busy", actor_id=1),
    ):
        with pytest.raises(DeletionRefused) as refusal:
            call()
        assert "$2b$" not in str(refusal.value)
        assert "password" not in str(refusal.value).lower()
