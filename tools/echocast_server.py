"""Repo-native EchoCast HQ launcher. One command, any operating system.

WHAT THIS REPLACES, AND WHY

The installed HQ copied itself into %LOCALAPPDATA%, kept its data in a second
place under the same root, and started from a Windows Scheduled Task. Three
consequences an operator actually felt: the application was somewhere other
than the folder they had, the data was somewhere else again, and nothing
started at all on a machine that is not Windows.

Here the repository IS the installation. Copy the folder, write one .env, run
one command. Data lives in ``<repo>/data`` so a backup is "copy that folder",
and there is no Task Scheduler entry for HQ at all.

The Store Receiver keeps its Scheduled Task. That is a genuinely different
problem - a till has no operator to start anything, and Windows session 0 has
no audio endpoint - and nothing here changes it.

FIVE VERBS

    run       foreground. The cloud/process-manager form: this process IS the
              server, so systemd, a container or a CI runner can supervise it.
    start     background. Windows operator convenience; POSIX gets it too.
    stop      stop the instance THIS repository started, and nothing else.
    restart   stop, confirm it stopped, start, confirm READY.
    status    report without changing anything.

BAT FILES ARE WRAPPERS, NOT LOGIC

start.bat, stop.bat and restart.bat each call this file. Every decision lives
here so that a Linux host loses only the double-click, not the capability.

WHAT THIS DELIBERATELY DOES NOT DO

Not a supervisor. It does not restart a crashed backend on a timer the way
the legacy runtime did - that behaviour belongs to whatever is supervising
``run`` (systemd, a container runtime, or an operator watching a window).
Adding a second supervisor underneath one of those is how a process ends up
being restarted by two things that disagree.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: The repository root, resolved from THIS FILE rather than from the working
#: directory. An operator double-clicking start.bat from Explorer, a scheduled
#: job with C:\Windows\System32 as its cwd, and `python tools/echocast_server.py`
#: from anywhere must all find the same repository - and none of them may
#: depend on the folder being called anything in particular.
REPO_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR_ENV = "ECHOCAST_DATA_DIR"
DEFAULT_DATA_DIRNAME = "data"

#: Reused from the existing configuration surface rather than invented. Every
#: one of these names already means this thing somewhere in the codebase.
DB_PATH_ENV = "ECHOCAST_DB_PATH"                 # backend/db_config.py
KEY_CONTAINER_ENV = "ECHOCAST_KEY_CONTAINER"     # backend/receiver_key_bootstrap.py
FRONTEND_BUILD_ENV = "ECHOCAST_FRONTEND_BUILD"   # backend/server.py

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000

STARTUP_TIMEOUT_SECONDS = 90
STOP_TIMEOUT_SECONDS = 30
POLL_SECONDS = 1.0


class LaunchError(RuntimeError):
    """Something an operator has to fix. Printed without a traceback."""


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------
def data_dir() -> Path:
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return REPO_ROOT / DEFAULT_DATA_DIRNAME


def runtime_dir() -> Path:
    return data_dir() / "runtime"


def logs_dir() -> Path:
    return data_dir() / "logs"


def keys_dir() -> Path:
    return data_dir() / "keys"


def database_path() -> Path:
    return data_dir() / "echocast.db"


def pid_file() -> Path:
    return runtime_dir() / "hq.pid"


def state_file() -> Path:
    return runtime_dir() / "hq-state.json"


def frontend_build_dir() -> Path:
    return REPO_ROOT / "frontend" / "build"


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def parse_env_file(path: Path) -> dict[str, str]:
    """A deliberately small KEY=VALUE reader.

    No python-dotenv dependency, because this file has to run BEFORE anything
    is installed - it is what creates the environment that installs things.
    Supports comments, blank lines, `export ` prefixes and quoted values;
    everything else is left alone rather than guessed at.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_environment() -> dict[str, str]:
    """Repo .env, then the real environment on top.

    That order is the important half: a value already exported by the shell,
    a container or a CI secret store WINS over the file. A cloud deployment
    that injects JWT_SECRET must not be silently overridden by a stale .env
    somebody committed to their own copy.
    """
    from_file = parse_env_file(REPO_ROOT / ".env")
    merged = dict(from_file)
    merged.update(os.environ)
    return merged


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.host = env.get("APP_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
        try:
            self.port = int(env.get("APP_PORT", str(DEFAULT_PORT)).strip() or DEFAULT_PORT)
        except ValueError:
            raise LaunchError(
                f"APP_PORT must be a number, not {env.get('APP_PORT')!r}.")
        self.app_env = (env.get("APP_ENV", "development").strip().lower()
                        or "development")

    @property
    def health_url(self) -> str:
        # A bind address is not a connect address: 0.0.0.0 means "every
        # interface", and connecting to it is undefined on some platforms.
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}/api/"

    @property
    def display_url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{host}:{self.port}/"


def require_bootstrap_values(config: Config, *, database_exists: bool) -> None:
    """Fail closed on a FRESH database, and only on a fresh one.

    Two rules, and the second is the one that protects a running estate:

    * Creating the first database needs ADMIN_USERNAME, ADMIN_PASSWORD and
      JWT_SECRET. Missing any of them is a refusal - never a default account,
      never a generated password written to a log. A half-initialized database
      reported as READY is worse than no database at all.

    * Once the database exists those admin values are IGNORED. Editing
      ADMIN_PASSWORD in .env must not silently reset a live account, and
      editing ADMIN_USERNAME must not silently mint a second Owner. Account
      state belongs to User Management after bootstrap. (backend/seed.py
      enforces the same rule independently; this is the earlier, clearer
      refusal, not the only one.)
    """
    missing: list[str] = []
    if not config.env.get("JWT_SECRET", "").strip():
        missing.append("JWT_SECRET")
    if not database_exists:
        for name in ("ADMIN_USERNAME", "ADMIN_PASSWORD"):
            if not config.env.get(name, "").strip():
                missing.append(name)

    if missing:
        what = "creating a new database" if not database_exists else "starting"
        verb = "is" if len(missing) == 1 else "are"
        raise LaunchError(
            f"Refusing to start: {', '.join(missing)} {verb} not set, and {what} "
            f"requires it.\n"
            f"  Copy .env.example to .env and fill it in:  {REPO_ROOT / '.env'}\n"
            "  No default account is ever created, and no password is invented."
        )


def child_environment(config: Config) -> dict[str, str]:
    """The environment the server runs with, with repo-local paths applied.

    Every path is set EXPLICITLY rather than left to a default, so the server
    cannot quietly fall back to the old AppData profile or to the historical
    backend/echocast_live.db.
    """
    env = dict(os.environ)
    env.update(config.env)
    env.setdefault("APP_ENV", config.app_env)
    env[DB_PATH_ENV] = str(database_path())
    env[KEY_CONTAINER_ENV] = str(keys_dir() / "receiver-hmac-keys.bin")
    env[FRONTEND_BUILD_ENV] = str(frontend_build_dir())
    env["PYTHONUNBUFFERED"] = "1"
    # The backend imports its own modules by bare name.
    existing = env.get("PYTHONPATH", "")
    backend = str(REPO_ROOT / "backend")
    env["PYTHONPATH"] = f"{backend}{os.pathsep}{existing}" if existing else backend
    return env


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
def venv_dir() -> Path:
    return REPO_ROOT / "backend" / ".venv"


def venv_python() -> Path:
    if platform.system() == "Windows":
        return venv_dir() / "Scripts" / "python.exe"
    return venv_dir() / "bin" / "python"


def requirements_file() -> Path:
    return REPO_ROOT / "backend" / "requirements.txt"


def dependency_stamp() -> Path:
    return runtime_dir() / "requirements.stamp"


def _requirements_fingerprint() -> str:
    import hashlib
    data = requirements_file().read_bytes()
    return hashlib.sha256(data).hexdigest()


def ensure_python_environment(*, quiet: bool = False) -> Path:
    """A repo-local virtual environment with the pinned requirements.

    Bootstrapped on demand so a Windows operator can go from a copied folder
    to a running HQ without a separate setup step - but stamped, so an
    ordinary start does not reinstall anything. The stamp is the hash of
    requirements.txt: change a pin, and exactly one start pays for it.

    NOT CROSS-PLATFORM AS A DIRECTORY. A .venv built on Windows contains
    Windows executables and cannot be copied to Linux. requirements.txt is
    what travels; the environment is rebuilt on the target machine.
    """
    python = venv_python()
    if not python.is_file():
        if not quiet:
            print(f"  creating a repo-local virtual environment in {venv_dir()}")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(venv_dir())],
                           check=True)
        except subprocess.CalledProcessError as failure:
            raise LaunchError(
                f"Could not create a virtual environment ({failure}).\n"
                "  Python 3.11+ with the venv module is required."
            ) from failure

    fingerprint = _requirements_fingerprint()
    stamp = dependency_stamp()
    if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == fingerprint:
        return python

    if not quiet:
        print("  installing pinned Python requirements (first run, or "
              "requirements.txt changed)")
    try:
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check",
             "--quiet", "-r", str(requirements_file())],
            check=True)
    except subprocess.CalledProcessError as failure:
        raise LaunchError(
            f"Installing requirements failed ({failure}). The pinned versions "
            "in backend/requirements.txt are authoritative and are never "
            "upgraded automatically."
        ) from failure

    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(fingerprint, encoding="utf-8")
    return python


def ensure_frontend_build(*, quiet: bool = False) -> None:
    """A built React app must exist. Node is a BUILD dependency only.

    Serving an already-built frontend needs nothing but Python, which is what
    makes a cloud deployment "build in CI, run the Python server". Node and
    Yarn are required only when there is no build to serve.
    """
    index = frontend_build_dir() / "index.html"
    if index.is_file():
        return

    yarn = shutil.which("yarn")
    if not yarn:
        raise LaunchError(
            f"There is no built frontend at {frontend_build_dir()} and Yarn is "
            "not installed, so one cannot be built.\n"
            "  Install Node.js and Yarn, or build the frontend elsewhere and "
            "copy frontend/build here.\n"
            "  Serving an existing build needs only Python - this is a "
            "build-time requirement, not a runtime one."
        )
    frontend = REPO_ROOT / "frontend"
    if not quiet:
        print("  building the React application (first run only)")
    if not (frontend / "node_modules").is_dir():
        subprocess.run([yarn, "install", "--frozen-lockfile"],
                       cwd=str(frontend), check=True, shell=False)
    subprocess.run([yarn, "build"], cwd=str(frontend), check=True, shell=False)
    if not index.is_file():
        raise LaunchError("The frontend build finished but produced no index.html.")


# ---------------------------------------------------------------------------
# Process identity
# ---------------------------------------------------------------------------
def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True)
        return f'"{pid}"' in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state() -> dict:
    path = state_file()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return {}


def write_state(**values) -> None:
    runtime_dir().mkdir(parents=True, exist_ok=True)
    # Identity, never secrets. This file is only ever read to decide whether a
    # pid belongs to us; nothing here would matter if it were read by somebody
    # else, and that is the property to preserve.
    state_file().write_text(json.dumps(values, indent=2), encoding="utf-8")


def running_pid() -> int | None:
    """The pid of OUR server, or None. Never somebody else's.

    A pid on its own is not identity - the number is reused, and on a busy
    machine it is reused quickly. Stopping "whatever holds this pid" is how a
    stop script kills an unrelated process. So the recorded pid is only
    believed when the state file also says it was this repository that started
    it, and the process is still alive.
    """
    state = read_state()
    pid = state.get("pid")
    if not isinstance(pid, int):
        return None
    if state.get("repo_root") != str(REPO_ROOT):
        return None
    if not _process_is_alive(pid):
        return None
    return pid


def clear_runtime_files() -> None:
    for path in (pid_file(), state_file()):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def health_ok(url: str, *, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_for_ready(config: Config, *, timeout: int = STARTUP_TIMEOUT_SECONDS,
                   is_alive=lambda: True) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive():
            return False
        if health_ok(config.health_url):
            return True
        time.sleep(POLL_SECONDS)
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def uvicorn_command(python: Path, config: Config) -> list[str]:
    """One worker, always.

    BroadcastRuntime, the WebSocket registry and the audio fan-out are all
    process-local state. A second worker would be a second, invisible copy of
    every live broadcast, and a Receiver would reach whichever one the load
    balancer happened to pick.
    """
    return [
        str(python), "-m", "uvicorn", "server:app",
        "--app-dir", str(REPO_ROOT / "backend"),
        "--host", config.host,
        "--port", str(config.port),
        "--workers", "1",
        "--no-access-log",
    ]


def prepare(config: Config, *, quiet: bool = False) -> Path:
    for directory in (data_dir(), runtime_dir(), logs_dir(), keys_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    require_bootstrap_values(config, database_exists=database_path().is_file())
    python = ensure_python_environment(quiet=quiet)
    ensure_frontend_build(quiet=quiet)
    return python


def command_run(config: Config) -> int:
    """Foreground. This process becomes the server.

    exec-style replacement is deliberately NOT used: the state file has to be
    cleaned up on the way out, and a signal has to reach the child.
    """
    python = prepare(config)
    existing = running_pid()
    if existing:
        raise LaunchError(
            f"EchoCast HQ is already running in this repository (pid {existing}).\n"
            "  Use restart, or stop it first.")

    print(f"EchoCast HQ starting on {config.display_url}")
    print(f"  data      : {data_dir()}")
    print(f"  database  : {database_path()}")
    print(f"  logs      : {logs_dir()}")
    process = subprocess.Popen(uvicorn_command(python, config),
                               env=child_environment(config), cwd=str(REPO_ROOT))
    write_state(pid=process.pid, repo_root=str(REPO_ROOT), host=config.host,
                port=config.port, mode="run", started_at=time.time())
    pid_file().write_text(str(process.pid), encoding="utf-8")

    def _forward(signum, _frame):
        process.terminate()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _forward)
            except (ValueError, OSError):
                # Not on the main thread, or unsupported on this platform.
                pass
    try:
        return process.wait()
    finally:
        clear_runtime_files()


def command_start(config: Config) -> int:
    """Background. Convenience, mainly for a Windows operator."""
    existing = running_pid()
    if existing:
        print(f"EchoCast HQ is already running in this repository (pid {existing}).")
        print(f"  {config.display_url}")
        return 0

    # A stale state file from a machine that lost power is not a running
    # server. Clearing it here is safe precisely because running_pid() just
    # said nothing of ours is alive.
    clear_runtime_files()

    python = prepare(config)
    logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = logs_dir() / "hq-runtime.log"
    handle = open(log_path, "ab", buffering=0)

    creation_flags = 0
    start_new_session = False
    if platform.system() == "Windows":
        # No console window for the detached server, and it must survive the
        # window that started it closing.
        creation_flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                          | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        start_new_session = True

    process = subprocess.Popen(
        uvicorn_command(python, config), env=child_environment(config),
        cwd=str(REPO_ROOT), stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
        creationflags=creation_flags, start_new_session=start_new_session)
    write_state(pid=process.pid, repo_root=str(REPO_ROOT), host=config.host,
                port=config.port, mode="start", started_at=time.time())
    pid_file().write_text(str(process.pid), encoding="utf-8")

    print(f"EchoCast HQ starting (pid {process.pid})")
    if wait_for_ready(config, is_alive=lambda: process.poll() is None):
        print(f"READY  {config.display_url}")
        return 0

    if process.poll() is not None:
        clear_runtime_files()
        raise LaunchError(
            f"EchoCast HQ exited during startup (code {process.returncode}).\n"
            f"  Read the log: {log_path}")
    raise LaunchError(
        f"EchoCast HQ did not answer within {STARTUP_TIMEOUT_SECONDS}s.\n"
        f"  It may still be starting. Read the log: {log_path}")


def command_stop(config: Config) -> int:
    """Stop OUR instance. Never a search for pythons to kill."""
    pid = running_pid()
    if pid is None:
        if state_file().is_file() or pid_file().is_file():
            clear_runtime_files()
            print("EchoCast HQ was not running. Cleared a stale runtime file.")
        else:
            print("EchoCast HQ is not running in this repository.")
        return 0

    print(f"Stopping EchoCast HQ (pid {pid})")
    if platform.system() == "Windows":
        # /T so the uvicorn child tree goes with it. Targeted at one pid that
        # has already been confirmed to be ours.
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _process_is_alive(pid):
            clear_runtime_files()
            print("Stopped.")
            return 0
        time.sleep(POLL_SECONDS)

    if platform.system() != "Windows":
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(1)
        except ProcessLookupError:
            pass
        if not _process_is_alive(pid):
            clear_runtime_files()
            print("Stopped.")
            return 0

    raise LaunchError(f"EchoCast HQ (pid {pid}) did not stop within "
                      f"{STOP_TIMEOUT_SECONDS}s.")


def command_restart(config: Config) -> int:
    command_stop(config)
    if running_pid() is not None:
        raise LaunchError("Refusing to start a second instance: the previous "
                          "one is still running.")
    return command_start(config)


def command_status(config: Config) -> int:
    """Read-only. Reports, changes nothing."""
    pid = running_pid()
    answering = health_ok(config.health_url)
    state = read_state()

    print("EchoCast HQ (repo-native)")
    print(f"  repository : {REPO_ROOT}")
    print(f"  data       : {data_dir()}")
    print(f"  database   : {database_path()}"
          f"{'' if database_path().is_file() else '   (not created yet)'}")
    print(f"  frontend   : {frontend_build_dir()}"
          f"{'' if (frontend_build_dir() / 'index.html').is_file() else '   (not built)'}")
    print(f"  url        : {config.display_url}")
    if pid:
        print(f"  process    : running, pid {pid} (started via {state.get('mode', '?')})")
    else:
        print("  process    : not running")
    print(f"  api        : {'answering' if answering else 'not answering'}")
    if pid and answering:
        print("  state      : READY")
        return 0
    if pid and not answering:
        print("  state      : STARTING or UNHEALTHY")
        return 1
    return 3


COMMANDS = {
    "run": command_run,
    "start": command_start,
    "stop": command_stop,
    "restart": command_restart,
    "status": command_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="echocast_server",
        description="Run EchoCast HQ from this repository. No installer, no "
                    "Task Scheduler, no AppData.")
    parser.add_argument("command", choices=sorted(COMMANDS), help="what to do")
    arguments = parser.parse_args(argv)

    try:
        config = Config(load_environment())
        return COMMANDS[arguments.command](config)
    except LaunchError as refusal:
        # An operator problem: say what to do, not where in Python it happened.
        print(f"\n{refusal}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
