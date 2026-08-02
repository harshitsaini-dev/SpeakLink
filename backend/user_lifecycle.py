"""A User's life: active, switched off, retired - and never actually deleted.

An HQ User is the author of broadcast history. Their id and username appear in
log lines saying who regenerated a Store token, who revoked a Device, who sent
an announcement to 44 Stores at nine on a Friday night. Deleting the row would
either orphan all of that or cascade it away, and both destroy the only record
of who did what. So "delete" means **archive** here: the row stays, the history
stays readable, and the account simply stops being one you can sign in as.

Three states, and the distinction between the last two is the point:

``active``
    Normal.

``disabled``
    Switched off and expected back - somebody on leave, an account being
    handed over. An administrator turns it on again.

``archived``
    Retired. Deliberately **not** reversible through the ordinary enable
    action, because "Enable" is a small button and un-retiring a person is not
    a small decision. Restoring puts the account back to ``disabled``, never
    straight to ``active``, so somebody has to look at its role and give it a
    password before it can sign in.

``is_active`` is kept in lockstep rather than replaced, exactly as for Stores:
login already filters on it, so it did not have to learn about archiving - and,
more usefully, cannot miss it.

TWO RULES CARRY MORE WEIGHT THAN THE REST

**The last active SUPER_ADMIN cannot be disabled, archived or demoted.** Not
because it would be untidy, but because an organisation that does it has no way
back in: there is no password-reset e-mail here, no support line, and the
recovery procedure is editing the database by hand.

**Nobody can disable or archive themselves.** That one is a safety net for the
person clicking, who is otherwise one click away from an action they cannot
undo. Editing your own display name is fine; it is not a lock-out.

Every action that must end an account's sessions bumps ``session_version``. A
JWT here is valid for eight hours and carries no server state, so without that
counter, disabling an account at 09:05 leaves whoever holds its token
broadcasting to 44 Stores until five in the evening.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from rbac import Role


ACTIVE = "active"
DISABLED = "disabled"
ARCHIVED = "archived"
#: Irreversible. Deliberately absent from every transition's allowed_from
#: below, so enable/disable/restore already refuse a deleted account through
#: the existing state machine rather than through a new special case. See
#: user_deletion.py.
DELETED = "deleted"
KNOWN_STATES = (ACTIVE, DISABLED, ARCHIVED, DELETED)

MAX_USERNAME_LENGTH = 100
MAX_DISPLAY_NAME_LENGTH = 200

#: Printable, no whitespace. A username is typed at a sign-in box, quoted in a
#: log line and read out over a phone; a space in one is a support call.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]*$")

#: Everything a caller may see. Deliberately not ``SELECT *``: adding a column
#: to the table must never silently start publishing it, and the two columns
#: most likely to be added next are exactly the two that must never leave.
_PUBLIC_COLUMNS = (
    "id", "username", "display_name", "role", "is_active",
    "lifecycle_state", "created_at", "disabled_at", "archived_at",
)


class UserLifecycleError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


class UserNotFoundError(UserLifecycleError):
    """No User with that identifier."""


class DuplicateUsernameError(UserLifecycleError):
    """That username already belongs to somebody - possibly somebody retired."""


class UserTransitionRefused(UserLifecycleError):
    """The move is not allowed from where this account currently is."""


class UserNotRestorableError(UserLifecycleError):
    """Only an archived account can be restored."""


class LastSuperAdminError(UserLifecycleError):
    """Doing this would leave nobody able to administer the system."""


class SelfActionRefused(UserLifecycleError):
    """You cannot switch yourself off."""


class RoleAssignmentRefused(UserLifecycleError):
    """That is not a role this system has."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def migrate_super_admin_to_owner(engine: Engine) -> int:
    """Rewrite the stored role string ``SUPER_ADMIN`` to ``OWNER``.

    Returns how many rows changed, so a caller can log "nothing to do" honestly
    rather than announcing a migration that did not happen.

    Forward-only, and deliberately tiny: one UPDATE inside one transaction, no
    table rebuild, no row deleted, no account created, no password hash read or
    written. Idempotent - the second run matches nothing.

    **Rollback**, if it were ever needed, is the mirror-image UPDATE:

        UPDATE hq_users SET role = 'SUPER_ADMIN' WHERE role = 'OWNER';

    No automatic down-migration is provided, because ``parse_role`` still
    accepts ``SUPER_ADMIN`` (see rbac.LEGACY_ROLE_ALIASES). A database that has
    not been migrated keeps working, and one that has can be reverted with the
    line above. There is no window in which an administrator is locked out.
    """
    with engine.begin() as connection:
        result = connection.execute(
            text("UPDATE hq_users SET role = :new WHERE role = :old"),
            {"new": Role.OWNER.value, "old": "SUPER_ADMIN"},
        )
        return int(result.rowcount or 0)


def ensure_user_lifecycle_schema(engine: Engine) -> None:
    """Add the lifecycle columns if they are missing, and backfill them.

    ``ALTER TABLE ... ADD COLUMN`` in SQLite rewrites no rows and rebuilds no
    indexes, so this is cheap enough to run at every boot rather than in a
    maintenance window.

    The backfill reads the existing ``is_active`` flag. An upgrade must not
    decide that everybody is suddenly archived, and it must not decide that
    somebody who was switched off is active again.
    """
    with engine.begin() as connection:
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(hq_users)")
        }
        for name, definition in (
            ("lifecycle_state", "VARCHAR(20)"),
            ("display_name", "VARCHAR(200)"),
            ("disabled_at", "DATETIME"),
            ("archived_at", "DATETIME"),
            # The irreversible-deletion columns belong HERE, not only in
            # user_deletion.ensure_user_deletion_schema, for the reason this
            # module's own ordering docstring gives: step 3
            # (migrate_legacy_roles) reads hq_users THROUGH THE ORM, so it
            # SELECTs every column declared on the HQUser model. Declaring
            # deleted_at/deleted_by on the model while creating them only in
            # a later migration meant that on a legacy table the ORM asked
            # for a column that did not exist yet and the whole
            # ensure_user_auth_schema sequence failed with an
            # OperationalError - which is exactly the bug the docstring above
            # was written about, reintroduced by a new column.
            ("deleted_at", "VARCHAR(40)"),
            ("deleted_by", "INTEGER"),
        ):
            if name not in columns:
                connection.exec_driver_sql(f"ALTER TABLE hq_users ADD COLUMN {name} {definition}")

        connection.execute(text(
            "UPDATE hq_users SET lifecycle_state = CASE WHEN is_active THEN :active ELSE :disabled END "
            "WHERE lifecycle_state IS NULL"
        ), {"active": ACTIVE, "disabled": DISABLED})
        # A person with no display name is shown by their username rather than
        # by a blank cell that looks like a broken row.
        connection.exec_driver_sql(
            "UPDATE hq_users SET display_name = username "
            "WHERE display_name IS NULL OR TRIM(display_name) = ''"
        )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def _row_to_record(row) -> dict:
    record = {column: getattr(row, column) for column in _PUBLIC_COLUMNS}
    record["is_active"] = bool(record["is_active"])
    return record


def _select(connection, where: str = "", parameters: dict | None = None):
    return connection.execute(
        text(f"SELECT {', '.join(_PUBLIC_COLUMNS)} FROM hq_users {where}"),
        parameters or {},
    )


def list_users(engine: Engine, *, include_deleted: bool = False) -> list[dict]:
    """Every account in every state.

    Archived accounts are included on purpose. Hiding them makes it look as
    though a username is free when it is not, and makes a retired colleague's
    history look like it was written by nobody.

    PERMANENTLY DELETED accounts are the one exception and are excluded by
    default. Unlike archived, that state is irreversible and the account is
    gone from the product - it is kept in the table only so history that
    names it stays readable. ``include_deleted=True`` is for the audit
    surfaces that deliberately want to see it.
    """
    where = ("ORDER BY id" if include_deleted else
             "WHERE lifecycle_state IS NULL OR lifecycle_state <> 'deleted' ORDER BY id")
    with engine.connect() as connection:
        return [_row_to_record(row) for row in _select(connection, where).all()]


def read_user(engine: Engine, *, user_id: int) -> dict:
    with engine.connect() as connection:
        row = _select(connection, "WHERE id = :user_id", {"user_id": user_id}).first()
    if row is None:
        raise UserNotFoundError(f"No HQ User with id {user_id}.")
    return _row_to_record(row)


def _require_row(connection, user_id: int):
    row = connection.execute(
        text("SELECT id, username, role, lifecycle_state FROM hq_users WHERE id = :user_id"),
        {"user_id": user_id},
    ).first()
    if row is None:
        raise UserNotFoundError(f"No HQ User with id {user_id}.")
    return row


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_username(username: str) -> str:
    candidate = (username or "").strip()
    if not candidate:
        raise UserLifecycleError("A username is required.")
    if len(candidate) > MAX_USERNAME_LENGTH:
        raise UserLifecycleError(f"A username may be at most {MAX_USERNAME_LENGTH} characters.")
    if not _USERNAME_PATTERN.match(candidate):
        raise UserLifecycleError(
            "A username may contain letters, digits, dot, dash, underscore and @, "
            "and may not contain spaces."
        )
    return candidate


def validate_display_name(display_name: str) -> str:
    candidate = (display_name or "").strip()
    if not candidate:
        raise UserLifecycleError("A display name is required.")
    if len(candidate) > MAX_DISPLAY_NAME_LENGTH:
        raise UserLifecycleError(
            f"A display name may be at most {MAX_DISPLAY_NAME_LENGTH} characters.")
    return candidate


def validate_role(role) -> str:
    # Role is a (str, Enum) mixin, and on Python 3.12 str(Role.OWNER) is
    # "Role.OWNER", not "OWNER" - so converting first and looking up
    # afterwards rejects the very values this system defines.
    if isinstance(role, Role):
        return role.value
    try:
        return Role(role).value
    except (ValueError, KeyError):
        raise RoleAssignmentRefused(f"{role!r} is not a role this system has.") from None


def _username_taken(connection, candidate: str, *, excluding: int | None = None) -> bool:
    # Case-insensitive, and archived accounts count. `Priya` and `priya` signing
    # in as different people is an audit trail nobody can read, and reusing a
    # retired person's username silently reattributes their history.
    row = connection.execute(
        text("SELECT id FROM hq_users WHERE LOWER(username) = LOWER(:username)"),
        {"username": candidate},
    ).first()
    return row is not None and row.id != excluding


# ---------------------------------------------------------------------------
# Creating and editing
# ---------------------------------------------------------------------------
def create_user(engine: Engine, *, username: str, display_name: str, role,
                password_hash: str) -> dict:
    username = validate_username(username)
    display_name = validate_display_name(display_name)
    role = validate_role(role)
    if not password_hash:
        raise UserLifecycleError("A password hash is required.")

    with engine.begin() as connection:
        if _username_taken(connection, username):
            raise DuplicateUsernameError(f"The username {username!r} is already in use.")
        connection.execute(text(
            "INSERT INTO hq_users (username, display_name, password_hash, role, is_active,"
            "                      lifecycle_state, session_version, created_at) "
            "VALUES (:username, :display_name, :password_hash, :role, 1, :state, 1, :created_at)"
        ), {"username": username, "display_name": display_name, "password_hash": password_hash,
            "role": role, "state": ACTIVE, "created_at": _now()})
        row = connection.execute(
            text("SELECT id FROM hq_users WHERE LOWER(username) = LOWER(:username)"),
            {"username": username}).first()
    return read_user(engine, user_id=row.id)


def update_user(engine: Engine, *, user_id: int, display_name: str | None = None,
                username: str | None = None) -> dict:
    """Change what somebody is called. Deliberately does NOT end their sessions.

    A typo in a display name must not sign somebody out mid-broadcast.
    """
    updates: dict = {}
    if display_name is not None:
        updates["display_name"] = validate_display_name(display_name)
    if username is not None:
        updates["username"] = validate_username(username)
    if not updates:
        return read_user(engine, user_id=user_id)

    with engine.begin() as connection:
        _require_row(connection, user_id)
        if "username" in updates and _username_taken(connection, updates["username"],
                                                     excluding=user_id):
            raise DuplicateUsernameError(
                f"The username {updates['username']!r} is already in use.")
        assignments = ", ".join(f"{column} = :{column}" for column in updates)
        connection.execute(text(f"UPDATE hq_users SET {assignments} WHERE id = :user_id"),
                           {**updates, "user_id": user_id})
    return read_user(engine, user_id=user_id)


# ---------------------------------------------------------------------------
# The lock-out rules
# ---------------------------------------------------------------------------
def _active_super_admin_ids(connection) -> set[int]:
    rows = connection.execute(text(
        "SELECT id FROM hq_users WHERE role = :role AND lifecycle_state = :state"
    ), {"role": Role.OWNER.value, "state": ACTIVE}).all()
    return {row.id for row in rows}


def _refuse_if_last_super_admin(connection, user_id: int, what: str) -> None:
    """A disabled or archived SUPER_ADMIN is not cover for anything.

    There is no password-reset e-mail here and no support line. An organisation
    that removes its last administrator gets back in by editing the database by
    hand, which is exactly the situation this whole module exists to avoid.
    """
    remaining = _active_super_admin_ids(connection) - {user_id}
    if not remaining and user_id in _active_super_admin_ids(connection):
        raise LastSuperAdminError(
            f"This is the only active SUPER_ADMIN. {what} would leave nobody able to "
            "administer EchoCast. Give somebody else that role first."
        )


def _refuse_self(actor_id: int | None, user_id: int, what: str) -> None:
    if actor_id is not None and int(actor_id) == int(user_id):
        raise SelfActionRefused(
            f"You cannot {what} your own account. Ask another administrator."
        )


def _bump_session_version(connection, user_id: int) -> None:
    connection.execute(
        text("UPDATE hq_users SET session_version = session_version + 1 WHERE id = :user_id"),
        {"user_id": user_id},
    )


def _transition(engine: Engine, *, user_id: int, actor_id: int | None, target: str,
                allowed_from: tuple[str, ...], is_active: bool, refuse_self_as: str | None,
                guard_last_super_admin: str | None, timestamp_column: str | None) -> dict:
    if refuse_self_as:
        _refuse_self(actor_id, user_id, refuse_self_as)
    with engine.begin() as connection:
        row = _require_row(connection, user_id)
        if row.lifecycle_state not in allowed_from:
            raise UserTransitionRefused(
                f"An account that is {row.lifecycle_state} cannot become {target}."
            )
        if guard_last_super_admin:
            _refuse_if_last_super_admin(connection, user_id, guard_last_super_admin)

        assignments = ["lifecycle_state = :state", "is_active = :is_active"]
        parameters = {"state": target, "is_active": 1 if is_active else 0, "user_id": user_id}
        if timestamp_column:
            assignments.append(f"{timestamp_column} = :stamp")
            parameters["stamp"] = _now()
        connection.execute(
            text(f"UPDATE hq_users SET {', '.join(assignments)} WHERE id = :user_id"), parameters)
        if not is_active:
            _bump_session_version(connection, user_id)
    return read_user(engine, user_id=user_id)


def disable_user(engine: Engine, *, user_id: int, actor_id: int | None = None) -> dict:
    return _transition(
        engine, user_id=user_id, actor_id=actor_id, target=DISABLED,
        allowed_from=(ACTIVE, DISABLED), is_active=False,
        refuse_self_as="disable", guard_last_super_admin="Disabling it",
        timestamp_column="disabled_at")


def enable_user(engine: Engine, *, user_id: int, actor_id: int | None = None) -> dict:
    # Not reachable from ARCHIVED on purpose: see restore_user.
    return _transition(
        engine, user_id=user_id, actor_id=actor_id, target=ACTIVE,
        allowed_from=(ACTIVE, DISABLED), is_active=True,
        refuse_self_as=None, guard_last_super_admin=None, timestamp_column=None)


def archive_user(engine: Engine, *, user_id: int, actor_id: int | None = None) -> dict:
    return _transition(
        engine, user_id=user_id, actor_id=actor_id, target=ARCHIVED,
        allowed_from=(ACTIVE, DISABLED, ARCHIVED), is_active=False,
        refuse_self_as="archive", guard_last_super_admin="Archiving it",
        timestamp_column="archived_at")


def restore_user(engine: Engine, *, user_id: int, actor_id: int | None = None) -> dict:
    """Back to DISABLED, never straight to ACTIVE.

    So somebody has to look at the account - its role, its password - before it
    can sign in again.
    """
    with engine.connect() as connection:
        row = _require_row(connection, user_id)
    if row.lifecycle_state != ARCHIVED:
        raise UserNotRestorableError("Only an archived account can be restored.")
    return _transition(
        engine, user_id=user_id, actor_id=actor_id, target=DISABLED,
        allowed_from=(ARCHIVED,), is_active=False,
        refuse_self_as=None, guard_last_super_admin=None, timestamp_column=None)


def assign_role(engine: Engine, *, user_id: int, role, actor_id: int | None = None) -> dict:
    role = validate_role(role)
    with engine.begin() as connection:
        _require_row(connection, user_id)
        if role != Role.OWNER.value:
            _refuse_if_last_super_admin(connection, user_id, "Changing its role")
        connection.execute(text("UPDATE hq_users SET role = :role WHERE id = :user_id"),
                           {"role": role, "user_id": user_id})
        # A role change is a permission change, and a token minted a moment ago
        # still carries the old one.
        _bump_session_version(connection, user_id)
    return read_user(engine, user_id=user_id)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def set_password_hash(engine: Engine, *, user_id: int, password_hash: str) -> dict:
    """Store an already-hashed password and end every existing session.

    This module never sees a plaintext password and never hashes one - that
    belongs with the code that owns the hashing algorithm. What it owns is the
    consequence: a changed password must invalidate the tokens issued under the
    old one, or "change your password, you may have been compromised" achieves
    nothing for the next eight hours.

    It deliberately does not touch ``lifecycle_state``. Resetting a password for
    somebody who has left must not quietly let them back in.
    """
    if not password_hash:
        raise UserLifecycleError("A password hash is required.")
    with engine.begin() as connection:
        _require_row(connection, user_id)
        connection.execute(
            text("UPDATE hq_users SET password_hash = :password_hash WHERE id = :user_id"),
            {"password_hash": password_hash, "user_id": user_id})
        _bump_session_version(connection, user_id)
    return read_user(engine, user_id=user_id)
