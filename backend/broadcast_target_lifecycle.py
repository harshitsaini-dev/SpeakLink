"""Where each Store is in its participation, and which participation it is on.

WHY THIS IS SEPARATE FROM play_status

``broadcast_targets.play_status`` answers "is audio arriving and playing" - it
is Receiver truth, reported by the Store. ``lifecycle_state`` answers "is this
Store part of the Broadcast at all", which only an operator changes, by adding,
pausing or removing it.

A Store can be ACTIVE here and silent there, and that combination is
informative rather than contradictory: it means the operator wants this shop in
the announcement and the Receiver has not confirmed audio. Collapsing the two
is how a console ends up reporting a shop as playing because a command was
sent to it.

WHY A GENERATION NUMBER

One generation is one stretch of being in the Broadcast, and - once the
Receiver work lands - one Windows volume baseline. Adding gives 1, a later
resume gives 2, a re-add after removal continues upward rather than restarting.

That number is what lets a late acknowledgement be recognised as belonging to a
participation that has already ended, and dropped, instead of landing on the
one that replaced it. Without it, a Store removed and re-added is
indistinguishable from itself a moment earlier.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

__all__ = [
    "LIFECYCLE_STATES",
    "ACTIVE",
    "ADDING",
    "PREPARING",
    "PAUSING",
    "PAUSED",
    "REMOVING",
    "REMOVED",
    "FAILED",
    "ensure_target_lifecycle_schema",
]

#: An operator asked for this Store and the lease is being claimed.
ADDING = "ADDING"
#: Lease held, prepare sent, waiting for the Receiver to say it is ready.
PREPARING = "PREPARING"
#: Ready acknowledged and audio is being delivered. NOT a claim that anything
#: is audible - that is play_status, and beyond it, acoustic verification.
ACTIVE = "ACTIVE"
#: Stand-down sent, waiting for the Receiver to confirm it has stopped.
PAUSING = "PAUSING"
#: Stopped for now, lease retained. Another Broadcast must not be able to take
#: a Store that this one intends to resume.
PAUSED = "PAUSED"
#: Leaving. Distinct from REMOVED so a second click is a no-op rather than a
#: second teardown.
REMOVING = "REMOVING"
#: Gone from this Broadcast. The row stays as history; the lease does not.
REMOVED = "REMOVED"
#: This participation could not be established. Terminal for its generation; a
#: fresh add starts a new one rather than reviving this.
FAILED = "FAILED"

LIFECYCLE_STATES = frozenset({
    ADDING, PREPARING, ACTIVE, PAUSING, PAUSED, REMOVING, REMOVED, FAILED,
})


def ensure_target_lifecycle_schema(engine: Engine) -> None:
    """Add the two columns if they are absent. Additive, idempotent.

    Existing rows get ACTIVE and generation 1, which is exactly what they were:
    a Store targeted when the Broadcast started and never touched again. No
    data is rewritten and no table is rebuilt, so this runs against a live
    database without a maintenance window.
    """
    with engine.begin() as connection:
        existing = {
            row[1] for row in connection.execute(
                text("PRAGMA table_info(broadcast_targets)"))
        } if engine.dialect.name == "sqlite" else {
            row[0] for row in connection.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'broadcast_targets'"))
        }
        if not existing:
            # The table itself has not been created yet; SQLAlchemy's metadata
            # will make it with both columns already present.
            return
        if "lifecycle_state" not in existing:
            connection.execute(text(
                "ALTER TABLE broadcast_targets ADD COLUMN lifecycle_state "
                "VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'"))
        if "current_generation" not in existing:
            connection.execute(text(
                "ALTER TABLE broadcast_targets ADD COLUMN current_generation "
                "INTEGER NOT NULL DEFAULT 1"))
