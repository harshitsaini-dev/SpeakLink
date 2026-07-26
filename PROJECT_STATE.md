# SpeakLink Project State

Last updated: 2026-07-25
Current branch: `feat/canonical-store-zone-catalog`

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

- Current branch: `docs/receiver-production-cutover-runbook`
- `9b6a936 test: rehearse receiver credential cutover`
- `1fc09f5 feat: add receiver dual authentication runtime boundary`
- `21ba47d feat: track receiver authentication source connections`
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

## Production cutover and key-custody review documents

`RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md` now defines a review-only,
maintenance-mode sequence from authorization through staged pilot expansion.
It covers separate database/WAL/SHM and HMAC-key backups, isolated restore
verification, additive migration, complete fleet backfill, controlled
`backfilled -> dual_verify -> hash_only` transitions, both approved rollbacks,
fresh source-count blockers, stop conditions, and emergency abort behavior.
The pilot expands through 1, 3, 5, and 10 Stores before the remaining fleet.

`RECEIVER_HMAC_KEY_CUSTODY.md` defines key purpose, positive versioning,
fail-closed lookup, Windows-compatible storage choices, role separation,
handling controls, separate encrypted recovery, loss/compromise response, and
rotation design. It selects no final platform and contains no real key material.
Stored HMACs cannot be converted to a new-key hash without a presented raw
credential; old versions remain required until no usable rows depend on them.

These documents do not authorize or execute production work. The default app
remains legacy Store-token-only; no migration, backup, cutover, key loading,
Receiver connection, frontend change, or database operation was performed.
`raw_neutralized` is excluded from the first pilot and requires a separate
reviewed migration because raw credentials cannot be reconstructed from hashes.
Authentication, connection freshness, readiness, software playback, and
trusted acoustic speaker verification remain separate evidence.

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
- Pure production-runbook contract tests report 9 passed. They read only the
  two Markdown deliverables and validate required phases, transition order,
  one-worker/source blockers, separate backups, staged cohorts, status-axis
  separation, neutralization exclusion, emergency controls, and absence of
  executable destructive or credential-bearing examples. Cutover/transition
  regressions report 56 passed with 21 existing dependency/deprecation
  warnings; runtime/inventory regressions report 54 passed with 5 existing
  warnings. The complete backend suite reports 347 passed, 1 guarded skip, and
  32 existing warnings.

## Receiver hosting, service-identity, and key-storage ADR

A formal security/operations review and hosting/key-storage ADR are complete:
`RECEIVER_SECURITY_OPERATIONS_REVIEW.md` and `RECEIVER_HOSTING_KEY_STORAGE_ADR.md`.
The ADR status is "Proposed for pilot approval" — it selects a provisional pilot
baseline and explicitly does not implement, configure, or execute it.

The provisional selected pilot model is: a dedicated supported Windows
Server/VM (not a developer laptop, not an all-Store deployment); one Uvicorn
worker; a dedicated non-admin Windows service identity; a DPAPI-protected
versioned HMAC-key container stored outside Git and SQLite with ACLs
restricted to the service identity and approved recovery operators; a separate
encrypted key backup; local SQLite with strict filesystem permissions and
verified backups; and HTTPS/WSS termination through a separately approved
Windows-compatible layer. No multi-worker deployment is selected.

No implementation occurred: no real HMAC key was generated or loaded, no
Windows service/account/ACL/Task Scheduler job was created, no TLS or network
configuration changed, and `backend/speaklink_live.db` was not opened, copied,
backed up, or modified. The normal application still authenticates only
`Store.receiver_token`; production default behavior remains legacy-only.

Named human approvals are still required before any implementation: a
security approver, operations approver, product owner, database backup owner,
and rollback owner must sign off on the ADR, and every "Not ready or
unresolved" item in the security review's pilot-readiness checklist needs an
owner before Phase E of `RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md` can begin.

Next recommended task: a test-first, implementation-neutral Windows
deployment specification (directory layout, service-identity permissions,
secret-file interface contract, DPAPI protection/unprotection boundary, log
rotation, one-worker startup command, graceful shutdown, health checks, and
Windows auto-start choice) — without creating a real service account, key,
Task Scheduler job, or production database.

## Canonical Zone and Store catalog

`backend/store_catalog.py` is now the single source of truth for the approved
retail catalog: exactly **9 Zones and 44 Stores**. It is validated at import
time and covered by 22 focused tests in
`backend/tests/test_store_catalog.py`.

Mapping onto the existing `Store` model, with **no schema change and no
migration**:

- `Store.store_code` <- canonical short name (already unique and indexed)
- `Store.store_name` <- canonical full name
- `Store.region` <- Zone display name (already indexed; the existing `region`
  broadcast target mode therefore becomes Zone targeting immediately)
- `Store.city` <- Zone display name. The approved source supplies no separate
  city data, so no city value was invented.

Short names are preserved verbatim, including the irregular ones
(Bhogal -> `RMCR`, Mahavir Enclave Dashrathpuri -> `RMME`,
Taimoor Nagar -> `TNS`, Noida Sector 104 -> `NS104`, Devli -> `DEVLI`,
Krishna Nagar 2 -> `KN2`, NIT Faridabad -> `NIT`, RRPL -> `RRPL`).
These are the operator's real store codes and must never be "corrected"
by code. Commit `d29e18e` corrected **14 catalog entries** after the initial
import, comprising **13 short names and 4 full names** (Vikaspuri New Store ->
Vikaspuri New, RRPL New Rajapuri -> RRPL, Vishnu Garden 2 -> Vishnu Garden New,
NIT 1 Faridabad -> NIT Faridabad; Vishnu Garden's short name `VG2` was
unchanged, which is why the short-name count is 13 rather than 14). The
contract test in `backend/tests/test_store_catalog.py` is what caught that
change, and it was realigned to the corrected values.

Backend demo seed data was removed: the previous 13-entry `SAMPLE_STORES`
list in `backend/seed.py` (Mumbai/Pune/Delhi/Gurgaon/Bangalore/Hyderabad/
Chennai/Kolkata/Online) no longer exists. No frontend mock catalog was found
or needed removal — the React dashboard already fetched Stores exclusively
from the existing `/api/stores` and `/api/stores/meta/regions-cities`
endpoints, so there is exactly one authoritative catalog definition.

Frontend changes were display-label only: "By Region" -> "By Zone",
the region `<label>` -> "Zone", the console table header
"City / Region" -> "City / Zone", the Store Management "Region" column
header -> "Zone", and the Add Store form's `region` field label -> "zone".
The `region` API field name, request parameters and `data-testid` values are
unchanged.

`seed_stores()` is a **first-run bootstrap, not a startup reconciler**. If the
Store table already holds any row it inserts, updates and deletes nothing, so
restarting the backend can never mutate an existing fleet, rotate a
`receiver_token`, or disturb Receiver Devices, Broadcast Targets, Events or
history. It performs no blanket `DELETE`, no cascade and no reseed.

The protected real database was **not accessed or modified**. Its metadata was
recorded before and after this task and is byte-identical
(487,424 bytes, LastWriteTimeUtc 2026-07-24 08:48:46). Consequently
`backend/speaklink_live.db` may still contain the old demo Store rows.
Reconciling an already-populated database with the approved catalog —
inserting missing canonical Stores and retiring superseded demo Stores that
may own Receiver Devices, Broadcast Targets and history — remains a
**separate reviewed operation** requiring a verified backup, a dry run and
explicit execution approval. It was deliberately not attempted here.

Store and Receiver Device remain separate entities. The catalog carries
identity only: no Receiver token, HMAC key, credential or other secret.
A Store appearing in the catalog proves only that HQ knows the Store — it is
not evidence of CONNECTED, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED or
SPEAKER_VERIFIED.

Tests: focused catalog 22 passed; Receiver WebSocket/auth/inventory
regressions 71 passed; credential/migration regressions 53 passed;
complete backend suite 388 passed, 1 skipped, 32 existing warnings, green
across 8 consecutive runs. `compileall` succeeded. The frontend was not
changed by the catalog-correction/stabilisation work; its last verified state
is `yarn build` compiling successfully with no frontend test files present.

## Receiver replacement handover fix

`WSManager.connect_receiver` closes the Store's previous socket while holding
`_receiver_lock`. `await old.close(...)` yields the event loop, and
`disconnect_receiver` is synchronous and does not take that lock, so the
outgoing socket's `finally` block could observe itself as the Store's current
connection. The server then wrote `status='offline'` for a Store whose
connection had just been handed to a healthy replacement, breaking the
documented invariant that a superseded or rejected connection produces no
Store health write.

`WSManager` now tracks `_superseded_connection_ids`. A connection is marked
before the close is awaited and cleared once its inventory record is removed;
`disconnect_receiver` returns `False` for a marked ID. The Store keeps a
current connection ID throughout the handover, so no observer sees a gap and
the existing capacity-rejection semantics are unchanged.

`backend/tests/test_receiver_replacement_race.py` forces the interleaving
explicitly rather than relying on scheduler timing, so the ordering cannot
regress silently. It uses no SQLite database, no socket and no credentials,
and imports `ws_manager` lazily so it cannot pollute the other suites'
`sys.modules` purity assertions under `--dist loadscope`.

A separate synchronisation gap was fixed in
`backend/tests/test_receiver_cutover_rehearsal.py`: after closing a socket it
waited only for the connection inventory to empty, but the server removes the
inventory record *before* writing Store health. The wait now also requires the
`offline` health write to land, so the test no longer captures a stale
`online` row that the pending write invalidates mid-assertion.

Both issues were latent. They surfaced when the catalog contract tests changed
from failing fast to passing, which shifted xdist worker timing. The full
backend suite was approximately 50% flaky at that point and is now green
across 8 consecutive runs.

## Store catalog reconciliation report

`backend/store_catalog_reconciliation.py` compares an explicitly supplied,
operator-isolated SQLite snapshot against the canonical catalog and reports the
difference. It is **implemented and strictly read-only**; it performs no
cleanup and authorizes none. `STORE_CATALOG_RECONCILIATION_RUNBOOK.md`
documents it in full.

Usage (from `backend`, with the project virtual environment):

```text
python -m store_catalog_reconciliation --database <ISOLATED_SNAPSHOT_PATH>
python -m store_catalog_reconciliation --database <ISOLATED_SNAPSHOT_PATH> --format json
```

Exit codes: `0` exact canonical match, `2` report completed and differences
found, `1` input/schema/safety failure including protected-path refusal.

Read-only guarantees, all covered by tests:

- `backend/speaklink_live.db` is refused **before any connection is opened**,
  by exact path, by a relative or `..` path that resolves to it, by a path
  supplied from another working directory, and by same-file identity via
  `os.path.samefile`. Filename comparison alone is never used.
- The snapshot is opened through a SQLite `mode=ro` URI with
  `PRAGMA query_only = ON`. A dedicated test proves `UPDATE`, `DELETE` and
  `CREATE TABLE` all raise on that connection.
- No `INSERT`/`UPDATE`/`DELETE`/`ATTACH`/`VACUUM`, temporary table,
  journal-mode change, migration or seed call exists in the module.
- Columns are always listed explicitly; `SELECT *` is never used and the Store
  credential column is never selected, so credential material cannot reach a
  report, log or terminal.
- A snapshot with an adjacent `-wal`/`-shm` file fails closed rather than being
  read, merged or repaired.
- Tests assert the snapshot's SHA-256, size and mtime are unchanged, and that
  Store rows and dependent rows are unchanged.

Classifications: `EXACT_CANONICAL_MATCH`, `CANONICAL_FIELD_MISMATCH`,
`KNOWN_LEGACY_DEMO_EXACT_MATCH`, `CUSTOM_OR_UNKNOWN_NON_CANONICAL`,
`AMBIGUOUS_IDENTITY_CONFLICT`. The legacy-demo fingerprint requires all five of
`(store_code, store_name, city, region, is_online_store)` to match exactly,
recovered read-only from `git show af168aa:backend/seed.py`; no approximate,
prefix, positional or case-insensitive matching is used, so a custom Store is
never misclassified as demo data.

Dependency counts cover every relationship proven from source:
`broadcast_targets.store_id` and `receiver_events.store_id` (always present,
from `models.py`), plus `receiver_devices.store_id`,
`receiver_credential_events.store_id` and `receiver_credentials` via
`receiver_devices` (Phase 1 migration only, from `migrations.py`). A table
absent from the snapshot reports `n/a`, never a proven `0`.

Recommendations are advisory only and are never executed. A proven demo row
that still has dependencies is recommended for `REVIEW_ARCHIVAL`, not deletion,
because `receiver_devices.store_id` is `ON DELETE RESTRICT` and history would
otherwise break.

The protected real database was **not opened, copied, queried or modified** by
this work, and **no reconciliation was run against real data**. Its metadata
was identical before and after: 487,424 bytes, LastWriteTimeUtc
2026-07-25 11:12:47. No cleanup of any kind was executed.

Tests: 41 focused reconciliation tests passed; complete backend suite 429
passed, 1 skipped, 32 existing warnings. `compileall` succeeded. No frontend,
runtime, authentication, WebSocket, seed or migration behaviour changed.

## Windows Deployment Specification

Status: **not started**. `RECEIVER_HOSTING_KEY_STORAGE_ADR.md` remains
"Proposed for pilot approval" and no deployment specification, Windows service
identity, DPAPI secret container, ACL layout or TLS configuration has been
written or executed.

## Database safety

The real database is `backend/speaklink_live.db` unless `SPEAKLINK_DB_PATH` is
configured. It must not be deleted, renamed, reset, or reseeded as a testing
shortcut. Automated smoke tests use a unique pytest temporary database. Future
schema changes require migrations that preserve transactions, foreign keys,
indexes, and existing data.

## Next recommended engineering task

Run the reconciliation report **once**, against an operator-produced isolated
snapshot of the real database, and review the output together. That means the
operator stops the backend, takes a quiesced copy of
`backend/speaklink_live.db` plus any WAL/SHM files to a path outside the
repository, and supplies that path. The tool then reports what the live fleet
actually contains. Nothing is changed, and the result decides whether a
cleanup task is even needed.

Only after that review should a cleanup task be considered, and it would still
require a verified backup, a reviewed change record, a dry run and an explicit
human decision per row.

The test-first Windows deployment specification (directory layout, dedicated
service-identity permissions, secret-file interface contract, DPAPI
protection/unprotection boundary, log rotation, one-worker startup command,
graceful shutdown, health checks, Windows auto-start choice) remains **not
started**.
