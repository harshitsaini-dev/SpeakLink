"""Packaged EXE playback, and proof of WHICH ffmpeg.exe the Agent launched.

The Python staging smoke proves the Agent's logic. This proves the thing an
operator actually copies to a till: the built executable, with no Python
anywhere on its PATH and no system FFmpeg either.

THE PROOF THAT NEEDED TWO ATTEMPTS

Running ffmpeg.exe directly proves the file works. It does not prove the Agent
chose it, and a Store PC with some other FFmpeg installed would fail in exactly
the way that test cannot see. So this watches the process table for the FFmpeg
child the Agent spawns.

The first version watched globally and reported failure: it caught this
machine's PATH FFmpeg, which the *broadcast driver* uses to generate its audio
fixture, and blamed the Agent for it. The claim is "the AGENT launched the
packaged FFmpeg", so the measurement has to be the Agent's own process tree -
scoped by parent PID, walked a few generations down. Scoped correctly, exactly
one FFmpeg appears and it is the one beside the executable.

Run it with the package directory as the only argument:

    python tools/receiver_exe_staging_smoke.py artifacts/SpeakLinkReceiver-<...>

Software and null-sink evidence only. Not an amplifier, not a speaker, not
SPEAKER_VERIFIED, and not a second desktop.
"""

import json, os, secrets, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(r"c:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)")
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "backend"))
import requests

PACKAGE = Path(sys.argv[1]).resolve()
EXE = PACKAGE / "SpeakLinkReceiver.exe"
EXPECTED_FFMPEG = (PACKAGE / "ffmpeg.exe").resolve()
HQ = "192.168.4.134"
BASE = f"http://{HQ}:8000"

from tools.lan_pilot import prepare
from tools.receiver_device_staging_smoke import _drive_one_broadcast, _wait_until

root = Path(os.environ["TEMP"]) / f"speaklink-exe-{time.strftime('%H%M%S')}"
password = "exe-staging-temporary-password"
manifest = prepare(root, hq_address=HQ, admin_username="exe-admin", admin_password=password)
logs = root / "logs"

env = dict(os.environ,
           SPEAKLINK_DB_PATH=manifest["database_path"],
           SPEAKLINK_KEY_CONTAINER=manifest["key_container"],
           SPEAKLINK_KEY_PROTECTOR="fake",
           JWT_SECRET=secrets.token_urlsafe(48),
           ADMIN_USERNAME="exe-admin", ADMIN_PASSWORD=password,
           CORS_ORIGINS=",".join(manifest["cors_origins"]))

# The Agent's environment: no Python, no WindowsApps aliases, no system FFmpeg.
stripped = os.pathsep.join(
    p for p in os.environ["PATH"].split(os.pathsep)
    if p and not any(m in p.lower() for m in ("python", ".venv", "ffmpeg", "windowsapps")))
agent_env = dict(os.environ, PATH=stripped)

ffmpeg_seen, python_children = {}, set()
watching = threading.Event()

AGENT_PID = {"value": None}

def watch():
    """Only processes descended from the Agent.

    A global scan answers the wrong question. This machine has FFmpeg on PATH,
    and the broadcast driver itself generates its fixture with it - so an
    unscoped watcher sees that too and reports the Agent used a system binary it
    never touched. The claim is "the AGENT launched the packaged FFmpeg", so the
    measurement has to be the Agent's own process tree.
    """
    while not watching.is_set():
        root_pid = AGENT_PID["value"]
        if root_pid is None:
            time.sleep(0.2)
            continue
        try:
            out = subprocess.run(
                ["wmic", "process", "get", "Name,ProcessId,ParentProcessId,ExecutablePath", "/format:csv"],
                capture_output=True, text=True, timeout=10)
            rows = []
            for line in out.stdout.splitlines():
                fields = [f.strip() for f in line.split(",")]
                if len(fields) < 5 or fields[1].lower() == "executablepath":
                    continue
                # Node,ExecutablePath,Name,ParentProcessId,ProcessId
                rows.append({"path": fields[1], "name": fields[2],
                             "ppid": fields[3], "pid": fields[4]})
            owned = {str(root_pid)}
            for _ in range(4):  # a few generations is plenty
                for row in rows:
                    if row["ppid"] in owned:
                        owned.add(row["pid"])
            for row in rows:
                if row["pid"] in owned and row["pid"] != str(root_pid):
                    if row["name"].lower() == "ffmpeg.exe" and row["path"]:
                        ffmpeg_seen[row["path"]] = ffmpeg_seen.get(row["path"], 0) + 1
                    if row["name"].lower() in ("python.exe", "py.exe", "pythonw.exe"):
                        python_children.add(f"{row['name']} {row['path']}")
        except Exception:
            pass
        time.sleep(0.2)

backend = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server:app", "--host", HQ, "--port", "8000",
     "--workers", "1", "--no-access-log"],
    cwd=str(ROOT / "backend"), env=env,
    stdout=open(logs / "backend.log", "w"), stderr=subprocess.STDOUT)

agent = None
checks = {}
try:
    for _ in range(80):
        try:
            if requests.get(f"{BASE}/docs", timeout=1).status_code == 200: break
        except Exception: time.sleep(0.3)
    checks["backend_on_lan"] = True

    token = requests.post(f"{BASE}/api/auth/login", timeout=10,
        json={"username": "exe-admin", "password": password}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    code = requests.post(f"{BASE}/api/receiver-devices/enrollment-codes", headers=auth,
        json={"store_id": manifest["store_id"]}, timeout=10).json()["code"]

    credential_path = root / "agent" / "device-credential.bin"
    enrolled = subprocess.run(
        [str(EXE), "enrol", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--device-name", "EXE staging device",
         "--credential-path", str(credential_path), "--from-stdin"],
        input=code + "\n", capture_output=True, text=True, timeout=120, env=agent_env)
    checks["exe_enrolled"] = enrolled.returncode == 0
    checks["dpapi_sealed"] = credential_path.exists()
    checks["code_absent_from_enrol_output"] = code not in (enrolled.stdout + enrolled.stderr)
    if not checks["exe_enrolled"]:
        raise SystemExit(f"enrol failed: {enrolled.stderr.strip()[:300]}")

    # Restart: a second process must decrypt the credential the first sealed.
    status = subprocess.run([str(EXE), "status", "--credential-path", str(credential_path)],
                            capture_output=True, text=True, timeout=60, env=agent_env)
    checks["credential_survives_restart"] = "enrolled: True" in status.stdout

    device = requests.get(f"{BASE}/api/stores/{manifest['store_id']}/receiver-devices/roles",
                          headers=auth, timeout=10).json()[-1]
    requests.post(f"{BASE}/api/receiver-devices/{device['public_id']}/promote", headers=auth, timeout=10)

    watcher = threading.Thread(target=watch, daemon=True); watcher.start()
    agent = subprocess.Popen(
        [str(EXE), "run", "--backend-url", BASE, "--allow-insecure-private-lan",
         "--expected-hq-host", HQ, "--credential-path", str(credential_path),
         "--report", str(logs / "session.json"), "--exit-after-stop"],
        cwd=str(PACKAGE), env=agent_env,
        stdout=open(logs / "agent.log", "w"), stderr=subprocess.STDOUT)
    AGENT_PID["value"] = agent.pid

    def online():
        for s in requests.get(f"{BASE}/api/stores", headers=auth, timeout=10).json():
            if s["id"] == manifest["store_id"]:
                return s["status"] in ("online", "playing")
        return False
    checks["connected"] = _wait_until(online, timeout=40)

    facts = _drive_one_broadcast(BASE, token, manifest["store_id"], Path(manifest["database_path"]))
    for key in ("ready", "audio_receiving", "playback_confirmed", "stopped"):
        checks[key] = facts[key]

    try:
        agent.wait(timeout=30)
        checks["agent_exited_cleanly"] = agent.returncode == 0
    except subprocess.TimeoutExpired:
        checks["agent_exited_cleanly"] = False
    agent = None
    watching.set(); watcher.join(timeout=3)

    report = json.loads((logs / "session.json").read_text())
    checks["queue_returned_to_zero"] = report.get("dropped_chunks", -1) == 0
    checks["ffmpeg_exited"] = report.get("ffmpeg_returncode") is not None
    checks["no_speaker_verified"] = report.get("speaker_verified") is False

    checks["no_python_child"] = not python_children
    ffmpeg_ok = bool(ffmpeg_seen) and all(Path(p).resolve() == EXPECTED_FFMPEG for p in ffmpeg_seen)
    checks["packaged_ffmpeg_only"] = ffmpeg_ok

    print()
    print("=== checks ===")
    for name in sorted(checks):
        print(f"   {name:34} {checks[name]}")
    print()
    print("=== FFmpeg processes observed while the Agent ran ===")
    for path, count in ffmpeg_seen.items():
        print(f"   {path}")
        print(f"     samples={count}  equals packaged: {Path(path).resolve() == EXPECTED_FFMPEG}")
    print(f"   expected: {EXPECTED_FFMPEG}")
    print()
    if ffmpeg_ok:
        print("PACKAGED_FFMPEG_CONFIRMED")
    else:
        print("PACKAGED_FFMPEG_NOT_CONFIRMED")
    if all(checks.values()):
        print("SPEAKLINK_RECEIVER_EXE_STAGING_SMOKE_PASSED")
    else:
        print("SPEAKLINK_RECEIVER_EXE_STAGING_SMOKE_FAILED")
        print("  failing:", [k for k, v in checks.items() if not v])
finally:
    watching.set()
    for proc in (agent, backend):
        if proc and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=10)
            except Exception: proc.kill()
