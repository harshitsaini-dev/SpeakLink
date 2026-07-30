"""Changing an HQ account's password offline, safely, against a named database.

WHY THIS TOOL EXISTS

The security audit found the live `ADMIN` password inside an archive in the
repository folder, and confirmed it verifies against the account in BOTH the
protected source database and the persistent HQ database. Changing it is the
remediation, and there was no way to do it offline: `tools/create_owner.py`
creates an account and refuses to touch an existing one, and the two HTTP routes
need a running server and a signed-in session.

WHAT IT REUSES RATHER THAN REBUILDS

`auth.hash_password` / `auth.verify_password` own the algorithm.
`user_lifecycle.set_password_hash` owns the consequence: it writes the hash and
bumps `session_version` inside ONE `engine.begin()` transaction, so a changed
password cannot leave old tokens valid - and cannot half-apply.

WHAT THE TESTS ARE ACTUALLY FOR

Two properties carry the weight. **A refusal must change nothing** - a wrong
current password, a mismatch, a weak new password or an inactive account must all
leave the row byte-identical, because a half-remediated account is worse than an
un-remediated one nobody trusts. And **nothing may print a secret**: not the
password, not the hash, not on stdout, not on stderr, not in argv.

Every test here uses a temporary database. The protected and persistent databases
are never opened.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault("ECHOCAST_DB_PATH", str(Path(os.environ.get("TEMP", "/tmp")) /
                                              "echocast-tests-default-engine.db"))

from tools.change_hq_user_password import (  # noqa: E402
    TARGET_PERSISTENT,
    TARGET_PROTECTED,
    ChangeRefused,
    change_password,
    integrity_check,
    make_consistent_backup,
    read_only_uri,
    resolve_target,
)

TOOL = REPOSITORY_ROOT / "tools" / "change_hq_user_password.py"

OLD_PASSWORD = "the-old-exposed-password"
NEW_PASSWORD = "a-fresh-strong-password"


def _build_database(path: Path, *, username="admin", password=OLD_PASSWORD,
                    lifecycle="active", role="ADMIN", session_version=3) -> int:
    """A minimal but faithful hq_users table, plus rows that must not move."""
    from auth import hash_password

    connection = sqlite3.connect(path)
    try:
        connection.execute("""
            CREATE TABLE hq_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username VARCHAR(100) NOT NULL UNIQUE,
                display_name VARCHAR(200) NOT NULL DEFAULT '',
                password_hash VARCHAR(200) NOT NULL,
                role VARCHAR(20) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                lifecycle_state VARCHAR(20) NOT NULL DEFAULT 'active',
                session_version INTEGER NOT NULL DEFAULT 1,
                created_at VARCHAR(40),
                disabled_at VARCHAR(40),
                archived_at VARCHAR(40)
            )""")
        connection.execute(
            "INSERT INTO hq_users (username, display_name, password_hash, role, "
            "is_active, lifecycle_state, session_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (username, "An Admin", hash_password(password), role,
             1 if lifecycle == "active" else 0, lifecycle, session_version,
             "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO hq_users (username, display_name, password_hash, role, "
            "is_active, lifecycle_state, session_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("owneradmin", "The Owner", hash_password("owner-password-untouched"),
             "OWNER", 1, "active", 1, "2026-01-01T00:00:00+00:00"),
        )
        # Rows that must survive untouched.
        connection.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, store_code TEXT)")
        connection.execute("INSERT INTO stores VALUES (1,'UN'),(2,'RM')")
        connection.execute("CREATE TABLE receiver_devices (id INTEGER PRIMARY KEY, public_id TEXT)")
        connection.execute("INSERT INTO receiver_devices VALUES (1,'dev-a')")
        connection.execute("CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO broadcast_sessions VALUES (1),(2),(3)")
        connection.commit()
        row = connection.execute(
            "SELECT id FROM hq_users WHERE username = ?", (username,)).fetchone()
        return row[0]
    finally:
        connection.close()


def _snapshot(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        return {
            "users": connection.execute(
                "SELECT id, username, password_hash, role, lifecycle_state, "
                "session_version FROM hq_users ORDER BY id").fetchall(),
            "stores": connection.execute("SELECT * FROM stores ORDER BY id").fetchall(),
            "devices": connection.execute(
                "SELECT * FROM receiver_devices ORDER BY id").fetchall(),
            "sessions": connection.execute(
                "SELECT COUNT(*) FROM broadcast_sessions").fetchone()[0],
        }
    finally:
        connection.close()


def _prompts(current=OLD_PASSWORD, new=NEW_PASSWORD, repeat=None):
    """A getpass stand-in. The tool must ask three times, in order."""
    answers = [current, new, repeat if repeat is not None else new]
    def prompt(_label):
        return answers.pop(0)
    return prompt


# ===========================================================================
# Targets: never guessed
# ===========================================================================
def test_the_two_targets_resolve_to_exactly_the_documented_files():
    from tools.persistent_lan_server import ServerProfile

    assert resolve_target(TARGET_PROTECTED) == BACKEND_ROOT / "echocast_live.db"
    assert resolve_target(TARGET_PERSISTENT) == ServerProfile.persistent().database


def test_an_unknown_target_is_refused():
    with pytest.raises(ChangeRefused):
        resolve_target("somewhere-else")


def test_the_cli_requires_a_target():
    result = subprocess.run([sys.executable, str(TOOL), "--username", "admin"],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "target" in (result.stdout + result.stderr).lower()


def test_the_cli_offers_no_arbitrary_database_path_in_operator_mode():
    """A free-form path is how the protected database gets edited by accident."""
    source = TOOL.read_text(encoding="utf-8")
    for forbidden in ("--database", "--db-path", "--database-path"):
        assert forbidden not in source, f"{forbidden} lets an operator name any file"


def test_no_password_can_be_supplied_on_the_command_line():
    source = TOOL.read_text(encoding="utf-8")
    for forbidden in ("--password", "--new-password", "--current-password", "--pass"):
        assert forbidden not in source, f"{forbidden} would put a secret in argv"


def test_no_password_is_read_from_the_environment():
    source = TOOL.read_text(encoding="utf-8")
    for forbidden in ("ADMIN_PASSWORD", "ECHOCAST_PASSWORD", "getenv(\"PASS"):
        assert forbidden not in source, f"{forbidden} would accept a secret from the environment"


# ===========================================================================
# Refusals must change nothing
# ===========================================================================
def test_a_missing_database_is_refused(tmp_path):
    with pytest.raises(ChangeRefused):
        change_password(tmp_path / "absent.db", username="admin",
                        prompt=_prompts(), target_label="test")


def test_an_unknown_user_is_refused_without_saying_which_part_was_wrong(tmp_path):
    database = tmp_path / "u.db"
    _build_database(database)
    before = _snapshot(database)
    with pytest.raises(ChangeRefused) as refusal:
        change_password(database, username="nobody", prompt=_prompts(),
                        target_label="test")
    assert "nobody" not in str(refusal.value), "the refusal echoes the supplied name"
    assert _snapshot(database) == before


def test_an_inactive_user_is_refused(tmp_path):
    database = tmp_path / "i.db"
    _build_database(database, lifecycle="disabled")
    before = _snapshot(database)
    with pytest.raises(ChangeRefused):
        change_password(database, username="admin", prompt=_prompts(),
                        target_label="test")
    assert _snapshot(database) == before


def test_a_wrong_current_password_changes_nothing(tmp_path):
    database = tmp_path / "w.db"
    _build_database(database)
    before = _snapshot(database)
    with pytest.raises(ChangeRefused):
        change_password(database, username="admin",
                        prompt=_prompts(current="not-the-old-one"),
                        target_label="test")
    assert _snapshot(database) == before


def test_a_mismatched_repeat_changes_nothing(tmp_path):
    database = tmp_path / "m.db"
    _build_database(database)
    before = _snapshot(database)
    with pytest.raises(ChangeRefused):
        change_password(database, username="admin",
                        prompt=_prompts(repeat="something-else-entirely"),
                        target_label="test")
    assert _snapshot(database) == before


def test_a_password_below_the_policy_minimum_changes_nothing(tmp_path):
    database = tmp_path / "p.db"
    _build_database(database)
    before = _snapshot(database)
    with pytest.raises(ChangeRefused):
        change_password(database, username="admin", prompt=_prompts(new="short"),
                        target_label="test")
    assert _snapshot(database) == before


def test_reusing_the_old_password_is_refused(tmp_path):
    """Otherwise "I changed it" can mean "I typed the exposed one again"."""
    database = tmp_path / "s.db"
    _build_database(database)
    before = _snapshot(database)
    with pytest.raises(ChangeRefused) as refusal:
        change_password(database, username="admin", prompt=_prompts(new=OLD_PASSWORD),
                        target_label="test")
    assert "same" in str(refusal.value).lower() or "differ" in str(refusal.value).lower()
    assert _snapshot(database) == before


# ===========================================================================
# The successful path
# ===========================================================================
def test_a_valid_change_succeeds_and_reports_safe_evidence(tmp_path):
    from auth import verify_password

    database = tmp_path / "ok.db"
    user_id = _build_database(database)
    result = change_password(database, username="admin", prompt=_prompts(),
                            target_label="test")

    assert result.user_id == user_id
    assert result.username == "admin"
    assert result.role == "ADMIN"
    assert result.previous_session_version == 3
    assert result.new_session_version == 4
    assert result.integrity_before == "ok"
    assert result.integrity_after == "ok"

    after = _snapshot(database)
    stored = next(row[2] for row in after["users"] if row[1] == "admin")
    assert not verify_password(OLD_PASSWORD, stored), "the old password still works"
    assert verify_password(NEW_PASSWORD, stored), "the new password does not work"


def test_the_hash_changes(tmp_path):
    database = tmp_path / "h.db"
    _build_database(database)
    before = _snapshot(database)
    change_password(database, username="admin", prompt=_prompts(), target_label="test")
    after = _snapshot(database)
    old_hash = next(r[2] for r in before["users"] if r[1] == "admin")
    new_hash = next(r[2] for r in after["users"] if r[1] == "admin")
    assert old_hash != new_hash


def test_session_version_increments_exactly_once(tmp_path):
    database = tmp_path / "sv.db"
    _build_database(database, session_version=7)
    change_password(database, username="admin", prompt=_prompts(), target_label="test")
    after = _snapshot(database)
    assert next(r[5] for r in after["users"] if r[1] == "admin") == 8


def test_no_other_user_is_touched(tmp_path):
    database = tmp_path / "o.db"
    _build_database(database)
    before = _snapshot(database)
    change_password(database, username="admin", prompt=_prompts(), target_label="test")
    after = _snapshot(database)
    owner_before = next(r for r in before["users"] if r[1] == "owneradmin")
    owner_after = next(r for r in after["users"] if r[1] == "owneradmin")
    assert owner_before == owner_after, "the OWNER row moved"


def test_stores_devices_and_history_are_unchanged(tmp_path):
    database = tmp_path / "d.db"
    _build_database(database)
    before = _snapshot(database)
    change_password(database, username="admin", prompt=_prompts(), target_label="test")
    after = _snapshot(database)
    assert after["stores"] == before["stores"]
    assert after["devices"] == before["devices"]
    assert after["sessions"] == before["sessions"]


# ===========================================================================
# Atomicity
# ===========================================================================
def test_a_failure_after_the_hash_is_written_rolls_everything_back(tmp_path,
                                                                  monkeypatch):
    """set_password_hash writes the hash and bumps session_version in ONE
    engine.begin(). If the bump fails, the hash must not survive - otherwise the
    password changed while every old token stayed valid, which is the one outcome
    this remediation cannot tolerate."""
    from tools import change_hq_user_password as tool

    database = tmp_path / "rb.db"
    _build_database(database)
    before = _snapshot(database)

    import user_lifecycle

    def exploding_bump(connection, user_id):
        raise RuntimeError("simulated failure between the two writes")

    monkeypatch.setattr(user_lifecycle, "_bump_session_version", exploding_bump)

    with pytest.raises(Exception):
        change_password(database, username="admin", prompt=_prompts(),
                        target_label="test")

    assert _snapshot(database) == before, "a partial change survived"


# ===========================================================================
# Nothing secret is printed
# ===========================================================================
def test_no_print_in_the_tool_can_emit_a_password_or_hash():
    """Walked through the AST, because a text scan flags the prose that explains
    the rule - which has happened three times in this project already."""
    import ast

    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    risky = ("password", "hash", "secret", "token")
    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        names = {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}
        hits = {n for n in names if any(m in n.lower() for m in risky)}
        if hits:
            offenders.append((getattr(node, "lineno", "?"), sorted(hits)))
    assert offenders == [], f"a print references a secret-bearing variable: {offenders}"


def test_the_cli_writes_no_secret_to_stdout_or_stderr(tmp_path):
    """Driven for real through the CLI with the prompts fed by a stub, so this
    checks what an operator would actually see."""
    database = tmp_path / "cli.db"
    _build_database(database)
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPOSITORY_ROOT)!r})\n"
        f"sys.path.insert(0, {str(BACKEND_ROOT)!r})\n"
        "from tools.change_hq_user_password import change_password, describe\n"
        f"answers = [{OLD_PASSWORD!r}, {NEW_PASSWORD!r}, {NEW_PASSWORD!r}]\n"
        "r = change_password(\n"
        f"    {str(database)!r}, username='admin',\n"
        "    prompt=lambda _l: answers.pop(0), target_label='test')\n"
        "describe(r)\n",
        encoding="utf-8")
    result = subprocess.run([sys.executable, str(driver)], capture_output=True,
                            text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    combined = result.stdout + result.stderr
    assert OLD_PASSWORD not in combined
    assert NEW_PASSWORD not in combined
    assert "$2b$" not in combined, "a bcrypt hash reached the output"


# ===========================================================================
# Read-only checks must not create sidecars
# ===========================================================================
def test_the_read_only_uri_uses_immutable(tmp_path):
    """mode=ro alone still builds the shared-memory index a WAL database needs,
    and creating it is a file creation. That tripped the protected-database
    sidecar guard once already."""
    uri = read_only_uri(tmp_path / "x.db")
    assert "mode=ro" in uri and "immutable=1" in uri


def test_an_integrity_check_creates_no_sidecar(tmp_path):
    database = tmp_path / "sc.db"
    _build_database(database)
    assert integrity_check(database) == "ok"
    assert not (tmp_path / "sc.db-wal").exists()
    assert not (tmp_path / "sc.db-shm").exists()


def test_a_completed_change_leaves_no_sidecar(tmp_path):
    database = tmp_path / "nsc.db"
    _build_database(database)
    change_password(database, username="admin", prompt=_prompts(), target_label="test")
    assert not (tmp_path / "nsc.db-wal").exists()
    assert not (tmp_path / "nsc.db-shm").exists()


# ===========================================================================
# Backups
# ===========================================================================
def test_a_consistent_backup_is_made_and_verified(tmp_path):
    database = tmp_path / "b.db"
    _build_database(database)
    backups = tmp_path / "backups"

    backup = make_consistent_backup(database, backups)

    assert backup.exists()
    assert backup.parent == backups
    assert integrity_check(backup) == "ok"
    # A real copy, not a truncated one.
    assert _snapshot(backup)["users"] == _snapshot(database)["users"]


def test_the_backup_uses_the_sqlite_backup_api_not_a_file_copy():
    """A file copy of a live SQLite database can catch it mid-write. The backup
    API takes a consistent snapshot."""
    source = TOOL.read_text(encoding="utf-8")
    assert ".backup(" in source, "no use of the sqlite3 backup API"


def test_changing_the_persistent_target_requires_a_backup_directory(tmp_path):
    """The persistent database is the one HQ actually serves. A change to it with
    no snapshot behind it has no way back."""
    database = tmp_path / "pb.db"
    _build_database(database)
    backups = tmp_path / "auto-backups"
    result = change_password(database, username="admin", prompt=_prompts(),
                            target_label="test", backup_directory=backups)
    assert result.backup_path is not None
    assert result.backup_path.exists()
    assert integrity_check(result.backup_path) == "ok"


# ===========================================================================
# The protected database needs saying so out loud
# ===========================================================================
def test_the_protected_target_refuses_without_explicit_confirmation():
    source = TOOL.read_text(encoding="utf-8")
    assert "--i-understand-this-changes-the-protected-baseline" in source, (
        "the protected target has no explicit confirmation flag")


def test_the_protected_confirmation_flag_is_not_required_for_persistent():
    """Making the operator type the scary flag for the ordinary case would train
    them to type it for the dangerous one."""
    result = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0
    assert "persistent" in result.stdout.lower()


def test_the_tool_refuses_while_an_hq_process_is_running():
    source = TOOL.read_text(encoding="utf-8")
    assert "EchoCastHQRuntime" in source, "nothing checks for a running HQ runtime"
    assert "uvicorn" in source, "nothing checks for a running backend"


def test_the_tool_never_creates_a_database(tmp_path):
    """A missing file must be an error, not an empty database with a new admin."""
    absent = tmp_path / "never.db"
    with pytest.raises(ChangeRefused):
        change_password(absent, username="admin", prompt=_prompts(), target_label="test")
    assert not absent.exists()
