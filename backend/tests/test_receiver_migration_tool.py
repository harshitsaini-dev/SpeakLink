"""Applying the Receiver schema must be boring, reversible and refusable.

`migrations.run_receiver_credential_phase_one` has existed and been tested for a
while, and it has never run against a real database. What was missing is the
part an operator actually uses: a way to see what state a database is in, a way
to rehearse the change, a backup taken before anything is touched, and a
verification afterwards that says more than "no exception".

This drives that tool. Every test uses a temporary database. The protected
database is never opened, and the tool refuses it outright rather than offering
an override.
"""

from __future__ import annotations

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

from tools.receiver_migration import (  # noqa: E402
    PHASE_ONE_TABLES,
    MigrationToolError,
    ProtectedDatabaseRefused,
    apply_phase_one,
    describe_status,
    preflight,
)


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"


def _legacy_database(path: Path) -> Path:
    """A database shaped like the live one: Stores, users, no Receiver tables."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE stores (
                id INTEGER PRIMARY KEY,
                store_code VARCHAR(50) NOT NULL UNIQUE,
                store_name VARCHAR(200) NOT NULL,
                city VARCHAR(100) NOT NULL,
                region VARCHAR(100) NOT NULL,
                is_online_store BOOLEAN NOT NULL DEFAULT 0,
                receiver_token VARCHAR(64) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'offline',
                last_seen DATETIME,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE hq_users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(100) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL DEFAULT 'admin',
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.execute(
            "INSERT INTO stores (store_code, store_name, city, region, receiver_token) "
            "VALUES ('UN', 'Uttam Nagar Old', 'UN ZONE', 'UN ZONE', 'a1b2c3')"
        )
        connection.execute(
            "INSERT INTO stores (store_code, store_name, city, region, receiver_token) "
            "VALUES ('ASR', 'Uttam Nagar ASR', 'UN ZONE', 'UN ZONE', 'd4e5f6')"
        )
        connection.execute(
            "INSERT INTO hq_users (username, password_hash) VALUES ('pilot-operator', '$2b$12$x')"
        )
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture()
def legacy(tmp_path) -> Path:
    return _legacy_database(tmp_path / "legacy.db")


def _tables(path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _rows(path: Path, table: str) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Status and preflight tell the truth before anything changes
# ---------------------------------------------------------------------------
def test_status_reports_a_legacy_database_as_not_migrated(legacy):
    status = describe_status(legacy)
    assert status["applied"] is False
    assert status["missing_tables"] == sorted(PHASE_ONE_TABLES)
    assert status["store_count"] == 2


def test_preflight_changes_nothing(legacy):
    before = (legacy.stat().st_size, _tables(legacy))
    preflight(legacy)
    assert (legacy.stat().st_size, _tables(legacy)) == before


def test_preflight_reports_what_it_would_create(legacy):
    report = preflight(legacy)
    assert report["would_create"] == sorted(PHASE_ONE_TABLES)
    assert report["safe_to_apply"] is True


def test_preflight_on_an_already_migrated_database_is_a_no_op(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    report = preflight(legacy)
    assert report["would_create"] == []
    assert report["already_applied"] is True


# ---------------------------------------------------------------------------
# The protected database is refused, with no override
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("operation", [describe_status, preflight])
def test_reading_the_protected_database_is_refused(operation):
    with pytest.raises(ProtectedDatabaseRefused):
        operation(PROTECTED_DATABASE)


def test_applying_to_the_protected_database_is_refused(tmp_path):
    with pytest.raises(ProtectedDatabaseRefused):
        apply_phase_one(PROTECTED_DATABASE, backup_dir=tmp_path)


def test_the_tool_exposes_no_protected_database_override():
    """migrations.py has allow_protected_database for its own tests. The
    operator-facing tool deliberately does not forward it: a live migration is a
    maintenance-window decision, not a flag."""
    import inspect

    from tools import receiver_migration

    for name in ("apply_phase_one", "preflight", "describe_status"):
        signature = inspect.signature(getattr(receiver_migration, name))
        assert "allow_protected" not in " ".join(signature.parameters)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------
def test_applying_creates_every_phase_one_table(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    assert PHASE_ONE_TABLES <= _tables(legacy)


def test_applying_takes_a_verified_backup_first(legacy, tmp_path):
    backups = tmp_path / "backups"
    result = apply_phase_one(legacy, backup_dir=backups)

    backup = Path(result["backup_path"])
    assert backup.exists()
    assert backup.parent == backups
    assert result["backup_bytes"] == backup.stat().st_size
    assert len(result["backup_sha256"]) == 64

    # The backup is the pre-migration database, so it still has no new tables.
    assert not (PHASE_ONE_TABLES & _tables(backup))


def test_the_backup_is_a_usable_database(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    backup = next((tmp_path / "backups").iterdir())
    assert _rows(backup, "stores") == 2
    assert _rows(backup, "hq_users") == 1


def test_existing_rows_survive_the_migration(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    assert _rows(legacy, "stores") == 2
    assert _rows(legacy, "hq_users") == 1


def test_store_tokens_and_password_hashes_are_untouched(legacy, tmp_path):
    connection = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
    before = (
        sorted(row[0] for row in connection.execute("SELECT receiver_token FROM stores")),
        [row[0] for row in connection.execute("SELECT password_hash FROM hq_users")],
    )
    connection.close()

    apply_phase_one(legacy, backup_dir=tmp_path / "backups")

    connection = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
    after = (
        sorted(row[0] for row in connection.execute("SELECT receiver_token FROM stores")),
        [row[0] for row in connection.execute("SELECT password_hash FROM hq_users")],
    )
    connection.close()
    assert after == before


def test_applying_twice_is_idempotent(legacy, tmp_path):
    first = apply_phase_one(legacy, backup_dir=tmp_path / "b1")
    second = apply_phase_one(legacy, backup_dir=tmp_path / "b2")
    assert first["applied"] is True
    assert second["applied"] is False
    assert second["already_applied"] is True
    assert PHASE_ONE_TABLES <= _tables(legacy)


def test_foreign_keys_are_enforced_afterwards(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    connection = sqlite3.connect(legacy)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO receiver_devices "
                "(store_id, public_id, display_name, status, enrolled_at, created_by) "
                "VALUES (999999, 'x', 'x', 'active', '2026-07-27T00:00:00+00:00', 1)"
            )
    finally:
        connection.close()


def test_post_migration_verification_checks_more_than_absence_of_errors(legacy, tmp_path):
    result = apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    verification = result["verification"]
    assert verification["tables_present"] is True
    assert verification["foreign_keys_enabled"] is True
    assert verification["row_counts_preserved"] is True
    assert verification["indexes_present"] is True


def test_a_missing_database_is_refused(tmp_path):
    with pytest.raises(MigrationToolError):
        apply_phase_one(tmp_path / "nothing-here.db", backup_dir=tmp_path / "backups")


def test_a_non_database_file_is_refused(tmp_path):
    junk = tmp_path / "not-a-database.db"
    junk.write_text("this is plainly not SQLite", encoding="utf-8")
    with pytest.raises(MigrationToolError):
        apply_phase_one(junk, backup_dir=tmp_path / "backups")


def test_no_database_is_ever_deleted(legacy, tmp_path):
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    assert legacy.exists()


# ---------------------------------------------------------------------------
# A copy of the isolated pilot database, never the original
# ---------------------------------------------------------------------------
def test_a_copy_of_the_pilot_database_migrates_cleanly(tmp_path):
    import shutil

    pilot = Path(os.environ.get("LOCALAPPDATA", "")) / "EchoCast-AI" / "local-pilot" / "data" / "echocast_local_pilot.db"
    if not pilot.exists():
        pytest.skip("no isolated pilot database on this machine")
    if PHASE_ONE_TABLES & _tables(pilot):
        # Already migrated, so there is nothing for this tool to do and
        # `applied` is correctly False. That is the normal state of any pilot
        # database prepared by a current release - `prepare()` creates these
        # tables itself - so this test can only exercise a real migration on a
        # machine still carrying one from an older build. Skipped honestly
        # rather than rewritten to assert the no-op, which would test nothing.
        pytest.skip("the local pilot database is already migrated")

    copy = tmp_path / "pilot-copy.db"
    shutil.copy2(pilot, copy)
    before_stores = _rows(copy, "stores")
    # What the original looks like BEFORE the copy is migrated. Recorded rather
    # than assumed: this used to assert the pilot had no phase-one tables at
    # all, which was never a statement about this tool. It only held while the
    # machine happened to carry a pilot database created by a release that
    # predated those tables - today `prepare()` itself creates them, so the
    # assertion tested the age of a local artefact rather than any behaviour.
    pilot_tables_before = _tables(pilot)
    pilot_stores_before = _rows(pilot, "stores")

    result = apply_phase_one(copy, backup_dir=tmp_path / "backups")

    assert result["applied"] is True
    assert PHASE_ONE_TABLES <= _tables(copy)
    assert _rows(copy, "stores") == before_stores
    # The original is untouched: only the copy was migrated. Compared against
    # its own earlier state, which is what "untouched" actually means.
    assert _tables(pilot) == pilot_tables_before
    assert _rows(pilot, "stores") == pilot_stores_before


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_metadata_is_unchanged(legacy, tmp_path):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    apply_phase_one(legacy, backup_dir=tmp_path / "backups")
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()
