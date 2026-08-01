"""Permanently deleting a Store that HAS history - a tombstone, not a row removal.

``deletion_safety.delete_store_if_unused`` already covers the easy case: a
Store nothing has ever referenced can be erased outright. This module covers
the opposite, harder case a SUPER ADMIN sometimes genuinely needs: a Store
with real Broadcast Targets, Receiver Devices, enrollment codes or Receiver
events, that must disappear from every operational surface forever, while
every one of those historical rows stays exactly as readable as it was.

WHY A TOMBSTONE, NOT A CASCADE DELETE

Every foreign key that points at ``stores.id`` (``receiver_devices``,
``broadcast_targets``, ``receiver_events``, ``receiver_enrollment_codes``, and
``receiver_credential_events`` via its own SET NULL) exists because losing
that row would mean losing the only record of what was announced where, on
which Device, at whose command. Physically deleting the Store row would
either violate those foreign keys outright (``receiver_devices`` is
``ON DELETE RESTRICT`` - it would simply refuse) or silently erase the
Store's name from anything using ``ON DELETE SET NULL``. Neither is
acceptable, so the Store row itself is never removed.

Instead the row is tombstoned: ``lifecycle_state`` moves to ``'deleted'``
(store_lifecycle.DELETED), which is not in any transition's ``allowed_from``
list, so disable/enable/archive/restore already refuse it automatically -
the state machine is the guard, not new code. ``is_active`` is cleared so
every existing operational filter (broadcast targeting, enrolment,
Receiver authentication) already excludes it without having learned
anything new, exactly like ``ARCHIVED`` before it. ``deleted_at``/
``deleted_by`` record who and when. The Store's ``store_code`` and
``store_name`` are left exactly as they were - that identity is what makes
old Broadcast History readable ("AYUSHK" rather than a blank name or a
raw id) - and are never handed out to a new Store afterward.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from deletion_safety import STORE_DEPENDENCY_TABLES, _count_dependencies


class StoreDeletionRefused(RuntimeError):
    """The Store was not tombstoned. Never carries a credential or a hash."""


@dataclass
class TombstoneResult:
    store_id: int
    store_code: str
    store_name: str
    deleted_at: str
    dependency_counts: dict
    device_public_ids: list
    enrollment_codes_revoked: int
    credentials_revoked: int


def permanently_delete_store_with_history(
    engine: Engine, *, store_id: int, typed_confirmation: str,
    actor_user_id: int, live_store_ids: set[int] | None = None,
) -> TombstoneResult:
    """Tombstone a Store forever. History stays; the Store stops being usable.

    ``typed_confirmation`` must equal the Store's exact code - the same
    "type the identifier of the thing being destroyed" rule every other
    permanent action in this codebase already uses. Every check below is
    re-evaluated on the connection holding the transaction, so nothing can be
    enrolled, targeted or re-broadcast between the check and the commit.
    """
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        row = connection.execute(
            text("SELECT id, store_code, store_name, lifecycle_state, is_active "
                 "FROM stores WHERE id = :i"),
            {"i": store_id},
        ).first()
        if row is None:
            raise StoreDeletionRefused("That Store no longer exists.")

        if typed_confirmation != row.store_code:
            raise StoreDeletionRefused(
                f"The typed confirmation did not match. Type the Store code "
                f"exactly: {row.store_code}"
            )

        if row.lifecycle_state == "deleted":
            raise StoreDeletionRefused("This Store was already permanently deleted.")

        if live_store_ids and store_id in live_store_ids:
            raise StoreDeletionRefused(
                "This Store is part of a live broadcast; stop the broadcast first."
            )
        # A DB-level check, independent of (and in addition to) the in-memory
        # live_store_ids guard above: a 'live' session row targeting this
        # Store means an announcement is actually in flight, not merely a
        # draft that was created and never started. A 'pending' session by
        # itself is just an unstarted draft - possibly abandoned months ago -
        # and must not block deletion forever.
        live_session = connection.execute(
            text(
                "SELECT 1 FROM broadcast_targets bt "
                "JOIN broadcast_sessions bs ON bs.id = bt.session_id "
                "WHERE bt.store_id = :store_id AND bs.status = 'live'"
            ),
            {"store_id": store_id},
        ).first()
        if live_session:
            raise StoreDeletionRefused(
                "This Store is part of a live broadcast; stop the broadcast first."
            )

        summary = _count_dependencies(
            connection, STORE_DEPENDENCY_TABLES, lambda _t: "store_id", store_id)

        # Every Receiver Device this Store owns: taken permanently out of
        # rotation (retired), never deleted - its identity and every
        # credential/enrolment event it ever recorded stay exactly as they
        # were, satisfying "preserve Device identity/history" while making
        # sure none of them can still serve a broadcast.
        device_ids = [r[0] for r in connection.execute(
            text("SELECT id FROM receiver_devices WHERE store_id = :i"),
            {"i": store_id},
        ).all()]
        device_public_ids = [r[0] for r in connection.execute(
            text("SELECT public_id FROM receiver_devices WHERE store_id = :i"),
            {"i": store_id},
        ).all()]
        if device_ids:
            connection.execute(
                text(
                    "UPDATE receiver_devices SET status = 'retired', "
                    "disabled_at = COALESCE(disabled_at, :now), updated_at = :now "
                    "WHERE store_id = :store_id AND status != 'retired'"
                ),
                {"now": now, "store_id": store_id},
            )
            has_primary_table = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='receiver_store_primary_device'"
            ).first()
            if has_primary_table:
                connection.execute(
                    text(
                        "DELETE FROM receiver_store_primary_device "
                        "WHERE store_id = :store_id"
                    ),
                    {"store_id": store_id},
                )

        # Every active/superseded credential on one of those Devices: revoked,
        # not deleted - the row and its full history remain, only its ability
        # to authenticate is removed.
        credentials_revoked = 0
        if device_ids:
            placeholders = ", ".join(f":d{i}" for i in range(len(device_ids)))
            params = {f"d{i}": device_id for i, device_id in enumerate(device_ids)}
            params["now"] = now
            credentials_revoked = connection.execute(
                text(
                    f"UPDATE receiver_credentials SET status = 'revoked', "
                    f"revoked_at = :now WHERE device_id IN ({placeholders}) "
                    f"AND status IN ('active', 'superseded')"
                ),
                params,
            ).rowcount

        # Every unredeemed, unexpired enrollment code for this Store: made
        # unusable by backdating its expiry to now - the row (and its
        # code_hash, which can never be reversed into the real code) stays
        # exactly as evidence of what was issued and when.
        enrollment_codes_revoked = connection.execute(
            text(
                "UPDATE receiver_enrollment_codes SET expires_at_epoch = :epoch "
                "WHERE store_id = :store_id AND redeemed_at_epoch IS NULL "
                "AND expires_at_epoch > :epoch"
            ),
            {"store_id": store_id, "epoch": datetime.now(timezone.utc).timestamp()},
        ).rowcount

        # The tombstone itself. store_code/store_name are left untouched -
        # that is what keeps old Broadcast History readable - and
        # receiver_token is rotated to a fresh, unusable value so no cached
        # Receiver credential can still present something that once worked.
        import uuid as _uuid
        connection.execute(
            text(
                "UPDATE stores SET lifecycle_state = 'deleted', is_active = 0, "
                "deleted_at = :now, deleted_by = :actor, receiver_token = :fresh_token, "
                "updated_at = :now WHERE id = :store_id"
            ),
            {"now": now, "actor": actor_user_id, "store_id": store_id,
             "fresh_token": _uuid.uuid4().hex},
        )

        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS store_deletion_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER NOT NULL,
                store_id INTEGER NOT NULL,
                store_code TEXT NOT NULL,
                store_name TEXT NOT NULL,
                dependency_counts_json TEXT NOT NULL,
                device_public_ids_json TEXT NOT NULL,
                enrollment_codes_revoked INTEGER NOT NULL,
                credentials_revoked INTEGER NOT NULL,
                deleted_at TEXT NOT NULL
            )
            """
        )
        import json as _json
        connection.execute(
            text(
                "INSERT INTO store_deletion_events "
                "(actor_user_id, store_id, store_code, store_name, "
                "dependency_counts_json, device_public_ids_json, "
                "enrollment_codes_revoked, credentials_revoked, deleted_at) "
                "VALUES (:actor, :store_id, :code, :name, :counts, :devices, "
                ":codes_revoked, :creds_revoked, :now)"
            ),
            {"actor": actor_user_id, "store_id": store_id, "code": row.store_code,
             "name": row.store_name, "counts": _json.dumps(summary.counts),
             "devices": _json.dumps(device_public_ids),
             "codes_revoked": enrollment_codes_revoked,
             "creds_revoked": credentials_revoked, "now": now},
        )

        violations = connection.execute(text("PRAGMA foreign_key_check")).all()
        if violations:
            # Should be unreachable - nothing was deleted, only updated - but
            # a change that would break referential integrity is refused
            # rather than committed either way.
            raise StoreDeletionRefused(
                "This Store could not be safely tombstoned without breaking "
                "referential integrity."
            )

        return TombstoneResult(
            store_id=store_id, store_code=row.store_code, store_name=row.store_name,
            deleted_at=now, dependency_counts=summary.counts,
            device_public_ids=device_public_ids,
            enrollment_codes_revoked=enrollment_codes_revoked,
            credentials_revoked=credentials_revoked,
        )


def list_store_deletion_events(engine: Engine, *, store_id: int) -> list[dict]:
    with engine.connect() as connection:
        table_exists = connection.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='store_deletion_events'"
        ).first()
        if not table_exists:
            return []
        rows = connection.execute(
            text(
                "SELECT id, actor_user_id, store_id, store_code, store_name, "
                "dependency_counts_json, device_public_ids_json, "
                "enrollment_codes_revoked, credentials_revoked, deleted_at "
                "FROM store_deletion_events WHERE store_id = :i ORDER BY id"
            ),
            {"i": store_id},
        ).all()
    import json as _json
    return [
        {"id": r.id, "actor_user_id": r.actor_user_id, "store_id": r.store_id,
         "store_code": r.store_code, "store_name": r.store_name,
         "dependency_counts": _json.loads(r.dependency_counts_json),
         "device_public_ids": _json.loads(r.device_public_ids_json),
         "enrollment_codes_revoked": r.enrollment_codes_revoked,
         "credentials_revoked": r.credentials_revoked, "deleted_at": r.deleted_at}
        for r in rows
    ]


__all__ = [
    "StoreDeletionRefused",
    "TombstoneResult",
    "list_store_deletion_events",
    "permanently_delete_store_with_history",
]
