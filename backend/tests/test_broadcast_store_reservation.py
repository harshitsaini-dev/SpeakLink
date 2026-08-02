"""One Store belongs to at most one live Broadcast, and the database says so.

WHY A TABLE AND NOT A SET IN MEMORY

SpeakLink is moving from one global broadcast to several concurrent ones. The
rule that has to survive that move is narrow and absolute: a Store must never
process two live broadcasts at once - two announcements over one set of
speakers is worse than either alone.

An in-memory registry cannot carry that rule. It is lost on restart, so a
crash mid-broadcast leaves no record of what was reserved; and it is only as
atomic as the code around it, so two requests that interleave between "check"
and "claim" both win. The guard here is therefore a UNIQUE INDEX, and the
application logic is a convenience in front of it - if the logic is ever
wrong, the database still refuses.

WHAT IS DELIBERATELY NOT HERE

Ownership visibility (who holds a busy Store) is a later phase and is not
built early: an API that reveals another operator's identity is not something
to add speculatively.

Audio routing is untouched. Reservations are written at session lifecycle
boundaries only - never per audio chunk, which would be a database write
thousands of times a minute per Store.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from sqlalchemy import create_engine, text  # noqa: E402

from broadcast_reservation import (  # noqa: E402
    StoreBusyError,
    StoreNotInScopeError,
    active_busy_store_ids,
    ensure_broadcast_lease_schema,
    release_session_leases,
    reserve_stores_for_session,
)

BP, KG, RG, VP, JHA6 = 101, 102, 103, 104, 105


@pytest.fixture()
def engine(tmp_path):
    """A private database with only what these tests need.

    Deliberately not the full application schema: the reservation invariant
    must hold on its own terms, and building it on top of a seeded HQ would
    hide a missing foreign key or index behind everything else that is there.
    """
    made = create_engine(f"sqlite:///{(tmp_path / 'leases.db').as_posix()}")
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE broadcast_sessions ("
            " id INTEGER PRIMARY KEY, campaign_name TEXT, status TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT)")
        for store_id, code in ((BP, "BP"), (KG, "KG"), (RG, "RG"),
                               (VP, "VP"), (JHA6, "JHA6")):
            connection.exec_driver_sql(
                "INSERT INTO stores (id, store_code) VALUES (?, ?)",
                (store_id, code))
        for session_id in (1, 2, 3):
            connection.exec_driver_sql(
                "INSERT INTO broadcast_sessions (id, campaign_name, status) "
                "VALUES (?, ?, 'live')",
                (session_id, f"Campaign {session_id}"))
    ensure_broadcast_lease_schema(made)
    return made


def active_leases(engine) -> dict:
    with engine.begin() as connection:
        return {
            row[0]: row[1] for row in connection.execute(text(
                "SELECT store_id, session_id FROM broadcast_store_leases "
                "WHERE released_at IS NULL"))
        }


# ===========================================================================
# The happy paths
# ===========================================================================
def test_a_user_can_reserve_a_free_store(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    assert active_leases(engine) == {BP: 1}


def test_two_sessions_can_hold_different_stores(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])
    reserve_stores_for_session(engine, session_id=2, store_ids=[RG, VP])
    assert active_leases(engine) == {BP: 1, KG: 1, RG: 2, VP: 2}


def test_three_sessions_can_be_reserved_at_once(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])
    reserve_stores_for_session(engine, session_id=2, store_ids=[RG, VP])
    reserve_stores_for_session(engine, session_id=3, store_ids=[JHA6])
    assert len(active_leases(engine)) == 5


# ===========================================================================
# Conflict
# ===========================================================================
def test_a_second_session_cannot_take_a_reserved_store(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with pytest.raises(StoreBusyError) as conflict:
        reserve_stores_for_session(engine, session_id=2, store_ids=[BP])
    assert conflict.value.busy_store_ids == (BP,)
    assert active_leases(engine) == {BP: 1}, "the first holder was disturbed"


def test_one_busy_store_fails_the_whole_request(engine):
    """The property that matters most operationally. Starting on 'the rest'
    would put a campaign on air half-targeted, and the operator would have no
    reason to suspect it - they asked for both."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with pytest.raises(StoreBusyError):
        reserve_stores_for_session(engine, session_id=2, store_ids=[BP, RG])
    assert RG not in active_leases(engine), \
        "RG stayed reserved after the request it belonged to was refused"


def test_a_refused_request_leaves_no_partial_rows(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with pytest.raises(StoreBusyError):
        reserve_stores_for_session(engine, session_id=2,
                                   store_ids=[RG, VP, BP, JHA6])
    assert active_leases(engine) == {BP: 1}


def test_the_conflict_names_every_busy_store_not_just_the_first(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])
    with pytest.raises(StoreBusyError) as conflict:
        reserve_stores_for_session(engine, session_id=2, store_ids=[BP, KG, RG])
    assert set(conflict.value.busy_store_ids) == {BP, KG}


def test_the_conflict_carries_no_owner_or_session_detail(engine):
    """A Broadcaster learns that a Store is busy and nothing else. Who is
    using it, and for what campaign, is somebody else's business."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with pytest.raises(StoreBusyError) as conflict:
        reserve_stores_for_session(engine, session_id=2, store_ids=[BP])

    rendered = str(conflict.value).lower()
    for leak in ("campaign", "session 1", "session_id", "user", "owner",
                 "founder", "broadcaster"):
        assert leak not in rendered, f"{leak!r} leaked into: {rendered}"
    assert not hasattr(conflict.value, "owner_user_id")
    assert not hasattr(conflict.value, "holding_session_id")


# ===========================================================================
# The database is the final guard
# ===========================================================================
def test_the_database_refuses_a_second_active_lease_directly(engine):
    """Application logic bypassed entirely. If this ever passes, the unique
    index is missing and every other test here is only testing Python."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with pytest.raises(Exception) as refusal:
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO broadcast_store_leases "
                "(session_id, store_id, acquired_at) "
                "VALUES (2, :store, '2026-08-03T00:00:00+00:00')"),
                {"store": BP})
    assert "unique" in str(refusal.value).lower()


def test_the_index_refusal_is_reported_as_store_busy(engine):
    """Deterministic cover for the path the race can only hit by luck.

    The pre-check inside reserve_stores_for_session exists to produce a good
    message; the INDEX is what enforces the rule. Blinding the pre-check
    proves the second line of defence converts a raw IntegrityError into the
    same StoreBusyError a caller already handles - rather than a 500.
    """
    import broadcast_reservation

    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    original = broadcast_reservation._busy_within
    # Blinded: the pre-check now reports every Store as free, exactly as it
    # would if another transaction committed a microsecond after it read.
    broadcast_reservation._busy_within = lambda connection, requested: set()
    try:
        with pytest.raises(StoreBusyError) as conflict:
            reserve_stores_for_session(engine, session_id=2, store_ids=[BP])
    finally:
        broadcast_reservation._busy_within = original

    assert conflict.value.busy_store_ids == (BP,)
    # The rolled-back attempt left nothing behind, and the original holder is
    # untouched.
    assert active_leases(engine) == {BP: 1}


def test_a_released_lease_does_not_block_a_new_one(engine):
    """Released rows stay for history, so the uniqueness must be PARTIAL - a
    plain UNIQUE(store_id) would let a Store be broadcast to exactly once,
    ever, and the failure would appear days later."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    release_session_leases(engine, session_id=1)
    reserve_stores_for_session(engine, session_id=2, store_ids=[BP])
    assert active_leases(engine) == {BP: 2}


def test_history_of_released_leases_is_kept(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    release_session_leases(engine, session_id=1)
    with engine.begin() as connection:
        total = connection.execute(text(
            "SELECT COUNT(*) FROM broadcast_store_leases")).scalar_one()
    assert total == 1, "the released lease row was destroyed rather than closed"


# ===========================================================================
# The race
# ===========================================================================
def test_two_genuinely_concurrent_reservations_produce_exactly_one_winner(engine):
    """Real threads, one barrier, one Store.

    Not a simulation of a race: both threads block on the same barrier and are
    released together, so the interleaving is the operating system's choice
    rather than the test's.
    """
    attempts = 12
    barrier = threading.Barrier(attempts)
    outcomes: list = []
    lock = threading.Lock()

    def attempt(session_id: int) -> None:
        barrier.wait()
        try:
            reserve_stores_for_session(engine, session_id=session_id,
                                       store_ids=[BP])
            result = "won"
        except StoreBusyError:
            result = "busy"
        except Exception as failure:      # noqa: BLE001 - recorded, not hidden
            result = f"error:{failure.__class__.__name__}"
        with lock:
            outcomes.append(result)

    # Only three session rows exist, so sessions repeat - which is fine and
    # realistic: the invariant is per STORE, not per session.
    threads = [threading.Thread(target=attempt, args=((i % 3) + 1,))
               for i in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [o for o in outcomes if o == "won"]
    errors = [o for o in outcomes if o.startswith("error:")]
    assert not errors, f"unexpected failures: {errors}"
    assert len(winners) == 1, f"expected exactly one winner, got {outcomes}"
    assert len(active_leases(engine)) == 1


def test_concurrent_reservations_of_different_stores_all_succeed(engine):
    """The other half of the race: contention must not become a global lock
    that serialises unrelated Stores into failure."""
    barrier = threading.Barrier(4)
    outcomes: list = []
    lock = threading.Lock()

    def attempt(session_id: int, store_id: int) -> None:
        barrier.wait()
        try:
            reserve_stores_for_session(engine, session_id=session_id,
                                       store_ids=[store_id])
            result = "won"
        except Exception as failure:      # noqa: BLE001
            result = f"error:{failure.__class__.__name__}"
        with lock:
            outcomes.append(result)

    pairs = ((1, BP), (2, RG), (3, VP), (1, KG))
    threads = [threading.Thread(target=attempt, args=pair) for pair in pairs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("won") == 4, outcomes
    assert len(active_leases(engine)) == 4


# ===========================================================================
# Release
# ===========================================================================
def test_releasing_one_session_leaves_another_sessions_stores_alone(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])
    reserve_stores_for_session(engine, session_id=2, store_ids=[RG, VP])

    release_session_leases(engine, session_id=1)

    assert active_leases(engine) == {RG: 2, VP: 2}


def test_releasing_a_session_twice_is_safe(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    released_first = release_session_leases(engine, session_id=1)
    released_again = release_session_leases(engine, session_id=1)
    assert released_first == 1
    assert released_again == 0


def test_releasing_a_session_that_never_reserved_anything_is_safe(engine):
    assert release_session_leases(engine, session_id=3) == 0


def test_a_store_is_free_again_after_its_session_is_released(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    assert BP in active_busy_store_ids(engine)
    release_session_leases(engine, session_id=1)
    assert BP not in active_busy_store_ids(engine)


# ===========================================================================
# Store Scope
# ===========================================================================
def test_a_store_outside_scope_cannot_be_reserved(engine):
    with pytest.raises(StoreNotInScopeError):
        reserve_stores_for_session(engine, session_id=1, store_ids=[BP],
                                   scope=frozenset({RG, VP}))
    assert active_leases(engine) == {}


def test_an_out_of_scope_store_fails_the_whole_request(engine):
    with pytest.raises(StoreNotInScopeError):
        reserve_stores_for_session(engine, session_id=1, store_ids=[RG, BP],
                                   scope=frozenset({RG}))
    assert active_leases(engine) == {}, "an in-scope Store stayed reserved"


def test_an_unrestricted_scope_reserves_normally(engine):
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP],
                               scope=None)
    assert active_leases(engine) == {BP: 1}


def test_busy_ids_can_be_narrowed_to_a_scope(engine):
    """A scoped operator must not learn that an out-of-scope Store is busy -
    that is an existence disclosure about an estate they cannot see."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, RG])
    visible = active_busy_store_ids(engine, scope=frozenset({RG}))
    assert visible == frozenset({RG})


# ===========================================================================
# Nothing else moved
# ===========================================================================
def test_reserving_writes_only_lease_rows(engine):
    """Reservation is a lifecycle event, not a broadcast-time cost. If this
    ever starts touching other tables, the per-chunk write it must never
    become has begun."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])
    with engine.begin() as connection:
        sessions = connection.execute(text(
            "SELECT COUNT(*) FROM broadcast_sessions")).scalar_one()
        stores = connection.execute(text(
            "SELECT COUNT(*) FROM stores")).scalar_one()
    assert (sessions, stores) == (3, 5)


def test_the_schema_helper_is_idempotent(engine):
    ensure_broadcast_lease_schema(engine)
    ensure_broadcast_lease_schema(engine)
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    assert active_leases(engine) == {BP: 1}
