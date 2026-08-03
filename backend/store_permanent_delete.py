"""Permanently deleting a Store - the row really goes, the history stays.

WHAT WAS WRONG BEFORE

``store_deletion.py`` called this "permanent deletion" but kept the row and
tombstoned it. Its own docstring says the Store's ``store_code`` is "left
exactly as it was ... and never handed out to a new Store afterward", and that
is precisely what the operator hit:

    Permanently delete AYUSHK
    Add Store -> store_code AYUSHK  ->  "store_code already exists"

A Store that still occupies the code namespace has not been deleted; it has
been hidden. This is the same defect the User feature fixed, in a second
place, and the fix has the same shape.

THE ID-REUSE TRAP

``stores.id`` is ``INTEGER PRIMARY KEY`` with **no AUTOINCREMENT**, so SQLite
assigns ``max(id) + 1``. In the live database the tombstones are ids 58, 59
and 60, and 60 IS the maximum - so deleting it and adding any Store would have
handed the new Store id 60, along with every history row still pointing there.
``sqlite_schema_surgery.make_ids_never_reused`` closes that; the snapshots
below close the other half.

A REUSED STORE CODE IS NOT THE OLD STORE

That is the whole security property. A new Store with a deleted Store's code
must inherit nothing operational: no Receiver Device, no credential, no
primary-device assignment, no enrolment code, no Store Scope, no lease, and
above all not the old ``receiver_token`` - a Receiver holding the old Store's
credential must not be able to authenticate as the new one.

WHAT IS DELETED, DETACHED AND KEPT

Deleted with the Store, because it is live operational state:

    broadcast_store_leases       runtime occupancy
    user_store_scope             which operators were limited to it
    receiver_store_primary_device  already ON DELETE CASCADE

Detached and neutralised, because the Devices are historical records but must
never serve the replacement Store:

    receiver_devices             credentials revoked, status retired,
                                 store_id nulled, code snapshotted
    receiver_credentials         revoked (already ON DELETE SET NULL by store)

Kept with the pointer nulled and the identity snapshotted:

    broadcast_targets            what was announced where
    receiver_events              Receiver telemetry history
    receiver_enrollment_codes    who was invited to enrol

Kept untouched - no foreign key, and they already carry their own snapshots:

    store_deletion_events, device_deletion_events, system_logs

WHAT THIS MODULE DOES NOT DO

It does not disable foreign keys to delete anything. The schema migration uses
the documented SQLite rebuild (see sqlite_schema_surgery), and the deletion
itself runs with ``PRAGMA foreign_keys`` fully on.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from sqlite_schema_surgery import drop_not_null, make_ids_never_reused


class StorePermanentDeletionRefused(RuntimeError):
    """The Store was not deleted. Never carries a token or a credential.

    Owned by this module rather than imported, for the same reason the User
    equivalent is: a test fixture that reloads one module and not the other
    would otherwise leave two same-named classes, and a clean refusal would
    surface as an unhandled 500.
    """


#: History that keeps its rows and loses its pointer.
HISTORY_REFERENCES: dict[str, tuple[str, ...]] = {
    "broadcast_targets": ("store_id",),
    "receiver_events": ("store_id",),
    "receiver_enrollment_codes": ("store_id",),
    "receiver_devices": ("store_id",),
}

#: Live operational state. Dies with the Store.
LIVE_STATE_TABLES: dict[str, str] = {
    "broadcast_store_leases": "store_id",
    "user_store_scope": "store_id",
    "receiver_store_primary_device": "store_id",
}

#: Snapshot columns, so history can still name the Store after its row is gone
#: without joining to a code that a different Store may now be using.
SNAPSHOT_COLUMNS: dict[str, dict[str, str]] = {
    "broadcast_targets": {"store_code_snapshot": "VARCHAR(50)",
                          "store_name_snapshot": "VARCHAR(200)"},
    "receiver_devices": {"store_code_snapshot": "VARCHAR(50)"},
    "receiver_events": {"store_code_snapshot": "VARCHAR(50)"},
}


@dataclass
class StorePermanentDeletionResult:
    store_id: int
    store_code: str
    store_name: str
    deleted_at: str
    devices_detached: int = 0
    credentials_revoked: int = 0
    live_rows_removed: dict = field(default_factory=dict)
    history_rows_detached: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------
def ensure_store_permanent_delete_schema(engine: Engine) -> None:
    """Snapshot columns, non-reusable ids, and the nullability deletion needs.

    Additive and idempotent. Running it twice changes nothing the second time.
    """
    # The audit table. The OLD design created it lazily, on the first Store
    # deletion; this module writes to it unconditionally, so it has to exist
    # before any deletion is attempted rather than being conjured by the first
    # one. Created from the same portable Core definition PostgreSQL uses.
    import postgres_schema
    postgres_schema.store_deletion_events.create(bind=engine, checkfirst=True)

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    for table, columns in SNAPSHOT_COLUMNS.items():
        if table not in existing:
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as connection:
            for column, sql_type in columns.items():
                if column not in present:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")

    if "stores" in existing:
        make_ids_never_reused(engine, "stores")

    drop_not_null(engine, HISTORY_REFERENCES)


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------
def _active_lease_count(connection, store_id: int) -> int:
    try:
        return int(connection.execute(
            text("SELECT COUNT(*) FROM broadcast_store_leases "
                 "WHERE store_id = :i AND released_at IS NULL"),
            {"i": store_id}).scalar() or 0)
    except Exception:
        return 0


def _snapshot_history(connection, store_id: int, code: str, name: str) -> None:
    """Write the Store's identity onto its history rows, BEFORE detaching.

    A failure here aborts the transaction with the pointers still intact,
    rather than leaving history that has forgotten which Store it belonged to.
    """
    connection.execute(
        text("UPDATE broadcast_targets SET store_code_snapshot = :c, "
             "store_name_snapshot = :n WHERE store_id = :i"),
        {"c": code, "n": name, "i": store_id})
    for table in ("receiver_devices", "receiver_events"):
        try:
            connection.execute(
                text(f"UPDATE {table} SET store_code_snapshot = :c WHERE store_id = :i"),
                {"c": code, "i": store_id})
        except Exception:
            # An older database without that column. The pointer is still
            # nulled below; the identity remains recoverable from
            # store_deletion_events, which records the id, code and name.
            pass


def _neutralise_receiver_identity(connection, store_id: int) -> tuple[int, int]:
    """Make sure nothing that authenticated as the OLD Store can serve the new.

    Three separate things, because each could independently let a Receiver
    reach the replacement Store:

    * every credential for the Store's Devices is revoked;
    * every Device is marked retired, so no operational path treats it as live;
    * the Store's legacy ``receiver_token`` dies with the row, and the new
      Store is issued its own on creation.

    Returns (devices, credentials) affected.
    """
    now = datetime.now(timezone.utc).isoformat()
    device_ids = [r[0] for r in connection.execute(
        text("SELECT id FROM receiver_devices WHERE store_id = :i"), {"i": store_id}).all()]
    if not device_ids:
        return 0, 0

    # Credentials first, and by STATUS - `receiver_credentials` records
    # revocation as status='revoked' plus a timestamp, not as a bare
    # revoked_at. Setting only the timestamp would leave the row still reading
    # as active to every query that filters on status, which is precisely the
    # query that decides whether a Receiver may authenticate.
    placeholders = ", ".join(f":d{i}" for i in range(len(device_ids)))
    params = {f"d{i}": device_id for i, device_id in enumerate(device_ids)}
    params["now"] = now
    credentials = int(connection.execute(
        text(f"UPDATE receiver_credentials SET status = 'revoked', revoked_at = :now "
             f"WHERE device_id IN ({placeholders}) "
             f"AND status IN ('active', 'superseded')"),
        params).rowcount or 0)

    # Then the Devices. `disabled_at` is not optional decoration here:
    # ck_receiver_devices_disabled_state requires that a retired or disabled
    # Device HAS one, so setting the status alone makes the UPDATE fail
    # outright and roll the whole deletion back.
    connection.execute(
        text("UPDATE receiver_devices SET status = 'retired', "
             "disabled_at = COALESCE(disabled_at, :now), updated_at = :now "
             "WHERE store_id = :i AND status != 'retired'"),
        {"now": now, "i": store_id})

    # Unredeemed enrolment codes are made unusable by backdating their expiry.
    # The row stays as evidence of what was issued; the code_hash cannot be
    # reversed into the code itself.
    try:
        connection.execute(
            text("UPDATE receiver_enrollment_codes SET expires_at_epoch = 0 "
                 "WHERE store_id = :i AND redeemed_at_epoch IS NULL"),
            {"i": store_id})
    except Exception:
        pass

    return len(device_ids), credentials


def permanently_delete_store(
    engine: Engine, *, store_id: int, actor_user_id: int,
    typed_confirmation: str, live_store_ids: set[int] | None = None,
) -> StorePermanentDeletionResult:
    """Delete the Store for real, in one transaction.

    Order matters:

      1. read and validate the target
      2. refuse while a live broadcast holds it
      3. snapshot identity onto the history rows
      4. neutralise Receiver identity (revoke, retire)
      5. remove live operational state
      6. null the historical pointers
      7. DELETE the Store row
      8. record the administrative audit

    Any failure rolls the whole thing back: no half-deleted Store, no history
    that has lost its Store, no Devices left revoked beside a Store that still
    exists.
    """
    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, store_code, store_name, city, region, lifecycle_state "
                 "FROM stores WHERE id = :i"), {"i": store_id}).first()
        if row is None:
            raise StorePermanentDeletionRefused("That Store no longer exists.")

        if typed_confirmation != row.store_code:
            raise StorePermanentDeletionRefused(
                "The typed confirmation did not match. Type the Store Code exactly: "
                f"{row.store_code}")

        # A Store cannot be removed from under a broadcast that is using it.
        # Deleting it here would silence somebody else's announcement as a side
        # effect of an administrative action, which is never the operator's
        # intent - they are asked to stop the broadcast first.
        # Two independent sources, because neither alone is sufficient. The
        # RUNTIME knows which Stores are receiving audio this instant; the
        # LEASE table is the durable record that survives a restart. Checking
        # only the leases would miss a broadcast the runtime is driving, which
        # is exactly the state a live announcement is in.
        if (live_store_ids and store_id in live_store_ids) or                 _active_lease_count(connection, store_id):
            raise StorePermanentDeletionRefused(
                "This Store is part of a broadcast that is on air right now. "
                "Stop that broadcast first, then delete the Store.")

        dependency_counts = {}
        for table, columns in HISTORY_REFERENCES.items():
            try:
                dependency_counts[table] = int(connection.execute(
                    text(f"SELECT COUNT(*) FROM {table} WHERE store_id = :i"),
                    {"i": store_id}).scalar() or 0)
            except Exception:
                dependency_counts[table] = None

        device_public_ids = []
        try:
            device_public_ids = [r[0] for r in connection.execute(
                text("SELECT public_id FROM receiver_devices WHERE store_id = :i"),
                {"i": store_id}).all()]
        except Exception:
            pass

        # ---- 3. identity onto the history --------------------------------
        _snapshot_history(connection, store_id, row.store_code, row.store_name)

        # ---- 4. the Receiver identity of the OLD Store --------------------
        devices, credentials = _neutralise_receiver_identity(connection, store_id)

        # ---- 5. live operational state ------------------------------------
        live_removed: dict[str, int] = {}
        for table, column in LIVE_STATE_TABLES.items():
            try:
                result = connection.execute(
                    text(f"DELETE FROM {table} WHERE {column} = :i"), {"i": store_id})
                live_removed[table] = int(result.rowcount or 0)
            except Exception:
                live_removed[table] = 0

        # ---- 6. history keeps its rows, loses its pointers ------------------
        detached: dict[str, int] = {}
        for table, columns in HISTORY_REFERENCES.items():
            for column in columns:
                try:
                    result = connection.execute(
                        text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :i"),
                        {"i": store_id})
                    detached[f"{table}.{column}"] = int(result.rowcount or 0)
                except Exception:
                    detached[f"{table}.{column}"] = 0

        # ---- 7. the row itself ---------------------------------------------
        connection.execute(text("DELETE FROM stores WHERE id = :i"), {"i": store_id})

        # ---- 8. the audit, which outlives the Store -------------------------
        # Written to the SAME table the tombstone used, so one query answers
        # "what happened to this Store" across both eras. Codes, names and
        # counts only - never the receiver_token or any credential.
        connection.execute(
            text("INSERT INTO store_deletion_events "
                 "(actor_user_id, store_id, store_code, store_name, "
                 "dependency_counts_json, device_public_ids_json, "
                 "enrollment_codes_revoked, credentials_revoked, deleted_at) "
                 "VALUES (:actor, :sid, :code, :name, :deps, :devices, :codes, "
                 ":creds, :now)"),
            {"actor": actor_user_id, "sid": store_id, "code": row.store_code,
             "name": row.store_name, "now": now,
             "deps": json.dumps({**dependency_counts, "row_deleted": True,
                                 "live_removed": live_removed,
                                 "history_detached": detached}),
             "devices": json.dumps(device_public_ids),
             "codes": dependency_counts.get("receiver_enrollment_codes") or 0,
             "creds": credentials})

    return StorePermanentDeletionResult(
        store_id=store_id, store_code=row.store_code, store_name=row.store_name,
        deleted_at=now, devices_detached=devices, credentials_revoked=credentials,
        live_rows_removed=live_removed, history_rows_detached=detached,
    )


# ---------------------------------------------------------------------------
# migration for Stores tombstoned by the previous design
# ---------------------------------------------------------------------------
def find_legacy_store_tombstones(engine: Engine) -> list[dict]:
    """Stores the OLD design marked deleted but left in the table.

    Deliberately narrow: ``lifecycle_state = 'deleted'`` only. An archived
    Store is restorable and must never be caught by this; an active or
    disabled Store obviously must not.
    """
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, store_code, store_name FROM stores "
                 "WHERE lifecycle_state = 'deleted'")).all()
    return [{"id": r.id, "store_code": r.store_code, "store_name": r.store_name}
            for r in rows]


def purge_legacy_store_tombstones(engine: Engine, *,
                                  actor_user_id: int | None = None) -> dict:
    """Finish the job the old design started, for Stores already marked deleted.

    An operator already decided these Stores were permanently deleted. What the
    old code could not do was release the Store Code, so this completes that
    decision rather than making a new one - which is why it touches ONLY
    ``lifecycle_state = 'deleted'``.

    Idempotent: once the rows are gone there is nothing left to match, and the
    audit row is written only when no row-deleted record already exists.
    """
    tombstones = find_legacy_store_tombstones(engine)
    if not tombstones:
        return {"purged": 0, "store_codes": []}

    purged: list[str] = []
    for entry in tombstones:
        now = datetime.now(timezone.utc).isoformat()
        with engine.begin() as connection:
            if _active_lease_count(connection, entry["id"]):
                # Should not be possible for a tombstone, but never silence a
                # live broadcast as a migration side effect.
                continue
            _snapshot_history(connection, entry["id"], entry["store_code"],
                              entry["store_name"])
            devices, credentials = _neutralise_receiver_identity(connection, entry["id"])
            live_removed = {}
            for table, column in LIVE_STATE_TABLES.items():
                try:
                    result = connection.execute(
                        text(f"DELETE FROM {table} WHERE {column} = :i"), {"i": entry["id"]})
                    live_removed[table] = int(result.rowcount or 0)
                except Exception:
                    live_removed[table] = 0
            detached = {}
            for table, columns in HISTORY_REFERENCES.items():
                for column in columns:
                    try:
                        result = connection.execute(
                            text(f"UPDATE {table} SET {column} = NULL WHERE {column} = :i"),
                            {"i": entry["id"]})
                        detached[f"{table}.{column}"] = int(result.rowcount or 0)
                    except Exception:
                        detached[f"{table}.{column}"] = 0

            connection.execute(text("DELETE FROM stores WHERE id = :i"), {"i": entry["id"]})

            already = connection.execute(
                text("SELECT COUNT(*) FROM store_deletion_events WHERE store_id = :i "
                     "AND dependency_counts_json LIKE '%\"row_deleted\": true%'"),
                {"i": entry["id"]}).scalar_one()
            if not already:
                connection.execute(
                    text("INSERT INTO store_deletion_events "
                         "(actor_user_id, store_id, store_code, store_name, "
                         "dependency_counts_json, device_public_ids_json, "
                         "enrollment_codes_revoked, credentials_revoked, deleted_at) "
                         "VALUES (:actor, :sid, :code, :name, :deps, '[]', 0, :creds, :now)"),
                    {"actor": actor_user_id if actor_user_id is not None else 0,
                     "sid": entry["id"], "code": entry["store_code"],
                     "name": entry["store_name"], "now": now, "creds": credentials,
                     "deps": json.dumps({"migrated_legacy_tombstone": True,
                                         "row_deleted": True,
                                         "live_removed": live_removed,
                                         "history_detached": detached})})
        purged.append(entry["store_code"])

    return {"purged": len(purged), "store_codes": purged}


__all__ = [
    "HISTORY_REFERENCES",
    "LIVE_STATE_TABLES",
    "SNAPSHOT_COLUMNS",
    "StorePermanentDeletionRefused",
    "StorePermanentDeletionResult",
    "ensure_store_permanent_delete_schema",
    "find_legacy_store_tombstones",
    "permanently_delete_store",
    "purge_legacy_store_tombstones",
]
