"""Removing a Store or a User for real, and refusing when history depends on it.

WHY ARCHIVE IS THE DEFAULT AND DELETION IS THE EXCEPTION

A Store owns Receiver Devices, broadcast targets, sessions and Receiver events.
A User is the actor named in audit lines. Removing either row would orphan that
history or cascade it away, and both destroy the only record of what was
announced where, and by whom. Neither is recoverable from inside the product.

So hard deletion exists for exactly one situation: a row somebody created by
mistake a few minutes ago that nothing has ever referenced. Everything else is
archived, which keeps the history readable and is reversible.

TWO RULES THAT CARRY THE WEIGHT

**Unknown is never safe.** If a dependency table cannot be read - an older
database that predates it, a permissions problem - that table is reported as
*unchecked* and deletion is refused. A count that could not be taken must never
be read as a count of zero.

**The check happens inside the transaction that deletes.** Counting first and
deleting afterwards is a race: a Device can be enrolled, or a broadcast started,
in the moment between. The counts that decide are the ones taken on the same
connection that performs the delete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Engine


class DeletionRefused(RuntimeError):
    """The row was not deleted. Never carries a credential or a hash."""


#: Every table that can reference a Store, named explicitly.
#:
#: A ``SELECT`` over whatever tables happen to exist would silently stop
#: protecting a Store the day somebody adds a new one. Naming them means a new
#: table is a decision - the test asserts this list and fails until it is made.
STORE_DEPENDENCY_TABLES = (
    "receiver_devices",
    "broadcast_targets",
    "receiver_events",
    "receiver_enrollment_codes",
)

#: Tables that record what a User did. Deleting the row would orphan them.
USER_DEPENDENCY_TABLES = (
    "broadcast_sessions",
)

_STORE_COLUMN = "store_id"
_USER_COLUMNS = {"broadcast_sessions": "started_by"}


@dataclass
class DependencySummary:
    """What refers to this row, counted rather than assumed."""

    counts: dict = field(default_factory=dict)
    #: Tables that could not be read. Their absence is not evidence of safety.
    unchecked: list = field(default_factory=list)
    exists: bool = True

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def deletable(self) -> bool:
        return self.exists and self.total == 0 and not self.unchecked

    def explain(self) -> str:
        if not self.exists:
            return "That record no longer exists."
        if self.unchecked:
            return ("Some dependency tables could not be read ("
                    + ", ".join(self.unchecked) +
                    "), so this cannot be shown to be unused. Archive it instead.")
        if self.total == 0:
            return "Nothing refers to this record, so it can be removed."
        parts = [f"{count} {name.replace('_', ' ')}"
                 for name, count in sorted(self.counts.items()) if count]
        return ("This record still has " + ", ".join(parts) +
                ". Deleting it would destroy operational history. Archive it instead.")


def _count_dependencies(connection, tables, column_for, row_id) -> DependencySummary:
    summary = DependencySummary()
    for table in tables:
        column = column_for(table)
        try:
            summary.counts[table] = int(connection.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :row_id"),
                {"row_id": row_id},
            ).scalar() or 0)
        except Exception:
            # Deliberately NOT counted as zero. See the module docstring.
            summary.unchecked.append(table)
    return summary


def store_dependencies(engine: Engine, *, store_id: int) -> DependencySummary:
    with engine.connect() as connection:
        summary = _count_dependencies(
            connection, STORE_DEPENDENCY_TABLES, lambda _t: _STORE_COLUMN, store_id)
        summary.exists = bool(connection.execute(
            text("SELECT COUNT(*) FROM stores WHERE id = :i"), {"i": store_id}).scalar())
    return summary


def user_dependencies(engine: Engine, *, user_id: int) -> DependencySummary:
    with engine.connect() as connection:
        summary = _count_dependencies(
            connection, USER_DEPENDENCY_TABLES,
            lambda table: _USER_COLUMNS.get(table, "user_id"), user_id)
        summary.exists = bool(connection.execute(
            text("SELECT COUNT(*) FROM hq_users WHERE id = :i"), {"i": user_id}).scalar())
    return summary


def delete_store_if_unused(engine: Engine, *, store_id: int,
                           typed_confirmation: str) -> dict:
    """Remove one never-used Store. Refuses anything else.

    ``typed_confirmation`` must equal the Store's short code exactly. Typing the
    name of the thing being destroyed is the one confirmation that cannot be
    clicked through by muscle memory.
    """
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, store_code, store_name FROM stores WHERE id = :i"),
            {"i": store_id}).first()
        if row is None:
            raise DeletionRefused("That Store no longer exists.")

        if typed_confirmation != row.store_code:
            raise DeletionRefused(
                f"The typed confirmation did not match. Type the Store code exactly: "
                f"{row.store_code}"
            )

        # Recounted here, on the connection holding the transaction, so nothing
        # can be enrolled or broadcast between the check and the delete.
        summary = _count_dependencies(
            connection, STORE_DEPENDENCY_TABLES, lambda _t: _STORE_COLUMN, store_id)
        if summary.total or summary.unchecked:
            raise DeletionRefused(
                "This Store contains operational history or Receiver Devices. "
                "Archive it instead. " + summary.explain()
            )

        connection.execute(text("DELETE FROM stores WHERE id = :i"), {"i": store_id})
        return {"id": row.id, "store_code": row.store_code, "store_name": row.store_name}


def delete_user_if_unused(engine: Engine, *, user_id: int, typed_confirmation: str,
                          actor_id: int | None = None) -> dict:
    """Remove one account that never did anything. Refuses anything else.

    Two refusals come before any dependency count, because neither is about
    history: an OWNER is never hard-deleted, and nobody deletes the account they
    are signed in as.
    """
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT id, username, role FROM hq_users WHERE id = :i"),
            {"i": user_id}).first()
        if row is None:
            raise DeletionRefused("That account no longer exists.")

        if str(row.role).upper() in {"OWNER", "SUPER_ADMIN"}:
            raise DeletionRefused(
                "An OWNER account is never hard-deleted. Losing the last one cannot "
                "be undone from inside EchoCast. Archive it instead."
            )
        if actor_id is not None and int(actor_id) == int(user_id):
            raise DeletionRefused(
                "You cannot delete your own account. Ask another administrator."
            )
        if typed_confirmation != row.username:
            raise DeletionRefused(
                f"The typed confirmation did not match. Type the username exactly: "
                f"{row.username}"
            )

        summary = _count_dependencies(
            connection, USER_DEPENDENCY_TABLES,
            lambda table: _USER_COLUMNS.get(table, "user_id"), user_id)
        if summary.total or summary.unchecked:
            raise DeletionRefused(
                "This account is recorded as the actor in operational history. "
                "Archive it instead. " + summary.explain()
            )

        # Only the account row. Audit history is never removed to make a delete
        # possible - that would erase the record of what somebody did in order
        # to erase the fact that they existed.
        connection.execute(text("DELETE FROM hq_users WHERE id = :i"), {"i": user_id})
        return {"id": row.id, "username": row.username, "role": row.role}
