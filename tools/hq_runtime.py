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

import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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
    InstanceLock,
    configure_logging,
    redact,
)


#: The production bundle. Never a development server: react-scripts start is a
#: watcher with a compiler attached, and it is not what an unattended Store
#: system should depend on at seven in the morning.
FRONTEND_BUILD = REPOSITORY_ROOT / "frontend" / "build"

DEFAULT_HQ_ADDRESS = "192.168.4.134"
DEFAULT_BACKEND_PORT = 8000
DEFAULT_FRONTEND_PORT = 3000

HEALTH_TIMEOUT_SECONDS = 3.0


class RuntimeState(str, Enum):
    STARTING = "STARTING"
    BACKEND_STARTING = "BACKEND_STARTING"
    BACKEND_HEALTHY = "BACKEND_HEALTHY"
    FRONTEND_STARTING = "FRONTEND_STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
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
    state: RuntimeState = RuntimeState.STARTING
    mode: str = PERSISTENT_MARKER

    @property
    def backend_health_url(self) -> str:
        return f"http://{self.hq_address}:{self.backend_port}/docs"

    @property
    def frontend_health_url(self) -> str:
        return f"http://{self.hq_address}:{self.frontend_port}/"


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
        raise RuntimeConfigError(
            f"The Receiver HMAC key container is missing from {server.key_container}. "
            "Without it no Device credential can be verified, so every Store would "
            "fail to authenticate. Restore it before starting."
        )

    jwt_secret_file = server.key_container.parent / "jwt-secret.txt"
    if not jwt_secret_file.exists():
        raise RuntimeConfigError(
            f"The signing secret is missing from {jwt_secret_file}. Starting "
            "without it would invalidate every session that exists."
        )

    if not (FRONTEND_BUILD / "index.html").exists():
        raise RuntimeConfigError(
            f"There is no production frontend at {FRONTEND_BUILD}. Run "
            "'yarn build' in frontend first. This runtime does not start a "
            "development server."
        )

    return RuntimeProfile(
        root=server.root,
        database=server.database,
        key_container=server.key_container,
        jwt_secret_file=jwt_secret_file,
        logs=server.logs,
        lock=server.root / "runtime" / "hq-runtime.lock",
        frontend_build=FRONTEND_BUILD,
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
        sys.executable, "-m", "uvicorn", "server:app",
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
        sys.executable, "-m", "http.server", str(profile.frontend_port),
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
