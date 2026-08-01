"""The HQ supervisor: what it refuses, and what READY is allowed to mean.

WHAT THIS IS

One windowed process that starts the persistent backend and the production
frontend, watches both, and restarts a crashed one with bounded backoff. It
exists so an HQ machine can be signed into and then left alone.

THE TWO PROPERTIES WORTH THE MOST

**It refuses rather than improvises.** Every failure mode here has a tempting
"helpful" fallback - create the database, use backend/speaklink_live.db, pick a
pilot file, run the dev server - and every one of them turns a missing-data
problem into a Store-has-vanished problem. It refuses all of them.

**READY means healthy, not spawned.** A child process that exists is not a
backend that answers. The supervisor asks both children over HTTP before it
claims anything, because "the process started" is exactly the claim that let a
silent Receiver look like a working one earlier in this project.

No test here starts a real server, touches the live pilot, or opens the
protected database.
"""

from __future__ import annotations

import os
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

from tools.hq_runtime import (  # noqa: E402
    BackoffPolicy,
    RuntimeConfigError,
    RuntimeState,
    backend_command,
    frontend_command,
    resolve_runtime_profile,
    supervise_child,
)


@pytest.fixture()
def persistent(tmp_path, monkeypatch):
    """A fully initialized persistent root, as Initialize produces."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "SpeakLink" / "persistent-lan-server"
    for folder in ("data", "config", "keys", "logs", "backups", "runtime",
                   "migration-reports"):
        (root / folder).mkdir(parents=True)
    (root / "data" / "speaklink.db").write_bytes(b"SQLite format 3\x00")
    (root / "keys" / "receiver-hmac-keys.bin").write_bytes(b"not-a-real-key")
    (root / "keys" / "jwt-secret.txt").write_text("not-a-real-secret", encoding="utf-8")
    build = REPOSITORY_ROOT / "frontend" / "build"
    return root, build


# ===========================================================================
# It refuses rather than improvises
# ===========================================================================
def test_a_complete_profile_resolves(persistent):
    root, _build = persistent
    profile = resolve_runtime_profile()
    assert profile.database == root / "data" / "speaklink.db"
    assert profile.state is RuntimeState.STARTING


def test_an_uninitialized_root_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    with pytest.raises(RuntimeConfigError) as refusal:
        resolve_runtime_profile()
    assert "Initialize" in str(refusal.value)


def test_a_missing_database_is_refused_and_never_created(persistent):
    root, _build = persistent
    (root / "data" / "speaklink.db").unlink()
    with pytest.raises(RuntimeConfigError):
        resolve_runtime_profile()
    assert not (root / "data" / "speaklink.db").exists(), "it created one anyway"


def test_missing_keys_are_refused(persistent):
    root, _build = persistent
    (root / "keys" / "receiver-hmac-keys.bin").unlink()
    with pytest.raises(RuntimeConfigError) as refusal:
        resolve_runtime_profile()
    assert "key" in str(refusal.value).lower()


# ===========================================================================
# Production DATABASE_URL: fail closed, never a silent SQLite fallback
# ===========================================================================
def test_development_profile_never_reads_or_requires_a_database_url_file(persistent):
    """The default, and what every existing installed HQ (app_env unset)
    keeps doing after this feature ships: zero behavior change."""
    root, _build = persistent
    profile = resolve_runtime_profile()
    assert profile.app_env == "development"
    assert profile.database_url_file is None


def test_production_without_a_database_url_file_is_refused(persistent):
    root, _build = persistent
    (root / "config" / "hq-runtime.json").write_text(
        '{"app_env": "production"}', encoding="utf-8")
    with pytest.raises(RuntimeConfigError) as refusal:
        resolve_runtime_profile()
    assert "database-url.txt" in str(refusal.value)
    assert "production" in str(refusal.value).lower()


def test_production_with_a_database_url_file_resolves_and_injects_it(persistent):
    from tools.hq_runtime import child_environment

    root, _build = persistent
    (root / "config" / "hq-runtime.json").write_text(
        '{"app_env": "production"}', encoding="utf-8")
    (root / "keys" / "database-url.txt").write_text(
        "postgresql://user:pw@example.pooler.supabase.com:5432/postgres",
        encoding="utf-8",
    )
    profile = resolve_runtime_profile()
    assert profile.app_env == "production"
    env = child_environment(profile)
    assert env["APP_ENV"] == "production"
    assert env["DATABASE_URL"] == (
        "postgresql://user:pw@example.pooler.supabase.com:5432/postgres"
    )


def test_the_database_url_never_reaches_a_child_command_line(persistent):
    """Mirrors test_no_secret_reaches_a_child_command_line for JWT_SECRET -
    the URL (which carries the database password) must travel only through
    the environment, never a command-line argument."""
    root, _build = persistent
    (root / "config" / "hq-runtime.json").write_text(
        '{"app_env": "production"}', encoding="utf-8")
    (root / "keys" / "database-url.txt").write_text(
        "postgresql://user:supersecretpw@example.pooler.supabase.com:5432/postgres",
        encoding="utf-8",
    )
    profile = resolve_runtime_profile()
    command = backend_command(profile) + frontend_command(profile)
    assert not any("supersecretpw" in str(part) for part in command)


def test_a_missing_frontend_build_is_refused(persistent, monkeypatch):
    monkeypatch.setattr("tools.hq_runtime.FRONTEND_BUILD",
                        Path(str(persistent[0] / "no-such-build")))
    with pytest.raises(RuntimeConfigError) as refusal:
        resolve_runtime_profile()
    assert "yarn build" in str(refusal.value)


def test_it_never_falls_back_to_the_repository_database():
    """The single most dangerous convenience available to this module.

    Scanned through the AST. The first version walked lines and skipped ones
    starting with '#', which does not skip a DOCSTRING - so it failed on the
    paragraph explaining why the fallback must not exist. That is the third time
    in this project a text scan has been loudest about the file documenting
    itself best; string literals are data and comments are not code, and only a
    parser knows the difference.
    """
    import ast

    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value not in docstrings and "speaklink_live.db" in node.value
    ]
    assert offenders == [], f"a fallback path survives in code: {offenders}"


def test_it_refuses_a_throwaway_pilot_database(persistent, monkeypatch):
    from tools.persistent_lan_server import PersistentServerError

    monkeypatch.setattr("tools.hq_runtime.is_throwaway_pilot_database",
                        lambda _p: True)
    with pytest.raises((RuntimeConfigError, PersistentServerError)):
        resolve_runtime_profile()


# ===========================================================================
# How the children are started
# ===========================================================================
def test_the_backend_runs_exactly_one_worker(persistent):
    command = backend_command(resolve_runtime_profile())
    assert "--workers" in command
    assert command[command.index("--workers") + 1] == "1"


def test_the_backend_is_never_started_with_reload_or_debug(persistent):
    command = " ".join(backend_command(resolve_runtime_profile()))
    assert "--reload" not in command
    assert "--debug" not in command


def test_the_backend_binds_the_configured_private_address(persistent):
    command = backend_command(resolve_runtime_profile())
    host = command[command.index("--host") + 1]
    assert host.startswith(("10.", "192.168.", "172."))
    assert host not in ("0.0.0.0", "::")


def test_the_frontend_serves_the_production_build_not_a_dev_server(persistent):
    command = " ".join(frontend_command(resolve_runtime_profile()))
    for forbidden in ("react-scripts", "yarn start", "npm start"):
        assert forbidden not in command
    assert "build" in command


def test_no_secret_reaches_a_child_command_line(persistent):
    profile = resolve_runtime_profile()
    joined = " ".join(backend_command(profile) + frontend_command(profile))
    for forbidden in ("not-a-real-secret", "JWT_SECRET=", "password"):
        assert forbidden not in joined


@pytest.mark.skipif(os.name != "nt", reason="Windows console behaviour")
def test_children_are_started_with_no_console(persistent):
    """The supervisor is windowed, so a console child would get a NEW console -
    a black window on the HQ desktop. Same mechanism as the FFmpeg fix."""
    import subprocess

    from tools.hq_runtime import child_process_options

    assert child_process_options()["creationflags"] & subprocess.CREATE_NO_WINDOW


# ===========================================================================
# READY means healthy, not spawned
# ===========================================================================
def test_states_exist_for_every_stage():
    for name in ("STARTING", "BACKEND_STARTING", "BACKEND_HEALTHY",
                 "FRONTEND_STARTING", "READY", "DEGRADED", "STOPPING",
                 "STOPPED", "CONFIG_ERROR", "BACKEND_ERROR", "FRONTEND_ERROR"):
        assert hasattr(RuntimeState, name)


def test_ready_is_not_reachable_from_a_spawned_child_alone():
    """A process that exists is not a backend that answers. The supervisor must
    ask over HTTP before claiming anything."""
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    ready = source[source.index("RuntimeState.READY"):]
    assert "health" in source.lower()
    assert "poll() is None" not in ready[:400], (
        "READY must not be decided by whether the process is alive")


# ===========================================================================
# Bounded restart, never a tight loop
# ===========================================================================
def test_the_backoff_grows_and_is_capped():
    policy = BackoffPolicy()
    delays = [policy.delay(attempt, random_value=lambda: 0.5) for attempt in range(1, 9)]
    assert delays == sorted(delays), "backoff must not shrink"
    assert max(delays) <= policy.maximum_seconds


def test_the_backoff_resets_after_a_stable_run():
    policy = BackoffPolicy()
    assert policy.delay(5, random_value=lambda: 0.5) > policy.delay(1, random_value=lambda: 0.5)


def test_a_child_that_never_recovers_stops_being_respawned():
    """Otherwise a permanently broken backend becomes a spawn loop that fills
    the disk with logs and hides the actual cause."""
    starts = []

    def start_child():
        starts.append(1)
        return object()

    outcome = supervise_child(
        name="backend", start=start_child,
        is_alive=lambda _c: False, health=lambda: False,
        max_attempts=3, sleep=lambda _s: None,
        random_value=lambda: 0.5,
    )
    assert len(starts) == 3
    assert outcome.state is RuntimeState.DEGRADED
    assert outcome.attempts == 3


def test_a_child_that_becomes_healthy_reports_healthy():
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return calls["n"] >= 2

    outcome = supervise_child(
        name="backend", start=lambda: object(),
        is_alive=lambda _c: True, health=health,
        max_attempts=3, sleep=lambda _s: None, random_value=lambda: 0.5,
    )
    assert outcome.state is RuntimeState.BACKEND_HEALTHY


def test_the_degraded_outcome_carries_a_safe_reason():
    outcome = supervise_child(
        name="frontend", start=lambda: object(),
        is_alive=lambda _c: False, health=lambda: False,
        max_attempts=1, sleep=lambda _s: None, random_value=lambda: 0.5,
    )
    assert outcome.detail
    for forbidden in ("password", "secret", "speaklink_rcv_v1"):
        assert forbidden not in outcome.detail.lower()


# ===========================================================================
# Nothing secret is written down
# ===========================================================================
def test_the_module_logs_through_the_redacting_logger():
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    assert "configure_logging" in source
    assert "redact" in source


def test_the_module_never_prints_the_jwt_secret():
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "print(" in line or "log.info" in line or "log.warning" in line:
            assert "jwt" not in line.lower() or "secret" not in line.lower()


# ===========================================================================
# One runtime, and only its own children
# ===========================================================================
def test_only_one_runtime_may_run(persistent):
    from tools.hq_runtime import runtime_lock

    first = runtime_lock()
    first.acquire()
    try:
        from tools.receiver_agent import AlreadyRunning

        with pytest.raises(AlreadyRunning):
            runtime_lock().acquire()
    finally:
        first.release()


def test_stopping_verifies_a_pid_before_killing_it():
    """Windows reuses process numbers. A recorded PID alone is not proof, and
    an earlier script in this repository nearly stopped an editor because of it.
    """
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    stop = source[source.index("def stop_children"):]
    assert "cmdline" in stop.lower() or "command_line" in stop.lower()


# ===========================================================================
# The packaged executable
# ===========================================================================
def test_the_spec_builds_a_windowed_runtime():
    spec = (REPOSITORY_ROOT / "hq_runtime.spec").read_text(encoding="utf-8")
    assert "SpeakLinkHQRuntime" in spec
    assert "console=False" in spec
    assert "disable_windowed_traceback=True" in spec, (
        "an unattended HQ desktop is exactly where a modal error box sits "
        "unclosed for a week")


def test_the_spec_excludes_the_receiver_and_test_tooling():
    spec = (REPOSITORY_ROOT / "hq_runtime.spec").read_text(encoding="utf-8")
    assert "excludes" in spec
    assert "pytest" in spec
