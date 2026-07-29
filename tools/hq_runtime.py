"""One windowed process that runs HQ: the backend, the frontend, and nothing else.

WHY THIS EXISTS

An HQ machine should be signed into and then left alone. Today somebody runs a
PowerShell script and leaves the window open. This starts the persistent backend
and the production frontend, watches both, and brings a crashed one back with
bounded backoff - with no console for anybody to close.

TWO PROPERTIES CARRY THE WEIGHT

**It refuses rather than improvises.** Every failure here has a tempting helpful
fallback: create the database, use ``backend/speaklink_live.db``, pick up a pilot
file, run the dev server. Each one turns "some data is missing" into "every
Store has vanished", which is precisely the failure this whole persistent-server
effort exists to end. So it refuses all of them and says what to run instead.

**READY means healthy, not spawned.** A child process that exists is not a
backend that answers. Both children are asked over HTTP before anything claims
to be working - because "the process started" is the same shape of claim that
once let a silent Receiver look like a working one.

Nothing new is invented here that already exists: the no-console child options
come from ``audio_receiver_pilot``, and the single-instance lock, redaction and
rotating logger come from ``receiver_agent``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.audio_receiver_pilot import hidden_child_process_options  # noqa: E402
from tools.persistent_lan_server import (  # noqa: E402
    PERSISTENT_MARKER,
    ServerProfile,
    is_throwaway_pilot_database,
)
from tools.receiver_agent import (  # noqa: E402
    AlreadyRunning,
    InstanceLock,
    configure_logging,
    redact,
)


def _packaged_root() -> "Path | None":
    """The folder the executable lives in, or None in a checkout.

    ``Path(__file__).parents[1]`` is the repository when this module is
    imported by Python and the unpacked PyInstaller bundle when it is frozen -
    which is why the packaged runtime went looking for the React build inside
    its own bundle and refused. Frozen builds ship their frontend and backend
    beside the executable.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def default_frontend_build() -> Path:
    """The production bundle. Never a development server: react-scripts start
    is a watcher with a compiler attached, and it is not what an unattended
    Store system should depend on at seven in the morning."""
    packaged = _packaged_root()
    return (packaged / "frontend") if packaged else (REPOSITORY_ROOT / "frontend" / "build")


def default_backend_root() -> Path:
    packaged = _packaged_root()
    return (packaged / "backend") if packaged else (REPOSITORY_ROOT / "backend")


#: Resolved once at import: whether this process is frozen cannot change while
#: it runs, and a module-level constant stays monkeypatchable in tests.
FRONTEND_BUILD = default_frontend_build()

DEFAULT_HQ_ADDRESS = "192.168.4.134"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 3000

HEALTH_TIMEOUT_SECONDS = 3.0

#: How many times a child is started before the runtime stops trying. Six
#: attempts with the backoff below is about a minute of patience, which covers
#: a network stack that is not up yet at logon without turning a permanently
#: broken backend into a spawn loop.
DEFAULT_MAX_ATTEMPTS = 6

#: Exit codes. Task Scheduler records these, and an operator reads them in the
#: task history, so a supervisor that gave up must never hand back zero.
EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_DEGRADED = 3
#: Deliberately the Receiver's number for the same situation.
EXIT_ALREADY_RUNNING = 4


def runtime_status_path() -> Path:
    """Where the runtime writes down what it is doing.

    Machine-level, not inside the persistent server root, and that placement is
    the point: the status file matters most when there is no persistent root
    to put it in. A windowed process has nowhere to print, so if the refusal is
    not on disk the operator sees a Scheduled Task that "ran successfully" and
    an HQ that is not there.
    """
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "SpeakLink" / "hq-runtime-status.json"


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    BACKEND_STARTING = "BACKEND_STARTING"
    BACKEND_HEALTHY = "BACKEND_HEALTHY"
    FRONTEND_STARTING = "FRONTEND_STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    #: The configuration is usable and NOTHING was started. Distinct from
    #: STOPPED on purpose: a config check is not a run, and overloading STOPPED
    #: made a checker conclude HQ had started when it never had.
    CONFIG_OK = "CONFIG_OK"
    CONFIG_ERROR = "CONFIG_ERROR"
    BACKEND_ERROR = "BACKEND_ERROR"
    FRONTEND_ERROR = "FRONTEND_ERROR"


class RuntimeConfigError(RuntimeError):
    """The persistent profile is not usable. Never carries a secret."""


@dataclass
class RuntimeProfile:
    root: Path
    database: Path
    key_container: Path
    jwt_secret_file: Path
    logs: Path
    lock: Path
    frontend_build: Path
    hq_address: str = DEFAULT_HQ_ADDRESS
    backend_port: int = DEFAULT_BACKEND_PORT
    frontend_port: int = DEFAULT_FRONTEND_PORT
    #: The interpreter that runs the backend, and the directory holding
    #: ``server.py``. Both are resolved once, at refusal time, rather than
    #: discovered when a child fails to start an hour later.
    python: str = sys.executable
    backend_root: Path = field(default_factory=default_backend_root)
    state: RuntimeState = RuntimeState.STARTING
    mode: str = PERSISTENT_MARKER
    status_file: Path = field(default_factory=runtime_status_path)

    @property
    def backend_health_url(self) -> str:
        return f"http://{self.hq_address}:{self.backend_port}/docs"

    @property
    def frontend_health_url(self) -> str:
        return f"http://{self.hq_address}:{self.frontend_port}/"


def count_enrolled_devices(database: Path) -> int:
    """How many Receiver Devices exist. Read-only, and never a guess.

    Opened read-only and immutable so a running server is not disturbed and
    nothing here can write. An unreadable database raises rather than returning
    zero: "I could not count them" must never quietly become "there are none",
    because that is the answer that would let a lost key container through.
    """
    import sqlite3

    uri = f"file:{database.as_posix()}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as failure:
        raise RuntimeConfigError(
            f"{database} could not be opened to check what is enrolled "
            f"({failure.__class__.__name__}). Refusing to start rather than "
            "assume it is empty."
        ) from None
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='receiver_devices'"
        ).fetchall()
        if not rows:
            # No table at all is a database that predates Device enrolment.
            return 0
        return int(connection.execute("SELECT COUNT(*) FROM receiver_devices").fetchone()[0])
    except sqlite3.Error as failure:
        raise RuntimeConfigError(
            f"{database} could not be read to check what is enrolled "
            f"({failure.__class__.__name__}). Refusing to start rather than "
            "assume it is empty."
        ) from None
    finally:
        connection.close()


def resolve_python_executable(configured: "str | None" = None) -> str:
    """The interpreter that will run the backend.

    ``sys.executable`` is right when this module is imported by Python and
    WRONG in the packaged executable, where it is SpeakLinkHQRuntime.exe itself -
    so a frozen supervisor would have relaunched *itself* with ``-m uvicorn``
    and failed with an import error nobody would connect to packaging. The spec
    excludes FastAPI and uvicorn on purpose: the backend is a child, run by the
    machine's own Python, not by the supervisor.
    """
    if configured:
        if not Path(configured).exists():
            raise RuntimeConfigError(
                f"The configured Python interpreter {configured} does not exist. "
                "Correct 'python' in config/hq-runtime.json."
            )
        return str(configured)
    if not getattr(sys, "frozen", False):
        return sys.executable
    import shutil

    found = shutil.which("python") or shutil.which("py")
    if not found:
        raise RuntimeConfigError(
            "No Python interpreter was found on PATH, and this packaged runtime "
            "starts the backend as a child process rather than hosting it. "
            "Install Python, or set 'python' in config/hq-runtime.json to the "
            "interpreter inside the backend virtual environment."
        )
    return found


def read_runtime_settings(root: Path) -> dict:
    """Optional, non-secret overrides. Absent is normal, not an error."""
    settings_file = root / "config" / "hq-runtime.json"
    if not settings_file.exists():
        return {}
    try:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise RuntimeConfigError(
            f"{settings_file} could not be read as JSON ({failure.__class__.__name__}). "
            "Fix or remove it; this runtime will not guess what it meant."
        ) from None
    return loaded if isinstance(loaded, dict) else {}


def resolve_runtime_profile() -> RuntimeProfile:
    """Load the initialized persistent profile, or refuse and say what to run.

    Every branch here is a refusal. There is deliberately no path that creates
    anything - a runtime that can create a database is a runtime that will one
    day create one over the real one.
    """
    server = ServerProfile.persistent()

    if not server.root.exists():
        raise RuntimeConfigError(
            f"There is no persistent server at {server.root}. Run "
            "Initialize-SpeakLinkPersistentLanServer.ps1 first. This runtime will "
            "not create one."
        )
    if not server.database.exists():
        raise RuntimeConfigError(
            f"The persistent database is missing from {server.database}. This "
            "runtime will NOT create an empty one - an empty database that looks "
            "healthy is worse than an obvious absence. Restore from "
            f"{server.backups} or re-run Initialize."
        )
    if is_throwaway_pilot_database(server.database):
        raise RuntimeConfigError(
            "The configured database belongs to a throwaway pilot, which is "
            "rebuilt from scratch on every start. Refusing to run HQ on it."
        )
    if not server.key_container.exists():
        # A missing key container means two completely different things, and
        # the difference is already on disk. On a profile that has never been
        # started there are no Devices and the backend mints the container
        # itself - refusing there would be a refusal no procedure in this
        # repository can satisfy, which is what the packaged executable
        # actually did. On a profile with Stores enrolled, a container that has
        # gone missing is an emergency: mint a new one and every Device
        # credential stops verifying while every Store still looks enrolled.
        enrolled = count_enrolled_devices(server.database)
        if enrolled != 0:
            raise RuntimeConfigError(
                f"The Receiver HMAC key container is missing from "
                f"{server.key_container}, and {enrolled} Device(s) are enrolled "
                "against it. Starting now would mint a new container, every one "
                "of those Devices would fail to authenticate, and all of them "
                "would have to re-enrol. Restore the container from a backup "
                "first. If the Devices really are gone, remove them "
                "deliberately through HQ rather than by starting the server."
            )

    jwt_secret_file = server.key_container.parent / "jwt-secret.txt"
    if not jwt_secret_file.exists():
        # Created rather than refused, and the asymmetry with the key container
        # is the point: a new signing secret costs everybody a sign-in, a new
        # HMAC container costs 44 Stores a re-enrolment.
        # Start-SpeakLinkPersistentLanServer.ps1 already mints it exactly here,
        # so this is the same behaviour rather than a new one.
        jwt_secret_file.parent.mkdir(parents=True, exist_ok=True)
        jwt_secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")

    if not (FRONTEND_BUILD / "index.html").exists():
        raise RuntimeConfigError(
            f"There is no production frontend at {FRONTEND_BUILD}. Run "
            "'yarn build' in frontend first. This runtime does not start a "
            "development server."
        )

    settings = read_runtime_settings(server.root)
    backend_root = Path(settings.get("backend_root") or default_backend_root())
    if not (backend_root / "server.py").exists():
        raise RuntimeConfigError(
            f"There is no server.py in {backend_root}, so there is nothing to "
            "start. Set 'backend_root' in config/hq-runtime.json to the backend "
            "directory of the deployed checkout."
        )

    return RuntimeProfile(
        root=server.root,
        database=server.database,
        key_container=server.key_container,
        jwt_secret_file=jwt_secret_file,
        logs=server.logs,
        lock=server.root / "runtime" / "hq-runtime.lock",
        frontend_build=FRONTEND_BUILD,
        hq_address=str(settings.get("hq_address") or DEFAULT_HQ_ADDRESS),
        backend_port=int(settings.get("backend_port") or DEFAULT_BACKEND_PORT),
        frontend_port=int(settings.get("frontend_port") or DEFAULT_FRONTEND_PORT),
        python=resolve_python_executable(settings.get("python")),
        backend_root=backend_root,
    )


def child_process_options() -> dict:
    """No console for any child. Reused, not reinvented.

    This runtime is windowed, so a console-subsystem child started without
    CREATE_NO_WINDOW gets a brand-new console - a black window on the HQ desk.
    Exactly the FFmpeg defect, one layer up.
    """
    return hidden_child_process_options()


def backend_command(profile: RuntimeProfile) -> list:
    """Uvicorn, one worker, private address, no reload, no debug.

    One worker is not a performance choice: WebSocket connection state is
    process-local, so a second worker loses half the Receivers depending on
    which process answered.
    """
    return [
        profile.python, "-m", "uvicorn", "server:app",
        "--app-dir", str(profile.backend_root),
        "--host", profile.hq_address,
        "--port", str(profile.backend_port),
        "--workers", "1",
        "--no-access-log",
    ]


def frontend_command(profile: RuntimeProfile) -> list:
    """Serve the production build with the standard library.

    Chosen over `serve`, `nginx` or a Node static server because it adds no
    dependency to an HQ machine that already has this Python. It is a private
    LAN pilot serving static files to a handful of browsers, not a public CDN.
    """
    return [
        profile.python, "-m", "http.server", str(profile.frontend_port),
        "--bind", profile.hq_address,
        "--directory", str(profile.frontend_build),
    ]


def child_environment(profile: RuntimeProfile) -> dict:
    """Secrets travel in the environment, never on a command line.

    A command line is visible in the process list to every user on the machine;
    an environment is not.
    """
    secret = profile.jwt_secret_file.read_text(encoding="utf-8").strip()
    return dict(
        os.environ,
        SPEAKLINK_DB_PATH=str(profile.database),
        SPEAKLINK_KEY_CONTAINER=str(profile.key_container),
        SPEAKLINK_KEY_PROTECTOR="dpapi",
        SPEAKLINK_SERVER_MODE=profile.mode,
        JWT_SECRET=secret,
        CORS_ORIGINS=f"http://{profile.hq_address}:{profile.frontend_port}",
    )


def http_ok(url: str, *, timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    """Did it answer? Not: did a process appear."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


@dataclass
class BackoffPolicy:
    """Bounded, jittered, and it gives up rather than looping for ever."""

    base_seconds: float = 2.0
    maximum_seconds: float = 60.0

    def delay(self, attempt: int, *, random_value) -> float:
        raw = min(self.base_seconds * (2 ** max(0, attempt - 1)), self.maximum_seconds)
        # Jitter, so several children never retry in lockstep.
        return min(self.maximum_seconds, raw * (0.75 + 0.5 * random_value()))


@dataclass
class ChildOutcome:
    state: RuntimeState
    attempts: int
    detail: str = ""


def supervise_child(*, name: str, start, is_alive, health, max_attempts: int = 5,
                    sleep=None, random_value=None, policy: "BackoffPolicy | None" = None
                    ) -> ChildOutcome:
    """Start a child until it is HEALTHY, or give up and say so.

    Giving up matters. A permanently broken backend that is respawned for ever
    fills the disk with logs, and buries the one line that says why.
    """
    if sleep is None:
        import time

        sleep = time.sleep
    if random_value is None:
        import random as _random

        random_value = _random.random
    policy = policy or BackoffPolicy()

    healthy_state = (RuntimeState.BACKEND_HEALTHY if name == "backend"
                     else RuntimeState.READY)

    for attempt in range(1, max_attempts + 1):
        child = start()
        if is_alive(child) and health():
            return ChildOutcome(state=healthy_state, attempts=attempt)
        if attempt < max_attempts:
            sleep(policy.delay(attempt, random_value=random_value))

    return ChildOutcome(
        state=RuntimeState.DEGRADED,
        attempts=max_attempts,
        detail=(f"the {name} did not become healthy after {max_attempts} attempts; "
                "the runtime log holds the sequence"),
    )


def runtime_lock() -> InstanceLock:
    """One HQ runtime per machine, scoped to the persistent root.

    Two supervisors would fight over one SQLite file and one port, and the
    second would look like a crash loop.
    """
    return InstanceLock(ServerProfile.persistent().root / "runtime" / "hq-runtime")


def stop_children(children, *, log=None) -> int:
    """Stop only processes this runtime started, verified before terminating.

    A recorded PID is not proof of identity: Windows reuses process numbers, and
    a stale record can name something else entirely. Each candidate's command
    line is checked before anything is signalled.
    """
    stopped = 0
    for child in children:
        pid = getattr(child, "pid", None)
        if pid is None or child.poll() is not None:
            continue
        cmdline = " ".join(getattr(child, "args", []) or [])
        if "uvicorn" not in cmdline and "http.server" not in cmdline:
            if log:
                log.warning("refusing to stop pid %s: not one of ours", pid)
            continue
        child.terminate()
        try:
            child.wait(timeout=10)
        except Exception:
            child.kill()
        stopped += 1
        if log:
            log.info("stopped child pid %s", pid)
    return stopped


def start_logging(profile: RuntimeProfile):
    """Rotating, bounded, redacted - the Receiver's logger, reused."""
    log = configure_logging(profile.logs)
    log.info("%s", redact(f"HQ runtime starting, mode={profile.mode}"))
    log.info("database=%s", profile.database)
    log.info("frontend=%s", profile.frontend_build)
    return log


# ===========================================================================
# What state the runtime is in, written where a windowed process can be read
# ===========================================================================
def write_status(path, state: RuntimeState, *, detail: str = "") -> None:
    """Replace the status file whole, and never let a secret into it.

    Replaced rather than appended because a status file is a current fact, not
    a history; the log is the history. Written through a temporary file and
    renamed so a reader never sees half a document.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state.value if isinstance(state, RuntimeState) else str(state),
        "detail": redact(detail),
        "pid": os.getpid(),
    }
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(target)


def read_status(path) -> dict:
    """Unreadable is UNKNOWN, not FAIL. A checker that scores 'I could not
    look' as 'it is broken' has already misled somebody in this repository."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def spawn_child(command: list, env: dict):
    """Start one child with no console and no inherited standard streams.

    The streams are discarded rather than piped: nobody reads them, and a full
    pipe buffer would silently wedge the child weeks into a deployment.
    """
    return subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **child_process_options(),
    )


# ===========================================================================
# The supervisor itself
# ===========================================================================
class HQRuntime:
    """Starts the backend, then the frontend, then keeps both answering.

    The order is deliberate. A frontend served before the backend answers is a
    login page that cannot log anybody in, which a Store manager reads as "HQ is
    broken" rather than "HQ is still starting".
    """

    def __init__(self, profile: RuntimeProfile, *, log=None, spawn=None, http=None,
                 sleep=None, random_value=None, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 watch_interval: float = 15.0) -> None:
        self.profile = profile
        self._log = log
        self._spawn = spawn
        self._http = http
        self._sleep = sleep
        self._random = random_value
        self._max_attempts = max_attempts
        self._watch_interval = watch_interval
        self._children: dict = {}
        self._restarts: dict = {"backend": 0, "frontend": 0}

    # -- the injectable edges -------------------------------------------------
    def _pause(self, seconds: float) -> None:
        (self._sleep or time.sleep)(seconds)

    def _chance(self) -> float:
        if self._random is not None:
            return self._random()
        import random

        return random.random()

    def _command(self, name: str) -> list:
        return (backend_command(self.profile) if name == "backend"
                else frontend_command(self.profile))

    def _start_one(self, name: str):
        command = self._command(name)
        environment = child_environment(self.profile)
        child = (self._spawn or spawn_child)(command, environment)
        self._children[name] = child
        if self._log:
            self._log.info("started the %s, pid %s", name, getattr(child, "pid", "?"))
        return child

    def _healthy(self, name: str) -> bool:
        url = (self.profile.backend_health_url if name == "backend"
               else self.profile.frontend_health_url)
        asked = self._http or http_ok
        return bool(asked(url, timeout=HEALTH_TIMEOUT_SECONDS))

    def _record(self, state: RuntimeState, detail: str = "") -> RuntimeState:
        write_status(self.profile.status_file, state, detail=detail)
        if self._log:
            self._log.info("state=%s %s", state.value, redact(detail))
        return state

    # -- starting -------------------------------------------------------------
    def start(self) -> RuntimeState:
        self._record(RuntimeState.BACKEND_STARTING)
        backend = supervise_child(
            name="backend",
            start=lambda: self._start_one("backend"),
            is_alive=lambda child: child.poll() is None,
            health=lambda: self._healthy("backend"),
            max_attempts=self._max_attempts,
            sleep=self._pause,
            random_value=self._chance,
        )
        if backend.state is not RuntimeState.BACKEND_HEALTHY:
            return self._record(RuntimeState.DEGRADED, backend.detail)

        self._record(RuntimeState.FRONTEND_STARTING)
        frontend = supervise_child(
            name="frontend",
            start=lambda: self._start_one("frontend"),
            is_alive=lambda child: child.poll() is None,
            health=lambda: self._healthy("frontend"),
            max_attempts=self._max_attempts,
            sleep=self._pause,
            random_value=self._chance,
        )
        if frontend.state is not RuntimeState.READY:
            return self._record(RuntimeState.DEGRADED, frontend.detail)

        return self._record(
            RuntimeState.READY,
            f"backend and frontend both answered on {self.profile.hq_address}",
        )

    # -- watching -------------------------------------------------------------
    def watch_once(self) -> RuntimeState:
        for name in ("backend", "frontend"):
            child = self._children.get(name)
            if child is not None and child.poll() is None and self._healthy(name):
                continue
            self._restarts[name] += 1
            if self._restarts[name] > self._max_attempts:
                return self._record(
                    RuntimeState.DEGRADED,
                    f"the {name} stopped answering {self._restarts[name]} times; "
                    "the runtime log holds the sequence",
                )
            if self._log:
                self._log.warning("the %s stopped answering; restarting it", name)
            self._start_one(name)
        return self._record(RuntimeState.READY)

    def run(self, iterations: "int | None" = None) -> RuntimeState:
        state = self.start()
        if state is not RuntimeState.READY:
            return state
        completed = 0
        while iterations is None or completed < iterations:
            self._pause(self._watch_interval)
            state = self.watch_once()
            if state is RuntimeState.DEGRADED:
                return state
            completed += 1
        return state

    # -- stopping -------------------------------------------------------------
    def stop(self, *, final_state: RuntimeState = RuntimeState.STOPPED) -> int:
        self._record(RuntimeState.STOPPING)
        stopped = stop_children(
            [child for child in self._children.values() if child is not None],
            log=self._log,
        )
        self._children.clear()
        self._record(final_state)
        return stopped


def main(argv=None) -> int:
    """Start HQ and stay out of the way, or refuse and write down why.

    Every return value here is read by Task Scheduler. Zero means the runtime
    did its job; a supervisor that gave up must never hand back zero, because a
    green task history and a dead HQ is the worst combination available.
    """
    parser = argparse.ArgumentParser(
        prog="SpeakLinkHQRuntime",
        description="Run the SpeakLink HQ backend and frontend, and keep them running.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Resolve the persistent profile and exit. Starts nothing.",
    )
    parser.add_argument(
        "--iterations", type=int, default=None,
        help="Supervise this many rounds and stop. Omit to run until stopped.",
    )
    arguments = parser.parse_args(argv)

    try:
        profile = resolve_runtime_profile()
    except Exception as refusal:  # noqa: BLE001 - every refusal must be recorded
        write_status(runtime_status_path(), RuntimeState.CONFIG_ERROR,
                     detail=str(refusal))
        return EXIT_CONFIG_ERROR

    if arguments.check:
        # A successful check may clear a CONFIG_ERROR, because it has just
        # proved that refusal false - and the status file is the only channel a
        # windowed process has, so a stale refusal in it is not clutter, it is
        # the runtime lying. It may clear NOTHING else: overwriting a live READY
        # with the result of a side check would destroy the evidence it was run
        # to read.
        if read_status(profile.status_file).get("state") == RuntimeState.CONFIG_ERROR.value:
            write_status(profile.status_file, RuntimeState.CONFIG_OK,
                         detail="configuration check passed; the earlier refusal "
                                "no longer applies. Nothing was started.")
        return EXIT_OK

    lock = runtime_lock()
    try:
        lock.acquire()
    except AlreadyRunning:
        return EXIT_ALREADY_RUNNING

    log = start_logging(profile)
    runtime = HQRuntime(profile, log=log)
    state = RuntimeState.STOPPED
    try:
        state = runtime.run(iterations=arguments.iterations)
    except KeyboardInterrupt:
        state = RuntimeState.STOPPED
    finally:
        runtime.stop(final_state=(RuntimeState.DEGRADED
                                  if state is RuntimeState.DEGRADED
                                  else RuntimeState.STOPPED))
        lock.release()

    return EXIT_OK if state in (RuntimeState.READY, RuntimeState.STOPPED) else EXIT_DEGRADED


if __name__ == "__main__":
    sys.exit(main())
