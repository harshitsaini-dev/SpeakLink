"""A restart must not leave a Store permanently BUSY.

THE FAILURE THIS PREVENTS

Store reservations live in the database so they survive a crash - that is the
whole point of them. The runtime that owns a broadcast does not: a restart
destroys the microphone socket and every audio queue. So after an unclean
stop the database can hold ``status='live'`` sessions whose leases are still
unreleased, with nothing in memory that knows about them. Those Stores answer
STORE_BUSY to every future broadcast, for ever, and no operator action clears
it.

WHAT RECOVERY MEANS HERE

An interrupted broadcast is NOT resumed. It cannot be: the audio source is
gone, the queues are gone, and the operator's browser is gone. Pretending
otherwise would mean reporting a session as live while no audio can possibly
reach it - the exact class of overclaim this project keeps removing.

So an orphaned session is closed honestly and its Stores are freed.

WHY 'failed' AND NOT A NEW LABEL

The vocabulary already has pending/live/ended/emergency_stopped/failed, and
the History screen already filters on failed. 'ended' would say an operator
stopped it, which is untrue and hides an incident. A sixth label would need
every consumer updated to avoid rendering a blank badge. So: 'failed', plus a
bounded reason in notes that names what actually happened.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from sqlalchemy import create_engine, text  # noqa: E402

from broadcast_reconciliation import (  # noqa: E402
    RESTART_REASON,
    reconcile_orphaned_broadcasts,
)
from broadcast_reservation import (  # noqa: E402
    active_busy_store_ids,
    ensure_broadcast_lease_schema,
    reserve_stores_for_session,
)

BP, KG, RG, VP = 101, 102, 103, 104


@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{(tmp_path / 'restart.db').as_posix()}")
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE broadcast_sessions ("
            " id INTEGER PRIMARY KEY, campaign_name TEXT, status TEXT,"
            " started_by INTEGER, started_at TEXT, ended_at TEXT, notes TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE broadcast_targets ("
            " id INTEGER PRIMARY KEY, session_id INTEGER, store_id INTEGER,"
            " play_status TEXT)")
        connection.exec_driver_sql(
            "CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT)")
        for store_id, code in ((BP, "BP"), (KG, "KG"), (RG, "RG"), (VP, "VP")):
            connection.exec_driver_sql(
                "INSERT INTO stores (id, store_code) VALUES (?, ?)",
                (store_id, code))
    ensure_broadcast_lease_schema(made)
    return made


def add_session(engine, session_id: int, status: str = "live",
                *, campaign: str = "Campaign", targets=()) -> int:
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO broadcast_sessions "
            "(id, campaign_name, status, started_by, started_at) "
            "VALUES (:i, :c, :s, 1, '2026-08-03T09:00:00+00:00')"),
            {"i": session_id, "c": campaign, "s": status})
        for store_id in targets:
            connection.execute(text(
                "INSERT INTO broadcast_targets (session_id, store_id, play_status) "
                "VALUES (:s, :st, 'playing')"), {"s": session_id, "st": store_id})
    return session_id


def status_of(engine, session_id: int):
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT status, ended_at, notes FROM broadcast_sessions "
            "WHERE id = :i"), {"i": session_id}).fetchone()


def lease_rows(engine):
    with engine.begin() as connection:
        return connection.execute(text(
            "SELECT store_id, session_id, released_at "
            "FROM broadcast_store_leases ORDER BY id")).fetchall()


# ===========================================================================
# The orphan
# ===========================================================================
def test_an_orphaned_live_session_is_closed_and_its_stores_freed(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert report.orphaned_session_ids == (1,)
    assert report.released_leases == 1
    status, ended_at, notes = status_of(engine, 1)
    assert status == "failed"
    assert ended_at is not None
    assert RESTART_REASON in (notes or "")
    assert BP not in active_busy_store_ids(engine)


def test_two_orphans_are_reconciled_independently(engine):
    add_session(engine, 1, "live", targets=[BP])
    add_session(engine, 2, "live", targets=[RG, VP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    reserve_stores_for_session(engine, session_id=2, store_ids=[RG, VP])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert set(report.orphaned_session_ids) == {1, 2}
    assert report.released_leases == 3
    assert status_of(engine, 1)[0] == "failed"
    assert status_of(engine, 2)[0] == "failed"
    assert active_busy_store_ids(engine) == frozenset()


def test_a_live_session_with_no_lease_is_still_closed(engine):
    """The crash window between marking a session live and claiming its
    Stores - or after a lease was already released by a partial stop."""
    add_session(engine, 1, "live", targets=[BP])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert report.orphaned_session_ids == (1,)
    assert status_of(engine, 1)[0] == "failed"


# ===========================================================================
# What must NOT be touched
# ===========================================================================
def test_a_session_owned_by_the_current_runtime_is_left_alone(engine):
    """The contract that keeps this from becoming release-all-leases.

    At real startup the runtime is empty, so this never fires in production -
    which is exactly why it must be tested directly. A future caller running
    reconciliation while broadcasts are live would otherwise cut them off.
    """
    add_session(engine, 1, "live", targets=[BP])
    add_session(engine, 2, "live", targets=[RG])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    reserve_stores_for_session(engine, session_id=2, store_ids=[RG])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=(2,))

    assert report.orphaned_session_ids == (1,)
    assert status_of(engine, 1)[0] == "failed"
    assert status_of(engine, 2)[0] == "live", "a live broadcast was cut off"
    assert active_busy_store_ids(engine) == frozenset({RG})


def test_an_already_ended_session_is_untouched(engine):
    add_session(engine, 1, "ended", targets=[BP])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert report.orphaned_session_ids == ()
    assert status_of(engine, 1)[0] == "ended"


def test_a_pending_session_is_not_closed(engine):
    """Pending is a session that was created and never started. It holds
    nothing and closing it would destroy a draft the operator is still
    editing."""
    add_session(engine, 1, "pending", targets=[BP])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert status_of(engine, 1)[0] == "pending"


def test_released_historical_leases_are_preserved(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    rows = lease_rows(engine)
    assert len(rows) == 1, "the lease row was deleted rather than closed"
    assert rows[0][2] is not None


def test_broadcast_history_survives(engine):
    add_session(engine, 1, "live", campaign="Diwali Offers", targets=[BP, KG])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    with engine.begin() as connection:
        row = connection.execute(text(
            "SELECT campaign_name FROM broadcast_sessions WHERE id = 1")).fetchone()
        targets = connection.execute(text(
            "SELECT COUNT(*) FROM broadcast_targets WHERE session_id = 1")).scalar_one()
    assert row[0] == "Diwali Offers"
    assert targets == 2


# ===========================================================================
# Inconsistent lease states
# ===========================================================================
def test_an_active_lease_for_an_already_ended_session_is_released(engine):
    """Crash between releasing a session and releasing its lease."""
    add_session(engine, 1, "ended", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert report.leases_for_finished_sessions == 1
    assert BP not in active_busy_store_ids(engine)
    assert status_of(engine, 1)[0] == "ended", "a finished session was rewritten"


def test_an_active_lease_whose_session_row_is_missing_is_released(engine):
    """Should be impossible - there is a foreign key - so it is reported
    rather than absorbed. The Store is still freed, because a Store that
    cannot be broadcast to is a worse outcome than an unexplained row."""
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(text("DELETE FROM broadcast_sessions WHERE id = 1"))

    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert report.leases_without_session == 1
    assert BP not in active_busy_store_ids(engine)
    assert len(lease_rows(engine)) == 1, "evidence of the anomaly was deleted"


def test_an_emergency_stopped_session_with_a_lease_is_released(engine):
    add_session(engine, 1, "emergency_stopped", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert BP not in active_busy_store_ids(engine)
    assert status_of(engine, 1)[0] == "emergency_stopped"


# ===========================================================================
# Idempotency and atomicity
# ===========================================================================
def test_running_reconciliation_twice_is_safe(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    first = reconcile_orphaned_broadcasts(engine, active_session_ids=())
    second = reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert first.orphaned_session_ids == (1,)
    assert second.orphaned_session_ids == ()
    assert second.released_leases == 0
    assert len(lease_rows(engine)) == 1


def test_a_second_run_does_not_rewrite_the_first_runs_verdict(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    reconcile_orphaned_broadcasts(engine, active_session_ids=())
    first = status_of(engine, 1)

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    assert status_of(engine, 1) == first


def test_nothing_to_do_is_not_an_error(engine):
    report = reconcile_orphaned_broadcasts(engine, active_session_ids=())
    assert report.orphaned_session_ids == ()
    assert report.released_leases == 0


def test_a_session_is_never_left_closed_with_its_lease_still_held(engine):
    """The half-state that would recreate the bug: status says finished,
    lease says busy. Asserted over several sessions so an ordering mistake
    inside the loop shows up."""
    for session_id, store_id in ((1, BP), (2, KG), (3, RG)):
        add_session(engine, session_id, "live", targets=[store_id])
        reserve_stores_for_session(engine, session_id=session_id,
                                   store_ids=[store_id])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    busy = active_busy_store_ids(engine)
    for session_id in (1, 2, 3):
        assert status_of(engine, session_id)[0] == "failed"
    assert busy == frozenset()


# ===========================================================================
# The Store is usable again
# ===========================================================================
def test_a_store_can_be_reserved_by_a_new_session_after_reconciliation(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])
    add_session(engine, 2, "pending", targets=[BP])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())
    reserve_stores_for_session(engine, session_id=2, store_ids=[BP])

    assert active_busy_store_ids(engine) == frozenset({BP})
    with engine.begin() as connection:
        holder = connection.execute(text(
            "SELECT session_id FROM broadcast_store_leases "
            "WHERE store_id = :s AND released_at IS NULL"), {"s": BP}).scalar_one()
    assert holder == 2


def test_reconciliation_touches_no_other_table(engine):
    """It must not manufacture playback evidence, and it must not go near
    Receiver credentials or enrolment. Asserted by counting rows in every
    table it has no business writing to."""
    add_session(engine, 1, "live", targets=[BP, KG])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP, KG])

    with engine.begin() as connection:
        before = connection.execute(text(
            "SELECT COUNT(*) FROM stores")).scalar_one()
        targets_before = connection.execute(text(
            "SELECT play_status FROM broadcast_targets WHERE session_id = 1"
        )).fetchall()

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    with engine.begin() as connection:
        after = connection.execute(text(
            "SELECT COUNT(*) FROM stores")).scalar_one()
        targets_after = connection.execute(text(
            "SELECT play_status FROM broadcast_targets WHERE session_id = 1"
        )).fetchall()
    assert before == after
    # play_status is deliberately NOT rewritten to anything that claims
    # knowledge of what the speakers did. The process that would have known
    # is gone.
    assert targets_before == targets_after


def test_no_playback_or_speaker_claim_is_manufactured(engine):
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    _status, _ended, notes = status_of(engine, 1)
    lowered = (notes or "").lower()
    for forbidden in ("speaker_verified", "playback_confirmed", "verified",
                      "heard"):
        assert forbidden not in lowered, f"{forbidden!r} claimed in: {notes}"


def test_the_recorded_reason_is_bounded(engine):
    """notes is free text on a row an operator reads. The reason must be a
    fixed phrase, not an exception string that could carry a path or a
    connection detail."""
    add_session(engine, 1, "live", targets=[BP])
    reserve_stores_for_session(engine, session_id=1, store_ids=[BP])

    reconcile_orphaned_broadcasts(engine, active_session_ids=())

    notes = status_of(engine, 1)[2] or ""
    assert len(notes) <= 200
    assert "Traceback" not in notes
