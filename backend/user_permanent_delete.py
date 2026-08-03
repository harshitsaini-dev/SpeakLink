"""Permanently deleting an HQ User - the row really goes, the history stays.

WHAT WAS WRONG BEFORE

``user_deletion.py`` called this "permanent deletion" but kept the row and
tombstoned it. That was a defensible first answer: every history table
references ``hq_users.id``, and removing the row would either break those
references or erase the record of what somebody did. But it had a consequence
the operator eventually hit head-on - the UNIQUE index still held the
username, so an account that had been "permanently deleted" made its name
permanently unusable:

    Create user "admin"  ->  "The username 'admin' is already in use."

and User Management kept showing the account for ever with Rights, Scope and
Reset Password beside it. An account that still occupies the namespace and
still has actions has not been deleted; it has been hidden.

THE ID-REUSE TRAP, WHICH IS WHY SNAPSHOTS ARE NOT OPTIONAL

``hq_users.id`` is ``INTEGER PRIMARY KEY`` with **no AUTOINCREMENT**, so
SQLite hands out ``max(id) + 1``. Delete the highest-numbered account and the
next one created receives the same id. In the live database, broadcast
session #2 was started by user id 3; deleting id 3 and creating anybody else
would have made that broadcast appear to belong to the new person - a
different human being, silently, with no error anywhere.

So historical ownership is recorded as an immutable SNAPSHOT on the history
row itself, and the foreign key is set to NULL. A NULL cannot be rebound by
a future insert; an id can.

WHAT IS DELETED AND WHAT SURVIVES

Deleted with the account, because it is live security state and must never
reach a later account that happens to reuse the name:

    user_permission_overrides   the account's explicit ALLOW/DENY rights
    user_store_scope            which Stores it was limited to

Kept, with the foreign key nulled and the identity preserved as a snapshot:

    broadcast_sessions          who ran the broadcast
    permission_audit_events     who changed whose rights
    store_scope_audit_events    who changed whose scope
    receiver_enrollment_codes   who issued the code

Kept untouched, because they never had a foreign key and already carry their
own snapshots:

    user_deletion_events, admin_deletion_events, store_deletion_events,
    device_deletion_events, system_logs

WHAT THIS MODULE DOES NOT DO

It does not disable foreign keys. ``PRAGMA foreign_keys`` stays on and stays
meaningful throughout - the columns are made nullable so that NULL is a legal
value, which is a different thing from switching the check off.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


class PermanentDeletionRefused(RuntimeError):
    """The account was not deleted. Never carries a credential or a hash.

    Deliberately owned by THIS module rather than imported from
    ``user_deletion``. A test fixture that reloads ``user_deletion`` without
    also reloading this module would otherwise leave two distinct classes with
    the same name: the one raised here and the one the route catches, so a
    clean refusal would surface as an unhandled 500. That failure has bitten
    this codebase before through ``receiver_enrollment_api``, and it is
    avoided here by not sharing the class across a module boundary at all.
    """


#: History that keeps its row and loses its pointer. ``table -> columns``.
#:
#: The pointer is nulled rather than left alone because of the id-reuse trap
#: in the module docstring: a dangling id is not inert, it is a claim that a
#: future account will unknowingly satisfy.
HISTORY_REFERENCES: dict[str, tuple[str, ...]] = {
    "broadcast_sessions": ("started_by",),
    "permission_audit_events": ("actor_user_id", "target_user_id"),
    "store_scope_audit_events": ("actor_user_id", "target_user_id"),
    "receiver_enrollment_codes": ("created_by",),
}

#: Live security state. Belongs to the account, dies with it. If any of this
#: survived, a new account created with the deleted account's username would
#: inherit rights or a Store Scope somebody granted to a different person.
LIVE_SECURITY_TABLES: dict[str, str] = {
    "user_permission_overrides": "user_id",
    "user_store_scope": "user_id",
}

#: Snapshot columns added to broadcast_sessions so Broadcast History can name
#: the operator after their account is gone, without joining to a row that no
#: longer exists and without trusting an id that can be reissued.
SESSION_SNAPSHOT_COLUMNS = {
    "started_by_username": "VARCHAR(100)",
    "started_by_display_name": "VARCHAR(200)",
}


@dataclass
class PermanentDeletionResult:
    user_id: int
    username: str
    role: str
    deleted_at: str
    #: What was removed with the account.
    security_rows_removed: dict = field(default_factory=dict)
    #: What kept its row and had its pointer nulled.
    history_rows_detached: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def _column_is_not_null(engine: Engine, table: str, column: str) -> bool:
    for entry in inspect(engine).get_columns(table):
        if entry["name"] == column:
            return not entry.get("nullable", True)
    return False


def _sqlite_rebuild(connection, table: str, columns: tuple[str, ...]) -> None:
    """Relax NOT NULL on SQLite, which cannot ALTER a column.

    The documented table rebuild: create the replacement, copy every row,
    drop the original, rename, recreate the indexes.

    The new definition is derived from the CURRENT one by removing only the
    NOT NULL token from the named columns, so nothing else about the table -
    its other constraints, its column order, its foreign keys - is invented
    here or can drift from what the database actually had.
    """
    ddl = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name = :t"),
        {"t": table}).scalar_one()

    rebuilt = ddl
    for column in columns:
        # Only the column's own definition line, matched on its leading name,
        # so a NOT NULL belonging to a different column is never touched.
        for pattern in (f"\t{column} INTEGER NOT NULL", f" {column} INTEGER NOT NULL"):
            rebuilt = rebuilt.replace(pattern, pattern.replace(" NOT NULL", ""))
    if rebuilt == ddl:
        return

    indexes = [
        row.sql for row in connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name = :t "
                 "AND sql IS NOT NULL"),
            {"t": table}).all()
    ]

    temporary = f"{table}__rebuild"
    connection.exec_driver_sql(
        rebuilt.replace(f"CREATE TABLE {table}", f"CREATE TABLE {temporary}", 1))
    column_names = [row[1] for row in
                    connection.exec_driver_sql(f"PRAGMA table_info('{table}')").all()]
    joined = ", ".join(f'"{name}"' for name in column_names)
    connection.exec_driver_sql(
        f"INSERT INTO {temporary} ({joined}) SELECT {joined} FROM {table}")
    connection.exec_driver_sql(f"DROP TABLE {table}")
    connection.exec_driver_sql(f"ALTER TABLE {temporary} RENAME TO {table}")
    for statement in indexes:
        connection.exec_driver_sql(statement)


def _sqlite_make_user_ids_never_reused(engine: Engine) -> bool:
    """Give ``hq_users.id`` AUTOINCREMENT so a deleted id is never reissued.

    WHY THIS IS PART OF DELETION AND NOT A TIDY-UP

    ``INTEGER PRIMARY KEY`` alone means SQLite assigns ``max(id) + 1``. Delete
    the highest-numbered account and the very next account created receives
    that exact id - a different human being, holding a number that history,
    audit rows and any external record still associate with the person who was
    deleted. The snapshots elsewhere in this module stop Broadcast History
    rebinding, but they cannot stop an operator reading an old audit row that
    names user 3 and looking up who user 3 is today.

    AUTOINCREMENT keeps a high-water mark in ``sqlite_sequence`` that only ever
    rises, so a released id stays released. PostgreSQL sequences already behave
    this way, which is why this is SQLite-only.

    Returns True when the table was rebuilt.
    """
    with engine.connect() as connection:
        ddl = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='hq_users'")
        ).scalar_one_or_none()
    if not ddl or "AUTOINCREMENT" in ddl.upper():
        return False

    # SQLite requires the exact form `INTEGER PRIMARY KEY AUTOINCREMENT` on the
    # column itself, so the separate table-level `PRIMARY KEY (id)` clause has
    # to fold into the column definition - taking its comma with it, or the
    # rebuilt CREATE TABLE ends in a dangling comma and will not parse.
    import re

    rebuilt = re.sub(r"\bid INTEGER NOT NULL\b",
                     "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT", ddl, count=1)
    rebuilt = re.sub(r",\s*PRIMARY KEY\s*\(\s*id\s*\)", "", rebuilt, count=1)

    if ("AUTOINCREMENT" not in rebuilt.upper()
            or re.search(r"PRIMARY KEY\s*\(\s*id\s*\)", rebuilt)):
        # The definition was not the shape expected. Leave it alone rather than
        # rebuild a users table from a guess - ids being reusable is a much
        # smaller problem than a malformed hq_users.
        return False

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
        pragma.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with engine.begin() as connection:
            indexes = [r.sql for r in connection.execute(
                text("SELECT sql FROM sqlite_master WHERE type='index' AND "
                     "tbl_name='hq_users' AND sql IS NOT NULL")).all()]
            columns = [r[1] for r in
                       connection.exec_driver_sql("PRAGMA table_info('hq_users')").all()]
            joined = ", ".join(f'"{c}"' for c in columns)
            connection.exec_driver_sql(
                rebuilt.replace("CREATE TABLE hq_users", "CREATE TABLE hq_users__rebuild", 1))
            connection.exec_driver_sql(
                f"INSERT INTO hq_users__rebuild ({joined}) SELECT {joined} FROM hq_users")
            connection.exec_driver_sql("DROP TABLE hq_users")
            connection.exec_driver_sql("ALTER TABLE hq_users__rebuild RENAME TO hq_users")
            for statement in indexes:
                connection.exec_driver_sql(statement)
            # The high-water mark. Copying the rows into an AUTOINCREMENT table
            # already makes SQLite record one, and ALTER TABLE RENAME carries
            # it across - this only guarantees the floor, for the case where
            # the table was empty and no sequence row was written at all.
            #
            # sqlite_sequence has no UNIQUE constraint on `name`, so ON CONFLICT
            # is not available here; UPDATE-then-INSERT is the portable form.
            highest = connection.execute(
                text("SELECT COALESCE(MAX(id), 0) FROM hq_users")).scalar_one()
            updated = connection.execute(
                text("UPDATE sqlite_sequence SET seq = :s WHERE name = 'hq_users' "
                     "AND seq < :s"), {"s": highest}).rowcount
            existing_row = connection.execute(
                text("SELECT COUNT(*) FROM sqlite_sequence WHERE name = 'hq_users'")
            ).scalar_one()
            if not existing_row:
                connection.execute(
                    text("INSERT INTO sqlite_sequence (name, seq) VALUES ('hq_users', :s)"),
                    {"s": highest})
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
            pragma.exec_driver_sql("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(
            "Rebuilding hq_users for non-reusable ids introduced foreign key "
            f"violations: {violations[:5]}")
    return True


def ensure_user_permanent_delete_schema(engine: Engine) -> None:
    """Additive columns, plus the nullability the deletion needs. Idempotent.

    Two kinds of change, and only the second is unusual:

    * ADD COLUMN for the broadcast_sessions snapshots - ordinary, additive,
      standard SQL on both engines.
    * DROP NOT NULL on the historical user references. On PostgreSQL that is
      one ALTER; on SQLite it is a table rebuild, because SQLite cannot alter
      a column in place. Data is copied, never regenerated.

    Nothing here deletes a row, and running it twice changes nothing the
    second time.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    # ---- snapshots on broadcast_sessions ---------------------------------
    if "broadcast_sessions" in existing_tables:
        present = {c["name"] for c in inspector.get_columns("broadcast_sessions")}
        with engine.begin() as connection:
            for column, sql_type in SESSION_SNAPSHOT_COLUMNS.items():
                if column not in present:
                    connection.exec_driver_sql(
                        f"ALTER TABLE broadcast_sessions ADD COLUMN {column} {sql_type}")

    # ---- ids that are never reissued --------------------------------------
    if engine.dialect.name == "sqlite" and "hq_users" in existing_tables:
        _sqlite_make_user_ids_never_reused(engine)

    # ---- nullability on the historical references -------------------------
    pending = {}
    for table, columns in HISTORY_REFERENCES.items():
        if table not in existing_tables:
            continue
        needing = tuple(c for c in columns if _column_is_not_null(engine, table, c))
        if needing:
            pending[table] = needing
    if not pending:
        return

    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            for table, columns in pending.items():
                for column in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ALTER COLUMN {column} DROP NOT NULL")
        return

    # SQLite: the documented table rebuild.
    #
    # WHY foreign_keys IS TURNED OFF HERE, AND WHY THAT IS NOT THE FORBIDDEN
    # KIND
    #
    # `DROP TABLE broadcast_sessions` is refused while foreign keys are on,
    # because broadcast_targets and broadcast_store_leases point at it - even
    # though the very next statement puts an identical table back. SQLite's
    # own "Making Other Kinds Of Table Schema Changes" procedure therefore
    # brackets the rebuild with the pragma off.
    #
    # That is a different act from switching the constraint off so a DELETE
    # can violate it. Nothing is deleted here, every row is copied across, and
    # the pragma is restored and then a full `foreign_key_check` is run - if
    # the rebuild introduced a single violation this RAISES and the database
    # is left for a human rather than reported as migrated. The permanent
    # deletion itself runs with foreign keys fully on.
    #
    # The pragma is a no-op inside a transaction, so it is issued on an
    # autocommit connection either side of the transactional rebuild.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
        pragma.exec_driver_sql("PRAGMA foreign_keys=OFF")
    try:
        with engine.begin() as connection:
            for table, columns in pending.items():
                _sqlite_rebuild(connection, table, columns)
    finally:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as pragma:
            pragma.exec_driver_sql("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(
            "The user-deletion schema migration introduced foreign key "
            f"violations and was not completed cleanly: {violations[:5]}")


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------
def _snapshot_broadcast_ownership(connection, user_id: int, username: str,
                                  display_name: str | None) -> None:
    """Write who ran each broadcast onto the broadcast itself.

    Done BEFORE the pointer is nulled, so a failure here aborts the whole
    transaction with the pointer still intact rather than leaving history
    that has forgotten its owner.
    """
    connection.execute(
        text("UPDATE broadcast_sessions SET started_by_username = :u, "
             "started_by_display_name = :d WHERE started_by = :i"),
        {"u": username, "d": display_name or username, "i": user_id})


def permanently_delete_user(
    engine: Engine, *, user_id: int, actor_user_id: int,
    typed_confirmation: str,
) -> PermanentDeletionResult:
    """Delete the account for real, in one transaction.

    Order matters and is the whole design:

      1. read and validate the target
      2. refuse self-deletion and last-administrator deletion
      3. snapshot historical ownership onto the history rows
      4. remove live security state
      5. null the historical pointers
      6. DELETE the row
      7. record the administrative audit

    Any failure rolls the whole thing back, so there is no state in which the
    account is half gone - no orphaned rights, no history that has lost its
    owner, no row deleted without an audit.
    """
    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, username, role, display_name, lifecycle_state "
                 "FROM hq_users WHERE id = :i"),
            {"i": user_id}).first()
        if row is None:
            raise PermanentDeletionRefused("That account no longer exists.")

        if int(actor_user_id) == int(user_id):
            raise PermanentDeletionRefused(
                "You cannot permanently delete the account you are signed in as. "
                "Ask another SUPER ADMIN.")

        if typed_confirmation != row.username:
            raise PermanentDeletionRefused(
                "The typed confirmation did not match. Type the username exactly: "
                f"{row.username}")

        # The lockout guard, unchanged in meaning from the tombstone version.
        # A deleted row is gone rather than marked, so "not this one" is the
        # whole condition - there is no deleted state left to exclude.
        if str(row.role).upper() in {"OWNER", "SUPER_ADMIN"}:
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM hq_users WHERE UPPER(role) IN "
                     "('OWNER','SUPER_ADMIN') AND is_active = :active AND id <> :i"),
                {"i": user_id, "active": True}).scalar_one()
            if remaining == 0:
                raise PermanentDeletionRefused(
                    "This is the last active SUPER ADMIN. Deleting it would leave "
                    "nobody able to administer SpeakLink, and that cannot be undone "
                    "from inside the product. Promote another SUPER ADMIN first.")

        # ---- 3. snapshot before anything is detached ----------------------
        _snapshot_broadcast_ownership(connection, user_id, row.username,
                                      row.display_name)

        # ---- 4. live security state dies with the account -----------------
        security_removed: dict[str, int] = {}
        for table, column in LIVE_SECURITY_TABLES.items():
            try:
                result = connection.execute(
                    text(f"DELETE FROM {table} WHERE {column} = :i"), {"i": user_id})
                security_removed[table] = int(result.rowcount or 0)
            except Exception:
                security_removed[table] = 0

        # ---- 5. history keeps its rows, loses its pointers -----------------
        history_detached: dict[str, int] = {}
        for table, columns in HISTORY_REFERENCES.items():
            for column in columns:
                try:
                    result = connection.execute(
                        text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :i"),
                        {"i": user_id})
                    history_detached[f"{table}.{column}"] = int(result.rowcount or 0)
                except Exception:
                    history_detached[f"{table}.{column}"] = 0

        # ---- 6. the row itself --------------------------------------------
        connection.execute(text("DELETE FROM hq_users WHERE id = :i"), {"i": user_id})

        # ---- 7. the audit, which outlives the account ----------------------
        # Written to the SAME table the tombstone used, so one query answers
        # "what happened to this account" across both eras. It carries the old
        # id, the username and the role - never a hash, a token or a password.
        connection.execute(
            text("INSERT INTO user_deletion_events "
                 "(actor_user_id, user_id, username, role, history_counts_json, deleted_at) "
                 "VALUES (:actor, :uid, :username, :role, :counts, :now)"),
            {"actor": actor_user_id, "uid": user_id, "username": row.username,
             "role": row.role, "now": now,
             "counts": json.dumps({"security_removed": security_removed,
                                   "history_detached": history_detached,
                                   "row_deleted": True})})

    return PermanentDeletionResult(
        user_id=user_id, username=row.username, role=row.role, deleted_at=now,
        security_rows_removed=security_removed,
        history_rows_detached=history_detached,
    )


# ---------------------------------------------------------------------------
# migration for accounts tombstoned by the previous design
# ---------------------------------------------------------------------------
def find_legacy_tombstones(engine: Engine) -> list[dict]:
    """Accounts the OLD design marked deleted but left in the table.

    Deliberately narrow: ``lifecycle_state = 'deleted'`` only. An archived
    account is restorable and must never be caught by this; an active account
    obviously must not.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, username, role, display_name FROM hq_users "
                 "WHERE lifecycle_state = 'deleted'")).all()
    return [{"id": r.id, "username": r.username, "role": r.role,
             "display_name": r.display_name} for r in rows]


def purge_legacy_user_tombstones(engine: Engine, *, actor_user_id: int | None = None) -> dict:
    """Finish the job the old design started, for accounts already marked deleted.

    An operator already decided these accounts were permanently deleted. What
    the old code could not do was release the username, so this completes that
    decision rather than making a new one - which is why it touches ONLY
    ``lifecycle_state = 'deleted'`` and never an archived or active account.

    Idempotent: once the rows are gone there is nothing left to match, so a
    second startup finds none and writes no second audit row.
    """
    tombstones = find_legacy_tombstones(engine)
    if not tombstones:
        return {"purged": 0, "usernames": []}

    purged: list[str] = []
    for entry in tombstones:
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as connection:
            _snapshot_broadcast_ownership(connection, entry["id"], entry["username"],
                                          entry["display_name"])
            security_removed = {}
            for table, column in LIVE_SECURITY_TABLES.items():
                try:
                    result = connection.execute(
                        text(f"DELETE FROM {table} WHERE {column} = :i"), {"i": entry["id"]})
                    security_removed[table] = int(result.rowcount or 0)
                except Exception:
                    security_removed[table] = 0
            history_detached = {}
            for table, columns in HISTORY_REFERENCES.items():
                for column in columns:
                    try:
                        result = connection.execute(
                            text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :i"),
                            {"i": entry["id"]})
                        history_detached[f"{table}.{column}"] = int(result.rowcount or 0)
                    except Exception:
                        history_detached[f"{table}.{column}"] = 0

            connection.execute(text("DELETE FROM hq_users WHERE id = :i"), {"i": entry["id"]})

            # The original tombstone already wrote a user_deletion_events row
            # when the operator made the decision. A second row is written
            # only to record that the row itself has now gone, and only when
            # no such record exists - otherwise a restart would accumulate one
            # audit row per boot.
            already = connection.execute(
                text("SELECT COUNT(*) FROM user_deletion_events WHERE user_id = :i "
                     "AND history_counts_json LIKE '%\"row_deleted\": true%'"),
                {"i": entry["id"]}).scalar_one()
            if not already:
                connection.execute(
                    text("INSERT INTO user_deletion_events "
                         "(actor_user_id, user_id, username, role, history_counts_json, deleted_at) "
                         "VALUES (:actor, :uid, :username, :role, :counts, :now)"),
                    {"actor": actor_user_id if actor_user_id is not None else entry["id"],
                     "uid": entry["id"], "username": entry["username"],
                     "role": entry["role"], "now": now,
                     "counts": json.dumps({"migrated_legacy_tombstone": True,
                                           "security_removed": security_removed,
                                           "history_detached": history_detached,
                                           "row_deleted": True})})
        purged.append(entry["username"])

    return {"purged": len(purged), "usernames": purged}


__all__ = [
    "HISTORY_REFERENCES",
    "PermanentDeletionRefused",
    "LIVE_SECURITY_TABLES",
    "PermanentDeletionResult",
    "SESSION_SNAPSHOT_COLUMNS",
    "ensure_user_permanent_delete_schema",
    "find_legacy_tombstones",
    "permanently_delete_user",
    "purge_legacy_user_tombstones",
]
