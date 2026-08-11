"""A Store's life: active, switched off, retired - and never actually deleted.

A Store owns Receiver Devices, broadcast sessions, targets, receiver events and
log lines. Deleting the row would either orphan all of that or cascade it away,
and both destroy the only record of what was announced where and when. So
"delete" means **archive** here: the row stays, the history stays readable, and
the Store simply stops being somewhere you can broadcast to.

Three states, and the distinction between the last two is the point:

``active``
    Normal.

``disabled``
    Switched off and expected back - a shop closed for refurbishment, a Receiver
    being replaced. An ordinary administrator turns it on again.

``archived``
    Retired. Deliberately **not** reversible through the ordinary enable action,
    because "Enable" is a small button and un-retiring a Store is not a small
    decision. Restoring puts it back to ``disabled``, never straight to
    ``active``, so somebody has to look at its Devices before it can broadcast.

**``is_active`` is kept in lockstep rather than replaced.** Broadcast targeting,
enrolment and Receiver authentication all already filter on it. Keeping it
correct means none of those had to learn about archiving, and - more usefully -
none of them can accidentally *miss* it. A test asserts the two never disagree.

The schema change is one additive column with a backfill, so it is safe to run
at startup on every boot rather than in a maintenance window.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine


ACTIVE = "active"
DISABLED = "disabled"
ARCHIVED = "archived"
#: A tombstone, not a fourth ordinary state. A DELETED Store never appears in
#: ``allowed_from`` for disable/enable/archive/restore below, so every one of
#: those transitions already refuses it (409) without any extra code here -
#: the state machine itself is the guard. See ``store_deletion.py`` for the
#: one-way transition into this state.
DELETED = "deleted"
KNOWN_STATES = (ACTIVE, DISABLED, ARCHIVED, DELETED)

MAX_STORE_CODE_LENGTH = 50
MAX_STORE_NAME_LENGTH = 200
MAX_LOCATION_LENGTH = 100

#: Printable, no whitespace. A code ends up in URLs, filenames and spoken
#: instructions over a phone, so a space in one is a support call.
_STORE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class StoreLifecycleError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


class StoreNotFoundError(StoreLifecycleError):
    """No Store with that identifier."""


class StoreTransitionRefused(StoreLifecycleError):
    """The move is not allowed from where this Store currently is."""


class StoreNotRestorableError(StoreLifecycleError):
    """Only an archived Store can be restored."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_store_lifecycle_schema(engine: Engine) -> None:
    """Add ``stores.lifecycle_state`` if it is missing, and backfill it.

    ``ALTER TABLE ... ADD COLUMN`` in SQLite rewrites no rows and rebuilds no
    indexes, so this is cheap and safe on a live database. The backfill reads
    the existing ``is_active`` flag: an upgrade must not decide that every Store
    is suddenly archived, and it must not decide that a Store somebody switched
    off is active again.
    """
    # Inspector, never PRAGMA. PRAGMA is SQLite-only, and this migration runs
    # at EVERY start-up - so on PostgreSQL the PRAGMA form did not fail some
    # later feature, it stopped HQ from booting at all. The Inspector answers
    # the same "does this column exist" question on either engine.
    from sqlalchemy import inspect

    columns = {c["name"] for c in inspect(engine).get_columns("stores")}
    with engine.begin() as connection:
        if "lifecycle_state" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE stores ADD COLUMN lifecycle_state VARCHAR(20)"
            )
        if "deleted_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE stores ADD COLUMN deleted_at VARCHAR(40)")
        if "deleted_by" not in columns:
            connection.exec_driver_sql("ALTER TABLE stores ADD COLUMN deleted_by INTEGER")
        # Backfill anything NULL - both a fresh column and a row inserted by an
        # older code path that did not know about this one.
        connection.execute(
            text(
                # :true_flag rather than the literal 1 - is_active is BOOLEAN, and
                # PostgreSQL refuses to compare it against an integer. This
                # migration runs at every start-up, so on PostgreSQL the literal
                # would have stopped HQ from booting at all.
                "UPDATE stores SET lifecycle_state = CASE "
                "WHEN is_active = :true_flag THEN :active ELSE :disabled END "
                "WHERE lifecycle_state IS NULL"
            ),
            {"active": ACTIVE, "disabled": DISABLED, "true_flag": True},
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_stores_lifecycle_state "
            "ON stores(lifecycle_state)"
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _bounded(value: object, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise StoreLifecycleError(f"{field} must be text")
    cleaned = value.strip()
    if not cleaned:
        raise StoreLifecycleError(f"{field} is required")
    if len(cleaned) > limit:
        raise StoreLifecycleError(f"{field} must be at most {limit} characters")
    if not cleaned.isprintable():
        raise StoreLifecycleError(f"{field} must not contain control characters")
    return cleaned


def validate_store_code(value: object) -> str:
    """Trimmed, bounded, and without whitespace.

    A Store code is read out over a phone, typed into a till and pasted into
    URLs. Whitespace inside one is a support call nobody enjoys.
    """
    cleaned = _bounded(value, field="store_code", limit=MAX_STORE_CODE_LENGTH)
    if _STORE_CODE_PATTERN.fullmatch(cleaned) is None:
        raise StoreLifecycleError(
            "a Store code may contain only letters, digits, dot, dash and "
            "underscore, and must start with a letter or digit"
        )
    return cleaned


def validate_store_name(value: object) -> str:
    return _bounded(value, field="store_name", limit=MAX_STORE_NAME_LENGTH)


def validate_location(value: object, *, field: str) -> str:
    return _bounded(value, field=field, limit=MAX_LOCATION_LENGTH)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def lifecycle_state(db, store_id: int) -> str:
    """The Store's state, derived from ``is_active`` if the column is unset.

    The fallback matters on a database where the migration has not run yet: a
    caller asking "is this archived?" must get a defensible answer rather than
    ``None``.
    """
    from models import Store

    store = db.query(Store).filter(Store.id == store_id).first()
    if store is None:
        raise StoreNotFoundError("Store not found")
    state = getattr(store, "lifecycle_state", None)
    if state in KNOWN_STATES:
        return state
    return ACTIVE if store.is_active else DISABLED


def _write_audit(db, message: str) -> None:
    """A bounded, secret-free line. Never a token, never a hash."""
    from models import SystemLog

    try:
        db.add(SystemLog(level="warn", message=message[:500]))
        db.commit()
    except Exception:
        db.rollback()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
def _transition(
    session_factory,
    *,
    store_id: int,
    actor_user_id: int | None,
    target_state: str,
    allowed_from: tuple[str, ...],
    live_store_ids: set[int] | None = None,
    refuse_live: bool = True,
    verb: str,
) -> dict:
    from models import Store

    with session_factory() as db:
        store = db.query(Store).filter(Store.id == store_id).first()
        if store is None:
            raise StoreNotFoundError("Store not found")

        current = lifecycle_state(db, store_id)
        if current == target_state and target_state != ARCHIVED:
            # Enabling an active Store, or disabling a disabled one, is what a
            # double-click looks like. It must not be an error.
            return {"store_id": store_id, "state": current, "changed": False}

        if current not in allowed_from:
            raise StoreTransitionRefused(
                f"a Store that is {current!r} cannot be {verb}; "
                f"only {', '.join(allowed_from)} may be"
            )

        if refuse_live and live_store_ids and store_id in live_store_ids:
            # Pulling a Store out from under a running announcement is the kind
            # of thing only noticed by the people standing in it.
            raise StoreTransitionRefused(
                "this Store is part of a live broadcast; stop the broadcast first"
            )

        store.lifecycle_state = target_state
        store.is_active = target_state == ACTIVE
        store.updated_at = datetime.now(timezone.utc)
        db.commit()

        _write_audit(
            db,
            f"store_{verb} store_id={store_id} code={store.store_code} "
            f"from={current} to={target_state} by_user_id={actor_user_id}",
        )
        return {"store_id": store_id, "state": target_state, "changed": True}


def disable_store(session_factory, *, store_id, actor_user_id, live_store_ids=None) -> dict:
    """Switch a Store off. Reversible, and its history is untouched."""
    return _transition(
        session_factory, store_id=store_id, actor_user_id=actor_user_id,
        target_state=DISABLED, allowed_from=(ACTIVE,),
        live_store_ids=live_store_ids, verb="disabled",
    )


def enable_store(session_factory, *, store_id, actor_user_id, live_store_ids=None) -> dict:
    """Switch a Store back on.

    An archived Store is deliberately not reachable from here. Restoring one is
    a separate, more privileged action for a reason.
    """
    return _transition(
        session_factory, store_id=store_id, actor_user_id=actor_user_id,
        target_state=ACTIVE, allowed_from=(DISABLED,),
        live_store_ids=live_store_ids, refuse_live=False, verb="enabled",
    )


def archive_store(session_factory, *, store_id, actor_user_id, live_store_ids=None) -> dict:
    """Retire a Store without deleting anything.

    No row is removed, no credential is rotated and no Device record is touched.
    The Store simply becomes inactive, which is what every existing path -
    broadcast targeting, enrolment, Receiver authentication - already checks.
    """
    return _transition(
        session_factory, store_id=store_id, actor_user_id=actor_user_id,
        target_state=ARCHIVED, allowed_from=(ACTIVE, DISABLED),
        live_store_ids=live_store_ids, verb="archived",
    )


def restore_store(session_factory, *, store_id, actor_user_id) -> dict:
    """Bring an archived Store back - to ``disabled``, never straight to active.

    Somebody has to look at its Devices before it can broadcast again. No
    credential is regenerated and no Device is promoted: a restore that quietly
    did either would be a different operation wearing this one's name.
    """
    from models import Store

    with session_factory() as db:
        if db.query(Store).filter(Store.id == store_id).first() is None:
            raise StoreNotFoundError("Store not found")
        if lifecycle_state(db, store_id) != ARCHIVED:
            raise StoreNotRestorableError("only an archived Store can be restored")

    return _transition(
        session_factory, store_id=store_id, actor_user_id=actor_user_id,
        target_state=DISABLED, allowed_from=(ARCHIVED,),
        refuse_live=False, verb="restored",
    )


__all__ = [
    "ACTIVE",
    "ARCHIVED",
    "DISABLED",
    "KNOWN_STATES",
    "StoreLifecycleError",
    "StoreNotFoundError",
    "StoreNotRestorableError",
    "StoreTransitionRefused",
    "archive_store",
    "disable_store",
    "enable_store",
    "ensure_store_lifecycle_schema",
    "lifecycle_state",
    "restore_store",
    "validate_location",
    "validate_store_code",
    "validate_store_name",
]
