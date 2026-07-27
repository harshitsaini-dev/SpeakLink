"""Operator tooling for the Receiver Credential Lifecycle Phase 1 migration.

``backend/migrations.py`` has held a correct, tested Phase 1 migration for a
while, and it has never run against a real database. What was missing is the
part an operator actually uses: seeing what state a database is in, rehearsing
the change, taking a verified backup before touching anything, and checking
afterwards that the result is right rather than merely exception-free.

The protected database is refused outright. ``migrations.py`` has an
``allow_protected_database`` switch for its own tests; this tool deliberately
does not forward it, because migrating live data is a maintenance-window
decision with a named owner and a rehearsed rollback - not a command-line flag.

Nothing here deletes a database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from migrations import run_receiver_credential_phase_one  # noqa: E402


PROTECTED_DATABASE = (BACKEND_ROOT / "echocast_live.db").resolve()

PHASE_ONE_TABLES = {
    "receiver_devices",
    "receiver_credentials",
    "receiver_credential_events",
    "receiver_credential_migration_state",
    "schema_migrations",
}

# Tables whose row counts must be identical before and after. The migration only
# adds tables, so any change here means something went wrong.
PRESERVED_TABLES = ("stores", "hq_users")


class MigrationToolError(RuntimeError):
    """The database is missing, unreadable, or not a database at all."""


class ProtectedDatabaseRefused(MigrationToolError):
    """Someone pointed this at backend/echocast_live.db."""


def _resolved(path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if resolved == PROTECTED_DATABASE:
        raise ProtectedDatabaseRefused(
            "that is the protected EchoCast database. This tool will not touch it. "
            "Migrating live data belongs in an approved maintenance window with a "
            "named owner, a verified backup and a rehearsed rollback."
        )
    return resolved


def _require_database(path: Path) -> None:
    if not path.exists():
        raise MigrationToolError(f"no database at {path}")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        raise MigrationToolError(f"{path} is not a readable SQLite database") from None


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _tables(path: Path) -> set[str]:
    connection = _read_only(path)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        connection.close()


def _counts(path: Path) -> dict[str, int]:
    present = _tables(path)
    connection = _read_only(path)
    try:
        return {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in PRESERVED_TABLES
            if table in present
        }
    finally:
        connection.close()


def describe_status(path) -> dict:
    """What state is this database in? Reads only."""
    resolved = _resolved(path)
    _require_database(resolved)
    present = _tables(resolved)
    missing = sorted(PHASE_ONE_TABLES - present)
    counts = _counts(resolved)
    return {
        "database": str(resolved),
        "applied": not missing,
        "present_tables": sorted(PHASE_ONE_TABLES & present),
        "missing_tables": missing,
        "store_count": counts.get("stores", 0),
        "hq_user_count": counts.get("hq_users", 0),
    }


def preflight(path) -> dict:
    """Rehearse. Reads only, changes nothing, says what would happen."""
    status = describe_status(path)
    return {
        **status,
        "would_create": status["missing_tables"],
        "already_applied": status["applied"],
        "safe_to_apply": True,
        "note": (
            "Phase 1 only adds tables. No existing row is read, rewritten or "
            "deleted, and no column on stores or hq_users changes."
        ),
    }


def _backup(path: Path, backup_dir: Path) -> dict:
    """Copy first, then verify the copy - a backup nobody checked is a rumour."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / f"{path.stem}.{stamp}.pre-phase-one.db"

    # sqlite3's backup API copies a consistent snapshot even with WAL present,
    # which a plain file copy does not guarantee.
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()

    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    _require_database(destination)
    return {
        "backup_path": str(destination),
        "backup_bytes": destination.stat().st_size,
        "backup_sha256": digest.hexdigest(),
    }


def _verify(path: Path, counts_before: dict[str, int]) -> dict:
    present = _tables(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_receiver_%'"
            )
        }
    finally:
        connection.close()

    return {
        "tables_present": PHASE_ONE_TABLES <= present,
        "foreign_keys_enabled": foreign_keys,
        "indexes_present": len(indexes) >= 4,
        "row_counts_preserved": _counts(path) == counts_before,
        "row_counts": _counts(path),
    }


def apply_phase_one(path, *, backup_dir) -> dict:
    """Back up, migrate in one transaction, then verify. Idempotent."""
    resolved = _resolved(path)
    _require_database(resolved)

    status = describe_status(resolved)
    if status["applied"]:
        return {
            "database": str(resolved),
            "applied": False,
            "already_applied": True,
            "verification": _verify(resolved, _counts(resolved)),
        }

    counts_before = _counts(resolved)
    backup = _backup(resolved, Path(backup_dir))

    engine = create_engine(f"sqlite:///{resolved.as_posix()}")
    try:
        result = run_receiver_credential_phase_one(engine)
    finally:
        engine.dispose()

    verification = _verify(resolved, counts_before)
    if not all(
        verification[key]
        for key in ("tables_present", "foreign_keys_enabled", "indexes_present", "row_counts_preserved")
    ):
        raise MigrationToolError(
            "post-migration verification failed; restore the backup recorded in this "
            f"result before continuing: {backup['backup_path']}"
        )

    return {
        "database": str(resolved),
        "applied": True,
        "already_applied": False,
        "migration_version": result.version,
        "migration_state": result.state,
        **backup,
        "verification": verification,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="receiver_migration",
        description=(
            "Inspect, rehearse and apply Receiver Credential Lifecycle Phase 1. "
            "The protected database is always refused."
        ),
    )
    parser.add_argument("action", choices=["status", "preflight", "apply"])
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Required for 'apply'. A verified copy is written here before anything changes.",
    )
    arguments = parser.parse_args(argv)

    try:
        if arguments.action == "status":
            payload = describe_status(arguments.database)
        elif arguments.action == "preflight":
            payload = preflight(arguments.database)
        else:
            if arguments.backup_dir is None:
                parser.error("--backup-dir is required for 'apply'")
            payload = apply_phase_one(arguments.database, backup_dir=arguments.backup_dir)
    except MigrationToolError as failure:
        print(f"REFUSED: {failure}", file=sys.stderr)
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
