"""The HQ runtime's entry point: what happens when Windows actually starts it.

WHY THIS FILE EXISTS AT ALL

``tools/hq_runtime.py`` was built into ``SpeakLinkHQRuntime.exe`` and the
executable was verified WINDOWS_GUI - correctly, and that verification still
holds. But the module defined a supervisor and never called it. Run it and it
imports, defines, and exits 0.

That is the worst available failure shape for this project, because Task
Scheduler records exit 0 as "the task ran successfully". A green Scheduled Task
history, no window, no error, no HQ. It is the same mistake as a Receiver
process that exists but never plays: the evidence looks like success because
nobody asked the running thing a question.

So these tests ask the entry point questions: does it refuse when it should,
what exit code does it hand back, does it write down what state it is in, and
does it hold the single-instance lock while it runs.

Nothing here starts a real server, opens a real port, or touches the protected
database.
"""

from __future__ import annotations

import json
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

from tools import hq_runtime  # noqa: E402
from tools.hq_runtime import (  # noqa: E402
    EXIT_ALREADY_RUNNING,
    EXIT_CONFIG_ERROR,
    EXIT_DEGRADED,
    EXIT_OK,
    HQRuntime,
    RuntimeState,
    main,
    read_status,
    write_status,
)


@pytest.fixture()
def persistent(tmp_path, monkeypatch):
    """An initialized persistent root, exactly as Initialize leaves one."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root = tmp_path / "SpeakLink" / "persistent-lan-server"
    for folder in ("data", "config", "keys", "logs", "backups", "runtime",
                   "migration-reports"):
        (root / folder).mkdir(parents=True)
    (root / "data" / "speaklink.db").write_bytes(b"SQLite format 3\x00")
    (root / "keys" / "receiver-hmac-keys.bin").write_bytes(b"not-a-real-key")
    (root / "keys" / "jwt-secret.txt").write_text("not-a-real-secret", encoding="utf-8")
    return root


class FakeChild:
    """A child process that is alive until told otherwise."""

    def __init__(self, args, alive=True):
        self.args = list(args)
        self.pid = 4242
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 1

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


# ===========================================================================
# There is an entry point, and it is the one PyInstaller builds
# ===========================================================================
def test_the_module_has_a_main_and_calls_it_when_run():
    """The defect this file was written for: a spec that builds a module which
    defines a supervisor and never starts one."""
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    assert "def main(" in source
    assert '__name__ == "__main__"' in source
    assert "sys.exit(main())" in source or "raise SystemExit(main())" in source


def test_check_mode_resolves_the_profile_and_exits_zero(persistent, monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    assert main(["--check"]) == EXIT_OK


def test_check_mode_does_not_start_anything(persistent, monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    started = []
    monkeypatch.setattr(hq_runtime, "spawn_child",
                        lambda *a, **k: started.append(a) or FakeChild(["x"]))
    main(["--check"])
    assert started == [], "a health check must not start a server"


def test_an_unusable_profile_exits_with_the_config_code(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert main(["--check"]) == EXIT_CONFIG_ERROR


def test_a_refusal_is_written_down_where_a_windowed_process_can_be_read(tmp_path,
                                                                       monkeypatch):
    """A GUI-subsystem process has nowhere to print. If the refusal is not on
    disk, the operator sees a task that 'ran successfully' and an HQ that is
    not there."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    main(["--check"])
    written = list(tmp_path.rglob("hq-runtime-status.json"))
    assert written, "nothing recorded why it refused"
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["state"] == RuntimeState.CONFIG_ERROR.value
    assert payload["detail"]


# ===========================================================================
# The status file is the evidence, so it must never become the leak
# ===========================================================================
def test_the_status_file_records_state_and_no_secret(tmp_path):
    status = tmp_path / "hq-runtime-status.json"
    write_status(status, RuntimeState.READY,
                 detail="backend healthy; token speaklink_rcv_v1.abc.def")
    payload = read_status(status)
    assert payload["state"] == "READY"
    assert "speaklink_rcv_v1" not in json.dumps(payload)


def test_the_status_file_is_replaced_whole_not_appended(tmp_path):
    status = tmp_path / "hq-runtime-status.json"
    write_status(status, RuntimeState.STARTING)
    write_status(status, RuntimeState.READY)
    assert read_status(status)["state"] == "READY"
    assert status.read_text(encoding="utf-8").count('"state"') == 1


def test_reading_a_missing_status_is_unknown_not_an_error(tmp_path):
    assert read_status(tmp_path / "nothing.json") == {}


# ===========================================================================
# READY is reached only when both children answer
# ===========================================================================
def _fake_build(root: Path) -> Path:
    build = root / "fake-frontend-build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return build


def _runtime(persistent, monkeypatch, *, backend_ok=True, frontend_ok=True,
             alive=True):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    profile = hq_runtime.resolve_runtime_profile()
    answers = {profile.backend_health_url: backend_ok,
               profile.frontend_health_url: frontend_ok}
    return HQRuntime(
        profile,
        log=None,
        spawn=lambda command, env: FakeChild(command, alive=alive),
        http=lambda url, timeout=None: answers.get(url, False),
        sleep=lambda _s: None,
        random_value=lambda: 0.5,
        max_attempts=2,
    )


def test_both_children_healthy_is_ready(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch)
    assert runtime.start() is RuntimeState.READY


def test_a_backend_that_never_answers_is_not_ready(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch, backend_ok=False)
    assert runtime.start() is RuntimeState.DEGRADED


def test_a_frontend_that_never_answers_is_not_ready(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch, frontend_ok=False)
    assert runtime.start() is RuntimeState.DEGRADED


def test_a_spawned_but_silent_backend_is_not_ready(persistent, monkeypatch):
    """The whole point. The process exists; it answers nothing."""
    runtime = _runtime(persistent, monkeypatch, backend_ok=False, alive=True)
    assert runtime.start() is not RuntimeState.READY


def test_the_frontend_is_not_started_before_the_backend_is_healthy(persistent,
                                                                  monkeypatch):
    """Otherwise the browser loads a page that cannot log anybody in, which
    reads to a Store manager as 'HQ is broken' rather than 'HQ is starting'."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    profile = hq_runtime.resolve_runtime_profile()
    order = []

    def spawn(command, env):
        order.append("frontend" if "http.server" in command else "backend")
        return FakeChild(command)

    runtime = HQRuntime(profile, log=None, spawn=spawn,
                        http=lambda url, timeout=None: url == profile.frontend_health_url,
                        sleep=lambda _s: None, random_value=lambda: 0.5,
                        max_attempts=2)
    runtime.start()
    assert "frontend" not in order, "the frontend started while the backend was silent"


def test_ready_writes_the_status_file(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch)
    runtime.start()
    assert read_status(runtime.profile.status_file)["state"] == "READY"


def test_degraded_writes_a_safe_reason(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch, backend_ok=False)
    runtime.start()
    payload = read_status(runtime.profile.status_file)
    assert payload["state"] == "DEGRADED"
    for forbidden in ("not-a-real-secret", "password", "speaklink_rcv_v1"):
        assert forbidden not in json.dumps(payload).lower()


# ===========================================================================
# Watching, and giving up
# ===========================================================================
def test_a_child_that_dies_is_restarted(persistent, monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    profile = hq_runtime.resolve_runtime_profile()
    spawned = []

    def spawn(command, env):
        child = FakeChild(command)
        spawned.append(child)
        return child

    runtime = HQRuntime(profile, log=None, spawn=spawn,
                        http=lambda url, timeout=None: True,
                        sleep=lambda _s: None, random_value=lambda: 0.5,
                        max_attempts=2)
    assert runtime.start() is RuntimeState.READY
    started = len(spawned)
    spawned[0].terminate()          # the backend falls over
    runtime.watch_once()
    assert len(spawned) > started, "a dead child was never restarted"


def test_watching_stops_restarting_after_the_bound(persistent, monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    profile = hq_runtime.resolve_runtime_profile()
    spawned = []

    runtime = HQRuntime(profile, log=None,
                        spawn=lambda c, env: spawned.append(1) or FakeChild(c, alive=False),
                        http=lambda url, timeout=None: False,
                        sleep=lambda _s: None, random_value=lambda: 0.5,
                        max_attempts=2)
    runtime.run(iterations=6)
    assert len(spawned) <= 12, f"spawn loop: {len(spawned)} starts"
    assert read_status(profile.status_file)["state"] == "DEGRADED"


def test_stopping_terminates_the_children_it_started(persistent, monkeypatch):
    runtime = _runtime(persistent, monkeypatch)
    runtime.start()
    assert runtime.stop() == 2
    assert read_status(runtime.profile.status_file)["state"] == "STOPPED"


# ===========================================================================
# One runtime per machine
# ===========================================================================
def test_a_second_runtime_exits_with_the_already_running_code(persistent,
                                                              monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    monkeypatch.setattr(hq_runtime, "spawn_child",
                        lambda command, env: FakeChild(command))
    monkeypatch.setattr(hq_runtime, "http_ok", lambda url, timeout=None: True)

    held = hq_runtime.runtime_lock()
    held.acquire()
    try:
        assert main(["--iterations", "1"]) == EXIT_ALREADY_RUNNING
    finally:
        held.release()


def test_a_degraded_run_exits_non_zero(persistent, monkeypatch):
    """Task Scheduler reads the exit code. A supervisor that gives up must not
    report success, or the task history says the HQ machine is fine."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    monkeypatch.setattr(hq_runtime, "spawn_child",
                        lambda command, env: FakeChild(command, alive=False))
    monkeypatch.setattr(hq_runtime, "http_ok", lambda url, timeout=None: False)
    monkeypatch.setattr(hq_runtime.time, "sleep", lambda _s: None)
    assert main(["--iterations", "1"]) == EXIT_DEGRADED


# ===========================================================================
# A refusal nobody can satisfy is a defect, not caution
# ===========================================================================
# FOUND BY RUNNING THE PACKAGED EXECUTABLE, NOT BY THE UNIT SUITE.
#
# SpeakLinkHQRuntime.exe --check against the real initialized persistent root
# returned 2 and wrote:
#
#   "The Receiver HMAC key container is missing ... Restore it before starting."
#
# It is missing because nothing creates it up front. Initialize makes the
# folders; the BACKEND mints the key container on first start, and
# Start-SpeakLinkPersistentLanServer.ps1 mints the signing secret. So the
# runtime refused a profile that was correctly initialized, with an instruction
# ("restore it") that no procedure in this repository can carry out.
#
# The fix is not to delete the check. A key container that vanishes from a
# server with Stores enrolled is a real emergency: mint a new one and every
# Device credential stops verifying, silently, and all 44 Stores need
# re-enrolling. The difference between the two situations is evidence that is
# already on disk - whether any Device is enrolled - so the runtime asks
# instead of guessing.
def _database_with_devices(root: Path, count: int) -> None:
    import sqlite3

    database = root / "data" / "speaklink.db"
    database.unlink()  # the fixture leaves a header, not a real database
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE receiver_devices (id INTEGER PRIMARY KEY)")
        for _ in range(count):
            connection.execute("INSERT INTO receiver_devices DEFAULT VALUES")
        connection.commit()
    finally:
        connection.close()


def test_a_fresh_profile_with_no_devices_may_start_without_a_key_container(
        persistent, monkeypatch):
    """First start of a brand-new HQ. The backend mints the container itself."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    (persistent / "keys" / "receiver-hmac-keys.bin").unlink()
    _database_with_devices(persistent, 0)
    profile = hq_runtime.resolve_runtime_profile()
    assert profile.database.exists()


def test_a_profile_with_enrolled_devices_and_no_key_container_is_refused(
        persistent, monkeypatch):
    """The emergency. Minting a new container here silently breaks every
    enrolled Store, and the Stores would look enrolled the whole time."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    (persistent / "keys" / "receiver-hmac-keys.bin").unlink()
    _database_with_devices(persistent, 3)
    with pytest.raises(hq_runtime.RuntimeConfigError) as refusal:
        hq_runtime.resolve_runtime_profile()
    message = str(refusal.value)
    assert "3" in message, "it must say how many Stores are at risk"
    assert "re-enrol" in message.lower() or "re-enroll" in message.lower()


def test_an_unreadable_database_is_refused_rather_than_assumed_empty(
        persistent, monkeypatch):
    """'I could not count the Devices' must never become 'there are none'."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    (persistent / "keys" / "receiver-hmac-keys.bin").unlink()
    (persistent / "data" / "speaklink.db").write_bytes(b"this is not a database")
    with pytest.raises(hq_runtime.RuntimeConfigError):
        hq_runtime.resolve_runtime_profile()


def test_a_missing_signing_secret_is_created_not_refused(persistent, monkeypatch):
    """Unlike the HMAC container, a new signing secret costs a sign-in, not a
    re-enrolment of 44 Stores. Start-SpeakLinkPersistentLanServer.ps1 already
    mints it exactly this way; this is the same behaviour, not a new one."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    secret_file = persistent / "keys" / "jwt-secret.txt"
    secret_file.unlink()
    profile = hq_runtime.resolve_runtime_profile()
    assert profile.jwt_secret_file.exists()
    assert len(profile.jwt_secret_file.read_text(encoding="utf-8").strip()) >= 32


def test_an_existing_signing_secret_is_reused_never_replaced(persistent, monkeypatch):
    """Replacing it would sign every user out on every restart."""
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    secret_file = persistent / "keys" / "jwt-secret.txt"
    before = secret_file.read_text(encoding="utf-8")
    hq_runtime.resolve_runtime_profile()
    assert secret_file.read_text(encoding="utf-8") == before


def test_the_created_secret_is_never_written_to_the_status_file(persistent,
                                                               monkeypatch):
    monkeypatch.setattr(hq_runtime, "FRONTEND_BUILD", _fake_build(persistent))
    (persistent / "keys" / "jwt-secret.txt").unlink()
    profile = hq_runtime.resolve_runtime_profile()
    secret = profile.jwt_secret_file.read_text(encoding="utf-8").strip()
    write_status(profile.status_file, RuntimeState.READY, detail="started")
    assert secret not in profile.status_file.read_text(encoding="utf-8")


# ===========================================================================
# Where things are, once it is an executable rather than a checkout
# ===========================================================================
# ALSO FOUND BY RUNNING THE PACKAGED EXECUTABLE. The module derives its paths
# from ``Path(__file__).parents[1]``, which inside a PyInstaller bundle is the
# unpacked bundle - so the frozen runtime looked for the React build at
# <dist>\SpeakLinkHQRuntime\frontend\build and refused. Correct refusal, wrong
# place to look. A packaged runtime ships its frontend and backend beside
# itself; a checkout has them in the repository.
def test_the_packaged_layout_looks_beside_the_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "SpeakLinkHQRuntime.exe"))
    assert hq_runtime.default_frontend_build() == tmp_path / "frontend"
    assert hq_runtime.default_backend_root() == tmp_path / "backend"


def test_the_checkout_layout_uses_the_repository(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert hq_runtime.default_frontend_build() == REPOSITORY_ROOT / "frontend" / "build"
    assert hq_runtime.default_backend_root() == REPOSITORY_ROOT / "backend"


def test_the_installer_and_the_runtime_agree_on_where_the_frontend_lives():
    """They disagreed. The installer checked package\\frontend\\index.html and
    the runtime looked for frontend\\build\\index.html, so a package that
    installed cleanly could not start."""
    installer = (REPOSITORY_ROOT / "scripts" /
                 "Install-SpeakLinkHQAutoStart.ps1").read_text(encoding="utf-8")
    assert "frontend\\index.html" in installer
    assert "frontend\\build\\index.html" not in installer


# ===========================================================================
# Arguments carry nothing secret
# ===========================================================================
def test_the_command_line_accepts_no_secret_option():
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    entry = source[source.index("def main("):]
    for forbidden in ("--password", "--jwt-secret", "--token", "--secret"):
        assert forbidden not in entry


def test_help_renders(persistent, monkeypatch, capsys):
    """A literal %LOCALAPPDATA% in an argparse help string once crashed --help
    with 'unsupported format character'. It was found by the packaged-EXE check,
    not by the unit suite, which is a slower way to find it than this."""
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "SpeakLinkHQRuntime" in capsys.readouterr().out
