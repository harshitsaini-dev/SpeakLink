"""Deleting Stores from a live HQ, provably and only the intended ones.

The persistent HQ was seeded from a 13-Store demo catalog. The approved 44-Store
catalog was cut over later and the demo rows were archived rather than removed -
right at the time, because archiving is reversible. The operator has authorised
their removal, which makes this the most dangerous tool in the repository: it
deletes rows from the database 44 Stores depend on.

So the tests are mostly about what it REFUSES to do. Every refusal asserts that
the database came out byte-identical, because "it refused" and "it refused
without writing" are different claims and only the second one is worth having.

Nothing here opens the live database. Every test builds its own temporary file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
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

from store_catalog import CANONICAL_STORES  # noqa: E402
from tools.purge_legacy_catalog import (  # noqa: E402
    LEGACY_FINGERPRINTS,
    PurgeRefused,
    apply_plan,
    backup,
    build_plan,
    verify,
)


LIVE_DATABASE = (
    Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    / "EchoCast-AI" / "persistent-lan-server" / "data" / "echocast.db"
)


@pytest.fixture(autouse=True)
def the_live_database_is_never_opened():
    """A whole-file guard. These tests delete rows for a living, so the one thing
    they must never touch is the database an operator is using."""
    before = (
        hashlib.sha256(LIVE_DATABASE.read_bytes()).hexdigest()
        if LIVE_DATABASE.exists() else None
    )
    yield
    if before is not None:
        after = hashlib.sha256(LIVE_DATABASE.read_bytes()).hexdigest()
        assert after == before, "a test wrote to the LIVE persistent database"


def _schema(connection):
    connection.executescript(
        """
        CREATE TABLE stores (
            id INTEGER PRIMARY KEY, store_code TEXT, store_name TEXT,
            city TEXT, region TEXT, is_active INTEGER DEFAULT 1,
            lifecycle_state TEXT DEFAULT 'active', receiver_token TEXT);
        CREATE TABLE hq_users (
            id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
            role TEXT, is_active INTEGER DEFAULT 1);
        CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE broadcast_targets (
            id INTEGER PRIMARY KEY, session_id INTEGER REFERENCES broadcast_sessions(id),
            store_id INTEGER REFERENCES stores(id));
        CREATE TABLE receiver_events (
            id INTEGER PRIMARY KEY, store_id INTEGER REFERENCES stores(id),
            event_type TEXT);
        CREATE TABLE receiver_devices (
            id INTEGER PRIMARY KEY, public_id TEXT,
            store_id INTEGER REFERENCES stores(id));
        CREATE TABLE receiver_credentials (
            id INTEGER PRIMARY KEY, device_id INTEGER REFERENCES receiver_devices(id));
        CREATE TABLE receiver_enrollment_codes (
            id INTEGER PRIMARY KEY, store_id INTEGER REFERENCES stores(id));
        CREATE TABLE system_logs (id INTEGER PRIMARY KEY, message TEXT);
        """
    )


def build_database(path: Path, *, legacy=True, canonical=True, extra_store=None,
                   mixed_session=False, legacy_device=False) -> Path:
    connection = sqlite3.connect(str(path))
    try:
        _schema(connection)
        connection.execute(
            "INSERT INTO hq_users (id, username, password_hash, role) "
            "VALUES (1,'admin','x','ADMIN'), (2,'owneradmin','x','OWNER')")
        if legacy:
            for store_id, code, name in sorted(LEGACY_FINGERPRINTS):
                connection.execute(
                    "INSERT INTO stores (id, store_code, store_name, city, region) "
                    "VALUES (?,?,?,?,?)", (store_id, code, name, "Demo", "Demo Zone"))
        if canonical:
            for index, entry in enumerate(CANONICAL_STORES, start=100):
                connection.execute(
                    "INSERT INTO stores (id, store_code, store_name, city, region) "
                    "VALUES (?,?,?,?,?)",
                    (index, entry.short_name, entry.full_name, entry.zone, entry.zone))
        if extra_store:
            connection.execute(
                "INSERT INTO stores (id, store_code, store_name, city, region) "
                "VALUES (?,?,?,?,?)", extra_store)
        # One legacy-only session with two legacy targets and some events.
        connection.execute("INSERT INTO broadcast_sessions (id, title) VALUES (1,'demo run')")
        connection.execute("INSERT INTO broadcast_targets (id, session_id, store_id) "
                           "VALUES (1,1,1), (2,1,2)")
        connection.execute("INSERT INTO receiver_events (id, store_id, event_type) "
                           "VALUES (1,1,'connected'), (2,2,'connected')")
        if mixed_session:
            connection.execute("INSERT INTO broadcast_sessions (id, title) VALUES (2,'mixed')")
            connection.execute("INSERT INTO broadcast_targets (id, session_id, store_id) "
                               "VALUES (3,2,1), (4,2,100)")
        if canonical:
            connection.execute(
                "INSERT INTO receiver_devices (id, public_id, store_id) VALUES (1,'keep-me',100)")
            connection.execute("INSERT INTO receiver_credentials (id, device_id) VALUES (1,1)")
            connection.execute(
                "INSERT INTO receiver_enrollment_codes (id, store_id) VALUES (1,100)")
        if legacy_device:
            connection.execute(
                "INSERT INTO receiver_devices (id, public_id, store_id) VALUES (9,'mock',1)")
            connection.execute("INSERT INTO receiver_credentials (id, device_id) VALUES (9,9)")
            connection.execute(
                "INSERT INTO receiver_enrollment_codes (id, store_id) VALUES (9,1)")
        connection.commit()
    finally:
        connection.close()
    return path


def fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_ro(path: Path):
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)


@pytest.fixture()
def database(tmp_path):
    return build_database(tmp_path / "hq.db")


# ===========================================================================
# The plan describes exactly what will go
# ===========================================================================
def test_the_plan_names_the_thirteen_legacy_stores(database):
    connection = open_ro(database)
    try:
        plan = build_plan(connection)
    finally:
        connection.close()

    assert plan.store_ids == list(range(1, 14))


def test_the_plan_includes_only_legacy_sessions_targets_and_events(database):
    connection = open_ro(database)
    try:
        plan = build_plan(connection)
    finally:
        connection.close()

    assert plan.session_ids == [1]
    assert plan.target_ids == [1, 2]
    assert plan.event_count == 2


def test_the_plan_leaves_the_canonical_device_alone(database):
    connection = open_ro(database)
    try:
        plan = build_plan(connection)
    finally:
        connection.close()

    assert plan.device_ids == []
    assert plan.credential_ids == []
    assert plan.enrollment_code_ids == []


def test_a_legacy_linked_device_is_included_but_a_canonical_one_is_not(tmp_path):
    database = build_database(tmp_path / "hq.db", legacy_device=True)
    connection = open_ro(database)
    try:
        plan = build_plan(connection)
    finally:
        connection.close()

    assert plan.device_ids == [9], "the canonical Device was swept in"
    assert plan.credential_ids == [9]
    assert plan.enrollment_code_ids == [9]


def test_building_a_plan_writes_nothing(database):
    before = fingerprint(database)
    connection = open_ro(database)
    try:
        build_plan(connection)
    finally:
        connection.close()
    assert fingerprint(database) == before


# ===========================================================================
# Refusals - and every one of them writes nothing
# ===========================================================================
def test_an_unknown_store_stops_the_run(tmp_path):
    database = build_database(
        tmp_path / "hq.db",
        extra_store=(999, "CUSTOM-1", "A Store Somebody Added", "Delhi", "Zone 1"))
    before = fingerprint(database)

    connection = open_ro(database)
    try:
        with pytest.raises(PurgeRefused) as refusal:
            build_plan(connection)
    finally:
        connection.close()

    assert "CUSTOM-1" in str(refusal.value)
    assert fingerprint(database) == before


def test_a_session_spanning_both_catalogs_stops_the_run(tmp_path):
    database = build_database(tmp_path / "hq.db", mixed_session=True)
    before = fingerprint(database)

    connection = open_ro(database)
    try:
        with pytest.raises(PurgeRefused) as refusal:
            build_plan(connection)
    finally:
        connection.close()

    assert "2" in str(refusal.value)
    assert "history" in str(refusal.value).lower()
    assert fingerprint(database) == before


def test_a_database_missing_approved_stores_is_refused(tmp_path):
    """Wrong database, or a cutover that never finished. Either way this tool
    would be deleting from something it does not understand."""
    database = build_database(tmp_path / "hq.db", canonical=False)
    before = fingerprint(database)

    connection = open_ro(database)
    try:
        with pytest.raises(PurgeRefused):
            build_plan(connection)
    finally:
        connection.close()

    assert fingerprint(database) == before


def test_a_renamed_legacy_row_is_unknown_not_legacy(tmp_path):
    """The fingerprint is id + code + name together. A row whose name was edited
    is no longer the row that was classified, and a name match alone is how a real
    Store gets deleted the day somebody opens one in Mumbai."""
    database = build_database(tmp_path / "hq.db")
    connection = sqlite3.connect(str(database))
    connection.execute("UPDATE stores SET store_name='Mumbai Andheri REAL' WHERE id=1")
    connection.commit()
    connection.close()

    connection = open_ro(database)
    try:
        with pytest.raises(PurgeRefused) as refusal:
            build_plan(connection)
    finally:
        connection.close()
    assert "Mumbai Andheri REAL" in str(refusal.value)


# ===========================================================================
# Applying it
# ===========================================================================
def test_applying_leaves_exactly_the_approved_catalog(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
        result = verify(connection)
    finally:
        connection.close()

    assert result["stores_total"] == len(CANONICAL_STORES) == 44
    assert result["codes_match_catalog"] is True
    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0


def test_applying_preserves_both_users(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
        result = verify(connection)
    finally:
        connection.close()

    assert result["users"] == ["admin", "owneradmin"]


def test_applying_preserves_the_canonical_device(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
        result = verify(connection)
    finally:
        connection.close()

    assert result["devices"] == [(1, 100)], "the canonical Device did not survive"


def test_applying_preserves_system_logs(database):
    connection = sqlite3.connect(str(database))
    connection.execute("INSERT INTO system_logs (id, message) VALUES (1,'an operational line')")
    connection.commit()
    try:
        apply_plan(connection, build_plan(connection))
        remaining = connection.execute("SELECT COUNT(*) FROM system_logs").fetchone()[0]
    finally:
        connection.close()

    assert remaining == 1, "logs were deleted; their identity cannot be proved from text"


def test_a_canonical_store_is_never_deleted(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
        codes = {r[0] for r in connection.execute("SELECT store_code FROM stores")}
    finally:
        connection.close()

    assert codes == {entry.short_name for entry in CANONICAL_STORES}


def test_canonical_store_ids_are_not_renumbered(database):
    before = {r[1]: r[0] for r in
              open_ro(database).execute("SELECT id, store_code FROM stores")}
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
        after = {r[1]: r[0] for r in connection.execute("SELECT id, store_code FROM stores")}
    finally:
        connection.close()

    for code, store_id in after.items():
        assert before[code] == store_id, f"{code} was renumbered"


# ===========================================================================
# Idempotence and rollback
# ===========================================================================
def test_a_second_run_finds_nothing_to_do(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
    finally:
        connection.close()

    connection = open_ro(database)
    try:
        second = build_plan(connection)
    finally:
        connection.close()

    assert second.empty, "a second run would delete something"


def test_a_second_apply_changes_no_bytes(database):
    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
    finally:
        connection.close()
    after_first = fingerprint(database)

    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, build_plan(connection))
    finally:
        connection.close()

    assert fingerprint(database) == after_first


class FailingConnection(sqlite3.Connection):
    """Fails partway through the deletion sequence.

    A Connection subclass rather than a monkeypatch, because
    ``sqlite3.Connection.cursor`` is a read-only attribute and cannot be replaced
    on an instance.
    """

    fail_after = 3

    def cursor(self, *args, **kwargs):
        inner = super().cursor(*args, **kwargs)
        state = {"calls": 0}
        limit = self.fail_after

        class Wrapped:
            def execute(self, *a, **k):
                state["calls"] += 1
                if state["calls"] > limit:
                    raise sqlite3.OperationalError("disk I/O error")
                return inner.execute(*a, **k)

        return Wrapped()


def test_a_failure_midway_rolls_everything_back(database):
    """Halfway through is the dangerous moment: targets gone, Stores still there.
    Either all of it happens or none of it does."""
    before = fingerprint(database)

    planner = sqlite3.connect(str(database))
    try:
        plan = build_plan(planner)
    finally:
        planner.close()

    connection = sqlite3.connect(str(database), factory=FailingConnection)
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_plan(connection, plan)
    finally:
        connection.close()

    assert fingerprint(database) == before, "a failed purge left the database changed"

    # And the data is genuinely still all there, not merely the same size.
    connection = open_ro(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 57
        assert connection.execute("SELECT COUNT(*) FROM broadcast_targets").fetchone()[0] == 2
    finally:
        connection.close()


# ===========================================================================
# The backup is a real backup
# ===========================================================================
def test_the_backup_is_consistent_and_verified(database, tmp_path):
    destination = tmp_path / "backups" / "copy.db"
    digest = backup(database, destination)

    assert destination.exists()
    assert len(digest) == 64
    connection = open_ro(destination)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 57
    finally:
        connection.close()


def test_the_backup_leaves_no_sidecar(database, tmp_path):
    destination = tmp_path / "backups" / "copy.db"
    backup(database, destination)
    for suffix in ("-wal", "-shm"):
        assert not Path(str(destination) + suffix).exists()
