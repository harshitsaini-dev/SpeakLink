"""Claiming and freeing ONE Store while a Broadcast is already running.

Starting a Broadcast reserves every Store at once, all or nothing. Adding one
mid-Broadcast cannot work that way: the session already holds leases, and the
new Store has to be claimed on its own without disturbing them or being able to
free anybody else's.

The dangerous shape here is a release keyed on the Store alone. The
session-wide release warns about exactly that, and it is right - it could free
a Store another campaign is broadcasting to, and the symptom would be a second
announcement arriving on speakers that were already busy. These tests hold the
per-Store release to naming both ids.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from sqlalchemy import create_engine, text  # noqa: E402

import broadcast_reservation as reservation  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'leases.db'}", future=True)
    reservation.ensure_broadcast_lease_schema(made)
    return made


def held(engine, session_id):
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT store_id FROM broadcast_store_leases "
            "WHERE session_id = :s AND released_at IS NULL"),
            {"s": session_id}).fetchall()
    return sorted(row[0] for row in rows)


# ===========================================================================
# Claiming one more
# ===========================================================================

def test_a_free_store_can_join_a_running_broadcast(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10, 11], scope=None)
    reservation.reserve_one_store_for_session(engine, session_id=1, store_id=12)
    assert held(engine, 1) == [10, 11, 12]


def test_a_store_another_broadcast_holds_is_refused(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    reservation.reserve_stores_for_session(engine, session_id=2,
                                           store_ids=[20], scope=None)

    with pytest.raises(reservation.StoreBusyError):
        reservation.reserve_one_store_for_session(engine, session_id=2,
                                                  store_id=10)

    # Nothing was written, and the Broadcast that legitimately holds it is
    # untouched - a refused Add must not disturb a running announcement.
    assert held(engine, 1) == [10]
    assert held(engine, 2) == [20]


def test_a_refused_claim_leaves_no_half_written_lease(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    with pytest.raises(reservation.StoreBusyError):
        reservation.reserve_one_store_for_session(engine, session_id=2,
                                                  store_id=10)
    with engine.connect() as connection:
        total = connection.execute(text(
            "SELECT count(*) FROM broadcast_store_leases")).scalar_one()
    assert total == 1, "the refused insert left a row behind"


# ===========================================================================
# Freeing one
# ===========================================================================

def test_one_store_can_leave_without_freeing_the_others(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10, 11, 12], scope=None)
    assert reservation.release_store_lease(engine, session_id=1, store_id=11)
    assert held(engine, 1) == [10, 12]


def test_releasing_cannot_reach_into_another_broadcast(engine):
    """The whole reason this takes both ids."""
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    reservation.reserve_stores_for_session(engine, session_id=2,
                                           store_ids=[20], scope=None)

    freed = reservation.release_store_lease(engine, session_id=2, store_id=10)

    assert freed is False, "session 2 freed a Store it never held"
    assert held(engine, 1) == [10], (
        "another Broadcast's Store was released - it would now be seizable "
        "while that announcement is still on air")


def test_releasing_twice_is_quiet(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    assert reservation.release_store_lease(engine, session_id=1, store_id=10)
    assert reservation.release_store_lease(engine, session_id=1,
                                           store_id=10) is False


def test_a_released_store_can_be_taken_by_someone_else(engine):
    """Remove has to actually free it, or the Store is stuck until the end."""
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    reservation.release_store_lease(engine, session_id=1, store_id=10)

    reservation.reserve_stores_for_session(engine, session_id=2,
                                           store_ids=[10], scope=None)
    assert held(engine, 2) == [10]


def test_a_removed_store_can_rejoin_the_same_broadcast(engine):
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10], scope=None)
    reservation.release_store_lease(engine, session_id=1, store_id=10)
    reservation.reserve_one_store_for_session(engine, session_id=1, store_id=10)
    assert held(engine, 1) == [10]


def test_the_session_wide_release_still_frees_everything(engine):
    """The existing path must keep working beside the new one."""
    reservation.reserve_stores_for_session(engine, session_id=1,
                                           store_ids=[10, 11], scope=None)
    reservation.reserve_one_store_for_session(engine, session_id=1, store_id=12)
    assert reservation.release_session_leases(engine, session_id=1) == 3
    assert held(engine, 1) == []
