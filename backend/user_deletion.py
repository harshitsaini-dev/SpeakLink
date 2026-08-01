"""Permanently deleting an HQ User that HAS history - a tombstone, not a row removal.

``deletion_safety.delete_user_if_unused`` already covers the easy case: an
account nothing has ever referenced can be erased outright. This module
covers the opposite case a SUPER ADMIN genuinely needs: an account that
started broadcasts and appears as the actor in audit history, removed
permanently from every operational surface while all of that history stays
exactly as readable as it was.

WHY A TOMBSTONE, NOT A ROW REMOVAL

``broadcast_sessions.started_by``, ``user_permission_overrides.user_id``,
``permission_audit_events.actor_user_id`` and
``store_scope_audit_events.actor_user_id`` all reference ``hq_users.id``.
Deleting the row would either violate those references or erase the record
of what somebody did in order to erase the fact that they existed. So the
row stays and is tombstoned instead:

* ``lifecycle_state`` becomes ``'deleted'`` - a state no lifecycle
  transition in ``user_lifecycle.py`` lists in its ``allowed_from``, so
  enable/disable/restore already refuse it without any new code;
* ``is_active`` is cleared, which every existing operational filter and the
  login path already check;
* ``session_version`` is bumped, which ends every live session immediately
  because ``auth.get_current_user`` compares it against the JWT on every
  request;
* ``password_hash`` is replaced with a value no password can produce, so
  authentication cannot succeed even if some future code path forgets one
  of the checks above;
* ``username`` is left exactly as it was. That is what keeps history
  readable ("started by raj") and is also what keeps the name reserved -
  the UNIQUE index still holds it, so it cannot be silently reused by a
  different person.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine


class UserDeletionRefused(RuntimeError):
    """The account was not tombstoned. Never carries a credential or a hash."""


@dataclass
class UserTombstoneResult:
    user_id: int
    username: str
    role: str
    deleted_at: str
    history_counts: dict


#: Tables whose rows keep pointing at this account after it is gone. Counted
#: for the audit record, never deleted.
_HISTORY_TABLES = {
    "broadcast_sessions": "started_by",
    "permission_audit_events": "actor_user_id",
    "store_scope_audit_events": "actor_user_id",
    "user_permission_overrides": "user_id",
    "user_store_scope": "user_id",
}


def ensure_user_deletion_schema(engine: Engine) -> None:
    """Additive and idempotent, and dialect-portable.

    Deliberately NOT written with ``PRAGMA table_info`` or ``AUTOINCREMENT``:
    both are SQLite-only, and this migration has to run unchanged against
    production PostgreSQL. SQLAlchemy's Inspector answers the "does this
    column exist" question on either engine, and the table is created from
    the same portable Core definition PostgreSQL uses.
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("hq_users")}
    with engine.begin() as connection:
        # ALTER TABLE ... ADD COLUMN with a plain nullable column is standard
        # SQL and behaves the same on both engines.
        if "deleted_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE hq_users ADD COLUMN deleted_at VARCHAR(40)")
        if "deleted_by" not in columns:
            connection.exec_driver_sql("ALTER TABLE hq_users ADD COLUMN deleted_by INTEGER")

    # One portable definition, created only if absent, on whichever engine.
    import postgres_schema
    postgres_schema.user_deletion_events.create(bind=engine, checkfirst=True)


def _count_history(connection, user_id: int) -> dict:
    counts = {}
    for table, column in _HISTORY_TABLES.items():
        try:
            counts[table] = int(connection.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :i"),
                {"i": user_id}).scalar() or 0)
        except Exception:
            # An older database without that table. Reported as unknown
            # rather than silently as zero.
            counts[table] = None
    return counts


def permanently_delete_user_with_history(
    engine: Engine, *, user_id: int, typed_confirmation: str,
    actor_user_id: int,
) -> UserTombstoneResult:
    """Tombstone an account forever. History stays; the account stops existing
    operationally and can never sign in again.

    Every check is re-evaluated on the connection holding the transaction, so
    nothing can change between the check and the commit.
    """
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, username, role, lifecycle_state FROM hq_users WHERE id = :i"),
            {"i": user_id}).first()
        if row is None:
            raise UserDeletionRefused("That account no longer exists.")

        if (row.lifecycle_state or "") == "deleted":
            raise UserDeletionRefused("That account was already permanently deleted.")

        if int(actor_user_id) == int(user_id):
            raise UserDeletionRefused(
                "You cannot permanently delete the account you are signed in as. "
                "Ask another SUPER ADMIN."
            )

        if typed_confirmation != row.username:
            raise UserDeletionRefused(
                "The typed confirmation did not match. Type the username exactly: "
                f"{row.username}"
            )

        if str(row.role).upper() in {"OWNER", "SUPER_ADMIN"}:
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM hq_users WHERE UPPER(role) IN "
                     "('OWNER','SUPER_ADMIN') AND is_active = 1 AND id <> :i "
                     "AND (lifecycle_state IS NULL OR lifecycle_state <> 'deleted')"),
                {"i": user_id}).scalar_one()
            if remaining == 0:
                raise UserDeletionRefused(
                    "This is the last active SUPER ADMIN. Deleting it would leave "
                    "nobody able to administer SpeakLink, and that cannot be undone "
                    "from inside the product. Promote another SUPER ADMIN first."
                )

        history = _count_history(connection, user_id)

        # The tombstone. username is deliberately NOT changed: it keeps
        # history readable and keeps the name reserved by the UNIQUE index.
        connection.execute(
            text(
                "UPDATE hq_users SET lifecycle_state = 'deleted', is_active = 0, "
                "session_version = session_version + 1, password_hash = :dead, "
                "deleted_at = :now, deleted_by = :actor WHERE id = :i"
            ),
            {"dead": "deleted-account-no-password-" + secrets.token_hex(16),
             "now": now, "actor": actor_user_id, "i": user_id},
        )

        import json as _json
        connection.execute(
            text(
                "INSERT INTO user_deletion_events "
                "(actor_user_id, user_id, username, role, history_counts_json, deleted_at) "
                "VALUES (:actor, :uid, :username, :role, :counts, :now)"
            ),
            {"actor": actor_user_id, "uid": user_id, "username": row.username,
             "role": row.role, "counts": _json.dumps(history), "now": now},
        )

        return UserTombstoneResult(
            user_id=user_id, username=row.username, role=row.role,
            deleted_at=now, history_counts=history,
        )


def list_user_deletion_events(engine: Engine, *, user_id: int) -> list[dict]:
    with engine.connect() as connection:
        try:
            rows = connection.execute(
                text("SELECT id, actor_user_id, user_id, username, role, "
                     "history_counts_json, deleted_at FROM user_deletion_events "
                     "WHERE user_id = :i ORDER BY id"),
                {"i": user_id}).all()
        except Exception:
            return []
    import json as _json
    return [
        {"id": r.id, "actor_user_id": r.actor_user_id, "user_id": r.user_id,
         "username": r.username, "role": r.role,
         "history_counts": _json.loads(r.history_counts_json),
         "deleted_at": r.deleted_at}
        for r in rows
    ]


__all__ = [
    "UserDeletionRefused",
    "UserTombstoneResult",
    "ensure_user_deletion_schema",
    "list_user_deletion_events",
    "permanently_delete_user_with_history",
]
