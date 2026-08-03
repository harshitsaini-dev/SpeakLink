"""The two SQLite schema changes that permanent deletion needs, done safely.

SQLite cannot ALTER a column. Relaxing a NOT NULL, or adding AUTOINCREMENT to
an existing primary key, both require the documented table rebuild: create the
replacement, copy every row, drop the original, rename, recreate the indexes.

WHY foreign_keys IS TURNED OFF AROUND A REBUILD, AND WHY THAT IS NOT A DODGE

``DROP TABLE stores`` is refused while foreign keys are on, because
``receiver_devices`` points at it - even though the very next statement puts an
identical table back. SQLite's own "Making Other Kinds Of Table Schema Changes"
procedure therefore brackets the rebuild with the pragma off.

That is a different act from switching the constraint off so a DELETE can
violate it:

* nothing is deleted during a rebuild - every row is copied across;
* the pragma is restored immediately afterwards;
* a full ``foreign_key_check`` then runs, and a single violation RAISES rather
  than being reported as a successful migration;
* the deletions themselves - of a User, of a Store - run with foreign keys
  fully on and are expected to satisfy every constraint honestly.

Extracted into one module so both the User and the Store feature share exactly
this behaviour rather than each carrying a copy that can drift.
"""

from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


class SchemaSurgeryError(RuntimeError):
    """A rebuild did not complete cleanly. The database is left for a human."""


def column_is_not_null(engine: Engine, table: str, column: str) -> bool:
    for entry in inspect(engine).get_columns(table):
        if entry["name"] == column:
            return not entry.get("nullable", True)
    return False


def _table_ddl(connection, table: str) -> str:
    return connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table}).scalar_one()


def _indexes(connection, table: str) -> list[str]:
    return [row.sql for row in connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name = :t "
             "AND sql IS NOT NULL"), {"t": table}).all()]


def _renamed_create(ddl: str, table: str, temporary: str) -> str:
    """Point a CREATE TABLE at the temporary name, quoted or not.

    SQLAlchemy emits `CREATE TABLE "receiver_enrollment_codes" (...)` for some
    tables and `CREATE TABLE stores (...)` for others, depending on how each
    was originally created. A plain string replace of the unquoted form
    silently missed the quoted ones - and the rebuild then tried to recreate
    the table under its own name, which failed with "table already exists"
    only because SQLite happened to notice. Matching both forms explicitly is
    the difference between a migration that works and one that half-works.
    """
    pattern = rf'CREATE TABLE\s+"?{re.escape(table)}"?'
    renamed, count = re.subn(pattern, f'CREATE TABLE "{temporary}"', ddl, count=1)
    if count != 1:
        raise SchemaSurgeryError(
            f"Could not locate the CREATE TABLE statement for {table!r}; "
            "refusing to rebuild it from a guess.")
    return renamed


def _copy_into(connection, table: str, temporary: str) -> None:
    columns = [row[1] for row in
               connection.exec_driver_sql(f"PRAGMA table_info('{table}')").all()]
    joined = ", ".join(f'"{name}"' for name in columns)
    connection.exec_driver_sql(
        f"INSERT INTO {temporary} ({joined}) SELECT {joined} FROM {table}")


def _swap(connection, table: str, temporary: str, indexes: list[str]) -> None:
    connection.exec_driver_sql(f"DROP TABLE {table}")
    connection.exec_driver_sql(f"ALTER TABLE {temporary} RENAME TO {table}")
    for statement in indexes:
        connection.exec_driver_sql(statement)


def _rebuild_bracketed(engine: Engine, work) -> None:
    """Run `work(connection)` with foreign keys off, then verify.

    The pragma is a no-op inside a transaction, so it is issued on autocommit
    connections either side of the transactional rebuild.
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
        pragma.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with engine.begin() as connection:
            work(connection)
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
            pragma.exec_driver_sql("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise SchemaSurgeryError(
            f"The rebuild introduced foreign key violations: {violations[:5]}")


def drop_not_null(engine: Engine, wanted: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """Make the named columns nullable. Idempotent; returns what it changed.

    On PostgreSQL this is one ALTER per column and needs no rebuild at all.
    """
    existing = set(inspect(engine).get_table_names())
    pending: dict[str, tuple[str, ...]] = {}
    for table, columns in wanted.items():
        if table not in existing:
            continue
        needing = tuple(c for c in columns if column_is_not_null(engine, table, c))
        if needing:
            pending[table] = needing
    if not pending:
        return {}

    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            for table, columns in pending.items():
                for column in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
        return pending

    def work(connection):
        for table, columns in pending.items():
            ddl = _table_ddl(connection, table)
            rebuilt = ddl
            for column in columns:
                # Only that column's own definition, anchored on its name, so a
                # NOT NULL belonging to a neighbour is never touched.
                rebuilt = re.sub(rf"(\b{re.escape(column)}\s+\w+(?:\(\d+\))?)\s+NOT NULL",
                                 r"\1", rebuilt, count=1)
            if rebuilt == ddl:
                continue
            indexes = _indexes(connection, table)
            temporary = f"{table}__rebuild"
            connection.exec_driver_sql(_renamed_create(rebuilt, table, temporary))
            _copy_into(connection, table, temporary)
            _swap(connection, table, temporary, indexes)

    _rebuild_bracketed(engine, work)
    return pending


def make_ids_never_reused(engine: Engine, table: str) -> bool:
    """Give ``table.id`` AUTOINCREMENT so a deleted id is never reissued.

    WHY THIS IS PART OF DELETION RATHER THAN A TIDY-UP

    ``INTEGER PRIMARY KEY`` alone means SQLite assigns ``max(id) + 1``. Delete
    the highest-numbered row and the very next insert receives that exact id -
    so a deleted Store or User hands its number to its replacement, and every
    history row, audit entry and external record that still names that id now
    points at something else entirely.

    Snapshots stop history REBINDING, but they cannot stop a human reading an
    old audit row that names id 60 and looking up what id 60 is today.
    AUTOINCREMENT keeps a high-water mark in ``sqlite_sequence`` that only ever
    rises. PostgreSQL sequences already behave this way, so this is SQLite-only.

    Returns True when the table was rebuilt.
    """
    if engine.dialect.name != "sqlite":
        return False
    with engine.connect() as connection:
        ddl = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name = :t"),
            {"t": table}).scalar_one_or_none()
    if not ddl or "AUTOINCREMENT" in ddl.upper():
        return False

    # SQLite requires `INTEGER PRIMARY KEY AUTOINCREMENT` on the column itself,
    # so the table-level `PRIMARY KEY (id)` clause folds in - taking its comma
    # with it, or the rebuilt CREATE TABLE ends in a dangling comma.
    rebuilt = re.sub(r"\bid INTEGER NOT NULL\b",
                     "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT", ddl, count=1)
    rebuilt = re.sub(r",\s*PRIMARY KEY\s*\(\s*id\s*\)", "", rebuilt, count=1)
    if ("AUTOINCREMENT" not in rebuilt.upper()
            or re.search(r"PRIMARY KEY\s*\(\s*id\s*\)", rebuilt)):
        # Not the shape expected. Leave it alone rather than rebuild a real
        # table from a guess - reusable ids are a far smaller problem than a
        # malformed table.
        return False

    def work(connection):
        indexes = _indexes(connection, table)
        temporary = f"{table}__rebuild"
        connection.exec_driver_sql(_renamed_create(rebuilt, table, temporary))
        _copy_into(connection, table, temporary)
        _swap(connection, table, temporary, indexes)
        # Copying into an AUTOINCREMENT table already makes SQLite record a
        # high-water mark, and RENAME carries it across; this only guarantees
        # the floor for a table that was empty. sqlite_sequence has no UNIQUE
        # constraint on `name`, so UPDATE-then-INSERT is the portable form.
        highest = connection.execute(
            text(f"SELECT COALESCE(MAX(id), 0) FROM {table}")).scalar_one()
        connection.execute(
            text("UPDATE sqlite_sequence SET seq = :s WHERE name = :t AND seq < :s"),
            {"s": highest, "t": table})
        if not connection.execute(
                text("SELECT COUNT(*) FROM sqlite_sequence WHERE name = :t"),
                {"t": table}).scalar_one():
            connection.execute(
                text("INSERT INTO sqlite_sequence (name, seq) VALUES (:t, :s)"),
                {"t": table, "s": highest})

    _rebuild_bracketed(engine, work)
    return True


__all__ = [
    "SchemaSurgeryError",
    "column_is_not_null",
    "drop_not_null",
    "make_ids_never_reused",
]
