"""Creating the Owner on a database that has never been migrated.

WHAT HAPPENED, AND WHY THE EXISTING TESTS DID NOT CATCH IT

Against the real HQ database the command failed with:

    sqlite3.OperationalError: table hq_users has no column named session_version

``create_user`` inserts eight columns. A legacy ``hq_users`` has six. The
command called ``ensure_user_lifecycle_schema``, which adds four of the missing
ones - and never called ``ensure_rbac_schema``, which is the one that adds
``session_version``. Two migrations, one call site, and the second was simply
forgotten.

**The test suite could not have found this**, because its fixture created
``hq_users`` with ``session_version`` already in it. It modelled a database that
had been migrated, so it tested the only case that already worked. That is the
more useful lesson here than the missing call: a fixture that describes the
world you wish you had tests nothing about the world you have.

This file uses the exact legacy schema, taken from the live database.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from auth import verify_password  # noqa: E402
from rbac import Role  # noqa: E402
from user_schema import REQUIRED_USER_COLUMNS, ensure_user_auth_schema  # noqa: E402

from tools.create_owner import OwnerBootstrapError, create_owner, main  # noqa: E402


#: Exactly what the live database had before any of this ran. Six columns.
LEGACY_SCHEMA = """
CREATE TABLE hq_users (
    id INTEGER PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'admin',
    is_active BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME
)
"""

#: A real-shaped bcrypt hash, of nothing in particular. Test-only.
EXISTING_ADMIN_HASH = "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOPQ"

PROBE = "a-temporary-probe-password"
OTHER = "a-different-probe-password"


def prompts(*answers):
    remaining = list(answers)

    def prompt(_message=""):
        return remaining.pop(0)

    return prompt


@pytest.fixture()
def legacy_engine(tmp_path):
    """A database with the six-column table and one administrator, as live."""
    made = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    with made.begin() as connection:
        connection.exec_driver_sql(LEGACY_SCHEMA)
        connection.execute(
            text("INSERT INTO hq_users (username, password_hash, role, is_active)"
                 " VALUES (:u, :h, :r, 1)"),
            {"u": "admin", "h": EXISTING_ADMIN_HASH, "r": "admin"},
        )
    return made


def columns_of(engine) -> set:
    with engine.connect() as connection:
        return {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(hq_users)")}


# ===========================================================================
# The schema the current code actually needs
# ===========================================================================
def test_the_legacy_table_really_is_missing_what_we_think(legacy_engine):
    """Pins the premise. If the live schema were different, everything below
    would be testing a database nobody has."""
    present = columns_of(legacy_engine)
    assert present == {"id", "username", "password_hash", "role", "is_active", "created_at"}
    assert "session_version" not in present
    assert "lifecycle_state" not in present
    assert "display_name" not in present


def test_the_required_columns_are_named_rather_than_remembered():
    """The list exists so a future column cannot be added to create_user without
    somebody also deciding which migration adds it."""
    assert "session_version" in REQUIRED_USER_COLUMNS
    assert "lifecycle_state" in REQUIRED_USER_COLUMNS
    assert "display_name" in REQUIRED_USER_COLUMNS


def test_one_call_prepares_everything(legacy_engine):
    ensure_user_auth_schema(legacy_engine)
    assert REQUIRED_USER_COLUMNS <= columns_of(legacy_engine)


def test_it_is_idempotent(legacy_engine):
    ensure_user_auth_schema(legacy_engine)
    ensure_user_auth_schema(legacy_engine)
    assert REQUIRED_USER_COLUMNS <= columns_of(legacy_engine)


def test_it_is_safe_on_an_already_migrated_database(legacy_engine):
    ensure_user_auth_schema(legacy_engine)
    with legacy_engine.connect() as connection:
        before = connection.execute(text("SELECT COUNT(*) FROM hq_users")).scalar()
    ensure_user_auth_schema(legacy_engine)
    with legacy_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM hq_users")).scalar() == before


def test_it_preserves_every_password_hash(legacy_engine):
    ensure_user_auth_schema(legacy_engine)
    with legacy_engine.connect() as connection:
        assert connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'admin'")
        ).scalar() == EXISTING_ADMIN_HASH


def test_the_legacy_admin_role_is_normalised(legacy_engine):
    """The row said lowercase 'admin'; the role model needs a known value."""
    ensure_user_auth_schema(legacy_engine, promote_missing_owner=False)
    with legacy_engine.connect() as connection:
        role = connection.execute(
            text("SELECT role FROM hq_users WHERE username = 'admin'")).scalar()
    assert role == Role.ADMIN.value


def test_startup_promotes_a_lone_administrator_but_the_command_does_not(legacy_engine):
    """Two callers, two correct answers.

    At startup a database with no OWNER must gain one, or nobody can ever change
    its security settings again. In the owner-bootstrap command the same
    promotion would silently change the existing administrator - the one account
    the operator was promised would not be touched - moments before creating the
    OWNER it was asked for.
    """
    ensure_user_auth_schema(legacy_engine, promote_missing_owner=False)
    with legacy_engine.connect() as connection:
        assert connection.execute(
            text("SELECT role FROM hq_users WHERE username = 'admin'")).scalar() == Role.ADMIN.value

    ensure_user_auth_schema(legacy_engine, promote_missing_owner=True)
    with legacy_engine.connect() as connection:
        assert connection.execute(
            text("SELECT role FROM hq_users WHERE username = 'admin'")).scalar() == Role.OWNER.value


def test_the_command_leaves_the_administrator_as_admin(legacy_engine):
    """End to end, through the command that actually runs on the live database."""
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with legacy_engine.connect() as connection:
        rows = {row[0]: row[1] for row in
                connection.execute(text("SELECT username, role FROM hq_users"))}
    assert rows == {"admin": Role.ADMIN.value, "owneradmin": Role.OWNER.value}


def test_it_creates_nobody(legacy_engine):
    ensure_user_auth_schema(legacy_engine)
    with legacy_engine.connect() as connection:
        names = {row[0] for row in connection.execute(text("SELECT username FROM hq_users"))}
    assert names == {"admin"}


# ===========================================================================
# The command, against the legacy database
# ===========================================================================
def test_the_owner_is_created_on_a_legacy_database(legacy_engine):
    """The regression. Before the fix this raised OperationalError: table
    hq_users has no column named session_version."""
    created = create_owner(legacy_engine, username="owneradmin",
                           prompt=prompts(PROBE, PROBE))
    assert created["username"] == "owneradmin"
    assert created["role"] == Role.OWNER.value


def test_the_existing_administrator_survives_the_migration(legacy_engine):
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with legacy_engine.connect() as connection:
        row = connection.execute(
            text("SELECT password_hash, is_active FROM hq_users WHERE username = 'admin'")
        ).first()
    assert row.password_hash == EXISTING_ADMIN_HASH, "the administrator's password changed"
    assert row.is_active


def test_the_new_owner_password_is_hashed(legacy_engine):
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with legacy_engine.connect() as connection:
        stored = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")
        ).scalar()
    assert stored != PROBE
    assert stored.startswith("$2b$")
    assert verify_password(PROBE, stored)


def test_no_plaintext_password_is_anywhere_in_the_table(legacy_engine):
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with legacy_engine.connect() as connection:
        rows = connection.execute(text("SELECT * FROM hq_users")).all()
    for row in rows:
        for value in row:
            assert value != PROBE


def test_a_duplicate_is_refused_on_a_legacy_database(legacy_engine):
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with pytest.raises(OwnerBootstrapError):
        create_owner(legacy_engine, username="owneradmin", prompt=prompts(OTHER, OTHER))


def test_a_refused_second_run_does_not_reset_the_password(legacy_engine):
    create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, PROBE))
    with legacy_engine.connect() as connection:
        before = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")).scalar()
    with pytest.raises(OwnerBootstrapError):
        create_owner(legacy_engine, username="owneradmin", prompt=prompts(OTHER, OTHER))
    with legacy_engine.connect() as connection:
        after = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")).scalar()
    assert after == before
    assert verify_password(PROBE, after)


def test_a_mismatched_confirmation_leaves_no_partial_row(legacy_engine):
    """The password is read before anything is written, so a mismatch cannot
    leave a half-made account behind."""
    with pytest.raises(OwnerBootstrapError):
        create_owner(legacy_engine, username="owneradmin", prompt=prompts(PROBE, OTHER))
    with legacy_engine.connect() as connection:
        names = {row[0] for row in connection.execute(text("SELECT username FROM hq_users"))}
    assert names == {"admin"}


# ===========================================================================
# A schema problem must not look like a crash
# ===========================================================================
def test_a_broken_schema_is_a_clean_refusal_not_a_traceback(tmp_path, capsys):
    """What the operator actually saw was a raw sqlite3.OperationalError and a
    traceback naming SQLAlchemy internals. An expected schema problem should
    read like a sentence."""
    made = create_engine(f"sqlite:///{tmp_path / 'nothing.db'}", future=True)
    # No hq_users table at all - the most broken a database can be here.
    code = main(["--username", "owneradmin"], engine=made, prompt=prompts(PROBE, PROBE))
    assert code != 0
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "Traceback" not in combined
    assert "OperationalError" not in combined
    assert "Refused" in combined or "Error" in combined
    assert PROBE not in combined
    assert "$2b$" not in combined


def test_a_failed_run_creates_no_owner(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'nothing.db'}", future=True)
    main(["--username", "owneradmin"], engine=made, prompt=prompts(PROBE, PROBE))
    with made.connect() as connection:
        tables = {row[0] for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "hq_users" not in tables or not connection.closed
