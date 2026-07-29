"""Who may do what, decided in the backend and nowhere else.

Every account in this system is an administrator today: ``hq_users.role``
defaults to ``"admin"`` and no endpoint reads it. The person who starts
broadcasts can rotate Receiver credentials; the person who only watches Store
status can archive a Store. That is not a policy, it is the absence of one.

Four roles, one fixed matrix, and the matrix lives in one place so it can be
read and argued with rather than reconstructed from twenty decorators.

The rule that shapes everything here: **hiding a button is not a permission
check**. Half of these tests do the thing a hidden button would have done, by
calling the function directly with a role that should not be allowed to.

Two protections exist because losing them is unrecoverable:

* the last active SUPER_ADMIN cannot be disabled, archived or demoted - if it
  could, an HQ could lock itself out of its own security settings with one
  click and no way back short of database surgery;
* nobody can disable or archive the account they are signed in as, which is the
  same accident wearing a friendlier face.

Every test uses a temporary database. ``backend/echocast_live.db`` is never
opened.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from auth import hash_password  # noqa: E402
from db import Base  # noqa: E402
from models import HQUser, Store  # noqa: E402

from rbac import (  # noqa: E402
    ALL_PERMISSIONS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    PermissionDenied,
    ensure_rbac_schema,
    effective_permissions,
    has_permission,
    may_manage_role,
    migrate_legacy_roles,
    require_permission,
)


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"


class Runtime:
    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.path = tmp_path / "rbac.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        ensure_rbac_schema(self.engine)

    def add(self, username: str, role: str, *, active: bool = True) -> int:
        with self.Session() as db:
            user = HQUser(username=username, password_hash=hash_password("x"),
                          role=role, is_active=active)
            db.add(user)
            db.commit()
            return user.id

    def role_of(self, username: str) -> str:
        with self.Session() as db:
            return db.query(HQUser).filter(HQUser.username == username).one().role

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()


@pytest.fixture()
def runtime(tmp_path) -> Runtime:
    made = Runtime(tmp_path)
    yield made
    made.engine.dispose()


# ===========================================================================
# The matrix itself
# ===========================================================================
def test_the_four_roles_are_the_only_roles():
    assert {role.value for role in Role} == {
        "OWNER", "ADMIN", "BROADCASTER", "VIEWER"
    }


def test_every_role_has_an_entry_and_every_entry_is_a_known_permission():
    """A role missing from the matrix would silently get nothing, which reads
    as "broken" rather than "denied" and sends somebody looking in the wrong
    place."""
    assert set(ROLE_PERMISSIONS) == set(Role)
    for role, granted in ROLE_PERMISSIONS.items():
        assert granted <= ALL_PERMISSIONS, f"{role} grants something undefined"


def test_super_admin_has_every_permission():
    assert ROLE_PERMISSIONS[Role.OWNER] == ALL_PERMISSIONS


def test_admin_can_run_the_estate_but_not_change_security():
    granted = ROLE_PERMISSIONS[Role.ADMIN]
    for permission in (Permission.MANAGE_STORES, Permission.MANAGE_DEVICES,
                       Permission.MANAGE_USERS, Permission.START_BROADCAST,
                       Permission.STOP_BROADCAST, Permission.EMERGENCY_STOP,
                       Permission.VIEW_STATUS, Permission.VIEW_HISTORY,
                       Permission.VIEW_LOGS):
        assert permission in granted
    assert Permission.MANAGE_SECURITY not in granted


def test_a_broadcaster_can_broadcast_and_nothing_administrative():
    granted = ROLE_PERMISSIONS[Role.BROADCASTER]
    for permission in (Permission.START_BROADCAST, Permission.STOP_BROADCAST,
                       Permission.EMERGENCY_STOP, Permission.VIEW_STATUS,
                       Permission.VIEW_HISTORY):
        assert permission in granted
    for permission in (Permission.MANAGE_STORES, Permission.MANAGE_USERS,
                       Permission.MANAGE_DEVICES, Permission.MANAGE_SECURITY):
        assert permission not in granted


def test_a_viewer_can_only_look():
    granted = ROLE_PERMISSIONS[Role.VIEWER]
    assert granted == {Permission.VIEW_STATUS, Permission.VIEW_HISTORY}
    for permission in (Permission.START_BROADCAST, Permission.STOP_BROADCAST,
                       Permission.EMERGENCY_STOP):
        assert permission not in granted


def test_emergency_stop_is_not_a_viewer_permission():
    """It stops every Store mid-announcement. Read-only means read-only."""
    assert Permission.EMERGENCY_STOP not in ROLE_PERMISSIONS[Role.VIEWER]


def test_effective_permissions_are_reported_for_a_user(runtime: Runtime):
    runtime.add("bc", Role.BROADCASTER.value)
    with runtime.Session() as db:
        user = db.query(HQUser).filter(HQUser.username == "bc").one()
        assert effective_permissions(user) == ROLE_PERMISSIONS[Role.BROADCASTER]


def test_an_unknown_role_grants_nothing(runtime: Runtime):
    """Fail closed. A typo in a role string must not become a wildcard."""
    runtime.add("odd", "WIZARD")
    with runtime.Session() as db:
        user = db.query(HQUser).filter(HQUser.username == "odd").one()
        assert effective_permissions(user) == set()
        assert not has_permission(user, Permission.VIEW_STATUS)


def test_an_inactive_user_has_no_permissions(runtime: Runtime):
    runtime.add("gone", Role.OWNER.value, active=False)
    with runtime.Session() as db:
        user = db.query(HQUser).filter(HQUser.username == "gone").one()
        assert effective_permissions(user) == set()


# ===========================================================================
# The guard, exercised the way a hidden button would be bypassed
# ===========================================================================
def test_require_permission_refuses_the_role_that_lacks_it(runtime: Runtime):
    runtime.add("viewer", Role.VIEWER.value)
    with runtime.Session() as db:
        user = db.query(HQUser).filter(HQUser.username == "viewer").one()
        with pytest.raises(PermissionDenied):
            require_permission(user, Permission.MANAGE_STORES)
        require_permission(user, Permission.VIEW_STATUS)


def test_the_refusal_says_nothing_about_what_exists(runtime: Runtime):
    """An error naming the permission tells a caller the shape of the system."""
    runtime.add("viewer", Role.VIEWER.value)
    with runtime.Session() as db:
        user = db.query(HQUser).filter(HQUser.username == "viewer").one()
        try:
            require_permission(user, Permission.MANAGE_SECURITY)
        except PermissionDenied as refusal:
            assert "MANAGE_SECURITY" not in str(refusal)
            assert "viewer" not in str(refusal).lower()


# ===========================================================================
# Who may manage whom
# ===========================================================================
def test_an_admin_cannot_touch_a_super_admin():
    assert not may_manage_role(Role.ADMIN, Role.OWNER)


def test_an_admin_cannot_promote_anyone_to_super_admin():
    """The escalation that makes every other restriction pointless."""
    assert not may_manage_role(Role.ADMIN, Role.OWNER)


def test_an_admin_may_manage_broadcasters_and_viewers():
    assert may_manage_role(Role.ADMIN, Role.BROADCASTER)
    assert may_manage_role(Role.ADMIN, Role.VIEWER)


def test_an_admin_cannot_manage_another_admin():
    """Two administrators disabling each other is a support call nobody wins."""
    assert not may_manage_role(Role.ADMIN, Role.ADMIN)


def test_a_super_admin_may_manage_every_role():
    for role in Role:
        assert may_manage_role(Role.OWNER, role)


def test_a_broadcaster_and_viewer_may_manage_nobody():
    for actor in (Role.BROADCASTER, Role.VIEWER):
        for target in Role:
            assert not may_manage_role(actor, target)


# ===========================================================================
# Migration from the legacy single role
# ===========================================================================
def test_legacy_admins_become_admin_and_exactly_one_becomes_super_admin(runtime: Runtime):
    """The deterministic rule, written down because an upgrade only runs once.

    Everyone keeps administrative access, so nobody is locked out by the
    upgrade. The lowest-id active account - the bootstrap administrator -
    becomes SUPER_ADMIN, because a system with none cannot change its own
    security settings ever again. Nobody new is created.
    """
    first = runtime.add("alice", "admin")
    runtime.add("bob", "admin")
    runtime.add("carol", "admin")

    result = migrate_legacy_roles(runtime.Session)

    assert runtime.role_of("alice") == Role.OWNER.value
    assert runtime.role_of("bob") == Role.ADMIN.value
    assert runtime.role_of("carol") == Role.ADMIN.value
    assert result["super_admin_user_id"] == first
    assert result["created_users"] == 0


def test_the_migration_creates_nobody(runtime: Runtime):
    runtime.add("alice", "admin")
    before = runtime.query("SELECT COUNT(*) FROM hq_users")
    migrate_legacy_roles(runtime.Session)
    assert runtime.query("SELECT COUNT(*) FROM hq_users") == before


def test_the_migration_changes_no_password_hash(runtime: Runtime):
    runtime.add("alice", "admin")
    before = runtime.query("SELECT username, password_hash FROM hq_users ORDER BY id")
    migrate_legacy_roles(runtime.Session)
    assert runtime.query("SELECT username, password_hash FROM hq_users ORDER BY id") == before


def test_the_migration_is_idempotent(runtime: Runtime):
    runtime.add("alice", "admin")
    runtime.add("bob", "admin")
    migrate_legacy_roles(runtime.Session)
    first = runtime.query("SELECT username, role FROM hq_users ORDER BY id")
    migrate_legacy_roles(runtime.Session)
    assert runtime.query("SELECT username, role FROM hq_users ORDER BY id") == first


def test_an_inactive_legacy_admin_is_not_chosen_as_super_admin(runtime: Runtime):
    """Promoting a disabled account would produce a system whose only
    SUPER_ADMIN cannot log in."""
    runtime.add("retired", "admin", active=False)
    active = runtime.add("alice", "admin")
    result = migrate_legacy_roles(runtime.Session)
    assert result["super_admin_user_id"] == active
    assert runtime.role_of("retired") == Role.ADMIN.value


def test_a_database_that_already_has_a_super_admin_gains_no_second_one(runtime: Runtime):
    runtime.add("root", Role.OWNER.value)
    runtime.add("alice", "admin")
    migrate_legacy_roles(runtime.Session)
    supers = runtime.query(
        "SELECT COUNT(*) FROM hq_users WHERE role = ?", (Role.OWNER.value,)
    )
    assert supers == [(1,)]
    assert runtime.role_of("alice") == Role.ADMIN.value


def test_migrating_an_empty_database_is_harmless(runtime: Runtime):
    result = migrate_legacy_roles(runtime.Session)
    assert result["super_admin_user_id"] is None


# ===========================================================================
# Schema
# ===========================================================================
def test_the_schema_change_is_additive_and_idempotent(runtime: Runtime):
    ensure_rbac_schema(runtime.engine)
    ensure_rbac_schema(runtime.engine)
    columns = {row[1] for row in runtime.query("PRAGMA table_info(hq_users)")}
    assert "session_version" in columns


def test_existing_users_start_at_session_version_one(runtime: Runtime):
    runtime.add("alice", "admin")
    versions = runtime.query("SELECT session_version FROM hq_users")
    assert versions == [(1,)]


def test_the_protected_database_is_never_opened(runtime: Runtime):
    before = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    runtime.add("alice", "admin")
    migrate_legacy_roles(runtime.Session)
    after = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    assert before == after
