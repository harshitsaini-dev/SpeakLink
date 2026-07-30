"""A restart must not need the credentials that created the administrator.

``test_startup_admin_bootstrap.py`` proves that ``bootstrap_administrator``
never modifies an administrator that already exists, and it proves it
thoroughly - same password, different password, different username, hash
untouched, original password still working.

Every one of those tests builds a ``BootstrapCredentials`` by hand and calls
``bootstrap_administrator`` directly. **None of them calls ``seed_admin``**, and
``seed_admin`` is where the ordering is:

    credentials = resolve_bootstrap_credentials()   # raises when unset
    return bootstrap_administrator(db, credentials) # would have returned
                                                    # ALREADY_PRESENT

So the write path was idempotent and well covered, and the *startup* path was
not idempotent at all: it demanded ADMIN_USERNAME and ADMIN_PASSWORD on every
boot for ever, long after the account they describe was created. The installed
HQ hit it on the first real start with a persistent database that already held
an enabled administrator:

    startup_event() -> seed_admin(db) -> resolve_bootstrap_credentials()
      -> AdminBootstrapError: ADMIN_USERNAME is not set, or is blank.

The idempotency was tested one layer below the defect. That is the lesson, and
these tests deliberately drive ``seed_admin`` - and one drives the whole
``startup_event`` - so the fix is proven where it actually failed.

What must be true:

* **An enabled administrator means the environment is not consulted at all.**
  Not "consulted and tolerated" - a restart must not depend on a plaintext
  credential existing anywhere on the machine.
* **Nothing is written.** No hash, no role, no username, no second row.
* **An empty database still refuses without credentials.** The fail-closed rule
  that replaced the known-password fallback is not being relaxed.
* **An unreadable database is not an empty one.** Fail closed; the convenient
  answer creates an administrator nobody asked for.

Every test uses its own temporary SQLite file. Nothing touches
``backend/echocast_live.db``, and no password or hash is printed.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
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

from admin_bootstrap import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    ALREADY_PRESENT,
    CREATED,
    NO_ENABLED_ADMINISTRATOR,
    AdminBootstrapError,
    AdminStateUnavailable,
    count_enabled_administrators,
)
from auth import verify_password  # noqa: E402
from db import Base  # noqa: E402
from models import HQUser  # noqa: E402
from seed import seed_admin  # noqa: E402


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"
EXISTING_PASSWORD = "the-password-that-created-this-account"


def _seed_globals():
    """The namespace ``seed_admin`` actually resolves its callables from.

    NOT ``monkeypatch.setattr("seed.…")``, and not ``sys.modules["seed"]``
    either - I tried both. Several fixtures in this suite pop ``seed`` and
    ``server`` out of ``sys.modules`` and re-import them, so by the time these
    tests run, ``sys.modules["seed"]`` can be a *different* module object from
    the one this file imported ``seed_admin`` out of. Patching either one patched
    an instance nobody was calling.

    ``__module__`` does not help, because it is the string ``"seed"`` and looking
    it up goes straight back to the wrong instance. ``__globals__`` is the actual
    dictionary this particular function object reads its globals from, whatever
    happened to ``sys.modules`` afterwards.

    Both tests passed in isolation and failed only in a full run, which is
    exactly how a real assertion gets written off as a flake.
    """
    return seed_admin.__globals__


@pytest.fixture()
def session_factory(tmp_path):
    database = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{database}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def no_bootstrap_environment(monkeypatch):
    """Every test starts with the environment unset.

    This is the state the installed HQ was actually in, and the state a normal
    restart is in: nobody re-exports a bootstrap password to reboot a server.
    """
    monkeypatch.delenv(ADMIN_USERNAME_ENV, raising=False)
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)


@pytest.fixture(autouse=True)
def protected_database_is_never_opened():
    """The whole-file guard this repository uses everywhere else."""
    before = (
        hashlib.sha256(PROTECTED_DATABASE.read_bytes()).hexdigest()
        if PROTECTED_DATABASE.exists() else None
    )
    yield
    if before is not None:
        after = hashlib.sha256(PROTECTED_DATABASE.read_bytes()).hexdigest()
        assert after == before, "a test touched the protected database"


def _administrator(factory, *, username="founder", role="ADMIN", is_active=True,
                   lifecycle_state="active", password=EXISTING_PASSWORD):
    from auth import hash_password

    with factory() as db:
        db.add(HQUser(
            username=username,
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
            lifecycle_state=lifecycle_state,
        ))
        db.commit()


def _row(factory, username="founder"):
    with factory() as db:
        user = db.query(HQUser).filter(HQUser.username == username).first()
        if user is None:
            return None
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "lifecycle_state": user.lifecycle_state,
            "session_version": user.session_version,
            # Fingerprint, never the hash itself.
            "hash_fingerprint": hashlib.sha256(
                str(user.password_hash).encode()).hexdigest()[:12],
        }


# ===========================================================================
# 1. The defect: a restart with an existing administrator and no environment
# ===========================================================================
def test_a_restart_with_an_existing_administrator_needs_no_environment(session_factory):
    """The exact failure from the installed machine."""
    _administrator(session_factory)

    with session_factory() as db:
        outcome = seed_admin(db)

    assert outcome == ALREADY_PRESENT


def test_the_restart_does_not_read_the_bootstrap_environment_at_all(session_factory,
                                                                    monkeypatch):
    """Not "reads it and tolerates absence" - does not read it.

    A restart that consults ADMIN_PASSWORD is a restart that depends on a
    plaintext credential still being on the machine, which is how the previous
    design ended up resetting passwords on boot.
    """
    _administrator(session_factory)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("startup read the bootstrap credentials")

    monkeypatch.setitem(_seed_globals(), "resolve_bootstrap_credentials", forbidden)

    with session_factory() as db:
        assert seed_admin(db) == ALREADY_PRESENT


def test_the_existing_password_hash_is_unchanged_by_a_restart(session_factory):
    _administrator(session_factory)
    before = _row(session_factory)

    with session_factory() as db:
        seed_admin(db)

    assert _row(session_factory) == before


def test_the_existing_password_still_works_after_a_restart(session_factory):
    _administrator(session_factory)

    with session_factory() as db:
        seed_admin(db)

    with session_factory() as db:
        user = db.query(HQUser).first()
        assert verify_password(EXISTING_PASSWORD, user.password_hash)


def test_a_restart_writes_nothing_at_all(session_factory):
    """No row added, no column changed, no session invalidated."""
    _administrator(session_factory)
    with session_factory() as db:
        before_count = db.query(HQUser).count()
    before = _row(session_factory)

    with session_factory() as db:
        seed_admin(db)

    with session_factory() as db:
        assert db.query(HQUser).count() == before_count
    after = _row(session_factory)
    assert after == before
    assert after["session_version"] == before["session_version"], (
        "startup invalidated the administrator's sessions"
    )


def test_many_restarts_remain_idempotent(session_factory):
    _administrator(session_factory)
    before = _row(session_factory)

    for _ in range(5):
        with session_factory() as db:
            assert seed_admin(db) == ALREADY_PRESENT

    assert _row(session_factory) == before


def test_a_restart_with_different_environment_credentials_changes_nothing(
        session_factory, monkeypatch):
    """Stale variables left in a service configuration must not reset anybody."""
    _administrator(session_factory)
    before = _row(session_factory)
    monkeypatch.setenv(ADMIN_USERNAME_ENV, "somebody-else")
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "a-completely-different-password")

    with session_factory() as db:
        assert seed_admin(db) == ALREADY_PRESENT

    assert _row(session_factory) == before
    with session_factory() as db:
        assert db.query(HQUser).count() == 1, "a second administrator was created"
        user = db.query(HQUser).first()
        assert verify_password(EXISTING_PASSWORD, user.password_hash)
        assert not verify_password("a-completely-different-password", user.password_hash)


# ===========================================================================
# 2. The fail-closed rules that must NOT be relaxed
# ===========================================================================
def test_an_empty_database_without_credentials_is_still_refused(session_factory):
    with session_factory() as db:
        with pytest.raises(AdminBootstrapError) as refusal:
            seed_admin(db)

    assert ADMIN_USERNAME_ENV in str(refusal.value)
    with session_factory() as db:
        assert db.query(HQUser).count() == 0


def test_an_empty_database_with_credentials_creates_exactly_one(session_factory,
                                                                monkeypatch):
    monkeypatch.setenv(ADMIN_USERNAME_ENV, "founder")
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "a-deliberate-bootstrap-password")

    with session_factory() as db:
        assert seed_admin(db) == CREATED

    with session_factory() as db:
        assert db.query(HQUser).count() == 1


def test_a_blank_password_is_still_refused_on_an_empty_database(session_factory,
                                                                monkeypatch):
    monkeypatch.setenv(ADMIN_USERNAME_ENV, "founder")
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "   ")

    with session_factory() as db:
        with pytest.raises(AdminBootstrapError):
            seed_admin(db)

    with session_factory() as db:
        assert db.query(HQUser).count() == 0


# ===========================================================================
# 3. What counts as an enabled administrator
# ===========================================================================
@pytest.mark.parametrize("role", ["OWNER", "ADMIN", "SUPER_ADMIN", "admin"])
def test_every_privileged_role_counts(session_factory, role):
    """The same set Test-EchoCastPersistentLanServer.ps1 uses. Two definitions of
    "administrator" would eventually disagree, and the verifier's is the one an
    operator has already been shown."""
    _administrator(session_factory, role=role)

    with session_factory() as db:
        assert count_enabled_administrators(db) == 1
        assert seed_admin(db) == ALREADY_PRESENT


@pytest.mark.parametrize("role", ["VIEWER", "BROADCASTER"])
def test_an_unprivileged_account_is_not_an_administrator(session_factory, role):
    _administrator(session_factory, role=role)

    with session_factory() as db:
        assert count_enabled_administrators(db) == 0


def test_a_disabled_administrator_does_not_count_as_enabled(session_factory):
    _administrator(session_factory, is_active=False, lifecycle_state="disabled")

    with session_factory() as db:
        assert count_enabled_administrators(db) == 0


def test_an_archived_administrator_does_not_count_as_enabled(session_factory):
    _administrator(session_factory, is_active=False, lifecycle_state="archived")

    with session_factory() as db:
        assert count_enabled_administrators(db) == 0


def test_only_a_disabled_administrator_creates_nothing_and_demands_nothing(
        session_factory, caplog):
    """THE DOCUMENTED POLICY, made explicit and tested rather than left to fall
    out of whichever branch runs first.

    ``bootstrap_administrator`` has always gated creation on "does any HQ user
    exist", not on "is there an enabled administrator", and says so in its
    docstring with the reason: a changed ADMIN_USERNAME used to add a second row
    that looked exactly like a broken password. That gate is unchanged here.

    So a database whose only administrator is disabled gets neither a new
    account nor a credential demand. Startup continues and says so loudly:

    * **Creating one would be startup performing an administrative act.** An
      operator who deliberately disabled an account would find a restart had
      quietly granted a new one - the exact class of behaviour this module was
      written to remove.
    * **Refusing to start would take the Receivers off the air** over an HQ
      sign-in problem. A Store playing announcements does not need anybody
      signed in to HQ.

    It is also not reachable through supported operations: the lifecycle rules
    refuse to disable or archive the last active privileged account.
    """
    import logging

    _administrator(session_factory, is_active=False, lifecycle_state="disabled")

    with caplog.at_level(logging.WARNING):
        with session_factory() as db:
            outcome = seed_admin(db)

    assert outcome == NO_ENABLED_ADMINISTRATOR
    with session_factory() as db:
        assert db.query(HQUser).count() == 1, "startup created an account"
        assert db.query(HQUser).first().is_active is False, "startup re-enabled an account"
    assert "administrator" in caplog.text.lower(), "the state was not reported"


def test_a_disabled_administrator_beside_an_enabled_one_is_fine(session_factory):
    _administrator(session_factory, username="active-one")
    _administrator(session_factory, username="disabled-one",
                   is_active=False, lifecycle_state="disabled")

    with session_factory() as db:
        assert count_enabled_administrators(db) == 1
        assert seed_admin(db) == ALREADY_PRESENT


# ===========================================================================
# 4. Fail closed - an unreadable database is not an empty one
# ===========================================================================
def test_an_unreadable_database_fails_closed(session_factory, monkeypatch):
    """The convenient answer is "no administrators", and that answer creates one."""
    from sqlalchemy.exc import OperationalError

    class BrokenSession:
        def query(self, *_args, **_kwargs):
            raise OperationalError("SELECT", {}, Exception("disk I/O error"))

    with pytest.raises(AdminStateUnavailable):
        seed_admin(BrokenSession())


def test_a_missing_users_table_fails_closed(tmp_path):
    """A database with no hq_users table cannot be counted. It is not empty - it
    is unreadable for this question, and the two must not be confused."""
    engine = create_engine(f"sqlite:///{tmp_path / 'no-table.db'}", future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    factory = sessionmaker(bind=engine, future=True)
    try:
        with factory() as db:
            with pytest.raises(AdminStateUnavailable):
                seed_admin(db)
    finally:
        engine.dispose()


def test_a_failure_to_count_creates_no_administrator(session_factory, monkeypatch):
    def exploding(_db):
        raise AdminStateUnavailable("the administrator state could not be read")

    monkeypatch.setitem(_seed_globals(), "count_enabled_administrators", exploding)

    with session_factory() as db:
        with pytest.raises(AdminStateUnavailable):
            seed_admin(db)

    with session_factory() as db:
        assert db.query(HQUser).count() == 0


# ===========================================================================
# 5. Nothing is written down
# ===========================================================================
def test_a_restart_prints_nothing(session_factory, capsys):
    _administrator(session_factory)

    with session_factory() as db:
        seed_admin(db)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_no_username_password_or_hash_reaches_the_log(session_factory, caplog):
    import logging

    _administrator(session_factory, username="founder")

    with caplog.at_level(logging.DEBUG):
        with session_factory() as db:
            seed_admin(db)

    with session_factory() as db:
        stored = db.query(HQUser).first().password_hash

    text_out = caplog.text
    assert EXISTING_PASSWORD not in text_out
    assert str(stored) not in text_out
    assert "$2b$" not in text_out


def test_a_refusal_never_quotes_a_credential_value(session_factory, monkeypatch):
    monkeypatch.setenv(ADMIN_USERNAME_ENV, "founder")
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "")

    with session_factory() as db:
        with pytest.raises(AdminBootstrapError) as refusal:
            seed_admin(db)

    message = str(refusal.value)
    assert "founder" not in message
    assert ADMIN_PASSWORD_ENV in message


# ===========================================================================
# 6. The path that actually failed on the installed machine
# ===========================================================================
def test_startup_event_completes_against_a_database_that_already_has_an_admin(tmp_path):
    """The regression test for the real failure, through the real startup path.

    A separate interpreter, because ``startup_event`` binds to the engine that
    ``db`` resolved at import and this process has already imported it. The
    environment is what a persistent HQ restart looks like: a database, a key
    container, a signing secret - and no ADMIN_USERNAME or ADMIN_PASSWORD
    anywhere.
    """
    import json
    import subprocess

    from auth import hash_password
    from key_custody import FakeProtector, create_key_container

    database = tmp_path / "persistent.db"
    container = tmp_path / "keys" / "receiver-hmac-keys.bin"
    container.parent.mkdir()
    create_key_container(container, protector=FakeProtector())

    # An HQ that has already been set up: one enabled administrator, created by
    # a previous start whose credentials are long gone.
    engine = create_engine(f"sqlite:///{database}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as db:
        db.add(HQUser(username="founder", password_hash=hash_password(EXISTING_PASSWORD),
                      role="ADMIN", is_active=True, lifecycle_state="active"))
        db.commit()
    engine.dispose()

    environment = {
        key: value for key, value in os.environ.items()
        if key not in {ADMIN_USERNAME_ENV, ADMIN_PASSWORD_ENV}
    }
    environment.update(
        ECHOCAST_DB_PATH=str(database),
        ECHOCAST_KEY_CONTAINER=str(container),
        ECHOCAST_KEY_PROTECTOR="fake",
        JWT_SECRET="a-test-only-signing-secret-that-is-long-enough",
    )
    # backend/.env is loaded by server.py from a path relative to itself, so no
    # amount of cwd or env filtering keeps its ADMIN_USERNAME out of this
    # subprocess - which is exactly why this defect never showed on a developer
    # machine and surfaced on the first installed start. THE INSTALLED HQ HAS NO
    # backend/.env, so dotenv is neutralised here to reproduce that faithfully.
    # The real file is not read, moved or modified.
    program = (
        "import os, json;"
        "os.environ.pop('ADMIN_USERNAME', None);"
        "os.environ.pop('ADMIN_PASSWORD', None);"
        "import dotenv;"
        "dotenv.load_dotenv = lambda *a, **k: False;"
        "import server;"
        "assert os.environ.get('ADMIN_USERNAME') is None, 'credentials leaked in';"
        "assert os.environ.get('ADMIN_PASSWORD') is None, 'credentials leaked in';"
        "server.startup_event();"
        "from db import SessionLocal;"
        "from models import HQUser;"
        "db = SessionLocal();"
        "rows = [(u.username, u.role, u.is_active) for u in db.query(HQUser).all()];"
        "db.close();"
        "print(json.dumps({'ok': True, 'rows': rows,"
        " 'admin_env': os.environ.get('ADMIN_USERNAME')}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=300,
        cwd=str(BACKEND_ROOT), env=environment,
    )

    assert completed.returncode == 0, (
        "startup_event failed on a database that already has an administrator\n"
        f"stdout:{completed.stdout}\nstderr:{completed.stderr[-4000:]}"
    )
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["ok"] is True
    assert report["admin_env"] is None, "the test leaked a bootstrap variable"
    assert len(report["rows"]) == 1, f"startup changed the accounts: {report['rows']}"
    assert report["rows"][0][0] == "founder"

    # And the password it was created with still works.
    engine = create_engine(f"sqlite:///{database}", future=True)
    factory = sessionmaker(bind=engine, future=True)
    try:
        with factory() as db:
            assert verify_password(EXISTING_PASSWORD,
                                   db.query(HQUser).first().password_hash)
    finally:
        engine.dispose()


def test_the_startup_path_checks_the_database_before_the_environment():
    """The ordering IS the defect, so it is asserted directly.

    ``seed_admin`` resolved credentials on line one and consulted the database on
    line two, which made a plaintext credential a precondition of every boot for
    ever. A future edit that puts them back in that order fails here rather than
    on an operator's machine.
    """
    import ast

    source = (BACKEND_ROOT / "seed.py").read_text(encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "seed_admin"
    )

    count_line = None
    resolve_line = None
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "count_enabled_administrators" and count_line is None:
                count_line = node.lineno
            if name == "resolve_bootstrap_credentials" and resolve_line is None:
                resolve_line = node.lineno

    assert count_line is not None, "seed_admin never counts administrators"
    assert resolve_line is not None, "seed_admin no longer resolves credentials at all"
    assert count_line < resolve_line, (
        "seed_admin resolves bootstrap credentials before checking the database, "
        "so every restart depends on a plaintext credential"
    )
