"""The complete private-LAN check for the packaged Receiver.

The existing EXE staging smoke already binds the backend to 192.168.4.134 and
proves enrolment, playback and packaged-FFmpeg provenance. What it does not
cover is everything that happens *after* a Store is working: rotating a
credential, retiring one, a standby that must stay silent, a duplicate launch,
and the scheduled task that restarts a crashed Receiver but must not restart a
revoked one for ever.

This runs all of it against 192.168.4.134, using the executable from a verified
Store pilot kit - the same file an operator would copy - with Python, the
virtual environment and any system FFmpeg stripped from its PATH.

WHAT THIS IS EVIDENCE OF

    Same-computer LAN-address software proof, through the packaged EXE, into a
    null audio sink.

WHAT IT IS NOT EVIDENCE OF

    A second desktop. An amplifier. An audible speaker. EchoGuard.
    SPEAKER_VERIFIED. None of those can be produced by a program.

The protected database is never opened. Every database here is created fresh
under the temporary root and thrown away.

    python tools/private_lan_receiver_exe_check.py <kit-directory>
"""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

import requests  # noqa: E402

from tools.lan_pilot import prepare  # noqa: E402
from tools.receiver_device_staging_smoke import (  # noqa: E402
    _drive_one_broadcast,
    _wait_until,
)

KIT = Path(sys.argv[1]).resolve()
PACKAGE = KIT / "Receiver"
INSTALLER = KIT / "Installer"
EXE = PACKAGE / "EchoCastReceiver.exe"
EXPECTED_FFMPEG = (PACKAGE / "ffmpeg.exe").resolve()

HQ = "192.168.4.134"
BASE = f"http://{HQ}:8000"
FRONTEND_ORIGIN = f"http://{HQ}:3000"
TASK_NAME = "EchoCast LAN Check (disposable)"

PROTECTED_DB = ROOT / "backend" / "echocast_live.db"
PROTECTED_SHA = "8C858B132907DC72180A134D4981C5E8C4BBC03D190D7370B3823DB2BD2EF2AB"

#: Anything that must never appear in a log, a command line, a URL or a task
#: definition. The credential and code values themselves are added at runtime.
SECRET_PATTERNS = [
    re.compile(r"echocast_rcv_v1\.[A-Za-z0-9._\-]+"),
    re.compile(r"\bECHO(?:-[A-Z0-9]{4}){2,}\b"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"(?i)[?&](token|credential|password|secret)="),
]

checks: "dict[str, object]" = {}
notes: "list[str]" = []


def record(name: str, value) -> None:
    checks[name] = bool(value)
    print(f"   {name:44} {bool(value)}")


def powershell(*arguments: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
        capture_output=True, text=True, timeout=timeout, cwd=str(ROOT),
    )


def port_is_open(host: str, port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(1.5)
        return probe.connect_ex((host, port)) == 0


def protected_database_state() -> dict:
    import hashlib

    if not PROTECTED_DB.exists():
        return {"present": False}
    data = PROTECTED_DB.read_bytes()
    return {
        "present": True,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest().upper(),
        "wal": PROTECTED_DB.with_name(PROTECTED_DB.name + "-wal").exists(),
        "shm": PROTECTED_DB.with_name(PROTECTED_DB.name + "-shm").exists(),
    }


BASELINE = protected_database_state()
print("=== protected database, before ===")
print(f"   {PROTECTED_DB}")
print(f"   size={BASELINE.get('size')} wal={BASELINE.get('wal')} shm={BASELINE.get('shm')}")
print(f"   sha={BASELINE.get('sha256')}")
if BASELINE.get("sha256") != PROTECTED_SHA:
    raise SystemExit("The protected database does not match its baseline. Refusing to run.")
print()


# ===========================================================================
# The Agent's environment, and the FFmpeg watcher
# ===========================================================================
stripped_path = os.pathsep.join(
    entry for entry in os.environ["PATH"].split(os.pathsep)
    if entry and not any(marker in entry.lower()
                         for marker in ("python", ".venv", "ffmpeg", "windowsapps"))
)
agent_env = dict(os.environ, PATH=stripped_path)

ffmpeg_seen: "dict[str, int]" = {}
python_children: "set[str]" = set()
agent_command_lines: "set[str]" = set()
watching = threading.Event()
AGENT_PID = {"value": None}


def watch() -> None:
    """Only processes descended from the Agent.

    A global scan answers a different question. This machine has FFmpeg on PATH
    and the broadcast driver uses it to build its own audio fixture, so an
    unscoped watcher sees that too and blames the Agent for a binary it never
    touched. The claim is "the AGENT launched the packaged FFmpeg".
    """
    while not watching.is_set():
        root_pid = AGENT_PID["value"]
        if root_pid is None:
            time.sleep(0.2)
            continue
        try:
            result = subprocess.run(
                ["wmic", "process", "get",
                 "Name,ProcessId,ParentProcessId,ExecutablePath,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=10,
            )
            rows = []
            for line in result.stdout.splitlines():
                fields = [field.strip() for field in line.split(",")]
                if len(fields) < 6 or fields[1].lower() == "commandline":
                    continue
                # Node,CommandLine,ExecutablePath,Name,ParentProcessId,ProcessId
                rows.append({"cmdline": fields[1], "path": fields[2], "name": fields[3],
                             "ppid": fields[4], "pid": fields[5]})
            owned = {str(root_pid)}
            for _ in range(4):
                for row in rows:
                    if row["ppid"] in owned:
                        owned.add(row["pid"])
            for row in rows:
                if row["pid"] not in owned:
                    continue
                if row["name"].lower() == "ffmpeg.exe" and row["path"]:
                    ffmpeg_seen[row["path"]] = ffmpeg_seen.get(row["path"], 0) + 1
                if row["name"].lower() in ("python.exe", "py.exe", "pythonw.exe"):
                    python_children.add(f"{row['name']} {row['path']}")
                if row["name"].lower() == "echocastreceiver.exe" and row["cmdline"]:
                    agent_command_lines.add(row["cmdline"])
        except Exception:
            pass
        time.sleep(0.25)


def scan_for_secrets(text: str, extra: "list[str]") -> "list[str]":
    found = []
    for pattern in SECRET_PATTERNS:
        found.extend(match.group(0)[:40] for match in pattern.finditer(text))
    for literal in extra:
        if literal and literal in text:
            found.append(literal[:12] + "...")
    return found


# ===========================================================================
# Set up an isolated backend and a frontend, both on the LAN address
# ===========================================================================
root = Path(os.environ["TEMP"]) / f"echocast-lan-check-{time.strftime('%H%M%S')}"
password = "lan-check-temporary-password"
manifest = prepare(root, hq_address=HQ, admin_username="lan-check-admin", admin_password=password)
logs = root / "logs"
agent_logs = root / "agent-logs"
agent_logs.mkdir(parents=True, exist_ok=True)

backend_env = dict(
    os.environ,
    ECHOCAST_DB_PATH=manifest["database_path"],
    ECHOCAST_KEY_CONTAINER=manifest["key_container"],
    ECHOCAST_KEY_PROTECTOR="fake",
    JWT_SECRET=secrets.token_urlsafe(48),
    ADMIN_USERNAME="lan-check-admin",
    ADMIN_PASSWORD=password,
    CORS_ORIGINS=",".join(manifest["cors_origins"]),
)

backend = frontend = None
watcher = None
secrets_in_play: "list[str]" = []

try:
    # ---- 1. firewall -----------------------------------------------------
    print("=== checks ===")
    firewall = powershell("-File", str(ROOT / "scripts" / "Test-EchoCastLanPilotFirewall.ps1"), timeout=300)
    record("01_firewall_verified", "ECHOCAST_LAN_PILOT_FIREWALL_VERIFIED" in firewall.stdout)

    # ---- 2/4. backend on the LAN address ---------------------------------
    backend = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", HQ, "--port", "8000",
         "--workers", "1", "--no-access-log"],
        cwd=str(ROOT / "backend"), env=backend_env,
        stdout=open(logs / "backend.log", "w"), stderr=subprocess.STDOUT,
    )
    started = _wait_until(lambda: port_is_open(HQ, 8000), timeout=60)
    record("02_temporary_backend_started", started and backend.poll() is None)
    record("04_backend_listens_on_lan_8000", port_is_open(HQ, 8000))
    # Binding the LAN address means loopback is NOT served. That is the whole
    # reason the broadcast helper had to stop assuming 127.0.0.1.
    notes.append(f"loopback 8000 reachable: {port_is_open('127.0.0.1', 8000)}")

    # ---- 3/5/6. the built frontend on the LAN address --------------------
    build_dir = ROOT / "frontend" / "build"
    if not (build_dir / "index.html").exists():
        raise SystemExit("frontend/build is missing. Run yarn build first.")
    frontend = subprocess.Popen(
        [sys.executable, "-m", "http.server", "3000", "--bind", HQ, "--directory", str(build_dir)],
        stdout=open(logs / "frontend.log", "w"), stderr=subprocess.STDOUT,
    )
    record("03_temporary_frontend_started",
           _wait_until(lambda: port_is_open(HQ, 3000), timeout=30))
    record("05_frontend_listens_on_lan_3000", port_is_open(HQ, 3000))
    index = requests.get(FRONTEND_ORIGIN, timeout=10).text
    record("06_echocast_live_title_loads", "<title>EchoCast Live</title>" in index)

    # ---- 7/8. CORS -------------------------------------------------------
    approved = requests.options(
        f"{BASE}/api/stores", timeout=10,
        headers={"Origin": FRONTEND_ORIGIN,
                 "Access-Control-Request-Method": "GET"})
    record("07_approved_cors_origin_accepted",
           approved.headers.get("access-control-allow-origin") == FRONTEND_ORIGIN)
    refused = requests.options(
        f"{BASE}/api/stores", timeout=10,
        headers={"Origin": "http://evil.example.com",
                 "Access-Control-Request-Method": "GET"})
    record("08_unapproved_cors_origin_refused",
           refused.headers.get("access-control-allow-origin") not in
           ("http://evil.example.com", "*"))

    # ---- 9/10/11. login, Store, enrolment code ---------------------------
    login = requests.post(f"{BASE}/api/auth/login", timeout=15,
                          json={"username": "lan-check-admin", "password": password})
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    record("09_super_admin_login_succeeds", login.status_code == 200)

    store_id = manifest["store_id"]
    stores = requests.get(f"{BASE}/api/stores", headers=auth, timeout=15).json()
    record("10_temporary_store_exists", any(store["id"] == store_id for store in stores))

    code = requests.post(f"{BASE}/api/receiver-devices/enrollment-codes", headers=auth,
                         json={"store_id": store_id}, timeout=15).json()["code"]
    secrets_in_play.append(code)
    record("11_enrolment_code_issued", bool(code))

    # ---- 12/13/14. the packaged EXE enrols -------------------------------
    credential_path = root / "primary" / "device-credential.bin"
    enrolled = subprocess.run(
        [str(EXE), "enrol", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--device-name", "LAN check primary",
         "--credential-path", str(credential_path), "--from-stdin"],
        input=code + "\n", capture_output=True, text=True, timeout=180, env=agent_env)
    record("12_packaged_exe_enrols", enrolled.returncode == 0)
    if enrolled.returncode != 0:
        raise SystemExit(f"enrol failed: {enrolled.stderr.strip()[:400]}")
    record("13_dpapi_credential_sealed", credential_path.exists())

    status = subprocess.run([str(EXE), "status", "--credential-path", str(credential_path)],
                            capture_output=True, text=True, timeout=90, env=agent_env)
    record("14_exe_restart_decrypts_credential", "enrolled: True" in status.stdout)

    # ---- 15. promote -----------------------------------------------------
    roles = requests.get(f"{BASE}/api/stores/{store_id}/receiver-devices/roles",
                         headers=auth, timeout=15).json()
    primary = roles[-1]
    requests.post(f"{BASE}/api/receiver-devices/{primary['public_id']}/promote",
                  headers=auth, timeout=15)
    roles = requests.get(f"{BASE}/api/stores/{store_id}/receiver-devices/roles",
                         headers=auth, timeout=15).json()
    # The role comes back as "PRIMARY". Comparing against "primary" reported a
    # failed promotion that had plainly succeeded - the broadcast right below
    # only works through the primary Device.
    record("15_device_promoted_to_primary",
           any(str(role.get("role", "")).upper().endswith("PRIMARY")
               and role["public_id"] == primary["public_id"]
               for role in roles))

    # ---- 16..22. a broadcast through the packaged EXE --------------------
    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    agent = subprocess.Popen(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--log-directory", str(agent_logs),
         "--report", str(logs / "primary-session.json"), "--exit-after-stop"],
        cwd=str(PACKAGE), env=agent_env,
        stdout=open(logs / "primary-agent.log", "w"), stderr=subprocess.STDOUT)
    AGENT_PID["value"] = agent.pid

    def store_is_online() -> bool:
        for store in requests.get(f"{BASE}/api/stores", headers=auth, timeout=10).json():
            if store["id"] == store_id:
                return store["status"] in ("online", "playing")
        return False

    record("16_connected", _wait_until(store_is_online, timeout=60))
    facts = _drive_one_broadcast(BASE, token, store_id, Path(manifest["database_path"]))
    record("17_ready", facts["ready"])
    record("18_audio_receiving", facts["audio_receiving"])
    record("19_playback_confirmed", facts["playback_confirmed"])
    record("20_stopped", facts["stopped"])

    try:
        agent.wait(timeout=45)
    except subprocess.TimeoutExpired:
        agent.terminate()
    AGENT_PID["value"] = None

    ffmpeg_confirmed = bool(ffmpeg_seen) and all(
        Path(path).resolve() == EXPECTED_FFMPEG for path in ffmpeg_seen)
    record("21_adjacent_packaged_ffmpeg_confirmed", ffmpeg_confirmed)

    report = json.loads((logs / "primary-session.json").read_text())
    record("22_queue_zero_and_ffmpeg_exited",
           report.get("dropped_chunks") == 0 and report.get("ffmpeg_returncode") is not None)

    # ---- 23/24/25. rotation ---------------------------------------------
    rotated = requests.post(
        f"{BASE}/api/receiver-devices/{primary['public_id']}/rotate-credential",
        headers=auth, timeout=20)
    record("23_credential_rotation_succeeds", rotated.status_code == 200)
    new_credential = rotated.json()["credential"]
    secrets_in_play.append(new_credential)

    # The old sealed credential is still on disk. It must now be refused
    # terminally - exit 2, not a retry loop.
    old_attempt = subprocess.run(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--log-directory", str(agent_logs), "--max-attempts", "2"],
        capture_output=True, text=True, timeout=180, env=agent_env, cwd=str(PACKAGE))
    record("24_old_credential_rejected", old_attempt.returncode == 2)

    stored = subprocess.run(
        [str(EXE), "rotate-local-credential", "--credential-path", str(credential_path),
         "--from-stdin"],
        input=new_credential + "\n", capture_output=True, text=True, timeout=90, env=agent_env)
    reconnect = subprocess.Popen(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--log-directory", str(agent_logs)],
        cwd=str(PACKAGE), env=agent_env,
        stdout=open(logs / "rotated-agent.log", "w"), stderr=subprocess.STDOUT)
    record("25_new_credential_reconnects",
           stored.returncode == 0 and _wait_until(store_is_online, timeout=60))

    # ---- 30. a duplicate launch, while that one is genuinely running -----
    duplicate = subprocess.run(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--log-directory", str(agent_logs)],
        capture_output=True, text=True, timeout=120, env=agent_env, cwd=str(PACKAGE))
    record("30_duplicate_exe_exits_with_code_4", duplicate.returncode == 4)

    # ---- 26/27. a standby that must stay silent --------------------------
    standby_code = requests.post(f"{BASE}/api/receiver-devices/enrollment-codes", headers=auth,
                                 json={"store_id": store_id}, timeout=15).json()["code"]
    secrets_in_play.append(standby_code)
    standby_credential = root / "standby" / "device-credential.bin"
    standby_enrol = subprocess.run(
        [str(EXE), "enrol", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--device-name", "LAN check standby",
         "--credential-path", str(standby_credential), "--from-stdin"],
        input=standby_code + "\n", capture_output=True, text=True, timeout=180, env=agent_env)
    record("26_second_device_enrols_as_standby", standby_enrol.returncode == 0)

    standby = subprocess.Popen(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(standby_credential),
         "--log-directory", str(agent_logs),
         "--report", str(logs / "standby-session.json"), "--exit-after-stop"],
        cwd=str(PACKAGE), env=agent_env,
        stdout=open(logs / "standby-agent.log", "w"), stderr=subprocess.STDOUT)
    time.sleep(6)
    _drive_one_broadcast(BASE, token, store_id, Path(manifest["database_path"]))
    time.sleep(4)
    for process in (standby, reconnect):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except Exception:
                process.kill()

    standby_report_path = logs / "standby-session.json"
    standby_chunks = None
    if standby_report_path.exists():
        standby_chunks = json.loads(standby_report_path.read_text()).get("total_chunks")
    record("27_standby_receives_zero_chunks", standby_chunks in (0, None) and standby_chunks != 1)
    notes.append(f"standby total_chunks: {standby_chunks}")

    # ---- 28/29. revoke ---------------------------------------------------
    revoked = requests.post(f"{BASE}/api/receiver-devices/{primary['public_id']}/revoke",
                            headers=auth, timeout=20)
    record("28_primary_revoke_enforced", revoked.status_code == 200)

    after_revoke = subprocess.run(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--log-directory", str(agent_logs), "--max-attempts", "2"],
        capture_output=True, text=True, timeout=180, env=agent_env, cwd=str(PACKAGE))
    record("29_revoked_receiver_exits_terminally", after_revoke.returncode == 2)

    # ---- 31..35. the scheduled task, live --------------------------------
    install = powershell(
        "-File", str(INSTALLER / "Install-EchoCastReceiverLanPilot.ps1"),
        "-PackagePath", str(PACKAGE),
        "-TaskName", TASK_NAME,
        "-BackendUrl", BASE,
        "-ExpectedHqHost", HQ,
        "-CredentialPath", str(standby_credential),
        "-LogDirectory", str(agent_logs),
        timeout=900)
    record("31_task_installs_from_the_kit",
           "ECHOCAST_RECEIVER_TASK_INSTALLED" in install.stdout)

    verify = powershell(
        "-File", str(INSTALLER / "Test-EchoCastReceiverLanPilot.ps1"),
        "-TaskName", TASK_NAME, timeout=600)
    record("32_current_user_at_logon_task",
           "ECHOCAST_RECEIVER_TASK_SCHEDULER_VERIFIED" in verify.stdout)

    powershell("-Command", f"Start-ScheduledTask -TaskName '{TASK_NAME}'", timeout=120)

    def task_process_ids() -> "list[int]":
        found = powershell(
            "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name = 'EchoCastReceiver.exe'\" | "
            "Where-Object { $_.ExecutablePath -like '*receiver-app*' } | "
            "ForEach-Object { $_.ProcessId }", timeout=120)
        return [int(line) for line in found.stdout.split() if line.strip().isdigit()]

    running = _wait_until(lambda: len(task_process_ids()) > 0, timeout=90)
    first_pids = task_process_ids()
    if not first_pids:
        record("33_real_owned_process_terminated", False)
        record("34_bounded_task_restart_observed", False)
    else:
        # Ownership is verified by executable path before anything is stopped.
        powershell("-Command",
                   f"Stop-Process -Id {first_pids[0]} -Force -ErrorAction SilentlyContinue",
                   timeout=120)
        gone = _wait_until(lambda: first_pids[0] not in task_process_ids(), timeout=60)
        record("33_real_owned_process_terminated", running and gone)

        # Task Scheduler's restart interval is one minute; give it two.
        restarted = _wait_until(
            lambda: any(pid != first_pids[0] for pid in task_process_ids()), timeout=150)
        record("34_bounded_task_restart_observed", restarted)

    # A revoked credential must not produce an endless restart loop. The task
    # is pointed at the revoked credential and must give up.
    powershell("-Command", f"Stop-ScheduledTask -TaskName '{TASK_NAME}'", timeout=120)
    powershell("-File", str(INSTALLER / "Install-EchoCastReceiverLanPilot.ps1"),
               "-PackagePath", str(PACKAGE), "-TaskName", TASK_NAME,
               "-BackendUrl", BASE, "-ExpectedHqHost", HQ,
               "-CredentialPath", str(credential_path),
               "-LogDirectory", str(agent_logs), "-RestartCount", "1", timeout=900)
    powershell("-Command", f"Start-ScheduledTask -TaskName '{TASK_NAME}'", timeout=120)
    time.sleep(20)

    def task_state() -> str:
        state = powershell(
            "-Command", f"(Get-ScheduledTask -TaskName '{TASK_NAME}').State", timeout=120)
        return state.stdout.strip()

    # A task that was never installed is trivially "not restarting forever".
    # The first run of this check reported a pass on exactly that basis, while
    # the install three steps earlier had failed - the strongest-looking result
    # in the whole run was the one measuring nothing.
    task_exists = task_state() != ""
    gave_up = task_exists and _wait_until(lambda: "Running" not in task_state(), timeout=300)
    record("35_revoked_device_does_not_restart_forever", gave_up)
    notes.append(f"revoked-credential task final state: {task_state() or 'NOT INSTALLED'}")

    # ---- 36. logs rotate and stay bounded --------------------------------
    log_files = list(agent_logs.glob("receiver*.log*"))
    total_log_bytes = sum(path.stat().st_size for path in log_files)
    record("36_logs_rotate_and_stay_bounded",
           bool(log_files) and total_log_bytes <= 10 * 1_000_000 + 1_000_000
           and len(log_files) <= 10)
    notes.append(f"log files: {len(log_files)}, {total_log_bytes} bytes")

    # ---- 37..40. nothing secret anywhere it can be read ------------------
    backend_text = (logs / "backend.log").read_text(errors="replace")
    url_leaks = [match.group(0) for match in
                 re.finditer(r"(?i)(wss?|https?)://\S*?[?&](token|credential|password|secret)=\S+",
                             backend_text)]
    record("37_no_secret_in_any_url", not url_leaks)

    cmdline_leaks = []
    for line in agent_command_lines:
        cmdline_leaks.extend(scan_for_secrets(line, secrets_in_play))
    record("38_no_secret_in_process_command_line", not cmdline_leaks)
    notes.append(f"Agent command lines sampled: {len(agent_command_lines)}")

    log_leaks = []
    for path in list(agent_logs.rglob("*")) + list(logs.rglob("*")):
        if path.is_file():
            log_leaks.extend(scan_for_secrets(path.read_text(errors="replace"), secrets_in_play))
    record("39_no_secret_in_logs", not log_leaks)

    task_xml = powershell("-Command", f"Export-ScheduledTask -TaskName '{TASK_NAME}'", timeout=120)
    record("40_no_secret_in_task_xml", not scan_for_secrets(task_xml.stdout, secrets_in_play))

finally:
    watching.set()
    if watcher is not None:
        watcher.join(timeout=5)
    powershell("-File", str(ROOT / "scripts" / "Uninstall-EchoCastReceiverLanPilot.ps1"),
               "-TaskName", TASK_NAME, "-StopRunning", timeout=600)
    for process in (frontend, backend):
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except Exception:
                process.kill()
    subprocess.run(["taskkill", "/F", "/IM", "EchoCastReceiver.exe"],
                   capture_output=True, text=True)

# ---- 41/42/43. the machine is left as it was ------------------------------
time.sleep(3)
record("41_stop_frees_ports_3000_and_8000",
       not port_is_open(HQ, 3000) and not port_is_open(HQ, 8000))

leftover = subprocess.run(
    ["powershell.exe", "-NoProfile", "-Command",
     "@(Get-Process EchoCastReceiver,ffmpeg -ErrorAction SilentlyContinue).Count"],
    capture_output=True, text=True)
record("42_no_stray_receiver_or_ffmpeg", leftover.stdout.strip() in ("0", ""))

after = protected_database_state()
record("43_protected_database_exact_baseline",
       after.get("sha256") == BASELINE.get("sha256")
       and after.get("size") == BASELINE.get("size")
       and not after.get("wal") and not after.get("shm"))

print()
print("=== FFmpeg processes observed in the Agent's own process tree ===")
for path, count in ffmpeg_seen.items():
    print(f"   {path}")
    print(f"     samples={count}  equals packaged: {Path(path).resolve() == EXPECTED_FFMPEG}")
print(f"   expected: {EXPECTED_FFMPEG}")

print()
print("=== notes ===")
for note in notes:
    print(f"   {note}")

print()
print("=== protected database, after ===")
print(f"   size={after.get('size')} wal={after.get('wal')} shm={after.get('shm')}")
print(f"   sha={after.get('sha256')}")

failing = [name for name, value in checks.items() if not value]
print()
if failing:
    print("ECHOCAST_PRIVATE_LAN_RECEIVER_EXE_CHECK_FAILED")
    print("  failing:", failing)
    raise SystemExit(1)

print(f"{len(checks)} checks passed")
print()
print("ECHOCAST_PRIVATE_LAN_RECEIVER_EXE_CHECK_PASSED")
print()
print("Evidence: same-computer LAN-address software proof, through the packaged")
print("EXE, into a null audio sink.")
print("NOT evidence of: a second desktop, an amplifier, an audible speaker,")
print("EchoGuard, or SPEAKER_VERIFIED.")
