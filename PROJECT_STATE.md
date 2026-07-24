# SpeakLink Project State

Last updated: 2026-07-24
Current branch: `feature/receiver-connection-inventory-runtime`

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
- The production receiver WebSocket uses a credential-free endpoint and strict
  `Authorization: Bearer` handshake authentication.
- A proposed Receiver Device/Credential lifecycle, SQLite-safe migration plan,
  and schema-independent security helpers are documented and pure-tested.

These statements are the verified starting state supplied for this baseline.
They do not establish receiver playback, audible speakers, production
readiness, or end-to-end WebSocket audio streaming.

## Git baseline

- Current branch: `feature/receiver-connection-inventory-runtime`
- `09077e0 feat: add receiver connection source inventory`
- `210b0a2 security: rehearse receiver migration state transitions`
- `a647a9f security: add isolated receiver dual verification`
- `3ab9d0f security: rehearse receiver credential backfill`
- Historical baseline branch: `feature/baseline-verification`
- `6cb48a1 security: add isolated receiver enrollment service`
- `139f39f security: add receiver credential migration phase one`
- `f3c021b security: authenticate receiver websocket by header`
- `873c655 test: add receiver protocol simulator`
- `fc0b350 feat: integrate receiver acknowledgement state`
- `2d3c4f8 feat: define receiver acknowledgement contract`
- `e3477a4 test: make local integration tests safe`
- `44f6f63 docs: document verified local baseline`
- `07a8392 fix: preserve configurable SQLite path`
- `e540ae8 fix: resolve SQLite path on Windows`
- `ae4c368 chore: import original SpeakLink baseline`
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
- A separate trusted LinkGuard schema/path for `SPEAKER_VERIFIED` that ordinary
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

The simulator connects to `/api/ws/receiver` and sends its credential only in
the WebSocket handshake `Authorization: Bearer` header. It never appends the
credential to the URL or prints handshake headers.

Supply the receiver credential without placing it in simulator output:

```powershell
$env:SPEAKLINK_RECEIVER_TOKEN = '<isolated-test-receiver-credential>'
python tools\receiver_simulator.py `
  --url ws://127.0.0.1:8000/api/ws/receiver `
  --scenario ready-only
Remove-Item Env:SPEAKLINK_RECEIVER_TOKEN
```

Available deterministic scenarios are `ready-only`, `successful-playback`,
`playback-error`, `device-error`, `duplicate-message-rejection`,
`out-of-order-sequence-rejection`, `wrong-session-rejection`, and `stopped`.
Media-scoped scenarios require `--session-id` and a matching active server
session. The simulator always generates UUID message IDs, UTC timestamps, and
monotonic normal-message sequences. It cannot generate `speaker_verified`.

## Receiver WebSocket authentication

The production endpoint is `/api/ws/receiver`. Missing, malformed, duplicate,
or invalid Authorization credentials are rejected before WebSocket acceptance,
snapshot creation, online registration, or database health updates. All
failures use close code `4401` and the fixed reason `Receiver authentication
failed`, without revealing whether a token exists. Active store credentials are
checked with constant-time comparisons.

The old `/api/ws/receiver/{token}` route has been removed with no compatibility
fallback. Query-string receiver credentials are not accepted by the production
endpoint. Production deployments must use TLS and `wss://`; header
authentication does not protect credentials over plaintext networks.

The React Receiver page is development-only legacy code. Native browser
WebSocket cannot set an Authorization header, and the page still obtains a
credential from its own `?token=` URL and uses the legacy verification API.
It therefore cannot connect to the production Receiver WebSocket. No insecure
browser compatibility route or development flag was added. Store Management's
generated receiver link and historical PRD/test-report references are also
legacy and must not be used for a production receiver.

## Current security limitations

- CORS defaults to `*` unless `CORS_ORIGINS` is configured.
- HQ WebSocket authentication currently places JWTs in query parameters.
- The authenticated HQ store APIs still return raw receiver tokens.
- The development-only browser Receiver page and verification endpoint still
  use receiver credentials in query strings; they are not production transport.
- Development credential defaults exist in current application/UI code.
- JWT revocation is not implemented; logout only removes the browser token.
- TLS termination, secret rotation, receiver provisioning, authorization roles,
  audit retention, and production hardening are not verified.

Do not place raw receiver tokens in production URLs. Do not print or log
passwords, JWTs, or receiver tokens.

## Receiver credential lifecycle Phase 1

`RECEIVER_CREDENTIAL_LIFECYCLE.md` defines separate `receiver_devices`,
`receiver_credentials`, structured credential audit events, and an explicit
migration-state record. Phase 1 now has an explicit versioned runner in
`backend/migrations.py`, validated only with pytest temporary SQLite files.
It creates additive tables, indexes, foreign keys, check/unique constraints,
and initial `legacy_only` state in one `BEGIN IMMEDIATE` transaction.

`backend/receiver_credentials.py` contains pure helpers for secure credential
generation, strict new/legacy migration formats, keyed hashing, constant-time
verification, UTC lifecycle boundaries, rotation planning, redacted
representations, allowlisted audit metadata, the approved two-active-device
limit, and the approved maximum 15-minute rotation grace.

Phase 1 is deliberately not invoked by `server.py`, not registered with
`Base.metadata.create_all`, and does not add ORM models. It preserves
`Store.receiver_token` and current WebSocket authentication unchanged. The
runner takes an explicitly supplied SQLite engine and refuses the protected
`backend/speaklink_live.db` path before connecting unless a future reviewed
maintenance-mode caller explicitly opts in. No credentials are backfilled.

Approved initial policies are maximum two active devices per Store,
non-expiring but revocable credentials, maximum 15-minute planned rotation
grace, immediate compromise invalidation, external HMAC keys, and at least 12
months of credential-audit retention.

## Receiver Device enrollment service Phase 2

`backend/receiver_device_service.py` now provides an isolated, typed enrollment
service over an explicitly injected SQLite engine. It validates the complete
Phase 1 contract and exact `legacy_only` state, active Store and actor, bounded
display name, injected HMAC key/version, UTC expiry, and the maximum two active
devices per Store. Disabled and retired devices do not count as active slots.

One `BEGIN IMMEDIATE` transaction creates an active device, version-1 hashed
credential, and two sanitized audit events. The raw credential is returned
through a redacted one-time delivery object and is never persisted. Rollback
tests cover failures after both device and credential insertion, and a
barrier-synchronized test proves concurrent connections cannot exceed two
active devices.

The service is not called by server startup, FastAPI, WebSockets, Store APIs,
the simulator, frontend, or Receiver Agent. `Store.receiver_token`, Store
runtime health, `schema_migrations`, and migration state remain unchanged.
These isolated-test credentials are not accepted by production authentication.

## Legacy Receiver Credential backfill rehearsal

`backend/receiver_credential_backfill.py` provides an isolated, fleet-wide
legacy backfill rehearsal over an explicitly injected temporary SQLite engine.
It refuses the protected real database before connection and uses one
`BEGIN IMMEDIATE` transaction.

Every Store receives one Device and one `legacy_uuid_hex` hash-only credential.
Active Stores map to active Devices; inactive Stores map to disabled Devices
with the supplied UTC timestamp. The service validates every legacy token
before writes, preserves every Store identity/token/operational field and the
schema ledger, writes two sanitized events per Store plus one state-change
event, and changes only temporary migration state from `legacy_only` to
`backfilled`. Empty fleets and partial/conflicting data fail closed.

All intermediate inserts and state changes roll back together. A validated
second call raises `BackfillAlreadyAppliedError` without duplicating rows, and
concurrent calls serialize so only one can perform the rehearsal. Legacy
verification remains enabled. Production WebSocket authentication is still
legacy Store-token verification only; runtime dual verification is disabled.

## Isolated Receiver Credential authentication service

`backend/receiver_auth_service.py` now provides a read-only, migration-state
governed identity verifier over an explicitly injected temporary SQLite engine
and external HMAC key ring. It implements the exact `legacy_only`, `backfilled`,
`dual_verify`, `hash_only`, and `raw_neutralized` policy matrix and fails closed
on inconsistent flags, schema, foreign keys, mappings, key versions, or
credential lifecycle state.

Legacy UUID-hex and `speaklink_rcv` formats remain strictly separated. Dual
verification canonicalizes a matching raw/HMAC legacy identity and rejects
disagreement. Hash-backed verification requires an active Store, active Device,
and usable credential. Results are immutable, redacted identity records, and
external credential rejection messages are identical.

The service performs no commits or persistent updates: Store operational
fields, `last_used_at`, audit events, migration state, schema ledger, and live
receiver snapshots remain unchanged. It is not connected to runtime or the
frontend. Authentication success does not imply READY, playback confirmation,
or speaker verification.

## Isolated migration-state transition service

`backend/receiver_migration_transition_service.py` rehearses only the four
approved adjacent transitions among `backfilled`, `dual_verify`, and
`hash_only`. It uses an explicitly injected temporary SQLite engine, bounded
HMAC key ring, active HQ actor, UTC time, and one `BEGIN IMMEDIATE` transaction.

Raw readiness, complete backfilled mappings, hash readiness, key versions,
foreign keys, and state/flag consistency are validated before the state row is
changed. Exactly one sanitized `migration_state_changed` audit event is
appended on success. Store, Device, Credential, schema-ledger, and existing
audit rows are preserved; state and audit changes roll back together.

An immutable connection summary contains only legacy/hash-authenticated counts
and a UTC capture time. Narrowing acceptance requires a summary no older than
30 seconds and zero connections using the path being disabled. The service
does not inspect, disconnect, re-authenticate, or create live WebSockets and
does not modify receiver snapshots or health axes. `raw_neutralized` remains
out of scope.

## Isolated active Receiver connection inventory

`backend/receiver_connection_inventory.py` provides a bounded, thread-safe,
process-local inventory for future WebSocket integration. Immutable records
contain only connection ID, Store ID, optional Device/Credential IDs, UTC
authentication time, and the handshake source (`legacy_store_token` or
`hashed_device_credential`). No socket, credential material, receiver snapshot,
health axis, session, or audio data is stored.

Registration is capacity-limited (256 by default, configurable from 1 through
4096). An identical duplicate is idempotent; a conflicting reuse of a
connection ID fails closed without replacement. Exact removal is idempotent,
and the generation changes only on real mutations. Lock-protected snapshots
atomically reconcile total, source, and per-Store counts, sort records
deterministically, and remain immutable after later changes.

The transition-summary adapter populates the existing summary shape from one
atomic snapshot via an injected constructor. It does not query SQLite, invoke a
transition, or couple the pure inventory module to SQLAlchemy. Authentication
source remains identity metadata only and never implies READY, playback, or
speaker verification.

The pure inventory does not import FastAPI, WebSockets, SQLAlchemy, production
authentication, or the transition service. The runtime manager now owns one
instance as described next, but the state is still lost on restart and is not
shared across workers, so the backend remains limited to one Uvicorn worker.
An empty new inventory after restart is not independent proof that remote
sockets or speakers are inactive.

## Legacy Receiver WebSocket inventory runtime

The current production Receiver WebSocket now registers each successfully
accepted legacy Store-token handshake in the manager-owned process-local
inventory. The server generates an immutable UUID-hex connection ID and UTC
authentication time; records contain Store identity and
`legacy_store_token` only, with no Device/Credential ID or credential material.
Failed authentication never reaches manager or inventory registration.

The existing one-current-socket-per-Store design remains. A replacement removes
the old exact inventory ID before installing a new ID. Disconnect and finally
cleanup require both the socket and connection ID to remain current, so delayed
cleanup from an older socket cannot remove or mark its replacement offline.
Cleanup is idempotent across normal disconnect, abrupt failure, protocol error,
send failure, cancellation, and replacement. Stale sockets cannot apply
acknowledgements or freshness changes to replacement snapshots.

The manager can construct the existing transition summary from one atomic
inventory snapshot. No transition is invoked and no API route is exposed.
Normal-application runtime counts are legacy-only; hashed authentication is
available only to explicitly injected isolated applications.
Authentication/connection identity still does not imply READY, audio receipt,
playback confirmation, LinkGuard verification, or audible speaker output.

This runtime state is process-local, observes only post-start registrations,
and disappears on restart. Multiple workers would have independent inventories,
so exactly one Uvicorn worker remains required. No credential migration, schema
change, real-database transition, dual-verification cutover, frontend change,
Receiver Agent, or audio behavior is included.

## Explicit dual-authentication runtime boundary

The Receiver WebSocket now asks an injected typed authenticator for an immutable
non-secret identity before acceptance. The normal application explicitly uses
the legacy implementation, so production behavior remains active raw
`Store.receiver_token` verification only and runtime inventory records remain
`legacy_store_token` with no Device/Credential IDs. Default startup does not
read migration state or require an HMAC key.

An isolated application may explicitly inject the migration-aware adapter with
a temporary SQLite engine and bounded HMAC key ring. It preserves the existing
read-only service matrix: legacy-only/backfilled raw success is classified
`legacy_store_token`; dual-verified, hash-only, and raw-neutralized hash-backed
success is `hashed_device_credential`. In `dual_verify`, an identity-consistent
legacy UUID match across raw and hash paths is canonically hash-backed with its
exact Device/Credential IDs. The client cannot choose or change this source.

Manager registration propagates that authoritative source and identity into the
process-local inventory. Replacements work in both source directions and exact
connection-ID cleanup prevents an older finally block from removing or marking
the replacement offline. A dual-matched legacy UUID therefore contributes to
the hashed summary count: it does not block `dual_verify -> hash_only`, but it
does block `dual_verify -> backfilled` until it disconnects or re-authenticates.

The credential, audit, migration-state, and schema-ledger tables remain
read-only during handshake. Existing Store connection-health writes occur only
after successful manager/inventory registration. Authentication failure and
inventory capacity failure produce no health write or Receiver snapshot.
Authentication source is immutable connection identity—not READY, playback,
acoustic verification, or speaker-output proof.

There is no automatic hashed-authentication cutover, environment switch,
transition API, public configuration route, frontend change, or real-database
migration. Migration-aware behavior remains explicit temporary-test injection.
Inventory remains process-local, so one Uvicorn worker is still required.

## Isolated Receiver credential cutover rehearsal

`backend/receiver_cutover_rehearsal.py` now composes the explicit
migration-aware authenticator, manager-owned source inventory, bounded injected
HMAC key ring, and transactional transition service for temporary databases.
It validates that the independently supplied authenticator, engine, and key
ring describe the same isolated rehearsal and refuses the protected real
database path before connection. It creates no FastAPI route, startup action,
environment switch, or default key loader.

The tested forward sequence is `backfilled -> dual_verify -> hash_only`; the
rollback is `hash_only -> dual_verify -> backfilled`. Expanding transitions do
not disconnect or relabel existing sockets. Narrowing to hash-only requires a
fresh inventory summary with zero legacy-source connections; narrowing to
backfilled requires zero hash-source connections. A socket source remains
immutable until disconnect and a new authenticated connection ID.

Test fixtures inject generated version-1 backfill and version-2 enrollment
keys. Both versions are required for full hash readiness. Each successful step
changes only the temporary migration-state row and appends one sanitized audit
event. Blocked, stale, missing-key, wrong-key, concurrent stale-state, and
injected-hook failures leave state and protected rows unchanged.

A one-worker loopback application on a random `127.0.0.1` port exercises real
Bearer handshakes, canonical legacy/hashed inventory sources, replacement,
acknowledgement separation, capacity failure, and the complete rollback. The
normal imported application remains bound to the legacy-only authenticator.
No real cutover, public transition API, frontend change, Receiver Agent, audio,
FFmpeg, or LinkGuard behavior is included.

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
  network socket, stream audio, or access `backend/speaklink_live.db`.
- Simulator integration tests start one Uvicorn worker on a random
  `127.0.0.1` port, provision generated credentials, and use a unique temporary
  SQLite database. Production database metadata is checked before and after.
- Header-authentication tests verify fixed rejection behavior and prove failed
  handshakes create no snapshot, online state, last-seen change, status change,
  or receiver event.
- Pure credential lifecycle helpers run without FastAPI, SQLAlchemy, sockets,
  environment secrets, or SQLite.
- Phase 1 migration tests use only pytest temporary SQLite files and cover an
  empty file, an isolated legacy Store, indexes/constraints, foreign-key
  enforcement, idempotency, deliberate rollback, and protected-path refusal.
- Phase 1 migration plus pure credential tests report 21 passed. The complete
  isolated backend suite reports 86 passed, 1 guarded skip, and 8 existing
  dependency/deprecation warnings. Python compilation also succeeds.
- Phase 2 enrollment service tests report 26 passed. The complete isolated
  backend suite reports 112 passed, 1 guarded skip, and 8 existing dependency/
  deprecation warnings. Python compilation succeeds.
- Focused legacy backfill rehearsal tests report 19 passed. Phase 1,
  credential-helper, and Phase 2 enrollment regressions report 47 passed. The
  complete isolated backend suite reports 131 passed, 1 guarded skip, and 8
  existing dependency/deprecation warnings. Python compilation succeeds.
- Focused read-only authentication-service tests report 32 passed. Credential
  lifecycle regressions report 66 passed; existing Receiver WebSocket
  authentication and contract regressions report 17 passed with 5 existing
  dependency/deprecation warnings. The complete isolated backend suite reports
  163 passed, 1 guarded skip, and 8 existing warnings. Python compilation
  succeeds.
- Focused migration-transition tests report 49 passed. Credential lifecycle
  service regressions report 98 passed; existing Receiver WebSocket tests
  report 17 passed with 5 existing dependency/deprecation warnings. The
  complete isolated backend suite reports 212 passed, 1 guarded skip, and 8
  existing warnings. Python compilation succeeds.
- Focused connection-inventory tests report 65 passed. They use no SQLite,
  FastAPI, Uvicorn, WebSocket, network socket, or production secret and cover
  capacity races, duplicate/conflict races, snapshot reconciliation, immutable
  history, source counts, and representative 40-Store load. Authentication and
  transition regressions report 81 passed; lifecycle regressions report 66
  passed; existing Receiver WebSocket regressions report 17 passed with 5
  existing warnings. The complete isolated backend suite reports 277 passed,
  1 guarded skip, and 8 existing warnings. Python compilation succeeds.
- Focused legacy WebSocket inventory integration tests report 20 passed with 3
  existing dependency/deprecation warnings using one pytest temporary SQLite
  database and in-process fake WebSockets. They cover authentication failure,
  legacy-only registration, exact disconnect/replacement cleanup, capacity,
  summaries, contract-axis separation, concurrency, browser query-token
  rejection, and protected-database metadata. Pure inventory regressions report
  65 passed; authentication/transition regressions report 81 passed; existing
  Receiver WebSocket regressions report 17 passed with 5 existing warnings.
  The complete backend suite reports 297 passed, 1 guarded skip, and 10 existing
  warnings. Python compilation succeeds.
- Focused explicit dual-authentication runtime-boundary tests report 34 passed
  with 3 existing dependency/deprecation warnings using generated credentials,
  injected test-only HMAC keys, in-process fake WebSockets, and one pytest
  temporary SQLite database. Runtime inventory regressions report 85 passed;
  authentication/transition regressions report 81 passed; lifecycle
  regressions report 66 passed; existing Receiver WebSocket regressions report
  17 passed. The complete backend suite reports 331 passed, 1 guarded skip, and
  12 existing warnings. Python compilation succeeds.
- Focused controlled-cutover rehearsal tests report 7 passed with 21
  dependency/deprecation warnings. They use generated credentials and HMAC
  keys, temporary file-backed SQLite databases, a random `127.0.0.1` port, and
  exactly one Uvicorn worker. Runtime/inventory regressions report 119 passed;
  authentication/transition regressions 81 passed; lifecycle regressions 66
  passed; and existing Receiver WebSocket regressions 17 passed. The complete
  backend suite reports 338 passed, 1 guarded skip, and 32 warnings. Python
  compilation succeeds.

## Database safety

The real database is `backend/speaklink_live.db` unless `SPEAKLINK_DB_PATH` is
configured. It must not be deleted, renamed, reset, or reseeded as a testing
shortcut. Automated smoke tests use a unique pytest temporary database. Future
schema changes require migrations that preserve transactions, foreign keys,
indexes, and existing data.

## Next recommended engineering task

Design a production cutover runbook and key-custody configuration contract for
review, without executing it. It should define maintenance-mode authorization,
verified database/WAL/SHM and external-key backups, operator approvals,
connection draining, rollback checkpoints, observability, and pilot acceptance
criteria before any real migration or default-runtime change.
