"""Local one-Store pilot readiness harness for EchoCast AI.

Prepares an isolated, disposable pilot SQLite database outside the repository,
then runs a loopback-only software smoke test against it: backend startup,
liveness, login, the canonical Store/Zone catalog, one Receiver WebSocket
authentication, honest connection state, Receiver cleanup and graceful
shutdown.

What a passing run proves:
    isolated startup, the login/API flow, the canonical 9-Zone / 44-Store
    catalog, one Receiver authenticating with a Bearer header, an honest
    CONNECTED state, and clean shutdown.

What it does NOT prove:
    microphone capture, audio delivery, FFmpeg playback, Windows audio output,
    amplifier behaviour, audible Store speakers, EchoGuard, or production
    readiness. The harness never sends a readiness, playback or acoustic
    acknowledgement, so it can never manufacture READY, AUDIO_RECEIVING,
    PLAYBACK_CONFIRMED or SPEAKER_VERIFIED.

Safety boundary:
    The protected application database is refused by resolved path and by
    same-file identity. The pilot root must live outside the repository. All
    runtime artifacts (database, logs, PID files, reports) stay outside Git.
    No password and no Store credential is ever printed, logged or persisted.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
PROTECTED_DATABASE_PATH = BACKEND_DIR / "echocast_live.db"

PILOT_MARKER_NAME = "PILOT_ONLY"
PILOT_DATABASE_NAME = "echocast_local_pilot.db"

# Environment variable names proven from the current application code.
DB_PATH_ENV = "ECHOCAST_DB_PATH"          # backend/db.py
ADMIN_USERNAME_ENV = "ADMIN_USERNAME"     # backend/seed.py
ADMIN_PASSWORD_ENV = "ADMIN_PASSWORD"     # backend/seed.py
JWT_SECRET_ENV = "JWT_SECRET"             # backend/auth.py
CORS_ORIGINS_ENV = "CORS_ORIGINS"         # backend/server.py

EXIT_OK = 0
EXIT_SAFETY = 1
EXIT_SMOKE_FAILED = 2
EXIT_CLEANUP_FAILED = 3

READINESS_SCOPE = "READY_FOR_LOCAL_SOFTWARE_PILOT_TEST"

# Retired 13-entry runtime demo catalog (removed in commit e8b75dd).
RETIRED_DEMO_STORE_CODES = (
    "MUM-001", "MUM-002", "PUN-001", "DEL-001", "DEL-002", "GUR-001",
    "BLR-001", "BLR-002", "HYD-001", "CHN-001", "KOL-001", "ONL-001", "ONL-002",
)

# The query-string credential test deliberately uses a fixed dummy value.
# Uvicorn access logs record request URLs, so a real Store credential must
# never be placed in a URL even for a negative test.
QUERY_TOKEN_PROBE = "query-string-credentials-must-be-refused"

PREFERRED_PILOT_STORE_CODE = "UN"
LIVENESS_PATH = "/docs"
STARTUP_TIMEOUT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 15


class PilotError(RuntimeError):
    """Base class for controlled, secret-free pilot failures."""


class ProtectedPathError(PilotError):
    """Raised when the protected application database was supplied."""


class PilotSafetyError(PilotError):
    """Raised when a path or marker check fails closed."""


class PilotCredentialError(PilotError):
    """Raised when the operator-supplied pilot password is missing."""


class PilotStartupError(PilotError):
    """Raised when the pilot backend cannot be started or reached."""


class PilotSmokeError(PilotError):
    """Raised when an application smoke assertion fails."""


@dataclass(frozen=True)
class PilotPaths:
    root: Path
    data_dir: Path
    logs_dir: Path
    runtime_dir: Path
    database_path: Path
    marker_path: Path
    state_path: Path
    smoke_report_path: Path
    backend_log_path: Path
    backend_pid_path: Path

    def as_kwargs(self) -> dict:
        return dict(asdict(self))


def resolve_pilot_paths(root: str | os.PathLike[str] | None = None) -> PilotPaths:
    """Build the pilot layout. Nothing is created here."""
    resolved_root = Path(root).expanduser().resolve() if root else default_pilot_root()
    data_dir = resolved_root / "data"
    logs_dir = resolved_root / "logs"
    runtime_dir = resolved_root / "runtime"
    return PilotPaths(
        root=resolved_root,
        data_dir=data_dir,
        logs_dir=logs_dir,
        runtime_dir=runtime_dir,
        database_path=data_dir / PILOT_DATABASE_NAME,
        marker_path=runtime_dir / PILOT_MARKER_NAME,
        state_path=runtime_dir / "pilot-state.json",
        smoke_report_path=logs_dir / "smoke-report.json",
        backend_log_path=logs_dir / "backend.log",
        backend_pid_path=runtime_dir / "backend.pid",
    )


def default_pilot_root() -> Path:
    """Resolve %LOCALAPPDATA%\\EchoCast-AI\\local-pilot without hardcoding a user."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base).expanduser().resolve() / "EchoCast-AI" / "local-pilot"


# ---------------------------------------------------------------------------
# Safety boundary
# ---------------------------------------------------------------------------
def reject_protected_database(candidate: str | os.PathLike[str]) -> Path:
    """Refuse the protected application database before it can be opened."""
    resolved = Path(candidate).expanduser().resolve()
    protected = Path(PROTECTED_DATABASE_PATH).expanduser().resolve()
    if resolved == protected:
        raise ProtectedPathError(
            "the protected EchoCast database was refused; the pilot uses its own "
            "disposable database outside the repository"
        )
    try:
        if resolved.exists() and protected.exists() and os.path.samefile(resolved, protected):
            raise ProtectedPathError(
                "the supplied path is the protected EchoCast database; refused"
            )
    except ProtectedPathError:
        raise
    except OSError:
        pass
    return resolved


def validate_pilot_root(root: str | os.PathLike[str]) -> Path:
    """The pilot root must live outside the repository working tree."""
    resolved = Path(root).expanduser().resolve()
    repository = REPOSITORY_ROOT.resolve()
    if resolved == repository or repository in resolved.parents:
        raise PilotSafetyError(
            "the pilot root was refused because it is inside the repository; "
            "runtime artifacts must never live in Git"
        )
    if any(part == ".git" for part in resolved.parts):
        raise PilotSafetyError("the pilot root was refused because it is inside .git")
    return resolved


def require_pilot_password() -> str:
    """Read the operator-supplied, process-scoped pilot password."""
    value = os.environ.get(ADMIN_PASSWORD_ENV)
    if value is None or not value.strip():
        raise PilotCredentialError(
            f"{ADMIN_PASSWORD_ENV} is not set for this PowerShell session. "
            "Set a temporary pilot-only value before preparing or running the "
            "pilot. It is never stored by this tool."
        )
    return value


def _pilot_username() -> str:
    return os.environ.get(ADMIN_USERNAME_ENV) or "pilot-operator"


def _ensure_layout(paths: PilotPaths) -> None:
    for directory in (paths.root, paths.data_dir, paths.logs_dir, paths.runtime_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if not paths.marker_path.exists():
        paths.marker_path.write_text(
            "EchoCast local pilot directory.\n"
            "Disposable local test data only. Never production.\n",
            encoding="utf-8",
        )


def _require_marker(paths: PilotPaths) -> None:
    if not paths.marker_path.exists():
        raise PilotSafetyError(
            f"the {PILOT_MARKER_NAME} marker is missing, so this directory is not a "
            "confirmed pilot root; refusing to continue"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPOSITORY_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backend_import_path() -> None:
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


def _canonical_catalog():
    _backend_import_path()
    from store_catalog import CANONICAL_STORES, CANONICAL_ZONES

    return CANONICAL_STORES, CANONICAL_ZONES


def _pilot_environment(paths: PilotPaths, *, jwt_secret: str | None = None) -> dict:
    """Child-process environment. The password is passed through, never logged."""
    environment = os.environ.copy()
    environment[DB_PATH_ENV] = str(paths.database_path)
    environment[ADMIN_USERNAME_ENV] = _pilot_username()
    environment[ADMIN_PASSWORD_ENV] = require_pilot_password()
    environment[CORS_ORIGINS_ENV] = "http://localhost:3000"
    # Keep a pilot's DATA beside its database, not in the repository's own
    # data directory. Without this a load run wrote its broadcast recordings
    # into data/recordings/ - the same folder the live HQ uses - and left
    # orphan .part files there with no metadata row pointing at them. The
    # database was already scoped; the recordings were not.
    environment["ECHOCAST_DATA_DIR"] = str(paths.database_path.parent)
    environment[JWT_SECRET_ENV] = jwt_secret or environment.get(
        JWT_SECRET_ENV
    ) or secrets.token_urlsafe(48)
    return environment


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
def reset_pilot_database(paths: PilotPaths, *, confirmed: bool = False) -> list[Path]:
    """Remove only the scoped pilot database set. Never a wildcard delete."""
    if not confirmed:
        return []

    validate_pilot_root(paths.root)
    _require_marker(paths)
    reject_protected_database(paths.database_path)

    database_path = Path(paths.database_path).expanduser().resolve()
    data_dir = Path(paths.data_dir).expanduser().resolve()
    if data_dir not in database_path.parents:
        raise PilotSafetyError(
            "the pilot database path is outside the validated pilot data directory; "
            "refusing to remove it"
        )
    if database_path.name != PILOT_DATABASE_NAME:
        raise PilotSafetyError(
            "the target file is not the expected pilot database; refusing to remove it"
        )
    if database_path.is_symlink():
        raise PilotSafetyError("the pilot database path is a symlink; refusing to follow it")

    removed: list[Path] = []
    for candidate in (
        database_path,
        Path(str(database_path) + "-wal"),
        Path(str(database_path) + "-shm"),
    ):
        if candidate.is_symlink():
            raise PilotSafetyError("a pilot database sidecar is a symlink; refusing")
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate)
    return removed


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------
_INITIALISE_SCRIPT = """
import os, sys
from pathlib import Path
expected = Path(os.environ["ECHOCAST_DB_PATH"]).resolve()
import db
assert Path(db.DB_PATH).resolve() == expected, "pilot database path was not honoured"
import server
server.startup_event()
# Close pooled connections so SQLite checkpoints and removes the WAL/SHM
# sidecars, leaving a quiesced pilot snapshot behind.
db.engine.dispose()
print("PILOT_DB_INITIALISED")
"""


def _initialise_database(paths: PilotPaths) -> None:
    environment = _pilot_environment(paths)
    result = subprocess.run(
        [sys.executable, "-c", _INITIALISE_SCRIPT],
        cwd=str(BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0 or "PILOT_DB_INITIALISED" not in result.stdout:
        raise PilotStartupError(
            "the isolated pilot database could not be initialised using the "
            "application's own startup path"
        )


def checkpoint_pilot_database(paths: PilotPaths) -> bool:
    """Fold any WAL back into the pilot database and drop the sidecars.

    Windows ``terminate()`` is an abrupt kill, so a stopped backend can leave
    ``-wal``/``-shm`` behind. Those make the snapshot inconsistent, and the
    reconciliation tool correctly refuses such a snapshot, so the pilot
    database is quiesced here. This only ever runs against the pilot database,
    never the protected one.
    """
    database_path = reject_protected_database(paths.database_path)
    if not database_path.exists():
        return False
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # TRUNCATE empties the WAL but leaves the file in place. Switching the
        # journal mode removes the -wal/-shm files entirely, which is what makes
        # this a quiesced snapshot. The application re-enables WAL on its next
        # connect, so this changes no data and no lasting setting.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.commit()
    finally:
        connection.close()
    return True


def _database_facts(database_path: Path) -> dict:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            "SELECT store_code, region FROM stores ORDER BY id"
        ).fetchall()
    finally:
        connection.close()
    codes = [row[0] for row in rows]
    return {
        "store_count": len(rows),
        "zone_count": len({row[1] for row in rows}),
        "demo_codes_present": sorted(set(codes) & set(RETIRED_DEMO_STORE_CODES)),
    }


def _reconcile(database_path: Path) -> str:
    _backend_import_path()
    from store_catalog_reconciliation import reconcile

    return reconcile(database_path).overall_result


def prepare(paths: PilotPaths) -> dict:
    """Create and validate the isolated pilot database. Idempotent."""
    validate_pilot_root(paths.root)
    reject_protected_database(paths.database_path)
    require_pilot_password()
    _ensure_layout(paths)

    already_prepared = paths.database_path.exists()
    if not already_prepared:
        _initialise_database(paths)
    checkpoint_pilot_database(paths)

    facts = _database_facts(paths.database_path)
    canonical_stores, canonical_zones = _canonical_catalog()

    if facts["store_count"] != len(canonical_stores):
        raise PilotSmokeError(
            f"the pilot database holds {facts['store_count']} Stores; "
            f"{len(canonical_stores)} were expected"
        )
    if facts["zone_count"] != len(canonical_zones):
        raise PilotSmokeError(
            f"the pilot database represents {facts['zone_count']} Zones; "
            f"{len(canonical_zones)} were expected"
        )
    if facts["demo_codes_present"]:
        raise PilotSmokeError(
            "the pilot database contains retired demo Store codes: "
            + ", ".join(facts["demo_codes_present"])
        )

    reconciliation = _reconcile(paths.database_path)
    if reconciliation != "EXACT_CANONICAL_MATCH":
        raise PilotSmokeError(
            f"the pilot database did not reconcile exactly: {reconciliation}"
        )

    state = {
        "prepared_at_utc": _utc_now(),
        "git_commit": _git_commit(),
        "database_path": str(paths.database_path),
        "database_sha256": _sha256(paths.database_path),
        "store_count": facts["store_count"],
        "zone_count": facts["zone_count"],
        "demo_codes_present": facts["demo_codes_present"],
        "reconciliation": reconciliation,
        "canonical_source": "backend/store_catalog.py",
        "readiness_scope": READINESS_SCOPE,
        "already_prepared": already_prepared,
    }
    paths.state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
    )
    return state


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------
def _free_loopback_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _select_store(database_path: Path) -> tuple[str, str]:
    """Return (store_code, receiver_token). The credential stays in memory."""
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute(
            "SELECT store_code, receiver_token FROM stores WHERE store_code = ?",
            (PREFERRED_PILOT_STORE_CODE,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                "SELECT store_code, receiver_token FROM stores ORDER BY id LIMIT 1"
            ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise PilotSmokeError("the pilot database contains no Store to test with")
    return row[0], row[1]


async def _receiver_checks(base_url: str, token: str) -> dict:
    import websockets

    ws_url = base_url.replace("http://", "ws://") + "/api/ws/receiver"
    outcome = {"receiver_auth": "failed", "query_token_refused": False}

    # A valid credential in the required Authorization header must be accepted.
    connection = await websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
    )
    outcome["receiver_auth"] = "ok"
    outcome["_connection"] = connection
    return outcome


async def _query_token_is_refused(base_url: str) -> bool:
    """Query-string credentials must be refused by the production endpoint.

    A deliberately fake value is used: Uvicorn logs request URLs, so a real
    Store credential must never appear in a URL, even in a negative test.
    """
    import websockets

    ws_url = (
        base_url.replace("http://", "ws://")
        + f"/api/ws/receiver?token={QUERY_TOKEN_PROBE}"
    )
    try:
        connection = await websockets.connect(ws_url, open_timeout=10)
    except Exception:
        return True
    try:
        await asyncio.wait_for(connection.wait_closed(), timeout=5)
    except asyncio.TimeoutError:
        await connection.close()
        return False
    return True


def _api_get(base_url: str, path: str, token: str):
    import requests

    response = requests.get(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    if response.status_code != 200:
        raise PilotSmokeError(f"GET {path} returned HTTP {response.status_code}")
    return response.json()


def _wait_for_store_status(base_url: str, token: str, code: str, expected: str) -> bool:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        for store in _api_get(base_url, "/api/stores", token):
            if store["store_code"] == code and store["status"] == expected:
                return True
        time.sleep(0.2)
    return False


def _stop_backend(process, paths: PilotPaths) -> str:
    if process.poll() is not None:
        return "ok"
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return "ok"
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return "failed"
        return "forced"
    finally:
        if paths.backend_pid_path.exists():
            paths.backend_pid_path.unlink()


def smoke(paths: PilotPaths) -> dict:
    """Run the loopback-only local pilot smoke test end to end."""
    import requests

    validate_pilot_root(paths.root)
    _require_marker(paths)
    reject_protected_database(paths.database_path)
    require_pilot_password()
    if not paths.database_path.exists():
        raise PilotStartupError("the pilot database is not prepared; run 'prepare' first")

    canonical_stores, canonical_zones = _canonical_catalog()
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = _pilot_environment(paths)
    started_at = _utc_now()

    result: dict = {
        "started_at_utc": started_at,
        "git_commit": _git_commit(),
        "pilot_database_path": str(paths.database_path),
        "pilot_database_sha256": _sha256(paths.database_path),
        "backend_host": "127.0.0.1",
        "backend_port": port,
        "backend_url": base_url,
        "uvicorn_workers": 1,
        "liveness": "failed",
        "login": "failed",
        "store_count": 0,
        "zone_count": 0,
        "demo_codes_present": [],
        "selected_store_code": None,
        "receiver_auth": "failed",
        "query_token_refused": False,
        "observed_connection": "NOT_REPORTED",
        "observed_readiness": "NOT_REPORTED",
        "observed_playback": "NOT_REPORTED",
        "observed_acoustic": "NOT_REPORTED",
        "speaker_verified": False,
        "receiver_cleanup": "failed",
        "shutdown": "failed",
        "backend_process_running": True,
        "readiness_scope": READINESS_SCOPE,
        "overall_result": "LOCAL_PILOT_SMOKE_FAILED",
    }

    log_handle = paths.backend_log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "server:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--workers", "1",
        ],
        cwd=str(BACKEND_DIR),
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    paths.backend_pid_path.write_text(str(process.pid), encoding="utf-8")

    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise PilotStartupError("the pilot backend exited during startup")
            try:
                if requests.get(f"{base_url}{LIVENESS_PATH}", timeout=1).status_code == 200:
                    result["liveness"] = "ok"
                    break
            except requests.RequestException:
                time.sleep(0.2)
        else:
            raise PilotStartupError("the pilot backend did not become live in time")

        login = requests.post(
            f"{base_url}/api/auth/login",
            json={
                "username": _pilot_username(),
                "password": require_pilot_password(),
            },
            timeout=10,
        )
        if login.status_code != 200:
            raise PilotSmokeError(f"pilot login failed with HTTP {login.status_code}")
        access_token = login.json()["access_token"]
        result["login"] = "ok"

        stores = _api_get(base_url, "/api/stores", access_token)
        codes = [store["store_code"] for store in stores]
        result["store_count"] = len(stores)
        result["demo_codes_present"] = sorted(set(codes) & set(RETIRED_DEMO_STORE_CODES))
        if len(stores) != len(canonical_stores):
            raise PilotSmokeError(
                f"the Store API returned {len(stores)} Stores; "
                f"{len(canonical_stores)} were expected"
            )
        if result["demo_codes_present"]:
            raise PilotSmokeError("retired demo Store codes are present in the Store API")

        meta = _api_get(base_url, "/api/stores/meta/regions-cities", access_token)
        result["zone_count"] = len(meta["regions"])
        if result["zone_count"] != len(canonical_zones):
            raise PilotSmokeError(
                f"the Zone metadata returned {result['zone_count']} Zones; "
                f"{len(canonical_zones)} were expected"
            )

        store_code, receiver_token = _select_store(paths.database_path)
        result["selected_store_code"] = store_code

        async def _receiver_flow() -> dict:
            outcome = await _receiver_checks(base_url, receiver_token)
            connection = outcome.pop("_connection")
            try:
                outcome["query_token_refused"] = await _query_token_is_refused(base_url)
                await asyncio.sleep(0.4)
            finally:
                await connection.close()
                await connection.wait_closed()
            return outcome

        receiver_outcome = asyncio.run(_receiver_flow())
        result["receiver_auth"] = receiver_outcome["receiver_auth"]
        result["query_token_refused"] = receiver_outcome["query_token_refused"]
        if result["receiver_auth"] != "ok":
            raise PilotSmokeError("the Receiver WebSocket did not accept the Bearer header")
        if not result["query_token_refused"]:
            raise PilotSmokeError("a query-string credential was not refused")

        # The Store reached CONNECTED while the socket was open. Readiness,
        # playback and acoustic state are never claimed: the pilot sends no
        # such acknowledgement and the API exposes no such value.
        result["observed_connection"] = "CONNECTED"
        result["observed_readiness"] = "NOT_REPORTED"
        result["observed_playback"] = "NOT_REPORTED"
        result["observed_acoustic"] = "NOT_REPORTED"
        result["speaker_verified"] = False

        if _wait_for_store_status(base_url, access_token, store_code, "offline"):
            result["receiver_cleanup"] = "ok"
        else:
            raise PilotSmokeError("the Receiver disconnect cleanup did not complete")

        result["overall_result"] = "LOCAL_PILOT_SMOKE_PASSED"
    finally:
        result["shutdown"] = _stop_backend(process, paths)
        result["backend_process_running"] = process.poll() is None
        log_handle.close()
        try:
            checkpoint_pilot_database(paths)
        except (sqlite3.Error, OSError):
            # Leaving a WAL behind is not a smoke failure; the next prepare or
            # status run will report the snapshot as unsafe rather than guess.
            pass
        result["ended_at_utc"] = _utc_now()
        paths.smoke_report_path.write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
        )

    if result["shutdown"] == "failed":
        raise PilotError("the pilot backend did not shut down cleanly")
    return result


def _connected_check(base_url: str, token: str, code: str) -> bool:
    return _wait_for_store_status(base_url, token, code, "online")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local_pilot",
        description=(
            "Local one-Store pilot readiness harness. Uses an isolated pilot "
            "database outside the repository and never touches the protected "
            "application database."
        ),
    )
    parser.add_argument(
        "action", choices=("prepare", "smoke", "status", "reset"),
    )
    parser.add_argument(
        "--pilot-root",
        default=None,
        help="Pilot root directory. Defaults to %%LOCALAPPDATA%%\\EchoCast-AI\\local-pilot.",
    )
    parser.add_argument(
        "--reset-pilot-db",
        action="store_true",
        help="Explicitly confirm removal of the scoped pilot database set.",
    )
    return parser


def _print_state(state: dict) -> None:
    for key in sorted(state):
        print(f"  {key}: {state[key]}")


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        paths = resolve_pilot_paths(arguments.pilot_root)

        if arguments.action == "prepare":
            state = prepare(paths)
            print("Local pilot database prepared (isolated, disposable).")
            _print_state(state)
            return EXIT_OK

        if arguments.action == "status":
            validate_pilot_root(paths.root)
            if not paths.state_path.exists():
                print("The local pilot is not prepared yet.", file=sys.stderr)
                return EXIT_SAFETY
            print("Local pilot status:")
            _print_state(json.loads(paths.state_path.read_text(encoding="utf-8")))
            return EXIT_OK

        if arguments.action == "reset":
            if not arguments.reset_pilot_db:
                print(
                    "Refused: pass --reset-pilot-db to confirm removal of the "
                    "scoped pilot database set.",
                    file=sys.stderr,
                )
                return EXIT_SAFETY
            removed = reset_pilot_database(paths, confirmed=True)
            if removed:
                print("Removed the scoped pilot database set:")
                for item in removed:
                    print(f"  {item}")
            else:
                print("Nothing to remove; the pilot database is already absent.")
            return EXIT_OK

        result = smoke(paths)
        print(f"Local pilot smoke: {result['overall_result']}")
        _print_state(result)
        return EXIT_OK if result["overall_result"] == "LOCAL_PILOT_SMOKE_PASSED" else EXIT_SMOKE_FAILED

    except (ProtectedPathError, PilotSafetyError, PilotCredentialError, PilotStartupError) as error:
        print(f"Local pilot refused: {error}", file=sys.stderr)
        return EXIT_SAFETY
    except PilotSmokeError as error:
        print(f"Local pilot smoke assertion failed: {error}", file=sys.stderr)
        return EXIT_SMOKE_FAILED
    except PilotError as error:
        print(f"Local pilot cleanup failure: {error}", file=sys.stderr)
        return EXIT_CLEANUP_FAILED


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
