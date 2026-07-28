"""A User's life: active, switched off, retired - and never actually deleted.

An HQ User is the author of broadcast history. Their id and username appear in
log lines saying who regenerated a Store token, who revoked a Device, who sent
an announcement to 44 Stores at nine on a Friday night. Deleting the row either
orphans that history or cascades it away, and both destroy the only record of
who did what. So "delete" means **archive**: the row stays, the history stays
readable, and the account simply stops being one you can sign in as.

The rules that carry real weight are the ones that stop an organisation locking
itself out, and the ones that make a revoked session actually stop working.

Nothing here opens a socket, starts a server, or touches the protected
database.
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

from rbac import Role  # noqa: E402
from user_lifecycle import (  # noqa: E402
    ACTIVE,
    ARCHIVED,
    DISABLED,
    DuplicateUsernameError,
    LastSuperAdminError,
    SelfActionRefused,
    UserLifecycleError,
    UserNotFoundError,
    UserNotRestorableError,
    UserTransitionRefused,
    RoleAssignmentRefused,
    archive_user,
    assign_role,
    create_user,
    disable_user,
    enable_user,
    ensure_user_lifecycle_schema,
    list_users,
    read_user,
    restore_user,
    set_password_hash,
    update_user,
)


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


def make(engine, username, role=Role.ADMIN, display_name=None):
    return create_user(
        engine,
        username=username,
        display_name=display_name or username.title(),
        role=role,
        password_hash="$2b$12$notarealhashbutlongenoughtolooklikeone.....",
    )


@pytest.fixture()
def founder(engine):
    """One SUPER_ADMIN, as a fresh install has."""
    return make(engine, "founder", role=Role.SUPER_ADMIN)


# ===========================================================================
# Creating
# ===========================================================================
def test_a_new_user_starts_active(engine):
    created = make(engine, "priya")
    assert created["lifecycle_state"] == ACTIVE
    assert created["is_active"] is True


def test_a_new_user_gets_a_session_version(engine):
    """Read from the row, not from the response.

    An earlier version had create_user return session_version so this could
    assert on it - which meant the one call that published a revocation counter
    was the one that created the account. The counter is checked where it
    lives.
    """
    created = make(engine, "priya")
    assert _session_version(engine, created["id"]) >= 1


def test_a_duplicate_username_is_refused(engine):
    make(engine, "priya")
    with pytest.raises(DuplicateUsernameError):
        make(engine, "priya")


def test_a_duplicate_username_is_refused_regardless_of_case(engine):
    """`Priya` and `priya` signing in as different people is a support call
    waiting to happen, and worse, an audit trail nobody can read."""
    make(engine, "priya")
    with pytest.raises(DuplicateUsernameError):
        make(engine, "PRIYA")


def test_an_archived_user_still_holds_their_username(engine):
    """Reusing a retired person's username silently reattributes their history."""
    created = make(engine, "priya")
    archive_user(engine, user_id=created["id"], actor_id=999)
    with pytest.raises(DuplicateUsernameError):
        make(engine, "priya")


def test_an_unknown_role_is_refused(engine):
    with pytest.raises(RoleAssignmentRefused):
        create_user(engine, username="x", display_name="X", role="WIZARD",
                    password_hash="hash")


@pytest.mark.parametrize("bad", ["", "   ", "a b", "x" * 200])
def test_an_unusable_username_is_refused(engine, bad):
    with pytest.raises(UserLifecycleError):
        create_user(engine, username=bad, display_name="X", role=Role.ADMIN,
                    password_hash="hash")


# ===========================================================================
# Reading
# ===========================================================================
def test_a_user_record_never_carries_the_password_hash(engine):
    """The single most important property in this module.

    A hash in a response body is a hash in a browser's memory, in a log, in a
    screenshot pasted into a chat. It is not a password, but it is the input to
    an offline attack that no rate limit protects against.
    """
    created = make(engine, "priya")
    for record in (created, read_user(engine, user_id=created["id"]), *list_users(engine)):
        assert "password_hash" not in record


def test_a_user_record_never_carries_the_session_version(engine):
    """It is a revocation counter. Publishing it tells an attacker exactly how
    many times they need to be wrong."""
    created = make(engine, "priya")
    for record in (created, read_user(engine, user_id=created["id"]), *list_users(engine)):
        assert "session_version" not in record


def test_reading_an_unknown_user_raises(engine):
    with pytest.raises(UserNotFoundError):
        read_user(engine, user_id=4242)


def test_listing_includes_every_state(engine, founder):
    disabled = make(engine, "disabled-one")
    archived = make(engine, "archived-one")
    disable_user(engine, user_id=disabled["id"], actor_id=founder["id"])
    archive_user(engine, user_id=archived["id"], actor_id=founder["id"])
    states = {record["username"]: record["lifecycle_state"] for record in list_users(engine)}
    assert states == {"founder": ACTIVE, "disabled-one": DISABLED, "archived-one": ARCHIVED}


# ===========================================================================
# Editing
# ===========================================================================
def test_display_name_and_username_can_be_changed(engine):
    created = make(engine, "priya")
    updated = update_user(engine, user_id=created["id"],
                          display_name="Priya Sharma", username="priya.sharma")
    assert updated["display_name"] == "Priya Sharma"
    assert updated["username"] == "priya.sharma"


def test_renaming_onto_another_username_is_refused(engine):
    first = make(engine, "priya")
    make(engine, "rahul")
    with pytest.raises(DuplicateUsernameError):
        update_user(engine, user_id=first["id"], username="rahul")


def test_editing_does_not_end_existing_sessions(engine):
    """A typo in somebody's display name must not sign them out mid-broadcast."""
    created = make(engine, "priya")
    before = _session_version(engine, created["id"])
    update_user(engine, user_id=created["id"], display_name="Priya S")
    assert _session_version(engine, created["id"]) == before


# ===========================================================================
# Lifecycle
# ===========================================================================
def test_disable_then_enable(engine, founder):
    created = make(engine, "priya")
    assert disable_user(engine, user_id=created["id"], actor_id=founder["id"])["lifecycle_state"] == DISABLED
    assert enable_user(engine, user_id=created["id"], actor_id=founder["id"])["lifecycle_state"] == ACTIVE


def test_is_active_stays_in_lockstep(engine, founder):
    """Login already filters on ``is_active``. Keeping it correct means login did
    not have to learn about archiving - and, more usefully, cannot miss it."""
    created = make(engine, "priya")
    for act, expected in ((disable_user, False), (enable_user, True), (archive_user, False)):
        record = act(engine, user_id=created["id"], actor_id=founder["id"])
        assert record["is_active"] is expected
        assert _is_active(engine, created["id"]) is expected


def test_archive_is_not_undone_by_enable(engine, founder):
    """Enable is a small button. Un-retiring a person is not a small decision."""
    created = make(engine, "priya")
    archive_user(engine, user_id=created["id"], actor_id=founder["id"])
    with pytest.raises(UserTransitionRefused):
        enable_user(engine, user_id=created["id"], actor_id=founder["id"])


def test_restore_returns_to_disabled_not_active(engine, founder):
    """So somebody has to look at the account, and give it a password, before it
    can sign in again."""
    created = make(engine, "priya")
    archive_user(engine, user_id=created["id"], actor_id=founder["id"])
    restored = restore_user(engine, user_id=created["id"], actor_id=founder["id"])
    assert restored["lifecycle_state"] == DISABLED
    assert restored["is_active"] is False


def test_only_an_archived_user_can_be_restored(engine, founder):
    created = make(engine, "priya")
    with pytest.raises(UserNotRestorableError):
        restore_user(engine, user_id=created["id"], actor_id=founder["id"])


def test_no_lifecycle_action_deletes_the_row(engine, founder):
    created = make(engine, "priya")
    archive_user(engine, user_id=created["id"], actor_id=founder["id"])
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM hq_users WHERE id = :i"), {"i": created["id"]}
        ).scalar() == 1


# ===========================================================================
# Locking yourself out: the rules that matter most
# ===========================================================================
def test_the_last_active_super_admin_cannot_be_disabled(engine, founder):
    with pytest.raises(LastSuperAdminError):
        disable_user(engine, user_id=founder["id"], actor_id=999)


def test_the_last_active_super_admin_cannot_be_archived(engine, founder):
    with pytest.raises(LastSuperAdminError):
        archive_user(engine, user_id=founder["id"], actor_id=999)


def test_the_last_active_super_admin_cannot_be_demoted(engine, founder):
    with pytest.raises(LastSuperAdminError):
        assign_role(engine, user_id=founder["id"], role=Role.ADMIN, actor_id=999)


def test_a_second_super_admin_makes_the_first_removable(engine, founder):
    second = make(engine, "second", role=Role.SUPER_ADMIN)
    assert disable_user(engine, user_id=founder["id"], actor_id=second["id"])


def test_a_disabled_super_admin_does_not_count_as_cover(engine, founder):
    """Two SUPER_ADMINs, one switched off, is one SUPER_ADMIN."""
    second = make(engine, "second", role=Role.SUPER_ADMIN)
    disable_user(engine, user_id=second["id"], actor_id=founder["id"])
    with pytest.raises(LastSuperAdminError):
        disable_user(engine, user_id=founder["id"], actor_id=second["id"])


def test_an_archived_super_admin_does_not_count_as_cover(engine, founder):
    second = make(engine, "second", role=Role.SUPER_ADMIN)
    archive_user(engine, user_id=second["id"], actor_id=founder["id"])
    with pytest.raises(LastSuperAdminError):
        archive_user(engine, user_id=founder["id"], actor_id=second["id"])


def test_you_cannot_disable_yourself(engine, founder):
    """Not a safety net for the organisation - a safety net for the person
    clicking, who is one row away from being unable to undo it."""
    second = make(engine, "second", role=Role.SUPER_ADMIN)
    with pytest.raises(SelfActionRefused):
        disable_user(engine, user_id=second["id"], actor_id=second["id"])


def test_you_cannot_archive_yourself(engine, founder):
    second = make(engine, "second", role=Role.SUPER_ADMIN)
    with pytest.raises(SelfActionRefused):
        archive_user(engine, user_id=second["id"], actor_id=second["id"])


def test_you_can_still_edit_your_own_display_name(engine, founder):
    """The self-action rules are about lock-out, not about a name."""
    assert update_user(engine, user_id=founder["id"], display_name="The Founder")


# ===========================================================================
# Roles
# ===========================================================================
def test_a_role_can_be_assigned(engine, founder):
    created = make(engine, "priya", role=Role.VIEWER)
    assert assign_role(engine, user_id=created["id"], role=Role.BROADCASTER,
                       actor_id=founder["id"])["role"] == Role.BROADCASTER


def test_an_unknown_role_cannot_be_assigned(engine, founder):
    created = make(engine, "priya")
    with pytest.raises(RoleAssignmentRefused):
        assign_role(engine, user_id=created["id"], role="OWNER", actor_id=founder["id"])


# ===========================================================================
# Every session-ending action must actually end sessions
# ===========================================================================
def _session_version(engine, user_id: int) -> int:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT session_version FROM hq_users WHERE id = :i"), {"i": user_id}
        ).scalar()


def _is_active(engine, user_id: int) -> bool:
    with engine.connect() as connection:
        return bool(connection.execute(
            text("SELECT is_active FROM hq_users WHERE id = :i"), {"i": user_id}
        ).scalar())


@pytest.mark.parametrize("action", ["disable", "archive", "role", "password"])
def test_the_session_version_is_bumped(engine, founder, action):
    """The whole revocation mechanism.

    A JWT is valid for eight hours and carries no state. Without this, disabling
    an account at 09:05 leaves whoever holds its token broadcasting to 44 Stores
    until 17:00. Every one of these must invalidate immediately.
    """
    created = make(engine, "priya")
    before = _session_version(engine, created["id"])
    {
        "disable": lambda: disable_user(engine, user_id=created["id"], actor_id=founder["id"]),
        "archive": lambda: archive_user(engine, user_id=created["id"], actor_id=founder["id"]),
        "role": lambda: assign_role(engine, user_id=created["id"], role=Role.VIEWER,
                                    actor_id=founder["id"]),
        "password": lambda: set_password_hash(engine, user_id=created["id"], password_hash="$2b$12$new"),
    }[action]()
    assert _session_version(engine, created["id"]) > before


def test_setting_a_password_does_not_reactivate_a_disabled_user(engine, founder):
    """Resetting a password for somebody who has left must not let them back in."""
    created = make(engine, "priya")
    disable_user(engine, user_id=created["id"], actor_id=founder["id"])
    set_password_hash(engine, user_id=created["id"], password_hash="$2b$12$new")
    assert read_user(engine, user_id=created["id"])["lifecycle_state"] == DISABLED
    assert _is_active(engine, created["id"]) is False


def test_a_password_hash_is_never_returned_by_setting_one(engine):
    created = make(engine, "priya")
    result = set_password_hash(engine, user_id=created["id"], password_hash="$2b$12$new")
    assert "password_hash" not in result


def test_setting_a_password_for_an_unknown_user_raises(engine):
    with pytest.raises(UserNotFoundError):
        set_password_hash(engine, user_id=4242, password_hash="$2b$12$new")


# ===========================================================================
# Schema migration
# ===========================================================================
def test_the_migration_runs_twice_without_complaint(engine):
    ensure_user_lifecycle_schema(engine)
    ensure_user_lifecycle_schema(engine)


def test_an_existing_active_row_is_backfilled_as_active(tmp_path):
    """An upgrade must not decide everybody is suddenly archived, and must not
    decide somebody switched off is active again."""
    made = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE hq_users ("
            " id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,"
            " password_hash VARCHAR(255) NOT NULL, role VARCHAR(50) NOT NULL DEFAULT 'admin',"
            " is_active BOOLEAN NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,"
            " created_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active) VALUES"
            " ('kept', 'h', 'super_admin', 1), ('switched-off', 'h', 'admin', 0)"
        )
    ensure_user_lifecycle_schema(made)
    states = {record["username"]: record["lifecycle_state"] for record in list_users(made)}
    assert states == {"kept": ACTIVE, "switched-off": DISABLED}


def test_the_migration_gives_everyone_a_display_name(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'old2.db'}", future=True)
    with made.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE hq_users ("
            " id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,"
            " password_hash VARCHAR(255) NOT NULL, role VARCHAR(50) NOT NULL DEFAULT 'admin',"
            " is_active BOOLEAN NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1,"
            " created_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash) VALUES ('nameless', 'h')"
        )
    ensure_user_lifecycle_schema(made)
    assert list_users(made)[0]["display_name"] == "nameless"
