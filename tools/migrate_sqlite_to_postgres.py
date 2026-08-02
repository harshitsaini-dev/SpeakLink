#!/usr/bin/env python
"""Migrate EchoCast's SQLite database to PostgreSQL (Supabase), preserving
every row, every id, and every history table exactly as it was.

    python migrate_sqlite_to_postgres.py --sqlite-path PATH --dry-run
    python migrate_sqlite_to_postgres.py --sqlite-path PATH
    python migrate_sqlite_to_postgres.py --sqlite-path PATH --verify

The destination PostgreSQL URL is read from the DATABASE_URL environment
variable - never a command-line argument, so it never appears in shell
history or a process list. Nothing this tool prints ever includes the URL,
a password, a JWT, a Receiver credential, or the Receiver HMAC key.

SAFETY

* The SQLite source is opened read-only (``file:...?mode=ro``) and this
  tool never executes a single write against it - not a backup, not a
  WAL checkpoint, nothing. Take a real backup with the existing backup
  procedure BEFORE running this, as its own separate step.
* --dry-run reports exactly what WOULD be migrated (table names and row
  counts, in FK-safe order) and touches neither database.
* The real run refuses to write into a PostgreSQL database that already
  has rows in any table this tool would populate, unless --force is
  passed explicitly - migrating into an unknown, already-populated
  database is exactly the "overwrite an unknown populated database"
  scenario the migration brief forbids.
* Existing integer primary keys are preserved exactly (explicit INSERTs
  with the id column set) - Broadcast History, Receiver events and every
  audit table reference these ids, and a re-numbered id would silently
  break every one of those references.
* After copying data, every PostgreSQL sequence backing a SERIAL/IDENTITY
  primary key is advanced past the highest migrated id, so the very next
  INSERT through the running application does not collide with migrated
  history.
* --verify re-opens both databases read-only and compares row counts
  table by table, plus a handful of specific identity checks (Bindapur's
  Store id and Device public_id, in particular) - it performs no writes.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
for candidate in (REPO_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# The exact FK-safe order, computed once from the real schema graph (ORM
# models.py tables + postgres_schema.py's Core tables) via SQLAlchemy's own
# topological sort - never hand-guessed. See tests/test_migration_tool.py
# for the assertion that this list stays in sync with the live schema.
TABLE_ORDER = [
    # No foreign keys, so the position is free - they lead because they are
    # what receiver_auth_service reads before it will authenticate anybody,
    # and because a reader of this list should see them before wondering
    # whether Receiver auth state travels. It does.
    "schema_migrations",
    "receiver_credential_migration_state",
    "stores",
    "hq_users",
    "login_security_state",
    "system_logs",
    "permissions",
    "permission_audit_events",
    "store_deletion_events",
    "user_deletion_events",
    "device_deletion_events",
    "admin_deletion_events",
    "receiver_enrollment_codes",
    "broadcast_sessions",
    "receiver_events",
    "receiver_devices",
    "role_permissions",
    "user_permission_overrides",
    "user_store_scope",
    "store_scope_audit_events",
    "broadcast_targets",
    # After both stores and broadcast_sessions, which it references. Its
    # partial unique index travels with the schema, not with the rows, so a
    # migrated copy enforces the one-Store-one-broadcast rule exactly as the
    # source did.
    "broadcast_store_leases",
    "receiver_credentials",
    "receiver_store_primary_device",
    "receiver_credential_events",
]

#: Tables whose primary key is a SERIAL/IDENTITY integer that must be
#: repaired after migration. permissions/role_permissions/receiver_store_
#: primary_device have non-integer or composite primary keys - no sequence
#: to repair.
SEQUENCE_TABLES = [
    "stores", "hq_users", "system_logs", "permission_audit_events",
    "store_deletion_events", "user_deletion_events", "device_deletion_events", "admin_deletion_events",
    "receiver_enrollment_codes", "broadcast_sessions",
    "receiver_events", "receiver_devices", "user_permission_overrides",
    "user_store_scope", "store_scope_audit_events", "broadcast_targets",
    "broadcast_store_leases",
    "receiver_credentials", "receiver_credential_events",
]


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"No SQLite database at {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        # The table does not exist in this source database (an older
        # SQLite database predating a feature) - zero to migrate, not an
        # error, so migrating an older database still works.
        return 0


def plan(sqlite_path: Path) -> list[tuple[str, int]]:
    """(table_name, row_count) in FK-safe order. Read-only."""
    connection = _open_sqlite_readonly(sqlite_path)
    try:
        return [(table, _row_count(connection, table)) for table in TABLE_ORDER]
    finally:
        connection.close()


def _destination_engine():
    from db_config import DatabaseConfigError, load_database_config

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "DATABASE_URL is not set. This tool never accepts the destination "
            "URL as a command-line argument - export DATABASE_URL first."
        )
    try:
        config = load_database_config(app_env="production", database_url=url)
    except DatabaseConfigError as error:
        raise SystemExit(str(error))
    if config.dialect != "postgresql":
        raise SystemExit("DATABASE_URL must be a postgresql:// URL for this tool.")

    from sqlalchemy import create_engine
    return create_engine(config.url)


def _destination_is_empty(engine) -> bool:
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.connect() as connection:
        for table in TABLE_ORDER:
            if table not in existing:
                continue
            count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            if count:
                return False
    return True


def migrate(sqlite_path: Path, *, force: bool = False) -> dict:
    """The real migration. Returns {table: rows_copied}."""
    import postgres_schema
    from sqlalchemy import text

    engine = _destination_engine()
    postgres_schema.create_all(engine)

    if not force and not _destination_is_empty(engine):
        raise SystemExit(
            "The destination PostgreSQL database already has rows in at "
            "least one table this tool would populate. Refusing to write - "
            "this tool never overwrites an unknown, already-populated "
            "database. Pass --force only if you have independently confirmed "
            "this destination is safe to overwrite."
        )

    source = _open_sqlite_readonly(sqlite_path)
    copied: dict[str, int] = {}
    try:
        with engine.begin() as connection:
            for table in TABLE_ORDER:
                columns = _table_columns(source, table)
                if not columns:
                    copied[table] = 0
                    continue
                rows = source.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
                if rows:
                    target = postgres_schema.metadata.tables[table]
                    payload = [_coerce_row(dict(row), target) for row in rows]
                    # An INSERT built from the Core Table (not a text() string)
                    # so SQLAlchemy applies each column's own bind processor -
                    # which is what turns a coerced Python bool/datetime into
                    # the right PostgreSQL wire type.
                    connection.execute(target.insert(), payload)
                copied[table] = len(rows)
            _repair_sequences(connection)
    finally:
        source.close()
    return copied


#: SQLite has no boolean and no date type. It stores booleans as INTEGER 0/1
#: and DateTime columns as TEXT. PostgreSQL has real ``boolean`` and
#: ``timestamp`` types and refuses the SQLite representations outright
#: ("column is of type boolean but expression is of type smallint"). Rather
#: than special-casing column names - which would silently miss the next
#: boolean somebody adds - each value is converted according to the TYPE
#: DECLARED FOR THAT COLUMN in postgres_schema/models, so the mapping stays
#: correct as the schema grows.
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


def _parse_datetime(value: str):
    from datetime import datetime

    text_value = value.strip()
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text_value, fmt)
        except ValueError:
            continue
    raise SystemExit(
        f"Could not parse a stored timestamp: {text_value!r}. Refusing to "
        "guess - a misread timestamp would silently corrupt broadcast history."
    )


def _coerce_row(row: dict, target) -> dict:
    """One SQLite row -> values PostgreSQL will accept, driven by the
    destination column types rather than by column-name guesswork."""
    from sqlalchemy import Boolean, DateTime

    coerced = {}
    for name, value in row.items():
        column = target.columns.get(name)
        if value is None or column is None:
            coerced[name] = value
            continue
        column_type = column.type
        if isinstance(column_type, Boolean) and not isinstance(value, bool):
            # SQLite's 0/1. Anything non-zero is true, matching how every
            # existing SQLite read of these columns already behaves.
            coerced[name] = bool(value)
        elif isinstance(column_type, DateTime) and isinstance(value, str):
            coerced[name] = _parse_datetime(value)
        else:
            coerced[name] = value
    return coerced


def _repair_sequences(connection) -> None:
    """After preserving explicit ids, move every SERIAL sequence past the
    highest migrated id - so the next INSERT through the running
    application never collides with migrated history.

    The set of tables is ASKED OF THE DATABASE rather than read from the
    hand-maintained SEQUENCE_TABLES list. That list had drifted: it omitted
    ``login_security_state``, whose sequence therefore stayed at
    (last_value=1, is_called=false) while rows 1 and 2 existed - so the next
    login-security write would have failed on a duplicate key. A list that has
    to be updated by hand every time a table gains an id column is a list that
    will be wrong again.

    ``pg_get_serial_sequence`` returns NULL for a table whose id is not backed
    by a sequence (composite or text primary keys), so those are skipped
    without needing to be enumerated either.
    """
    from sqlalchemy import text

    # A pure catalog read. pg_get_serial_sequence() cannot be used as the
    # filter here: PostgreSQL is free to evaluate it before the join that
    # restricts to tables having an 'id' column, and it RAISES rather than
    # returning NULL for a table that has none ("column id of relation
    # permissions does not exist"). information_schema answers the same
    # question without executing anything that can fail.
    tables = connection.execute(text(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND column_name = 'id' "
        # SERIAL carries a nextval default; a GENERATED ... AS IDENTITY column
        # has NO default at all and is flagged separately. Matching only the
        # first would repeat the original mistake in a new form.
        "AND (column_default LIKE 'nextval(%' OR is_identity = 'YES') "
        "ORDER BY table_name"
    )).scalars().all()

    for table in tables:
        connection.execute(text(
            f'SELECT setval(pg_get_serial_sequence(\'"{table}"\', \'id\'), '
            f'COALESCE((SELECT MAX(id) FROM "{table}"), 1), '
            f'(SELECT MAX(id) FROM "{table}") IS NOT NULL)'
        ))


def verify(sqlite_path: Path) -> dict:
    """Compare row counts and specific identities. Read-only both sides."""
    from sqlalchemy import text

    engine = _destination_engine()
    source = _open_sqlite_readonly(sqlite_path)
    report: dict = {"counts": {}, "identity_checks": {}}
    try:
        with engine.connect() as connection:
            for table in TABLE_ORDER:
                sqlite_count = _row_count(source, table)
                try:
                    pg_count = connection.execute(
                        text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                except Exception:
                    pg_count = None
                report["counts"][table] = {
                    "sqlite": sqlite_count, "postgres": pg_count,
                    "match": sqlite_count == pg_count,
                }

            bindapur_sqlite = source.execute(
                "SELECT id, store_code, store_name FROM stores WHERE store_code = 'BP'"
            ).fetchone()
            bindapur_pg = connection.execute(
                text("SELECT id, store_code, store_name FROM stores WHERE store_code = 'BP'")
            ).first()
            report["identity_checks"]["bindapur_store"] = {
                "sqlite": dict(bindapur_sqlite) if bindapur_sqlite else None,
                "postgres": dict(bindapur_pg._mapping) if bindapur_pg else None,
            }

            device_sqlite = source.execute(
                "SELECT public_id, store_id, status FROM receiver_devices "
                "WHERE public_id = '3b1ff11f-0b18-4f56-b911-30f036cbddd9'"
            ).fetchone()
            device_pg = connection.execute(
                text("SELECT public_id, store_id, status FROM receiver_devices "
                    "WHERE public_id = '3b1ff11f-0b18-4f56-b911-30f036cbddd9'")
            ).first()
            report["identity_checks"]["bindapur_device"] = {
                "sqlite": dict(device_sqlite) if device_sqlite else None,
                "postgres": dict(device_pg._mapping) if device_pg else None,
            }
    finally:
        source.close()
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", required=True, type=Path,
                        help="Path to the source echocast.db (opened read-only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the migration plan; write nothing")
    parser.add_argument("--verify", action="store_true",
                        help="Compare an already-migrated destination against the source")
    parser.add_argument("--force", action="store_true",
                        help="Allow migrating into a non-empty destination (dangerous)")
    args = parser.parse_args(argv)

    if args.dry_run:
        print("=== DRY RUN - nothing will be written ===")
        for table, count in plan(args.sqlite_path):
            print(f"  {table:<32} {count:>8} row(s)")
        print("(FK-safe order shown above; destination is never opened in --dry-run)")
        return 0

    if args.verify:
        report = verify(args.sqlite_path)
        print("=== VERIFICATION ===")
        all_match = True
        for table, row in report["counts"].items():
            flag = "OK" if row["match"] else "MISMATCH"
            if not row["match"]:
                all_match = False
            print(f"  {table:<32} sqlite={row['sqlite']:>6}  postgres={row['postgres']!s:>6}  {flag}")
        print("--- identity checks ---")
        for name, check in report["identity_checks"].items():
            same = check["sqlite"] == check["postgres"]
            if not same:
                all_match = False
            print(f"  {name}: {'MATCH' if same else 'MISMATCH'}")
        print("=== " + ("ALL MATCH" if all_match else "MISMATCHES FOUND") + " ===")
        return 0 if all_match else 1

    copied = migrate(args.sqlite_path, force=args.force)
    print("=== MIGRATION COMPLETE ===")
    for table, count in copied.items():
        print(f"  {table:<32} {count:>8} row(s) copied")
    print("Run again with --verify to compare source and destination.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
