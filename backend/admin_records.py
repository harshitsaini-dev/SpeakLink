"""Archive and irreversible permanent deletion for Broadcast History and
System Logs, including bulk operations over a server-side filter.

TWO DIFFERENT KINDS OF DESTRUCTION, AND THE DIFFERENCE MATTERS

Everywhere else in SpeakLink, "permanent delete" means a tombstone: the row
survives so history that references it stays readable. That is right for a
Store, a User or a Device, because each of those is *referenced by* history.

Broadcast History and System Logs ARE the history. There is nothing further
downstream for them to protect, and an operator clearing years of log noise
genuinely means "remove these rows". So here, permanent delete is real:

* a broadcast session's rows go, and so do its ``broadcast_targets`` - the
  only table that references a session, and rows that are meaningless
  without their parent. Nothing else is touched: never a Store, never a
  User, never a Receiver Device;
* system_logs rows go outright.

WHAT SURVIVES, ALWAYS

Every destructive action writes one immutable row to
``admin_deletion_events`` recording the actor, the moment, how many rows
went, and the filter that selected them. That table is deliberately NOT a
system_logs row: a purge of system_logs must never be able to erase the
record of the purge. ``purge_system_logs`` therefore only ever deletes from
``system_logs``, and ``admin_deletion_events`` lives outside its reach.

The audit deliberately does NOT copy the deleted content. Retaining the
bodies would defeat the delete the operator asked for - the audit answers
"who removed how much, and by what filter", not "what did it say".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine


class AdminRecordError(RuntimeError):
    """Base class so no caller handles one refusal and misses another."""


@dataclass
class BulkResult:
    """Explicit about every row: silence about a skipped row is how a partial
    delete gets mistaken for a complete one."""
    requested: int = 0
    affected: int = 0
    skipped: int = 0
    failed: int = 0
    ids: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"requested": self.requested, "affected": self.affected,
                "skipped": self.skipped, "failed": self.failed}


def ensure_admin_records_schema(engine: Engine) -> None:
    """Additive, idempotent, dialect-portable (Inspector, never PRAGMA)."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    with engine.begin() as connection:
        session_columns = {c["name"] for c in inspector.get_columns("broadcast_sessions")}
        if "archived_at" not in session_columns:
            connection.exec_driver_sql(
                "ALTER TABLE broadcast_sessions ADD COLUMN archived_at VARCHAR(40)")
        log_columns = {c["name"] for c in inspector.get_columns("system_logs")}
        if "archived_at" not in log_columns:
            connection.exec_driver_sql(
                "ALTER TABLE system_logs ADD COLUMN archived_at VARCHAR(40)")
        # Structured entity columns for NEW log rows only. Deliberately never
        # back-filled: the existing messages are free text, and regexing them
        # into relationships would present guesses as facts. Filters built on
        # these are labelled as covering newer logs.
        for column, ddl in (
            ("actor_user_id", "ALTER TABLE system_logs ADD COLUMN actor_user_id INTEGER"),
            ("store_id", "ALTER TABLE system_logs ADD COLUMN store_id INTEGER"),
            ("device_public_id",
             "ALTER TABLE system_logs ADD COLUMN device_public_id VARCHAR(36)"),
        ):
            if column not in log_columns:
                connection.exec_driver_sql(ddl)

    import postgres_schema
    postgres_schema.admin_deletion_events.create(bind=engine, checkfirst=True)

    with engine.begin() as connection:
        # Indexes for the filter paths these features introduce. Deliberately
        # few: archived-state and time are what every list query narrows on.
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_broadcast_sessions_archived "
            "ON broadcast_sessions(archived_at)",
            "CREATE INDEX IF NOT EXISTS ix_system_logs_archived ON system_logs(archived_at)",
            "CREATE INDEX IF NOT EXISTS ix_system_logs_created ON system_logs(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_broadcast_sessions_created "
            "ON broadcast_sessions(created_at)",
        ):
            try:
                connection.exec_driver_sql(statement)
            except Exception:
                pass


def _audit(connection, *, actor_user_id: int, record_type: str, action: str,
           affected: int, filters: dict) -> None:
    connection.execute(
        text("INSERT INTO admin_deletion_events "
             "(actor_user_id, record_type, action, affected_count, filter_json, created_at) "
             "VALUES (:actor, :rtype, :action, :n, :f, :now)"),
        {"actor": actor_user_id, "rtype": record_type, "action": action,
         "n": affected, "f": json.dumps(filters, default=str),
         "now": datetime.now(timezone.utc).isoformat()},
    )


# ===========================================================================
# Broadcast History
# ===========================================================================
def archive_sessions(engine: Engine, *, session_ids: list[int],
                     actor_user_id: int, archived: bool = True) -> BulkResult:
    """Archive (or restore) sessions. Reversible; nothing is removed."""
    result = BulkResult(requested=len(session_ids))
    if not session_ids:
        return result
    stamp = datetime.now(timezone.utc).isoformat() if archived else None
    with engine.begin() as connection:
        for session_id in session_ids:
            row = connection.execute(
                text("SELECT id, archived_at FROM broadcast_sessions WHERE id = :i"),
                {"i": session_id}).first()
            if row is None:
                result.skipped += 1
                continue
            already = row.archived_at is not None
            if already == archived:
                result.skipped += 1
                continue
            connection.execute(
                text("UPDATE broadcast_sessions SET archived_at = :stamp WHERE id = :i"),
                {"stamp": stamp, "i": session_id})
            result.affected += 1
            result.ids.append(session_id)
        _audit(connection, actor_user_id=actor_user_id, record_type="broadcast_session",
               action="archived" if archived else "unarchived",
               affected=result.affected, filters={"session_ids": session_ids})
    return result


def delete_sessions_permanently(engine: Engine, *, session_ids: list[int],
                                actor_user_id: int,
                                filters: dict | None = None) -> BulkResult:
    """Really remove broadcast sessions and everything that belongs to them.

    Two tables reference a session, and both are PART of it rather than
    independent records: ``broadcast_targets`` (which Stores it addressed)
    and ``broadcast_store_leases`` (which Stores it held while live). Neither
    means anything without its session, so both go with it. Nothing else is
    touched - a Store, a User and a Receiver Device all outlive the campaign
    that mentioned them.

    THE LEASE TABLE WAS MISSED WHEN IT WAS ADDED

    ``broadcast_store_leases`` arrived with concurrent broadcasts and carries
    a foreign key to ``broadcast_sessions``. This function was not updated,
    so with ``PRAGMA foreign_keys=ON`` deleting any session that had actually
    RUN raised IntegrityError. It went unnoticed because a session that was
    only ever created - never started - holds no lease and deletes cleanly,
    which is precisely the shape most tests had.

    Leases are deleted rather than preserved deliberately: they are runtime
    occupancy bookkeeping, not history. The administrative record of the
    deletion lives in ``admin_deletion_events``, which this cannot touch.
    """
    result = BulkResult(requested=len(session_ids))
    if not session_ids:
        return result
    with engine.begin() as connection:
        for session_id in session_ids:
            exists = connection.execute(
                text("SELECT 1 FROM broadcast_sessions WHERE id = :i"),
                {"i": session_id}).first()
            if exists is None:
                result.skipped += 1
                continue
            connection.execute(
                text("DELETE FROM broadcast_targets WHERE session_id = :i"),
                {"i": session_id})
            # Before the session row, or the foreign key refuses the delete.
            connection.execute(
                text("DELETE FROM broadcast_store_leases WHERE session_id = :i"),
                {"i": session_id})
            connection.execute(
                text("DELETE FROM broadcast_sessions WHERE id = :i"), {"i": session_id})
            result.affected += 1
            result.ids.append(session_id)
        _audit(connection, actor_user_id=actor_user_id, record_type="broadcast_session",
               action="deleted", affected=result.affected,
               filters=filters or {"session_ids": session_ids})
    return result


# ===========================================================================
# System Logs
# ===========================================================================
def archive_logs(engine: Engine, *, log_ids: list[int], actor_user_id: int,
                 archived: bool = True) -> BulkResult:
    result = BulkResult(requested=len(log_ids))
    if not log_ids:
        return result
    stamp = datetime.now(timezone.utc).isoformat() if archived else None
    with engine.begin() as connection:
        for log_id in log_ids:
            row = connection.execute(
                text("SELECT id, archived_at FROM system_logs WHERE id = :i"),
                {"i": log_id}).first()
            if row is None:
                result.skipped += 1
                continue
            if (row.archived_at is not None) == archived:
                result.skipped += 1
                continue
            connection.execute(
                text("UPDATE system_logs SET archived_at = :stamp WHERE id = :i"),
                {"stamp": stamp, "i": log_id})
            result.affected += 1
            result.ids.append(log_id)
        _audit(connection, actor_user_id=actor_user_id, record_type="system_log",
               action="archived" if archived else "unarchived",
               affected=result.affected, filters={"log_ids": log_ids})
    return result


def delete_logs_permanently(engine: Engine, *, log_ids: list[int],
                            actor_user_id: int,
                            filters: dict | None = None) -> BulkResult:
    """Really remove system_logs rows. Irreversible.

    Only ``system_logs`` is touched. ``admin_deletion_events`` is a separate
    table precisely so a log purge can never erase the record of the purge.
    """
    result = BulkResult(requested=len(log_ids))
    if not log_ids:
        return result
    with engine.begin() as connection:
        for log_id in log_ids:
            exists = connection.execute(
                text("SELECT 1 FROM system_logs WHERE id = :i"), {"i": log_id}).first()
            if exists is None:
                result.skipped += 1
                continue
            connection.execute(text("DELETE FROM system_logs WHERE id = :i"), {"i": log_id})
            result.affected += 1
            result.ids.append(log_id)
        _audit(connection, actor_user_id=actor_user_id, record_type="system_log",
               action="deleted", affected=result.affected,
               filters=filters or {"log_ids": log_ids})
    return result


def list_admin_deletion_events(engine: Engine, *, record_type: str | None = None,
                               limit: int = 200) -> list[dict]:
    where = "WHERE record_type = :rtype " if record_type else ""
    params = {"rtype": record_type} if record_type else {}
    with engine.connect() as connection:
        try:
            rows = connection.execute(
                text("SELECT id, actor_user_id, record_type, action, affected_count, "
                     f"filter_json, created_at FROM admin_deletion_events {where}"
                     "ORDER BY id DESC LIMIT :limit"),
                {**params, "limit": limit}).all()
        except Exception:
            return []
    return [
        {"id": r.id, "actor_user_id": r.actor_user_id, "record_type": r.record_type,
         "action": r.action, "affected_count": r.affected_count,
         "filters": json.loads(r.filter_json), "created_at": r.created_at}
        for r in rows
    ]


__all__ = [
    "AdminRecordError",
    "BulkResult",
    "archive_logs",
    "archive_sessions",
    "delete_logs_permanently",
    "delete_sessions_permanently",
    "ensure_admin_records_schema",
    "list_admin_deletion_events",
]
