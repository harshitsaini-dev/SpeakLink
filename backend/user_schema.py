"""One call that prepares ``hq_users`` for the code that reads and writes it.

WHY THIS EXISTS

The migrations were correct. The *order* was correct. What was missing was a
single place holding that order, so every caller had to remember it - and one
of them did not.

``tools/create_owner.py`` called ``ensure_user_lifecycle_schema`` and stopped
there. That adds four columns. The fifth, ``session_version``, is added by
``ensure_rbac_schema``, which the command never called. Against a database that
had already been through application startup, everything worked. Against the
real HQ database, which had the original six-column table, the insert failed:

    sqlite3.OperationalError: table hq_users has no column named session_version

The password had already been typed and hashed by then. Nothing was written -
the transaction failed - but the operator got a raw traceback for what is really
an ordinary "this database is older than this code" situation.

WHAT THE ORDER IS, AND WHY - INCLUDING A SECOND BUG FOUND HERE

**Every column first. Only then anything that reads rows through the ORM.**

That rule is not decorative. ``migrate_legacy_roles`` queries the ``HQUser``
model, and that model gained ``display_name``, ``lifecycle_state``,
``disabled_at`` and ``archived_at`` when User management was added. So its
SELECT asks for columns that ``ensure_user_lifecycle_schema`` has not created
yet, and on a legacy table it fails with::

    (sqlite3.OperationalError) no such column: hq_users.display_name

Application startup had it in the old order too, wrapped in a ``try`` that logs
"Role model could not be prepared" and carries on. On a fresh database
``create_all`` builds the full table so the step works and nobody notices; on
the real legacy database it has been failing silently, which means those roles
were never normalised.

1. ``ensure_rbac_schema``           - adds ``session_version``.
2. ``ensure_user_lifecycle_schema`` - adds ``lifecycle_state``, ``display_name``,
                                      ``disabled_at``, ``archived_at``, and
                                      backfills lifecycle from ``is_active``.
                                      Depends only on ``is_active``, so it is
                                      safe this early.
3. ``migrate_legacy_roles``         - ORM-based, so it runs only once every
                                      column it selects exists. Turns the legacy
                                      lowercase ``admin`` role into the named
                                      role model and promotes the lowest-id
                                      active administrator to OWNER if there is
                                      none - a system with no OWNER can never
                                      change its own security settings again.
4. ``migrate_super_admin_to_owner`` - renames the stored ``SUPER_ADMIN`` string.
                                      Last, because it rewrites a value the step
                                      above may have just written.

Every step is additive and idempotent. None deletes a row, rebuilds the table,
touches a password hash, or creates an account.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from rbac import ensure_rbac_schema, migrate_legacy_roles
from user_lifecycle import (
    ensure_user_lifecycle_schema,
    migrate_super_admin_to_owner,
)


class UserSchemaError(RuntimeError):
    """The user tables could not be prepared. Carries no credential."""


#: Every column ``user_lifecycle.create_user`` writes or reads back.
#:
#: Written down rather than remembered, so a column added to ``create_user``
#: without a migration to add it fails a test here instead of failing on
#: somebody's live database at the moment they were told to run a command.
REQUIRED_USER_COLUMNS = frozenset({
    "id",
    "username",
    "display_name",
    "password_hash",
    "role",
    "is_active",
    "lifecycle_state",
    "session_version",
    "created_at",
    "disabled_at",
    "archived_at",
})


def user_table_columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(hq_users)")}


def ensure_user_auth_schema(engine: Engine, *, session_factory=None,
                            promote_missing_owner: bool = True) -> None:
    """Bring ``hq_users`` up to what the current code needs.

    Safe to call on a fully migrated database - every step then finds nothing to
    do. Safe to call on the original six-column table. Safe to call twice.

    Raises ``UserSchemaError`` rather than letting a driver exception escape, so
    a caller can print a sentence instead of a stack trace.
    """
    if session_factory is None:
        session_factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    try:
        with engine.connect() as connection:
            tables = {
                row[0] for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'")
            }
        if "hq_users" not in tables:
            raise UserSchemaError(
                "this database has no hq_users table, so it is not an EchoCast HQ "
                "database. Check ECHOCAST_DB_PATH points at the right file."
            )

        # Columns first, ORM-based data migrations second. See the module
        # docstring: migrate_legacy_roles selects through the HQUser model,
        # which knows about columns ensure_user_lifecycle_schema creates.
        ensure_rbac_schema(engine)
        ensure_user_lifecycle_schema(engine)
        migrate_legacy_roles(session_factory,
                             promote_missing_owner=promote_missing_owner)
        migrate_super_admin_to_owner(engine)
    except UserSchemaError:
        raise
    except Exception as failure:
        # The driver's message names SQLAlchemy internals and is useless to an
        # operator. The class name is kept because it is the only part worth
        # searching for.
        raise UserSchemaError(
            f"the user tables could not be prepared ({failure.__class__.__name__}). "
            "No account was created and nothing was changed."
        ) from None

    missing = sorted(REQUIRED_USER_COLUMNS - user_table_columns(engine))
    if missing:
        # Belt and braces. If this ever fires, a migration exists for a column
        # nobody wired into the order above - which is the exact shape of the
        # bug this module was written to end.
        raise UserSchemaError(
            "the user table is still missing " + ", ".join(missing) +
            " after migration. No account was created."
        )


def describe_user_schema(engine: Engine) -> dict:
    """Read-only summary for diagnostics. Never reads a password hash."""
    columns = user_table_columns(engine)
    with engine.connect() as connection:
        total = connection.execute(text("SELECT COUNT(*) FROM hq_users")).scalar()
        roles = {
            row[0]: row[1] for row in
            connection.execute(text("SELECT role, COUNT(*) FROM hq_users GROUP BY role"))
        }
    return {
        "columns": sorted(columns),
        "missing": sorted(REQUIRED_USER_COLUMNS - columns),
        "user_count": total,
        "roles": roles,
    }
