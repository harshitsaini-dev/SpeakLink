"""Starting the backend must never invent, restore or rotate an administrator.

``seed_admin`` used to read ADMIN_USERNAME and ADMIN_PASSWORD with hard-coded
fallbacks, and then re-aligned the stored hash on every startup:

    if not verify_password(password, existing.password_hash):
        existing.password_hash = hash_password(password)

Two separate problems. A machine with no configuration got a known password. And
a machine whose ADMIN_PASSWORD was merely *unset* had its administrator's
password silently reset back to that known value on **every restart** - a
credential rotation nobody asked for, performed by a routine boot.

Bootstrapping is now fail-closed: explicit credentials or a controlled refusal,
and an existing administrator is never modified by startup at all.

Every test here uses its own temporary SQLite file. Nothing touches
``backend/speaklink_live.db``, and no password or hash is ever printed.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# Importing models pulls in db, which resolves DB_PATH once at import time and
# defaults to backend/speaklink_live.db. This module never uses that engine - it
# builds its own per-test SQLite file - but pointing the default somewhere
# disposable first means no import order can ever aim it at the protected
# database. This file is also named to sort after test_smoke.py, which asserts
# that it is the module that imported db under its own isolated path.
os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-bootstrap-tests-never-used.db"),
)

from admin_bootstrap import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    AdminBootstrapError,
    bootstrap_administrator,
    resolve_bootstrap_credentials,
)
from auth import verify_password  # noqa: E402
from db import Base  # noqa: E402
from models import HQUser  # noqa: E402


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"

# Test-only values. Obviously fake, and never a real or historical credential.
OPERATOR = "pilot-operator"
FIRST_PASSPHRASE = "first-test-only-passphrase"
SECOND_PASSPHRASE = "second-test-only-passphrase"


@pytest.fixture()
def session_factory(tmp_path):
    """A private SQLite database for one test."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'bootstrap.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield factory
    engine.dispose()


def _environment(username=OPERATOR, password=FIRST_PASSPHRASE) -> dict:
    environment = {}
    if username is not None:
        environment[ADMIN_USERNAME_ENV] = username
    if password is not None:
        environment[ADMIN_PASSWORD_ENV] = password
    return environment


def _administrators(factory) -> list[HQUser]:
    with factory() as db:
        return db.query(HQUser).all()


def _stored_hash(factory, username: str = OPERATOR) -> str:
    with factory() as db:
        row = db.query(HQUser).filter(HQUser.username == username).first()
        assert row is not None, "the administrator row is missing"
        return row.password_hash


# ---------------------------------------------------------------------------
# 1. No fallback credential survives anywhere in the bootstrap path
# ---------------------------------------------------------------------------
def test_the_bootstrap_source_declares_no_fallback_credential():
    """os.environ.get(NAME, "something") is exactly the defect being removed."""
    import re

    for module in ("seed.py", "admin_bootstrap.py"):
        source = (BACKEND_ROOT / module).read_text(encoding="utf-8")
        offenders = re.findall(
            r"environ\.get\(\s*[^,)]*(?:ADMIN_USERNAME|ADMIN_PASSWORD|"
            rf"{ADMIN_USERNAME_ENV}|{ADMIN_PASSWORD_ENV})[^,)]*,\s*[^)]+\)",
            source,
        )
        assert not offenders, f"{module} still supplies a default credential: {offenders}"


def test_the_historical_default_password_is_absent_from_the_bootstrap_path():
    # Built from parts so the value is not written down again in this repository.
    historical = "admin" + "123"
    for module in ("seed.py", "admin_bootstrap.py", "server.py"):
        source = (BACKEND_ROOT / module).read_text(encoding="utf-8")
        assert historical not in source, f"{module} still contains the old default password"


# ---------------------------------------------------------------------------
# 2-5. Missing or blank credentials fail closed
# ---------------------------------------------------------------------------
def test_a_missing_username_is_refused():
    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials(_environment(username=None))


def test_a_missing_password_is_refused():
    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials(_environment(password=None))


def test_both_missing_is_refused():
    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials({})


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "  \r\n  "])
def test_a_blank_username_is_refused(blank: str):
    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials(_environment(username=blank))


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", "  \r\n  "])
def test_a_blank_password_is_refused(blank: str):
    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials(_environment(password=blank))


def test_the_refusal_names_the_variable_but_never_its_value():
    """An operator must learn what to set without the log learning the secret."""
    try:
        resolve_bootstrap_credentials(_environment(password="   "))
    except AdminBootstrapError as refusal:
        message = str(refusal)
    else:
        pytest.fail("a blank password was accepted")

    assert ADMIN_PASSWORD_ENV in message
    for secret in (FIRST_PASSPHRASE, SECOND_PASSPHRASE, "   "):
        assert secret not in message.replace(ADMIN_PASSWORD_ENV, "")


# ---------------------------------------------------------------------------
# 6. Nothing is written when the configuration is refused
# ---------------------------------------------------------------------------
def test_a_refusal_creates_no_administrator(session_factory):
    with pytest.raises(AdminBootstrapError):
        with session_factory() as db:
            credentials = resolve_bootstrap_credentials({})
            bootstrap_administrator(db, credentials)
    assert _administrators(session_factory) == []


def test_a_refusal_leaves_an_existing_administrator_untouched(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    before = _stored_hash(session_factory)

    with pytest.raises(AdminBootstrapError):
        resolve_bootstrap_credentials({})

    assert _stored_hash(session_factory) == before


# ---------------------------------------------------------------------------
# 7. A fresh database gets exactly one administrator
# ---------------------------------------------------------------------------
def test_an_empty_database_gets_one_administrator(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))

    rows = _administrators(session_factory)
    assert len(rows) == 1
    assert rows[0].username == OPERATOR
    assert rows[0].role == "admin"
    assert rows[0].is_active is True


def test_the_created_administrator_can_authenticate(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    assert verify_password(FIRST_PASSPHRASE, _stored_hash(session_factory)) is True


# ---------------------------------------------------------------------------
# 8-11. Restarting must never rotate anything
# ---------------------------------------------------------------------------
def test_restarting_with_the_same_credentials_does_not_touch_the_hash(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    before = _stored_hash(session_factory)

    for _ in range(3):
        with session_factory() as db:
            bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))

    assert _stored_hash(session_factory) == before, (
        "the stored hash was rewritten by a restart; bcrypt salts differ every "
        "time, so even re-hashing the same password is a silent change"
    )


def test_restarting_with_a_different_password_does_not_rotate_it(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    before = _stored_hash(session_factory)

    with session_factory() as db:
        bootstrap_administrator(
            db, resolve_bootstrap_credentials(_environment(password=SECOND_PASSPHRASE))
        )

    assert _stored_hash(session_factory) == before


def test_the_original_password_still_works_after_a_restart(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    with session_factory() as db:
        bootstrap_administrator(
            db, resolve_bootstrap_credentials(_environment(password=SECOND_PASSPHRASE))
        )

    assert verify_password(FIRST_PASSPHRASE, _stored_hash(session_factory)) is True


def test_the_new_environment_password_does_not_silently_become_valid(session_factory):
    """This is the whole point: startup is not a password-rotation tool."""
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    with session_factory() as db:
        bootstrap_administrator(
            db, resolve_bootstrap_credentials(_environment(password=SECOND_PASSPHRASE))
        )

    assert verify_password(SECOND_PASSPHRASE, _stored_hash(session_factory)) is False


def test_a_different_username_does_not_create_a_second_administrator(session_factory):
    """A changed ADMIN_USERNAME used to add a row, which then looked exactly
    like a broken password: the operator was signing in as a user that had
    never been created."""
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    with session_factory() as db:
        bootstrap_administrator(
            db, resolve_bootstrap_credentials(_environment(username="someone-else"))
        )

    rows = _administrators(session_factory)
    assert len(rows) == 1
    assert rows[0].username == OPERATOR


# ---------------------------------------------------------------------------
# 12-13. Nothing plaintext, nothing leaked
# ---------------------------------------------------------------------------
def test_no_plaintext_password_is_stored(session_factory):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))

    with session_factory() as db:
        row = db.query(HQUser).first()
        stored = " ".join(str(value) for value in (row.username, row.password_hash, row.role))
    assert FIRST_PASSPHRASE not in stored
    assert row.password_hash.startswith("$2")  # bcrypt, unchanged algorithm


def test_bootstrapping_prints_nothing(session_factory, capsys):
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert FIRST_PASSPHRASE not in stream
        assert "$2" not in stream


def test_the_credentials_object_does_not_leak_the_password_when_displayed():
    """A dataclass repr in a traceback would otherwise publish the secret."""
    credentials = resolve_bootstrap_credentials(_environment())
    for rendering in (repr(credentials), str(credentials)):
        assert FIRST_PASSPHRASE not in rendering


# ---------------------------------------------------------------------------
# 14. The local pilot still supplies explicit credentials
# ---------------------------------------------------------------------------
def test_the_local_pilot_supplies_both_bootstrap_variables(monkeypatch):
    from tools import local_pilot

    monkeypatch.setenv(local_pilot.ADMIN_USERNAME_ENV, OPERATOR)
    monkeypatch.setenv(local_pilot.ADMIN_PASSWORD_ENV, FIRST_PASSPHRASE)

    assert local_pilot.require_pilot_password() == FIRST_PASSPHRASE
    assert local_pilot._pilot_username().strip() != ""


def test_the_local_pilot_refuses_a_blank_password(monkeypatch):
    from tools import local_pilot

    monkeypatch.setenv(local_pilot.ADMIN_PASSWORD_ENV, "   ")
    with pytest.raises(local_pilot.PilotCredentialError):
        local_pilot.require_pilot_password()


# ---------------------------------------------------------------------------
# 15. The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched_by_bootstrapping(session_factory):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    with session_factory() as db:
        bootstrap_administrator(db, resolve_bootstrap_credentials(_environment()))
    assert metadata() == before

    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists(), (
            f"a {sidecar} sidecar appeared next to the protected database"
        )
