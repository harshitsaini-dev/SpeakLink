"""The Owner account, and a password minimum a human can actually type.

TWO THINGS, AND ONE OF THEM IS MOSTLY A RENAME.

The brief asked for an OWNER role that ADMIN cannot touch, with at least one
enabled OWNER always remaining. That role already existed here under the name
SUPER_ADMIN, with exactly those rules and tests behind them. Adding a *second*
top-level role beside it would have been the dangerous option: two overlapping
"you may not remove this one" protections is how one of them quietly stops
applying. So SUPER_ADMIN is renamed to OWNER, the old string is still accepted
wherever a role is read, and a migration rewrites the stored value.

The password minimum moves from 12 to 8. Note for the record: the brief asked
to remove a 16-character rule. There was never one - `git grep` for
``min_length=16``, ``minLength={16}`` and "16 characters" across all tracked
files returns nothing. The real value was 12, in four places.

Nothing here opens a socket or touches the protected database.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from rbac import (  # noqa: E402
    LEGACY_ROLE_ALIASES,
    Permission,
    Role,
    effective_permissions,
    may_manage_role,
    parse_role,
)
from user_lifecycle import (  # noqa: E402
    ACTIVE,
    LastSuperAdminError,
    archive_user,
    assign_role,
    create_user,
    disable_user,
    ensure_user_lifecycle_schema,
    list_users,
    migrate_super_admin_to_owner,
    read_user,
)
from password_policy import (  # noqa: E402
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    PasswordPolicyError,
    validate_password,
)


# ===========================================================================
# The password minimum, in one place
# ===========================================================================
def test_the_minimum_is_eight():
    assert PASSWORD_MIN_LENGTH == 8


def test_the_maximum_is_the_existing_safe_limit():
    """Unchanged at 200. bcrypt only reads the first 72 bytes anyway; the limit
    exists to stop somebody posting a megabyte, not to add strength."""
    assert PASSWORD_MAX_LENGTH == 200


def test_seven_characters_is_refused():
    with pytest.raises(PasswordPolicyError):
        validate_password("a" * 7)


def test_exactly_eight_is_accepted():
    assert validate_password("a" * 8) == "a" * 8


def test_longer_is_accepted():
    assert validate_password("a" * 40)


def test_an_empty_password_is_refused():
    with pytest.raises(PasswordPolicyError):
        validate_password("")


def test_too_long_is_refused():
    with pytest.raises(PasswordPolicyError):
        validate_password("a" * (PASSWORD_MAX_LENGTH + 1))


def test_whitespace_is_not_silently_trimmed():
    """Trimming would mean a password typed with a trailing space is stored as a
    different password from the one the person believes they chose - and they
    only find out at the sign-in box."""
    password = "  eight!!  "
    assert validate_password(password) == password


def test_a_password_of_only_spaces_still_counts_its_length():
    """Not a recommendation. Just: the rule is length, and it is applied to what
    was actually typed rather than to a cleaned-up version of it."""
    assert validate_password(" " * 8) == " " * 8


def test_the_refusal_never_echoes_the_password():
    secret = "abc"
    with pytest.raises(PasswordPolicyError) as refusal:
        validate_password(secret)
    assert secret not in str(refusal.value)
    assert "8" in str(refusal.value)


def test_no_twelve_character_rule_survives_anywhere():
    """The value that actually existed before this change, in four places."""
    import re

    offenders = []
    candidates = list(BACKEND_ROOT.rglob("*.py")) + \
        list((REPOSITORY_ROOT / "frontend" / "src").rglob("*.jsx")) + \
        list((REPOSITORY_ROOT / "frontend" / "src").rglob("*.js"))
    for path in candidates:
        # The first version of this walk included backend/.venv and reported
        # site-packages/compat.py for the phrase "12 characters". A scan that
        # includes third-party code is reporting on somebody else's project.
        if ".venv" in path.parts or "site-packages" in path.parts:
            continue
        if "test_owner_role_and_password_policy" in path.name:
            continue
        text_content = path.read_text(encoding="utf-8", errors="replace")
        for pattern in (r"min_length=12", r"minLength=\{?12", r"12 characters"):
            if re.search(pattern, text_content):
                offenders.append(f"{path.name}: {pattern}")
    assert offenders == [], f"a 12-character password rule survives: {offenders}"


# ===========================================================================
# OWNER is the top role, and SUPER_ADMIN still parses
# ===========================================================================
def test_owner_exists():
    assert Role.OWNER.value == "OWNER"


def test_there_is_exactly_one_top_role():
    """Two roles holding every permission would mean two separate "cannot be
    removed" rules, and eventually only one of them being applied."""
    top = [role for role in Role if len(rbac_permissions(role)) == len(list(Permission))]
    assert top == [Role.OWNER]


def rbac_permissions(role):
    from rbac import ROLE_PERMISSIONS

    return ROLE_PERMISSIONS[role]


def test_super_admin_is_gone_as_a_role():
    assert not hasattr(Role, "SUPER_ADMIN")


def test_the_old_string_still_parses_to_owner():
    """Rows written before this change say SUPER_ADMIN, and a token minted a
    minute before the upgrade carries it too. Refusing it would sign out every
    administrator at the moment of the upgrade."""
    assert parse_role("SUPER_ADMIN") is Role.OWNER
    assert parse_role("super_admin") is Role.OWNER
    assert "SUPER_ADMIN" in LEGACY_ROLE_ALIASES


def test_an_unknown_role_still_parses_to_nothing():
    assert parse_role("OWNER_SUPREME") is None
    assert parse_role("") is None
    assert parse_role(None) is None


# ===========================================================================
# What each role may do
# ===========================================================================
class FakeUser:
    def __init__(self, role, is_active=True):
        self.role = role
        self.is_active = is_active


def test_owner_has_everything():
    assert effective_permissions(FakeUser(Role.OWNER.value)) == frozenset(Permission)


def test_admin_has_every_operational_permission():
    """The brief: ADMIN has full operational access. The only thing it may not
    do is touch an OWNER account."""
    admin = effective_permissions(FakeUser(Role.ADMIN.value))
    for permission in (Permission.MANAGE_USERS, Permission.MANAGE_STORES,
                       Permission.MANAGE_DEVICES, Permission.START_BROADCAST,
                       Permission.STOP_BROADCAST, Permission.EMERGENCY_STOP,
                       Permission.VIEW_STATUS, Permission.VIEW_HISTORY,
                       Permission.VIEW_LOGS):
        assert permission in admin, f"ADMIN is missing {permission}"


def test_admin_still_cannot_change_how_authentication_works():
    """MANAGE_SECURITY is not on the operational list, and it is the first thing
    somebody who compromised an ADMIN account would reach for."""
    assert Permission.MANAGE_SECURITY not in effective_permissions(FakeUser(Role.ADMIN.value))


def test_a_disabled_owner_has_no_permissions():
    assert effective_permissions(FakeUser(Role.OWNER.value, is_active=False)) == frozenset()


def test_only_an_owner_may_manage_an_owner():
    assert may_manage_role(Role.OWNER, Role.OWNER)
    assert not may_manage_role(Role.ADMIN, Role.OWNER)
    assert not may_manage_role(Role.BROADCASTER, Role.OWNER)
    assert not may_manage_role(Role.VIEWER, Role.OWNER)


def test_admin_may_still_manage_the_people_it_runs():
    assert may_manage_role(Role.ADMIN, Role.BROADCASTER)
    assert may_manage_role(Role.ADMIN, Role.VIEWER)


# ===========================================================================
# The lockout protection
# ===========================================================================
@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'users.db'}", future=True)
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE hq_users ("
            " id INTEGER PRIMARY KEY,"
            " username VARCHAR(100) NOT NULL UNIQUE,"
            " password_hash VARCHAR(255) NOT NULL,"
            " role VARCHAR(50) NOT NULL DEFAULT 'admin',"
            " is_active BOOLEAN NOT NULL DEFAULT 1,"
            " session_version INTEGER NOT NULL DEFAULT 1,"
            " created_at DATETIME)"
        )
    ensure_user_lifecycle_schema(made)
    return made


def make(engine, username, role=Role.ADMIN):
    return create_user(engine, username=username, display_name=username.title(),
                       role=role, password_hash="$2b$12$notarealhash")


def test_the_last_owner_cannot_be_disabled(engine):
    owner = make(engine, "owneradmin", role=Role.OWNER)
    with pytest.raises(LastSuperAdminError):
        disable_user(engine, user_id=owner["id"], actor_id=999)


def test_the_last_owner_cannot_be_archived(engine):
    owner = make(engine, "owneradmin", role=Role.OWNER)
    with pytest.raises(LastSuperAdminError):
        archive_user(engine, user_id=owner["id"], actor_id=999)


def test_the_last_owner_cannot_be_downgraded(engine):
    owner = make(engine, "owneradmin", role=Role.OWNER)
    with pytest.raises(LastSuperAdminError):
        assign_role(engine, user_id=owner["id"], role=Role.ADMIN, actor_id=999)


def test_a_second_owner_makes_the_first_removable(engine):
    first = make(engine, "owneradmin", role=Role.OWNER)
    second = make(engine, "owner-two", role=Role.OWNER)
    assert disable_user(engine, user_id=first["id"], actor_id=second["id"])


def test_a_disabled_owner_is_not_cover_for_the_last_one(engine):
    first = make(engine, "owneradmin", role=Role.OWNER)
    second = make(engine, "owner-two", role=Role.OWNER)
    disable_user(engine, user_id=second["id"], actor_id=first["id"])
    with pytest.raises(LastSuperAdminError):
        disable_user(engine, user_id=first["id"], actor_id=second["id"])


# ===========================================================================
# The migration
# ===========================================================================
def test_stored_super_admin_rows_become_owner(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active, lifecycle_state)"
            " VALUES ('legacy-top', 'h', 'SUPER_ADMIN', 1, 'active')")
    changed = migrate_super_admin_to_owner(engine)
    assert changed == 1
    assert read_user(engine, user_id=1)["role"] == Role.OWNER.value


def test_the_migration_is_idempotent(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active, lifecycle_state)"
            " VALUES ('legacy-top', 'h', 'SUPER_ADMIN', 1, 'active')")
    assert migrate_super_admin_to_owner(engine) == 1
    assert migrate_super_admin_to_owner(engine) == 0


def test_the_migration_preserves_every_other_user(engine):
    make(engine, "admin", role=Role.ADMIN)
    make(engine, "priya", role=Role.VIEWER)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active, lifecycle_state)"
            " VALUES ('legacy-top', 'h', 'SUPER_ADMIN', 1, 'active')")
    before = {record["username"] for record in list_users(engine)}
    migrate_super_admin_to_owner(engine)
    after = {record["username"]: record["role"] for record in list_users(engine)}
    assert set(after) == before
    assert after["admin"] == Role.ADMIN.value, "the existing administrator must stay ADMIN"
    assert after["priya"] == Role.VIEWER.value


def test_the_migration_creates_nobody(engine):
    make(engine, "admin", role=Role.ADMIN)
    migrate_super_admin_to_owner(engine)
    assert {record["username"] for record in list_users(engine)} == {"admin"}


def test_the_migration_touches_no_password_hash(engine):
    make(engine, "admin", role=Role.ADMIN)
    with engine.connect() as connection:
        before = connection.execute(text("SELECT password_hash FROM hq_users")).scalar()
    migrate_super_admin_to_owner(engine)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT password_hash FROM hq_users")).scalar() == before


def test_no_column_stores_a_plaintext_password(engine):
    with engine.connect() as connection:
        columns = {row[1].lower() for row in
                   connection.exec_driver_sql("PRAGMA table_info(hq_users)")}
    assert "password" not in columns
    assert "plaintext" not in columns
    assert "password_hash" in columns
