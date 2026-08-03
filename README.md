# EchoCast AI

EchoCast AI is a live retail announcement system: audio goes from an HQ browser
dashboard to Receiver PCs in around 40 stores.

```text
HQ Browser -> React Dashboard -> FastAPI Server -> Secure WebSocket
           -> Windows Receiver Agent -> FFmpeg / Windows Audio
           -> Amplifier -> Store Speakers
```

---

## Running HQ (the current way)

HQ is **repository-native**. The folder you have IS the installation - there is
no installer, nothing is copied into AppData, and HQ registers no Windows
Scheduled Task.

### Windows

1. Copy or clone this repository somewhere you can write to.
2. Copy `.env.example` to `.env`.
3. Fill in `ADMIN_USERNAME`, `ADMIN_PASSWORD` and `JWT_SECRET`.
4. Double-click **`start.bat`** (or run it from a terminal).
5. Open the URL it prints - by default `http://localhost:8000/`.

Then `stop.bat` and `restart.bat` as needed.

The first start creates a repo-local virtual environment and installs the
pinned requirements, so it takes a few minutes. Later starts do not.

### Cloud / Linux / macOS

The `.bat` files are **Windows convenience wrappers only**. Every decision
lives in the cross-platform launcher, so any OS can run:

```bash
python tools/echocast_server.py run        # foreground - for systemd, a
                                           # container, or a process manager
python tools/echocast_server.py status
```

`run` stays in the foreground deliberately: whatever supervises it (systemd, a
container runtime) should own restarts. Build the frontend in CI and ship the
`frontend/build` directory - serving it needs only Python, so Node and Yarn are
build-time requirements, not runtime ones.

### What lives where

```text
repo/
    .env                 your configuration (gitignored)
    .env.example         the template
    start.bat            Windows wrappers around the launcher
    stop.bat
    restart.bat
    build-store-receiver.bat
    backend/             FastAPI application
    frontend/            React application
    tools/
        echocast_server.py       the cross-platform launcher
    data/                EVERYTHING LIVE - gitignored
        echocast.db      the SQLite database
        keys/            signing secret, Receiver key container
        logs/
        runtime/         PID and state files
```

Backing HQ up is "copy `data/`". Nothing in it is ever committed.

### One origin

One Uvicorn worker serves the API, the WebSocket endpoints and the built React
app on the same origin, so production has no CORS to configure and the same
build works whether you open it by IP, by hostname or through a cloud name.
CORS support remains for two-port development, where the React dev server on
3000 talks to the API on 8000.

### The first administrator

`ADMIN_USERNAME` and `ADMIN_PASSWORD` are used **once**, when the database does
not yet exist. After that they are ignored: changing them cannot reset a live
account or create a second Owner, because account state belongs to User
Management once it exists. If they are missing when a database has to be
created, startup refuses - there is no default login anywhere in this product.

---

## Store Receiver

The Store Receiver stays Windows-specific, and keeps its Windows Scheduled Task
- a till has nobody to start anything, and Windows session 0 has no audio
endpoint.

Build the kit to take to a Store:

```
build-store-receiver.bat
```

That produces one versioned ZIP in `artifacts/` with a manifest, SHA-256
checksums, the setup wizard, the Receiver, the windowless background Receiver
and the installer scripts. **Extract it to a short path** on the Store PC (for
example `C:\EchoCast`): the package is deeply nested and a long extraction path
can exceed the Windows 260-character limit, which shows up as a DLL failing to
load.

---

## Legacy HQ deployment (rollback only)

The previous HQ deployment installed into `%LOCALAPPDATA%\EchoCast-AI\hq-app`
and started from an "EchoCast HQ Runtime" Scheduled Task. Those scripts are
still present and still supported **as the rollback path** while repo-native
operation is being proven on the live machine:

```text
scripts/Install-EchoCastHQAutoStart.ps1     LEGACY HQ deployment
scripts/Repair-EchoCastHQAutoStart.ps1      LEGACY HQ deployment
scripts/Test-EchoCastHQAutoStart.ps1        LEGACY HQ deployment
scripts/Uninstall-EchoCastHQAutoStart.ps1   LEGACY HQ deployment
tools/hq_runtime.py                         LEGACY HQ deployment
```

Do not delete them until the live HQ has been migrated to repo-native mode and
accepted. The Store Receiver scripts in `scripts/` are NOT legacy - they are
the current, supported Store flow.

---

## Windows prerequisites

- Git
- Python 3.11+ (the verified local version is 3.12.10)
- Node.js and Yarn 1.x - **only to build the frontend**
- FFmpeg - only to build the Store Receiver package
- PowerShell - only for the Store Receiver build and install scripts

## Developer setup (two-port mode)

Run these commands from the repository root in PowerShell:

```powershell
Set-Location backend
py -3.12 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Create `backend/.env` locally with the configuration required by the server.
Do not paste credentials into documentation, shell history, commits, or issue
reports. The application loads this file at startup, and Git ignores it.

`ADMIN_USERNAME` and `ADMIN_PASSWORD` are **required**. The backend refuses to
start without them, and there is no default: an unconfigured machine used to get
a password everybody already knew. Choose your own values.

They bootstrap the **first** administrator only. Once one exists, startup never
touches it again — not the username, not the password. Changing `ADMIN_PASSWORD`
later does not rotate anything, and a different `ADMIN_USERNAME` does not create
a second account. Rotating a password is a deliberate administrative action, not
something a restart does on your behalf.

### How credentials travel

| Path | Transport |
| --- | --- |
| Ordinary authenticated HTTP | `Authorization: Bearer <token>` — **header only** |
| HQ dashboard WebSocket | single-use ticket in the URL, valid ~20 s, redeemed once |
| Broadcaster WebSocket | same single-use ticket scheme |
| Receiver WebSocket | `Authorization: Bearer <store credential>` header |

No reusable credential is ever accepted from a URL. A URL is the least private
part of a request — it reaches access logs, proxy logs, browser history, copied
links and Referer headers — so the only thing allowed there is a ticket that is
worthless seconds later and after a single use.

### Login protection (optional, with safe defaults)

The login is rate-limited and locks an account after repeated failures. You do
not have to configure anything; these are the defaults.

| Variable | Default | Meaning |
| --- | ---: | --- |
| `LOGIN_MAX_ATTEMPTS` | 10 | attempts allowed per client and per username inside the window |
| `LOGIN_WINDOW_SECONDS` | 60 | length of that window |
| `LOGIN_MAX_FAILURES` | 5 | consecutive failures before an existing account is locked |
| `LOGIN_LOCKOUT_SECONDS` | 900 | how long that lock lasts |
| `LOGIN_LIMITER_MAX_ENTRIES` | 4096 | upper bound on tracked clients/usernames |
| `TRUST_PROXY_HEADERS` | off | see below |

Unusable values stop the backend at startup rather than quietly disabling the
protection.

`X-Forwarded-For` is **ignored** unless `TRUST_PROXY_HEADERS` is switched on.
Only enable it when a proxy you control sets that header; otherwise any caller
can invent a new identity per request and walk past the rate limit.

The rate limiter lives in one process. That is correct for this deployment,
which runs exactly one Uvicorn worker. Running several workers would give each
its own limiter and multiply the effective allowance — that needs shared
rate-limit storage, which is not implemented. The **account lock** is stored in
the database, so it is not affected by this and survives a restart.

Start exactly one backend worker:

```powershell
Set-Location backend
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1
```

One worker is required because receiver connections and live broadcast state
are currently held in process memory.

## Frontend setup

Open a second PowerShell window at the repository root:

```powershell
Set-Location frontend
yarn install
yarn start
```

Create `frontend/.env` locally and set `REACT_APP_BACKEND_URL` to the local
backend origin. Do not add `/api` to that value because the frontend adds it.
Restart the frontend after changing its environment file.

## Local URLs

- Dashboard: `http://localhost:3000`
- Backend: `http://127.0.0.1:8000`
- Interactive API documentation: `http://127.0.0.1:8000/docs`
- OpenAPI document: `http://127.0.0.1:8000/openapi.json`

## Safe startup order

1. Confirm `git status` does not show `.env`, SQLite, WAL, or SHM files.
2. Start one backend worker and wait for the startup-complete log.
3. Open `/docs` to confirm the backend is reachable.
4. Start the frontend in a second terminal.
5. Open the dashboard and sign in with credentials from the local configuration.
6. Expect stores to remain OFFLINE until a receiver is genuinely connected.

## Stopping locally

Press `Ctrl+C` once in the frontend terminal and once in the backend terminal.
Allow each process to exit before closing its terminal. If a port remains in
use, identify the owning process before stopping it:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess
Get-NetTCPConnection -LocalPort 3000 -State Listen | Select-Object OwningProcess
Stop-Process -Id <confirmed-process-id>
```

Never delete the database to resolve a startup or port problem.

## Safe smoke tests

The smoke suite creates a unique SQLite database under pytest's temporary
directory. It does not use `backend/echocast_live.db`.

```powershell
Set-Location backend
& .\.venv\Scripts\python.exe -m pytest -n 0 tests/test_smoke.py -q
```

The broader legacy integration suite is intentionally gated. It requires an
explicit `ECHOCAST_TEST_BASE_URL`, explicit confirmation that the target server
uses an isolated test database, and test credentials supplied through
environment variables. Non-loopback targets are rejected unless
`ECHOCAST_ALLOW_NONLOCAL_WRITE_TESTS=1` is deliberately set. Do not point that
suite at a normal development or production server.

## Putting a Receiver on a Store computer

The Store computer needs **no Python, no Node, no FFmpeg install and no copy of
this repository** — only a kit folder.

```powershell
# 1. build the Receiver package (refuses a dirty tree; verifies the executable
#    is newer than every source file that went into it)
.\scripts\Build-EchoCastReceiver.ps1
.\scripts\Test-EchoCastReceiverPackage.ps1 -PackagePath "artifacts\EchoCastReceiver-1.0.0-<commit>-<stamp>"

# 2. wrap it, with the installer scripts, into a kit the operator can follow
.\scripts\Build-EchoCastStorePilotKit.ps1 -PackagePath "artifacts\EchoCastReceiver-1.0.0-<commit>-<stamp>"
.\scripts\Test-EchoCastStorePilotKit.ps1 -KitPath "artifacts\EchoCast-Store-Pilot-<commit>-<stamp>"
```

Copy the whole kit folder to the Store computer. The operator follows
`README-FIRST.txt` inside it. Details and the reasoning:
[STORE_PILOT_KIT_RUNBOOK.md](STORE_PILOT_KIT_RUNBOOK.md),
[RECEIVER_TASK_SCHEDULER_RUNBOOK.md](RECEIVER_TASK_SCHEDULER_RUNBOOK.md),
[PRIVATE_LAN_TWO_DESKTOP_TEST_RUNBOOK.md](PRIVATE_LAN_TWO_DESKTOP_TEST_RUNBOOK.md).

Never use `artifacts\EchoCastReceiver-1.0.0` — it is marked
`STALE-DO-NOT-DEPLOY` and kept only as evidence.

**What autorun does and does not do.** The Receiver starts when a user *logs
on*. It is not a Windows service; it does not start before logon, and it does
not keep playing on a locked desktop with nobody signed in. That is not a flag
left unset — the Receiver plays audio into a user session, and session 0 has no
audio device.

## Who can sign in

HQ accounts are managed at `/users` by an account holding `MANAGE_USERS`, and
anybody signed in can change their own password at `/account/password`.

Accounts are **archived, never deleted** — a User is the author of broadcast
history. Restoring an archived account returns it to *disabled*, never straight
to active. The last active SUPER_ADMIN cannot be disabled, archived or demoted,
and nobody can switch off their own account: there is no reset e-mail here and
no support line, so the recovery from getting that wrong is editing the database
by hand.

Disabling, archiving, changing a role or changing a password ends that account's
existing sessions immediately, rather than eight hours later when its JWT would
have expired.

## Common errors

- **Activation script is blocked:** set the execution policy only for the
  current PowerShell process, then activate `.venv` again.
- **Address already in use:** identify the process that owns port 8000 or 3000;
  do not start duplicate backend workers.
- **Frontend cannot reach the API:** verify `REACT_APP_BACKEND_URL`, omit the
  `/api` suffix, and restart `yarn start`.
- **Login fails:** verify local environment configuration without printing its
  values. Do not replace or reseed the real database as a shortcut.
- **Stores show OFFLINE:** this is expected when no Receiver Agent has a live
  WebSocket connection.
- **Tests mention an explicit URL or isolated database:** use the safe smoke
  command above unless you intentionally prepared a separate integration server.

## Repository safety

Never commit `backend/.env`, `frontend/.env`, passwords, JWTs, receiver tokens,
SQLite databases, SQLite WAL/SHM files, `node_modules`, or `.venv`. Never delete,
rename, reset, or reseed `backend/echocast_live.db` to make tests pass. Automated
tests must use an isolated temporary or dedicated test database.
