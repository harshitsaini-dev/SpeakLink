"""The repo-native launcher's contract, and the promises the README makes.

WHAT THIS CAN AND CANNOT PROVE

It proves the STRUCTURE: that paths resolve from the repository rather than
from AppData or the working directory, that the fail-closed bootstrap rule is
real, that stop can only target a process this repository started, and that
every Windows-specific call is behind a platform guard.

It does NOT prove Linux production. Nothing here has run on Linux, and a test
that asserts "this code looks portable" must not be read as "this has been
accepted on POSIX". The honest claim is structural portability; the clean-room
and move/copy runs that back it were performed on Windows.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPOSITORY_ROOT / "tools"
for candidate in (REPOSITORY_ROOT, TOOLS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

LAUNCHER = TOOLS / "speaklink_server.py"
SOURCE = LAUNCHER.read_text(encoding="utf-8")


@pytest.fixture()
def launcher():
    import importlib
    module = importlib.import_module("speaklink_server")
    return importlib.reload(module)


# ===========================================================================
# Layout
# ===========================================================================
def test_the_repository_root_is_found_from_the_file_not_the_cwd(launcher):
    """An operator double-clicking start.bat from Explorer, and a shortcut with
    some other working directory, must both find the same repository."""
    assert launcher.REPO_ROOT == REPOSITORY_ROOT
    assert "parents[1]" in SOURCE


def test_data_lives_inside_the_repository_by_default(launcher):
    assert launcher.data_dir() == REPOSITORY_ROOT / "data"
    assert launcher.database_path() == REPOSITORY_ROOT / "data" / "speaklink.db"
    assert launcher.keys_dir() == REPOSITORY_ROOT / "data" / "keys"
    assert launcher.logs_dir() == REPOSITORY_ROOT / "data" / "logs"
    assert launcher.runtime_dir() == REPOSITORY_ROOT / "data" / "runtime"


def test_the_data_directory_can_be_relocated(launcher, tmp_path, monkeypatch):
    """So data can live on a separate or backed-up volume."""
    monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path / "elsewhere"))
    assert launcher.data_dir() == (tmp_path / "elsewhere").resolve()


def test_nothing_resolves_through_appdata(launcher):
    """The whole point of the change.

    Checked as USE rather than as the word: the docstrings deliberately
    explain what this replaced, and a test that forbids mentioning AppData
    would forbid explaining why AppData is gone. What must not exist is a
    lookup of it - that is what would put data back there.
    """
    import ast
    tree = ast.parse(SOURCE)
    lookups = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.upper() in ("LOCALAPPDATA", "APPDATA"):
                lookups.append(node.value)
    assert not lookups, f"the launcher looks up {lookups}"

    # And the resolved data directory is genuinely under the repository.
    assert launcher.REPO_ROOT in launcher.data_dir().parents or \
        launcher.data_dir() == launcher.REPO_ROOT / "data"


def test_hq_registers_no_scheduled_task(launcher):
    for forbidden in ("schtasks", "Register-ScheduledTask", "ScheduledTask"):
        assert forbidden not in SOURCE, (
            f"{forbidden} appears - HQ must not depend on Task Scheduler")


def test_no_developer_path_or_lan_address_is_hardcoded(launcher):
    for forbidden in ("C:\\Users\\", "192.168.4.134", "Desktop\\SpeakLink"):
        assert forbidden not in SOURCE, f"{forbidden} is hardcoded"


# ===========================================================================
# Configuration and the bootstrap rule
# ===========================================================================
def test_environment_variables_win_over_the_env_file(launcher, monkeypatch):
    """A container or CI secret must not be overridden by a stale .env."""
    monkeypatch.setenv("APP_PORT", "9999")
    config = launcher.Config(launcher.load_environment())
    assert config.port == 9999


def test_a_fresh_database_refuses_without_bootstrap_credentials(launcher):
    config = launcher.Config({"JWT_SECRET": "x"})
    with pytest.raises(launcher.LaunchError) as refusal:
        launcher.require_bootstrap_values(config, database_exists=False)
    message = str(refusal.value)
    assert "ADMIN_USERNAME" in message and "ADMIN_PASSWORD" in message
    # It tells the operator what to do, and promises no default account.
    assert ".env" in message
    assert "No default account" in message


def test_a_missing_signing_secret_refuses_even_on_an_existing_database(launcher):
    config = launcher.Config({"ADMIN_USERNAME": "a", "ADMIN_PASSWORD": "b"})
    with pytest.raises(launcher.LaunchError):
        launcher.require_bootstrap_values(config, database_exists=True)


def test_admin_credentials_are_not_required_once_the_database_exists(launcher):
    """The rule that protects a running estate: editing ADMIN_PASSWORD must not
    be able to reset a live account, so it is not even read."""
    config = launcher.Config({"JWT_SECRET": "x"})
    launcher.require_bootstrap_values(config, database_exists=True)   # no raise


def test_no_default_credential_appears_anywhere_in_the_launcher(launcher):
    lowered = SOURCE.lower()
    for forbidden in ("admin/admin", "superadmin", "password123", "changeme",
                      '"admin"', "'admin'"):
        assert forbidden not in lowered, f"{forbidden} appears in the launcher"


# ===========================================================================
# The child process
# ===========================================================================
def test_exactly_one_uvicorn_worker(launcher):
    config = launcher.Config({"APP_HOST": "127.0.0.1", "APP_PORT": "8000"})
    command = launcher.uvicorn_command(Path("python"), config)
    assert "--workers" in command
    assert command[command.index("--workers") + 1] == "1", (
        "BroadcastRuntime and the WebSocket registry are process-local - a "
        "second worker would be a second invisible copy of every broadcast")


def test_the_child_is_told_where_the_repo_local_data_is(launcher):
    config = launcher.Config({"JWT_SECRET": "x"})
    env = launcher.child_environment(config)
    assert env[launcher.DB_PATH_ENV] == str(launcher.database_path())
    assert env[launcher.KEY_CONTAINER_ENV].startswith(str(launcher.keys_dir()))
    assert env[launcher.FRONTEND_BUILD_ENV] == str(launcher.frontend_build_dir())


def test_no_secret_is_passed_on_the_command_line(launcher):
    config = launcher.Config({"APP_HOST": "127.0.0.1", "APP_PORT": "8000",
                              "JWT_SECRET": "a-secret-value",
                              "ADMIN_PASSWORD": "a-password-value"})
    command = " ".join(launcher.uvicorn_command(Path("python"), config))
    # A command line is visible in the process list to every user on the box.
    assert "a-secret-value" not in command
    assert "a-password-value" not in command


def test_the_health_url_never_tries_to_connect_to_a_bind_address(launcher):
    """0.0.0.0 means 'every interface'; connecting to it is not defined."""
    config = launcher.Config({"APP_HOST": "0.0.0.0", "APP_PORT": "8000"})
    assert "0.0.0.0" not in config.health_url
    assert "127.0.0.1" in config.health_url


# ===========================================================================
# Stop safety
# ===========================================================================
def test_stop_never_searches_for_processes_to_kill(launcher):
    """The failure this avoids: stopping "whatever is on port 8000", or every
    python.exe, and taking an unrelated process with it."""
    # /IM kills BY IMAGE NAME - every python.exe on the machine. /PID with a
    # pid this launcher recorded and validated is the safe form, and is what
    # command_stop uses.
    for forbidden in ("pkill", "killall", "/IM", "netstat",
                      "Get-NetTCPConnection", "Get-Process -Name"):
        assert forbidden not in SOURCE, f"{forbidden} appears in the launcher"


def test_a_recorded_pid_is_only_believed_for_this_repository(launcher, tmp_path,
                                                             monkeypatch):
    """A pid is not identity - the number is reused. Ours is the one whose
    state file also names this repository."""
    monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path))
    launcher.runtime_dir().mkdir(parents=True, exist_ok=True)

    # A live pid (this test process) recorded by a DIFFERENT repository.
    launcher.write_state(pid=os.getpid(), repo_root=r"C:\some\other\checkout")
    assert launcher.running_pid() is None, (
        "a process started by another checkout was claimed as ours")

    launcher.write_state(pid=os.getpid(), repo_root=str(launcher.REPO_ROOT))
    assert launcher.running_pid() == os.getpid()


def test_a_dead_pid_is_not_reported_as_running(launcher, tmp_path, monkeypatch):
    monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path))
    launcher.runtime_dir().mkdir(parents=True, exist_ok=True)
    # Very unlikely to exist, and validated as ours before any signal.
    launcher.write_state(pid=999_999_998, repo_root=str(launcher.REPO_ROOT))
    assert launcher.running_pid() is None


def test_the_runtime_state_file_carries_nothing_secret(launcher, tmp_path,
                                                       monkeypatch):
    monkeypatch.setenv(launcher.DATA_DIR_ENV, str(tmp_path))
    launcher.runtime_dir().mkdir(parents=True, exist_ok=True)
    launcher.write_state(pid=1234, repo_root=str(launcher.REPO_ROOT),
                         host="0.0.0.0", port=8000)
    text = launcher.state_file().read_text(encoding="utf-8").lower()
    for forbidden in ("password", "secret", "token", "bearer", "credential"):
        assert forbidden not in text


# ===========================================================================
# Portability - structural only
# ===========================================================================
def test_every_windows_specific_call_is_behind_a_platform_guard():
    """Read as: this can RUN on POSIX. Not as: this has been accepted there."""
    lines = SOURCE.splitlines()
    windows_only = ("tasklist", "taskkill", "CREATE_NO_WINDOW",
                    "DETACHED_PROCESS", '"Scripts"')
    for index, line in enumerate(lines):
        if not any(token in line for token in windows_only):
            continue
        if line.strip().startswith("#"):
            continue
        window = "\n".join(lines[max(0, index - 12):index])
        assert 'platform.system() == "Windows"' in window, (
            f"line {index + 1} uses a Windows-only call with no platform "
            f"guard above it: {line.strip()}")


def test_the_launcher_imports_on_any_platform(launcher):
    """No Windows-only module at import time - importing it here on Windows
    proves little, so the check is that nothing Windows-only is imported at
    module level at all."""
    for forbidden in ("import winreg", "import win32", "import pywintypes",
                      "import msvcrt"):
        assert forbidden not in SOURCE


def test_paths_are_built_with_pathlib_not_string_concatenation():
    assert "os.path.join" not in SOURCE
    assert "\\\\" not in SOURCE.replace("\\\\Users", "")  # the one guard string


def test_run_is_documented_as_the_foreground_cloud_form():
    assert "foreground" in SOURCE.lower()
    assert "run" in launcher_commands()


def launcher_commands():
    import importlib
    return importlib.import_module("speaklink_server").COMMANDS


def test_all_five_verbs_exist():
    assert set(launcher_commands()) == {"run", "start", "stop", "restart", "status"}


# ===========================================================================
# The Windows wrappers
# ===========================================================================
@pytest.mark.parametrize("name", ["start.bat", "stop.bat", "restart.bat",
                                  "build-store-receiver.bat"])
def test_the_wrappers_are_thin(name):
    """Logic in a .bat cannot be tested, read or reused, and is lost on any
    other operating system. Each wrapper only finds Python and delegates."""
    path = REPOSITORY_ROOT / name
    assert path.is_file(), f"{name} is missing"
    text = path.read_text(encoding="utf-8")
    assert "tools\\" in text, f"{name} does not call the Python launcher"
    # Short enough that nothing substantial can be hiding in it.
    real_lines = [l for l in text.splitlines()
                  if l.strip() and not l.strip().upper().startswith("REM")]
    assert len(real_lines) < 40, f"{name} has grown logic of its own"


@pytest.mark.parametrize("name", ["start.bat", "stop.bat", "restart.bat",
                                  "build-store-receiver.bat"])
def test_the_wrappers_do_not_depend_on_the_working_directory(name):
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    assert "%~dp0" in text, (
        f"{name} must resolve the repository from its own location")


@pytest.mark.parametrize("name", ["start.bat", "stop.bat", "restart.bat"])
def test_the_wrappers_register_no_scheduled_task(name):
    text = (REPOSITORY_ROOT / name).read_text(encoding="utf-8")
    assert "schtasks" not in text.lower()


# ===========================================================================
# .env.example
# ===========================================================================
def test_the_env_example_exists_and_is_tracked():
    example = REPOSITORY_ROOT / ".env.example"
    assert example.is_file()
    text = example.read_text(encoding="utf-8")
    for name in ("APP_HOST", "APP_PORT", "APP_ENV", "ADMIN_USERNAME",
                 "ADMIN_PASSWORD", "JWT_SECRET"):
        assert name in text, f"{name} is not documented in .env.example"


def test_the_env_example_ships_no_actual_secret():
    """A filled-in example is a credential in version control."""
    text = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() in ("ADMIN_USERNAME", "ADMIN_PASSWORD", "JWT_SECRET"):
            assert value.strip() == "", (
                f"{key.strip()} has a value in .env.example")


def test_the_env_example_explains_that_admin_values_are_first_boot_only():
    text = (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8").lower()
    assert "once" in text
    assert "does not reset" in text or "not reset" in text
