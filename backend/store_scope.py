"""Per-user Store/City/Zone scope, layered on top of role-based permissions.

A permission answers "may this account start a broadcast at all." Scope
answers a different question: "which Stores." An ADMIN or BROADCASTER can now
be limited to one Store, one city, or one Zone (region) - they see and manage
only what is assigned, everywhere a Store is listed, edited, or targeted.

An account with NO scope rows is unrestricted - this is an opt-in narrowing,
not a new default, so an existing pilot with nobody scoped keeps working
exactly as before this feature shipped. OWNER is always unrestricted:
scoping the account of last resort would create exactly the kind of
self-lockout risk permission overrides already refuse outright.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from rbac import Role, parse_role

SCOPE_TYPES = ("STORE", "CITY", "REGION")


class InvalidScopeEntry(ValueError):
    pass


def ensure_store_scope_schema(engine: Engine) -> None:
    """Additive and idempotent. Never rewrites an existing assignment other
    than by an explicit replace-the-set call from set_user_scope."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS user_store_scope (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES hq_users(id),
                scope_type TEXT NOT NULL CHECK (scope_type IN ('STORE', 'CITY', 'REGION')),
                store_id INTEGER REFERENCES stores(id),
                scope_value TEXT,
                created_at TEXT NOT NULL,
                CHECK (
                    (scope_type = 'STORE' AND store_id IS NOT NULL AND scope_value IS NULL)
                    OR
                    (scope_type IN ('CITY', 'REGION') AND store_id IS NULL AND scope_value IS NOT NULL)
                )
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_store_scope_user ON user_store_scope(user_id)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_store_scope_store "
            "ON user_store_scope(user_id, store_id) WHERE scope_type = 'STORE'"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_store_scope_value "
            "ON user_store_scope(user_id, scope_type, scope_value) "
            "WHERE scope_type IN ('CITY', 'REGION')"
        )


def resolve_store_scope(engine: Engine, user) -> frozenset[int] | None:
    """The set of Store ids this account may see and manage, or ``None`` for
    unrestricted (OWNER, or any account with no scope rows at all).

    An empty ``frozenset()`` is a real, different answer from ``None``: scope
    rows exist but currently resolve to zero Stores (e.g. a city assignment
    for a city with no Stores in it right now) - that is a genuine
    "nothing", never silently upgraded to "everything".
    """
    if user is None or not getattr(user, "is_active", False):
        return frozenset()
    role = parse_role(getattr(user, "role", None))
    if role is Role.OWNER:
        return None

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT scope_type, store_id, scope_value FROM user_store_scope "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user.id},
        ).all()
        if not rows:
            return None

        store_ids: set[int] = set()
        for row in rows:
            if row.scope_type == "STORE":
                store_ids.add(row.store_id)
            else:
                column = "city" if row.scope_type == "CITY" else "region"
                matched = connection.execute(
                    text(f"SELECT id FROM stores WHERE {column} = :value"),
                    {"value": row.scope_value},
                ).all()
                store_ids.update(r[0] for r in matched)
    return frozenset(store_ids)


def list_user_scope(engine: Engine, *, user_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, scope_type, store_id, scope_value FROM user_store_scope "
                "WHERE user_id = :user_id ORDER BY id"
            ),
            {"user_id": user_id},
        ).all()
    return [
        {"id": r.id, "scope_type": r.scope_type, "store_id": r.store_id, "scope_value": r.scope_value}
        for r in rows
    ]


def set_user_scope(engine: Engine, *, user_id: int, entries: list[dict]) -> None:
    """Replace this account's entire scope set in one transaction. An empty
    list means "unrestricted again" - the same "no rows means no
    restriction" rule resolve_store_scope reads."""
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM hq_users WHERE id = :user_id"), {"user_id": user_id}
        ).first()
        if not exists:
            raise InvalidScopeEntry("That account no longer exists.")

        connection.execute(
            text("DELETE FROM user_store_scope WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        for entry in entries:
            scope_type = entry.get("scope_type")
            if scope_type not in SCOPE_TYPES:
                raise InvalidScopeEntry(f"Unknown scope_type: {scope_type!r}")
            if scope_type == "STORE":
                store_id = entry.get("store_id")
                if not store_id:
                    raise InvalidScopeEntry("A STORE scope entry needs a store_id.")
                found = connection.execute(
                    text("SELECT 1 FROM stores WHERE id = :id"), {"id": store_id}
                ).first()
                if not found:
                    raise InvalidScopeEntry(f"No Store with id {store_id}.")
                connection.execute(
                    text(
                        "INSERT INTO user_store_scope "
                        "(user_id, scope_type, store_id, scope_value, created_at) "
                        "VALUES (:user_id, 'STORE', :store_id, NULL, :now)"
                    ),
                    {"user_id": user_id, "store_id": store_id, "now": now},
                )
            else:
                value = (entry.get("scope_value") or "").strip()
                if not value:
                    raise InvalidScopeEntry(f"A {scope_type} scope entry needs a scope_value.")
                connection.execute(
                    text(
                        "INSERT INTO user_store_scope "
                        "(user_id, scope_type, store_id, scope_value, created_at) "
                        "VALUES (:user_id, :scope_type, NULL, :value, :now)"
                    ),
                    {"user_id": user_id, "scope_type": scope_type, "value": value, "now": now},
                )


__all__ = [
    "SCOPE_TYPES",
    "InvalidScopeEntry",
    "ensure_store_scope_schema",
    "list_user_scope",
    "resolve_store_scope",
    "set_user_scope",
]
