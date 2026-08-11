"""Comparing candidate databases before choosing one to keep for ever.

Three candidates exist and each holds something the others do not:

* the repository database  - ``admin``, ``owneradmin``, 13 real Stores, history
* the newest pilot database - the Stores an operator created most recently
* an older pilot database   - the Receiver Device that last played audible sound

Choosing between them by hand, from memory, at the end of a long day, is how a
Store's identity gets thrown away. This produces the same answer every time,
reads nothing secret, and writes nothing at all.

**It never decides.** It reports, and a person chooses.
"""

from __future__ import annotations

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

from tools.compare_databases import (  # noqa: E402
    CandidateReport,
    compare,
    describe_comparison,
    inspect_database,
)


LEGACY = """
CREATE TABLE hq_users (id INTEGER PRIMARY KEY, username VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL, role VARCHAR(50) NOT NULL DEFAULT 'admin',
    is_active BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME);
CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code VARCHAR(50) NOT NULL,
    store_name VARCHAR(200) NOT NULL, is_active BOOLEAN NOT NULL DEFAULT 1);
CREATE TABLE receiver_devices (id INTEGER PRIMARY KEY, public_id VARCHAR(64) NOT NULL,
    store_id INTEGER NOT NULL, display_name VARCHAR(200), status VARCHAR(32));
CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY);
CREATE TABLE system_logs (id INTEGER PRIMARY KEY);
"""

SECRET_LOOKING_HASH = "$2b$12$abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP"


def build(path: Path, *, users=(), stores=(), devices=(), sessions=0, logs=0):
    con = sqlite3.connect(path)
    try:
        con.executescript(LEGACY)
        for username, role in users:
            con.execute("INSERT INTO hq_users (username, password_hash, role) VALUES (?,?,?)",
                        (username, SECRET_LOOKING_HASH, role))
        for code, name in stores:
            con.execute("INSERT INTO stores (store_code, store_name) VALUES (?,?)", (code, name))
        for public_id, store_id, name, status in devices:
            con.execute("INSERT INTO receiver_devices (public_id, store_id, display_name, status)"
                        " VALUES (?,?,?,?)", (public_id, store_id, name, status))
        for _ in range(sessions):
            con.execute("INSERT INTO broadcast_sessions DEFAULT VALUES")
        for _ in range(logs):
            con.execute("INSERT INTO system_logs DEFAULT VALUES")
        con.commit()
    finally:
        con.close()
    return path


@pytest.fixture()
def candidates(tmp_path):
    repository = build(tmp_path / "speaklink_live.db",
                       users=[("admin", "ADMIN"), ("owneradmin", "OWNER")],
                       stores=[("MUM-001", "Mumbai"), ("DEL-001", "Delhi")],
                       sessions=17, logs=194)
    newest = build(tmp_path / "lan-pilot" / "20260729-181918" / "lan-pilot.db".replace("/", os.sep)
                   if False else _mk(tmp_path / "newest" / "lan-pilot.db"),
                   users=[("lan-pilot-98quvc", "OWNER")],
                   stores=[("LAN-1", "LAN pilot Store"), ("IH", "HARSHIT")],
                   devices=[("b2591b29", 1, "Legacy Receiver 1", "active")],
                   sessions=4, logs=42)
    older = build(_mk(tmp_path / "older" / "lan-pilot.db"),
                  users=[("lan-pilot-ufkzyp", "OWNER")],
                  stores=[("LAN-1", "LAN pilot Store")],
                  devices=[("c5e23dff", 1, "Legacy Receiver 1", "active"),
                           ("1f5a6c77", 1, "TESTPC", "active")],
                  sessions=28, logs=96)
    return repository, newest, older


def _mk(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ===========================================================================
# It reads, and only reads
# ===========================================================================
def test_inspecting_changes_nothing(candidates):
    import hashlib

    repository, _newest, _older = candidates
    before = hashlib.sha256(repository.read_bytes()).hexdigest()
    inspect_database(repository)
    assert hashlib.sha256(repository.read_bytes()).hexdigest() == before


def test_inspecting_leaves_no_sidecar(candidates):
    repository, _newest, _older = candidates
    inspect_database(repository)
    assert not repository.with_name(repository.name + "-wal").exists()


def test_a_missing_file_is_reported_not_raised(tmp_path):
    report = inspect_database(tmp_path / "absent.db")
    assert report.readable is False
    assert report.error


# ===========================================================================
# Nothing secret comes out
# ===========================================================================
def test_no_stored_secret_appears_anywhere(candidates):
    """Deliberately NOT named after the column it protects.

    The first version was called ``test_no_password_hash_appears_anywhere``, and
    pytest derives its tmp_path directory name from the test function - so the
    directory was ``test_no_password_hash_appears_0``, the report printed that
    path, and the assertion failed on its own test name. A scan whose forbidden
    word appears in the fixture path is measuring the fixture.

    What matters is that no stored *value* leaks, which is what is asserted.
    """
    repository, newest, older = candidates
    text = describe_comparison(compare([repository, newest, older]))
    assert SECRET_LOOKING_HASH not in text
    assert "$2b$" not in text
    # The column name, checked only in the report body rather than in paths.
    body = "\n".join(line for line in text.splitlines()
                     if "pytest-of-" not in line and "Temp" not in line)
    assert "password" not in body.lower()


def test_no_device_credential_is_read(candidates):
    """The comparison names Devices, never their credentials."""
    repository, newest, older = candidates
    text = describe_comparison(compare([repository, newest, older]))
    assert "speaklink_rcv_v1" not in text


# ===========================================================================
# What it actually reports
# ===========================================================================
def test_it_counts_what_matters(candidates):
    repository, _newest, _older = candidates
    report = inspect_database(repository)
    assert report.users == 2
    assert report.stores == 2
    assert report.sessions == 17
    assert report.logs == 194
    assert report.integrity == "ok"


def test_it_names_the_accounts_and_their_roles(candidates):
    repository, _newest, _older = candidates
    report = inspect_database(repository)
    assert ("owneradmin", "OWNER") in report.accounts
    assert ("admin", "ADMIN") in report.accounts


def test_it_names_devices_without_their_secrets(candidates):
    _repository, _newest, older = candidates
    report = inspect_database(older)
    # `devices` is the count; `devices_detail` carries the safe rows.
    names = {name for _public, name, _status in report.devices_detail}
    assert "TESTPC" in names
    assert report.devices == 2


def test_it_flags_a_throwaway_candidate(candidates):
    _repository, newest, _older = candidates
    assert inspect_database(newest).throwaway is True


def test_the_repository_database_is_not_flagged_as_throwaway(candidates):
    repository, _newest, _older = candidates
    assert inspect_database(repository).throwaway is False


# ===========================================================================
# The comparison a person reads
# ===========================================================================
def test_the_comparison_is_deterministic(candidates):
    repository, newest, older = candidates
    first = describe_comparison(compare([repository, newest, older]))
    second = describe_comparison(compare([repository, newest, older]))
    assert first == second


def test_it_says_which_database_holds_which_unique_thing(candidates):
    repository, newest, older = candidates
    text = describe_comparison(compare([repository, newest, older]))
    assert "owneradmin" in text
    assert "TESTPC" in text


def test_it_recommends_without_deciding(candidates):
    """A tool that silently picks is a tool that picks wrong at 6pm."""
    repository, newest, older = candidates
    result = compare([repository, newest, older])
    assert result.recommended is not None
    text = describe_comparison(result)
    assert "operator" in text.lower() or "you" in text.lower()
    assert "nothing has been changed" in text.lower()


def test_it_recommends_the_non_throwaway_with_the_most_history(candidates):
    repository, newest, older = candidates
    result = compare([repository, newest, older])
    assert result.recommended.path == repository


def test_it_warns_that_devices_live_elsewhere(candidates):
    """The whole reason a Store went OFFLINE."""
    repository, newest, older = candidates
    text = describe_comparison(compare([repository, newest, older]))
    assert "device" in text.lower()
    assert "re-enrol" in text.lower() or "re-enrolment" in text.lower()


def test_it_reports_an_unreadable_candidate_without_failing(tmp_path, candidates):
    repository, newest, older = candidates
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a database")
    result = compare([repository, newest, older, broken])
    text = describe_comparison(result)
    assert "broken.db" in text
    assert result.recommended.path == repository
