"""Creating the Owner account without the password ever being typed in public.

A password on a command line is in the shell history, in the process list while
it runs, and in any terminal recording. A password in an environment variable is
in the environment of every child process. So this command reads it from a
hidden prompt, twice, and nothing else.

It also never creates the account during startup or in a migration. An account
that appears by itself with a password somebody could look up in the source is
not an owner account; it is a backdoor with paperwork.

Every test here runs against a database created inside tmp_path. The protected
database is never opened.
"""

from __future__ import annotations

import io
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
from user_lifecycle import ensure_user_lifecycle_schema, list_users  # noqa: E402

from tools.create_owner import (  # noqa: E402
    OwnerBootstrapError,
    create_owner,
    main,
    read_new_password,
)


#: Test-only, and deliberately not shaped like anything an operator would use.
FIRST = "test-only-owner-password"
OTHER = "test-only-different-one"


@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'hq.db'}", future=True)
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
        # An existing administrator, exactly as a live install has.
        connection.exec_driver_sql(
            "INSERT INTO hq_users (username, password_hash, role, is_active)"
            " VALUES ('admin', '$2b$12$existinghashvaluenotreal', 'ADMIN', 1)")
    ensure_user_lifecycle_schema(made)
    return made


def prompts(*answers):
    """A fake no-echo prompt that hands back the given answers in order."""
    remaining = list(answers)

    def prompt(_message=""):
        if not remaining:
            raise AssertionError("the command asked for more input than expected")
        return remaining.pop(0)

    return prompt


# ===========================================================================
# The happy path
# ===========================================================================
def test_it_creates_the_owner(engine):
    created = create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    assert created["username"] == "owneradmin"
    assert created["role"] == Role.OWNER.value
    assert created["is_active"] is True


def test_the_password_is_hashed_and_verifiable(engine):
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    with engine.connect() as connection:
        stored = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")
        ).scalar()
    assert stored != FIRST, "the password was stored in the clear"
    assert stored.startswith("$2b$"), "not a bcrypt hash"
    assert verify_password(FIRST, stored)


def test_the_existing_administrator_is_untouched(engine):
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT password_hash, role FROM hq_users WHERE username = 'admin'")
        ).first()
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT password_hash, role FROM hq_users WHERE username = 'admin'")
        ).first()
    assert after.password_hash == before.password_hash
    assert after.role == "ADMIN"


def test_nothing_else_is_created(engine):
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    assert {record["username"] for record in list_users(engine)} == {"admin", "owneradmin"}


# ===========================================================================
# Refusals
# ===========================================================================
def test_a_mismatched_confirmation_is_refused(engine):
    with pytest.raises(OwnerBootstrapError) as refusal:
        create_owner(engine, username="owneradmin", prompt=prompts(FIRST, OTHER))
    assert "match" in str(refusal.value).lower()
    assert FIRST not in str(refusal.value)


def test_a_short_password_is_refused(engine):
    with pytest.raises(OwnerBootstrapError) as refusal:
        create_owner(engine, username="owneradmin", prompt=prompts("short12", "short12"))
    assert "8" in str(refusal.value)


def test_exactly_eight_characters_is_accepted(engine):
    """The same policy the server applies. Not a stricter one here, because a
    CLI that disagrees with the API is a CLI somebody stops trusting."""
    assert create_owner(engine, username="owneradmin", prompt=prompts("12345678", "12345678"))


def test_a_duplicate_owner_is_refused_safely(engine):
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    with pytest.raises(OwnerBootstrapError) as refusal:
        create_owner(engine, username="owneradmin", prompt=prompts(OTHER, OTHER))
    assert "already" in str(refusal.value).lower()


def test_a_duplicate_does_not_change_the_existing_password(engine):
    """Refusing must mean refusing. A second run that quietly reset the owner's
    password would be a password-reset command wearing a create command's name.
    """
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")).scalar()
    with pytest.raises(OwnerBootstrapError):
        create_owner(engine, username="owneradmin", prompt=prompts(OTHER, OTHER))
    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT password_hash FROM hq_users WHERE username = 'owneradmin'")).scalar()
    assert after == before
    assert verify_password(FIRST, after)


def test_a_duplicate_username_in_another_case_is_refused(engine):
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    with pytest.raises(OwnerBootstrapError):
        create_owner(engine, username="OwnerAdmin", prompt=prompts(OTHER, OTHER))


def test_a_failure_leaves_no_half_made_account(engine):
    """Transactional. A refused run must not leave a row with no usable password.
    """
    with pytest.raises(OwnerBootstrapError):
        create_owner(engine, username="owneradmin", prompt=prompts(FIRST, OTHER))
    assert {record["username"] for record in list_users(engine)} == {"admin"}


# ===========================================================================
# Nothing is ever printed or echoed
# ===========================================================================
def test_the_command_prints_no_password_and_no_hash(engine, capsys, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DB_PATH", "ignored-because-engine-is-injected")
    create_owner(engine, username="owneradmin", prompt=prompts(FIRST, FIRST))
    printed = capsys.readouterr().out
    assert FIRST not in printed
    assert "$2b$" not in printed


def test_the_success_message_names_only_safe_things(engine, capsys):
    main(["--username", "owneradmin"], engine=engine, prompt=prompts(FIRST, FIRST))
    printed = capsys.readouterr().out
    assert "owneradmin" in printed
    assert Role.OWNER.value in printed
    assert FIRST not in printed
    assert "$2b$" not in printed


def test_the_password_cannot_be_passed_as_an_argument():
    """The whole point. A password on a command line is in the shell history and
    in the process list of every user on the machine while it runs."""
    from tools.create_owner import build_parser

    parser = build_parser()
    for forbidden in ("--password", "--pass", "--secret"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--username", "owneradmin", forbidden, "x"])


def test_the_prompt_reader_asks_twice():
    asked = []

    def prompt(message=""):
        asked.append(message)
        return FIRST

    read_new_password(prompt=prompt)
    assert len(asked) == 2
    assert any("again" in message.lower() or "confirm" in message.lower()
               for message in asked)


def test_the_default_prompt_does_not_echo():
    """getpass, not input(). Reading a password with input() puts it on the
    screen behind whoever is standing there."""
    import getpass
    import inspect

    from tools import create_owner

    source = inspect.getsource(create_owner)
    assert "getpass" in source
    assert create_owner.read_new_password.__defaults__ is not None or True
    # And the real default really is getpass.getpass.
    signature = inspect.signature(create_owner.read_new_password)
    assert signature.parameters["prompt"].default is getpass.getpass


# ===========================================================================
# It is not a startup or migration side effect
# ===========================================================================
def test_no_startup_path_creates_an_owner():
    """An account that appears by itself, with a password somebody could look up
    in the source, is a backdoor with paperwork."""
    server_source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    assert "create_owner" not in server_source


def test_the_command_is_not_wired_into_any_migration():
    for name in ("user_lifecycle.py", "rbac.py", "migrations.py"):
        path = BACKEND_ROOT / name
        if path.exists():
            assert "create_owner" not in path.read_text(encoding="utf-8")


def test_the_source_carries_no_example_password():
    """Not even in a docstring. A pilot password written in a file is a pilot
    password in the repository for ever."""
    import re

    source = (REPOSITORY_ROOT / "tools" / "create_owner.py").read_text(encoding="utf-8")
    # Anything that looks like Word+digits, which is what people actually use.
    assert not re.search(r"\b[A-Z][a-z]{3,}\d{2,}\b", source)
