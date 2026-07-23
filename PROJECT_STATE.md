# EchoCast AI Project State

Last updated: 2026-07-23
Current branch: `feature/receiver-status-contract`

## Current architecture

```text
HQ Browser
  -> React Dashboard
  -> FastAPI Server
  -> Secure WebSocket
  -> Windows Receiver Agent
  -> FFmpeg / Windows Audio
  -> Amplifier
  -> Store Speakers
```

The checked-in application currently provides the React HQ dashboard, FastAPI
API, SQLite persistence, JWT-based HQ authentication, and WebSocket endpoints.
The Windows Receiver Agent is not implemented or verified as part of this
baseline. WebSocket routing state is held in memory, so the backend must run
with one Uvicorn worker.

## Verified local baseline

- Python 3.12.10, Yarn 1.22.22, and FFmpeg 8.1.2 are installed locally.
- The backend starts successfully at `http://127.0.0.1:8000` with one worker.
- The React frontend compiles and starts successfully at `http://localhost:3000`.
- Local login works with credentials held in local configuration.
- The dashboard and seeded stores load after authentication.
- Stores appear OFFLINE when no receiver is connected; this is expected.
- The configurable Windows SQLite path fixes are present.
- A pure receiver status and acknowledgement contract is defined and unit-tested.
- The authenticated receiver WebSocket path now applies that contract to
  process-local immutable receiver snapshots.
- A local non-audio protocol simulator has exercised the authenticated contract
  over a real `127.0.0.1` WebSocket against an isolated Uvicorn instance.

These statements are the verified starting state supplied for this baseline.
They do not establish receiver playback, audible speakers, production
readiness, or end-to-end WebSocket audio streaming.

## Git baseline

- Current branch: `feature/receiver-status-contract`
- Historical baseline branch: `feature/baseline-verification`
- `2d3c4f8 feat: define receiver acknowledgement contract`
- `e3477a4 test: make local integration tests safe`
- `44f6f63 docs: document verified local baseline`
- `07a8392 fix: preserve configurable SQLite path`
- `e540ae8 fix: resolve SQLite path on Windows`
- `ae4c368 chore: import original EchoCast AI baseline`
- The working tree was clean before this baseline task began.
- Local `.env`, database, WAL, and SHM files are ignored and untracked.

## Exact startup commands

Backend, from the repository root in PowerShell:

```powershell
Set-Location backend
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& .\.venv\Scripts\Activate.ps1
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --workers 1
```

Frontend, from the repository root in a second PowerShell window:

```powershell
Set-Location frontend
yarn start
```

Stop each process with `Ctrl+C` in its own terminal.

## Known issues

- No Windows Receiver Agent is connected or verified.
- Receiver playback, FFmpeg decoding/output, amplifier delivery, and audible
  speaker output have not been tested.
- WebSocket broadcast and receiver state is process-local and supports only one
  backend worker.
- Receiver connection, readiness, audio receipt, playback confirmation, and
  speaker verification are modeled as independent axes. The runtime receiver
  WebSocket uses these axes, but the frontend still displays its existing
  coarse store/target fields.
- Live receiver snapshots are process-local and are lost on backend restart.
- No production Windows Receiver Agent has exercised the typed acknowledgement
  protocol yet; only the local non-audio simulator has done so.

## Receiver status contract

`RECEIVER_STATUS_CONTRACT.md` and `backend/receiver_contract.py` define:

- Independent connection, readiness, playback, and acoustic state axes.
- Strict Pydantic acknowledgement schemas with UTC timestamps, UUID message
  identifiers, monotonic sequence numbers, and session matching.
- Explicit transition, duplicate, ordering, session, and timestamp errors.
- Server-derived stale state at 15 seconds and offline state at 30 seconds.
- A separate trusted EchoGuard schema/path for `SPEAKER_VERIFIED` that ordinary
  receiver parsing cannot invoke.
- Pure immutable state functions that remain independent of FastAPI,
  WebSockets, SQLAlchemy, the frontend, Receiver Agent, and audio streaming.

The authenticated receiver WebSocket now parses every ordinary acknowledgement
through this contract. Each accepted connection gets an immutable live
snapshot. Message UUID deduplication, monotonic sequence enforcement, active
session matching, and the 15-second stale/30-second offline boundaries are
applied in memory. Heartbeats refresh freshness only. PLAY dispatch leaves the
snapshot `STOPPED`, leaves the target `pending`, and does not set
`started_playing_at`.

Meaningful acknowledgements are recorded using the existing schema. A matching
`audio_receiving` changes the target to `audio_receiving`; a matching
`playback_confirmed` changes it to `playback_confirmed` and uses server UTC
receipt time for `started_playing_at`; errors and matching `stopped` events are
also recorded. Heartbeats are not written to SQLite. Receiver-supplied free-form
error details are not persisted; only the bounded error code is retained.
Ordinary receiver parsing cannot invoke the trusted speaker-verification path.

## Local receiver protocol simulator

`tools/receiver_simulator.py` is a standalone Python 3.12 client for protocol
testing. It does not capture, send, decode, or play audio and does not invoke
FFmpeg. By default it accepts only an explicit `ws://` URL using a literal
loopback IP address and port. A clearly named `--allow-non-loopback` option is
required to override that safety boundary.

Supply the receiver credential without placing it in simulator output:

```powershell
$env:ECHOCAST_RECEIVER_TOKEN = '<isolated-test-receiver-credential>'
python tools\receiver_simulator.py `
  --url ws://127.0.0.1:8000/api/ws/receiver `
  --scenario ready-only
Remove-Item Env:ECHOCAST_RECEIVER_TOKEN
```

Available deterministic scenarios are `ready-only`, `successful-playback`,
`playback-error`, `device-error`, `duplicate-message-rejection`,
`out-of-order-sequence-rejection`, `wrong-session-rejection`, and `stopped`.
Media-scoped scenarios require `--session-id` and a matching active server
session. The simulator always generates UUID message IDs, UTC timestamps, and
monotonic normal-message sequences. It cannot generate `speaker_verified`.

## Current security limitations

- CORS defaults to `*` unless `CORS_ORIGINS` is configured.
- HQ WebSocket authentication currently places JWTs in query parameters.
- Receiver tokens are used in receiver URLs and are returned by store APIs.
- Development credential defaults exist in current application/UI code.
- JWT revocation is not implemented; logout only removes the browser token.
- TLS termination, secret rotation, receiver provisioning, authorization roles,
  audit retention, and production hardening are not verified.

Do not place raw receiver tokens in production URLs. Do not print or log
passwords, JWTs, or receiver tokens.

## Current testing limitations

- The pre-existing integration suite was designed for an old Emergent endpoint
  and includes destructive/write scenarios.
- It now requires an explicit test URL, explicit isolated-database confirmation,
  and environment-supplied test credentials; non-local targets require a clearly
  named opt-in flag.
- The minimal smoke suite imports the app only after assigning a pytest
  temporary SQLite database and runtime-only test secrets.
- The smoke scope covers app import, SQLite connection, `/docs`,
  `/openapi.json`, unauthenticated `/api/auth/me`, development login, and an
  authenticated store listing.
- WebSocket streaming, concurrent receivers, audio delivery, Windows playback,
  and acoustic speaker output remain untested.
- Existing `backend/pytest.ini` xdist configuration is preserved.
- Pure contract tests do not import the server, start Uvicorn, use sockets, or
  access SQLite.
- Receiver WebSocket integration tests use an in-process fake WebSocket and a
  unique pytest temporary SQLite database. They do not start Uvicorn, open a
  network socket, stream audio, or access `backend/echocast_live.db`.
- Simulator integration tests start one Uvicorn worker on a random
  `127.0.0.1` port, provision generated credentials, and use a unique temporary
  SQLite database. Production database metadata is checked before and after.
- The isolated backend suite currently reports 62 passed and 1 guarded skip;
  dependency/deprecation warnings remain.

## Database safety

The real database is `backend/echocast_live.db` unless `ECHOCAST_DB_PATH` is
configured. It must not be deleted, renamed, reset, or reseeded as a testing
shortcut. Automated smoke tests use a unique pytest temporary database. Future
schema changes require migrations that preserve transactions, foreign keys,
indexes, and existing data.

## Next recommended engineering task

Design and test header-based Receiver WebSocket authentication so production
receiver credentials no longer need to appear in URL paths. Keep any transition
backward-compatible and do not begin audio or Windows Receiver Agent work in
the same change.
