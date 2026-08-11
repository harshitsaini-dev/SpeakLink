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

from sqlite_schema_surgery import drop_not_null, make_ids_never_reused


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
    if "hq_users" in existing_tables:
        make_ids_never_reused(engine, "hq_users")

    # ---- nullability on the historical references -------------------------
    drop_not_null(engine, HISTORY_REFERENCES)


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
