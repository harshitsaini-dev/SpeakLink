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
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS store_scope_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                target_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                scope_type TEXT NOT NULL CHECK (scope_type IN ('STORE', 'CITY', 'REGION')),
                scope_label TEXT NOT NULL,
                action TEXT NOT NULL CHECK (action IN ('ADDED', 'REMOVED')),
                created_at TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_store_scope_audit_target "
            "ON store_scope_audit_events(target_user_id)"
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
                # A permanently deleted Store grants no scope - it must stay
                # absent from every operational surface, including this one,
                # even though the assignment row itself is left alone (it is
                # historical evidence of what was once assigned, not a live
                # grant).
                still_exists = connection.execute(
                    text("SELECT 1 FROM stores WHERE id = :id AND "
                         "(lifecycle_state IS NULL OR lifecycle_state != 'deleted')"),
                    {"id": row.store_id},
                ).first()
                if still_exists:
                    store_ids.add(row.store_id)
            else:
                column = "city" if row.scope_type == "CITY" else "region"
                matched = connection.execute(
                    text(f"SELECT id FROM stores WHERE {column} = :value AND "
                         "(lifecycle_state IS NULL OR lifecycle_state != 'deleted')"),
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


def _entry_key(scope_type: str, *, store_id=None, scope_value=None) -> tuple:
    return (scope_type, store_id, scope_value)


def _store_label(connection, store_id: int) -> str:
    row = connection.execute(
        text("SELECT store_code, store_name FROM stores WHERE id = :id"), {"id": store_id}
    ).first()
    return f"{row.store_name} ({row.store_code})" if row else f"store#{store_id}"


def set_user_scope(engine: Engine, *, user_id: int, entries: list[dict],
                   actor_id: int | None = None) -> list[dict]:
    """Replace this account's entire scope set in one transaction. An empty
    list means "unrestricted again" - the same "no rows means no
    restriction" rule resolve_store_scope reads.

    Every row added or removed by this call is recorded in
    ``store_scope_audit_events`` with a safe, human-readable label (a Store's
    name and code, or the city/Zone name) - never an internal id alone, and
    never anything that could be mistaken for a credential. Returns the list
    of audit rows written, for the caller to log a count without needing a
    second query.
    """
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM hq_users WHERE id = :user_id"), {"user_id": user_id}
        ).first()
        if not exists:
            raise InvalidScopeEntry("That account no longer exists.")

        before_rows = connection.execute(
            text(
                "SELECT scope_type, store_id, scope_value FROM user_store_scope "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ).all()
        before = {
            _entry_key(r.scope_type, store_id=r.store_id, scope_value=r.scope_value): r
            for r in before_rows
        }

        validated: list[dict] = []
        for entry in entries:
            scope_type = entry.get("scope_type")
            if scope_type not in SCOPE_TYPES:
                raise InvalidScopeEntry(f"Unknown scope_type: {scope_type!r}")
            if scope_type == "STORE":
                store_id = entry.get("store_id")
                if not store_id:
                    raise InvalidScopeEntry("A STORE scope entry needs a store_id.")
                found = connection.execute(
                    text("SELECT 1 FROM stores WHERE id = :id AND "
                         "(lifecycle_state IS NULL OR lifecycle_state != 'deleted')"),
                    {"id": store_id}
                ).first()
                if not found:
                    raise InvalidScopeEntry(f"No Store with id {store_id}.")
                validated.append({"scope_type": "STORE", "store_id": store_id, "scope_value": None})
            else:
                value = (entry.get("scope_value") or "").strip()
                if not value:
                    raise InvalidScopeEntry(f"A {scope_type} scope entry needs a scope_value.")
                column = "city" if scope_type == "CITY" else "region"
                # Canonical-value guard: a City/Zone that matches zero Stores
                # right now (a typo, or a retired name) must never be saved -
                # it would silently resolve to nothing later, which looks
                # identical to "no assignments" only from the wrong side.
                known = connection.execute(
                    text(f"SELECT 1 FROM stores WHERE {column} = :value AND "
                         "(lifecycle_state IS NULL OR lifecycle_state != 'deleted') LIMIT 1"),
                    {"value": value},
                ).first()
                if not known:
                    raise InvalidScopeEntry(
                        f"No Store currently has {scope_type.title()} = {value!r}."
                    )
                validated.append({"scope_type": scope_type, "store_id": None, "scope_value": value})

        after = {
            _entry_key(e["scope_type"], store_id=e["store_id"], scope_value=e["scope_value"]): e
            for e in validated
        }

        connection.execute(
            text("DELETE FROM user_store_scope WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        for entry in validated:
            connection.execute(
                text(
                    "INSERT INTO user_store_scope "
                    "(user_id, scope_type, store_id, scope_value, created_at) "
                    "VALUES (:user_id, :scope_type, :store_id, :scope_value, :now)"
                ),
                {"user_id": user_id, "scope_type": entry["scope_type"],
                 "store_id": entry["store_id"], "scope_value": entry["scope_value"], "now": now},
            )

        def label(scope_type, store_id, scope_value):
            return _store_label(connection, store_id) if scope_type == "STORE" else scope_value

        audit_rows: list[dict] = []
        removed_keys = set(before) - set(after)
        added_keys = set(after) - set(before)
        for key in removed_keys:
            scope_type, store_id, scope_value = key
            audit_rows.append({"scope_type": scope_type,
                               "scope_label": label(scope_type, store_id, scope_value),
                               "action": "REMOVED"})
        for key in added_keys:
            scope_type, store_id, scope_value = key
            audit_rows.append({"scope_type": scope_type,
                               "scope_label": label(scope_type, store_id, scope_value),
                               "action": "ADDED"})

        for row in audit_rows:
            connection.execute(
                text(
                    "INSERT INTO store_scope_audit_events "
                    "(actor_user_id, target_user_id, scope_type, scope_label, action, created_at) "
                    "VALUES (:actor_id, :target_id, :scope_type, :scope_label, :action, :now)"
                ),
                {"actor_id": actor_id, "target_id": user_id, "scope_type": row["scope_type"],
                 "scope_label": row["scope_label"], "action": row["action"], "now": now},
            )
        return audit_rows


def list_scope_audit_events(engine: Engine, *, user_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT actor_user_id, target_user_id, scope_type, scope_label, action, "
                "created_at FROM store_scope_audit_events WHERE target_user_id = :user_id "
                "ORDER BY id"
            ),
            {"user_id": user_id},
        ).all()
    return [
        {"actor_user_id": r.actor_user_id, "target_user_id": r.target_user_id,
         "scope_type": r.scope_type, "scope_label": r.scope_label, "action": r.action,
         "created_at": r.created_at}
        for r in rows
    ]


__all__ = [
    "SCOPE_TYPES",
    "InvalidScopeEntry",
    "ensure_store_scope_schema",
    "list_scope_audit_events",
    "list_user_scope",
    "resolve_store_scope",
    "set_user_scope",
]
