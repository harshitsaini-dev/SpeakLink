"""Close broadcasts that a restart orphaned, so no Store stays BUSY for ever.

THE ASYMMETRY THAT CAUSES THIS

Store reservations are deliberately persistent - that is what makes the
one-broadcast-per-Store rule survive a crash. The runtime that owns a
broadcast is deliberately not: a restart destroys the HQ microphone socket and
every audio queue with it.

So after an unclean stop the database can hold ``status='live'`` sessions whose
leases are unreleased, with nothing in memory that knows about them. Those
Stores answer STORE_BUSY to every future broadcast for ever, and no operator
action clears it. That is the failure this module exists to prevent.

AN INTERRUPTED BROADCAST IS NOT RESUMED

It cannot be. The audio source is gone, the queues are gone, and the
operator's browser is gone. Marking such a session live again would report a
broadcast as on air while no audio can possibly reach it - the exact class of
overclaim this project keeps removing. It is closed honestly instead, and its
Stores are freed.

WHY 'failed' RATHER THAN A NEW LABEL

The vocabulary is pending/live/ended/emergency_stopped/failed, and the History
screen already filters on failed. 'ended' would say an operator stopped it,
which is untrue and would hide an incident. A sixth label would need every
consumer updated to avoid rendering a blank badge for a state they have never
heard of. So 'failed', plus a fixed reason in notes naming what happened.

WHAT IT REFUSES TO GUESS

Nothing here rewrites ``BroadcastTarget.play_status``. Whether a Store's
speakers were actually playing when the process died is knowledge the dead
process had and this one does not. Leaving those rows as they were is the
honest answer; writing 'stopped' would be a claim about the physical world
made by a program that has just started.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from broadcast_reservation import release_session_leases_in

logger = logging.getLogger("echocast.broadcast")

__all__ = [
    "RESTART_REASON",
    "ReconciliationReport",
    "BroadcastReconciliationError",
    "reconcile_orphaned_broadcasts",
]

#: Fixed phrase, never an exception string. notes is free text an operator
#: reads on a history row, and an interpolated error could carry a filesystem
#: path or a connection detail into a screen that is not meant to show either.
RESTART_REASON = (
    "Interrupted: HQ restarted while this broadcast was live. The audio "
    "source and its queues did not survive, so the broadcast was closed and "
    "its Stores released."
)

#: Sessions that are finished in every sense. An active lease held by one of
#: these is a crash between "the session ended" and "its Stores were freed".
FINISHED_STATUSES = ("ended", "failed", "emergency_stopped")


class BroadcastReconciliationError(RuntimeError):
    """Startup could not make the broadcast state safe. Never carries a
    credential or a Store token."""


@dataclass(frozen=True)
class ReconciliationReport:
    #: Sessions that were live in the database with no runtime owner.
    orphaned_session_ids: tuple = ()
    #: Lease rows closed, across every category below.
    released_leases: int = 0
    #: Leases held by a session that had already finished.
    leases_for_finished_sessions: int = 0
    #: Leases whose session row does not exist. Should be impossible - there
    #: is a foreign key - so it is counted and logged rather than absorbed.
    leases_without_session: int = 0

    @property
    def changed_anything(self) -> bool:
        return bool(self.orphaned_session_ids) or self.released_leases > 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_orphaned_broadcasts(engine: Engine, *,
                                  active_session_ids=()) -> ReconciliationReport:
    """Close orphaned broadcasts and free their Stores.

    ``active_session_ids`` is the set of sessions the CURRENT runtime owns.
    Only persisted live sessions absent from it are touched.

    That parameter is the contract that stops this becoming a generic
    "release every lease" helper. At real startup the runtime is empty, so it
    is always the empty tuple in production - which is precisely why it has to
    be explicit and directly tested. A future caller running reconciliation
    while broadcasts are live would otherwise cut them off mid-announcement.

    Raises BroadcastReconciliationError if the state cannot be made safe. A
    caller must not treat that as a warning to log beside "startup complete":
    an unreleased lease is a Store that can never be broadcast to again.
    """
    owned = {int(session_id) for session_id in active_session_ids}

    try:
        with engine.begin() as connection:
            orphans = [
                int(row[0]) for row in connection.execute(text(
                    "SELECT id FROM broadcast_sessions "
                    "WHERE status = 'live' ORDER BY id")).fetchall()
                if int(row[0]) not in owned
            ]

            released = 0
            for session_id in orphans:
                # Both halves in ONE transaction. A crash between them leaves
                # either a closed session still holding Stores - the permanent
                # STORE_BUSY this exists to prevent - or a freed Store whose
                # session still claims to be live.
                connection.execute(text(
                    "UPDATE broadcast_sessions "
                    "SET status = 'failed', ended_at = :now, "
                    "    notes = COALESCE(notes || ' ', '') || :reason "
                    "WHERE id = :id AND status = 'live'"),
                    {"now": _now(), "reason": RESTART_REASON, "id": session_id})
                released += release_session_leases_in(connection,
                                                      session_id=session_id)

            # Leases still held by a session that already finished: a crash
            # between ending a session and releasing its Stores. The session
            # is NOT rewritten - its recorded outcome is already correct and
            # overwriting it would destroy the real one.
            finished_rows = connection.execute(text(
                "SELECT DISTINCT l.session_id FROM broadcast_store_leases l "
                "JOIN broadcast_sessions s ON s.id = l.session_id "
                "WHERE l.released_at IS NULL AND s.status IN "
                "('ended', 'failed', 'emergency_stopped')")).fetchall()
            finished = 0
            for row in finished_rows:
                if int(row[0]) in owned:
                    continue
                finished += release_session_leases_in(
                    connection, session_id=int(row[0]))

            # Leases whose session row is gone. There is a foreign key, so
            # this should be unreachable; it is reported loudly rather than
            # absorbed. The Store is still freed - a Store nothing can ever
            # broadcast to is a worse outcome than an unexplained row - and
            # the row itself is CLOSED, not deleted, so the evidence survives.
            dangling_rows = connection.execute(text(
                "SELECT l.id, l.store_id FROM broadcast_store_leases l "
                "LEFT JOIN broadcast_sessions s ON s.id = l.session_id "
                "WHERE l.released_at IS NULL AND s.id IS NULL")).fetchall()
            dangling = 0
            if dangling_rows:
                logger.error(
                    "%d Store lease(s) reference a Broadcast Session that does "
                    "not exist (store ids: %s). This should be impossible - "
                    "there is a foreign key. The rows have been closed so the "
                    "Stores are usable, and kept so the anomaly can be "
                    "investigated.",
                    len(dangling_rows),
                    sorted({int(row[1]) for row in dangling_rows}),
                )
                result = connection.execute(text(
                    "UPDATE broadcast_store_leases SET released_at = :now "
                    "WHERE released_at IS NULL AND session_id NOT IN "
                    "(SELECT id FROM broadcast_sessions)"), {"now": _now()})
                dangling = int(result.rowcount or 0)
    except Exception as failure:
        raise BroadcastReconciliationError(
            "Broadcast restart reconciliation failed "
            f"({failure.__class__.__name__}). Store leases may still be held "
            "by broadcasts that no longer exist, which would leave those "
            "Stores permanently unavailable."
        ) from failure

    report = ReconciliationReport(
        orphaned_session_ids=tuple(orphans),
        released_leases=released + finished + dangling,
        leases_for_finished_sessions=finished,
        leases_without_session=dangling,
    )
    if report.changed_anything:
        logger.warning(
            "Broadcast restart reconciliation: closed %d interrupted "
            "broadcast(s) %s and released %d Store lease(s).",
            len(report.orphaned_session_ids), list(report.orphaned_session_ids),
            report.released_leases,
        )
    return report
