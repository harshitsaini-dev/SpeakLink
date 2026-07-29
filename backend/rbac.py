"""Who may do what, decided here and enforced in the backend.

Every account was an administrator: ``hq_users.role`` defaulted to ``"admin"``
and no endpoint read it. The person who starts broadcasts could rotate Receiver
credentials; the person who only watches Store status could archive a Store.
That is not a policy, it is the absence of one.

Four roles and one fixed matrix. The matrix is a table in this module rather
than a decorator on each route, so it can be read in one screen and argued with,
instead of reconstructed by grepping twenty endpoints and hoping none was
missed.

**Hiding a button is not a permission check.** Everything here is enforced
server-side; the frontend hides controls only so people are not offered actions
that will be refused.

Two protections exist because losing them cannot be undone from inside the
product:

* the last active SUPER_ADMIN cannot be disabled, archived or demoted. Without
  that, an HQ can lock itself out of its own security settings with one click,
  and the only way back is editing the database by hand;
* nobody can disable or archive the account they are currently signed in as -
  the same accident wearing a friendlier face.

Failure is closed everywhere. An unknown role grants nothing rather than
everything, and an inactive user grants nothing regardless of role.
"""

from __future__ import annotations

from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Engine


class Role(str, Enum):
    #: The account that owns the system. Formerly called SUPER_ADMIN.
    #:
    #: Renamed rather than joined by a second top-level role. A separate OWNER
    #: beside SUPER_ADMIN would have meant two accounts-of-last-resort, two
    #: "this one may not be removed" rules, and eventually only one of them
    #: being applied - which is exactly the lockout the rule exists to prevent.
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    BROADCASTER = "BROADCASTER"
    VIEWER = "VIEWER"


#: Old role strings that still resolve. Rows written before the rename say
#: SUPER_ADMIN, and so does any token minted in the minutes before an upgrade.
#: Refusing them would sign out every administrator at the moment of upgrade -
#: including the one running it.
LEGACY_ROLE_ALIASES: dict[str, Role] = {
    # The literal old string. A bulk rename briefly turned this into
    # {"OWNER": Role.OWNER}, which is a no-op that silently removed backward
    # compatibility while looking entirely reasonable - every stored
    # SUPER_ADMIN row would have parsed to None, meaning no permissions at all.
    "SUPER_ADMIN": Role.OWNER,
}


class Permission(str, Enum):
    MANAGE_SECURITY = "MANAGE_SECURITY"
    MANAGE_USERS = "MANAGE_USERS"
    MANAGE_STORES = "MANAGE_STORES"
    MANAGE_DEVICES = "MANAGE_DEVICES"
    START_BROADCAST = "START_BROADCAST"
    STOP_BROADCAST = "STOP_BROADCAST"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    VIEW_STATUS = "VIEW_STATUS"
    VIEW_HISTORY = "VIEW_HISTORY"
    VIEW_LOGS = "VIEW_LOGS"


ALL_PERMISSIONS = frozenset(Permission)

_VIEW_ONLY = {Permission.VIEW_STATUS, Permission.VIEW_HISTORY}

_BROADCAST = {
    Permission.START_BROADCAST,
    Permission.STOP_BROADCAST,
    Permission.EMERGENCY_STOP,
}

#: The whole policy, in one place.
#:
#: ADMIN deliberately lacks MANAGE_SECURITY. An administrator runs the estate -
#: Stores, Devices, broadcasts, and the accounts of people who use them - but
#: changing how the system authenticates is a different kind of decision, and it
#: is the one an attacker who compromises an ADMIN account would reach for
#: first.
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: ALL_PERMISSIONS,
    Role.ADMIN: frozenset(
        {
            Permission.MANAGE_USERS,
            Permission.MANAGE_STORES,
            Permission.MANAGE_DEVICES,
            Permission.VIEW_LOGS,
        }
        | _BROADCAST
        | _VIEW_ONLY
    ),
    Role.BROADCASTER: frozenset(_BROADCAST | _VIEW_ONLY),
    Role.VIEWER: frozenset(_VIEW_ONLY),
}

#: Which roles each role may create, edit, disable, archive or assign.
#:
#: ADMIN cannot manage another ADMIN: two administrators disabling each other is
#: a support call nobody wins. ADMIN cannot manage or create a SUPER_ADMIN,
#: because being able to promote yourself makes every other restriction here
#: decorative.
_MANAGEABLE_ROLES: dict[Role, frozenset[Role]] = {
    Role.OWNER: frozenset(Role),
    Role.ADMIN: frozenset({Role.BROADCASTER, Role.VIEWER}),
    Role.BROADCASTER: frozenset(),
    Role.VIEWER: frozenset(),
}


class RbacError(RuntimeError):
    """Base class, so no caller handles one refusal and misses another."""


class PermissionDenied(RbacError):
    """This account may not do that.

    The message is deliberately identical for every permission. Naming the one
    that was missing tells a caller the shape of the system, and tells an
    attacker which account is worth taking next.
    """

    def __init__(self) -> None:
        super().__init__("You do not have permission to perform this action.")


def parse_role(value: object) -> Role | None:
    """A known role, or None. Never a guess."""
    if isinstance(value, Role):
        return value
    if not isinstance(value, str):
        return None
    candidate = value.strip().upper()
    try:
        return Role(candidate)
    except ValueError:
        return LEGACY_ROLE_ALIASES.get(candidate)


def effective_permissions(user) -> frozenset[Permission]:
    """What this account can actually do right now.

    Inactive means nothing, whatever the role says: a disabled SUPER_ADMIN is a
    disabled account, and reading the role first would invert that.
    """
    if user is None or not getattr(user, "is_active", False):
        return frozenset()
    role = parse_role(getattr(user, "role", None))
    if role is None:
        # Fail closed. A typo in a role string must not become a wildcard.
        return frozenset()
    return ROLE_PERMISSIONS[role]


def has_permission(user, permission: Permission) -> bool:
    return permission in effective_permissions(user)


def require_permission(user, permission: Permission) -> None:
    if not has_permission(user, permission):
        raise PermissionDenied()


def may_manage_role(actor_role, target_role) -> bool:
    """May an account with ``actor_role`` act on one with ``target_role``?"""
    actor = parse_role(actor_role)
    target = parse_role(target_role)
    if actor is None or target is None:
        return False
    return target in _MANAGEABLE_ROLES[actor]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_rbac_schema(engine: Engine) -> None:
    """Add ``hq_users.session_version`` if missing. Additive and idempotent.

    A single integer is the whole revocation mechanism: the JWT carries the
    value it was minted with, every authenticated request compares it with the
    database, and anything that must end a session - a password change, a role
    change, a disable, an archive - increments it. Existing tokens then fail on
    their next request rather than remaining valid for the rest of their eight
    hours.

    Chosen over a token blacklist because a blacklist needs storage,
    expiry sweeping, and shared state across workers. This needs one column and
    one comparison, and EchoCast runs exactly one Uvicorn worker anyway.
    """
    with engine.begin() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(hq_users)")
        }
        if "session_version" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE hq_users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            text("UPDATE hq_users SET session_version = 1 WHERE session_version IS NULL")
        )


LEGACY_ADMIN_ROLE = "admin"


def migrate_legacy_roles(session_factory) -> dict:
    """Turn the legacy single ``admin`` role into the four-role model.

    The rule is deterministic and written down here because an upgrade runs
    once and nobody gets to re-read the code while it is happening:

    1. every legacy ``admin`` becomes ``ADMIN`` - nobody loses access, so the
       upgrade cannot lock anybody out of work they were doing yesterday;
    2. if the database has no SUPER_ADMIN, the **lowest-id active** legacy
       administrator is promoted to one. A system with no SUPER_ADMIN can never
       change its own security settings again;
    3. nobody is created. If there is no active account to promote, the result
       says so rather than inventing one.

    Idempotent: running it again finds no legacy roles and no missing
    SUPER_ADMIN, and changes nothing. It touches no password hash.
    """
    from models import HQUser

    with session_factory() as db:
        legacy = (
            db.query(HQUser)
            .filter(HQUser.role.notin_([role.value for role in Role]))
            .all()
        )
        for user in legacy:
            user.role = Role.ADMIN.value
        if legacy:
            db.commit()

        existing_super = (
            db.query(HQUser)
            .filter(HQUser.role == Role.OWNER.value)
            .order_by(HQUser.id)
            .first()
        )
        if existing_super is not None:
            return {
                "super_admin_user_id": existing_super.id,
                "promoted": False,
                "migrated_legacy_roles": len(legacy),
                "created_users": 0,
            }

        candidate = (
            db.query(HQUser)
            .filter(HQUser.role == Role.ADMIN.value, HQUser.is_active.is_(True))
            .order_by(HQUser.id)
            .first()
        )
        if candidate is None:
            # Nothing to promote. Deliberately not creating an account: an
            # upgrade that mints an administrator is an upgrade that hands out
            # credentials nobody asked for.
            return {
                "super_admin_user_id": None,
                "promoted": False,
                "migrated_legacy_roles": len(legacy),
                "created_users": 0,
            }

        candidate.role = Role.OWNER.value
        db.commit()
        return {
            "super_admin_user_id": candidate.id,
            "promoted": True,
            "migrated_legacy_roles": len(legacy),
            "created_users": 0,
        }


__all__ = [
    "ALL_PERMISSIONS",
    "LEGACY_ADMIN_ROLE",
    "ROLE_PERMISSIONS",
    "Permission",
    "PermissionDenied",
    "RbacError",
    "Role",
    "effective_permissions",
    "ensure_rbac_schema",
    "has_permission",
    "may_manage_role",
    "migrate_legacy_roles",
    "parse_role",
    "require_permission",
]
