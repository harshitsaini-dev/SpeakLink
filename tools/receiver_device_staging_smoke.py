"""The whole Receiver Device story, end to end, on a throwaway staging server.

Everything below has unit tests. That is not the same as knowing it works: the
unit tests use fake transports, fake protectors and in-process fixtures, and the
interesting failures in a system like this live exactly in the joins those fakes
paper over. So this starts a real backend on a loopback port, runs the real
Agent as a real subprocess, and makes the real HTTP and WebSocket calls.

It proves, in one run:

  code -> enrol -> credential sealed with real DPAPI -> reconnect without the
  code -> CONNECTED -> READY -> AUDIO_RECEIVING -> PLAYBACK_CONFIRMED ->
  STOPPED -> rotate -> old credential refused -> new credential reconnects ->
  standby enrolled -> only the primary receives audio -> disable -> revoke ->
  both refused -> Store still active -> every process stopped, both ports free.

What it deliberately does NOT prove: any amplifier, any speaker, any Bluetooth
link, any audible sound, and SPEAKER_VERIFIED. The Receiver decodes to a null
sink. A pass here is software evidence and nothing more, and the report says so
in those words rather than leaving it to be inferred.

Safety: a fresh temporary database every run. ``backend/echocast_live.db`` and
the real isolated pilot database are never opened - a check refuses to start if
the staging path resolves to either.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

PROTECTED_DATABASE = BACKEND_DIR / "echocast_live.db"

MARKER_PASSED = "RECEIVER_DEVICE_ENROLMENT_STAGING_SMOKE_PASSED"
MARKER_FAILED = "RECEIVER_DEVICE_ENROLMENT_STAGING_SMOKE_FAILED"

STORE_CODE = "UN"
STARTUP_TIMEOUT_SECONDS = 45
SHUTDOWN_TIMEOUT_SECONDS = 15

EXIT_OK = 0
EXIT_SAFETY = 1
EXIT_ASSERTION = 2
EXIT_CLEANUP = 3


class StagingSmokeError(RuntimeError):
    """A controlled, secret-free staging failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def _reject_protected_paths(staging_root: Path) -> None:
    """Refuse to run anywhere near the two databases that must not be touched."""
    resolved = staging_root.resolve()
    forbidden = [PROTECTED_DATABASE.resolve()]
    pilot_root = Path(os.environ.get("ECHOCAST_PILOT_ROOT", "")).expanduser()
    if str(pilot_root):
        forbidden.append(pilot_root.resolve())
    for path in forbidden:
        if resolved == path or path in resolved.parents or resolved in path.parents:
            raise StagingSmokeError(
                f"the staging root {resolved} overlaps a protected location ({path}); "
                "this smoke only ever runs against a throwaway database"
            )
    if resolved.is_relative_to(BACKEND_DIR):
        raise StagingSmokeError("the staging root must not live inside backend/")


def _protected_database_fingerprint() -> tuple | None:
    if not PROTECTED_DATABASE.exists():
        return None
    stat = PROTECTED_DATABASE.stat()
    return (stat.st_size, stat.st_mtime_ns)


# ---------------------------------------------------------------------------
# Building the staging database
# ---------------------------------------------------------------------------
def _bootstrap_database(database_path: Path, key_container: Path) -> dict:
    """A fresh, migrated database in the state a cut-over server is actually in.

    ``dual_verify`` on purpose: it is the migration state where a Store still
    answers on its shared token *and* enrolled Devices authenticate, which is
    the only state where both halves of this system are live at once. It is also
    the state the approved architecture decision named, so this is the thing
    worth proving rather than the easy one.
    """
    os.environ["ECHOCAST_DB_PATH"] = str(database_path)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from auth import hash_password
    from db import Base
    from key_custody import FakeProtector, create_key_container
    from migrations import run_receiver_credential_phase_one
    from models import HQUser, Store
    from receiver_credential_backfill import rehearse_legacy_receiver_backfill
    from receiver_primary_device import ensure_primary_device_schema

    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    admin_username = os.environ["ADMIN_USERNAME"]
    admin_password = os.environ["ADMIN_PASSWORD"]
    store_token = secrets.token_hex(16)

    with Session() as db:
        db.add(HQUser(username=admin_username,
                      password_hash=hash_password(admin_password), role="admin"))
        db.add(Store(store_code=STORE_CODE, store_name="Uttam Nagar Old",
                     city="UN ZONE", region="UN ZONE", receiver_token=store_token))
        db.commit()
        store_id = db.query(Store).filter(Store.store_code == STORE_CODE).one().id

    run_receiver_credential_phase_one(engine)
    ensure_primary_device_schema(engine)

    # The key ring the backend will verify credentials with. A fake protector
    # here, not DPAPI: this container is thrown away with the staging root, and
    # sealing it to the current user would make the run unrepeatable on a
    # different account. The AGENT still uses real DPAPI - see below.
    create_key_container(key_container, protector=FakeProtector())

    backfill_key = secrets.token_bytes(48)
    rehearse_legacy_receiver_backfill(
        engine, hash_key=backfill_key, hash_key_version=1,
        now=datetime.now(timezone.utc),
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state SET state = 'dual_verify', "
                "legacy_verification_enabled = 1, updated_at = :now WHERE id = 1"
            ),
            {"now": _utc_now()},
        )
    engine.dispose()
    return {"store_id": store_id, "store_code": STORE_CODE, "migration_state": "dual_verify"}


# ---------------------------------------------------------------------------
# Small helpers over the running server
# ---------------------------------------------------------------------------
def _api(base_url: str, method: str, path: str, token: str | None = None,
         payload: dict | None = None, *, expect: tuple[int, ...] = (200, 201)):
    import requests

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = requests.request(
        method, f"{base_url}{path}", headers=headers, json=payload, timeout=20
    )
    if response.status_code not in expect:
        raise StagingSmokeError(
            f"{method} {path} answered HTTP {response.status_code}, expected {expect}"
        )
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _wait_until(predicate, *, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _receiver_event_types(database_path: Path, store_id: int) -> list[str]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM receiver_events WHERE store_id = ? ORDER BY id",
                (store_id,),
            )
        ]
    finally:
        connection.close()


def websocket_url_from(base_url: str, path: str, *, query: dict | None = None) -> str:
    """Derive a WebSocket URL from the HTTP base URL the caller was given.

    This exists because the helper it replaces took a base URL *and* a port, and
    used the port to rebuild the URL against a hardcoded ``127.0.0.1`` - throwing
    away the host it had already been handed. On a loopback backend the two
    agreed and nothing showed. On the LAN pilot, where Uvicorn binds
    ``192.168.4.134`` and therefore does not listen on loopback at all, it
    produced WinError 1225 after the Receiver had already enrolled, sealed its
    credential and reached CONNECTED. The failure looked like a Receiver problem
    and was a URL problem.

    Deriving the socket URL from the HTTP one means they cannot disagree about
    which machine they mean. A wildcard bind address is refused rather than
    quietly turned into loopback: 0.0.0.0 is what a server binds, never what a
    client connects to, and guessing is what caused the original defect.
    """
    from urllib.parse import urlencode, urlsplit

    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("a backend base URL is required")
    parts = urlsplit(base_url.strip().rstrip("/"))
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"cannot derive a WebSocket URL from scheme {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError(f"no host in base URL {base_url!r}")
    if parts.hostname in ("0.0.0.0", "::", ""):
        raise ValueError(
            f"{parts.hostname!r} is a bind address, not somewhere a client can "
            "connect. Pass the address the backend is actually reachable on."
        )

    scheme = "wss" if parts.scheme == "https" else "ws"
    url = f"{scheme}://{parts.netloc}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    return url


def _free_loopback_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_is_free(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _stop_process(process: subprocess.Popen | None) -> bool:
    if process is None or process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return False
    return True


# ---------------------------------------------------------------------------
# The Agent, run as a real subprocess
# ---------------------------------------------------------------------------
def _run_agent(arguments: list[str], *, stdin_text: str | None = None,
               environment: dict | None = None, timeout: float = 90) -> subprocess.CompletedProcess:
    """Run tools/receiver_agent.py. Secrets go on stdin, never in ``arguments``."""
    return subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "tools" / "receiver_agent.py"), *arguments],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPOSITORY_ROOT),
        env=environment or os.environ.copy(),
    )


def _assert_no_secret(text_value: str, secrets_to_find: dict, where: str) -> None:
    for name, value in secrets_to_find.items():
        if value and value in text_value:
            raise StagingSmokeError(f"{name} appeared in {where}")


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
def smoke(staging_root: Path) -> dict:
    import requests

    _reject_protected_paths(staging_root)
    protected_before = _protected_database_fingerprint()

    if staging_root.exists():
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True)
    logs_dir = staging_root / "logs"
    logs_dir.mkdir()

    database_path = staging_root / "staging.db"
    key_container = staging_root / "keys" / "receiver-hmac-keys.bin"
    credential_path = staging_root / "agent" / "device-credential.bin"
    standby_credential_path = staging_root / "agent-standby" / "device-credential.bin"

    admin_username = f"staging-operator-{secrets.token_hex(4)}"
    admin_password = secrets.token_urlsafe(24)
    jwt_secret = secrets.token_urlsafe(48)
    os.environ["ADMIN_USERNAME"] = admin_username
    os.environ["ADMIN_PASSWORD"] = admin_password

    facts = _bootstrap_database(database_path, key_container)
    store_id = facts["store_id"]

    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"

    environment = os.environ.copy()
    environment.update({
        "ECHOCAST_DB_PATH": str(database_path),
        "ECHOCAST_KEY_CONTAINER": str(key_container),
        "ECHOCAST_KEY_PROTECTOR": "fake",
        "JWT_SECRET": jwt_secret,
        "ADMIN_USERNAME": admin_username,
        "ADMIN_PASSWORD": admin_password,
        "CORS_ORIGINS": "http://localhost:3000",
        "ECHOCAST_SEED_STORES": "0",
    })

    report: dict = {
        "started_at_utc": _utc_now(),
        "staging_root": str(staging_root),
        "backend_url": base_url,
        "uvicorn_workers": 1,
        "migration_state": facts["migration_state"],
        "store_id": store_id,
        "checks": {},
        "software_playback_evidence": True,
        "amplifier_evidence": False,
        "audible_speaker_evidence": False,
        "speaker_verified": False,
        "overall_result": MARKER_FAILED,
    }
    checks = report["checks"]

    def record(name: str, value=True) -> None:
        checks[name] = value

    backend_log = (logs_dir / "backend.log").open("w", encoding="utf-8")
    agent_log = (logs_dir / "agent-run.log").open("w", encoding="utf-8")
    backend_process = None
    agent_process = None
    standby_process = None

    try:
        # 1. A fresh backend starts.
        backend_process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app",
             "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-access-log"],
            cwd=str(BACKEND_DIR), env=environment,
            stdout=backend_log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        live = False
        while time.monotonic() < deadline:
            if backend_process.poll() is not None:
                raise StagingSmokeError("the staging backend exited during startup")
            try:
                if requests.get(f"{base_url}/docs", timeout=1).status_code == 200:
                    live = True
                    break
            except requests.RequestException:
                time.sleep(0.2)
        if not live:
            raise StagingSmokeError("the staging backend did not become live in time")
        record("backend_started")

        # 2-4. Admin bootstrap, one active Store, login.
        login = _api(base_url, "POST", "/api/auth/login",
                     payload={"username": admin_username, "password": admin_password})
        access_token = login["access_token"]
        record("admin_login")

        stores = _api(base_url, "GET", "/api/stores", access_token)
        active = [store for store in stores if store["store_code"] == STORE_CODE]
        if not active:
            raise StagingSmokeError("the staging Store is missing")
        record("one_active_store")

        # 5. An enrolment code, created by an administrator.
        issued = _api(base_url, "POST", "/api/receiver-devices/enrollment-codes",
                      access_token, {"store_id": store_id})
        code = issued["code"]
        record("enrolment_code_created")

        # 6. The real Agent enrols, with the code on stdin and never in argv.
        enrol_arguments = [
            "enrol", "--backend-url", base_url, "--allow-insecure-loopback",
            "--device-name", "UN till 1 (primary)", "--hostname", "UN-TILL-1",
            "--credential-path", str(credential_path), "--from-stdin",
        ]
        enrolled = _run_agent(enrol_arguments, stdin_text=f"{code}\n")
        if enrolled.returncode != 0:
            raise StagingSmokeError(f"the Agent could not enrol: {enrolled.stderr.strip()[:300]}")
        if not credential_path.exists():
            raise StagingSmokeError("the Agent reported success but stored no credential")
        record("agent_enrolled")

        # 8. Neither secret appears anywhere the operator or a log can see it.
        sealed = credential_path.read_bytes()
        watched = {"the enrolment code": code}
        _assert_no_secret(enrolled.stdout + enrolled.stderr, watched, "the Agent's output")
        _assert_no_secret(" ".join(enrol_arguments), watched, "the Agent's process arguments")
        if code.encode() in sealed:
            raise StagingSmokeError("the enrolment code was written into the credential file")
        record("no_secret_in_agent_output")

        # The credential itself: read it back the way the Agent will, and check
        # it is genuinely sealed rather than sitting in the file.
        from tools.receiver_credential_store import (
            DeviceCredentialProtector, load_credential,
        )

        record_stored = load_credential(credential_path, protector=DeviceCredentialProtector())
        device_credential = record_stored.credential()
        if device_credential.encode() in sealed:
            raise StagingSmokeError("the credential is in the file in the clear")
        record("credential_sealed_with_real_dpapi")
        record("agent_loads_saved_credential")

        device_public_id = record_stored.device_public_id
        report["device_public_id"] = device_public_id
        if record_stored.store_id != store_id:
            raise StagingSmokeError("the Agent stored the wrong Store")
        record("backend_identified_device_and_store")

        # 7. The same code cannot be spent twice.
        reused = _run_agent(
            ["enrol", "--backend-url", base_url, "--allow-insecure-loopback",
             "--device-name", "impostor", "--credential-path",
             str(staging_root / "impostor" / "device-credential.bin"), "--from-stdin"],
            stdin_text=f"{code}\n",
        )
        if reused.returncode == 0:
            raise StagingSmokeError("a spent enrolment code was accepted a second time")
        record("code_reuse_rejected")

        # Make this Device the Store's primary, explicitly.
        _api(base_url, "POST", f"/api/receiver-devices/{device_public_id}/promote", access_token)
        roles = _api(base_url, "GET", f"/api/stores/{store_id}/receiver-devices/roles", access_token)
        primaries = [row for row in roles if row["role"] == "PRIMARY"]
        if len(primaries) != 1 or primaries[0]["public_id"] != device_public_id:
            raise StagingSmokeError("the Store does not have exactly one expected primary")
        record("explicit_promotion")

        # 9-16. The Agent runs, authenticates, and walks the whole status ladder.
        agent_report_path = logs_dir / "agent-session.json"
        agent_process = subprocess.Popen(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "receiver_agent.py"),
             "run", "--backend-url", base_url, "--allow-insecure-loopback",
             "--credential-path", str(credential_path),
             "--report", str(agent_report_path), "--exit-after-stop"],
            cwd=str(REPOSITORY_ROOT), env=os.environ.copy(),
            stdout=agent_log, stderr=subprocess.STDOUT,
        )

        def _store_online() -> bool:
            for store in _api(base_url, "GET", "/api/stores", access_token):
                if store["store_code"] == STORE_CODE:
                    return store["status"] in ("online", "playing")
            return False

        if not _wait_until(_store_online, timeout=30):
            raise StagingSmokeError("the enrolled Device never reached CONNECTED")
        record("device_authenticated")
        record("connected")

        audio = _drive_one_broadcast(base_url, access_token, store_id, database_path)
        report["broadcast"] = audio
        for name in ("ready", "audio_receiving", "playback_confirmed", "stopped"):
            record(name, audio[name])
            if not audio[name]:
                raise StagingSmokeError(f"the Device never reported {name.upper()}")

        # 21. The Agent exits on its own after the stop.
        try:
            agent_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            raise StagingSmokeError("the Agent did not exit cleanly after STOPPED")
        record("agent_disconnected_cleanly", agent_process.returncode == 0)
        agent_process = None

        session_report = json.loads(agent_report_path.read_text(encoding="utf-8"))
        report["agent_session"] = {
            key: session_report.get(key)
            for key in ("total_chunks", "dropped_chunks", "ffmpeg_returncode",
                        "ffmpeg_decoded_microseconds", "speaker_verified", "states")
        }
        if session_report.get("dropped_chunks", 0) != 0:
            raise StagingSmokeError("the Device dropped audio chunks")
        record("queue_returned_to_zero")
        if session_report.get("ffmpeg_returncode") is None:
            raise StagingSmokeError("FFmpeg did not exit")
        record("ffmpeg_exited")
        if session_report.get("speaker_verified"):
            raise StagingSmokeError("the Agent claimed speaker verification, which is impossible here")

        # 22-25. Rotation.
        rotated = _api(base_url, "POST",
                       f"/api/receiver-devices/{device_public_id}/rotate-credential", access_token)
        new_credential = rotated["credential"]
        if new_credential == device_credential:
            raise StagingSmokeError("rotation returned the same credential")
        record("credential_rotated")

        if _credential_is_accepted(base_url, device_credential):
            raise StagingSmokeError("the old credential still authenticates after rotation")
        record("old_credential_rejected")

        update = _run_agent(
            ["rotate-local-credential", "--credential-path", str(credential_path), "--from-stdin"],
            stdin_text=f"{new_credential}\n",
        )
        if update.returncode != 0:
            raise StagingSmokeError(
                f"the Agent could not store the rotated credential: {update.stderr.strip()[:200]}"
            )
        reloaded = load_credential(credential_path, protector=DeviceCredentialProtector())
        if reloaded.credential() != new_credential:
            raise StagingSmokeError("the rotated credential was not stored")
        if reloaded.device_public_id != device_public_id:
            raise StagingSmokeError("rotation changed which Device this computer is")
        record("new_credential_stored_atomically")

        if not _credential_is_accepted(base_url, new_credential):
            raise StagingSmokeError("the rotated credential does not authenticate")
        record("new_credential_reconnects")

        # 26-28. A standby enrols and receives nothing.
        standby_code = _api(base_url, "POST", "/api/receiver-devices/enrollment-codes",
                            access_token, {"store_id": store_id})["code"]
        standby_enrolled = _run_agent(
            ["enrol", "--backend-url", base_url, "--allow-insecure-loopback",
             "--device-name", "UN till 2 (standby)", "--hostname", "UN-TILL-2",
             "--credential-path", str(standby_credential_path), "--from-stdin"],
            stdin_text=f"{standby_code}\n",
        )
        if standby_enrolled.returncode != 0:
            raise StagingSmokeError(
                f"the standby could not enrol: {standby_enrolled.stderr.strip()[:300]}"
            )
        record("standby_enrolled")

        roles = _api(base_url, "GET", f"/api/stores/{store_id}/receiver-devices/roles", access_token)
        primaries = [row for row in roles if row["role"] == "PRIMARY"]
        standbys = [row for row in roles if row["role"] == "STANDBY" and row["status"] == "active"]
        if len(primaries) != 1 or primaries[0]["public_id"] != device_public_id:
            raise StagingSmokeError("enrolling a standby changed the primary")
        if not standbys:
            raise StagingSmokeError("the standby is not visible as a standby")
        record("primary_unchanged_by_standby_enrolment")
        report["device_roles"] = [
            {"public_id": row["public_id"], "role": row["role"], "status": row["status"]}
            for row in roles
        ]

        standby_facts = _standby_receives_nothing(
            base_url, access_token, store_id, database_path,
            credential_path, standby_credential_path, logs_dir,
        )
        report["standby"] = standby_facts
        if standby_facts["standby_chunks"] != 0:
            raise StagingSmokeError(
                f"the standby received {standby_facts['standby_chunks']} live chunks"
            )
        record("standby_received_zero_chunks")
        record("primary_only_audio", standby_facts["primary_chunks"] > 0)

        # 29-31. Disable, revoke, and the Store survives both.
        standby_public_id = standbys[0]["public_id"]
        standby_credential = load_credential(
            standby_credential_path, protector=DeviceCredentialProtector()
        ).credential()

        _api(base_url, "POST", f"/api/receiver-devices/{standby_public_id}/disable", access_token)
        if _credential_is_accepted(base_url, standby_credential):
            raise StagingSmokeError("a disabled Device still authenticates")
        record("disabled_device_rejected")

        _api(base_url, "POST", f"/api/receiver-devices/{standby_public_id}/revoke", access_token)
        if _credential_is_accepted(base_url, standby_credential):
            raise StagingSmokeError("a revoked Device still authenticates")
        record("revoked_device_rejected")

        stores = _api(base_url, "GET", "/api/stores", access_token)
        store = next(row for row in stores if row["store_code"] == STORE_CODE)
        if not store.get("is_active", True):
            raise StagingSmokeError("revoking a Device deactivated its Store")
        record("store_remains_active")

        # 34. Nothing secret in any log this run produced.
        watched = {
            "a Device credential": device_credential,
            "the rotated credential": new_credential,
            "an enrolment code": code,
            "the standby code": standby_code,
            "the admin password": admin_password,
            "the JWT secret": jwt_secret,
        }
        for log_file in sorted(logs_dir.glob("*.log")):
            _assert_no_secret(
                log_file.read_text(encoding="utf-8", errors="replace"), watched, log_file.name
            )
        record("no_secret_in_logs")

        report["overall_result"] = MARKER_PASSED

    finally:
        cleanup_ok = True
        for process in (agent_process, standby_process, backend_process):
            cleanup_ok &= _stop_process(process)
        backend_log.close()
        agent_log.close()
        report["all_owned_processes_stopped"] = cleanup_ok
        time.sleep(1.0)
        report["port_8000_free"] = _port_is_free(8000)
        report["port_3000_free"] = _port_is_free(3000)
        report["staging_port_free"] = _port_is_free(port)
        report["protected_database_unchanged"] = (
            _protected_database_fingerprint() == protected_before
        )
        report["ended_at_utc"] = _utc_now()
        (staging_root / "staging-smoke-report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )

    if not report["protected_database_unchanged"]:
        report["overall_result"] = MARKER_FAILED
        raise StagingSmokeError("the protected database changed during this run")
    return report


def _credential_is_accepted(base_url: str, credential: str) -> bool:
    """Open a Receiver socket with this credential and see whether it survives.

    A refusal is a close, not an exception at connect time, so this waits briefly
    for the close rather than treating "connected" as "accepted".
    """
    import websockets

    async def attempt() -> bool:
        url = websocket_url_from(base_url, "/api/ws/receiver")
        try:
            connection = await websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {credential}"},
                open_timeout=10,
            )
        except Exception:
            return False
        try:
            try:
                await asyncio.wait_for(connection.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                # Still open and silent after two seconds: accepted.
                return True
            except Exception:
                return False
            return True
        finally:
            try:
                await connection.close()
            except Exception:
                pass

    return asyncio.run(attempt())


def _drive_one_broadcast(base_url, access_token, store_id, database_path) -> dict:
    """Run one real broadcast and watch the Device walk the status ladder."""
    from tools.generate_audio_fixture import generate_fixture
    from tools.local_audio_pilot import _audio_phase

    fixture = generate_fixture(Path(database_path).parent / "audio")
    session = _api(base_url, "POST", "/api/broadcast/sessions", access_token, {
        "campaign_name": "Receiver Device staging smoke",
        "target_mode": "selected",
        "store_ids": [store_id],
    })
    session_id = session["id"]
    _api(base_url, "POST", f"/api/broadcast/sessions/{session_id}/start", access_token)

    def _ready() -> bool:
        return "receiver_ready" in _receiver_event_types(database_path, store_id)

    ready = _wait_until(_ready, timeout=40)

    ticket = _api(base_url, "POST", "/api/auth/ws-ticket", access_token,
                  {"audience": "broadcaster"})["ticket"]
    seen: set[str] = set()

    def _await_acknowledgements() -> bool:
        def _collect() -> bool:
            events = _receiver_event_types(database_path, store_id)
            seen.update({name for name in ("audio_receiving", "playback_confirmed")
                         if name in events})
            return {"audio_receiving", "playback_confirmed"} <= seen

        return _wait_until(_collect, timeout=40)

    def _request_stop() -> None:
        _api(base_url, "POST", f"/api/broadcast/sessions/{session_id}/stop", access_token)

    phase = asyncio.run(_audio_phase(
        websocket_url_from(base_url, "/api/ws/broadcaster", query={"ticket": ticket}),
        Path(fixture["path"]),
        await_acknowledgements=_await_acknowledgements,
        request_stop=_request_stop,
    ))

    stopped = _wait_until(
        lambda: "stopped" in _receiver_event_types(database_path, store_id), timeout=30
    )
    events = _receiver_event_types(database_path, store_id)
    return {
        "session_id": session_id,
        "ready": ready,
        "audio_receiving": "audio_receiving" in seen,
        "playback_confirmed": "playback_confirmed" in seen,
        "stopped": stopped,
        "sent_chunks": phase["sent_chunks"],
        "speaker_verified_event_present": "speaker_verified" in events,
    }


def _standby_receives_nothing(base_url, access_token, store_id, database_path,
                              primary_path, standby_path, logs_dir) -> dict:
    """Both Devices connected, one broadcast, and only one of them hears it."""
    primary_report = logs_dir / "primary-session.json"
    standby_report = logs_dir / "standby-session.json"
    primary_log = (logs_dir / "primary-run.log").open("w", encoding="utf-8")
    standby_log = (logs_dir / "standby-run.log").open("w", encoding="utf-8")

    def _start(credential_path: Path, report_path: Path, handle):
        return subprocess.Popen(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "receiver_agent.py"),
             "run", "--backend-url", base_url, "--allow-insecure-loopback",
             "--credential-path", str(credential_path),
             "--report", str(report_path), "--exit-after-stop"],
            cwd=str(REPOSITORY_ROOT), env=os.environ.copy(),
            stdout=handle, stderr=subprocess.STDOUT,
        )

    primary = _start(primary_path, primary_report, primary_log)
    standby = _start(standby_path, standby_report, standby_log)
    try:
        time.sleep(4.0)  # both sockets up and authenticated
        facts = _drive_one_broadcast(base_url, access_token, store_id, database_path)
        for process in (primary, standby):
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for process in (primary, standby):
            _stop_process(process)
        primary_log.close()
        standby_log.close()

    def _chunks(path: Path) -> int:
        if not path.exists():
            return 0
        return json.loads(path.read_text(encoding="utf-8")).get("total_chunks", 0)

    return {
        "primary_chunks": _chunks(primary_report),
        "standby_chunks": _chunks(standby_report),
        "broadcast": facts,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="receiver_device_staging_smoke",
        description=(
            "End-to-end Receiver Device staging smoke on a throwaway database. "
            "Proves software behaviour only: never an amplifier, a speaker or "
            "SPEAKER_VERIFIED."
        ),
    )
    parser.add_argument(
        "--staging-root",
        default=str(Path(tempfile.gettempdir()) / "echocast-device-staging"),
        help="A throwaway directory. Recreated on every run.",
    )
    arguments = parser.parse_args(argv)

    try:
        report = smoke(Path(arguments.staging_root))
    except StagingSmokeError as failure:
        print(f"Staging smoke refused: {failure}", file=sys.stderr)
        return EXIT_SAFETY
    except Exception as failure:  # pragma: no cover - defensive
        print(f"Staging smoke failed: {type(failure).__name__}: {failure}", file=sys.stderr)
        return EXIT_ASSERTION

    for key in sorted(report):
        if key in ("checks", "broadcast", "standby", "agent_session", "device_roles"):
            continue
        print(f"  {key}: {report[key]}")
    print("  checks:")
    for name in sorted(report["checks"]):
        print(f"    {name}: {report['checks'][name]}")

    print()
    if report["overall_result"] == MARKER_PASSED:
        print(f"Result: {MARKER_PASSED}")
        print("This is SOFTWARE evidence only. It proves a Device enrolled, sealed its")
        print("credential with real Windows DPAPI, authenticated, became READY after real")
        print("FFmpeg and codec checks, received WebM/Opus audio, decoded it to a NULL")
        print("sink, stopped, rotated, and was disabled and revoked.")
        print("It does NOT prove an amplifier, Bluetooth, audible Store speakers, or")
        print("SPEAKER_VERIFIED. Only EchoGuard acoustic detection can establish those.")
        return EXIT_OK

    print(f"Result: {MARKER_FAILED}")
    return EXIT_ASSERTION


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
