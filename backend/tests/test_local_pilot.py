"""Tests for the local one-Store pilot readiness harness.

Every test uses pytest temporary directories and temporary SQLite files. None
of them open, copy, migrate, seed or modify ``backend/echocast_live.db``.

The harness exists to make EchoCast ready for *local software* pilot testing.
A passing run proves isolated startup, login, the canonical catalog, one
Receiver authentication, honest connection state and clean shutdown. It proves
nothing about live audio, FFmpeg output, amplifiers or audible speakers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from store_catalog import CANONICAL_STORES, CANONICAL_ZONES  # noqa: E402
from tools.local_pilot import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    DB_PATH_ENV,
    EXIT_OK,
    EXIT_SAFETY,
    PILOT_MARKER_NAME,
    RETIRED_DEMO_STORE_CODES,
    PilotCredentialError,
    PilotSafetyError,
    ProtectedPathError,
    main,
    prepare,
    require_pilot_password,
    reject_protected_database,
    reset_pilot_database,
    resolve_pilot_paths,
    smoke,
    validate_pilot_root,
)

PROTECTED_DATABASE = REPOSITORY_ROOT / "backend" / "echocast_live.db"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _protected_metadata():
    if not PROTECTED_DATABASE.exists():
        return None
    stat = PROTECTED_DATABASE.stat()
    return stat.st_size, stat.st_mtime_ns


@pytest.fixture()
def pilot_root(tmp_path):
    """An isolated pilot root well outside the repository."""
    return tmp_path / "EchoCast-AI" / "local-pilot"


@pytest.fixture()
def pilot_credentials(monkeypatch):
    monkeypatch.setenv(ADMIN_USERNAME_ENV, "pilot-operator")
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "pilot-only-temporary-passphrase")
    return {
        "username": "pilot-operator",
        "password": "pilot-only-temporary-passphrase",
    }


def _prepared(pilot_root, **kwargs):
    return prepare(resolve_pilot_paths(pilot_root), **kwargs)


# ---------------------------------------------------------------------------
# 1-5. Protected path and pilot-root safety
# ---------------------------------------------------------------------------
def test_exact_protected_database_path_is_refused():
    before = _protected_metadata()
    with pytest.raises(ProtectedPathError):
        reject_protected_database(PROTECTED_DATABASE)
    assert _protected_metadata() == before


def test_relative_and_dotdot_protected_paths_are_refused(monkeypatch):
    before = _protected_metadata()
    monkeypatch.chdir(PROTECTED_DATABASE.parent)
    with pytest.raises(ProtectedPathError):
        reject_protected_database(Path("echocast_live.db"))
    with pytest.raises(ProtectedPathError):
        reject_protected_database(
            PROTECTED_DATABASE.parent / ".." / "backend" / "echocast_live.db"
        )
    assert _protected_metadata() == before


def test_same_file_hard_link_to_protected_path_is_refused(tmp_path, monkeypatch):
    """Proven with an isolated stand-in; the real database is never linked."""
    stand_in = tmp_path / "stand-in.db"
    stand_in.write_bytes(b"not the real database")
    monkeypatch.setattr("tools.local_pilot.PROTECTED_DATABASE_PATH", stand_in)
    alias = tmp_path / "alias.db"
    try:
        os.link(stand_in, alias)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(ProtectedPathError):
        reject_protected_database(alias)


def test_pilot_root_inside_the_repository_is_refused():
    for candidate in (
        REPOSITORY_ROOT / "backend" / "pilot",
        REPOSITORY_ROOT / ".git" / "pilot",
        REPOSITORY_ROOT,
    ):
        with pytest.raises(PilotSafetyError):
            validate_pilot_root(candidate)


def test_pilot_root_outside_the_repository_is_accepted(pilot_root):
    validate_pilot_root(pilot_root)


def test_pilot_database_path_is_never_the_protected_filename_in_backend(tmp_path):
    paths = resolve_pilot_paths(tmp_path / "pilot")
    assert paths.database_path.name != "echocast_live.db"
    assert REPOSITORY_ROOT not in paths.database_path.parents


# ---------------------------------------------------------------------------
# 6-10. Reset safety
# ---------------------------------------------------------------------------
def test_reset_without_explicit_confirmation_removes_nothing(pilot_root, pilot_credentials):
    paths = resolve_pilot_paths(pilot_root)
    _prepared(pilot_root)
    assert paths.database_path.exists()

    removed = reset_pilot_database(paths, confirmed=False)

    assert removed == []
    assert paths.database_path.exists()


def test_reset_fails_closed_when_the_marker_is_absent(pilot_root, pilot_credentials):
    paths = resolve_pilot_paths(pilot_root)
    _prepared(pilot_root)
    paths.marker_path.unlink()

    with pytest.raises(PilotSafetyError):
        reset_pilot_database(paths, confirmed=True)
    assert paths.database_path.exists()


def test_reset_treats_database_wal_and_shm_as_one_scoped_set(pilot_root, pilot_credentials):
    paths = resolve_pilot_paths(pilot_root)
    _prepared(pilot_root)
    wal = Path(str(paths.database_path) + "-wal")
    shm = Path(str(paths.database_path) + "-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"")
    unrelated = paths.data_dir / "operator-notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    removed = reset_pilot_database(paths, confirmed=True)

    assert set(removed) == {paths.database_path, wal, shm}
    assert not paths.database_path.exists()
    assert not wal.exists()
    assert not shm.exists()
    # A scoped reset must never behave like a wildcard delete.
    assert unrelated.exists()
    assert paths.marker_path.exists()


def test_reset_refuses_a_database_path_outside_the_pilot_root(pilot_root, tmp_path, pilot_credentials):
    paths = resolve_pilot_paths(pilot_root)
    _prepared(pilot_root)
    outsider = tmp_path / "somewhere-else.db"
    outsider.write_bytes(b"not a pilot database")
    hijacked = paths.__class__(**{**paths.as_kwargs(), "database_path": outsider})

    with pytest.raises(PilotSafetyError):
        reset_pilot_database(hijacked, confirmed=True)
    assert outsider.exists()


def test_reset_never_targets_the_protected_database(pilot_root, pilot_credentials):
    before = _protected_metadata()
    paths = resolve_pilot_paths(pilot_root)
    _prepared(pilot_root)
    hijacked = paths.__class__(
        **{**paths.as_kwargs(), "database_path": PROTECTED_DATABASE}
    )

    with pytest.raises((PilotSafetyError, ProtectedPathError)):
        reset_pilot_database(hijacked, confirmed=True)
    assert _protected_metadata() == before


# ---------------------------------------------------------------------------
# 11-14. Credential boundary
# ---------------------------------------------------------------------------
def test_missing_pilot_password_produces_a_controlled_error(monkeypatch):
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    with pytest.raises(PilotCredentialError):
        require_pilot_password()


def test_blank_pilot_password_produces_a_controlled_error(monkeypatch):
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "   ")
    with pytest.raises(PilotCredentialError):
        require_pilot_password()


def test_credential_error_message_never_contains_the_password(monkeypatch):
    monkeypatch.setenv(ADMIN_PASSWORD_ENV, "")
    try:
        require_pilot_password()
    except PilotCredentialError as error:
        assert ADMIN_PASSWORD_ENV in str(error)
        assert "password=" not in str(error).lower()


# ---------------------------------------------------------------------------
# 15-25. Preparation of the isolated pilot database
# ---------------------------------------------------------------------------
def test_preparation_creates_an_isolated_database_outside_git(pilot_root, pilot_credentials):
    before = _protected_metadata()
    result = _prepared(pilot_root)
    paths = resolve_pilot_paths(pilot_root)

    assert paths.database_path.exists()
    assert paths.marker_path.exists()
    assert paths.marker_path.name == PILOT_MARKER_NAME
    assert REPOSITORY_ROOT not in paths.database_path.parents
    assert result["store_count"] == len(CANONICAL_STORES)
    assert _protected_metadata() == before


def test_prepared_database_holds_exactly_the_canonical_catalog(pilot_root, pilot_credentials):
    result = _prepared(pilot_root)
    paths = resolve_pilot_paths(pilot_root)

    connection = sqlite3.connect(paths.database_path)
    try:
        rows = connection.execute(
            "SELECT store_code, store_name, region FROM stores ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    assert len(rows) == 44 == len(CANONICAL_STORES)
    assert [row[0] for row in rows] == [e.short_name for e in CANONICAL_STORES]
    assert [row[1] for row in rows] == [e.full_name for e in CANONICAL_STORES]
    assert [row[2] for row in rows] == [e.zone for e in CANONICAL_STORES]
    assert len({row[0] for row in rows}) == len(rows)
    assert len({row[2] for row in rows}) == 9 == len(CANONICAL_ZONES)
    assert result["zone_count"] == 9


def test_prepared_database_contains_no_retired_demo_store_codes(pilot_root, pilot_credentials):
    _prepared(pilot_root)
    paths = resolve_pilot_paths(pilot_root)
    connection = sqlite3.connect(paths.database_path)
    try:
        codes = {
            row[0]
            for row in connection.execute("SELECT store_code FROM stores")
        }
    finally:
        connection.close()

    for retired in ("MUM-001", "DEL-001", "BLR-001", "ONL-001"):
        assert retired in RETIRED_DEMO_STORE_CODES
        assert retired not in codes
    assert not (codes & set(RETIRED_DEMO_STORE_CODES))


def test_preparation_requires_reconciliation_exact_match(pilot_root, pilot_credentials):
    result = _prepared(pilot_root)
    assert result["reconciliation"] == "EXACT_CANONICAL_MATCH"


def test_preparation_refuses_to_overwrite_an_existing_pilot_database(pilot_root, pilot_credentials):
    _prepared(pilot_root)
    paths = resolve_pilot_paths(pilot_root)
    first_hash = result_hash = None
    connection = sqlite3.connect(paths.database_path)
    try:
        first_hash = connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    finally:
        connection.close()

    # A second prepare must be a safe no-op rather than a silent recreate.
    second = _prepared(pilot_root)
    connection = sqlite3.connect(paths.database_path)
    try:
        result_hash = connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    finally:
        connection.close()

    assert second["already_prepared"] is True
    assert first_hash == result_hash == len(CANONICAL_STORES)


def test_prepared_state_file_contains_no_secret(pilot_root, pilot_credentials):
    _prepared(pilot_root)
    paths = resolve_pilot_paths(pilot_root)
    content = paths.state_path.read_text(encoding="utf-8")
    payload = json.loads(content)

    assert payload["store_count"] == len(CANONICAL_STORES)
    lowered = content.lower()
    for marker in ("password", "receiver_token", "authorization", "bearer", "jwt", "secret"):
        assert marker not in lowered
    assert "pilot-only-temporary-passphrase" not in content


def test_preparation_never_touches_the_protected_database(pilot_root, pilot_credentials):
    before = _protected_metadata()
    _prepared(pilot_root)
    assert _protected_metadata() == before
    assert not Path(str(PROTECTED_DATABASE) + "-wal").exists()
    assert not Path(str(PROTECTED_DATABASE) + "-shm").exists()


# ---------------------------------------------------------------------------
# 26-51. End-to-end smoke against the isolated pilot database
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def smoke_result(tmp_path_factory):
    """Run the real local pilot smoke once and reuse the result."""
    root = tmp_path_factory.mktemp("echocast-pilot") / "local-pilot"
    previous = {
        key: os.environ.get(key)
        for key in (ADMIN_USERNAME_ENV, ADMIN_PASSWORD_ENV, DB_PATH_ENV)
    }
    os.environ[ADMIN_USERNAME_ENV] = "pilot-operator"
    os.environ[ADMIN_PASSWORD_ENV] = "pilot-only-temporary-passphrase"
    os.environ.pop(DB_PATH_ENV, None)
    before = _protected_metadata()
    try:
        paths = resolve_pilot_paths(root)
        prepare(paths)
        result = smoke(paths)
        yield {"result": result, "paths": paths, "protected_before": before}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_smoke_binds_loopback_only_with_one_worker(smoke_result):
    result = smoke_result["result"]
    assert result["backend_host"] == "127.0.0.1"
    assert result["uvicorn_workers"] == 1
    assert result["backend_url"].startswith("http://127.0.0.1:")
    assert 1 <= result["backend_port"] <= 65535


def test_smoke_reports_liveness_and_login(smoke_result):
    result = smoke_result["result"]
    assert result["liveness"] == "ok"
    assert result["login"] == "ok"


def test_smoke_store_and_zone_counts_match_the_canonical_catalog(smoke_result):
    result = smoke_result["result"]
    assert result["store_count"] == 44
    assert result["zone_count"] == 9
    assert result["demo_codes_present"] == []


def test_smoke_authenticates_one_receiver_with_a_bearer_header(smoke_result):
    result = smoke_result["result"]
    assert result["receiver_auth"] == "ok"
    assert result["selected_store_code"] in {e.short_name for e in CANONICAL_STORES}
    # A query-string credential must still be refused by the production endpoint.
    assert result["query_token_refused"] is True


def test_smoke_reports_connected_without_inventing_readiness_or_playback(smoke_result):
    result = smoke_result["result"]
    assert result["observed_connection"] == "CONNECTED"
    # The pilot never sends a readiness, playback or acoustic acknowledgement,
    # so none of these may be claimed.
    assert result["observed_readiness"] == "NOT_REPORTED"
    assert result["observed_playback"] == "NOT_REPORTED"
    assert result["observed_acoustic"] == "NOT_REPORTED"
    assert result["speaker_verified"] is False


def test_smoke_cleans_up_the_receiver_and_shuts_down(smoke_result):
    result = smoke_result["result"]
    assert result["receiver_cleanup"] == "ok"
    assert result["shutdown"] == "ok"
    assert result["backend_process_running"] is False
    assert result["overall_result"] == "LOCAL_PILOT_SMOKE_PASSED"


def test_smoke_never_touched_the_protected_database(smoke_result):
    assert _protected_metadata() == smoke_result["protected_before"]


def test_smoke_report_and_logs_contain_no_secret(smoke_result):
    paths = smoke_result["paths"]
    report_text = paths.smoke_report_path.read_text(encoding="utf-8")
    payload = json.loads(report_text)

    assert payload["overall_result"] == "LOCAL_PILOT_SMOKE_PASSED"
    contents = [report_text, paths.state_path.read_text(encoding="utf-8")]
    for log_file in paths.logs_dir.glob("*.log"):
        contents.append(log_file.read_text(encoding="utf-8", errors="replace"))

    for content in contents:
        lowered = content.lower()
        assert "pilot-only-temporary-passphrase" not in content
        for marker in ("receiver_token", "authorization:", "bearer ", "password"):
            assert marker not in lowered

    # The selected Store's credential must never be persisted anywhere.
    connection = sqlite3.connect(paths.database_path)
    try:
        token = connection.execute(
            "SELECT receiver_token FROM stores WHERE store_code = ?",
            (payload["selected_store_code"],),
        ).fetchone()[0]
    finally:
        connection.close()
    for content in contents:
        assert token not in content


def test_smoke_report_records_honest_readiness_scope(smoke_result):
    payload = json.loads(
        smoke_result["paths"].smoke_report_path.read_text(encoding="utf-8")
    )
    assert payload["readiness_scope"] == "READY_FOR_LOCAL_SOFTWARE_PILOT_TEST"
    for forbidden in (
        "READY_FOR_PRODUCTION",
        "READY_FOR_LIVE_AUDIO",
        "READY_FOR_STORE_SPEAKERS",
        "SPEAKER_VERIFIED",
        "ALL_40_STORES_READY",
    ):
        assert forbidden not in json.dumps(payload)


# ---------------------------------------------------------------------------
# 55. CLI exit codes
# ---------------------------------------------------------------------------
def test_cli_refuses_a_pilot_root_inside_the_repository(capsys):
    before = _protected_metadata()
    exit_code = main(["prepare", "--pilot-root", str(REPOSITORY_ROOT / "backend")])
    assert exit_code == EXIT_SAFETY
    assert "refused" in capsys.readouterr().err.lower()
    assert _protected_metadata() == before


def test_cli_prepare_and_status_succeed(pilot_root, pilot_credentials, capsys):
    assert main(["prepare", "--pilot-root", str(pilot_root)]) == EXIT_OK
    prepare_output = capsys.readouterr().out
    assert "44" in prepare_output

    assert main(["status", "--pilot-root", str(pilot_root)]) == EXIT_OK
    status_output = capsys.readouterr().out
    assert "EXACT_CANONICAL_MATCH" in status_output
    assert "pilot-only-temporary-passphrase" not in status_output


def test_cli_prepare_without_password_fails_safely(pilot_root, monkeypatch, capsys):
    monkeypatch.delenv(ADMIN_PASSWORD_ENV, raising=False)
    assert main(["prepare", "--pilot-root", str(pilot_root)]) == EXIT_SAFETY
    assert ADMIN_PASSWORD_ENV in capsys.readouterr().err


def test_cli_reset_requires_the_explicit_flag(pilot_root, pilot_credentials, capsys):
    assert main(["prepare", "--pilot-root", str(pilot_root)]) == EXIT_OK
    capsys.readouterr()
    paths = resolve_pilot_paths(pilot_root)

    assert main(["reset", "--pilot-root", str(pilot_root)]) == EXIT_SAFETY
    assert paths.database_path.exists()

    assert (
        main(["reset", "--pilot-root", str(pilot_root), "--reset-pilot-db"]) == EXIT_OK
    )
    assert not paths.database_path.exists()
    assert paths.marker_path.exists()


# ---------------------------------------------------------------------------
# 54. The frontend must not duplicate the catalog
# ---------------------------------------------------------------------------
def test_frontend_contains_no_duplicated_store_catalog():
    frontend_src = REPOSITORY_ROOT / "frontend" / "src"
    canonical_names = {entry.full_name for entry in CANONICAL_STORES}
    for source in frontend_src.rglob("*.js*"):
        text = source.read_text(encoding="utf-8", errors="replace")
        matches = {name for name in canonical_names if name in text}
        assert not matches, f"{source} appears to duplicate catalog data: {matches}"
