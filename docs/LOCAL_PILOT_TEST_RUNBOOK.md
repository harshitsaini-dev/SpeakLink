# SpeakLink Local One-Store Pilot Test Runbook

Status: local **software** pilot only. This runbook does not authorize a
production deployment, a Store rollout, or any live audio test.

Everything here runs on your own machine, on loopback only, against a
disposable pilot database. The protected application database
`backend\speaklink_live.db` is never used, opened, copied or modified.

## What this test proves

- The backend starts against an isolated pilot database.
- Login works with your temporary pilot credentials.
- The Store API returns exactly **44 Stores** across exactly **9 Zones**.
- The retired 13-entry demo catalog is absent.
- One Store Receiver can authenticate over the WebSocket using the required
  `Authorization: Bearer` header.
- A query-string credential is still refused.
- The Store reports **CONNECTED** honestly.
- Receiver disconnect cleanup returns the Store to OFFLINE.
- The backend shuts down cleanly and frees its port.

## What this test does **not** prove

- Microphone audio capture
- Audio delivery to a Receiver
- FFmpeg decoding or playback
- Correct Windows audio output device selection
- Bluetooth or wired amplifier connection
- Amplifier input selection
- **Audible Store speakers**
- LinkGuard pause/resume
- `SPEAKER_VERIFIED`
- Production readiness, TLS/WSS, or a multi-Store rollout

A Store showing "online" in the dashboard means **CONNECTED only**. It is not
evidence of READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED or SPEAKER_VERIFIED.

## Prerequisites

1. Python virtual environment at `backend\.venv` (see `README.md`).
2. Node.js and Yarn 1.22.x on `PATH`.
3. Loopback ports free (default `8000` for backend, `3000` for frontend).
4. **No SpeakLink backend or frontend already running.** Check first:
   ```powershell
   Get-CimInstance Win32_Process |
       Where-Object { $_.Name -in 'python.exe','node.exe' -and $_.CommandLine -match 'uvicorn|craco' } |
       Select-Object ProcessId, Name
   ```
   If anything is listed, stop it in its own window with `Ctrl+C` first.
5. Record the protected database metadata before and after (never open it):
   ```powershell
   Get-Item 'backend\speaklink_live.db' | Select-Object Length, LastWriteTimeUtc
   ```

## Step 1 - Set pilot-only credentials

These are **process-scoped**: they exist only in this PowerShell window and
disappear when you close it. They are never written to Git, to a repository
`.env`, to the pilot state file or to any log.

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink'
$env:ADMIN_USERNAME = 'pilot-operator'
$env:ADMIN_PASSWORD = 'choose-a-temporary-pilot-only-value'
$env:JWT_SECRET     = 'choose-another-temporary-pilot-only-value'
```

Use throwaway values. Do not reuse a real password.

## Step 2 - Prepare the isolated pilot database

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\backend'
& .\.venv\Scripts\python.exe ..\tools\local_pilot.py prepare
```

Expected output (values will differ):

```text
Local pilot database prepared (isolated, disposable).
  already_prepared: False
  canonical_source: backend/store_catalog.py
  database_path: C:\Users\<you>\AppData\Local\SpeakLink\local-pilot\data\speaklink_local_pilot.db
  demo_codes_present: []
  reconciliation: EXACT_CANONICAL_MATCH
  store_count: 44
  zone_count: 9
```

Exit code `0`. Anything else means it refused - read the message.

## Step 3 - Run the automated smoke test

```powershell
& .\.venv\Scripts\python.exe ..\tools\local_pilot.py smoke
```

Expected output (abridged):

```text
Local pilot smoke: LOCAL_PILOT_SMOKE_PASSED
  backend_host: 127.0.0.1
  liveness: ok
  login: ok
  observed_connection: CONNECTED
  observed_readiness: NOT_REPORTED
  observed_playback: NOT_REPORTED
  observed_acoustic: NOT_REPORTED
  query_token_refused: True
  receiver_auth: ok
  receiver_cleanup: ok
  selected_store_code: UN
  shutdown: ok
  speaker_verified: False
  store_count: 44
  uvicorn_workers: 1
  zone_count: 9
```

The smoke test starts and stops its own backend on a random free loopback
port. Nothing is left running.

## Step 4 - Start the pilot for manual browser testing

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink'
.\scripts\Start-SpeakLinkLocalPilot.ps1
```

It prints the backend URL, frontend URL, pilot database path and PID file
locations. It refuses to start if the pilot database is missing, if a
credential variable is unset, or if the port is already in use.

## Step 5 - Browser checklist

1. Open `http://localhost:3000`.
2. Log in with your pilot-only `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
3. Open **Store Management**.
4. Verify exactly **44** Stores are listed.
5. Verify **9** Zones appear (Broadcast Console -> Target Mode -> *By Zone*).
6. Spot-check the corrected Store codes:
   `UN`, `ASR`, `VP2`, `RRPL`, `JHA`, `JHA2`, `RMME`, `ME3`, `RG2`, `RMCR`,
   `NS104`, `GZBD`, `NIT`.
7. Confirm the retired demo codes are **absent**:
   `MUM-001`, `DEL-001`, `BLR-001`, `ONL-001`.
8. Open **Receiver Status**. Stores are grouped by Zone.
9. Before connecting a simulator, your chosen Store must show **OFFLINE**.
10. In a **second** PowerShell window, start the simulator for one Store.
    Copy that Store's receiver credential from Store Management's receiver URL:
    ```powershell
    Set-Location 'C:\Users\admin\Desktop\SpeakLink'
    $env:SPEAKLINK_RECEIVER_TOKEN = '<paste-the-store-credential>'
    python tools\receiver_simulator.py `
      --url ws://127.0.0.1:8000/api/ws/receiver `
      --scenario ready-only
    Remove-Item Env:SPEAKLINK_RECEIVER_TOKEN
    ```
11. Verify the selected Store becomes **CONNECTED** (shown as "online").
12. Understand what that means: "online" is the **connection** axis only. The
    current dashboard does not display the readiness, playback or acoustic
    axes at all, so it can never show a fake READY or PLAYBACK_CONFIRMED. The
    automated smoke in Step 3 is what proves a bare connection never invents
    readiness, because it sends no acknowledgement whatsoever.
13. Stop the simulator (`Ctrl+C`).
14. Verify the Store returns to **OFFLINE** - that is disconnect cleanup.
15. **Do not start a microphone broadcast yet.** Live audio is a separate,
    not-yet-approved task.

## Step 6 - Stop the pilot

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink'
.\scripts\Stop-SpeakLinkLocalPilot.ps1
```

It reads only the scoped pilot PID files, verifies each process really is an
SpeakLink pilot process before stopping it, and clears the pilot variables from
your session. It never terminates unrelated Python or Node processes, and it
never deletes the pilot database.

## Resetting the pilot database

Only if you want a clean pilot database:

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\backend'
& .\.venv\Scripts\python.exe ..\tools\local_pilot.py reset --reset-pilot-db
```

Without `--reset-pilot-db` nothing is removed. Even with it, the tool removes
only the pilot database and its `-wal`/`-shm` sidecars, only from inside the
validated pilot directory, only when the `PILOT_ONLY` marker is present.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Action succeeded |
| `1` | Input, safety or startup failure (including a refused path) |
| `2` | An application smoke assertion failed |
| `3` | Cleanup or shutdown failure |

## Common errors

| Message / symptom | What it means |
| --- | --- |
| `Port 8000 is already in use` | Something else holds the port. Stop it yourself or pass `-BackendPort`. |
| `ADMIN_PASSWORD is not set` | Step 1 was skipped, or you opened a new window. |
| `the protected SpeakLink database was refused` | A path resolved to `backend\speaklink_live.db`. This is the safety net working. |
| `the pilot root was refused because it is inside the repository` | Runtime artifacts must live outside Git. |
| `the pilot database is not prepared` | Run Step 2 first. |
| `Yarn was not found on PATH` | Install Yarn 1.22.x. |
| `the pilot backend did not become live in time` | Check `logs\backend.out.log` under the pilot root. |
| `the Store API returned N Stores` | The pilot database does not match the catalog; reset and prepare again. |
| `a WAL file sits beside the snapshot` | The database was not quiesced. Stop the backend, then re-run prepare. |
| Stale PID file | `Stop-SpeakLinkLocalPilot.ps1` detects and removes it safely. |

## Safety and cleanup

Files that remain after a pilot run, all **outside** the repository under
`%LOCALAPPDATA%\SpeakLink\local-pilot`:

```text
data\speaklink_local_pilot.db   disposable pilot database
logs\backend.log               smoke-run backend log
logs\backend.out.log           manual-run backend stdout
logs\backend.err.log           manual-run backend stderr
logs\smoke-report.json         secret-free smoke result
runtime\pilot-state.json       secret-free preparation record
runtime\PILOT_ONLY             safety marker
runtime\backend.pid            present only while running
runtime\frontend.pid           present only while running
```

Nothing above is committed to Git.

Never delete the repository, and never delete `backend\speaklink_live.db`, its
`-wal` file or its `-shm` file. If you want a fresh pilot, use the reset
command above - it touches only the pilot database.
