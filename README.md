# Here are your Instructions

# EchoCast AI

EchoCast AI is a live retail announcement system intended to send audio from an
HQ browser dashboard to receivers in approximately 40 stores.

```text
HQ Browser -> React Dashboard -> FastAPI Server -> Secure WebSocket
           -> Windows Receiver Agent -> FFmpeg / Windows Audio
           -> Amplifier -> Store Speakers
```

This repository currently contains the HQ React application and FastAPI
backend. The Windows Receiver Agent is not part of the verified baseline yet.
See `PROJECT_STATE.md` for the precise verification status and known limits.

## Windows prerequisites

- Git
- Python 3.12 (the verified local version is 3.12.10)
- Node.js compatible with Create React App
- Yarn 1.x (the verified local version is 1.22.22)
- FFmpeg on `PATH` for future receiver/audio work (verified locally with 8.1.2)
- PowerShell

Keep the repository in a path you can write to. The current verified checkout
uses `C:\Users\admin\Desktop\EchoCast-AI\HQ-Broadcast-Full (1)`.

## Backend setup

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
