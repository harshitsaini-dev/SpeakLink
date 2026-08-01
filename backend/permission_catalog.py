"""Fine-grained, per-user permissions layered on top of the four HQ roles.

``rbac.py`` already has a coarse, ten-permission role matrix and a central
``require(Permission.X)`` FastAPI dependency - the right shape, the wrong
grain. "Can edit a Store" and "can archive a Store" were the same
``MANAGE_STORES`` flag, so there was no way to let someone view Stores without
also letting them archive one.

This module adds a second, finer layer UNDER the same central-resolver
philosophy: one canonical catalog of permission codes, one default role
matrix, one table of per-user ALLOW/DENY overrides, and one function -
``resolve_effective_permissions`` - that everything else calls. Nothing
outside this module and ``server.py``'s ``require()`` factory ever compares a
role string directly; that is exactly the scattered ``if user.role == "ADMIN"``
pattern this replaces.

Effective permission, in order:

    explicit DENY override  >  explicit ALLOW override  >  role default  >  deny

Only one override row can exist per (user, permission) - the unique
constraint enforces it - so "DENY beats ALLOW" is really just "an override,
whichever effect it carries, beats the role default." INHERIT is not a stored
value; it is the absence of a row.

Nothing here decides who is signed in or whether their session is still valid
- that remains ``auth.py``/``rbac.py``. This only decides what an
already-authenticated, already-active account may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Engine

from rbac import PermissionDenied, Role, parse_role


class Effect(str, Enum):
    INHERIT = "INHERIT"
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    group: str
    label: str


#: The one catalog. A permission that is not listed here does not exist:
#: ``resolve_effective_permissions`` only ever returns codes drawn from this
#: list, and ``set_permission_overrides`` refuses an unknown code rather than
#: silently storing it.
PERMISSION_DEFINITIONS: tuple[PermissionDefinition, ...] = (
    PermissionDefinition("menu.broadcast.view", "Broadcast", "View Broadcast Console"),
    PermissionDefinition("broadcast.start", "Broadcast", "Start Broadcast"),
    PermissionDefinition("broadcast.stop", "Broadcast", "Stop Broadcast"),
    PermissionDefinition("broadcast.emergency_stop", "Broadcast", "Emergency Stop"),

    PermissionDefinition("menu.stores.view", "Stores", "View Store Management"),
    PermissionDefinition("stores.create", "Stores", "Create Store"),
    PermissionDefinition("stores.update", "Stores", "Edit Store"),
    PermissionDefinition("stores.archive", "Stores", "Disable / Archive Store"),

    PermissionDefinition("menu.receivers.view", "Receivers", "View Receiver Status"),
    PermissionDefinition("devices.enrollment.create", "Receivers", "Create Enrolment Code"),
    PermissionDefinition("devices.primary.assign", "Receivers", "Assign Primary Device"),
    PermissionDefinition("devices.rotate", "Receivers", "Rotate Device Credential"),
    PermissionDefinition("devices.disable", "Receivers", "Disable Device"),
    PermissionDefinition("devices.revoke", "Receivers", "Revoke Device"),

    PermissionDefinition("menu.history.view", "History", "View Broadcast History"),

    PermissionDefinition("menu.logs.view", "Logs", "View System Logs"),

    PermissionDefinition("menu.users.view", "Users", "View User Management"),
    PermissionDefinition("users.create", "Users", "Create User"),
    PermissionDefinition("users.update", "Users", "Edit User"),
    PermissionDefinition("users.disable", "Users", "Disable / Archive User"),
    PermissionDefinition("users.permissions.manage", "Users", "Manage User Rights"),
)

PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSION_DEFINITIONS)
PERMISSIONS_BY_CODE: dict[str, PermissionDefinition] = {p.code: p for p in PERMISSION_DEFINITIONS}

_ALL_CODES = frozenset(PERMISSION_CODES)
_BROADCAST_CODES = frozenset({
    "menu.broadcast.view", "broadcast.start", "broadcast.stop", "broadcast.emergency_stop",
})
_VIEW_ONLY_CODES = frozenset({
    "menu.broadcast.view", "menu.stores.view", "menu.receivers.view",
    "menu.history.view", "menu.logs.view",
})

#: The default role matrix. This is the ONE place a role's fine-grained rights
#: are decided; overrides are layered on top of it in
#: ``resolve_effective_permissions``, never merged into it.
#:
#: ADMIN deliberately gets everything except ``users.permissions.manage`` -
#: the same reasoning ``rbac.ROLE_PERMISSIONS`` already applies to
#: ``MANAGE_SECURITY``: running the estate is a different kind of trust than
#: deciding who else gets to run it.
DEFAULT_ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: _ALL_CODES,
    Role.ADMIN: _ALL_CODES - frozenset({"users.permissions.manage"}),
    # Broadcast Console, its three controls, History and Receiver Status -
    # nothing that edits a Store, a Device's security, or a User.
    Role.BROADCASTER: _BROADCAST_CODES | frozenset({
        "menu.history.view", "menu.receivers.view",
    }),
    # Read-only, and only over the operational surfaces - not User Management,
    # matching the existing frontend nav restriction (`roles: ["OWNER", "ADMIN"]`
    # on the Users link) and the fact VIEWER never held MANAGE_USERS before.
    Role.VIEWER: _VIEW_ONLY_CODES - frozenset({"menu.users.view"}),
}


def _require_known_code(code: str) -> None:
    if code not in PERMISSION_CODES:
        raise UnknownPermissionCode(code)


class UnknownPermissionCode(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Unknown permission code: {code}")
        self.code = code


class OwnerOverrideRefused(RuntimeError):
    """An OWNER's effective rights are never narrowed by an override.

    OWNER already holds every permission by role default, and OWNER is the
    account of last resort - the one that can always fix a lockout. Allowing a
    DENY override on an OWNER would let a single click on the wrong row create
    exactly the lockout the role exists to prevent, and unlike disabling the
    last OWNER (guarded in ``user_lifecycle.py``) there is no equivalent
    per-permission guard to fall back on, so this is refused outright rather
    than only for the last one.
    """

    def __init__(self) -> None:
        super().__init__("OWNER permissions cannot be overridden.")


def ensure_permission_schema(engine: Engine) -> None:
    """Additive and idempotent. Never touches ``user_permission_overrides``
    or ``permission_audit_events`` once created - those hold operator
    decisions and history, not derived configuration."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS permissions (
                code TEXT PRIMARY KEY,
                permission_group TEXT NOT NULL,
                label TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS role_permissions (
                role TEXT NOT NULL,
                permission_code TEXT NOT NULL REFERENCES permissions(code),
                PRIMARY KEY (role, permission_code)
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS user_permission_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES hq_users(id),
                permission_code TEXT NOT NULL REFERENCES permissions(code),
                effect TEXT NOT NULL CHECK (effect IN ('ALLOW', 'DENY')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, permission_code)
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_user "
            "ON user_permission_overrides(user_id)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS permission_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                target_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                permission_code TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_permission_audit_target "
            "ON permission_audit_events(target_user_id)"
        )

        # The catalog and the default matrix are DERIVED configuration - the
        # code above is the source of truth - so they are safely reseeded on
        # every boot. Re-running this is what keeps a running HQ in sync after
        # a permission is added or a role's default changes upstream.
        for definition in PERMISSION_DEFINITIONS:
            connection.execute(
                text(
                    "INSERT INTO permissions (code, permission_group, label) "
                    "VALUES (:code, :group_name, :label) "
                    "ON CONFLICT(code) DO UPDATE SET "
                    "permission_group=excluded.permission_group, label=excluded.label"
                ),
                {"code": definition.code, "group_name": definition.group, "label": definition.label},
            )
        connection.exec_driver_sql("DELETE FROM role_permissions")
        for role, codes in DEFAULT_ROLE_PERMISSIONS.items():
            for code in codes:
                connection.execute(
                    text(
                        "INSERT INTO role_permissions (role, permission_code) "
                        "VALUES (:role, :code)"
                    ),
                    {"role": role.value, "code": code},
                )


def _role_default_codes(engine: Engine, role: Role) -> frozenset[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT permission_code FROM role_permissions WHERE role = :role"),
            {"role": role.value},
        ).all()
    if rows:
        return frozenset(row[0] for row in rows)
    # Table not seeded yet (e.g. a test engine that never ran
    # ensure_permission_schema) - fall back to the in-code matrix so the
    # resolver still fails closed rather than granting nothing by accident
    # only in some environments and something else in others.
    return DEFAULT_ROLE_PERMISSIONS.get(role, frozenset())


def _overrides_for_user(engine: Engine, user_id: int) -> dict[str, Effect]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT permission_code, effect FROM user_permission_overrides "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ).all()
    return {row[0]: Effect(row[1]) for row in rows}


def resolve_effective_permissions(engine: Engine, user) -> frozenset[str]:
    """What this account can actually do right now, including overrides.

    Fails closed exactly like ``rbac.effective_permissions``: an inactive
    account or an unparsable role grants nothing, never "everything".
    """
    if user is None or not getattr(user, "is_active", False):
        return frozenset()
    role = parse_role(getattr(user, "role", None))
    if role is None:
        return frozenset()

    base = set(_role_default_codes(engine, role))
    for code, effect in _overrides_for_user(engine, user.id).items():
        if effect is Effect.ALLOW:
            base.add(code)
        elif effect is Effect.DENY:
            base.discard(code)
    return frozenset(base)


def has_permission_code(engine: Engine, user, code: str) -> bool:
    return code in resolve_effective_permissions(engine, user)


def require_permission_code(engine: Engine, user, code: str) -> None:
    if not has_permission_code(engine, user, code):
        raise PermissionDenied()


def describe_user_permissions(engine: Engine, *, user_id: int, role: Role) -> list[dict]:
    """Per-permission breakdown for the rights-editor UI: role default,
    stored override (or INHERIT), and the effective result - so the UI can
    show "Role: Allowed / Override: DENY / Effective: Denied" without the
    frontend re-implementing the priority rule."""
    role_defaults = _role_default_codes(engine, role)
    overrides = _overrides_for_user(engine, user_id)
    rows = []
    for definition in PERMISSION_DEFINITIONS:
        role_allowed = definition.code in role_defaults
        override = overrides.get(definition.code, Effect.INHERIT)
        if override is Effect.ALLOW:
            effective = True
        elif override is Effect.DENY:
            effective = False
        else:
            effective = role_allowed
        rows.append({
            "code": definition.code,
            "group": definition.group,
            "label": definition.label,
            "role_allowed": role_allowed,
            "override": override.value,
            "effective": effective,
        })
    return rows


def set_permission_overrides(
    engine: Engine,
    *,
    actor,
    target_user_id: int,
    target_role: Role,
    changes: list[dict],
) -> list[dict]:
    """Apply a batch of {code, effect} changes for one user, audited one row
    at a time. Refuses the whole batch (no partial write) if any code is
    unknown or the target is an OWNER - the caller already gated this to
    OWNER-only actors (``require_super_admin``), so this is a second,
    independent check against narrowing an OWNER's rights, not the only one.
    """
    if target_role is Role.OWNER:
        raise OwnerOverrideRefused()

    parsed: list[tuple[str, Effect]] = []
    for change in changes:
        code = change["code"]
        _require_known_code(code)
        parsed.append((code, Effect(change["effect"])))

    now = datetime.now(timezone.utc).isoformat()
    audit_rows: list[dict] = []
    with engine.begin() as connection:
        existing = {
            row[0]: Effect(row[1])
            for row in connection.execute(
                text(
                    "SELECT permission_code, effect FROM user_permission_overrides "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": target_user_id},
            ).all()
        }
        for code, effect in parsed:
            old_value = existing.get(code, Effect.INHERIT)
            if effect is Effect.INHERIT:
                connection.execute(
                    text(
                        "DELETE FROM user_permission_overrides "
                        "WHERE user_id = :user_id AND permission_code = :code"
                    ),
                    {"user_id": target_user_id, "code": code},
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO user_permission_overrides "
                        "(user_id, permission_code, effect, created_at, updated_at) "
                        "VALUES (:user_id, :code, :effect, :now, :now) "
                        "ON CONFLICT(user_id, permission_code) DO UPDATE SET "
                        "effect=excluded.effect, updated_at=excluded.updated_at"
                    ),
                    {"user_id": target_user_id, "code": code, "effect": effect.value, "now": now},
                )
            connection.execute(
                text(
                    "INSERT INTO permission_audit_events "
                    "(actor_user_id, target_user_id, permission_code, old_value, "
                    "new_value, created_at) "
                    "VALUES (:actor_id, :target_id, :code, :old_value, :new_value, :now)"
                ),
                {
                    "actor_id": actor.id, "target_id": target_user_id, "code": code,
                    "old_value": old_value.value, "new_value": effect.value, "now": now,
                },
            )
            audit_rows.append({"code": code, "old_value": old_value.value, "new_value": effect.value})
    return audit_rows


__all__ = [
    "DEFAULT_ROLE_PERMISSIONS",
    "Effect",
    "OwnerOverrideRefused",
    "PERMISSION_CODES",
    "PERMISSION_DEFINITIONS",
    "PERMISSIONS_BY_CODE",
    "UnknownPermissionCode",
    "describe_user_permissions",
    "ensure_permission_schema",
    "has_permission_code",
    "require_permission_code",
    "resolve_effective_permissions",
    "set_permission_overrides",
]
