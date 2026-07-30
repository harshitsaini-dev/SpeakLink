"""Somebody has to mint the first Receiver HMAC key container. Nobody did.

Three places in this repository stated that the backend creates the container on
first start:

* ``tools/hq_runtime.py`` - *"the backend mints the container itself"*, which is
  why it stopped refusing on a zero-Device profile;
* ``scripts/Test-SpeakLinkHQAutoStart.ps1`` - *"the backend mints the HMAC
  container"*, which is why it treats an absent container as normal before the
  first start;
* the tests that were written to match both.

``backend/server.py`` does not. ``receiver_key_ring()`` calls ``load_key_ring``
and its own docstring says *"The container is never created here"*. So the
container had three documented owners and no implementation, and the first real
installed HQ start produced a running server that failed
``the Receiver key container is present``.

The failure is quieter than that check makes it look.
``build_receiver_runtime_authenticator()`` runs at import, and with no ring it
returns the **legacy Store-token authenticator alone** - for the whole life of the
process. So a Store that had enrolled a Device would silently fall back to a
shared token, and a container appearing later would change nothing until restart.

What this module has to get right, and what each group below protects:

* **Create only into emptiness.** Zero enrolled Devices is the only state where a
  new key harms nobody. One enrolled Device and a missing container is an
  emergency, not a first start.
* **Never guess the Device count.** "I could not read the database" must fail
  closed, because the convenient wrong answer is zero and zero is the answer that
  mints a key over 44 Stores' credentials.
* **Never rotate on startup.** Every restart must be a no-op. A startup path that
  can rotate is a startup path that eventually revokes every Receiver at 3am.
* **Leave nothing behind on failure.** A half-written container makes every
  credential in the database unverifiable with no obvious way back.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from key_custody import (  # noqa: E402
    FakeProtector,
    KeyContainerCorrupt,
    ProtectionScope,
    create_key_container,
    load_key_ring,
    rotate_signing_key,
)
from receiver_key_bootstrap import (  # noqa: E402
    BootstrapOutcome,
    KeyBootstrapRefused,
    bootstrap_receiver_key_container,
)


def _database(path: Path, *, devices: int = 0, with_table: bool = True) -> Path:
    """A database shaped like the real one, holding a chosen number of Devices."""
    connection = sqlite3.connect(str(path))
    try:
        if with_table:
            connection.execute(
                "CREATE TABLE receiver_devices ("
                " id INTEGER PRIMARY KEY, public_id TEXT, store_id INTEGER)"
            )
            for index in range(devices):
                connection.execute(
                    "INSERT INTO receiver_devices (id, public_id, store_id) VALUES (?,?,?)",
                    (index + 1, f"device-{index + 1}", 1),
                )
        else:
            connection.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    return path


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path: Path) -> list:
    return [s for s in ("-wal", "-shm") if Path(str(path) + s).exists()]


@pytest.fixture()
def profile(tmp_path: Path):
    """A persistent-server-shaped profile: a database and a keys directory."""
    keys = tmp_path / "keys"
    keys.mkdir()
    container = keys / "receiver-hmac-keys.bin"
    database = _database(tmp_path / "speaklink.db")
    return container, database


# ===========================================================================
# 1. A fresh, zero-Device profile gets a container
# ===========================================================================
def test_a_fresh_zero_device_profile_gets_a_container(profile):
    container, database = profile
    assert not container.exists()

    outcome = bootstrap_receiver_key_container(
        container_path=container,
        database_path=database,
        protector=FakeProtector(),
    )

    assert outcome is BootstrapOutcome.CREATED
    assert container.exists(), "the first start did not mint a key container"


def test_the_backend_can_load_the_ring_that_was_just_created(profile):
    """Creating a file is not the requirement. Creating one the backend can open
    with the same protector is."""
    container, database = profile
    protector = FakeProtector()

    bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )

    ring = load_key_ring(container, protector=protector)
    version, key = ring.signing_key()
    assert version == 1
    assert len(key) >= 32, "the first signing key is too short to be an HMAC key"
    assert ring.versions() == [1]


def test_the_container_records_the_current_user_scope(profile):
    """DPAPI CURRENT_USER binds the blob to the identity that sealed it. The
    scope is recorded so a container sealed under the wrong identity is a named
    error rather than a mystery."""
    container, database = profile
    protector = FakeProtector()

    bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )

    assert protector.scope is ProtectionScope.CURRENT_USER
    # Sealed by that identity, so a different identity cannot open it.
    with pytest.raises(Exception):
        load_key_ring(container, protector=FakeProtector(identity="somebody-else"))


def test_creating_the_container_does_not_touch_the_database(profile):
    """Requirement: solve this without changing the database. Including its
    sidecars - a read that leaves a -wal behind has written to the directory."""
    container, database = profile
    before = _fingerprint(database)

    bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=FakeProtector()
    )

    assert _fingerprint(database) == before
    assert _sidecars(database) == [], f"the count left {_sidecars(database)} behind"


# ===========================================================================
# 2. Every start after the first is a no-op
# ===========================================================================
def test_a_second_start_reuses_the_container_byte_for_byte(profile):
    container, database = profile
    protector = FakeProtector()

    first = bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )
    fingerprint = _fingerprint(container)

    second = bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )

    assert first is BootstrapOutcome.CREATED
    assert second is BootstrapOutcome.REUSED
    assert _fingerprint(container) == fingerprint, "a restart rewrote the key container"


def test_a_restart_does_not_rotate_the_signing_key(profile):
    """A startup path that can rotate is one that eventually revokes every
    Receiver during a routine reboot."""
    container, database = profile
    protector = FakeProtector()

    bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )
    before = load_key_ring(container, protector=protector)

    for _ in range(3):
        bootstrap_receiver_key_container(
            container_path=container, database_path=database, protector=protector
        )

    after = load_key_ring(container, protector=protector)
    assert after.active_version == before.active_version == 1
    assert after.versions() == before.versions()


def test_an_existing_multi_version_container_is_left_exactly_alone(profile):
    """A container that has been rotated deliberately must survive a restart with
    every key and the same active version."""
    container, database = profile
    protector = FakeProtector()
    create_key_container(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    fingerprint = _fingerprint(container)
    before = load_key_ring(container, protector=protector)
    assert before.versions() == [1, 2, 3]

    outcome = bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )

    assert outcome is BootstrapOutcome.REUSED
    assert _fingerprint(container) == fingerprint
    after = load_key_ring(container, protector=protector)
    assert after.versions() == [1, 2, 3]
    assert after.active_version == 3


def test_an_existing_container_is_reused_even_with_devices_enrolled(profile):
    """The enrolled-Device rule guards CREATION. It must not refuse a perfectly
    good container - that would take a fully working HQ off the air."""
    container, _ = profile
    database = _database(container.parent.parent / "with-devices.db", devices=12)
    protector = FakeProtector()
    create_key_container(container, protector=protector)

    outcome = bootstrap_receiver_key_container(
        container_path=container, database_path=database, protector=protector
    )

    assert outcome is BootstrapOutcome.REUSED


# ===========================================================================
# 3. Refusals - and every one of them creates nothing
# ===========================================================================
def test_a_missing_container_with_devices_enrolled_refuses(profile):
    container, _ = profile
    database = _database(container.parent.parent / "with-devices.db", devices=7)

    with pytest.raises(KeyBootstrapRefused) as refusal:
        bootstrap_receiver_key_container(
            container_path=container, database_path=database, protector=FakeProtector()
        )

    assert not container.exists(), "a refusal still created a key container"
    message = str(refusal.value)
    assert "7" in message, "the refusal does not say how many Devices are at risk"
    assert "re-enrol" in message.lower() or "re-enroll" in message.lower(), (
        "the refusal does not name the consequence the operator must weigh"
    )


def test_a_database_file_that_does_not_exist_yet_is_zero_not_unknown(profile):
    """The distinction that matters, and one I got wrong first time.

    "Absent" and "unreadable" are not the same claim. Nothing can be enrolled in a
    file that is not there, so an absent database is 0 with certainty rather than
    a guess - and a backend creating its own schema on a genuinely fresh install
    is imported before that file exists. Treating it as unknown refused 66 tests
    and would have refused a first-ever start.

    The production supervisor refuses on a missing persistent database long before
    the backend starts, so on a managed HQ this branch is not how a real database
    goes missing.
    """
    container, _ = profile
    not_yet = container.parent.parent / "created-by-startup.db"
    assert not not_yet.exists()

    outcome = bootstrap_receiver_key_container(
        container_path=container, database_path=not_yet, protector=FakeProtector()
    )

    assert outcome is BootstrapOutcome.CREATED
    assert not not_yet.exists(), "counting Devices created a database"


def test_a_database_that_exists_but_cannot_be_read_refuses(profile):
    """The other side of the same distinction: a file that IS there and will not
    open is exactly the case where zero is the dangerous answer."""
    container, _ = profile
    locked = container.parent.parent / "unreadable.db"
    locked.write_bytes(b"this is not a database at all, but it does exist")

    with pytest.raises(KeyBootstrapRefused):
        bootstrap_receiver_key_container(
            container_path=container, database_path=locked, protector=FakeProtector()
        )

    assert not container.exists()


def test_a_corrupt_database_refuses_rather_than_counting_zero(profile):
    """The convenient wrong answer is zero, and zero is the answer that mints a
    key over everybody's credentials."""
    container, _ = profile
    corrupt = container.parent.parent / "corrupt.db"
    corrupt.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)

    with pytest.raises(KeyBootstrapRefused):
        bootstrap_receiver_key_container(
            container_path=container, database_path=corrupt, protector=FakeProtector()
        )

    assert not container.exists()


def test_a_database_that_cannot_be_counted_refuses(profile):
    """Whatever the reason, an unestablished count is not zero."""
    container, database = profile

    def exploding_count(_path):
        raise sqlite3.DatabaseError("disk I/O error")

    with pytest.raises(KeyBootstrapRefused):
        bootstrap_receiver_key_container(
            container_path=container,
            database_path=database,
            protector=FakeProtector(),
            count_devices=exploding_count,
        )

    assert not container.exists()


def test_a_database_with_no_device_table_is_treated_as_empty(profile):
    """A database that predates Device enrolment genuinely has no Devices. This
    is the one absence that is safe, and it is safe because the table itself is
    missing rather than the count being unreadable."""
    container, _ = profile
    legacy = _database(container.parent.parent / "legacy.db", with_table=False)

    outcome = bootstrap_receiver_key_container(
        container_path=container, database_path=legacy, protector=FakeProtector()
    )

    assert outcome is BootstrapOutcome.CREATED


# ===========================================================================
# 4. A failed creation leaves nothing behind
# ===========================================================================
def test_a_protector_failure_leaves_no_partial_container(profile):
    container, database = profile

    class BrokenProtector(FakeProtector):
        def protect(self, payload: bytes) -> bytes:
            raise OSError("DPAPI is unavailable for this identity")

    with pytest.raises(KeyBootstrapRefused):
        bootstrap_receiver_key_container(
            container_path=container, database_path=database, protector=BrokenProtector()
        )

    assert not container.exists(), "a failed seal left a key container behind"
    leftovers = [p.name for p in container.parent.iterdir()]
    assert leftovers == [], f"a failed seal left {leftovers} in the keys directory"


def test_a_container_that_cannot_be_read_back_is_refused_not_trusted(profile):
    """Creation is only complete when the ring opens. A container that seals but
    does not load is worse than none, because the backend would start and answer
    503 to every enrolment instead of refusing at the point of the fault."""
    container, database = profile
    protector = FakeProtector()

    def corrupting_create(path, *, protector):
        Path(path).write_bytes(b"not a key container at all")

    with pytest.raises(KeyBootstrapRefused):
        bootstrap_receiver_key_container(
            container_path=container,
            database_path=database,
            protector=protector,
            create=corrupting_create,
        )

    assert not container.exists(), "an unreadable container was left in place"


def test_an_existing_corrupt_container_refuses_rather_than_replacing_it(profile):
    """A corrupt container might be recoverable from a backup. Overwriting it
    destroys the only copy of keys that verify existing credentials."""
    container, database = profile
    container.write_bytes(b"SPEAKLINK-KEYS-v1\x00garbage that will not decode")
    fingerprint = _fingerprint(container)

    with pytest.raises((KeyBootstrapRefused, KeyContainerCorrupt)):
        bootstrap_receiver_key_container(
            container_path=container, database_path=database, protector=FakeProtector()
        )

    assert _fingerprint(container) == fingerprint, "a corrupt container was overwritten"


# ===========================================================================
# 5. Nothing about a key is ever written down
# ===========================================================================
def test_no_key_material_reaches_the_log_or_the_refusal(profile, caplog):
    import base64
    import logging

    container, database = profile
    protector = FakeProtector()

    with caplog.at_level(logging.DEBUG):
        bootstrap_receiver_key_container(
            container_path=container, database_path=database, protector=protector
        )

    ring = load_key_ring(container, protector=protector)
    _, key = ring.signing_key()
    encoded = base64.b64encode(key).decode("ascii")
    text = caplog.text + "".join(str(record.args or "") for record in caplog.records)

    assert encoded not in text
    assert key.hex() not in text
    assert container.read_bytes().hex() not in text


def test_the_module_never_prints_and_never_formats_key_material():
    """Walk the AST rather than grep the text.

    A text scan flags the prose that explains the rule - this repository has been
    caught by that four times.
    """
    import ast

    source = (BACKEND_ROOT / "receiver_key_bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {"key", "keys", "payload", "secret", "material", "ring", "signing_key"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "print":
                raise AssertionError("this module prints; the backend log is the channel")
        # No f-string or format may interpolate a name that holds key material.
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    target = value.value
                    name = getattr(target, "id", None) or getattr(target, "attr", None)
                    assert name not in forbidden, (
                        f"a formatted string interpolates {name!r}"
                    )


# ===========================================================================
# 6. It is actually wired into the startup that ships
# ===========================================================================
def test_the_server_module_calls_the_bootstrap_before_building_the_authenticator():
    """The ordering is the whole defect.

    ``build_receiver_runtime_authenticator()`` runs at import and returns the
    legacy authenticator alone when there is no ring - for the life of the
    process. A container minted after that line changes nothing until a restart,
    so the bootstrap has to come first.
    """
    import ast

    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Either the direct call or the environment-gated production wrapper. The
    # ordering is the requirement, not which name it is spelled with.
    entry_points = {"bootstrap_receiver_key_container", "bootstrap_from_environment"}
    bootstrap_line = None
    configure_line = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in entry_points and bootstrap_line is None:
                bootstrap_line = node.lineno
            if name == "configure_receiver_runtime" and configure_line is None:
                configure_line = node.lineno

    assert bootstrap_line is not None, "server.py never calls the bootstrap"
    assert configure_line is not None
    assert bootstrap_line < configure_line, (
        "the bootstrap runs after the authenticator is built, so the ring it "
        "creates is not used until the next restart"
    )


def test_the_hq_package_ships_the_bootstrap_module():
    """The packaged HQ copies backend source. A module the startup path imports
    and the package does not carry is an ImportError on an operator's desk."""
    build_script = (REPOSITORY_ROOT / "scripts" / "Build-SpeakLinkHQPackage.ps1").read_text(
        encoding="utf-8"
    )
    # The package copies backend/*.py wholesale, excluding a named list. The
    # bootstrap must not be caught by any of those exclusions.
    assert "receiver_key_bootstrap" not in build_script, (
        "the build script names the bootstrap module in an exclusion list"
    )
    assert (BACKEND_ROOT / "receiver_key_bootstrap.py").exists()


# ===========================================================================
# 7. The gate - it must be impossible for a test run to mint a real key
# ===========================================================================
def test_an_unconfigured_process_attempts_nothing(monkeypatch, tmp_path):
    """The first version of this gated on SPEAKLINK_DB_PATH alone, and it would
    have had THIS SUITE mint a live container under C:\\ProgramData: conftest
    always sets SPEAKLINK_DB_PATH, the temporary database has zero Devices, and the
    container path falls back to the real service custody path. A key nobody
    decided to make is precisely what this module exists to prevent.
    """
    from receiver_key_bootstrap import bootstrap_from_environment

    monkeypatch.delenv("SPEAKLINK_KEY_CONTAINER", raising=False)
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(_database(tmp_path / "e.db")))
    would_be = tmp_path / "keys" / "receiver-hmac-keys.bin"

    assert bootstrap_from_environment(
        container_path=would_be, protector=FakeProtector()
    ) is None
    assert not would_be.exists()


def test_a_managed_start_with_both_variables_creates(monkeypatch, tmp_path):
    from receiver_key_bootstrap import bootstrap_from_environment

    container = tmp_path / "keys" / "receiver-hmac-keys.bin"
    container.parent.mkdir()
    monkeypatch.setenv("SPEAKLINK_KEY_CONTAINER", str(container))
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(_database(tmp_path / "e.db")))

    outcome = bootstrap_from_environment(
        container_path=container, protector=FakeProtector()
    )

    assert outcome is BootstrapOutcome.CREATED
    assert container.exists()


def test_the_real_service_container_path_is_never_the_implicit_target():
    """A guard on the guard. If someone re-adds a default that resolves to the
    machine's service custody path without an explicit variable, this fails.

    The docstring of the function under test *explains* that path, so this walks
    the AST with the docstring stripped rather than scanning the text. Prose that
    describes a rule has tripped a text scan in this repository five times now.
    """
    import ast

    source = (BACKEND_ROOT / "receiver_key_bootstrap.py").read_text(encoding="utf-8")
    function = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "bootstrap_from_environment"
    )
    body = list(function.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]  # drop the docstring

    names = {
        getattr(node, "id", None) or getattr(node, "attr", None)
        for statement in body for node in ast.walk(statement)
    }
    assert "SERVICE_CONTAINER_PATH" not in names, (
        "the environment gate resolves to the machine's service custody path"
    )

    literals = {
        node.value for statement in body for node in ast.walk(statement)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "SPEAKLINK_KEY_CONTAINER" in literals, (
        "the gate no longer requires an explicitly configured container path"
    )


# ===========================================================================
# 8. A real backend process, started the way the supervisor starts it
# ===========================================================================
def test_a_real_backend_import_mints_the_container_and_loads_the_ring(tmp_path):
    """The AST test proves the call is in the right place. This proves it works.

    A separate interpreter is used because the bootstrap runs at import and this
    process has already imported ``server``. The environment is the one
    ``child_environment`` in tools/hq_runtime.py builds.
    """
    import json
    import subprocess

    keys = tmp_path / "keys"
    keys.mkdir()
    container = keys / "receiver-hmac-keys.bin"
    database = _database(tmp_path / "speaklink.db")

    environment = dict(
        os.environ,
        SPEAKLINK_DB_PATH=str(database),
        SPEAKLINK_KEY_CONTAINER=str(container),
        SPEAKLINK_KEY_PROTECTOR="fake",
        JWT_SECRET="a-test-only-signing-secret-that-is-long-enough",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="a-test-only-password",
    )
    program = (
        "import server, json, os;"
        "from key_custody import load_key_ring, FakeProtector;"
        "path = os.environ['SPEAKLINK_KEY_CONTAINER'];"
        "ring = load_key_ring(path, protector=FakeProtector());"
        "print(json.dumps({'exists': os.path.exists(path),"
        " 'versions': ring.versions(), 'active': ring.active_version}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True, text=True, timeout=180,
        cwd=str(BACKEND_ROOT), env=environment,
    )

    assert completed.returncode == 0, (
        f"a managed backend start failed\nstdout:{completed.stdout}\n"
        f"stderr:{completed.stderr[-3000:]}"
    )
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["exists"] is True, "importing the backend did not mint the container"
    assert report["versions"] == [1]
    assert report["active"] == 1
    # This is the check that failed on the real installed HQ.
    assert container.exists(), "the Receiver key container is present -> FAIL"


def test_a_second_real_backend_import_changes_nothing(tmp_path):
    """Restart safety, in a real process rather than a unit."""
    import subprocess

    keys = tmp_path / "keys"
    keys.mkdir()
    container = keys / "receiver-hmac-keys.bin"
    database = _database(tmp_path / "speaklink.db")
    environment = dict(
        os.environ,
        SPEAKLINK_DB_PATH=str(database),
        SPEAKLINK_KEY_CONTAINER=str(container),
        SPEAKLINK_KEY_PROTECTOR="fake",
        JWT_SECRET="a-test-only-signing-secret-that-is-long-enough",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="a-test-only-password",
    )
    for _ in range(2):
        completed = subprocess.run(
            [sys.executable, "-c", "import server"],
            capture_output=True, text=True, timeout=180,
            cwd=str(BACKEND_ROOT), env=environment,
        )
        assert completed.returncode == 0, completed.stderr[-3000:]
        if not container.exists():
            raise AssertionError("the first start did not mint the container")
        current = _fingerprint(container)
        try:
            assert current == first
        except NameError:
            first = current


def test_a_real_backend_start_refuses_when_devices_are_enrolled(tmp_path):
    """The emergency case, end to end: the process must fail rather than start
    and mint a key over credentials that are still in use."""
    import subprocess

    keys = tmp_path / "keys"
    keys.mkdir()
    container = keys / "receiver-hmac-keys.bin"
    database = _database(tmp_path / "speaklink.db", devices=3)
    environment = dict(
        os.environ,
        SPEAKLINK_DB_PATH=str(database),
        SPEAKLINK_KEY_CONTAINER=str(container),
        SPEAKLINK_KEY_PROTECTOR="fake",
        JWT_SECRET="a-test-only-signing-secret-that-is-long-enough",
        ADMIN_USERNAME="admin",
        ADMIN_PASSWORD="a-test-only-password",
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import server"],
        capture_output=True, text=True, timeout=180,
        cwd=str(BACKEND_ROOT), env=environment,
    )

    assert completed.returncode != 0, "the backend started anyway"
    assert not container.exists(), "a refused start still created a key container"
    assert "re-enrol" in completed.stderr.lower() or "re-enroll" in completed.stderr.lower()


def test_the_frozen_runtime_does_not_need_the_bootstrap_module():
    """The supervisor must NOT be the creator.

    hq_runtime.spec excludes SQLAlchemy and starts the backend as a child under
    the machine's own Python. More importantly, DPAPI CURRENT_USER binds a
    container to the identity that sealed it, so it has to be sealed by the
    process that will open it. This asserts the split stays that way.
    """
    runtime_source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    assert "create_key_container" not in runtime_source, (
        "the supervisor creates the container; it must only refuse"
    )
    assert "bootstrap_receiver_key_container" not in runtime_source
