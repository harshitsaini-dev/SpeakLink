"""How every SQLite connection in this product is configured.

WHY THIS IS ITS OWN FILE

These three pragmas were set in db.py, on the application's engine, and
nowhere else. Anything that opened its own engine - a test, a tool, a
migration - got SQLite's defaults instead: no WAL, no foreign keys, and no
patience for a busy database.

That is not a tidiness problem. It meant a test could report a failure the
product would never have, and - worse in the other direction - a tool could
write to the live database in a way the application had been configured to
avoid. One function, called wherever an engine is made.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.engine import Engine


def apply_sqlite_pragmas(engine: Engine) -> None:
    """Configure every connection this engine opens.

    Registered as a connect listener rather than executed once, because a
    pragma is a property of a CONNECTION: a pool that opens a second one later
    would otherwise have it on the first and not the second, which is the kind
    of bug that only appears under load.
    """
    if engine.dialect.name != "sqlite":
        # PostgreSQL enforces foreign keys unconditionally and has its own
        # concurrency model. There is nothing here to apply.
        return

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        # Readers do not block the writer, and the writer does not block
        # readers. Without it, every page load competes with every write.
        cursor.execute("PRAGMA journal_mode=WAL")
        # The schema's relationships are enforced by the database rather than
        # remembered by the application.
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAIT for a busy database instead of failing on it.
        #
        # Two writers that collide otherwise produce "database is locked"
        # immediately, and the caller sees an internal error for what was only
        # ever a queue. Six Store computers redeeming enrolment codes at the
        # same moment did exactly that: one enrolled, one was correctly
        # refused, and the rest were told the server had broken.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
