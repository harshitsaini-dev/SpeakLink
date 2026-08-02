"""Which live Broadcast Session holds which Store, enforced by the database.

THE RULE

A Store must never process two live broadcasts at once. Two announcements
over one set of speakers is worse than either alone: neither is intelligible,
and nobody in the Store can tell which one was supposed to be playing.

THE GUARD IS AN INDEX, NOT THIS MODULE

Everything here is a convenience in front of a PARTIAL UNIQUE INDEX on
``store_id WHERE released_at IS NULL``. That ordering is deliberate. An
in-memory set cannot carry this rule:

* it is lost on restart, so a crash mid-broadcast leaves no record of what was
  held and by whom;
* it is only as atomic as the code around it, so two requests that interleave
  between "is it free?" and "claim it" both believe they won.

So the check-then-claim sequence below is an optimisation that produces a good
error message. If it is ever wrong, the INSERT still fails and the transaction
still rolls back. The test suite asserts that directly, by bypassing this
module entirely and inserting a conflicting row by hand.

WHY RELEASED ROWS ARE KEPT

Releasing sets ``released_at`` rather than deleting, so a session's Store
history survives the session. That is why the uniqueness must be PARTIAL: a
plain ``UNIQUE(store_id)`` would allow a Store to be broadcast to exactly
once, ever, and the failure would surface days later as an unexplainable
refusal.

WHAT THIS DELIBERATELY DOES NOT DO

It never reports WHO holds a Store. A Broadcaster is told that a Store is
busy and nothing else - not the campaign, not the session, not the operator.
Ownership visibility is a separate, permission-gated feature, and an
identity-revealing field added here "for later" is one nobody would notice
becoming reachable.

It is never called per audio chunk. Reservations are written at session
lifecycle boundaries only; a database write per chunk would be thousands of
writes a minute per Store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

__all__ = [
    "BroadcastReservationError",
    "StoreBusyError",
    "StoreNotInScopeError",
    "active_busy_store_ids",
    "ensure_broadcast_lease_schema",
    "release_session_leases",
    "reserve_stores_for_session",
    "session_leased_store_ids",
]

#: The index that actually enforces the rule. Named so a schema dump makes the
#: intent obvious, and so a future migration can find it.
ACTIVE_LEASE_INDEX = "uq_broadcast_store_lease_active"


class BroadcastReservationError(RuntimeError):
    """A reservation could not be made. Never carries owner identity."""


class StoreBusyError(BroadcastReservationError):
    """One or more requested Stores are already held by a live broadcast.

    Carries ONLY Store ids, and only ones the caller was already entitled to
    see. There is deliberately no owner, campaign or session field: this
    exception is rendered straight into a Broadcaster-facing message, and an
    attribute that exists is an attribute that eventually gets logged.
    """

    def __init__(self, busy_store_ids) -> None:
        self.busy_store_ids = tuple(sorted(busy_store_ids))
        super().__init__(
            "One or more selected Stores are currently in use by another "
            "broadcast. Remove them and try again."
        )


class StoreNotInScopeError(BroadcastReservationError):
    """A requested Store lies outside the caller's Store Scope.

    Distinct from StoreBusyError on purpose. "Busy" tells a caller the Store
    exists and is in use; for a Store they may not see, even that is more than
    they are entitled to know.
    """

    def __init__(self, store_ids) -> None:
        self.store_ids = tuple(sorted(store_ids))
        super().__init__(
            "One or more selected Stores are not available to this account."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_broadcast_lease_schema(engine: Engine) -> None:
    """Create the table and its partial unique index. Additive, idempotent.

    The table comes from the portable definition in ``postgres_schema`` so
    there is only one description of its shape; the index is issued directly
    because the same statement is valid on both SQLite (3.8+) and PostgreSQL,
    and SQLAlchemy's dialect-neutral Index would not express the partial
    predicate identically on both.
    """
    import postgres_schema

    postgres_schema.broadcast_store_leases.create(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {ACTIVE_LEASE_INDEX} "
            "ON broadcast_store_leases (store_id) WHERE released_at IS NULL"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_broadcast_store_lease_session "
            "ON broadcast_store_leases (session_id)"
        )


def active_busy_store_ids(engine: Engine, *, scope=None) -> frozenset:
    """Every Store currently held by a live session.

    ``scope`` narrows the answer to what this caller may see. A scoped
    operator must not learn that an out-of-scope Store is busy - that is an
    existence disclosure about an estate they are not permitted to know
    anything about.
    """
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT store_id FROM broadcast_store_leases "
            "WHERE released_at IS NULL")).fetchall()
    busy = {row[0] for row in rows}
    if scope is not None:
        busy &= set(scope)
    return frozenset(busy)


def session_leased_store_ids(engine: Engine, *, session_id: int) -> frozenset:
    """The Stores one session currently holds."""
    with engine.begin() as connection:
        rows = connection.execute(text(
            "SELECT store_id FROM broadcast_store_leases "
            "WHERE session_id = :session AND released_at IS NULL"),
            {"session": session_id}).fetchall()
    return frozenset(row[0] for row in rows)


def reserve_stores_for_session(engine: Engine, *, session_id: int,
                               store_ids, scope=None) -> frozenset:
    """Claim every one of ``store_ids`` for ``session_id``, or claim none.

    ALL OR NOTHING, and that is the operationally important part. Starting on
    "the ones that were free" would put a campaign on air half-targeted, and
    the operator would have no reason to suspect it - they asked for both.

    Raises StoreNotInScopeError if any Store is outside ``scope``, and
    StoreBusyError if any is already held. Both leave the table exactly as
    they found it.
    """
    requested = {int(store_id) for store_id in store_ids}
    if not requested:
        return frozenset()

    if scope is not None:
        outside = requested - set(scope)
        if outside:
            # Checked before touching the table at all: a Store the caller
            # cannot see must not even influence a transaction they started.
            raise StoreNotInScopeError(outside)

    acquired_at = _now()
    try:
        with engine.begin() as connection:
            # One transaction, one statement per Store. The SELECT below is
            # only here to produce a good message - it names every busy Store
            # rather than the first one the index happens to reject, so an
            # operator can fix the whole selection in one go instead of
            # discovering the conflicts one at a time.
            busy = _busy_within(connection, requested)
            if busy:
                raise StoreBusyError(busy)

            for store_id in sorted(requested):
                connection.execute(text(
                    "INSERT INTO broadcast_store_leases "
                    "(session_id, store_id, acquired_at) "
                    "VALUES (:session, :store, :acquired)"),
                    {"session": session_id, "store": store_id,
                     "acquired": acquired_at})
    except IntegrityError as collision:
        # The race the SELECT above cannot close: another transaction
        # committed between the read and the insert. The index caught it, the
        # whole transaction rolled back, and nothing partial survives.
        #
        # Re-read to say WHICH Stores are busy now. If the winner has already
        # released them the set can come back empty, which is honest - the
        # request still failed, and retrying is now the right move.
        raise StoreBusyError(_busy_among(engine, requested)) from collision

    return frozenset(requested)


def _busy_within(connection, requested: set) -> set:
    """Which of ``requested`` are already held, read inside the caller's
    transaction.

    A separate function so a test can blind it and leave the unique index as
    the only thing able to refuse - otherwise the index path is covered only
    when the thread scheduler happens to interleave two writers, which is not
    something to depend on for the guarantee that matters most here.
    """
    rows = connection.execute(text(
        "SELECT store_id FROM broadcast_store_leases "
        "WHERE released_at IS NULL")).fetchall()
    return {row[0] for row in rows} & requested


def _busy_among(engine: Engine, requested: set) -> set:
    try:
        with engine.begin() as connection:
            rows = connection.execute(text(
                "SELECT store_id FROM broadcast_store_leases "
                "WHERE released_at IS NULL")).fetchall()
        return {row[0] for row in rows} & requested
    except Exception:
        # Reporting a conflict matters more than naming it precisely.
        return set(requested)


def release_session_leases(engine: Engine, *, session_id: int) -> int:
    """Release everything this session holds. Returns how many were released.

    Scoped to ONE session by construction. A release that took a store_id
    instead could free a Store another session is legitimately broadcasting
    to, and the symptom would be a second campaign appearing on speakers that
    were already busy.

    Idempotent: releasing twice returns 0 the second time rather than failing,
    because the callers are cleanup paths and a cleanup path that raises when
    there is nothing to clean up is a cleanup path people wrap in try/except.
    """
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE broadcast_store_leases SET released_at = :now "
            "WHERE session_id = :session AND released_at IS NULL"),
            {"now": _now(), "session": session_id})
        return int(result.rowcount or 0)
