"""Permanently deleting a Receiver Device that HAS history - a tombstone.

``deletion_safety.delete_device_if_unused`` removes only a Device with zero
credentials and zero credential events. Enrolment is what creates both, so
in practice it refuses every Device that was ever really used. This module
covers the case a SUPER ADMIN actually needs.

WHY THE ROW SURVIVES

``receiver_credentials.device_id`` is ``ON DELETE RESTRICT`` and
``receiver_credential_events`` is the only record of what a Store's
Receiver did and when. Deleting the row would either be refused by the
database or erase that evidence.

WHY status BECOMES 'retired' RATHER THAN A NEW 'deleted' VALUE

``receiver_devices.status`` carries a CHECK constraint allowing exactly
``active``/``disabled``/``retired``. Adding a fourth value would require
rebuilding the table on SQLite - a destructive-shaped migration for a
cosmetic gain. ``retired`` is already the terminal state the Receiver
authentication path refuses (``receiver_auth_service`` compares the
Device's status against ``active``/``disabled`` only), so a tombstoned
Device cannot reconnect without any new check being written. The extra
``deleted_at`` column is what distinguishes irreversibly deleted from
merely revoked, and is what the operational views filter on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine


class DeviceDeletionRefused(RuntimeError):
    """The Device was not tombstoned. Never carries a credential or a hash."""


@dataclass
class DeviceTombstoneResult:
    device_id: int
    public_id: str
    store_id: int
    display_name: str
    deleted_at: str
    credentials_revoked: int
    was_primary: bool


def ensure_device_deletion_schema(engine: Engine) -> None:
    """Additive, idempotent and dialect-portable - Inspector, never PRAGMA."""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    try:
        columns = {c["name"] for c in inspector.get_columns("receiver_devices")}
    except Exception:
        # Phase-one schema not present yet; nothing to add to.
        return
    with engine.begin() as connection:
        if "deleted_at" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE receiver_devices ADD COLUMN deleted_at VARCHAR(40)")
        if "deleted_by" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE receiver_devices ADD COLUMN deleted_by INTEGER")

    import postgres_schema
    postgres_schema.device_deletion_events.create(bind=engine, checkfirst=True)


def permanently_delete_device_with_history(
    engine: Engine, *, public_id: str, typed_confirmation: str,
    actor_user_id: int,
) -> DeviceTombstoneResult:
    """Tombstone a Device forever. History stays; the Device can never
    authenticate, reconnect, or be restored."""
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON") \
            if engine.dialect.name == "sqlite" else None
        row = connection.execute(
            text("SELECT id, public_id, store_id, display_name, status, deleted_at "
                 "FROM receiver_devices WHERE public_id = :p"),
            {"p": public_id}).first()
        if row is None:
            raise DeviceDeletionRefused("That Receiver Device no longer exists.")
        if row.deleted_at is not None:
            raise DeviceDeletionRefused("That Device was already permanently deleted.")
        if typed_confirmation != row.public_id:
            raise DeviceDeletionRefused(
                "The typed confirmation did not match. Type the Device id exactly: "
                f"{row.public_id}"
            )

        was_primary = bool(connection.execute(
            text("SELECT 1 FROM receiver_store_primary_device WHERE device_id = :d"),
            {"d": row.id}).first())
        if was_primary:
            # Nothing is promoted in its place: a silent failover would put
            # the announcement on a computer nobody has confirmed is plugged
            # into the amplifier. Same rule _set_status already follows.
            connection.execute(
                text("DELETE FROM receiver_store_primary_device WHERE device_id = :d"),
                {"d": row.id})

        credentials_revoked = connection.execute(
            text("UPDATE receiver_credentials SET status = 'revoked', revoked_at = :now "
                 "WHERE device_id = :d AND status IN ('active', 'superseded')"),
            {"now": now, "d": row.id}).rowcount

        connection.execute(
            text("UPDATE receiver_devices SET status = 'retired', "
                 "disabled_at = COALESCE(disabled_at, :now), deleted_at = :now, "
                 "deleted_by = :actor, updated_at = :now WHERE id = :d"),
            {"now": now, "actor": actor_user_id, "d": row.id})

        connection.execute(
            text("INSERT INTO device_deletion_events "
                 "(actor_user_id, device_id, public_id, store_id, display_name, "
                 "credentials_revoked, was_primary, deleted_at) "
                 "VALUES (:actor, :d, :p, :s, :name, :creds, :primary, :now)"),
            {"actor": actor_user_id, "d": row.id, "p": row.public_id,
             "s": row.store_id, "name": row.display_name,
             "creds": credentials_revoked, "primary": bool(was_primary), "now": now})

        return DeviceTombstoneResult(
            device_id=row.id, public_id=row.public_id, store_id=row.store_id,
            display_name=row.display_name, deleted_at=now,
            credentials_revoked=credentials_revoked, was_primary=was_primary,
        )


def list_device_deletion_events(engine: Engine, *, public_id: str) -> list[dict]:
    with engine.connect() as connection:
        try:
            rows = connection.execute(
                text("SELECT id, actor_user_id, device_id, public_id, store_id, "
                     "display_name, credentials_revoked, was_primary, deleted_at "
                     "FROM device_deletion_events WHERE public_id = :p ORDER BY id"),
                {"p": public_id}).all()
        except Exception:
            return []
    return [
        {"id": r.id, "actor_user_id": r.actor_user_id, "device_id": r.device_id,
         "public_id": r.public_id, "store_id": r.store_id,
         "display_name": r.display_name, "credentials_revoked": r.credentials_revoked,
         "was_primary": bool(r.was_primary), "deleted_at": r.deleted_at}
        for r in rows
    ]


__all__ = [
    "DeviceDeletionRefused",
    "DeviceTombstoneResult",
    "ensure_device_deletion_schema",
    "list_device_deletion_events",
    "permanently_delete_device_with_history",
]
