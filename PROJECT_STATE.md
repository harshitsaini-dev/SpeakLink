# SpeakLink Project State

Last updated: 2026-07-26
Current branch: `test/one-store-bluetooth-amplifier-live`

> **Live audio now reaches a real amplifier on one Store.** A spoken
> announcement was heard clearly through the speakers connected to a Bluetooth
> amplifier — see
> [`ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md)
> and the summary at the end of this document. `SPEAKER_VERIFIED` remains
> `NOT_IMPLEMENTED` and the project is `NOT_READY_FOR_PRODUCTION`.

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

## Local one-Store pilot readiness harness

Status: **READY_FOR_LOCAL_SOFTWARE_PILOT_TEST**. This is a local software
readiness statement only. It is explicitly **not** READY_FOR_PRODUCTION, not
READY_FOR_LIVE_AUDIO, not READY_FOR_STORE_SPEAKERS, not SPEAKER_VERIFIED and
not ALL_40_STORES_READY.

`tools/local_pilot.py` prepares an isolated, disposable pilot database outside
the repository and runs a loopback-only software smoke test against it.
`LOCAL_PILOT_TEST_RUNBOOK.md` is the beginner-facing Windows runbook, and
`scripts/Start-SpeakLinkLocalPilot.ps1` / `scripts/Stop-SpeakLinkLocalPilot.ps1`
wrap manual browser testing.

Isolated pilot design, all outside Git:

```text
%LOCALAPPDATA%\SpeakLink\local-pilot\
    data\speaklink_local_pilot.db      disposable pilot database
    logs\backend.log, smoke-report.json
    runtime\pilot-state.json, PILOT_ONLY, backend.pid, frontend.pid
```

Safety boundary, all covered by tests:

- The protected `backend/speaklink_live.db` is refused by exact path, by a
  relative or `..` path that resolves to it, and by same-file identity via
  `os.path.samefile`.
- A pilot root inside the repository or inside `.git` is refused.
- Reset removes nothing without an explicit `--reset-pilot-db`, requires the
  `PILOT_ONLY` marker, refuses a path outside the validated pilot data
  directory, refuses a symlink, and treats the database plus its `-wal`/`-shm`
  sidecars as one scoped set. It is never a wildcard delete.
- The pilot password is read from the process-scoped `ADMIN_PASSWORD`
  environment variable, is required, and is never printed, logged, persisted or
  placed in a command argument.
- The selected Store's credential is read from the isolated pilot database into
  memory only. It never reaches a log, report, exception, URL or argument. The
  query-string negative test deliberately uses a fixed dummy value because
  Uvicorn logs request URLs.

Verified pilot facts, reused from actual application code rather than
duplicated: `SPEAKLINK_DB_PATH` (backend/db.py), `ADMIN_USERNAME`/
`ADMIN_PASSWORD` (backend/seed.py), `JWT_SECRET` (backend/auth.py),
`CORS_ORIGINS` (backend/server.py), liveness `/docs`, login
`POST /api/auth/login`, Stores `GET /api/stores`, Zones
`GET /api/stores/meta/regions-cities`, Receiver `WS /api/ws/receiver` with an
`Authorization: Bearer` header. The pilot database is initialised by running
the application's own `server.startup_event()` in a child process, so no schema
SQL, catalog data or model definition is duplicated.

Actual recorded run at commit `b8f2292`:

- Pilot database `%LOCALAPPDATA%\SpeakLink\local-pilot\data\speaklink_local_pilot.db`
- SHA-256 `4e0710f859f08ec3cfef947d6797c01f5d52bd4922173cbbb951bf170e766dd4`
- 44 Stores, 9 Zones, `demo_codes_present: []`, reconciliation
  `EXACT_CANONICAL_MATCH`
- Backend `127.0.0.1:61266`, **exactly one Uvicorn worker**, loopback only
- Liveness ok, login ok, Receiver Bearer authentication ok for Store `UN`
- Query-string credential refused
- `observed_connection: CONNECTED`; readiness, playback and acoustic all
  `NOT_REPORTED`; `speaker_verified: false`
- Receiver cleanup ok, graceful shutdown ok, no process left running, port
  released
- Secret scan: `backend.log`, `smoke-report.json`, `pilot-state.json` and
  `PILOT_ONLY` are all clean

Frontend: `yarn test --watchAll=false --passWithNoTests` reports no test files
exist, and `yarn build` compiled successfully. The dashboard still reads the
catalog only from the Store API — a test asserts no frontend file duplicates
canonical Store data. "By Zone" labels are present, play status comes from the
API rather than being assumed, and no frontend file claims SPEAKER_VERIFIED.
**No visual browser test was performed by this task**; the browser checklist in
the runbook is for the operator to run.

Tests: 35 focused local-pilot tests passed; complete backend suite 464 passed,
1 skipped, 32 existing warnings, green across 5 consecutive runs (429 baseline
plus 35 new). `python -m compileall -q backend tools` succeeded.

Protected database impact: **not opened, not copied, not modified** by this
task. A blocker was found and reported during preflight: two Uvicorn processes
and the React dev server were running, and the protected database had a live
910 KB `-wal` file, meaning a live backend was writing to it. The operator
stopped those processes; SQLite then checkpointed the WAL into the main file on
clean shutdown, so the accepted baseline moved from 487,424 bytes /
2026-07-25 11:12:47 to **507,904 bytes / 2026-07-26 07:42:09** with no `-wal`
or `-shm` remaining. That change came from the operator's own running
application, not from any task command, and the baseline was unchanged across
all pilot work afterwards.

What remains before any live audio testing: a Receiver Agent that actually
decodes and plays audio, FFmpeg output control, a verified Windows audio output
device, amplifier routing, LinkGuard, and acoustic verification. None of these
exist yet.

## One-Store live-audio software pilot

Status: **READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST**. This is a software
readiness statement only. It is explicitly **not** READY_FOR_SPEAKER_TEST, not
SPEAKER_VERIFIED, not AMPLIFIER_VERIFIED, not READY_FOR_PRODUCTION and not
ALL_STORES_READY.

`ONE_STORE_LIVE_AUDIO_TEST_RUNBOOK.md` is the operator-facing runbook.

### Audio format

| Property | Value |
| --- | --- |
| Container / codec | WebM / Opus |
| MIME | `audio/webm;codecs=opus` |
| Channels | mono |
| Target bitrate | ~32 kbps |
| Chunk duration | ~250 ms |

These match what the existing React `HQBroadcaster` already produced, so no
browser format change was needed. If a browser cannot produce WebM/Opus the
dashboard now raises an honest codec error and sends nothing; it never
substitutes another format silently.

### Protocol

`backend/audio_protocol.py` adds only the control message the protocol was
missing: `prepare` (backend -> Receiver), carrying the session ID, target Store
and negotiated audio format. `stop` already existed. The Receiver -> backend
direction is **unchanged**: it still uses the frozen `receiver_contract`
acknowledgements, so there is exactly one Receiver protocol and no parallel
system. Validation rejects unknown types, wrong protocol version, missing or
non-positive session/store IDs, unsupported container/codec/channels, and
empty, non-binary or oversized (>256 KiB) chunks.

The diagnostic detail (FFmpeg availability, codec support, sink mode, byte and
chunk counts) is recorded in the Receiver's own secret-free JSON report rather
than being forced into the frozen wire contract.

### Bounded queue

`backend/audio_streaming.py` gives every targeted Store an independent bounded
queue (**24 chunks**, about six seconds at 250 ms) and one sender task.
`WSManager.fanout_audio` now enqueues without ever awaiting a Receiver, so a
slow Store can no longer stall the broadcaster read loop or any other Store -
the previous implementation awaited each Store's socket in sequence with no
bound at all. On overflow the **oldest** chunk is dropped and a per-Store
`dropped` counter records it. `_end_session` closes every queue and cancels
every sender task. No audio chunk is written to a database or a log.

### Receiver and FFmpeg evidence

`tools/audio_receiver_pilot.py` runs one FFmpeg process per session:

```text
ffmpeg -hide_banner -nostdin -loglevel error -f webm -i pipe:0 -ac 1 \
       -progress pipe:1 -f null -
```

Sink mode is **null** on purpose, so nothing can be played through the wrong
Windows audio device.

- `receiver_ready` is sent only after FFmpeg is proven present, Opus/WebM
  decode support is proven, and the bounded queue exists.
- `audio_receiving` is sent only after real audio bytes arrive.
- `playback_confirmed` is sent only after FFmpeg's own `out_time_ms` progress
  counter advances past zero - real decode evidence, not an assumption.
- `speaker_verified` is **never** sent. A null sink cannot know anything about
  output devices, amplifiers or speakers.

`/api/broadcast/current` now also returns `ready_receivers`, derived from the
existing process-local readiness axis. The React console waits for that
acknowledgement before opening the microphone, so no audio byte is sent on the
strength of a command having been issued.

### Recorded end-to-end run

Synthetic deterministic fixture (`-bitexact`, so it is byte-reproducible):
`pilot-tone.webm`, opus, mono, matroska/webm, 4.008 s, 22,967 bytes,
SHA-256 `9fc6898d72dc7b82ff9e5b88f1fddddb8392bf84813d3fdf1e0604d8d4419e2d`,
generated by ffmpeg 8.1.2 and stored outside Git under the pilot root.

Result `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` on `127.0.0.1:63167` with
exactly one Uvicorn worker, Store `UN`:

- CONNECTED, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED, STOPPED - all observed
- `ffmpeg_returncode: 0`, `ffmpeg_decoded_microseconds: 4000000`
- 17 chunks / 22,967 bytes sent; 17 chunks / 22,967 bytes received; **0 dropped**
- `sink_mode: null`, `speaker_verified: false`
- Clean shutdown; no backend, Receiver or FFmpeg process left; port released

The HQ broadcaster endpoint still takes its JWT in the query string (a
pre-existing limitation recorded above). The pilot backend therefore runs with
`--no-access-log`, and the audio backend log was verified to contain no
credential.

### Protected database

**Not opened, not copied, not modified.** Before and after:
507,904 bytes, LastWriteTimeUtc 2026-07-26 07:42:09, no `-wal` or `-shm`.

### Tests

29 protocol/queue tests and 23 audio-pilot tests added. Complete backend suite
**516 passed, 1 skipped, 32 existing warnings, green across 5 consecutive
runs**. `compileall backend tools` succeeded. Frontend `yarn build` compiled;
`yarn test --passWithNoTests` still reports no frontend test files exist, which
is not behavioural frontend coverage. **No manual browser microphone test was
performed by this task** - that checklist is in the runbook for the operator.

### Remaining limitations before any speaker claim

No production Windows Receiver Agent, no Windows output-device selection, no
amplifier or Bluetooth control, no LinkGuard, no acoustic verification, no
TLS/WSS, no security approval, and no multi-Store load evidence.

## One-Store Windows output-device pilot

Status: **READY_FOR_ONE_STORE_WINDOWS_OUTPUT_TEST**. This is explicitly **not**
SPEAKER_VERIFIED, not AMPLIFIER_VERIFIED, not BLUETOOTH_VERIFIED, not
ECHO_GUARD_VERIFIED, not READY_FOR_PRODUCTION and not ALL_STORES_READY.

`ONE_STORE_WINDOWS_OUTPUT_TEST_RUNBOOK.md` is the operator-facing runbook.

### Why a new dependency was required

The installed FFmpeg 8.1.2 build has **no audio output muxer at all** — its
full `-devices` list contains exactly one muxer, `caca` (ASCII-art video).
`ffplay` exists but its `-audio_device_*` options are DirectShow **capture**
options; it plays only to whatever SDL treats as the default. No Python audio
library was installed. So nothing on this machine could send audio to one
**explicitly chosen** Windows endpoint, and Hard Safety Rule 4 forbids silently
using the default device.

`sounddevice==0.5.2` (MIT, PortAudio V19.7.0, Python 3.12 + Windows) was added
after operator approval. Its only requirement, `cffi`, was already present;
installing it moved `cffi` 2.0.0 -> 2.1.0 and added `pycparser==3.0`, and
`backend/requirements.txt` records all three truthfully.

### Selected playback architecture

FFmpeg decodes WebM/Opus to raw PCM on stdout; `WindowsPcmSink` writes that PCM
to exactly one operator-selected device through PortAudio. **FFmpeg itself is
never pointed at an audio device**, so it cannot open an unknown default.

| Sink mode | Command tail | Playback evidence |
| --- | --- | --- |
| `null` (default) | `-progress pipe:1 -f null -` | FFmpeg `out_time_ms` advancing |
| `windows` | `-ar 48000 -f s16le pipe:1` | PCM frames the selected device accepted |

Decoded PCM is 48 kHz, mono, `int16`.

### Device selection safety

`tools/windows_audio_devices.py` enumerates playback endpoints read-only: it
opens no stream, plays no sound and never changes the Windows default.
Configuration is process-scoped via `SPEAKLINK_AUDIO_SINK_MODE`
(`null` default | `windows`) and `SPEAKLINK_AUDIO_OUTPUT_DEVICE`.

- Hardware mode **fails closed** with no selector, an unknown selector, or a
  name matching more than one device.
- Partial names and different casing are refused.
- A stable `index:N` selector is preferred over a display name.
- A configured device is deliberately **ignored** in `null` mode, so a leftover
  variable can never cause an unexpected sound.
- A Bluetooth endpoint is listed and flagged but never chosen automatically.
- A device name is never treated as a Store or Receiver identity.

This is not theoretical: on this machine `index:3` and `index:4` have the
**identical** name "LG IPS QHD-1 (NVIDIA High Definition Audio)" under
DirectSound and WASAPI, so name-only selection really is ambiguous.

### Receiver behaviour

READY now additionally requires, in hardware mode, that the selected device
actually **opened**. A device that cannot be opened yields `DEVICE_ERROR` and
READY is withheld. An output stream that fails mid-session yields
`PLAYBACK_ERROR`. STOP and disconnect both close the output stream and the
FFmpeg child. `speaker_verified` is still never sent.

An opt-in manual chime (`audio_receiver_pilot.py test-output`) prints the exact
device, requires the operator to type `yes`, plays one quiet ~1.5 s tone at
gain 0.08, and never changes system volume or the default device.

### Actual device enumeration on this machine

8 output endpoints were listed read-only, including the current default
(`index:1`, an HDMI monitor) and a Bluetooth headset (`index:5`, flagged).
Nothing was opened and nothing was changed.

### Results

- 40 new device/sink tests, **all using an injected fake backend** — no
  automated test opens a real device or plays a sound.
- Null-sink audio smoke re-ran green: `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED`,
  `sink_mode: null`, ffmpeg return code 0, 0 dropped, `speaker_verified: false`.
- Complete backend suite **556 passed, 1 skipped, 32 existing warnings, green
  across 5 consecutive runs** (516 baseline plus 40 new).
- `compileall backend tools` succeeded; `yarn build` compiled; `yarn test`
  still reports no frontend test files, which is not behavioural coverage.

**No real hardware test was performed by this task.** The chime and the browser
microphone hardware checklist are for the operator to run, and no operator
audible observation has been recorded yet.

### Protected database

**Not opened, not copied, not modified.** The accepted baseline moved to
507,904 bytes / 2026-07-26 08:43:13 with no `-wal` or `-shm`, after the
operator confirmed they had run the application; it was byte-identical before
and after all pilot work.

## One-Store Windows output hardware validation

Outcome: **`HARDWARE_PILOT_BLOCKED`**. Full evidence is in
`ONE_STORE_WINDOWS_OUTPUT_VALIDATION_RESULT.md`.

No sound was played, no audio device was ever opened, and no operator audible
observation was obtained. The operator had **no amplifier and no wired path to
one**, so the run could not satisfy `HARDWARE_PILOT_PASSED`, which requires
hearing audio through the intended amplifier/speaker path. Testing over
Bluetooth TWS earbuds was offered by the operator and recorded as an explicit
scope deviation, but was **not executed**: the task forbids it and it could not
have produced a pass.

The validation still paid for itself by finding **four real defects before any
sound was played**, all fixed test-first with 16 new tests against an injected
fake backend:

1. **Output format was hardcoded** at 48 kHz mono, ignoring the device. The
   real endpoint advertised 44.1 kHz stereo under WDM-KS, which is strict about
   formats. The sink now adopts the device's own sample rate and channel count
   (capped at 2) and FFmpeg resamples/re-channels to match.
2. **The test chime crashed** with a raw `EOFError` traceback in a
   non-interactive shell. The gate correctly blocked playback, but the failure
   is now a controlled refusal.
3. **`index:N` was documented as stable — it is not.** Connecting a Bluetooth
   earbud set renumbered every device and moved the wired endpoint from
   `index:7` to `index:18`. A **verified selector** `index:N@<exact name>` was
   added: it pins the index to the name and fails closed after a renumber,
   naming what is actually at that index now.
4. **Bluetooth detection missed A2DP endpoints.** `Headphones (Nirvana X TWS
   Stereo)` was unflagged. The heuristic now also matches tws, a2dp, airpods,
   wireless, earbud and handsfree, and prints `wireless?` so it reads as a hint,
   not a guarantee.

Protected database: **not opened, not copied, not modified** —
507,904 bytes / 2026-07-26 08:43:13, no `-wal` or `-shm`, before and after.

Tests: 65 Windows-output tests, 52 audio tests, complete backend suite
**581 passed, 1 skipped, 32 existing warnings** across 5 consecutive runs
(556 baseline plus 25 new).

`SPEAKER_VERIFIED` remains **NOT_IMPLEMENTED**, and the amplifier path is still
unproven.

## Windows Deployment Specification

Status: **not started**. `RECEIVER_HOSTING_KEY_STORAGE_ADR.md` remains
"Proposed for pilot approval" and no deployment specification, Windows service
identity, DPAPI secret container, ACL layout or TLS configuration has been
written or executed.

## Guarded live RC8 install and hashed-fleet transition

The live HQ was upgraded RC7 -> RC8 (commit `bb36964`) and its Receiver
credential migration state was moved `legacy_only -> hash_only` using the
supported `transition_receiver_migration_state` service - not a raw `UPDATE`.
Both a pre-install and a pre-transition SQLite backup were taken and verified
(`integrity_check: ok`, `foreign_key_check: 0`); the two backups are
byte-identical, proving the RC8 install itself wrote nothing to the database.

All 44 Stores, all 4 Devices, all 4 credential hashes (SHA-256 digest-compared
against the backup), and Device `3b1ff11f` (active, Store 31/Bindapur) were
preserved exactly. Exactly one `migration_state_changed` audit event was
added. The Receiver HMAC key file hash is unchanged. HQ reached RuntimeState
READY both before and after the transition, and `/api/`, `/`, `/login`, and
`/console` all answered 200.

**First real broadcast result:** a Bindapur-only broadcast reached
`PLAYBACK_CONFIRMED` with 1/1 stores online, 1/1 receiver confirmed, 0 errors -
the first real Receiver authentication and playback confirmation against the
hashed-fleet credential path. This is what surfaced the timer/mojibake/
lifecycle defects documented below; it does not by itself establish
`SPEAKER_VERIFIED` or production readiness.

## HQ frontend/session stability and UI polish

Branch: `fix/broadcast-session-stability-and-ui-polish`. Five defects found
during the first real broadcast, all fixed test-first:

1. **Broadcast timer / System Logs showed a ~05:30 (UTC+05:30) offset.** Root
   cause: SQLite drops `tzinfo` on round-trip, so `BroadcastSession.started_at`
   came back from the ORM as a naive Python `datetime`; Pydantic's default
   JSON serialization of a naive datetime omits any offset, and a browser's
   `new Date(...)` parses an offset-less string as **local time**. On an IST
   browser that is exactly UTC+05:30. Fixed by attaching an explicit UTC
   `tzinfo` at the API serialization boundary (`backend/schemas.py`'s
   `_utc_iso`, applied via Pydantic `field_serializer` on every timestamp
   field in `SessionOut`/`TargetOut`/`SystemLogOut`) and by routing every
   frontend timestamp parse through one function
   (`frontend/src/lib/time.js#parseUtcMs`) that treats an offset-less string
   as UTC rather than local, plus `elapsedSeconds()` (epoch-based, never a
   formatted-string re-parse) and `formatIst()` (explicit Asia/Kolkata display
   for this deployment). See Learning Box 20.
2. **Header mojibake, "HQ Broadcast Console Â· v1.0".** The `Â` was pasted
   directly into the JSX source as a double-decode artifact, not a build/
   charset defect. Fixed at the source in `Layout.jsx`; the "Windows 11 ·
   Local Server · SQLite" environment banner was removed entirely, leaving
   the top-right header area clean. See Learning Box 22.
3. **Favicon.** `favicon_io.zip` (not committed) was inspected and found to
   contain the standard favicon.io set; `favicon.ico`, `favicon-16x16.png`,
   `favicon-32x32.png`, `apple-touch-icon.png`, `android-chrome-192x192.png`,
   `android-chrome-512x512.png`, and `site.webmanifest` were copied into
   `frontend/public/`, and `index.html` gained the corresponding `<link>`
   tags via `%PUBLIC_URL%` (no absolute developer-machine path). Confirmed
   present in the production `yarn build` output. Browsers cache favicons
   aggressively: after deploying, a hard refresh (Ctrl+Shift+R, or clear
   site data for the HQ origin) is required to see the new icon - a normal
   refresh alone may keep showing the old/missing one.
4. **F5/Refresh during a live broadcast silently reloaded the page and
   stopped the broadcast, with no warning.** Fixed with a native
   `beforeunload` handler installed only while `current.live` is true
   (`frontend/src/lib/beforeUnloadGuard.js`), removed immediately on normal
   Stop or Emergency Stop. Browsers ignore any custom message here and show
   their own fixed confirmation text, so no specific wording is promised.
5. **Microphone level meter went to zero after navigating away from
   Broadcast Console and back, while the broadcast was still LIVE.** Root
   cause: the `HQBroadcaster` instance (owning the `MediaStream`,
   `AudioContext`, `AnalyserNode`, `MediaRecorder`, and broadcaster
   `WebSocket`) lived in `BroadcastConsole`'s own component state, which React
   discards on route unmount. The audio pipeline itself kept running - only
   the reference to it was lost. Fixed by moving ownership into a new
   `BroadcastProvider` (`frontend/src/contexts/BroadcastContext.js`) mounted
   once, above the router's `<Outlet/>` (wrapping `<Layout/>` in `App.js`),
   holding the `HQBroadcaster` in a `useRef` so it survives every route
   change; `BroadcastConsole` is now a consumer, not an owner. No second
   capture is ever created on remount. See Learning Box 21.

Two existing backend tests (`test_the_frontend_websocket_urls_carry_only_a_
ticket`, `test_the_frontend_asks_for_the_audience_it_needs`) asserted their
ticket-handshake evidence against `BroadcastConsole.jsx`; both were updated to
read `BroadcastContext.js`, where that logic now correctly lives after the
ownership move in fix 5 - the assertions themselves are unchanged.

New tests: `frontend/src/lib/time.test.js` (10, including a UTC+05:30
regression guard and a known-UTC-to-IST display case),
`frontend/src/lib/beforeUnloadGuard.test.js` (7, framework-free against a
fake event target), `frontend/src/components/Layout.header.test.js` (3,
reads the real source file), and `backend/tests/test_timestamp_serialization.
py` (4, asserting the literal API JSON string carries an explicit UTC marker
and that `datetime.fromisoformat` on the API's own `started_at` parses as
timezone-aware). Full frontend suite: 52 passed. Full backend suite: 2495
passed, 3 skipped (baseline 2493 + 2 updated - net +2 new: the 4 timestamp
tests minus the 2 pre-existing tests whose target file changed). `compileall`,
`pip check`, and `git diff --check` all clean. `yarn build` compiled
successfully with the favicon and header fix present in the output.

Verdict: **READY_FOR_RC9_PILOT_RETEST**. RC9 has not been installed on the
live HQ; the live HQ remains on RC8/`hash_only` from the guarded transition
above. `SPEAKER_VERIFIED` is still not claimed.

## Database safety

The real database is `backend/speaklink_live.db` unless `SPEAKLINK_DB_PATH` is
configured. It must not be deleted, renamed, reset, or reseeded as a testing
shortcut. Automated smoke tests use a unique pytest temporary database. Future
schema changes require migrations that preserve transactions, foreign keys,
indexes, and existing data.

## Next recommended engineering task

Implement **LinkGuard acoustic verification for one wired Store pilot**: an
independent microphone path that listens in the Store and confirms that a
broadcast was actually audible, feeding the existing trusted-verifier
`speaker_verified` path in `receiver_contract`. Only that can set
`SPEAKER_VERIFIED`. Until it exists, an operator saying "I heard it" stays
operator observation and nothing more.

Three separate still-open tasks: run the hardware checklist in
`ONE_STORE_WINDOWS_OUTPUT_TEST_RUNBOOK.md` with a real USB/3.5 mm adapter and
record the observation form; perform the browser microphone checklist in
`ONE_STORE_LIVE_AUDIO_TEST_RUNBOOK.md`; and run the reconciliation report once
against an operator-produced isolated snapshot of the real database. That means the
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


---

## One-Store Bluetooth amplifier live test (2026-07-26)

**Outcome: `BLUETOOTH_AMPLIFIER_LIVE_TEST_PASSED`.** Full record:
[`ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md).
Procedure to repeat it:
[`ONE_STORE_BLUETOOTH_AMPLIFIER_TEST_RUNBOOK.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_TEST_RUNBOOK.md).

```
OPERATOR_CHIME_OBSERVATION      = HEARD_CLEARLY
OPERATOR_LIVE_AUDIO_OBSERVATION = Haan, clear   (heard clearly)
SPEAKER_VERIFIED                = NOT_IMPLEMENTED
NOT_READY_FOR_PRODUCTION
```

A live announcement spoken into the HQ browser microphone was heard clearly on
the speakers connected to a Bluetooth amplifier, for Store `UN` (Uttam Nagar
Old). The path proven end to end:

```
HQ browser mic -> React console -> FastAPI broadcaster WebSocket
  -> bounded 24-chunk Store queue -> local Receiver -> FFmpeg WebM/Opus decode
  -> index:4@Headphones (Bluetooth Stereo)  [Windows DirectSound, 44100 Hz, 2 ch]
  -> Makook/BARROT USB adapter -> Bluetooth -> amplifier -> speakers
```

Measured: `CONNECTED 12:56:03.702 -> READY 13:34:27.464 ->
AUDIO_RECEIVING 13:34:28.226 -> PLAYBACK_CONFIRMED 13:34:28.235 ->
STOPPED 13:37:07.467`. 533 chunks, 606 393 bytes, 0 dropped, FFmpeg decoded
159.42 s with return code 0, 7 030 422 PCM frames written (which divides by
44 100 Hz to exactly the decoded duration — nothing was lost). Each state came
from an explicit Receiver acknowledgement; none was inferred from another.

`backend/speaklink_live.db` was never opened, copied or modified: 507 904 bytes,
`2026-07-26 08:43:13` UTC, WAL and SHM absent, before and after.

### Corrected earlier claim

`ONE_STORE_WINDOWS_OUTPUT_VALIDATION_RESULT.md` called
`index:18@Headphones ()` "the only candidate wired analog endpoint". That is
wrong: `Headphones ()` is a WDM-KS view of the **Bluetooth** stack and vanishes
when the adapter is unplugged. Both that document and
`ONE_STORE_WINDOWS_OUTPUT_TEST_RUNBOOK.md` now carry a correction notice.

### Defects found and fixed, each test-first

| # | Defect | Why it mattered |
| --- | --- | --- |
| 1 | Chime wrote mono PCM into a stereo stream (`c77a4fb`) | 0.75 s at 880 Hz instead of 1.5 s at 440 Hz — swallowed by Bluetooth DAC wake-up, so the operator heard nothing |
| 2 | `Start-Process -ArgumentList` did not quote paths (`bd8e419`) | The repository path contains a space, so the Receiver never launched and never reported an error either |
| 3 | Receiver sent no idle heartbeat (`3ffd638`) | Backend closed the socket with 4408 after exactly 30 s, before the operator could open the console |
| 4 | Stop script left the whole process tree alive (`4aa9b2a`) | Six processes survived every "successful" stop and port 3000 stayed bound |
| 5 | Console counted `playing`/`failed`, states the backend never writes (`f533c06`) | Both counters were permanently zero; the header read `Currently Playing 0 / 1` during confirmed playback |
| 6 | A reusable JWT travelled in the WebSocket URL and was logged (`f27bd08`) | Anyone who could read one uvicorn access-log line could replay the session |

Defect 3 also settled a disputed observation: the Receiver Status page had shown
Store UN as OFFLINE and was accused of a mismatch. **The frontend was right** —
the Receiver really had died.

### Test position

| Suite | Result |
| --- | --- |
| Complete backend suite | 666 passed, 1 skipped, 32 warnings (was 581) |
| Five consecutive runs | all identical, 22.7-23.2 s, no flakiness |
| Playwright Chromium | 32 passed (the frontend had no tests at all before) |
| Frontend production build | compiled successfully |
| Null-sink audio smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED`, 0 dropped |
| `compileall backend tools` | exit 0 |

### Synthetic multi-Store load

`tools/load_test_receivers.py` with null-sink Receivers only:

| Stores | connect p95 | READY p95 | first chunk p95 | dropped | delivered |
| ---: | ---: | ---: | ---: | ---: | :---: |
| 5 | 26.9 ms | 33.6 ms | 3.4 ms | 0 | 5/5 |
| 10 | 37.5 ms | 27.5 ms | 4.4 ms | 0 | 10/10 |
| 20 | 68.2 ms | 35.2 ms | 6.8 ms | 0 | 20/20 |
| 40 | 125.6 ms | 55.4 ms | 11.4 ms | 0 | 40/40 |

Fan-out reached 1 058 kbps at 40 Stores; backend RSS grew about 0.27 MB per
Store. Every Receiver here is synthetic — none opens an audio device or reaches
a speaker. **A one-Store hardware test does not become a 44-Store rollout
because this passed.**

### Still blocking production

1. `frontend/src/pages/Login.jsx` pre-fills a default username and password into
   the sign-in form and prints both on screen to every visitor.
2. Password hashing, rate limiting and account lockout unreviewed.
3. Device enrolment and unique per-Receiver credentials incomplete.
4. Receiver token hashing and rotation not implemented.
5. No HTTPS/WSS — everything ran on loopback HTTP.
6. CORS restricted by pilot configuration, not by policy.
7. No audit logging of broadcast actions beyond `system_logs`.
8. No restart recovery or Windows auto-start for Receivers.
9. LinkGuard pause/resume integration does not exist.
10. **Acoustic speaker verification does not exist.** No code path can produce
    `SPEAKER_VERIFIED`, and no report claims it.
11. The in-browser Receiver page targets `/ws/receiver/{token}`, a route the
    backend does not have, and would put a Store credential in a URL path.
12. Only 1 of 44 Stores has been tested on real hardware.

The test-first Windows deployment specification remains **not started**.


---

## Default login credentials removed from the HQ UI (2026-07-26)

Branch `security/remove-default-login-credentials`. Frontend only — no backend
seed behaviour, no API contract and no database was changed.

### What the login page did

`frontend/src/pages/Login.jsx` shipped with the sign-in form already filled in
(both `useState` initial values populated - [REMOVED INSECURE HISTORICAL DEFAULT]) and printed
a `Default:` line naming both [REMOVED INSECURE HISTORICAL DEFAULT] under the Sign In button. Anyone who could reach the
page could read a working credential and sign in without knowing anything.

Pre-filling is the same problem wearing a convenience costume: the operator
never has to know their own password, so it never gets changed.

### What it does now

| | Before | After |
| --- | --- | --- |
| Username field | pre-filled `admin` | empty |
| Password field | pre-filled [REMOVED INSECURE HISTORICAL DEFAULT] | empty |
| Hint under the button | a `Default:` line naming both [REMOVED INSECURE HISTORICAL DEFAULT] | "Use the HQ credentials issued to you… they are never shown on this page." |
| `autocomplete` | absent | `username` / `current-password` |
| Label association | none | `htmlFor` / `id` on both fields |

The shipped production bundle no longer contains that default password string at all.
Loading state, the honest error message and the existing login API contract are
unchanged.

Seven Playwright tests were written first and captured the failure (6 failed,
4 passed) before any edit.

### This blocker is only partly closed

The **UI** no longer reveals or pre-fills a credential. The **default itself
still exists in the backend**, and was deliberately left alone because changing
seed behaviour was out of scope for this task:

```
backend/seed.py:16   username = os.environ.get("ADMIN_USERNAME", "admin")
backend/seed.py:17   password = os.environ.get("ADMIN_PASSWORD", [REMOVED INSECURE HISTORICAL DEFAULT])
```

Worse than a one-time seed: `seed_admin` also re-aligns the stored hash on every
startup (`seed.py:24-26`). If `ADMIN_PASSWORD` is unset, an existing
administrator's password is silently reset back to the default **on every
boot**. That must be fixed before any deployment — a start-up that refuses to
run without an explicit `ADMIN_PASSWORD` is the obvious shape.

Two tracked documents still contain the default pair and should be reviewed
separately: `memory/PRD.md` and `test_reports/iteration_1.json`.

### Verification

| Check | Result |
| --- | --- |
| Focused login Playwright | 10 passed (7 new) |
| Complete Playwright suite | 39 passed |
| Frontend production build | compiled successfully |
| Focused backend auth tests | 59 passed (`test_smoke`, `test_websocket_ticket_auth`, `test_receiver_ws_auth`, `test_receiver_auth_service`) |
| Complete backend suite | 666 passed, 1 skipped, 32 warnings |
| `compileall backend tools` | exit 0 |
| historical default password in the production bundle | not present |
| Protected database | 507 904 bytes, `2026-07-26 08:43:13`, WAL and SHM absent — unchanged |

No hardware was opened and no broadcast was run. Playwright proves nothing about
amplifier sound.

`NOT_READY_FOR_PRODUCTION` still stands. Remaining blockers:

1. `backend/seed.py` defaults to a known password and re-applies it on every startup.
2. Password hashing and security review.
3. Rate limiting and account lockout.
4. Receiver enrolment and unique per-Receiver credentials.
5. Receiver token hashing and rotation.
6. HTTPS/WSS.
7. Production CORS policy.
8. Audit logging.
9. Windows auto-start and restart recovery.
10. LinkGuard pause/resume.
11. Acoustic speaker verification — `SPEAKER_VERIFIED` remains `NOT_IMPLEMENTED`.
12. Two-Store real hardware evidence (only Store UN has been tested).


---

## Administrator bootstrap is now fail-closed (2026-07-26)

Branch `security/fail-closed-admin-bootstrap`. This closes the blocker the
previous task could only document.

### What startup used to do

```python
username = os.environ.get("ADMIN_USERNAME", <a known value>)
password = os.environ.get("ADMIN_PASSWORD", <a known value>)
existing = db.query(HQUser).filter(HQUser.username == username).first()
if existing is None:
    db.add(HQUser(...))
else:
    if not verify_password(password, existing.password_hash):
        existing.password_hash = hash_password(password)   # every restart
```

Three separate faults:

1. An unconfigured machine got an administrator with a password everybody
   already knew.
2. If `ADMIN_PASSWORD` was merely **unset**, the fallback disagreed with the
   stored hash, so the administrator's password was silently reset back to that
   known value **on every restart**. A credential rotation nobody asked for,
   performed by a routine boot.
3. Because the lookup was by username, changing `ADMIN_USERNAME` added a
   *second* row. The operator then signed in as an account that had just been
   created — which is exactly what produced the confusing 401 during the
   amplifier pilot.

### What startup does now

`backend/admin_bootstrap.py`:

- reads `ADMIN_USERNAME` and `ADMIN_PASSWORD` with **no fallback of any kind**;
- refuses missing or blank values with `AdminBootstrapError`, raised **before**
  anything is read from or written to the database;
- names the variable in the message and never the value;
- creates an administrator **only when the `hq_users` table is empty**;
- when any administrator already exists, does nothing at all — no username
  change, no hash change, no second row.

`BootstrapCredentials` defines its own `__repr__`, because the default dataclass
one would print the password into any traceback that happened to include it.

Password rotation is deliberately **not** part of startup and remains a separate
explicit administrative action.

### Live verification

Run against a private temporary database:

| Scenario | Result |
| --- | --- |
| No credentials at all | exit 1, refused, **zero rows created**, no secret in the message |
| Blank password | exit 1, refused, zero rows |
| Explicit credentials, empty database | `created`, exactly one administrator, bcrypt `$2b$12$` |
| Restart with a **different** password | `already_present`, stored hash **unchanged** |
| Restart with a **different** username | still one row, no second administrator |
| Original password after those restarts | still valid |
| The different password | **not** valid — startup granted it nothing |

### Tests

31 focused tests in `backend/tests/test_startup_admin_bootstrap.py`, written
first and captured RED before implementation.

| Suite | Result |
| --- | --- |
| Focused bootstrap tests | 31 passed |
| Complete backend suite | **697 passed, 1 skipped, 32 warnings** (was 666) |
| Complete backend suite, serial `-n 0` | 697 passed, 1 skipped |
| Playwright Chromium | 39 passed |
| Frontend production build | compiled successfully |
| Null-sink one-Store smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` |
| `compileall backend tools` | exit 0 |

A latent test fragility surfaced and was handled rather than worked around:
`test_smoke.py` asserts that **it** is the module which imported `db` under an
isolated `SPEAKLINK_DB_PATH`. That assertion is a real safety guard — if another
module imports `db` first with no path configured, `db.engine` points at the
**protected** database. The new test module is therefore named to sort after
`test_smoke.py`, and additionally sets a disposable `SPEAKLINK_DB_PATH` before
importing, so no collection order can ever aim the default engine at the
protected file. The default suite runs `-n 2 --dist loadscope`, which had been
hiding this by distributing modules across workers.

A second, unrelated flake was found by the same full run and fixed rather than
re-run: two Playwright helpers in `broadcast-console.spec.js` mutated the mocked
backend state and then waited for the console's 3 s poll, which occasionally
raced the 7 s expect timeout under full-suite load. They now click the console's
own Refresh button, which is deterministic. The suite passed twice consecutively
afterwards and got faster (2.7 min → 1.6 min).

### Historical default credential removed from the repository

The security finding stays on the record; the usable pair does not. Replaced
with `[REMOVED INSECURE HISTORICAL DEFAULT]` in `memory/PRD.md`,
`test_reports/iteration_1.json`, `PROJECT_STATE.md` and `test_result.md`. The
Playwright regression guard now assembles the historical value from parts, so
the assertion still protects against its return without the repository holding
a copy-pasteable credential. `README.md` now states that `ADMIN_USERNAME` and
`ADMIN_PASSWORD` are required and what they do and do not affect.

`git grep` for the historical password across all tracked files returns nothing.

### Security review

| Check | Result |
| --- | --- |
| Password hashing | bcrypt, `auth.py` unchanged |
| Plaintext password stored | none — only `password_hash` |
| Credential fallback in backend | none |
| Automatic hash change | none |
| Credentials logged | none — no logging in the bootstrap path |
| Startup failure message | names the variable, never the value |
| Secret in a URL | none |

### Status

`NOT_READY_FOR_PRODUCTION`. Remaining blockers:

1. Rate limiting and account lockout.
2. Complete authentication and security review.
3. Receiver device enrolment.
4. Unique per-Receiver credentials.
5. Receiver token hashing and rotation.
6. Production HTTPS/WSS.
7. Restricted production CORS.
8. Audit logs.
9. Windows Receiver auto-start and recovery.
10. LinkGuard pause/resume.
11. Acoustic speaker verification — `SPEAKER_VERIFIED` remains `NOT_IMPLEMENTED`.
12. Two-Store real hardware evidence.
13. Staging deployment validation.

Noted while reading the code, not changed here: `backend/auth.py:54-57`
(`_extract_token`) still accepts a `?token=` query parameter as a fallback for
HTTP requests. The WebSocket sockets no longer use it, so it is now dead weight
that would put a reusable token in a URL — and therefore in an access log. It
belongs in the authentication review above.


---

## Login rate limiting and account lockout (2026-07-26)

Branch `security/auth-rate-limit-and-lockout`.

### The abuse it stops

The login route looked the username up, ran bcrypt, and returned 401. Nothing
counted failures, so an attacker could try passwords as fast as the network
allowed, indefinitely, and nothing anywhere would notice.

It also leaked which usernames exist — not through the message, which was
already generic, but through **time**. An unknown username skipped
`verify_password` entirely and answered in microseconds; a real one paid for a
full bcrypt comparison first. That gap is trivially measurable, so accounts
could be enumerated before a single password was guessed.

### Two defences, deliberately different

**Short-window rate limiter** (`LoginRateLimiter`, in process): a sliding window
keyed separately by client address and by normalised username, so a burst from
one client and a slow grind against one account are both throttled. Bounded by
`max_entries`, evicting the least recently active key, with an injectable clock
and a lock for concurrent access.

**Persistent account lock** (`login_security_state` table): consecutive failures
counted against a username that really exists. Survives a restart.

The split is the point, and the limitation is stated rather than hidden: a
bounded in-process limiter **can** be flushed by an attacker who floods it with
distinct keys, and that is proven by a test. Sustained guessing against a real
account is stopped by the persistent lock, which cannot be flushed and is not
lost on restart.

### Defaults

| Setting | Default | Bounds enforced at startup |
| --- | ---: | --- |
| `LOGIN_MAX_ATTEMPTS` | 10 | 1–1000 |
| `LOGIN_WINDOW_SECONDS` | 60 | 1–3600 |
| `LOGIN_MAX_FAILURES` | 5 | 1–100 |
| `LOGIN_LOCKOUT_SECONDS` | 900 | 1–86400 |
| `LOGIN_LIMITER_MAX_ENTRIES` | 4096 | 1–1000000 |
| `TRUST_PROXY_HEADERS` | off | — |

An unusable value raises at import, so the process stops instead of silently
serving an unguarded login. An unbounded lockout is refused too: a lock that
never expires turns a guessing attempt into a denial of service against the
operator.

### Responses

| Situation | Response |
| --- | --- |
| Unknown username | `401` `Invalid username or password` |
| Wrong password | `401` — byte-identical to the above |
| Throttled | `429`, `Retry-After` header |
| Account temporarily locked | `429` — identical to throttled |

Throttled and locked answer the same way on purpose: telling them apart would
say whether the account exists, which is exactly what the generic 401 refuses to
reveal. The body carries no number at all — no count, no threshold, no unlock
time. `Retry-After` is the one number an honest client needs and it lives in the
header.

The unknown-username path now performs a bcrypt comparison against a throwaway
hash, so the clock no longer answers a question the message declines to.

### Storage

A **new table**, not columns on `hq_users`. Adding columns would need an
`ALTER TABLE` against the table holding every password hash; a new table is
created by `create_all` on an existing database without touching a single
existing row, so no migration was required and no hash was ever rewritten.

A row exists only for a username that really exists. Invented usernames are
handled by the in-memory limiter and never reach the table, so they cannot
become unbounded rows — and a lock is never reported for an account that does
not exist, which would confirm that it does. Times are epoch seconds; the row
holds no credential.

### Logging

Structured events only: `login_failed`, `login_rate_limited`,
`account_temporarily_locked`, `login_succeeded`. No password, hash, token,
Authorization value or request body.

`login_failed` deliberately records **no** username: it is attacker-supplied on
that path, so logging it would let anyone write arbitrary text into
`system_logs` and would publish every account name they guessed at.
`account_temporarily_locked` does record it, because a lock exists only for a
real account and the operator needs to know which one.

### Tests

52 focused tests in `backend/tests/test_login_rate_limit_and_lockout.py`,
written first and captured RED. They drive pure functions and a temporary
database through an injected clock — no test sleeps for a real lockout, and none
binds a port.

Starlette's `TestClient` needs `httpx`, which this project does not depend on,
and a subprocess would put the limiter out of reach of a per-test reset. The
route-level tests therefore call the route directly with a small fake request
and assert on the `HTTPException` it raises — the same status, detail and
headers FastAPI would serialise.

| Suite | Result |
| --- | --- |
| Focused login guard | 52 passed |
| Complete backend | **749 passed, 1 skipped** (was 697) |
| Complete backend, serial `-n 0` | 749 passed, 1 skipped |
| Playwright Chromium | 42 passed (was 39) |
| Frontend production build | compiled |
| Null-sink one-Store smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` |
| `compileall backend tools` | exit 0 |

### Frontend

`Login.jsx` maps 429 to a fixed neutral message rather than echoing the server
detail, so no future server wording can leak a count or an unlock time into the
page. Fields stay empty, no token is stored on any refusal, and the Sign In
button returns from its loading state.

### The HTTP query-token fallback is now pinned, not just noted

`backend/auth.py` `_extract_token` still accepts `?token=` on ordinary HTTP
requests. Two tests record the current facts: the fallback exists, and no
WebSocket route depends on it any more. Removing it is a separate change with
its own blast radius — see the next branch below.

### Status

`NOT_READY_FOR_PRODUCTION`. Remaining blockers:

1. Remove the HTTP query-token fallback (`security/remove-http-token-query-fallback`).
2. Shared rate-limit storage before running more than one worker.
3. Receiver device enrolment.
4. Unique per-Receiver credentials.
5. Receiver token hashing and rotation.
6. HTTPS/WSS.
7. Production CORS policy.
8. Broader audit logging.
9. Windows Receiver auto-start and recovery.
10. LinkGuard pause/resume.
11. Acoustic speaker verification — `SPEAKER_VERIFIED` remains `NOT_IMPLEMENTED`.
12. Two-Store real hardware evidence.
13. Staging deployment validation.

### A test fragility removed along the way

`test_smoke.py` asserted that **it** was the module which imported `db` under an
isolated `SPEAKLINK_DB_PATH`. That silently required it to be collected first,
which is what two new test modules kept breaking. It now asserts the two things
that actually matter — that the environment the subprocess inherits carries the
isolated path, and that the parent's engine never points at the protected
database — and passes in any collection order.

---

## HTTP authentication is header-only (2026-07-27)

Branch `security/remove-http-token-query-fallback`.

### What was there

`backend/auth.py` `_extract_token` accepted an access token from `?token=` on
ordinary HTTP requests:

```python
auth = request.headers.get("Authorization", "")
if auth.startswith("Bearer "):
    return auth[7:]
# fallback: query param (used by WebSocket)
qtoken = request.query_params.get("token")
if qtoken:
    return qtoken
```

The comment was accurate when it was written. Once the HQ sockets moved to
single-use tickets it stopped protecting anything and became a second, worse way
into all **17** routes that depend on `get_current_user`.

Worse, because a URL is the least private part of a request. It reaches
application access logs, reverse-proxy logs, browser history, copied links,
monitoring tools, screenshots and Referer headers. A reusable JWT sitting in one
is a session anybody who can read a log line can take over — and ordinary HTTP
has no excuse, because every client here, browsers included, can set a header.

### Every authentication path, checked rather than assumed

| Path | Credential transport | Depended on the query fallback |
| --- | --- | :---: |
| Ordinary authenticated HTTP | `Authorization: Bearer` **or** `?token=` | **this is where it lived** |
| HQ dashboard WebSocket | single-use ticket (`?ticket=`) | no |
| Broadcaster WebSocket | single-use ticket (`?ticket=`) | no |
| Receiver WebSocket | `Authorization: Bearer` header | no — query tokens were already rejected |
| Receiver enrolment / runtime auth | dedicated service, header-based | no |
| Frontend HTTP calls | `Authorization: Bearer` (`api.js`) | no |
| Frontend WebSocket creation | ticket only | no |
| `tools/*` HTTP calls | `Authorization: Bearer` | no |

**No caller depended on it.** The two remaining `?token=` uses in tools and
tests are probes that assert a query token is *rejected*, not clients that rely
on one. `StoreManagement.jsx` builds a `/receiver?token=` link for the
in-browser Receiver page — a separate, already-recorded finding about that page,
not an API authentication URL.

### What it does now

```python
authorization = request.headers.get("Authorization", "")
scheme, _, credential = authorization.partition(" ")
if scheme == "Bearer" and credential.strip():
    return credential
raise HTTPException(status_code=401, detail="Not authenticated")
```

Header only, with no compatibility fallback. The existing controlled 401, the
JWT validation, the password hashing, the WebSocket ticket scheme and the
Receiver transport are all unchanged. The refused value is never echoed into the
response and never logged.

The scheme match is now exact: `bearer`, `BearerX`, `Basic`, a bare `Bearer` and
an empty credential are all refused, where the old `startswith("Bearer ")` would
have accepted `Bearer ` followed by whitespace.

### Tests

31 focused tests in `backend/tests/test_http_token_transport.py`, written first.
RED was 6 failed / 25 passed — exactly the six about the fallback.

They cover: a query token alone refused; a genuine unexpired JWT refused purely
because of its transport, with the same token accepted in a header in the same
test; a query token unable to rescue a malformed header; four other query
parameter names refused; the refusal never echoing or logging the value; no user
context created; the header path, malformed headers and expired tokens behaving
as before; and source-level guards that `auth.py` never reads the query string
again and no HTTP route declares a credential query parameter. The WebSocket and
Receiver transports are pinned so this change cannot quietly alter them, and the
frontend is checked for building any API URL with a reusable token.

| Suite | Result |
| --- | --- |
| Focused transport tests | 31 passed |
| Focused auth/login/bootstrap/ticket/receiver/smoke | 226 passed |
| Complete backend | **779 passed, 1 skipped** (was 749), twice |
| Complete backend, serial `-n 0` | 779 passed, 1 skipped |
| Playwright Chromium | 42 passed |
| Frontend production build | compiled |
| Null-sink one-Store smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` |
| `compileall backend tools` | exit 0 |

One of the new tests was flaky under `xdist` and was fixed rather than re-run:
it monkeypatched `auth.datetime` to mint an expired token, which was really
testing the clock. It now signs a token whose `exp` is simply in the past.

### Status

`NOT_READY_FOR_PRODUCTION`. Remaining blockers:

1. Receiver device enrolment.
2. Unique per-Receiver credentials.
3. Receiver token hashing and rotation.
4. Shared rate-limit storage before any multi-worker deployment.
5. HTTPS/WSS.
6. Restricted production CORS.
7. Broader audit logging.
8. Windows Receiver auto-start and recovery.
9. LinkGuard pause/resume.
10. Acoustic speaker verification — `SPEAKER_VERIFIED` remains `NOT_IMPLEMENTED`.
11. Two-Store real hardware evidence.
12. Staging deployment validation.

Still open and unchanged by this branch: the in-browser Receiver page
(`Receiver.jsx`) targets `/ws/receiver/{token}`, a backend route that does not
exist, and would place a Store credential in a URL path. It belongs with
Receiver device enrolment.


---

## Receiver device enrolment, first half (2026-07-27)

Branch `security/receiver-device-enrolment`. Full detail:
[`RECEIVER_ENROLMENT.md`](RECEIVER_ENROLMENT.md).

### What the inspection found

A Store is a broadcast target; a Receiver Device is one Windows computer. Today
they are conflated: every Receiver for a Store presents the same raw 32-hex
value from `stores.receiver_token`. Two machines in one shop are
indistinguishable, revoking one revokes both, and the credential reached a
machine by being copied out of the UI.

A great deal of the *right* design already exists and was not rebuilt:
`migrations.py` writes `receiver_devices`, `receiver_credentials`,
`receiver_credential_events` and `receiver_credential_migration_state` with keys,
CHECK constraints and indexes; `receiver_device_service.enroll_receiver_device`
issues one device credential once; `receiver_credentials.py` defines the
versioned `speaklink_rcv_v1.<uuid>.<secret>` format with HMAC-SHA256 verifiers and
a key ring; and there are backfill, transition and cutover rehearsals.

**None of it has ever run against a live database.** The pilot database holds
only `stores`, `hq_users`, `broadcast_sessions`, `broadcast_targets`,
`receiver_events` and `system_logs` — no `schema_migrations`, no
`receiver_devices`.

The missing piece was the front door: nothing let a Receiver computer *obtain* a
credential.

### Delivered: one-time enrolment codes

`backend/receiver_enrollment_codes.py` and the `receiver_enrollment_codes` table,
created by `create_all` so no migration was required and no existing row was
touched.

| Property | Behaviour |
| --- | --- |
| Material | 24 bytes from `secrets.token_urlsafe` |
| Stored | SHA-256 verifier only — never the code |
| Lifetime | 900 s |
| Uses | exactly one |
| Concurrency | conditional `UPDATE`; a threaded race has exactly one winner |
| Refusal | never echoes the supplied value; `IssuedEnrollmentCode.__repr__` hides it |
| Store state | refused for unknown, inactive, or disabled-after-issue Stores |

25 tests, written first, captured RED. One of them was wrong on the first run
and was fixed rather than loosened: it asserted `"UN"` did not appear inside the
code, but random base64url contains any two-character string often enough that
this proved nothing. It now asserts the real property — URL-safe alphabet, and a
length that does not vary with the Store or the administrator.

### Deliberately not delivered: the credential half

Redeeming a code must produce a device credential, and issuing one needs the
HMAC key ring. `RECEIVER_HOSTING_KEY_STORAGE_ADR.md` already decided how that key
must be held:

> DPAPI-protected versioned HMAC-key container outside Git and SQLite …
> Only non-secret key-version metadata lives in normal application
> configuration.

Supplying it through an environment variable would contradict that approved
decision, and the ADR still lists the DPAPI protection-scope choice as an open
prerequisite. So no enrolment endpoint was written. The code layer establishes
**who may ask**; wiring redemption to `enroll_receiver_device` is the next
branch, and it starts with key custody rather than with a route.

### Delivered: credentials removed from the browser

Store Management rendered `${origin}/receiver?token=<credential>` behind a Copy
button for every Store — a long-lived shared secret travelling through a
clipboard, and through any chat message, browser history entry or log that saw
the link. That column, the button and the helper are gone. Credential rotation
stays, because an operator must be able to revoke; what changed is that the new
value is never displayed.

`Receiver.jsx` is no longer routed or imported. It connected to
`/ws/receiver/{token}`, a backend route that does not exist — the real socket is
`/api/ws/receiver` with an `Authorization` header — so it could never have
worked. The component is kept, with a header explaining why, because a
browser-based Receiver harness is worth rebuilding on top of device enrolment.

### Verification

| Suite | Result |
| --- | --- |
| Focused enrolment code tests | 25 passed, three consecutive runs |
| Complete backend | **804 passed, 1 skipped** (was 779) |
| Complete backend, serial `-n 0` | 804 passed, 1 skipped |
| Playwright Chromium | 47 passed (was 42) |
| Frontend production build | compiled |
| Null-sink one-Store smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` |
| `compileall backend tools` | exit 0 |

### Status

`NOT_READY_FOR_PRODUCTION`.

### Delivered since this list was last written (2026-07-28)

Several entries below used to be blockers and are not any more. They are named
here rather than quietly deleted, so the history of what was blocking stays
readable:

- Enrolment HTTP endpoints, and Windows Receiver agent enrolment with the
  credential sealed by DPAPI under `%LOCALAPPDATA%\SpeakLink\receiver`.
- Receiver credential hashing and rotation, with no overlap window.
- A standalone Receiver package needing no Python and carrying its own FFmpeg,
  and a self-contained Store pilot kit that also carries the installer scripts.
- Receiver auto-start **at logon**, one instance per credential, bounded
  rotating logs, and recovery from a crash through a bounded repetition
  schedule.
- A private-LAN pilot on `192.168.4.134`, proven end to end through the
  packaged executable (43/43).
- HQ User lifecycle, password change and administrator reset, immediate session
  revocation, and RBAC enforced at every endpoint.
- A fresh install now actually has a SUPER_ADMIN. It did not: the role
  migration ran before seeding, so every `require_super_admin` endpoint
  answered 403 to everyone, silently.

### 2026-07-29 — why a Store was green and silent

The two-PC test reached PLAYBACK_CONFIRMED on the HQ dashboard and produced no
sound on a Realtek desktop whose Windows test tone was audible through the same
earphones. Three facts, all read from the source:

1. **The Receiver discards audio by default.** `resolve_sink_configuration`
   returns the *null* sink unless `SPEAKLINK_AUDIO_SINK_MODE` says otherwise
   ([audio_receiver_pilot.py:148-158](tools/audio_receiver_pilot.py#L148-L158)),
   and in null mode the decoder thread is `_read_progress`, which reads FFmpeg's
   progress output and never opens a Windows device
   ([audio_receiver_pilot.py:401](tools/audio_receiver_pilot.py#L401)).

2. **PLAYBACK_CONFIRMED never meant "a speaker played it".** It is emitted when
   `decoder.wait_for_decode` returns true
   ([audio_receiver_pilot.py:759-769](tools/audio_receiver_pilot.py#L759-L769)).
   In *windows* mode `_pump_pcm` sets that flag only after frames were written
   to the output stream
   ([audio_receiver_pilot.py:417-423](tools/audio_receiver_pilot.py#L417-L423)),
   so there it does mean "accepted by the device". In *null* mode it means
   "FFmpeg produced PCM". Both are honest; neither was visible.

3. **The escape hatch was unreachable and failed like a crash.** Windows mode
   needs an exact, unambiguous selector, and one endpoint appears under MME,
   DirectSound, WASAPI and WDM-KS — measured on the build machine, one display
   name matched three devices. The only way to discover a stable `index:N@Name`
   selector was `python tools/windows_audio_devices.py`, which does not exist on
   a Store desktop. `SinkConfigurationError` inherits `AudioReceiverError`, not
   `AgentError`, so `main()` never caught it: the operator got a traceback and
   the Store went OFFLINE with nothing explaining why. That is exactly what the
   attempted `SPEAKLINK_AUDIO_OUTPUT_DEVICE="Speakers (Realtek(R) Audio)"` did —
   the variable names were right, the value was ambiguous.

Fixed in `tools/receiver_agent.py` only; `tools/audio_receiver_pilot.py` is
untouched because its per-mode semantics were already correct and it is the
hardware-proven path. The Agent gained a `list-audio-devices` command needing no
credential or backend, `--audio-sink` / `--audio-output-device`, sink resolution
*before* the credential is unsealed, a start-up banner naming the mode, and a
clean `Refused:` for audio errors. The kit README and the logon-task installer
carry the same options.

**Still unproven:** that any sound was heard. That needs a person in the room.

### 2026-07-29 (later) — the Store background runtime

**Architecture chosen: the Receiver runs in the Store user's own interactive
session, started at logon by Task Scheduler.** A plain SYSTEM Windows service
was rejected on a measurable fact, not a preference: a service runs in session
0, which has no audio endpoint, so it would authenticate, decode and write PCM
into nothing — a more convincing silence than the one this project already
shipped once. A service-plus-agent split was also rejected: it adds a second
process, a second failure domain and a local IPC channel, and *still* cannot
play a sound before somebody logs in, because the audio endpoint does not exist
until then. It buys nothing for the requirement that was actually stated.

**Stated honestly: announcements need the Store user signed in.** A locked
screen with the user still signed in is expected to be fine; a machine sitting
at the login screen is not, and no setting here changes that.

Three changes made it workable:

1. **Two executables from one analysis** ([receiver_agent.spec](receiver_agent.spec)).
   Windows decides on a console from the PE Subsystem field, not from how the
   process was launched — Task Scheduler's "hidden" setting hides the task in
   its own UI and does nothing about a black window on the counter.
   `SpeakLinkReceiverBackground.exe` is windowed (Subsystem 2) and is what the
   task runs; `SpeakLinkReceiver.exe` stays console (Subsystem 3) for `enrol`,
   `status`, `list-audio-devices` and `diagnose`, which all print things a
   person reads. Verified by reading the PE header of both.

2. **A configuration file** at `%LOCALAPPDATA%\SpeakLink\receiver\config.json`.
   Non-secret by construction: a file containing a credential, code, password or
   token is refused rather than loaded. The task command line is now just `run`,
   so it cannot go stale against a speaker Windows renumbered.

3. **A `diagnose` command** — read-only, safe to read out over the phone, shows
   no secret.

Installer suite: `Install-` / `Test-` / `Repair-` / `Uninstall-SpeakLinkStoreReceiver.ps1`.
Uninstall keeps the Device credential and logs by default. Repair and upgrade
are the same operation with a different package.

**Not tested, and not claimed:** reboot recovery, sign-out/sign-in recovery,
lock/unlock behaviour, crash recovery timing, backend-outage reconnect timing,
and audible playback. All of those need the operator and the second desktop —
the manual checklist is in
[STORE_RECEIVER_ACCEPTANCE_CHECKLIST.md](STORE_RECEIVER_ACCEPTANCE_CHECKLIST.md).

### 2026-07-29 (later still) — OWNER, and an 8-character password minimum

**Two corrections to the request, both checked before any edit.**

There was never a 16-character password rule. `git grep` for `min_length=16`,
`minLength={16}` and `"16 characters"` across all tracked files returns nothing.
The real value was **12**, in four places: two Pydantic schemas, two React forms
and a PowerShell guard.

The OWNER role already existed, under the name **SUPER_ADMIN**, with exactly the
rules that were asked for — ADMIN cannot create, promote to or manage it, and the
last active one cannot be disabled, archived or demoted. So it was **renamed**,
not duplicated. A second top-level role beside it would have meant two
accounts-of-last-resort and two "may not be removed" rules, and eventually only
one of them being applied — which is the lockout the rule exists to prevent.

| | OWNER | ADMIN |
|---|---|---|
| Dashboard, Stores, Devices, broadcasts, emergency stop, history, logs | yes | yes |
| User management | yes | yes, for BROADCASTER and VIEWER |
| Change how authentication works (`MANAGE_SECURITY`) | yes | **no** |
| Create, promote to, disable, archive or demote an OWNER | yes | **no** |
| Be the last enabled OWNER and still be removable | **no** | n/a |

The rename is backward compatible: `rbac.LEGACY_ROLE_ALIASES` maps the old
string to `Role.OWNER`, so rows written before the change and tokens minted
minutes before an upgrade both keep working. **No session is invalidated by the
rename itself** — the role is read from the database on every request and the
stored value parses either way.

Migration: `user_lifecycle.migrate_super_admin_to_owner` — one `UPDATE` in one
transaction, forward-only, idempotent, creates nobody, touches no password hash.
Rollback is the mirror `UPDATE` documented in its docstring; no automatic
down-migration exists because the legacy string still parses.

Password minimum is now **8**, defined once in `backend/password_policy.py` and
imported by the schemas; the React forms and the pilot script match. Maximum
stays 200. Whitespace is deliberately **not** trimmed.

`tools/create_owner.py` creates the Owner account from a hidden prompt asked
twice. No `--password` option and no environment variable, and nothing creates
the account at startup or in a migration.

**Not done, and not claimed:** the live `owneradmin` account does not exist yet.
The protected database was read but never migrated or written — the interactive
command has to be run by the operator.

**Resolved 2026-07-29 — the protected database was rebaselined by operator
decision.** The schema gained four additive user-lifecycle columns
(`lifecycle_state`, `display_name`, `disabled_at`, `archived_at`) from a
migration that ran against it. An `ALTER TABLE` rewrites the file, so the hash
moved while the size did not — which is why the guard checks both and why size
alone would have said nothing.

Verified before the new baseline was accepted:

| | |
|---|---|
| `PRAGMA integrity_check` | ok |
| `hq_users` | 1 row — `admin`, role `admin`, active, password hash unchanged |
| `stores` / `broadcast_sessions` / `system_logs` | 13 / 17 / 194 |
| plaintext password column | none |
| consistent backup | `backups/speaklink_live-20260729-160359.db` (SQLite backup API) |

| | SHA-256 | size |
|---|---|---|
| previous | `8C858B13…BD2EF2AB` | 507,904 |
| **current baseline** | `EEF1EA79…9DD70D51` | 507,904 |

Both values are recorded in `test_protected_database_isolation.py`, and a second
test asserts they differ — so a future failure cannot be "fixed" by pasting the
new hash over the old one and losing the fact that it ever moved.

The transient `-wal` (0 bytes) and `-shm` (32 KB) were removed after proving
nothing owned the file: an exclusive open succeeded, and no process command line
referenced it. Removing them did not change the main file's hash.

**Still not done:** `owneradmin` does not exist. The interactive command has not
been run.

### 2026-07-29 (last) — the black window at broadcast time

A Store running the background Receiver showed a console window the moment HQ
started a broadcast. **Two independent causes, both real.**

**1. FFmpeg had no console to inherit, so Windows gave it one.**
`SpeakLinkReceiverBackground.exe` is GUI-subsystem and has no console.
`ffmpeg.exe` is a console application. A parent with no console that starts a
console child *without* `CREATE_NO_WINDOW` gets that child a brand-new console —
and a new console is a visible window. Measured with `pythonw.exe` as the
parent, which is GUI-subsystem exactly like the background Receiver:

| spawn | child console |
|---|---|
| today's flags (none) | `has_console=True`, `console_hwnd=721134` |
| `CREATE_NO_WINDOW` | `has_console=False`, `console_hwnd=0` |

It appeared at broadcast time because that is when `FfmpegDecoder.start` runs.
`hidden_child_process_options()` in
[tools/audio_receiver_pilot.py](tools/audio_receiver_pilot.py) now covers all
three FFmpeg spawn sites; it is empty off Windows, where those constants do not
exist.

**2. The old pilot task ran the console executable.**
`Install-SpeakLinkReceiverLanPilot.ps1` registers `SpeakLinkReceiver.exe`, so a
Store still carrying `SpeakLink Receiver LAN Pilot (disposable)` shows a window at
every logon regardless of the FFmpeg fix — and both tasks would compete for the
same credential. The Store installer now removes obsolete SpeakLink tasks,
stopping their console Receiver first, and refuses to remove a task whose action
is not ours. No credential and no settings are touched.

**Not proven here, and not claimed:** that a Store desktop shows no window. This
environment has no interactive desktop — window enumeration returned no visible
window for *either* variant, so it proves nothing either way. The manual Store
acceptance test is the only evidence for that claim, and it has not been run.

### 2026-07-29 (final) — persistent HQ is built, initialized and verified

The P0 is closed in code and the persistent server now exists on disk.

| | |
|---|---|
| root | `%LOCALAPPDATA%\SpeakLink\persistent-lan-server\` (fixed, no date) |
| database | `data\speaklink.db` — `admin` (ADMIN), `owneradmin` (OWNER), 13 Stores, 17 sessions, 194 logs, integrity ok |
| source | `backend\speaklink_live.db` — **byte-identical after the copy** |
| backups | `backups\source-20260729-190540.db`, `backups\speaklink_live-20260729-160359.db` |
| scripts | Initialize / Start / Stop / Test / **Repair** |
| verification | `SPEAKLINK_PERSISTENT_SERVER_VERIFIED`, 11/11 |

Protected baseline rebaselined a second time under every authorized condition:
`8C858B13… → EEF1EA79… → 8A7E3413…` (a third, `→ 9F155E1D…`, followed on
2026-07-30 for the credential-incident remediation — see the incident section
below). The whole chain is in
`BASELINE_HISTORY` with what moved it, and two tests now prevent a value being
pasted over history instead of appended to it. `admin`'s password-hash
fingerprint is unchanged, so the existing account was preserved rather than
rewritten.

**Automated gate: green.** 1687 passed, 2 skipped, 0 failed; `compileall` 0;
`pip check` clean; frontend production build succeeds; Receiver package and
Store kit verified; secret scan clean; tree clean.

**Not done, and not claimed** — see
[docs/NONSTOP_COMPLETION_QUEUE.md](docs/NONSTOP_COMPLETION_QUEUE.md): User and
Store hard-delete with dependency rules, their frontend dialogs, Receiver
onboarding UI, `SpeakLinkHQRuntime.exe`, `SpeakLinkStoreSetup.exe`, the
audio/WebSocket/queue audit, the security audit document, load tests and E2E.

**Verdict: `GREEN_FOR_MANUAL_ACCEPTANCE` (partial scope).** Not
`GREEN_FOR_CONTROLLED_TWO_STORE_PILOT`.

### Remaining blockers

1. **DPAPI key custody under the dedicated service identity.** The prerequisite
   for issuing any production device credential. Scope and path are decided;
   the account does not exist on this machine.
2. Applying `run_receiver_credential_phase_one` to a real database, with a
   verified backup and a rehearsed rollback.
3. Retiring `stores.receiver_token` once every Store has enrolled Devices.
4. **Actual second-desktop operator execution.** Everything so far is
   same-computer evidence. `PRIVATE_LAN_TWO_DESKTOP_TEST_RUNBOOK.md` is ready
   and has not been run by an operator.
5. HTTPS/WSS persistent staging. The LAN pilot sends the Device credential over
   plain HTTP and says so at every start.
6. Restricted production CORS.
7. Shared rate-limit storage before any multi-worker deployment.
8. Broader audit logging.
9. **Windows service / boot-before-logon operation.** Not a missing flag: the
   Receiver plays into a user session and session 0 has no audio device. A
   Store that must announce with nobody signed in is not covered by anything
   built so far.
10. LinkGuard pause/resume against actual LinkGuard.
11. Acoustic speaker verification — `SPEAKER_VERIFIED` remains
    `NOT_IMPLEMENTED`.
12. Physical amplifier and speaker pilot; two-Store real hardware evidence.
13. Device-credential load campaign at 5, 10, 20 and 40 Devices.
14. Staged 40-Store rollout, and staging deployment validation.

---

## 2026-07-29 — the HQ runtime became a thing that runs

Commits: `0efe7b8`, `e0cc648`, plus the documentation commit.
Branch `feature/persistent-hq-and-one-click-store-setup`. Not pushed.

### The finding that mattered most

The previous session built `SpeakLinkHQRuntime.exe`, verified its PE subsystem
directly from the file, and committed it. That verification was correct and
still holds. **The executable did nothing.** `tools/hq_runtime.py` defined a
supervisor and never called it — no `main`, no `__main__` block. Run it and it
imports, defines classes, and exits 0.

Exit 0 is what Task Scheduler records as "the task ran successfully". The end
state would have been a green task history, no window, no error and no HQ.

It is the same failure shape as a Receiver process that exists and never plays:
**the evidence looks like success because nobody asked the running thing a
question.** The lesson is not "add an entry point" — it is that a build
artifact verified only for its *shape* has not been verified for its *behaviour*,
and shape is the easier thing to check, so it is the thing that gets checked.

### What running the executable found that the unit suite could not

Three defects, all found by `SpeakLinkHQRuntime.exe --check` against the real
initialized persistent root rather than by 1864 passing tests:

1. **A refusal nobody could satisfy.** It demanded
   `keys\receiver-hmac-keys.bin` and told the operator to restore it. Nothing
   creates that file: `Initialize` makes empty folders, the *backend* mints the
   container on first start, and the existing start script mints the signing
   secret. A correctly initialized HQ could never start.
2. **`sys.executable` is the supervisor when frozen** — the backend command
   would have relaunched the supervisor with `-m uvicorn`.
3. **`Path(__file__).parents[1]` is the bundle when frozen** — it looked for the
   React build inside its own unpacked bundle.

Then the installer dry run found the *same* refusal as (1), still present in
PowerShell after being fixed in Python. One defect, two languages; fixing one
and not searching for the other is how it survived.

### The asymmetry the fix turns on

Deleting the check would have been wrong. A key container that vanishes from a
server with Stores enrolled is a real emergency — mint a new one and every
Device credential stops verifying while every Store still *looks* enrolled, and
all 44 need re-enrolling.

The two situations differ by evidence already on disk, so the runtime counts
enrolled Devices instead of guessing. And the two secrets are treated
differently on purpose:

> A new **signing secret** costs everybody one sign-in.
> A new **HMAC container** costs 44 Stores a re-enrolment.

So the signing secret is created when absent and never replaced; the key
container is never created, and a missing one refuses only when Devices exist.
An unreadable database refuses rather than reporting zero — "I could not count
them" must never quietly become "there are none".

### Delivered

- `tools/hq_runtime.py`: `main()`, `HQRuntime`, a status file a windowed process
  can be read through, `count_enrolled_devices`, frozen-aware path resolution,
  exit codes Task Scheduler can act on
- `scripts/Install-`/`Test-`/`Repair-`/`Uninstall-SpeakLinkHQAutoStart.ps1`
- `scripts/Build-`/`Test-SpeakLinkHQPackage.ps1`
- `docs/HQ_RUNTIME_DESIGN.md`, `docs/HQ_AUTO_START.md`, `docs/ROLLBACK_PLAN.md`

### Gate

```
full backend suite                1864 passed, 2 skipped, 0 FAILED  (was 1758)
compileall backend tools          exit 0
HQ runtime + entry point          54 passed
HQ auto-start                     73 passed
HQ package                        40 passed
SpeakLinkHQRuntime.exe             PE subsystem 2 (WINDOWS_GUI), read from the file
  sha256                          B8F3FA90…4A19903B0
RC package                        SpeakLinkHQ-0.1.0-rc1-e0cc648-20260729-212302
  verification                    SPEAKLINK_HQ_PACKAGE_VERIFIED, 32/32
secret scan                       clean
git diff --check / status         clean
protected database                unchanged
```

### Not claimed

The live HQ Scheduled Task was **not installed**. The live pilot was **not
stopped**. Nothing was pushed. `SpeakLinkStoreSetup.exe`, the Store
task/recovery tests, the audio/WebSocket/queue audit, the security audit
document and the load tests are `NOT STARTED` — see
[docs/NONSTOP_COMPLETION_QUEUE.md](docs/NONSTOP_COMPLETION_QUEUE.md).

---

## 2026-07-30 — SpeakLinkStoreSetup.exe: built, windowed, and started for real

Commits: `da9c0e8`, `ed4b58b`, `5717306`, `8710ef1`.
Branch `feature/persistent-hq-and-one-click-store-setup`. Not pushed.

### The gap that had to close first: CONNECTED had no local evidence

StoreSetup has to say whether a Store came online after installing the
Receiver. The Receiver already tracked CONNECTED/READY/etc in memory, but that
state dies with the process's own memory and nothing outside it could read it -
the Receiver is backgrounded on purpose. So it now writes the same fact to
`receiver-status.json`, reusing `write_status`/`read_status` verbatim by moving
them out of `hq_runtime.py` into `receiver_agent.py`, the module both now share.

Wiring this up found a real correctness bug before it shipped: my first draft
wrote a top-level `STOPPED` state whenever `report["stopped"]` was true, but
that field means one *broadcast* ended, not that the Receiver itself stopped
running. Folding it into a Receiver-level state would have been exactly the
kind of overclaim this project keeps finding and removing. Fixed to a single
`DISCONNECTED` state with the distinction only in `detail`.

### store_setup_core.py — every decision, no GUI import

Connection safety, enrolment, audio classification, Test Sound, and waiting for
CONNECTED are all plain functions, reusing `receiver_agent.enrol()`,
`windows_audio_devices.list_output_devices()`, `hidden_child_process_options()`
and `WindowsPcmSink` unchanged. 25 tests, RED first - one real design question
(should the timeout path be provably reachable, not just declared - answered
with a fake clock) and four fixture bugs worth naming rather than hiding: a
fake audio stream missing `.start()`, a `FakeCredentialProtector()` called
without the identity argument every other test in the repo already passes, and
two fake credential strings that didn't match the real credential regex.

### store_setup_gui.py — a real threading bug, caught by driving the window

The enrolment screen's background thread called `self.device_name_var.get()`
from *inside* the worker thread. Tkinter raises "main thread is not in main
loop" for that - and because the exception was silently swallowed by an empty
result list, the screen just hung on `ENROLLING...` forever with nothing to
look at. A quick manual click-through would not have caught this; it only
reproduces under the real thread-scheduling race that a headless instantiation
test, waiting on the actual background thread, produces on every run. Fixed:
every Tk variable is read on the main thread before the thread starts, and a
worker exception is now caught and re-raised on the Tk thread instead of
leaving the screen hung.

### Built and verified

```
SpeakLinkStoreSetup.exe
  PE Optional Header Subsystem = 2 (WINDOWS_GUI), read directly from the file
  sha256  112ECBBC0369585BAA3EC23BF26264F404894869E8705303828A618990ED9072
```

Started for real and confirmed a window titled "SpeakLink Store Setup" with no
console - not a PyInstaller `--windowed` claim, and not a PE header read from a
file that was never run.

### Gate

```
full backend suite      1967 passed, 2 skipped, 0 FAILED   (was 1919)
compileall               exit 0
receiver status file     5 passed
store_setup_core         25 passed
store_setup_gui          13 passed
store_setup.spec         5 passed (source inspection)
```

### Not claimed

The Rerun screen's Status/Repair/Change Audio Output/Test Sound/Restart/Stop/
Diagnostics/Uninstall buttons exist and are asserted present, but call a named
placeholder rather than `store_setup_core` - only Replace Device Identity is
fully wired. There is no `artifacts/receiver-package` yet for Install to point
at, so a real end-to-end enrollment through the wizard has not been run. Store
task/recovery tests, Receiver enrolment status evidence (USED state),
audio/WebSocket/queue audit, `docs/SECURITY_AUDIT.md`, load tests and the final
Release Candidate artifacts are all `NOT STARTED` - see
[docs/NONSTOP_COMPLETION_QUEUE.md](docs/NONSTOP_COMPLETION_QUEUE.md).

---

## 2026-07-30 (2) — StoreSetup points at a real, verified Receiver package

Commits: `7e6d704`, `7f5cc76`.

### The gap: a placeholder path that read correctly and pointed at nothing

`InstallScreen` called the Receiver installer with
`-PackagePath artifacts\receiver-package` - a path that has never existed on
this branch. It would have failed the first time anyone actually clicked
Install, which is the exact "looks finished" pattern this project keeps
finding: code that reads correctly and was never run.

`locate_verified_receiver_package()` finds the newest `SpeakLinkReceiver-*`
package `Build-SpeakLinkReceiver.ps1` already produces under `artifacts/`, and
does not trust it by name or recency - each candidate is independently
re-verified: both PE subsystems read from the file (`WINDOWS_GUI` for the
background executable, `WINDOWS_CUI` for the console one), every file against
`SHA256SUMS.txt`, and a scan for anything that must never ship (`.env`, a
database, `server.py`, a key). A newer package that fails any check is skipped
in favour of an older one that passes - never installed because it sorted
first. This is a second, independent check inside the process about to
install from the package, not a replacement for
`Test-SpeakLinkReceiverPackage.ps1`, which stays the authoritative build-time
gate.

### A second defect, found only by actually building the package

`Build-SpeakLinkReceiver.ps1` had apparently never been run end to end on this
branch. PyInstaller writes its own progress to stderr, and under
`$ErrorActionPreference = 'Stop'` - set for this whole script, like every
script in this repository - Windows PowerShell 5.1 treats any stderr text from
a native command as a terminating error, even when the process later exits 0.
The build aborted on PyInstaller's first INFO line. Fixed the same way every
other chatty native-command call in this repository already handles it:
`ErrorActionPreference` flipped to `Continue` for exactly that call,
`$LASTEXITCODE` checked by hand afterward.

### Built and verified, two independent ways

```
SpeakLinkReceiver-1.0.0-7e6d704-20260730-124134
  Test-SpeakLinkReceiverPackage.ps1   SPEAKLINK_RECEIVER_PACKAGE_VERIFIED (20+ checks)
  locate_verified_receiver_package() chose it without special-casing
```

### Gate

```
full backend suite      1986 passed, 2 skipped, 0 FAILED   (was 1967)
compileall               exit 0
receiver package tests   18 passed (backend/tests/test_store_setup_receiver_package.py)
```

### Not claimed

A real end-to-end enrollment through the wizard against a running HQ instance
has not been run - that needs a live HQ to enrol against. The Rerun screen's
placeholder buttons, Store task/recovery tests, enrolment USED-state evidence,
the audio/WebSocket/queue audit, the security audit document, load tests and
the final Release Candidate artifacts remain `NOT STARTED` - see
[docs/NONSTOP_COMPLETION_QUEUE.md](docs/NONSTOP_COMPLETION_QUEUE.md).

---

## 2026-07-30 (3) — StoreSetup has no placeholders left, and is proven end to end

Commits: `9657558`, `11a95c6`, `bbab632`, `34f6a6e`.
Branch `feature/persistent-hq-and-one-click-store-setup`. Not pushed.

### The two defects that mattered most

Both were in my own first draft of the Rerun screen, and both were found by
**driving the window rather than reading it**.

`_replace_identity` called `core.replace_device_identity(...,
confirmation_word=core.CONFIRMATION_WORD)` — passing the *expected* word in as
the answer. The core function compares it properly, and that comparison could
never fail, because the caller supplied the correct value regardless of what the
operator typed.

The modal that was supposed to gate it was not a gate either. In an automated or
headless session the dialog's default button fires on its own: `_confirm_dialog`
returned `True` with **nothing typed**. Measured directly — a script that built
the real app, called `_replace_identity()` and typed nothing printed
`credential still exists: False`. It also blocked the Tk loop for 6–36 seconds
per call, which is why the GUI suite had quietly gone from 1.2s to 37s.

Either alone would have destroyed a Store's Device identity on a stray click.
Together they made an unconfirmable destructive action look carefully guarded.

The fix removes the modal entirely. The confirmation is an inline field, the
operator's own text is handed to `store_setup_core` as data, and the GUI no
longer knows the expected word for Replace Device Identity at all — so core's
comparison is the single real gate.

### A flake that only the parallel suite could find

Every `tk.StringVar`/`BooleanVar` in the GUI was created with no master, binding
to tkinter's module-global `_default_root`. One root in production works fine;
across the repeated root creation an xdist worker does, `_default_root` can
point at a destroyed interpreter and the *next* `StoreSetupApp()` fails inside
`tk.Tk()`. It surfaced as an intermittent setup error on an unrelated test, in
the full suite only, never in that file alone. Every variable is owned by its
widget now, and a test greps for the pattern.

### Proven, not asserted

The end-to-end test drives Test Connection → redeem a real code → seal a real
credential → select a verified package → write config → invoke the installer →
wait for CONNECTED against the **actual FastAPI routes, enrolment service and
credential store**. Only the PowerShell installer and the Receiver process are
stood in for, and the Receiver's stand-in writes the same status file a real one
writes through.

**All 12 passed first run, so I mutated the code to prove they can fail:**
loosening `wait_for_connected`'s CONNECTED check broke both timeout tests;
removing `enrol()`'s already-enrolled guard broke the code-not-spent test;
replacing the generic refusal with a real reason broke all three
generic-failure tests. Sources restored, `git diff` clean.

### The asymmetry that was closed

The Store Scheduled Task's requirements were verified only by a PowerShell
script reading an **already-installed** task — so none of them were checked by
any automated run. The HQ side had 82 such tests; the Store side, which is what
ships to 44 tills, had none. Now 49.

### Same rule, two languages, one incomplete — for the third time

`receiver_agent.remove_local_credential()` always warned that removing a
credential does **not** revoke the Device at HQ.
`Uninstall-SpeakLinkStoreReceiver.ps1 -RemoveCredential` never mentioned it,
leaving a Device HQ still lists as enrolled that will never connect — a Store
that looks fine on the dashboard and is silent. Both paths carry the sentence
now, and a test holds them together.

### Artifacts

```
SpeakLinkStoreSetup.exe   rebuilt from current source (the previous build was stale)
  PE subsystem           2 (WINDOWS_GUI), read from the file
  sha256                 079CC0FE7004D6A75075F74D1E173155A1CDEE2B08CD108B52B9DB458A503480
  launched               real window "SpeakLink Store Setup", 0 new conhost processes
SpeakLinkReceiver package artifacts\SpeakLinkReceiver-1.0.0-7e6d704-20260730-124134
```

### Gate

```
full backend suite      2088 passed, 3 skipped, 0 FAILED, 0 errors   (was 1986)
compileall               exit 0
pip check                No broken requirements found
```

### Not claimed

Playwright and the frontend production build were **not re-run** this sprint —
no frontend code changed, and quoting an old result as if it were fresh would be
worse than saying so. Enrollment USED-state evidence, the audio/WebSocket
bounded-queue audit, `docs/SECURITY_AUDIT.md`, load tests and the final Release
Candidate artifacts are all `NOT STARTED`. A real enrollment against a running
HQ with a real code on real Store hardware remains an operator checkpoint.

---

## 2026-07-30 (4) — the enrolment page stopped guessing

Commits: `1981f78`, `8788628`, `2cac9b6`.
Branch `feature/persistent-hq-and-one-click-store-setup`. Not pushed.

### The misreport this closes

`EnrolmentCodePanel` derived its state from a countdown, because nothing told it
anything else. That is honest for UNUSED and EXPIRED and wrong for the case that
matters: **a code redeemed thirty seconds into a fifteen-minute life, viewed
eleven minutes later, was labelled EXPIRED.** The Store *is* enrolled; the page
said its setup had failed.

`GET /api/stores/{id}/enrollment-codes` now supplies the evidence, and when the
server says USED, the server wins — the clock is a fallback for the window
before the first poll returns, never a second opinion that can contradict
evidence. A Playwright test drives exactly the old misreport: a USED record with
a one-second expiry, still USED after the countdown runs out.

### A column, because the alternative was inference

`receiver_enrollment_codes` recorded *that* a code was spent but nothing about
*which Device* the redemption produced. Answering that from a store_id plus a
timestamp is inference from elapsed time — precisely what this evidence model
forbids. Redemption records the Device's public id now. `NULL` means "not
recorded", never "no Device", so a code redeemed before the column existed
degrades the reported progress instead of asserting something false. Proven
against a hand-built legacy table: column added, run twice safely, existing row
preserved.

**No REVOKED state.** Nothing here can revoke a code, so the label would have
nothing behind it.

### Three real bugs in my own first draft

A **stored** fact gated behind a **live** one: `PRIMARY_ASSIGNED` was hidden
whenever the Device happened to be offline. Being primary is stored; holding a
socket is live; a Device can be primary and switched off. Hiding a promotion
that definitely happened is the same class of error as asserting one that did
not.

A case-mismatched role comparison (`"primary"` vs `DeviceRole.PRIMARY`) — the
same silently-always-false shape this repository has hit before.

And the one worth remembering: **`except Exception` swallowed a `NameError`**
from a missing `text` import, so the device-id lookup silently returned `{}` and
`DEVICE_CONNECTED` was *unreachable*. The test asserting that stage was absent
passed for entirely the wrong reason — a test that only ever checks a stage is
missing cannot tell "correctly withheld" from "impossible to produce". There is
now a test proving the stage *does* appear, and the handler is narrowed to
`SQLAlchemyError`.

### Phase 5 was mostly already built, and saying so is the point

Bounded per-Store queues, drop-oldest overflow with counters, a `broadcast()`
that never awaits a Receiver, and `_end_session` clearing the fanout *before*
clearing live state — all already real, all already covered by 29 tests. None of
it was rewritten.

What was genuinely missing was the **high-water mark**: `depth` is sampled, so a
Store that filled up and drained a moment before anybody looked reads as zero,
indistinguishable from one that never queued anything. `max_depth` identifies a
Store nearly dropping audio *before* it starts.

Two of my own new tests failed for being unfaithful: I enqueued eight chunks at
capacity two and expected only the slow Store to overflow, but `broadcast()` is
synchronous so every chunk queued before any sender ran — the healthy Store
overflowed too. Real chunks arrive ~250 ms apart; there is a yield between them
now and a note saying why.

### Gate — all run fresh this sprint

```
full backend suite      2124 passed, 3 skipped, 0 FAILED   (was 2088)
Playwright chromium      164 passed, 0 FAILED              (was 155)
frontend build           Done
compileall               exit 0
pip check                No broken requirements found
protected database       8A7E3413…B1A547CA unchanged, integrity_check ok
```

### Not claimed

`docs/SECURITY_AUDIT.md`, the synthetic Receiver load tests (2/5/10/20/40) and
the final Release Candidate artifact set are all `NOT STARTED`. A real
enrollment against a running HQ with a real code on real Store hardware remains
an operator checkpoint.


---

## 2026-07-30 (5) - security audit, load evidence, and one finding that blocks the gate

Commits: `9e84dca`, `4eb865f`, `9e6d83b`, plus the audit document.
Branch `feature/persistent-hq-and-one-click-store-setup`. Not pushed.

### The finding that matters most

**`speaklink-live.zip` in the repository root contains the current live
`JWT_SECRET`, `ADMIN_USERNAME` and `ADMIN_PASSWORD` - byte-identical**, confirmed
by SHA-256 fingerprint comparison rather than by printing anything.

The signing secret *is* the authentication system: whoever holds that file can
mint a valid token for any account, and no rate limit or lockout applies to a
token that verifies.

It was never committed - `.gitignore` matches `*.zip` and it is untracked. That is
exactly why every existing secret scan missed it: they all enumerate through
`git ls-files`, so an ignored file is invisible to all of them. **Not in git is
not the same as not in the folder somebody zips up and emails.**

I did not delete it. Removing an operator's archive is irreversible and rotating a
live credential is an operational decision - both are human checkpoints. There is
now a guard test that fails while any archive in the tree holds a `.env`, a
database or a key container, and it **stays RED until the archive is gone**.

**The security gate therefore does not pass, and must not be made to pass by
editing that test.** A genuine live-secret exposure turned into a green tick would
be worth less than no gate at all.

### Three P1s found and fixed

**The microphone uplink had no authorization at all.** `/api/ws/broadcaster`
redeemed a handshake ticket, *discarded the user id*, and accepted audio - no
permission, no role lookup. And one ticket opened both the dashboard and the
uplink. A read-only `VIEWER` could push arbitrary audio to the loudspeakers of
every targeted Store, or occupy the single slot and deny it to the operator who
was allowed to use it. Reported independently by three of the fourteen audit
areas. Tickets are audience-scoped now and the permission is checked twice - to
mint, and again at the handshake against a freshly loaded account.

**A Store could be locked out of enrolment permanently.** The per-Store cap
counted every unredeemed code with no expiry term, and nothing ever prunes an
expired code. Three abandoned codes and that Store can never enrol again - while
the refusal advised waiting for them to expire, which could never help. The
constant's own comment always said "in flight" and "live credentials"; only the
filter disagreed.

**A comment stopped the HQ server launching.** A `#` comment after a backtick
continuation merges into one logical line and swallows everything after it. The
parser confirms: zero errors, `Start-Process` with three elements, and a separate
command literally named `-ArgumentList`. At runtime that launches a bare Python
REPL in a visible window instead of uvicorn - on the documented operator path.
The comment that broke it was explaining a *previous* launcher bug.

### One P1 deliberately deferred

Standby Device acknowledgements are applied to the primary's Store snapshot, so
two Devices in one Store reject each other's messages - including the primary's
`playback_confirmed`. It only manifests with a primary *and* a standby connected
at once, which no Store does today, and the correct fix changes the live status
model. Recorded in the completion queue as **must be fixed before any Store runs
two Devices**, rather than buried in a commit message.

### A metric nobody could read

`WSManager.audio_metrics()` had existed since the bounded queues were built and
was reachable from nowhere. `GET /api/broadcast/audio-metrics` exposes it under
`VIEW_STATUS`, integers only. The load tool now reads the server's own counters
mid-broadcast instead of inferring drops from what synthetic Receivers happened to
receive - two different questions.

### My own verification step tripped a guard

Last session's `PRAGMA integrity_check` used `mode=ro`, which still creates
`-shm`/`-wal` for a WAL database, and 13 tests failed on the next run. The main
file was byte-identical and the WAL was empty, so nothing was lost - but the guard
fired correctly. `immutable=1` is the rule, and it was already the rule the
repository used in two other places. There is a test for it now.

### Load evidence

Five levels, 2 to 40 synthetic Receivers, 40 run only after 20 was clean. Every
level: every Receiver READY, full delivery, zero dropped by the server's own
counter, no queue over capacity, no queue surviving the stop. At 40: 0.27 s CPU
across a 6.9 s broadcast, 86.1 MB RSS, 1072 kbps.

`max_depth` was 1 of 24 at every level - which means there was no pressure, not
that overflow works. [docs/LOAD_TEST_REPORT.md](docs/LOAD_TEST_REPORT.md) says so
and points at the unit tests that force a stall instead.

### Gate - everything run fresh

```
full backend suite      2153 passed, 3 skipped, 0 failed
  + 1 RED by design      test_no_secret_archives_in_tree (the P0 above)
Playwright chromium      164 passed, 0 failed
frontend build           Done
compileall / pip check   exit 0 / No broken requirements
protected database       8A7E3413...B1A547CA unchanged, integrity ok, no sidecars
```

### Verdict

**NOT `GREEN_FOR_MANUAL_TWO_STORE_ACCEPTANCE`.** Every automated gate is green
except one, and that one is telling the truth about a live credential in the
working tree. Rotate the secret, remove the archive, and the verdict follows.

---

## 2026-07-30 — Credential incident: remediation verified, protected baseline moved

The archive found by the security audit held a live `JWT_SECRET`, `ADMIN_USERNAME`
and `ADMIN_PASSWORD`. Remediation was carried out by the operator, one action at a
time, with every step verified afterwards from the files rather than from the
report.

### What was actually exposed, and what was not

| Credential | Exposed | Action |
| --- | --- | --- |
| `backend/.env` `JWT_SECRET` | yes | rotated — fingerprint `05902bbbbf87` → `275f08985899` |
| `ADMIN` password | yes, verified against **both** databases | changed in both, offline |
| Persistent HQ `jwt-secret.txt` | **no** — `01fe26c76c5a`, minted later by `hq_runtime` | not rotated |
| `OWNER` password | **no** — archived username fingerprint `3d9a13ea8e39` ≠ live `5e1bb91bcea8` | not changed |
| Receiver Device credentials | no | not rotated |

Two of those five rows correct an earlier version of this report. Both were wrong
in the same direction: they assumed a secret that *appeared* in the archive was
the one in use. Fingerprint comparison, not inspection, settled each one.

### Both password changes, proven from the backups

`tools/change_hq_user_password.py` takes a SQLite-backup-API copy before it
writes. Both copies were compared against the live files afterwards, every open
through `mode=ro&immutable=1`:

```
                          protected                persistent
integrity before/after    ok / ok                  ok / ok
schema, table list        identical                identical
changed rows              hq_users id 1 only       hq_users id 1 only
changed columns           password_hash,           password_hash,
                          session_version          session_version
admin session_version     1 -> 2                   1 -> 2
admin id/username/role    1 / admin / ADMIN, unchanged (both)
admin lifecycle/active    active / 1, unchanged (both)
exposed password verifies  no                      no
owneradmin (OWNER)        every column unchanged, session_version still 1 (both)
stores                    13, rows byte-identical (both)
sessions / targets        17 / 175 unchanged (both)
receiver_events / logs    3014 / 194 unchanged (both)
sidecars                  none on any of the four files
SHA-256 after             9F155E1D...D993AE523     E4808707...A00F641EF
```

The exposed password was read out of the archive in memory for that check, so
"no longer verifies" is a real bcrypt comparison rather than an assumption. It
was never printed and never asked for.

### Two things the pass list did not actually prove

**`receiver_devices` and `receiver_enrollment_codes` do not exist in either
database.** Both are created by `backend/migrations.py`, which has never run
against either file — `receiver_enrollment_codes` and `login_security_state` are
in `models.py` but absent here too. So "Device rows unchanged" and "enrollment
records unchanged" hold because there is nothing to change: neither database holds
a single Device row or Receiver credential. No re-enrollment is required, for that
reason and not because rows were compared. The first version of the comparison
script skipped those tables silently and printed nothing for them, which reads as
coverage. That is the failure mode this project keeps finding.

**The pre-change backups are not byte-identical to the recorded baseline** —
`63BEC580…` against `8A7E3413…`. That is correct and expected: `source.backup()`
writes a fresh database with its own page layout, so a backup can prove the
*logical* pre-change state and never the byte state. The byte baseline is carried
by the chain in `BASELINE_HISTORY`, confirmed unchanged at `8A7E3413…` by the gate
run at `a6b69b6`, immediately before the operator's change. Both backups sharing
one hash is the same effect: two logically identical databases, each rewritten by
the backup API.

### Baseline moved: `8A7E3413…` → `9F155E1D…`

The size stayed at **507904 bytes**. On the failing run the size assertion passed
and only the hash caught it — a password change rewrites one row in place. Hashing
as well as sizing is the only reason this was visible at all.

`test_the_remediated_admin_session_version_did_not_go_backwards` was added
alongside. The hash guard would notice a restore from a pre-remediation backup,
but only as "the hash moved", which reads like any other schema change and invites
another rebaseline. The new test says the specific thing: `session_version` only
counts up, it reached 2 here, and below 2 means the exposed password works again.

### Still open

`speaklink-live.zip` is still in the working tree, so
`test_no_secret_archives_in_tree` is still correctly RED. Removal needs explicit
operator approval and has not been requested yet.

---

## 2026-07-30 — Release candidate, rebuilt after remediation

### The gate, everything run fresh after the archive was removed

```
compileall backend tools          exit 0
pip check                         No broken requirements found
test_no_secret_archives_in_tree   2 passed        <- the P0 guard, green at last
test_protected_database_isolation 17 passed       <- baseline 9F155E1D...D993AE523
full backend suite                2238 passed, 3 skipped, 0 failed
Playwright chromium               164 passed (3.8m)
frontend production build         Done, main bundle 121.37 kB
secret scan                       clean - 11 hits, all triaged
archive scan                      62 archives, none carries a secret or database
protected database                9F155E1D...D993AE523, integrity ok, no sidecars
git diff --check / git status      clean
```

The 11 secret-scan hits were each checked rather than assumed: three runbooks
saying `'choose-a-temporary-pilot-only-value'`, a load-test doc saying
`"<a fresh throwaway value>"`, rotator fixtures, and a bcrypt string commented
*"a real-shaped bcrypt hash, of nothing in particular"*. The pattern is
deliberately broad, so placeholders are the price of catching real ones.

### Packages, all four rebuilt from current source

| Package | Commit | Evidence |
| --- | --- | --- |
| HQ | `3c3d945` | 31 checks passed, 87 files hashed, runtime WINDOWS_GUI |
| Receiver | `3c3d945` | verified; background EXE GUI, operator EXE CUI, packaged FFmpeg runs |
| Store kit | `3c3d945` | 43 checks passed, 50 files |
| StoreSetup | `013fb5e` | **new package** — 968 files hashed, wizard WINDOWS_GUI |

**A stale package nearly shipped.** The first Store kit build was made from
`SpeakLinkReceiver-1.0.0-ff04aea-20260727-211145` — a package from three days
earlier — because I selected the newest by sorting on **name**, and `ff04aea`
sorts above `3c3d945`. The kit printed "package commit ff04aea" and
"kit commit 3c3d945" on adjacent lines and still declared
`SPEAKLINK_STORE_PILOT_KIT_BUILT`. Rebuilt by creation time: 50 files against the
stale build's 49, so it was a real content difference and not a cosmetic one.
Sort artifacts by time, never by a name containing a commit hash.

**`SpeakLinkStoreSetup.exe` was shipping in nothing.** Built into `dist/` with no
version, no recorded commit and no `SHA256SUMS`, and absent from the Store kit —
so a Store could not check that the wizard it received was the one that was built.
`scripts/Build-SpeakLinkStoreSetupPackage.ps1` now packages it with the same
guarantees as HQ, plus one the HQ script lacks: the executable must be newer than
`store_setup_gui.py`, `store_setup_core.py` and `store_setup.spec`. Whether the
wizard should also live *inside* the Store kit is an operational decision, left to
the operator rather than changed quietly.

### Two documentation defects, found by using the documentation

**`docs/ROLLBACK_PLAN.md` documented a command that does not work.** It showed
`compare_databases.py --left <current> --right <backup>`; the tool takes
positional paths, so `--left` was read as a filename and reported
`UNREADABLE - file does not exist` while still printing a confident-looking
SUGGESTION about the two real files underneath. Found while using it during a real
incident, which is the worst possible moment to find it.

**That SUGGESTION is wrong after a security change.** It ranks by operational
history, and with equal counts it favours the older file — so it recommended
keeping the *pre-change* backup of both databases, which would have restored the
exposed ADMIN password and undone the remediation. The tool's own closing line,
*"This tool does not choose. You do."*, is the operative part, and the plan now
says so explicitly.

The tool did independently confirm the Phase 1 and Phase 3 comparisons: identical
SHA-256 values, 2 users, 13 Stores, **0 Devices**, 17 sessions, 194 logs, and
integrity ok on all four files.

### One thing I got wrong about StoreSetup, recorded rather than buried

While probing for a `--version` flag, the packaged wizard appeared to exit 0
instantly with no window — which would have been a P0 for a one-click installer.
It does not: on a normal launch the window opens, titled "SpeakLink Store Setup",
and stays open. The instant exit happens only when stdout and stderr are
redirected, which is a harness artifact. `--version` does not exist at all —
`main()` ignores `argv` — so the probe was looking for something that was never
there.

Launching the real first-run wizard as part of an automated sweep was careless. It
wrote nothing, and that was luck rather than design. Verified afterwards: no state
under `%LOCALAPPDATA%`, `%PROGRAMDATA%` or `%APPDATA%`, no scheduled task, tree
clean.

### Verdict

**`GREEN_FOR_MANUAL_TWO_STORE_ACCEPTANCE`.**

Every automated gate is green, with no test weakened and no failure reclassified as
an accepted risk. The credential incident is closed and verified from the files
rather than from the report.

**NOT `GREEN_FOR_CONTROLLED_TWO_STORE_PILOT`.** That needs physical two-Store
sound, reboot, recovery and network-isolation results — and it now also needs
**Part E2** of the two-desktop runbook: primary + standby on two real machines,
including the primary switched off at the wall while the standby keeps
heartbeating. The automated proof for that is unit-level on a fake socket.

**NOT `PRODUCTION_READY`.** Unchanged: HTTPS/WSS, firewall, monitoring, a proven
backup restore, amplifier and speaker evidence, an LinkGuard decision, and a staged
rollout.

### Still open

* **DPAPI under `SpeakLinkService`** — the account does not exist on this machine.
  Operator gate, unchanged.
* **Primary + standby on real hardware** — Part E2 above. Until it is signed off, a
  Store runs **one** Receiver.
* `backend/.env` has one LF line among five CRLF ones and lost its quoting, from the
  rotator bug fixed in `a6b69b6`. The value is correct and working; repairing the
  formatting would mint a third secret and void the fingerprint the operator
  recorded. Optional, and only worth doing alongside a deliberate rotation.

---

## 2026-07-30 — First installed HQ start: nobody minted the key container

The first real installed HQ acceptance run started the server successfully and
failed `Test-SpeakLinkHQAutoStart.ps1`:

```
the Receiver key container is present    FAIL
```

### Root cause: three documented owners, no implementation

Three places stated that the backend creates the Receiver HMAC container on first
start:

| Place | What it said |
| --- | --- |
| `tools/hq_runtime.py` | *"the backend mints the container itself"* — the reason it stopped refusing on a zero-Device profile |
| `scripts/Test-SpeakLinkHQAutoStart.ps1` | *"the backend mints the HMAC container"* — the reason it treats an absent container as normal before the first start |
| the tests | written to match both |

`backend/server.py` did not. `receiver_key_ring()` calls `load_key_ring` and its
own docstring says *"The container is never created here"* — correct in itself,
and correct about not minting a key as a side effect of a request. Nobody minted
it anywhere else.

**This is the inverse of the duplicated-policy defect this project already
learned from.** That one had a rule written twice and fixed in one copy. This one
had a rule written three times and implemented in none. Each statement read like
the authority, and each was quoting the others.

The removal of the unsatisfiable zero-Device refusal was right — it was a refusal
no procedure in this repository could satisfy. It was made on the strength of a
creator that did not exist.

### The failure was quieter than that check makes it look

`build_receiver_runtime_authenticator()` runs **at import**, and with no key ring
it returns the legacy Store-token authenticator **alone, for the life of the
process**. So an HQ with enrolled Devices would come up looking healthy while
every Device credential was unusable, and a container appearing later would change
nothing until a restart. The auto-start check caught the visible half.

### Where the bootstrap went, and why

`backend/receiver_key_bootstrap.py`, called from `backend/server.py` **before**
`configure_receiver_runtime`.

* **DPAPI `CURRENT_USER` binds the sealed blob to the identity that sealed it.**
  The backend must *open* it, so the backend is the only process that can
  guarantee it will be openable. A supervisor that sealed it would work today —
  the child runs as the same user — and would fail the day HQ moves to a service
  account, as `KeyCustodyUnavailable` → `None` → legacy authenticator → a server
  that looks fine.
* **One resolver.** `receiver_key_container_path()` and
  `receiver_key_protector()` are now the single source for both the reader and
  the bootstrap. Two resolvers drift, which is the defect above in another form.
* **Ordering.** Anything minted after that import line is not used until restart.
* **`hq_runtime.spec` excludes SQLAlchemy** and starts the backend as a child
  under the machine's own Python. Creation there would need a second Device count
  and a second path resolution.

The split: **the supervisor refuses early** (missing container + Devices enrolled
→ no child is started); **the backend creates** only when creating harms nobody.
Neither creates what the other should.

### Two things I got wrong while writing it

**The gate would have had the test suite mint a live key.** The first version
gated on `SPEAKLINK_DB_PATH`, which conftest always sets. The container path falls
back to `SERVICE_CONTAINER_PATH` — `C:\ProgramData\SpeakLink\keys\` — and the
temporary test database has zero Devices, so **every test run would have created a
real key container in the machine's service custody path**, which a later
service-account HQ would find and reuse. A key nobody decided to make is exactly
what this module exists to prevent. Caught before the suite was run; the gate now
requires `SPEAKLINK_KEY_CONTAINER` *and* `SPEAKLINK_DB_PATH` to be set explicitly,
and a test asserts the service path is never the implicit target.

**"Absent" and "unreadable" are not the same claim.** I treated a missing database
file as "count could not be established" and refused. That failed 66 tests and
would have refused a first-ever start, because the backend is imported before it
creates its own schema. Nothing can be enrolled in a file that is not there, so an
absent database is **zero with certainty**. The dangerous case is a file that
*exists* and will not open — corrupt, locked, permission denied — where the
convenient answer is zero and zero mints a key over credentials still in use.
Both are now separate, named, and tested.

### Evidence

```
RED     collection ERROR - no module receiver_key_bootstrap  (28 tests uncollectable)
RED     20 passed, 1 failed - "server.py never calls the bootstrap"
GREEN   28 passed - tests/test_receiver_key_bootstrap.py
GREEN   178 passed - package, runtime, custody, protected-DB and archive tests
GREEN   2267 passed, 3 skipped, 0 failed - full backend suite
```

Four of the 28 start a **real backend process** with the environment
`child_environment()` builds, and assert the container is minted, the ring loads,
a second start changes nothing, and an enrolled-Device profile refuses with a
non-zero exit and no file created.

### Still not proven

Every bootstrap test uses `FakeProtector`. **Real DPAPI on the installed HQ, under
the account the Scheduled Task runs as, has not been exercised** — that is the
first-start retest, and it is the only thing that can prove the container is
sealed by and openable to the right identity.

---

## 2026-07-30 — Every restart demanded the credentials that created the account

With the key container minted, the installed HQ got one step further and stopped:

```
startup_event() -> seed_admin(db) -> resolve_bootstrap_credentials()
  -> AdminBootstrapError: ADMIN_USERNAME is not set, or is blank.
```

against a persistent database the verifier had already confirmed holds an enabled
administrator.

### Root cause: two lines in the wrong order

```python
def seed_admin(db):
    credentials = resolve_bootstrap_credentials()   # raises when unset
    return bootstrap_administrator(db, credentials) # would have said ALREADY_PRESENT
```

`bootstrap_administrator` was **already idempotent** — `if db.query(HQUser).first()
is not None: return ALREADY_PRESENT` — and thoroughly tested: same password,
different password, different username, hash untouched, original password still
valid. None of that ever ran, because resolving the credentials came first and
refused. A plaintext `ADMIN_PASSWORD` was therefore a precondition of **every
boot, for ever**, long after the account it describes was created.

### Why it survived: the idempotency was tested one layer below the defect

Every "restart" test in `test_startup_admin_bootstrap.py` constructs a
`BootstrapCredentials` by hand and calls `bootstrap_administrator` directly.
**Not one of them calls `seed_admin`.** The write path had excellent coverage; the
startup path had none, and the bug lived exactly in the gap between them.

### Why nobody hit it in development

`server.py` calls `load_dotenv(Path(__file__).parent / ".env")` from a path
relative to itself. Any developer run picks up `backend/.env`, which supplies
`ADMIN_USERNAME` and `ADMIN_PASSWORD`, so the unconditional resolve always
succeeded. The installed HQ has no `backend/.env` — correctly, and by policy —
so it was the first environment in which the ordering could show at all.

This is why the regression test neutralises `dotenv` in its subprocess rather
than reading the repository's `.env`: without that it passes against the broken
code and proves nothing. The real file was not read, moved or modified.

### The fix

`seed_admin` consults the database first. Three outcomes, and only the third
reads the environment:

| State | Behaviour |
| --- | --- |
| An enabled administrator exists | nothing read, nothing written, `ALREADY_PRESENT` |
| Rows exist but none is an enabled administrator | reported loudly, nothing read, nothing written |
| The table is empty | explicit credentials, or the existing refusal |

`count_enabled_administrators` uses the **same definition an operator has already
been shown** — `Test-SpeakLinkPersistentLanServer.ps1`'s *'it holds at least one
enabled administrator'*: role in `{OWNER, ADMIN, SUPER_ADMIN, admin}` and
`is_active`. Roles are compared in Python for the reason that check documents: an
`IN (...)` list is one quoting accident away from reporting a healthy database as
having no administrator. Both spellings of the legacy role count, because a
database that has not been through `migrate_legacy_roles` still holds lowercase
`admin` and that account really can administer.

Any failure to read raises `AdminStateUnavailable`, a subclass of
`AdminBootstrapError` so an existing narrower `except` still fails closed. There is
deliberately no branch that returns 0 — the convenient answer to an unreadable
database is "no administrators", and that is the answer that creates one.

### The middle row is a policy decision, not a fallthrough

A database with rows but no enabled administrator neither creates nor refuses. It
logs and continues, because both alternatives are worse:

* **Creating one** would be startup performing an administrative act. An operator
  who deliberately disabled an account would find a restart had quietly granted a
  new one — the exact behaviour this module was written to remove.
  `bootstrap_administrator` has always gated creation on *"does any HQ user
  exist"* for a documented reason, and that gate is unchanged.
* **Refusing to start** would take Receivers off the air over an HQ sign-in
  problem. A Store plays announcements without anybody signed in to HQ.

It is also not reachable through supported operations: the lifecycle rules refuse
to disable, archive or demote the last active privileged account. It is tested
explicitly rather than left to whichever branch happened to run first.

**Known gap:** there is no offline tool to *re-enable* a disabled administrator.
`tools/change_hq_user_password.py` changes a password but not lifecycle state. If
this state is ever reached, recovery needs one.

### Two things I got wrong

**A test that passed alone and failed in the full suite.** Two tests patched
`seed.count_enabled_administrators` by name. Several fixtures in this suite pop
`seed` and `server` out of `sys.modules` and re-import them, so
`sys.modules["seed"]` was a *different* module object from the one the test file
imported `seed_admin` out of — the patch landed on an instance nobody was calling.
`__module__` did not help either: it is the string `"seed"` and resolves straight
back to the wrong instance. Fixed with `monkeypatch.setitem(seed_admin.__globals__,
…)`, the dictionary that particular function object actually reads from. Three
consecutive full runs confirm it. **This is precisely how a real assertion gets
written off as a flake.**

**My first regression test proved nothing.** It asserted `ADMIN_USERNAME` was
absent from the subprocess and failed, because dotenv had loaded it from
`backend/.env`. Had I asserted only the outcome, it would have passed against the
broken code.

### Evidence

```
RED    collection ERROR - no NO_ENABLED_ADMINISTRATOR / AdminStateUnavailable
RED    19 failed, 9 passed with only seed.py reverted to the shipped version
GREEN  28 passed  test_startup_admin_bootstrap_restart.py
GREEN  59 passed  both admin-bootstrap suites together
GREEN  208 passed persistent-server, HQ runtime, user-admin, catalog, key bootstrap
GREEN  2295 passed, 3 skipped, 0 failed - full backend suite, three times running
```

One of the 28 drives the real `startup_event()` in a separate interpreter against
a database that already holds an administrator, with no bootstrap credentials
anywhere — the path that failed on the installed machine.

### Still not proven

The installed HQ has not been restarted. This is verified against temporary
SQLite databases and a subprocess, not against the live persistent profile under
the Scheduled Task's account. That is the retest.

---

## 2026-07-31 — RC4 was healthy and nobody could sign in

Runtime READY. Backend answering on `192.168.4.134:8000`. Frontend serving on
`:3000`. The official auto-start verifier passed **34 checks**. And the login page
failed every attempt with "Login failed".

The browser console had the real story:

```
POST http://127.0.0.1:8000/api/auth/login    ERR_CONNECTION_REFUSED
```

The request never reached authentication. The message was not evidence of a wrong
password.

### Root cause: a build-time constant describing a runtime fact

```js
const BACKEND_URL = process.env.REACT_APP_BACKEND_URL;   // frontend/src/lib/api.js
REACT_APP_BACKEND_URL=http://127.0.0.1:8000              // frontend/.env
```

Create React App inlines every `REACT_APP_*` value at compile time, so the address
was frozen into the bundle on the build machine. `127.0.0.1` means *the computer
running the browser*, so every operator's dashboard called itself. The HQ machine's
address is not knowable when the bundle is compiled — it is only knowable at the
moment somebody opens the page, and at that moment the browser already knows it,
because it just used it to fetch the page.

### Why every gate passed

Nothing in this repository looked inside the built bundle. The build is generated
and gitignored, and every secret and content scan here walks **tracked files**.

That is the same blind spot as the credential archive in the 2026-07-30 incident:
*not in git* is not *not in the artifact*. Two different defects, one missing
habit — inspect what ships, not what is committed.

### The fix

`frontend/src/lib/api.js` resolves the backend from `window.location`:

* hostname from the page, so one bundle works from every machine on the LAN;
* `https:` page → `https:`/`wss:`, `http:` page → `http:`/`ws:`, because an https
  page loading an http API is blocked as mixed content and presents as a silent
  network failure;
* port from `REACT_APP_BACKEND_PORT`, defaulting to `8000`;
* IPv6 hostnames bracketed — `window.location.hostname` returns them *without*
  brackets and `http://::1:8000` is not a URL;
* `REACT_APP_BACKEND_URL` still overrides, for a deployment where the API really
  is elsewhere — **except** when it names a loopback address and the page did not
  come from one. That combination has exactly one meaning: a development value
  that escaped into a production build. Honouring it would be this same defect
  arriving through configuration instead of through code.

`REACT_APP_BACKEND_URL` is gone from `frontend/.env`, so the literal is not
compiled into anything.

### The second defect, which is what made the first expensive

`Login.jsx` did `setErr(e2?.response?.data?.detail || "Login failed")`. With no
response there is no `detail`, so a **transport failure** rendered as a
**credential rejection**. An operator reads "Login failed" as "wrong password" and
the next thing they do is reset one — which in this system is a deliberate,
audited, offline act, and would not have helped.

`frontend/src/lib/loginError.js` now separates them:

| Condition | Message |
| --- | --- |
| no response at all | names the backend host, and says **the password has not been checked** |
| 401 / 403 | incorrect username or password |
| 429 | unchanged fixed wording — no count, threshold, unlock time, or whether the account exists |
| 5xx | the backend answered with an error; the password has not been checked |

### A guard that inspects the artifact

`backend/tests/test_frontend_backend_url.py` reads the built assets: no loopback
backend in any executable asset, none in a shipped source map, **and no hard-coded
LAN address either** — because replacing the loopback constant with today's IP is
the tempting fix that fails identically the day the HQ machine gets a new address.

Proof it works, run against both artifacts:

```
RC4 (installed, broken) : main.a7a94de3.js -> 127.0.0.1:8000
new build (fixed)       : none
```

The exact loopback URL is also kept out of source comments, because the source map
ships: a package containing the string is indistinguishable, to a grep, from one
that still calls it.

### Evidence

```
RED    15 failed, 6 passed - frontend/src/lib/api.test.js against the old resolver
RED    RC4's shipped bundle flagged by the artifact guard
GREEN  32 passed - full frontend unit suite (21 resolver + 11 login-error)
GREEN  102 passed - CORS, HQ runtime, package and artifact-guard tests
GREEN  2302 passed, 3 skipped, 0 failed - full backend suite
       yarn build Done; compileall 0; pip check clean; scans clean
       protected database 9F155E1D...D993AE523 unchanged, no sidecars
```

### Playwright was NOT run, and why

The live HQ is running on this machine — `python` listening on
`192.168.4.134:3000` and `:8000`, Scheduled Task `Ready`. The Playwright config
starts its own dev server on port 3000 and would either collide with the live
frontend or reuse it. Neither is acceptable while a pilot is up, so the suite was
left alone rather than forced.

The login assertions were read instead of run: they check behaviour (an error is
visible, no token is stored, the page stays on `/login`) and the 429 wording rule
(contains "try again", no digits, no "lock", no "exist"). The new messages satisfy
all of them, and the route interceptors match on path rather than host so the
resolver change cannot affect them. **That is a reading, not a run** — the suite
needs re-running once HQ is stopped.

### Still not proven

No browser has loaded the fixed bundle from the HQ LAN address. That is the retest.

---

## 2026-07-31 — Clean catalog, and a StoreSetup package that contains a Receiver

### One sentence that explains the second-PC confusion

**"Store 1" meant three different shops in three databases.**

| Database | Store id 1 was |
| --- | --- |
| LAN pilot `20260729-115328` | `LAN-1` "LAN pilot Store" — what the second PC holds |
| local-pilot | `UN` "Uttam Nagar Old" — what the operator remembered |
| Live HQ (before purge) | `MUM-001` "Mumbai Andheri Flagship", archived |

Device `1f5a6c77-…` was created `2026-07-29T06:48:06` in that LAN pilot by user
`lan-pilot-ufkzyp`. **It is not in the current HQ**, so there is nothing to
retire server-side — the fix is entirely local to that PC.

The remembered LAN-pilot user existed only inside an isolated pilot database.
Not renamed, not deleted, never migrated: each pilot run minted a throwaway
SUPER_ADMIN. Nothing was recreated.

### The purge

Classification came back cleanly separable, which is what made deletion safe:
all 17 sessions and all 175 targets referenced only legacy Stores, all 3014
events only stores 7 and 8, zero mixed, zero unknown.

```
before  57 stores · 17 sessions · 175 targets · 3014 events · 216 logs
after   44 stores ·  0 sessions ·   0 targets ·    0 events · 216 logs
codes exactly equal store_catalog.py · zones 9 · integrity ok · fk 0
users admin + owneradmin preserved · Device ee6160cb… preserved on store 31 (BP)
second run: NO_CHANGES_REQUIRED
```

Logs kept in full: a log line's identity cannot be proved from its text, and
losing operational history to tidy a catalog is a bad trade.

### The packaging defect, measured

The shipped StoreSetup package contained **no Receiver, no FFmpeg and none of
the five scripts** — verified against the built artifact. Two halves:

* `store_setup_core` used `Path(__file__).resolve().parents[1]`, which frozen is
  `_internal`, so it looked for `_internalrtifacts` and `_internal\scripts`
  and an operator was told to hand-create them;
* the build script copied only `dist\`.

`hq_runtime` had already solved half of this with `_packaged_root()`. The rule
lived in one module and not the other. `tools/resource_paths` is that idea
generalised and tested across source, one-folder, one-file and installed.

### A defect measured while stopping HQ

`Stop-ScheduledTask` left **all six** descendants alive with both ports bound.
The listeners are *grandchildren* — the venv python re-execs the real
interpreter — so stopping direct children is not enough. Descendants are now
stopped deepest-first, resolved by parent link and never by process name; two
unrelated VS Code pythons survived the manual stop that proved it.

### Verdict

`READY_FOR_ONE_STORE_GUI_RETEST`. The wizard pages that present the new
enrolment states are not yet built, and no Store PC has run this package.

## Per-user permissions / RBAC

Branch: `feature/user-permissions-rbac`. `rbac.py` already had a coarse,
ten-permission role matrix (`MANAGE_STORES`, `MANAGE_DEVICES`,
`MANAGE_USERS`, ...) and a central `require(Permission.X)` dependency - the
right shape, the wrong grain: "view a Store" and "edit a Store" were the same
flag, so there was no way to let someone see the Store list without also
letting them archive one.

### Permission catalog

`backend/permission_catalog.py` is the single source of truth: 20 codes
across six groups (Broadcast, Stores, Receivers, History, Logs, Users),
each a `menu.*.view` right or a specific action
(`stores.create`/`update`/`archive`, `devices.enrollment.create`/
`primary.assign`/`rotate`/`disable`/`revoke`, `broadcast.start`/`stop`/
`emergency_stop`, `users.create`/`update`/`disable`/`permissions.manage`).

### Default role matrix

- **OWNER** - every code.
- **ADMIN** - every code except `users.permissions.manage`, mirroring how
  `rbac.py` already withheld `MANAGE_SECURITY` from ADMIN.
- **BROADCASTER** - `menu.broadcast.view`, `broadcast.start/stop/
  emergency_stop`, `menu.history.view`, `menu.receivers.view`. No Store
  modification, no Device security changes, no User management.
- **VIEWER** - every `menu.*.view` code except `menu.users.view`, matching the
  existing frontend nav restriction (`roles: ["OWNER", "ADMIN"]` on the Users
  link) and the fact VIEWER never held `MANAGE_USERS`.

### Effective permission algorithm

`resolve_effective_permissions(engine, user)`: an inactive account or an
unparsable role returns empty (fail closed, same as `rbac.effective_
permissions`). Otherwise: start from the role's default codes
(`role_permissions` table, reseeded from the code matrix on every boot),
then apply this user's `user_permission_overrides` rows - `ALLOW` adds a
code, `DENY` removes it. Only one override row can exist per
`(user_id, permission_code)` (unique constraint), so "DENY beats ALLOW" is
really "an override, whichever effect it carries, beats the role default."
`INHERIT` is not a stored value - it is the absence of a row, applied by
deleting it rather than storing a redundant one.

### Database migration

`ensure_permission_schema(engine)`, additive and idempotent, called from
`startup_event` alongside the other schema helpers:

- `permissions(code PK, permission_group, label)`
- `role_permissions(role, permission_code PK, FK -> permissions)`
- `user_permission_overrides(id PK, user_id FK -> hq_users, permission_code
  FK -> permissions, effect CHECK IN ('ALLOW','DENY'), created_at,
  updated_at, UNIQUE(user_id, permission_code))`
- `permission_audit_events(id PK, actor_user_id FK, target_user_id FK,
  permission_code, old_value, new_value, created_at)`

`permissions` and `role_permissions` are DERIVED configuration - the code is
the source of truth - so they are safely deleted and reseeded on every boot.
`user_permission_overrides` and `permission_audit_events` hold operator
decisions and history and are never touched by seeding.

### Backend endpoints protected

Every one of the ~29 existing `require(Permission.MANAGE_STORES/
MANAGE_DEVICES/MANAGE_USERS)` call sites was converted to its specific
fine-grained code (e.g. `create_store` -> `require("stores.create")`,
`update_store` -> `require("stores.update")`, `rotate_receiver_device` ->
`require("devices.rotate")`). The `require()` factory itself now accepts
either a legacy `rbac.Permission` (mapped through `_COARSE_TO_FINE` onto its
matching fine-grained code - broadcast start/stop/emergency-stop and the two
remaining view-only dashboards) or a fine-grained string directly, and every
route's actual decision still runs through exactly one function,
`permission_catalog.resolve_effective_permissions`. No route compares a role
string. `tests/test_rbac_endpoint_matrix.py` pins the guard on every route by
reading the running app's own routing table.

New: `GET /auth/permissions` (any signed-in account - its own effective
role + codes), `GET/PUT /users/{id}/permissions` (OWNER only, via the same
`require_super_admin` dependency `reset-hq-user-password` already used -
not `require("users.permissions.manage")`, deliberately, so an override can
never grant an ADMIN a path to grant themselves more).

### Owner lockout protections

`set_permission_overrides` refuses outright (`OwnerOverrideRefused`, HTTP
409) if the target account's role is OWNER - not just "the last one": OWNER's
rights are never narrowed by an override, full stop, because OWNER is the
account of last resort and there is no per-permission equivalent of the
existing last-OWNER lifecycle guard to fall back on. Only OWNER may reach
the endpoints that write overrides at all, enforced independently of the
override system itself.

### Frontend

`AuthContext` fetches `/auth/permissions` on login and on `/auth/me`
rehydration, exposing `can(code)`. `frontend/src/lib/menuPermissions.js` is
the one map from route path to menu permission, used by both `Layout.jsx`
(hides the sidebar link) and `ProtectedRoute.jsx` (redirects a direct URL
visit to the first route the account can actually reach) - menu hiding is a
courtesy; the backend enforces the same rule again, independently, on every
request. `UserManagement.jsx` gained a "Rights" button (OWNER only, hidden
for OWNER targets) opening a rights editor: Role / Override (Inherit/Allow/
Deny select) / Effective per permission, grouped by Broadcast/Stores/
Receivers/History/Logs/Users, with Save Changes/Cancel and explicit loading/
success/error states. Changing rights calls only `PUT /users/{id}/
permissions` - never a password or role endpoint.

### Audit

Every override write - including reverting to INHERIT - appends one
`permission_audit_events` row: actor, target, permission code, old value,
new value, UTC timestamp. No password, JWT, Receiver credential or HMAC key
ever reaches that table or the accompanying `system_logs` line (which
carries only a change count, never a per-permission diff).

### Tests

`backend/tests/test_permission_catalog.py` (20 new): default role matrices
(pure, against the in-code matrix), ALLOW-overrides-role-DENY and
DENY-overrides-role-ALLOW and INHERIT-reverts, menu-view-without-action-right
independence for Stores and Broadcast, Emergency Stop as its own permission,
Device rotate/revoke/enrollment independence, OWNER-only override management,
OWNER-cannot-be-overridden (both the signed-in OWNER and a second OWNER
account), a disabled account losing all effective permissions, the audit row
shape, and the absence of secrets in the audit table and API responses.
`frontend/src/lib/menuPermissions.test.js` (4 new): the route->permission
map, and the redirect target for VIEWER/BROADCASTER-shaped permission sets.
Two existing tests that asserted an exact coarse-permission string in a
route's source (`test_rbac_endpoint_matrix.py`,
`test_enrollment_code_status.py`) were updated to the new fine-grained codes -
the guard genuinely changed, on purpose, to something more precise.

Full backend suite: **2518 passed, 3 skipped, 0 failed**, including the 20 new
permission tests, with 2 existing route-guard tests updated in place to the
new fine-grained codes rather than added. `compileall`, `pip check` and
`git diff --check` clean. `yarn build` compiled successfully.

No full Playwright/E2E menu-visibility run was performed in this session -
the frontend unit tests above cover the routing decision itself
(`menuPermissions.js`), but a real browser walk of "VIEWER cannot see the
Users link, and cannot reach `/users` by typing the URL" was not exercised.

## Follow-up: Device lifecycle, role display, favicon, Store/City/Zone scope

Four smaller, separately requested changes on the same branch.

**Receiver Device archive/restore/permanent-delete.** Revoke already existed
(permanent by design). Added the reversible pair Store/HQUser already have:
a nullable `receiver_devices.archived_at` column (status keeps its fixed
`active`/`disabled`/`retired` CHECK; archiving does not add a fourth value),
`POST /receiver-devices/{id}/archive` and `/restore`, plus
`GET .../dependencies` and `DELETE .../permanently` following
`deletion_safety.py`'s exact Store/User pattern - refused unless
`receiver_credentials`/`receiver_credential_events` show zero rows, which is
every real enrolled Device, since enrolment is what creates one. Both new
schema helpers are self-healing: called from `_require_phase_one` itself, not
only from `startup_event`, so a caller that ran Phase One directly (a test
fixture, a maintenance script) still gets the column.

**OWNER displayed as SUPER ADMIN.** Display-only. `rbac.py`'s own history -
`LEGACY_ROLE_ALIASES`, the earlier SUPER_ADMIN -> OWNER rename - is why the
stored value, the API, and every backend comparison stay `"OWNER"`; renaming
it again would need a second migration and touch every test asserting
`role == "OWNER"` for no behavioural gain. `frontend/src/pages/
UserManagement.jsx`'s `roleLabel()` renames only what a person reads: the
role badge, the role dropdowns, the owner-protection note, and the rights
editor's Base Role line.

**Favicon, actually replaced this time.** The operator's real updated icon
set was in `~/Downloads/favicon_io.zip`, not the placeholder original still
sitting in the repository root - verified by SHA-256 that all seven files
in Downloads genuinely differed from what was committed, then replaced them
in `frontend/public/` and confirmed the new `favicon.ico` hash matches
exactly in the production `yarn build` output.

**Per-user Store/City/Zone scope.** `backend/store_scope.py`: a new
`user_store_scope` table (STORE/CITY/REGION rows, additive, an account with
zero rows is unrestricted so nothing already working changes). OWNER is
always unrestricted - the ADMIN or BROADCASTER may now be limited to a
single Store, a city, or a Zone, and `_require_store_in_scope` enforces it
independently of the permission on every Store-by-id route and every
Store-id-keyed Device route. `_resolve_targets` applies it to broadcast
target resolution: an explicit `target_mode=selected` Store outside scope is
refused (the caller asked for it by id), while `all`/`region`/`city`/
`online_only` narrow silently, because narrowing to "my Stores" is the whole
point of those modes. `GET/PUT /users/{id}/store-scope` is OWNER-only via
`require_super_admin` - the same reservation as permission overrides, so a
scope assignment can never grant an ADMIN a path to grant themselves more.
`UserManagement.jsx` gained a "Scope" action (OWNER only, ADMIN/BROADCASTER
targets only) listing current assignments with add/remove and Save Changes.

New tests: `test_receiver_device_archive_and_delete.py` (6),
`test_store_scope.py` (8) - unscoped-sees-everything, Store-scoped
list/edit, City-scoped broadcast start plus explicit-selected-outside-scope
refusal, Zone-scoped `all`-mode silent narrowing, Device-endpoint scoping,
OWNER-never-scoped, and clearing every entry returning to unrestricted.
`test_rbac_endpoint_matrix.py` gained the 6 new routes (device archive/
restore/dependencies/permanent-delete, store-scope read/write).

Full backend suite: **2538 passed, 3 skipped, 0 failed**. `compileall`,
`pip check` and `git diff --check` clean. Frontend: 56 passed, `yarn build`
compiled successfully with the new favicon and role label in the output.

Not done in this pass: a UI affordance to view a Device's `archived_at`
timestamp anywhere other than the raw API response, and no Playwright/E2E
walk of the new Scope/Rights editors in a real browser.

## Closing the authorization/lifecycle/E2E gaps before a scoped-RBAC pilot

### Real Device archive/delete permissions

`devices.disable`/`devices.revoke` were being reused for Archive/Delete -
not fine-grained enough for per-user customisation. Added
`devices.archive` and `devices.delete_permanently` to the catalog.
Defaults: OWNER both; ADMIN gets `devices.archive` but **not**
`devices.delete_permanently` (excluded the same way `users.permissions.
manage` already is); BROADCASTER/VIEWER neither. `archive_receiver_device`/
`restore_receiver_device` now require `devices.archive`;
`hard_delete_receiver_device` requires `devices.delete_permanently`.
Permanent deletion is effectively SUPER ADMIN-only by default.

### Hardened permanent delete

`delete_device_if_unused` now refuses outright, before any dependency
count, unless the Device is already ARCHIVED (`archived_at` set) or
REVOKED/RETIRED (`status='retired'`) - an ACTIVE or merely DISABLED Device
is still ordinary rotation and is never eligible. Also refuses if the
Device is still a Store's primary, and re-runs `PRAGMA foreign_key_check`
after the delete inside the same transaction as a last-resort guard.
Nothing is ever cascade-deleted to make a delete succeed. Six RED-then-
GREEN tests in `test_receiver_device_archive_and_delete.py` prove: ACTIVE
refused, DISABLED-not-archived refused, ARCHIVED-and-unused allowed,
REVOKED-and-unused allowed, still-primary refused, wrong confirmation
refused.

### Archived Device UI

`ReceiverDevices.jsx` hides archived Devices by default behind a "Show
Archived" toggle (with a count badge), shows an Archived date/time column
and an "Archived" status badge, and only offers Restore for an archived
row / Archive for a non-archived one. Delete is now never rendered for an
ACTIVE or merely DISABLED Device - only when `archived_at` is set or
`status === 'retired'` AND the account holds `devices.delete_permanently`.
`describe_store_devices` (backend) now returns `archived_at` per row so the
frontend never has to re-derive it.

### Scope audit

New `store_scope_audit_events` table: actor, target, scope type, a safe
human-readable label (a Store's name+code, or the city/Zone name - never a
bare id), action (`ADDED`/`REMOVED`), UTC timestamp. `set_user_scope` diffs
the before/after scope sets inside the same transaction that writes them
and records one row per real change - clearing every entry logs `REMOVED`
for each one that existed. No password, JWT, Receiver credential or HMAC
key ever reaches it or the accompanying `system_logs` line (proven by
`test_scope_audit_contains_no_secrets`).

### Scope enforcement, audited end to end

Store-associated endpoints now all apply `_require_store_in_scope` or an
equivalent filter: Store Management (list + every by-id action), Receiver
Devices (list/read/dependencies + every store-id-keyed route),
Broadcast Console's live status, Broadcast target creation/start
(`_resolve_targets`), and - newly this round - **Broadcast History**:
`broadcast_history` hides a session with zero in-scope targets entirely,
and `session_detail` 404s the same way for a session that never reached
this account's Stores (indistinguishable from "no such session," so a
scoped user cannot infer an out-of-scope campaign existed) - `selected_
store_count`/`online_store_count`/`offline_store_count` are recomputed to
the in-scope subset, never the real totals. **System Logs** was
deliberately left unscoped: `system_logs` has no `store_id` column, so it
is not Store-associated, and scoping unstructured free text would mean
guessing - it stays governed only by `menu.logs.view`, proven by a test
asserting a scoped ADMIN sees exactly as many log rows as OWNER.

### Action buttons converted to `can(permission)`

`StoreManagement.jsx` (Add/Edit/Regenerate-token/Disable/Enable/Archive/
Restore/Delete), `BroadcastConsole.jsx` (Start/Stop/Emergency Stop),
`ReceiverDevices.jsx` (already converted last round; extended with the two
new device codes), and `UserManagement.jsx` (New User/Edit/Disable/Enable/
Archive/Restore/Delete, layered on top of the existing role-based "which
OTHER role you may manage" check, which is a business rule about targets
and not a coarse permission substitute) all now gate on the exact effective
permission rather than role or unconditional visibility. The Rights/Scope
buttons remain OWNER-only, the one deliberately protected exception the
task itself calls for.

### Playwright / real browser E2E

The repository already had a configured Playwright suite
(`frontend/e2e`, 174 tests, backend fully mocked via `page.route`) that had
never been run against this branch's changes. Running it first surfaced a
real gap: `GET /auth/permissions` had no mock at all, so every `can()`
call would have silently returned `false` and hidden every action button in
every existing spec (see Learning Box 26). Fixed by mirroring `DEFAULT_
ROLE_PERMISSIONS` in `e2e/support/backend.js`, plus mocks for the new
Device archive/restore/dependencies/permanently and Rights/Scope editor
endpoints.

Three existing tests then failed for a legitimate reason - they encoded the
pre-this-branch design ("a route only administrators can reach in the
router is a lock on a door with no walls"), which `ProtectedRoute`'s new
menu-permission gating deliberately supersedes. Updated: one to expect
"SUPER ADMIN" instead of "OWNER", one to use an ADMIN (who the frontend
does admit) to prove the backend's independent 403, and one rewritten to
assert the new, correct behaviour - a VIEWER visiting `/users` directly is
redirected, not shown a forbidden page.

New `frontend/e2e/permissions-and-scope.spec.js` (10 tests) proves, in a
real Chromium: (A) an ADMIN with `stores.update` denied keeps the Store
list but loses Edit, and a hand-crafted `fetch` to the API is refused
anyway; (B) a BROADCASTER scoped to one Store sees only it and a
hand-crafted out-of-scope broadcast target is refused; (C) a Zone-scoped
BROADCASTER sees only that Zone's Stores; (D) VIEWER offers no
create/edit/lifecycle actions and the Users nav link/route are both gone;
(E) SUPER ADMIN sees every Store action and can open the Rights and Scope
editors; (F) an unauthenticated visit to any protected route lands on
`/login`; (G) an archived Device is hidden by default and appears with
"Show Archived", with Restore offered and Archived badge/date shown.

**Full Playwright suite: 184 passed, 0 failed** (174 existing + 10 new),
run against an isolated port (3577) specifically so as not to disturb the
real, currently-running live HQ RC9 install serving the actual port 3000 on
this machine - `playwright.config.js` was reverted to port 3000 afterwards
with no net diff.

### Migration safety

`test_rbac_migration_safety.py` (6 tests), all against a temporary clone
built by the app's own `startup_event`, never the live database: existing
users/roles/Stores survive; an existing ADMIN with zero scope rows stays
unrestricted after every new migration re-runs; `ensure_permission_schema`/
`ensure_store_scope_schema`/`ensure_device_archive_schema` are each
idempotent across three consecutive re-runs with identical row counts;
the archive column never duplicates; no migration ever shrinks or deletes
the database file; a full archive→restore→archive→permanent-delete
lifecycle on a real clone leaves `foreign_key_check` empty and
`integrity_check` at `ok`.

### Totals

Backend: **2557 passed, 3 skipped, 0 failed**. Frontend unit: **56
passed**. Playwright: **184 passed**. `compileall`, `pip check`,
`git diff --check` and a secret scan all clean. `yarn build` compiled.

### Remaining limitations

No UI surface for scope-audit or permission-audit history (the tables are
queryable, not yet displayed anywhere); System Logs remaining
permission-only (by design, documented above) means a scoped BROADCASTER
can still read log lines that mention an out-of-scope Store by name in
free text.

## RC10 live defect: Receiver Status blank page for a scoped BROADCASTER

### Root cause

Not a UNION bug. `resolve_store_scope()` already combined STORE ∪ CITY ∪
REGION correctly. The real defect: `Role.BROADCASTER`'s default
permissions never included `menu.stores.view`, which `GET /api/stores` -
the only endpoint `ReceiverStatus.jsx` calls - hard-requires. A fresh
BROADCASTER with no manually added per-user override got a 403 on every
load; `ReceiverStatus.jsx` had no try/catch around that fetch, so the
error rendered as silent blank whitespace, indistinguishable from "zero
Stores in scope."

Read-only diagnosis of the live database (`persistent-lan-server/data/
speaklink.db`, not the repo-local dev copy) at the time of inspection
showed only one scope row for the operator's `broadcaster` account
(`STORE` → Uttam Nagar ASR) and an already-present ad hoc
`menu.stores.view` ALLOW override, added by hand at some point after the
original blank-page report - consistent with the 403 having been the
actual first cause.

### Fix

- `menu.stores.view` added to `Role.BROADCASTER`'s default permission set
  (`permission_catalog.py`) - the role-default fix, not a per-user
  workaround.
- `set_user_scope()` now rejects a CITY/REGION value that matches zero
  Stores at save time (400), closing the gap noted in the prior
  entry - a typo can no longer be persisted and silently resolve to
  nothing later.
- Scope editor's City/Zone entry is now a dropdown sourced from the
  existing `/stores/meta/regions-cities` endpoint - no free text, no
  hard-coded catalog in React.
- Scope editor shows "No assignments = All Stores (unrestricted)" or
  "Restricted Scope — Effective Stores: N" plus an expandable preview,
  computed client-side from data already fetched from the backend for
  display only - the backend resolver remains the sole
  security-enforcing authority.
- `ReceiverStatus.jsx` now shows a visible error on API failure and an
  explicit "No Stores are available in this account's current Scope."
  empty-state - never silent blank whitespace either way.

### RED evidence

Five new backend tests failed before the fix: a fresh BROADCASTER could
not load `/api/stores` at all (403); a BROADCASTER with REGION+CITY+STORE
scope got a 403-shaped error object instead of the union; saving an
unknown City/Zone succeeded when it should have been rejected (x2).

### Totals (this round)

Targeted (`test_store_scope.py`): 21 passed. Full backend: 2560 passed,
3 skipped (2 environment-flaky GUI/lock-timing tests confirmed to pass
in isolation, unrelated to this change). Frontend unit: 56 passed.
Playwright: 188 passed (184 existing + a Receiver Status regression suite
and a Scope-editor dropdown suite), run on the isolated port 3577 per the
existing safety convention, config reverted to port 3000 afterward with
zero net diff. `compileall`, `pip check`, `git diff --check` and a secret
scan all clean. `yarn build` compiled.

## History-preserving permanent Store deletion

### Semantics

Two different operations now exist. ARCHIVE (existing): reversible, Store
hidden from operation, restorable. PERMANENT DELETE (new): irreversible,
Store gone from every operational surface, **not** restorable, but every
historical row that referenced it stays exactly as readable as before.

### Tombstone model (Option A - the Store row is never removed)

`stores.lifecycle_state` gains a fourth value, `'deleted'`, plus new
`deleted_at`/`deleted_by` columns (additive, backfilled by
`ensure_store_lifecycle_schema`, same pattern as the existing
`lifecycle_state` migration). `'deleted'` is deliberately absent from
every transition's `allowed_from` tuple in `store_lifecycle.py`, so
disable/enable/archive/restore already refuse a deleted Store with no
new code - the state machine itself is the guard.

### Every Store FK/history dependency inspected

`receiver_devices.store_id` (`ON DELETE RESTRICT`), `broadcast_targets.
store_id`, `receiver_events.store_id`, `receiver_enrollment_codes.
store_id` (all plain FKs, no cascade), `receiver_credential_events.
store_id` (`ON DELETE SET NULL`), `user_store_scope.store_id`. None of
these ever fire, because the Store row is never deleted - confirming the
tombstone model is the only safe option once RESTRICT/SET NULL
semantics are read directly off the schema, not assumed.

### Receiver credential / enrollment handling

Inside one transaction (`store_deletion.
permanently_delete_store_with_history`): every Device the Store owns is
retired (`status='retired'`, not deleted), its primary assignment row
removed, its active/superseded credentials revoked (`status='revoked'`,
`revoked_at` stamped - the row and its full history remain), and every
unredeemed/unexpired enrollment code has its `expires_at_epoch`
backdated to now (unusable, code_hash - which can never be reversed -
preserved as evidence). `receiver_token` is rotated to a fresh, unusable
value.

### History preservation, proven

Broadcast History, session detail, `broadcast_targets`,
`receiver_events` and Device rows all read back unchanged after a
tombstone - proven directly against the database inside the test suite,
not just through the API. `TargetOut` now carries `store_code`/
`store_name`/`store_deleted`, populated from the (never-removed) Store
row, so a historical target renders "AYUSHK (Deleted)" instead of a bare
id or a blank name.

### Permission and confirmation

New `stores.delete_permanently`, defaulting OWNER/SUPER ADMIN only (same
exclusion list ADMIN already has for `devices.delete_permanently`/
`users.permissions.manage`). Store Management's "Permanently Delete"
button is never disabled by a history count - it shows the real counts
and requires the exact Store code typed AND a separate "cannot be
restored" checkbox, both enforced again server-side.

### Totals (this round)

New (`test_store_permanent_deletion.py`): 24 passed, covering permission
matrix, disappearance from every operational surface, non-restorability,
full history preservation, credential/primary/enrollment handling,
`PRAGMA foreign_key_check`/`integrity_check`, code-reuse refusal, and
confirmation enforcement. Two pre-existing tests updated for the new
permission code (`test_admin_default_permissions_exclude_...`,
`test_no_authenticated_route_is_missing_from_this_table`). Full backend:
2588 passed, 3 skipped (flaky xdist-timing tests, different ones each
run, all confirmed to pass in isolation - a known pre-existing property
of this suite under `-n auto`, unrelated to this change). Frontend unit:
56 passed. Playwright: 192 passed (188 + 4 tombstone-UI tests), run on
the isolated port 3577, config reverted to 3000 with zero net diff.
`compileall`, `pip check`, `git diff --check` and a secret scan all
clean. `yarn build` compiled.

### Remaining limitations

No UI list of tombstoned Stores (the audit endpoint exists, `GET
/stores/{id}/deletion-events`, but nothing links to it from a normal
screen since the Store itself is gone from every list); Broadcast
Console's live target selector was not separately re-tested against a
tombstoned Store beyond the existing `is_active` filter it already
honors (a deleted Store has `is_active=False`, the same filter every
other inactive Store already goes through).

## RC12 installed on the live Windows HQ

Installed 2026-08-01 from
`artifacts/SpeakLinkHQ-0.1.0-rc12-7d1cc5b-20260801-174322`, runtime
SHA-256 `4363436F3410DD5C74AD6DAF002399612CD13F188FDCADDCAB93ECE811056D33`,
package verifier 31/31.

Pre-install backup:
`backups/speaklink-20260801-pre-rc12-install.db`, 692,224 bytes, SHA-256
`3D09ABCE8E16744EB30A430CD3DC562797FDF9D6182704230FDA9BF3FF645892`,
`integrity_check=ok`, `foreign_key_check=[]`.

Every count identical before and after: 45 Stores, 3 Users, 5 Receiver
Devices, 6 Receiver credentials, 3 primary assignments, 13 broadcast
sessions, 102 broadcast targets. Bindapur (Store 31, `BP`) and its
primary Device `3b1ff11f-0b18-4f56-b911-30f036cbddd9` unchanged. Receiver
HMAC key file SHA-256 identical before and after
(`748A99F2...DB057B`). Runtime `READY`; `/api/`, `/`, `/login`,
`/console` all 200; missing asset 404. Bindapur reconnected naturally
after the restart with no action taken on the Store PC.

**Not performed, and deliberately not claimed:** the signed-in GUI
acceptance phases (Scope dropdown check, scoped-BROADCASTER test,
disposable-Store permanent-delete pilot, short Bindapur regression
broadcast). Those require real operator credentials the assistant does
not hold. RC12's install is verified safe and data-preserving; its
*feature acceptance* is still pending those manual steps, so no
`PLAYBACK_CONFIRMED` or `SPEAKER_VERIFIED` claim is made for this round.

### A pre-existing serial-mode test-suite defect, found here

`pytest.ini` configures this suite as `-n 2 --dist loadscope` (two xdist
worker processes). Run instead as a single process (`-n 0`), four tests
in `test_receiver_enrollment_service.py` fail. Proven pre-existing and
unrelated to any RC11/RC12 change: the same four fail with every file
touched in those rounds excluded, and the file passes 32/32 in
isolation. Some module leaves process-global state behind that only
collides when every test file shares one interpreter. Not fixed in this
round - recorded here so the next person does not mistake it for a
regression.

## Dual database: SQLite development, PostgreSQL (Supabase) production

Branch `feature/supabase-postgres-production`, from RC12's `7d1cc5b`.
Only the production DATABASE moves. The Windows HQ remains the
application and WebSocket server; Receiver audio still flows HQ FastAPI
-> Windows Receiver Agent -> Store speakers and never touches Supabase.
Supabase Auth, Realtime, Storage, Edge Functions and the JS client are
not used at all - this is managed PostgreSQL only.

### The configuration rule

One authoritative function, `backend/db_config.py::load_database_config`:

* `APP_ENV` unset or `development` -> local SQLite, exactly as before. No
  `DATABASE_URL` needed, no internet connection, no configuration step.
* `APP_ENV=production` -> `DATABASE_URL` is **required** and must be a
  `postgresql://` URL. Missing, blank, or a sqlite URL is a hard startup
  refusal. Production never falls back to a local SQLite file - a file
  that would look like it works while silently diverging from the real
  production database.

The URL is normalized to force the psycopg 3 driver and to require
`sslmode=require` if the operator's string omitted it. No Supabase
project id, hostname, username or password appears anywhere in source.

### Secret storage

`keys/database-url.txt` in the persistent root - the same
outside-Git, outside-the-package shape `jwt-secret.txt` already uses.
Unlike `jwt-secret.txt` it is **never auto-created**: with
`app_env=production` set and the file missing, `tools/hq_runtime.py`
refuses to start. It is read once per HQ start and handed to the backend
child only through its environment, never a command line (a command line
is visible in the process list to every user on the machine). Never
logged. `backend/.env.example` documents variable NAMES only.

### Schema

`models.py`'s ORM tables were already dialect-portable and needed no
change beyond declaring the two tombstone columns (`deleted_at`,
`deleted_by`) that previously existed only via a raw SQLite `ALTER
TABLE` - and so would have been silently missing from PostgreSQL.
`backend/postgres_schema.py` re-declares the eleven tables that were
only ever created by raw SQLite `CREATE TABLE` strings
(`receiver_devices`, `receiver_credentials`, `receiver_credential_events`,
`receiver_store_primary_device`, `permissions`, `role_permissions`,
`user_permission_overrides`, `permission_audit_events`,
`user_store_scope`, `store_scope_audit_events`, `store_deletion_events`)
as portable SQLAlchemy Core `Table` objects. SQLite keeps using its
existing raw-SQL `ensure_*_schema` functions unchanged - this module is
never called against SQLite.

### Migration tool

`tools/migrate_sqlite_to_postgres.py`, with `--dry-run`, `--verify` and
`--force`. `DATABASE_URL` from the environment only, never a CLI
argument. SQLite source opened read-only (`file:...?mode=ro`) and never
written under any flag. Refuses a destination that already has rows
unless `--force`. Primary-key ids preserved exactly (history references
them by number), then every SERIAL sequence advanced past the highest
migrated id so the next application INSERT cannot collide.

FK-safe order computed from the real schema graph via SQLAlchemy's own
`sort_tables`, never hand-guessed, and asserted against the live schema
by a test that fails the moment a new FK is added without updating it.

### Totals

Backend: see the run recorded at commit time - includes 15 new
`test_database_config.py` tests, 8 new always-run
`test_postgres_schema.py` tests, and 4 new `test_hq_runtime.py`
production-config tests. Frontend unit: 56 passed (unchanged - no
frontend change this round).

### Honest status of the PostgreSQL tests

Three `test_postgres_schema.py` tests exercise **real** PostgreSQL
behavior (CREATE TABLE succeeding, an FK actually being enforced, a
repaired sequence actually allowing the next INSERT). They are gated
behind `TEST_POSTGRES_URL` and **are currently SKIPPED** - no PostgreSQL
server and no Supabase project is reachable from this machine yet. They
have never run. Everything proven so far about PostgreSQL is proven at
the schema-graph level (portable DDL, FK ordering, sequence-table
selection), not against a live server.

### A `.env` incident, and the guard added because of it

Mid-round, a `backend/.env` appeared containing `APP_ENV=production` and
a real Supabase `DATABASE_URL` (password included). It was correctly
gitignored and never tracked, so no credential could reach Git - but
`server.py` calls `load_dotenv(backend/.env)` at import, so the entire
test suite inherited production settings: the shared engine resolved to
PostgreSQL, `db.DB_PATH` became `None`, and ~30 modules asserting
`Path(db.DB_PATH) == their_temp_file` failed with an
unrelated-looking `TypeError`. 70 failures, 250 errors.

The serious part was not the noise: this suite creates, migrates and
permanently deletes Stores, and it had been pointed at the live
production database.

**CORRECTION (verified the following day against the real Supabase
project).** An earlier version of this entry claimed "only the absence of
a reachable connection prevented destructive writes". That claim was
WRONG, and the evidence is now in the Supabase project itself: it
contains eight tables created 2026-08-01 14:17:35 UTC, holding 44 seeded
Stores, one `hq_users` row named `founder` - the exact ADMIN_USERNAME the
test fixtures use, and a name that appears nowhere in the live SQLite
database - and 268 `system_logs` rows spanning 14:17:36 to 14:25:45 UTC,
beginning with "SpeakLink server started" and "login_succeeded
user=founder".

Only the eight ORM (`Base.metadata`) tables exist there; all eleven
raw-SQL tables are absent, which is exactly what happens when
`Base.metadata.create_all()` succeeds on PostgreSQL and then
`run_receiver_credential_phase_one`'s SQLite-only DDL (`AUTOINCREMENT`,
`GLOB`) fails against it.

So the writes DID happen: the suite connected to the production database
and seeded it. Nothing of value was destroyed only because the project
was new and empty at the time - not because anything stopped it. The
guard below is what actually prevents a recurrence.

`backend/tests/conftest.py` now forces `APP_ENV=development` and blanks
`DATABASE_URL` before any test module is imported, in every worker.
Blanked rather than deleted, deliberately: `load_dotenv` defaults to
`override=False`, so a deleted variable is simply re-filled from `.env`
a moment later, while an empty string is "present" to dotenv and "not
configured" to `load_database_config`. A regression test asserts the
fail-safe directly (`DB_DIALECT == "sqlite"`, `DB_PATH is not None`).

`backend/.env.example` was rewritten to say plainly that production
settings must NOT go in `backend/.env`, and to point at
`keys/database-url.txt` instead.

### Not done, deliberately

No real data has been migrated to Supabase. The live RC12 installation
and the live SQLite database are untouched by this round.

## Real PostgreSQL compatibility, actually proven

The first Supabase project is treated as **disposable test
infrastructure**, not the future production database - it was polluted
by the `.env` incident above, so it is used to prove compatibility and
then discarded. The production project will be created fresh.

**PostgreSQL 17.6**, TLS confirmed active client-side via libpq
`PQsslInUse` (note: `pg_stat_ssl` reports `False` through Supabase's
Session Pooler because it describes the pooler-to-Postgres hop, not the
client connection - the client-side check is the authoritative one).

### Test isolation, and the incident that shaped it

Every real-PostgreSQL test runs inside a generated `speaklink_test_*`
schema and drops exactly that schema afterwards. Getting that right took
three attempts, two of which looked correct and were not:

1. a `connect` listener issuing `SET search_path` - `SET` is
   transactional, SQLAlchemy's pool defaults to
   `reset_on_return="rollback"`, so the setting was silently reverted on
   the first pool return. **All nineteen tables and five rows were
   created in `public`** while the tests reported green;
2. the libpq option `-csearch_path=` - non-transactional and correct in
   general, but Supavisor does not pass `options` through;
3. the `SET` issued with the DBAPI connection temporarily in
   **autocommit** - session state, nothing for a rollback to revert, no
   cooperation needed from the pooler. This is what is used.

The leak was caught by
`test_the_test_schema_is_isolated_and_public_is_never_touched`, which
asserts the boundary from inside a test rather than only in the fixture.
The leaked tables were dropped by name (SpeakLink-owned tables only) and
`public` is empty again. See Learning Box 32.

### Totals

`test_postgres_schema.py` 12 passed (including the 3 that had never
executed: real CREATE TABLE, real FK enforcement, real sequence repair)
and `test_postgres_integration.py` 14 passed - 26 real-PostgreSQL tests
covering user creation and unique-username enforcement, Store CRUD and
lifecycle, tombstone fields with history preserved, RBAC roles and
overrides with their CHECK constraints, Store/City/Zone scope and its
shape constraint, Device enrolment, credential uniqueness and the
revocation rule, at-most-one-primary-per-Store, `ON DELETE RESTRICT`
protecting a Store that owns Devices, broadcast session/target/event
writes, and UTC timestamp round-tripping in both DateTime and string
columns.

Residue after the run: 0 tables in `public`, 0 leftover test schemas.
`auth` (23), `storage` (8), `realtime` (3), `vault` (2) and `extensions`
(2) all untouched throughout.

Offline behaviour is unchanged: without `TEST_POSTGRES_URL` these 18
tests skip and the ordinary suite needs no PostgreSQL and no network.

### Incompatibilities found

None in the schema itself. Two test-only defects were found and fixed:
raw INSERTs omitted `is_online_store`/`status`, which have Python-side
ORM defaults but no server defaults - harmless for the migration tool,
which copies every column explicitly, but a real difference between
ORM-mediated and raw-SQL writes.

## Supabase region change and the production snapshot

The first production candidate was created in the wrong region and has been
rejected. Three projects have existed; identify them by the non-secret
project-ref fingerprint (sha256, first 16 hex) - never by hostname, which
two projects in one region share:

* `94778c6f130c34c1` - disposable test infrastructure (polluted by an
  accidental seed, later used deliberately for real-PostgreSQL tests);
* `faee6d157d5f03d3` - **rejected, wrong region**. A full verified
  migration was completed into it before the region problem surfaced. Must
  not be used for cutover;
* `e720ac35878a1d7b` - **the production database**, correct region, holds
  the verified snapshot.

`system_identifier` is not a discriminator on Supabase: projects are
provisioned from a common base image and share it. Freshness was proven
instead by the project-ref fingerprint plus zero SpeakLink statements in an
otherwise-populated `extensions.pg_stat_statements` (618 rows of the
project's own provisioning queries).

### Snapshot contents and verification

Source: a fresh SQLite backup of live RC12 (`speaklink-20260801-pre-supabase-
region-migration.db`, SHA-256 `A300D48FCE7F3306C7C15F9E4EBF016F574F2F6F6A
3BFD8AB0D398632E3C3162`, integrity ok, FK 0, taken with no broadcast live).

All 19 source-backed tables match exactly. Three tables exist only on the
admin-management feature branch and migrated as **NEW_SCHEMA_EMPTY** (0
rows): `user_deletion_events`, `device_deletion_events`,
`admin_deletion_events`. That is correct - the live RC12 database does not
contain them, and inventing rows for them would be fabrication.

Bindapur remains Store id 31 with Device
`3b1ff11f-0b18-4f56-b911-30f036cbddd9` active and primary. RBAC roles,
permission overrides, scope audit history and the AYUSHK tombstone (with
its deletion audit row and three broadcast-target history rows) all
survived. Zero orphans across ten FK relationships, zero duplicate
store_code/public_id/username/primary-per-Store, and every sequence
resumes past its migrated MAX(id).

### A correction about sequence verification

An earlier round verified sequences by calling `nextval` inside a
transaction it then rolled back, and called that non-destructive. That was
wrong: `nextval` is explicitly non-transactional and a rollback does not
give the number back. The consequence was harmless - one skipped id - but
the claim was false. Sequence state is now read with
`SELECT last_value, is_called FROM <sequence>`, which allocates nothing.

### Migrated RBAC reflects RC12, not the feature branch

`permissions` (24) and `role_permissions` (57) were copied from the RC12
source, so the migrated matrix is RC12's. The five permission codes the
admin-management branch adds are not present yet. That is expected and
self-correcting: `ensure_permission_schema` re-seeds the catalog
additively at startup, so whichever RC is eventually installed will add
them on first boot.

### Live HQ unchanged

RC12 + SQLite remains authoritative. No RC was installed, `hq-runtime.json`
was not modified, `APP_ENV` was not switched, and nothing was pushed. The
Supabase project holds a verified snapshot only - anything written to RC12
after the snapshot is not in it, so the cutover must re-run the migration
as its delta step.

---

## Admin Management frontend (feature/admin-management-search-filter-delete)

The six admin screens now use the server-side search, filter and paging
endpoints the previous round built. Nothing was installed, no RC was cut,
and nothing was pushed.

### The shared toolkit

`frontend/src/lib/adminList.js` and `frontend/src/components/AdminFilters.jsx`
hold the parts all six screens share, for one reason: writing this six times
produces six subtly different versions, and the difference that bites is
always the same one. Two rules are encoded there rather than repeated:

1. **Any filter change resets to page 1.** Without it, narrowing a search
   while on page 3 shows an empty screen that reads as "no matches" rather
   than "you are past the end".
2. **Select All Filtered is a MODE, not a materialised id list.** The UI
   holds the intent - "everything matching what you can see" - and the
   request carries `{mode: "filtered", filters}`. The backend resolves the
   matched set inside the caller's own Store Scope using the same query as
   `/search`. React never pages through thousands of rows to enumerate ids
   it would post straight back, and the count the operator agrees to is the
   server's own `total`.

Loading, error and empty are three distinct states in `ListState`, because
collapsing them is how a failed request gets read as "nothing found". A 403
says so in those words.

### Screens

| Screen | Endpoint | Controls |
|---|---|---|
| Receiver Status | `/receivers/search` | Search, Zone, City, Store, status, Primary |
| System Logs | `/logs/search` | Search, date range, level, User, Store, Device, archived; row select, Select Page, Select All Filtered, Archive, Permanent Delete |
| Broadcast History | `/broadcast/history/search` | Search, date range, status, User, Zone, City, Store, archived; bulk Archive/Unarchive, Permanent Delete |
| User Management | `/users/search` | Search, Role, lifecycle state, Store/City/Zone Scope, include-deleted; permanent delete with typed username + acknowledgement |
| Receiver Devices (new, `/devices`) | `/receiver-devices/search` | Search, Zone, City, Store, status, Primary, lifecycle; permanent delete with typed public_id + acknowledgement |
| Rights | (none - see below) | Search, Category, Allow/Deny/Inherit, explicit-override filter |

The Zone/City/Store dropdowns come from `/receivers/filter-options`, which
is built from the same scoped query as the list - so the options can never
name a Zone whose Stores this account may not open.

### Rights filtering is client-side, deliberately

Every other admin list filters on the server because every other one is
unbounded. The permission catalog is not: 29 fixed rows defined in
`backend/permission_catalog.py`, all of them already fetched by the single
`GET /users/{id}/permissions` the editor makes, and a new one appears only
when somebody writes code to add it. A round trip per keystroke to narrow a
constant would be slower and would add an endpoint whose only job is
re-filtering source code.

The rule this exception is measured against: **filter on the server when the
row count is driven by data, on the client when it is driven by source
code.** If the catalog ever becomes data-driven, this moves. The page says
so on screen as well, so the exception is not mistaken for an oversight.

### Archived and deleted never look alike

Archived is reversible and keeps its Restore control. Permanently deleted is
a tombstone kept only so history stays readable: it is red, it says "kept
only so history stays readable, this cannot be restored" in words, and
Restore/Enable/Delete are all absent from the row. This is enforced for both
Users and Receiver Devices, and proven in the browser rather than asserted.

### New fleet-wide Receiver Devices page

`/devices` answers "where is that Device?", which cannot be asked one Store
at a time across dozens of Stores. The per-Store page keeps enrolment,
credential rotation and promotion - those are things you do while looking at
one Store. It shares `menu.receivers.view` rather than inventing a second
permission that could grant one view and withhold the other.

### One backend change, test-first

`SessionOut` gained `archived_at`. With `include_archived` the History list
mixes archived and live rows, and the operator has to be able to tell them
apart and know whether Archive or Unarchive applies - so the flag has to
travel on the row, not only in the filter. RED test
(`test_a_session_row_says_whether_it_is_archived`) written first, then the
field.

### A bug the tests found

`craco.config.js` declared the `@` webpack alias but no Jest
`moduleNameMapper`, so any unit test importing a module that used `@/...`
failed to run at all - not failed an assertion, failed to load. The two now
name the same directory. This is why `adminList.test.js` exists at all: it
was the first unit test to import through the alias.

### Verification

- Backend: **2716 passed, 21 skipped, 0 failed**. One run before this showed
  a single `test_smoke.py::test_sqlite_test_database_connection` failure;
  it passes in isolation and passed on the immediately following full run,
  and is a known race between `-n 2` workers over the shared smoke database,
  not a defect in this change.
- Frontend unit: **66 passed** (was 56; +10 for the new toolkit).
- Playwright: **211 passed** (was 192; +19 in
  `e2e/admin-search-and-delete.spec.js`).
- Production frontend build: compiled successfully.
- `python -m compileall backend`, `pip check`, `git diff --check`: clean.
- Secret scan over the whole change: clean.

Four existing Playwright assertions were updated to the shared list-state
test ids (`list-empty`, `list-error`, `list-loading`), which replaced the
per-page ones. The behaviour they assert is unchanged.

### Not done, and why

Real PostgreSQL proof has NOT been run: `TEST_POSTGRES_URL` is not
configured in this environment, and production Supabase is not an acceptable
target for destructive tests. RC14 is not built, per the standing
instruction that it waits for frontend + Playwright + a disposable
PostgreSQL proof.

Status at this point: **READY_FOR_DISPOSABLE_POSTGRES_VALIDATION**.

---

## Deployment hosting discovery (research round, nothing deployed)

Discovery only. No code changed, no cloud resource created, no router port
opened, nothing installed, nothing pushed.

### Requirements read out of the code, not assumed

| Property | Value | Source |
|---|---|---|
| WebSocket routes | 3 (`/api/ws/receiver`, `/api/ws/hq`, `/api/ws/broadcaster`) | `server.py` |
| Sockets per Store | 1 per enrolled Device; primary carries audio, standbys carry none | `ws_manager.py` |
| Heartbeat | every 5 s; stale 15 s; offline 30 s | `receiver_contract.py` |
| Audio | WebM/Opus mono, 32 kbps target, 250 ms chunks (4/s) | `audio_protocol.py` |
| Server audio work | **none** - a pure byte relay. FFmpeg is a RECEIVER requirement only | `audio_streaming.py` |
| Measured cost | 86.1 MB RSS and ~4 % of one core at 40 Receivers | `docs/LOAD_TEST_REPORT.md` |
| Restart safety | in-memory socket state only; Agents reconnect with jittered backoff and **do not re-enrol** | `ws_manager.py`, `tools/receiver_agent.py` |
| Inbound ports | HTTPS/WSS on 443 is sufficient; no arbitrary TCP/UDP | - |

### The constraint that decides the hosting question

`receiver_key_ring()` opens a **Windows DPAPI**-sealed HMAC container on the
local filesystem, and `DpapiProtector` refuses to run when
`sys.platform != "win32"`. So the server as it stands needs **Windows and a
persistent disk**, and that is not a data-storage requirement Supabase can
absorb - it is a key-custody requirement. Any host without a persistent disk
(every free PaaS tier examined) cannot carry Receiver Device authentication
without either a Linux key-custody port or the staging-only
`SPEAKLINK_KEY_PROTECTOR=fake`, which must never hold real Store credentials.

### Bandwidth, so platforms could be rejected on evidence

32 kbps/Store payload + 10 % for WebSocket/TLS/TCP framing:

| Stores | 1 | 10 | 40 | 50 | 100 |
|---|---|---|---|---|---|
| Wire | 35 kbps | 352 kbps | 1.41 Mbps | 1.76 Mbps | **3.52 Mbps** |

A 100-Store broadcast costs ~26 MB/minute, ~1.58 GB/hour. Idle heartbeat at
100 Stores is ~3.6 GB/month outbound (the server sends no per-heartbeat
reply, and heartbeats write nothing to the database). **Bandwidth is not the
binding constraint** - sleep, restarts, instance-hours and terms are.

### Decisions recorded

* **No reputable unrestricted lifetime-free, no-card managed compute was
  proven to exist.** Oracle Cloud is the one credible always-free VM and the
  operator excludes it (card/identity verification). Everything else found in
  search was an SEO content farm and is not recommended at any level.
* **Render Free = PILOT_ONLY.** Singapore region suits Delhi, WebSockets are
  supported and inbound WebSocket messages reset the idle timer. But it caps
  at 750 instance-hours/month, has **no persistent disk** (so no DPAPI
  container), the provider restarts services at will, the card requirement at
  signup is **UNPROVEN** (docs imply none, Render's own feedback board carries
  repeated reports of one), and Render's documentation says plainly: *"Do not
  use them for production applications."*
* **Cloudflare Workers = REJECT for the current architecture.** Not because
  Workers are weak, but because the execution model is different: 10 ms CPU
  per request, 128 MB per isolate, WebSocket server state only via Durable
  Objects, no OS, no filesystem, no subprocess, and no established path for
  SQLAlchemy 2 + psycopg 3 or DPAPI. Adopting it means rewriting `ws_manager`,
  `audio_streaming`, `receiver_auth_*`, `key_custody*` and the DB layer -
  precisely the modules the 2716-test suite covers. No benefit justifies that
  now.
* **Self-hosted Windows HQ + Cloudflare Tunnel = strongest zero-card pilot
  candidate.** It is the only option that preserves DPAPI custody, the Windows
  runtime, one Uvicorn worker and no Docker; it works behind CGNAT because the
  tunnel is outbound-only; it gives a permanent public hostname so **Receivers
  never re-enrol when the server moves**; and Cloudflare states WebSockets are
  supported on all plans. Cloudflare's own Delhi edge suits the Stores.
* **Production approval remains BLOCKED**, on two things and not on opinion:
  1. **Cloudflare Service-Specific Terms §2.8**, which reserves the right to
     limit CDN use for serving *"video or a disproportionate percentage of
     pictures, audio files, or other large files"* without the relevant paid
     services. Whether sustained WebSocket Opus fan-out to 100 endpoints falls
     inside that is **not settled by the text**, and the remedy is at
     Cloudflare's discretion. This must be answered by Cloudflare in writing
     before the fleet grows past a pilot.
  2. **No tunnel soak or load evidence exists yet** - the 40-Receiver load
     test was loopback, with the tunnel absent from the path.

  Also note a named tunnel requires a domain registered on the Cloudflare
  account. That is a real cost (~Rs 1,000/year), so this route is
  **no-card**, not **zero-cost**.
* `trycloudflare.com` quick tunnels are rejected outright: 200 concurrent
  requests, no SLA, and Cloudflare documents them as *"testing and development
  only"*.
* **Fly.io rejected** - *"All organizations ... require a credit card on
  file."* **Railway rejected** - $5 one-time trial then a paid plan.
  **Hugging Face Spaces rejected** - Docker/Gradio Spaces now require a paid
  plan, free hardware sleeps, and outbound is limited to ports 80/443/8080 so
  the Supabase pooler is unreachable. **PythonAnywhere free rejected** -
  outbound is whitelist-restricted, so the Supabase pooler is unreachable, and
  ASGI/WebSocket support is beta. **Koyeb** no longer advertises a free
  compute tier. **Vercel/Netlify** cannot host a long-lived WebSocket server
  at all, though either (or Cloudflare Pages) is fine for the React bundle.

### Two deployment facts found in code, not yet acted on

1. The frontend resolves the API origin as `{page-protocol}//{page-host}:8000`
   unless `REACT_APP_BACKEND_URL` is set at build time (`frontend/src/lib/api.js`).
   Behind any 443-only host that port does not exist, so the production bundle
   must be built with the public origin.
2. HQ currently runs two processes on two ports (backend 8000, frontend 3000)
   via `tools/hq_runtime.py`, while most managed hosts expose one.

Neither is a defect - both follow from a LAN-first design - and neither was
changed in this round.

### Verdict

`PILOT_ONLY_ZERO_CARD_OPTION_FOUND`. A zero-card pilot path exists. It is not
yet proven as a zero-card production path for 50-100 Stores.

---

## Real-PostgreSQL validation of the admin-management round

Run against a disposable Supabase TEST project (fingerprint
`7263a431a7754638`, PostgreSQL 17.6, ap-south-1). Production
(`e720ac35878a1d7b`) was touched only by a read-only transaction. Nothing was
installed, no cutover was performed, nothing was pushed.

### The gates that ran before anything destructive

| Gate | Result |
|---|---|
| Production fingerprint matches documented `e720ac35878a1d7b` | PASS |
| TEST fingerprint differs, different project, different password | PASS |
| Read-only inventory: `public` empty, 0 leftover schemas, no `public.stores` | PASS |

A note on the convention, because it cost a round: the documented fingerprint
is `sha256("postgres.<project-ref>")[:16]` - the **full pooler username**, not
the bare ref. Hashing the ref alone produces a different value that reads as a
mismatch on a project that is in fact correct.

### Seven PostgreSQL-only defects, every one invisible to 2716 SQLite tests

| File | Defect | Consequence on Supabase |
|---|---|---|
| `user_deletion.py` | `is_active = 1` in WHERE | last-SUPER-ADMIN guard crashes |
| `user_deletion.py` | `is_active = 0` in UPDATE | permanent User deletion fails |
| `store_deletion.py` | `is_active = 0` in UPDATE | permanent Store deletion fails |
| `store_deletion.py` | one bind parameter shared by `VARCHAR` + `TIMESTAMP` | "inconsistent types deduced for parameter" |
| `store_deletion.py` | `CREATE TABLE ... AUTOINCREMENT`, unguarded `PRAGMA foreign_key_check` | the deletion audit table cannot be created |
| `store_lifecycle.py` | `PRAGMA table_info` | **HQ does not boot** |
| `user_lifecycle.py` | `PRAGMA table_info` | **HQ does not boot** |

The last two are the ones that matter most. They run in `ensure_*_schema` at
every start-up, so the failure would not have been a broken feature - the
cutover would have failed at boot, before anything could report why.

Fixed with bound Python booleans (which SQLAlchemy renders correctly per
dialect, so there is no second code path to keep in step), the SQLAlchemy
Inspector in place of `PRAGMA`, distinct bind parameters per column type, and
the portable `postgres_schema` Table definition that already existed beside
the raw DDL. `server.py:2752` already carried a correct dialect branch for
exactly this - the trap was known, and had been fixed in one of the five
places it existed.

### Totals

| Suite | Result |
|---|---|
| `test_postgres_schema.py` + `test_postgres_integration.py` | 28 passed |
| `test_postgres_admin_management.py` (new, 29 tests) | 29 passed |
| **PostgreSQL total** | **57 passed, 0 failed** |
| Backend (SQLite, after the fixes) | 2716 passed, 50 skipped, 0 failed |
| Frontend unit | 66 passed |
| Playwright | 211 passed |
| Production frontend build | Compiled successfully |
| compileall / pip check / git diff --check / secret scan | PASS |

The backend skip count moved 21 -> 50 because the 29 new PostgreSQL tests
skip when `TEST_POSTGRES_URL` is absent, which is every ordinary offline run.

### Cleanup evidence

```
leaked speaklink_test_* schemas : 0
tables in public               : 0
supabase-managed schemas       : auth, extensions, graphql, realtime, storage, vault
  their table counts           : auth=23, extensions=2, graphql=0, realtime=3, storage=8, vault=2
  SpeakLink tables inside them  : 0
```

### Production read-only verification

Executed inside `SET TRANSACTION READ ONLY` (confirmed `on`). Nothing written.

| Check | Result |
|---|---|
| Fingerprint | `e720ac35878a1d7b` |
| Rotated credential connects | yes |
| Snapshot | 22 tables (19 source-backed + 3 feature-branch), 45 Stores, 3 Users, 5 Devices - identical to the migration record |
| Store 31 | `BP` / Bindapur / ME ZONE / active |
| Store 31 primary Device | identity `9301b399232a8e7c`, status active - unchanged |
| `speaklink_test_*` schemas in production | 0 |

### A credential mistake worth recording

The first attempt failed because the TEST file had been built by copying the
production URI and changing only the project-ref - so it carried the
**production password**, which the test project rightly rejected. Two
consequences: the production credential existed in a second file on disk, and
it was transmitted (over TLS, to Supabase's own pooler, and refused) during
two authentication attempts. Build a test URI from the test project's own
connection string, never by editing production's.

Related: a password containing `%` is percent-decoded by URI parsing, so the
value actually sent is not the value in the file. Alphanumeric database
passwords avoid the entire class of problem.

### Incident: live HQ frontend stopped during this round

While arranging Playwright, port 3000 was found occupied by the live RC12 HQ
static server (`spa_server.py`, bound to 192.168.4.134). The Playwright run
was moved to port 3123 via a config held outside the repository, and the
suite passed 211/211 - but `spa_server.py` had already exited during the two
earlier attempts against port 3000. The HQ **backend** stayed up throughout,
so Store Receivers - which connect to the backend WebSocket on 8000 and never
to 3000 - were unaffected; the HQ browser UI was not reachable until an
operator restarted it.

The rule this earns: **never point a test harness at a port on a machine
running live HQ without checking who owns it first.** Playwright's
`reuseExistingServer` makes this worse, not better, because it will silently
adopt whatever answers.

---

## Supabase cutover attempt: BLOCKED by the Receiver authentication service

The final cutover was authorised and attempted. It stopped before any
destructive operation. **Production Supabase was not reset, not migrated and
not written to.** Live HQ is back up on SQLite and unchanged.

### The blocker

`receiver_auth_service.authenticate_receiver_credential` is the single
function every Receiver WebSocket handshake goes through. Its first
precondition is:

```python
if engine.dialect.name != "sqlite" or _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
    raise _configuration_failure()
```

**It refuses any engine that is not SQLite.** Not degrades - refuses. So
`RC14 + Supabase` produces an HQ that boots, serves the admin UI, and cannot
authenticate a single Store Receiver. That is the worst possible shape of
failure, because everything an operator can see looks healthy.

The same function is SQLite-only in four further places, all unconditional:

| Line | Construct | On PostgreSQL |
|---|---|---|
| 531-533 | `PRAGMA foreign_keys=ON` / `PRAGMA foreign_keys` | syntax error |
| 205 | `PRAGMA table_info("<table>")` | syntax error |
| 211, 222 | `SELECT ... FROM sqlite_master` | relation does not exist |
| 247 | `PRAGMA foreign_key_check` | syntax error |

### And two tables the migration tool does not carry

The auth service also reads `schema_migrations` and
`receiver_credential_migration_state` to decide the credential verification
mode (currently `hash_only`). Neither table is in
`migrate_sqlite_to_postgres.TABLE_ORDER`, so neither would reach PostgreSQL
even once the dialect checks are fixed. Frozen SQLite holds one row in each.

### Why the earlier PostgreSQL round did not catch this

The 57 PostgreSQL tests exercise the schema, the service modules and the
query semantics. They do not drive the Receiver WebSocket handshake, and
`test_postgres_admin_management.py` says so in its own docstring. This is
exactly the gap recorded in the cutover plan as "Stage A5 - RC14 has not been
proven to boot and operate against PostgreSQL at runtime level, only at
test-suite level". That assumption turned out to be false, and it was false
in the one subsystem that must never break.

**Test count is not coverage of a code path nobody called.**

### What was done, and what was not

| Phase | Outcome |
|---|---|
| 0 Safety | PASS - RC14 31/31, fingerprint `e720ac35878a1d7b`, no active broadcast |
| 1 Reset tool | DONE - `tools/reset_postgres_destination.py` + 13 tests |
| 2 Freeze SQLite | DONE - task stopped, two orphaned uvicorn processes from the 11:33 double-start also stopped, quiescence proved by two samples 5 s apart |
| 3 Final backup | DONE - SQLite backup API, `integrity_check ok`, 0 FK violations |
| 4 Reset production | **NOT RUN** |
| 5 Migration | **NOT RUN** |
| 6-9 | **NOT RUN** |
| 10 Rollback | DONE - HQ restarted on SQLite, `READY`, backend and frontend HTTP 200 |

### Frozen SQLite counts (the backup is the immutable source when this resumes)

796 rows across 21 tables: 45 stores, 3 hq_users, 16 broadcast_sessions,
105 broadcast_targets, 149 receiver_events, 339 system_logs, 5
receiver_devices, 6 receiver_credentials, 1 receiver_store_primary_device,
24 permissions, 57 role_permissions, 2 user_permission_overrides, 0
user_store_scope, 1 store_deletion_events, 12 store_scope_audit_events,
12 receiver_enrollment_codes, 12 receiver_credential_events, 3
permission_audit_events, 2 login_security_state, 1
receiver_credential_migration_state, 1 schema_migrations.

The three feature-branch tables (`user_deletion_events`,
`device_deletion_events`, `admin_deletion_events`) do not exist in SQLite and
would migrate as empty, as previously recorded.

### Bindapur

Store 31 was online at Phase 0 and disconnected at 09:11:45 UTC when HQ was
stopped - expected in a maintenance window. It had **not** reconnected after
~2.5 minutes of polling, which is past the Agent's 60 s maximum backoff. HQ
itself is healthy (backend HTTP 200), so the cause is Store-side or VPN-side
and needs an operator check on the Store PC. No Receiver was re-enrolled and
no credential was rotated.

### What has to happen before this can be retried

1. Make the Receiver authentication path dialect-aware - the four SQLite-only
   constructs and the hard `dialect.name != "sqlite"` refusal. This is the
   highest-risk code in the product and needs its own round with real
   PostgreSQL tests driving an actual handshake, not a schema check.
2. Add `schema_migrations` and `receiver_credential_migration_state` to
   `TABLE_ORDER` (with their FK position), or decide deliberately that the
   PostgreSQL side re-derives them at start-up.
3. Rebuild and verify a new RC carrying those fixes. **RC14 cannot perform
   this cutover.**
4. Prove the whole thing at runtime against the disposable TEST project
   before touching production: HQ start-up, `READY`, and one real Receiver
   handshake.

---

## RC15: Receiver authentication works on PostgreSQL

The blocker that stopped the cutover is fixed, and - more importantly - it is
now proven by a real Receiver reaching CONNECTED over a real WebSocket against
real PostgreSQL. That proof did not exist before RC14 and is why RC14 shipped
a total Receiver outage nobody could see.

### What was actually wrong

Nine SQLite-only assumptions on the path a Receiver takes to authenticate:

| Where | Assumption | Effect on PostgreSQL |
|---|---|---|
| `receiver_auth_service._validate_inputs` | `dialect.name != "sqlite"` -> refuse | **every Receiver refused** |
| same | `PROTECTED_DATABASE_PATH` file-path compare | meaningless off SQLite |
| `_columns` | `PRAGMA table_info` | syntax error |
| `_validate_schema_and_state` | `sqlite_master` (tables) | relation does not exist |
| same | `sqlite_master` (indexes) | relation does not exist |
| same | `PRAGMA foreign_key_check` | syntax error |
| `authenticate_receiver_credential` | `PRAGMA foreign_keys=ON` + verify | syntax error |
| `_hash_candidates` | `:public_id IS NULL` with a bare parameter | `could not determine data type of parameter` |
| `server.build_receiver_runtime_authenticator` | `sqlite_master` probe inside a bare `except` | **silently degrades the whole fleet to legacy Store-token auth** |

The last one deserves its own line. It does not raise anything a caller sees:
the probe throws, the `except Exception: return None` swallows it, and HQ then
serves the legacy authenticator - which refuses every Device credential. Same
shape as the RC14 blocker: healthy dashboard, zero Stores able to connect.

Plus two schema gaps that would have refused everybody even after the dialect
fixes: `schema_migrations` and `receiver_credential_migration_state` were
absent from `postgres_schema` and from the migration tool's `TABLE_ORDER`, and
none of the four `_REQUIRED_INDEXES` existed on the PostgreSQL side.

### The two state tables are MIGRATED, not re-derived

Deliberate, and the reasoning is a security one.
`run_receiver_credential_phase_one` creates them fresh at `legacy_only` with
`legacy_verification_enabled = 1`. The live fleet runs `hash_only` with legacy
verification OFF, meaning a Store's old shared token is no longer accepted
anywhere. Re-deriving on a PostgreSQL start-up would silently **reactivate**
legacy Store-token authentication - a regression in the dangerous direction,
because it authenticates MORE, so nothing fails and nothing alerts. Copying
the rows preserves the decision that was actually made.

### One more thing start-up did not do

`ensure_permission_schema` was SQLite-only (`AUTOINCREMENT`, which `IF NOT
EXISTS` does not save because the statement still has to parse). Its DDL was
not the important part - the RESEED was. Without it a cutover would carry the
RC12 catalog forever and every feature guarded by a newly added code would be
denied to everybody with nothing explaining why. Measured on PostgreSQL after
the fix: **29 permissions, 64 role_permissions, 44 Stores, 1 administrator**,
including all five admin-management codes that do not exist in RC12's catalog.

### The proof RC14 never had

`tests/test_postgres_receiver_handshake.py` starts the whole application
against an isolated PostgreSQL schema and drives a real Receiver WebSocket:

* the application starts and serves, dialect asserted `postgresql`;
* an administrator signs in;
* **a Receiver with an existing credential reaches CONNECTED**, is registered
  in the connection manager, and its connection is recorded in
  `receiver_events`;
* a wrong credential is refused at the socket;
* authenticating rotates and re-issues nothing - **no re-enrolment**.

It claims nothing about audio. No `PLAYBACK_CONFIRMED`, no
`SPEAKER_VERIFIED`; those need a real Receiver, a real amplifier and an
operator's ears.

### Gates

| Gate | Result |
|---|---|
| PostgreSQL suite | **90 passed, 0 failed** (was 57) |
| Backend SQLite suite | **2724 passed, 75 skipped, 0 failed** |
| Frontend unit | 66 passed |
| Playwright | 211 passed (isolated port 3123 - live HQ owns 3000) |
| Production build | Compiled successfully |
| compileall / pip check / diff --check / secret scan | PASS |

### A test-isolation bug this round exposed

`test_smoke.py` asserted `PRAGMA database_list` returns exactly one row. It
also returns a `temp` row once anything on that connection has created a
temporary object, so the assertion really said "nobody has ever used a temp
table on this connection" - which the test does not control. Adding two
unrelated test files changed xdist's `loadscope` distribution and made it
true. It now selects the `main` row by name, which is what it always meant.

### A process mistake worth recording

Midway through, a `git stash` used to compare against clean code was
interrupted after the stash but before the pop. Several gate runs then
executed against stashed-out code and looked green while proving nothing about
the change. The stash was recovered intact and every gate re-run. **A gate run
is only evidence if the working tree contained the change** - and a stash is
an easy way to quietly break that.

---

## Incident: SpeakLink outage, and the three supervisor defects behind it

Two symptoms were reported: enrolment codes refused, and Stores offline. They
had different causes and only one of them was a code defect.

### Why enrolment failed

Not corruption, and not an enrolment bug. Two codes for Store 25 (JHA) were
created at 11:40:22 and 11:41:04 while HQ was temporarily running against
PostgreSQL during the cutover attempt. HQ was rolled back to SQLite at
11:43:30, and writes made in that window are not replayed backwards - so from
the live system's point of view those codes had never been issued, and the
backend refused them correctly.

The live SQLite database held 12 codes, **0 of them usable**, and 0 for Store
25. Also lost: 11 `system_logs` rows. Nothing else - no Store, Device,
credential or history row was affected.

This was avoidable and is on me: the rollback was performed while the operator
was actively using the system, and the lossy-rollback property was documented
but not communicated loudly enough before the window opened.

### Why Stores showed offline

`RECEIVER_NOT_REACHING_HQ`, not `AUTH_REJECTED`. Zero ESTABLISHED connections
on port 8000, zero Receiver authentication failures ever recorded, and no
`receiver_event` since 09:44:28. HQ was refusing nobody; nobody was arriving.

Store 31's row still read `status='online'` because HQ had been stopped
abruptly and never wrote the disconnect. That column is NOT what the dashboard
uses - `search_receiver_status` overrides it with
`manager.online_store_ids()`, the live WebSocket state - so the dashboard was
telling the truth while the column lied. Verified from code; no change made.

### The three P0 defects found

**1. Start-up spawned duplicate backends.** `supervise_child` started a child,
asked for health once immediately, and on a negative answer started another
without stopping the first. Against SQLite the first answers instantly.
Against Supabase the backend needs ~10 s for its start-up migrations, so
attempts 2 and 3 landed on top of a still-starting backend; the first won port
8000, the others died, and the supervisor was left watching a dead child.
Every 15 s it "restarted the backend", lost the same race, and after six
strikes went DEGRADED and stopped the frontend - while reporting READY
throughout.

Fixed by separating start-up from ongoing health: a bounded startup grace
during which a live-but-silent child is left alone, every failed attempt
reaped (whole tree) before the next, and `ChildOutcome` now carries the child
that actually became healthy. Deliberately not "raise the timeout" - that
hides a dead backend for exactly as long as it protects a slow one.

**2. One missed probe created a duplicate.** `watch_once` restarted on the
first failed probe. Now three consecutive misses are required, any good probe
clears the count, and the old child is reaped before a replacement starts.

**3. Children outlived a hard-killed supervisor.** `stop()` reaps correctly -
but only when it runs. Task Scheduler's Stop kills the supervisor outright,
which stranded uvicorn and spa_server.py on ports 8000 and 3000 three separate
times during this work. Children are now assigned to a Windows Job Object with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, moving the guarantee into the kernel.
**Proven on the live install**: stopping the task released both ports with no
survivors and no manual PID hunting.

**4. Sequence repair used a hand-kept list** (found during the cutover, fixed
here). `login_security_state` was missing from it, so its sequence sat at
`(1, false)` with rows 1 and 2 present - the next login-security write would
have failed on a duplicate key. The set is now discovered from the catalog,
covering SERIAL and IDENTITY alike.

### Neither database was corrupted

SQLite: `integrity_check ok`, 0 FK violations, Bindapur Device/primary/
credentials intact, `hash_only`/`0` preserved. PostgreSQL holds its verified
migrated copy, untouched by this incident.

### Gates

| Gate | Result |
|---|---|
| Backend SQLite | **2736 passed, 80 skipped, 0 failed** |
| PostgreSQL | **95 passed, 0 failed** |
| Frontend unit | 66 passed |
| Playwright | 211 passed (isolated port 3123) |
| Production build | Compiled successfully |
| compileall / pip check / diff --check / secret scan | PASS |

### RC16

`SpeakLinkHQ-0.1.0-rc16-227c2b8-20260802-180703`, 31/31 PASS, installed.
The runtime executable was genuinely rebuilt from `hq_runtime.spec`:
`FC97E7EB…65F` -> `224DB2AD…78F4`. A hash unchanged after `hq_runtime.py`
changed would have meant a stale build, and was checked for explicitly.

Installed with `app_env` absent, so **SQLite remains authoritative**. Supabase
was not activated.

---

## Self-contained HQ installer — PAUSED by the operator (2026-08-03)

The project to ship a single `SpeakLinkHQSetup-*.exe` that needs no Python,
Node or the repository on a fresh Windows PC is **paused**. No installer is
currently being pursued.

### Where the work lives

Branch `wip/self-contained-hq-installer-paused`, checkpoint `6d0bfae`.
`feature/self-contained-hq-installer` is also kept. Nothing was deleted.

**That branch is not a release candidate and must not be installed on live
HQ.** Its Phase 7 tests were not green when work stopped.

### What it had established

* `SpeakLinkHQBackend.exe` and `SpeakLinkHQFrontend.exe` — the API server and
  the SPA server, each carrying its own CPython. Proven to run the tree
  `SpeakLinkHQRuntime.exe -> Backend + Frontend`, READY, both ports 200, with
  `PATH` stripped to the Windows system directories: no `python.exe`, no
  `node.exe`, no command line naming the repository or `.venv`, zero orphans.
* The supervisor's PATH search for `python` — which ran in the **packaged**
  runtime — removed, and a missing child executable now fails closed.
* The bind address moved out of source. `DEFAULT_HQ_ADDRESS` was the literal
  address of one machine on one LAN.

### The finding worth remembering

`migrations.run_receiver_credential_phase_one` creates every Receiver table
and has **no production caller** — 89 callers, all tests. A fresh machine can
reach "startup complete" with no `receiver_devices` table, and the only hint
is a warning blaming a column. Whoever resumes this starts there.

### The active application branch

`feature/admin-management-search-filter-delete` is **architecturally
identical to RC18**, plus one independent API fix cherry-picked from the
installer work (`d2454db`): a permanently deleted Receiver Device could still
be returned by `/api/receiver-devices/search?include_deleted=true`. No
installer file, self-contained runtime change or fresh-profile initializer
exists on this branch.

Live RC18 was not modified at any point and remains READY on SQLite.

---

## Concurrent broadcasts (in progress, 2026-08-03)

Several HQ operators can now broadcast at once, and a Store belongs to at most
one of them.

### What exists

**Store leases** (`bc337a3`). `broadcast_store_leases` with a PARTIAL UNIQUE
INDEX on `store_id WHERE released_at IS NULL`. The database is the final
guard, not the application: a 12-thread barrier race on one Store produces
exactly one winner, and a hand-written conflicting INSERT is refused. Claims
are all-or-nothing, so a start that touches one busy Store reserves none of
them. `STORE_BUSY` names Store codes only - never the owning user, their
campaign, or the session id.

**Per-session runtime** (`42da03d`). The four singleton fields
(`active_broadcaster_ws`, `live_session_id`, `live_target_store_ids`, one
`AudioFanout`) are gone. Each live broadcast owns its own fanout, queues and
microphone socket. Byte-marked tests prove audio from one session never
reaches another's Stores, and a stalled Store in one session delays neither
its siblings nor any other session.

**Ownership** (`d0887d2`). Normal Stop is own-session-only for EVERY role
including OWNER; the refusal is byte-identical to "no such session" so the
route cannot be used to enumerate other people's broadcasts. The microphone
socket takes `session_id` and is refused unless that session is live and
`started_by` the authenticated account.

### Restart orphan reconciliation

A restart destroys the microphone socket and every audio queue, but leases are
persistent by design - so an unclean stop could leave `status='live'` rows
holding Stores that then answered STORE_BUSY for ever.

At startup, after the schema exists and before any broadcast can start,
`broadcast_reconciliation.reconcile_orphaned_broadcasts` closes every
persisted live session with no runtime owner and releases its leases in ONE
transaction. It fails loudly rather than warning: an unreleased lease is a
Store nothing can ever broadcast to again.

**An interrupted broadcast receives `status='failed'`**, `ended_at` set, and a
fixed reason in `notes`. Not `ended` - that would claim an operator stopped it
and would hide the incident. Not a new label - every consumer would need
updating to avoid rendering a blank badge. `BroadcastTarget.play_status` is
deliberately NOT rewritten: whether the speakers were playing when the process
died is knowledge the dead process had and this one does not.

Receivers need no extra STOP. `AudioReceiverPilot._shutdown` closes the FFmpeg
decoder, PCM sink and queue in the `finally` of the session loop, so playback
ends whenever the HQ connection does.

### Still to come

Emergency Stop permission redesign, admin ownership visibility, the
multi-broadcast console UI, the load matrix, and release packaging.

Live RC18 was not modified at any point and remains READY on SQLite.

### Control and privacy for concurrent broadcasts (2026-08-03)

**Emergency Stop is now its own capability.** BROADCASTER inherited it through
the group named after ordinary broadcasting; with concurrent sessions that
means terminating every other operator's broadcast. It is ADMIN/OWNER by
default, reachable per user through the existing override system, and an
explicit DENY still removes it from an ADMIN. No username is special-cased.

**Emergency Stop ALL** snapshots the active session ids before iterating -
ending a session mutates the registry it is read from, so a live iteration
would skip sessions and report "all stopped" with one still on air. Each
session is ended through the same path an ordinary stop uses, so each gets
STOP carrying its own session_id, its own queues closed, its own microphone
socket closed and its own leases released. A failure on one is collected and
returned honestly rather than abandoning the rest.

**broadcast.view_ownership** gates who may learn WHOSE broadcast is using a
Store. `GET /api/broadcast/active` returns three tiers decided server-side:
your own broadcast in full, Scope-filtered `busy_store_ids` for everyone, and
other people's sessions with owner and campaign only for holders of the new
permission. Hidden fields are not serialised at all - a field that reaches the
browser has been disclosed whatever the interface does with it.

Target lists are intersected with Store Scope and `target_store_count` counts
what survived that intersection, so a scoped Admin cannot infer how many
Stores they are not allowed to see.

Normal Stop remains own-session-only for every role including OWNER.

### The multi-broadcast console (2026-08-03)

The UI no longer treats "a broadcast exists" as "I cannot broadcast".
`GET /api/broadcast/active` is the source of truth and its redaction is
respected exactly: `mine` in full, Scope-filtered `busy_store_ids` that say
only that a Store is unavailable, and `sessions` with owner and campaign only
for accounts holding `broadcast.view_ownership`.

A busy Store is marked **IN USE** and cannot be selected. The badge says what,
never who. Without the ownership permission there is no Active Broadcasts
panel at all - not an anonymised one, because the server withholds even the
number of other broadcasts.

The privileged panel is view-only: no Stop beside another operator's
broadcast, asserted by a test that counts buttons. `target_store_count` is
printed as the API returned it, never recomputed, so a scoped Admin cannot
infer how many Stores they may not see.

**EMERGENCY STOP ALL** has its own confirmation naming other operators, and a
partial failure renders a high-visibility error rather than a success. A
STORE_BUSY start is fully refused - no microphone, no socket, no local live
state - and a failed microphone start stops the half-started broadcaster.

Proven with 112 frontend unit tests and 249 Playwright tests, including three
operators live at once in three separate browser contexts, each seeing only
their own broadcast.

### Remaining before release

The load matrix (2/5/10 concurrent sessions across 5-40 Stores), the Store Kit
Settings Password, and release packaging. Live RC18 remains untouched and no
new build has been installed.

### Concurrent broadcast load validation (2026-08-03)

Measured on one Uvicorn worker - the deployment shape - against a throwaway
pilot profile on a free loopback port. Live ports, catalog and credentials
were never touched.

| Scenario | CPU (of 1 core) | RSS peak | Max queue depth | Drops | Stop | Verdict |
|---|---|---|---|---|---|---|
| 5 Stores / 2 sessions | 1.5% | 84 MB | 1 | 0 | 39 ms | GREEN |
| 10 Stores / 2 sessions | 1.2% | 86 MB | 1 | 0 | 62 ms | GREEN |
| 10 Stores / 5 sessions | 1.8% | 87 MB | 1 | 0 | 87 ms | GREEN |
| 20 Stores / 5 sessions | 3.0% | 90 MB | 1 | 0 | 110 ms | GREEN |
| 40 Stores / 5 sessions | 3.7% | 98 MB | 1 | 0 | 123 ms | GREEN |
| 40 Stores / 10 sessions | 5.1% | 99 MB | 1 | 0 | 188 ms | GREEN |
| 40 Stores / 7 uneven (12/8/6/5/4/3/2) | 4.6% | 98 MB | 1 | 0 | 143 ms | GREEN |
| **Soak: 40 Stores / 10 sessions / 10 min** | 3.8% | 100 MB | 1 | 0 | 166 ms | GREEN |

Soak delivered **91,600 chunks with zero drops**. RSS plateaued rather than
climbing: second-half mean 98.66 MB against a first-half mean of 99.31 MB.
Enqueue latency across 22,900 samples: median 0.22 ms, p95 0.68 ms, max
1.83 ms.

**The bounded queue was proven by actually filling it.** At the live ~1 kB
profile a stalled Store never exerts backpressure within a short run, so that
scenario proves nothing; with 64 kB payloads the stalled Store's queue reached
**exactly 24 = capacity and never exceeded it**, dropped 60 chunks, and **no
healthy Store dropped anything**. One slow Store delayed neither its siblings
nor any other session.

Other results: 20 churn cycles (60 sessions) left zero leases and zero
sessions; a contended Store under live streaming produced exactly one winner
and one privacy-safe STORE_BUSY; Emergency Stop All cleared 6 sessions over 18
Stores in 39 ms with leases settled in 14 ms and a safe second call; a real
process restart marked 3 orphaned sessions `failed`, released all 6 leases,
and the Stores were immediately reusable.

**Limitation:** these runs measure routing, queueing and lifecycle with marked
byte payloads, deliberately not audio decoding - identical Opus frames cannot
prove where a chunk came from. Decode correctness remains the job of
local_audio_pilot and the staging smoke. No acoustic claim is made anywhere.

Result artifacts are JSON written to a scratch directory and are not
committed.

---

## Store Kit Settings Password (2026-08-03)

A local password protecting Store Kit CONFIGURATION on a Store PC. It is not
the HQ login, not the Device credential, not an enrolment code, not the HMAC
key, and **not required to receive announcements**.

### Protected (authorization required in the CORE, not just the GUI)

Change Audio Output · Repair · Stop Receiver · Uninstall · Replace Device
Identity · Enrol / re-enrol · Remove stale enrolment.

Replace Device Identity needs the password **and** the typed confirmation
word; neither substitutes for the other.

### Unprotected, deliberately

Status · health · Receiver state · HQ reachability · selected output device ·
current HQ address · redacted diagnostics and export · log folder · task
state · Test Sound · Restart. A signature test asserts no read-only helper
ever grows an `authorization` parameter.

And every runtime path: auto-start, credential load, HQ authentication,
reconnect, heartbeat, PREPARE, playback, STOP.

### Design

`settings-password.json` beside the Receiver's own state, written
tmp-then-replace. **scrypt** (n=2¹⁴, r=8, p=1, 32-byte key, 16-byte
per-install salt), algorithm and parameters recorded for future migration.
Chosen over bcrypt because the Store Kit is PyInstaller-frozen and scrypt is
standard library.

**No DPAPI, deliberately.** DPAPI protects secrets that must be recovered; a
verifier is one-way. `CURRENT_USER` would also bind it to the sealing
identity, and the Store Kit may run as a different Windows user than the
Agent.

### Upgrade

An existing Store has no verifier: the Receiver keeps running, read-only
screens work, and the first protected change directs the operator to set a
password. No default is ever created.

### Corrupt verifier

Settings changes fail closed. The file is never deleted, rewritten, or offered
a reset — that would make corrupting it the way past the password. The
Receiver keeps running and keeps playing.

### Recovery boundary — FINAL

**There is no in-app Forgot or Reset Password**, no master password, no
recovery code, no security question. Recovery is an authorized Windows
Administrator / support procedure: deleting `settings-password.json` only.
That procedure must never delete `config.json`, `receiver-credential.bin` or
logs, and must never auto re-enrol. Afterwards the next protected change
requires establishing a new password.

**Honest limit:** a Windows Administrator already owns the filesystem. This is
app-level protection against ordinary unauthorized Store users, not a boundary
against the machine's administrator.

### Totals

Store Kit 181 + 20 new · backend **2933 passed, 0 failed** · frontend 112 ·
Playwright 249 · build clean.

### Known limitation

GUI journey coverage (A–F) is not written: protection is proven at the core
and by structural tests, but the Tkinter dialog flows themselves are not
driven end to end.

---

## Windows executable branding and Goal A+B acceptance (2026-08-03)

### Every shipped executable carries the website icon

`assets/speaklink.ico`, composed by `tools/build_windows_icon.py` from
`frontend/public` — the three entries already inside the website `favicon.ico`
(16/32/48) reused byte for byte, plus `android-chrome-192x192.png` embedded as
a PNG entry. A test asserts the website entries appear unchanged, so a
lookalike cannot drift in.

| Executable | Spec | Icon verified in the built file |
|---|---|---|
| SpeakLinkHQRuntime.exe | `hq_runtime.spec` | 16, 32, 48, 192 |
| SpeakLinkReceiver.exe | `receiver_agent.spec` | 16, 32, 48, 192 |
| SpeakLinkReceiverBackground.exe | `receiver_agent.spec` | 16, 32, 48, 192 |
| SpeakLinkStoreSetup.exe | `store_setup.spec` | 16, 32, 48, 192 |

**No 256 entry**: producing one would require resampling the 512 PNG, and
there is no image library here. Windows scales the 192 entry. Nothing is
claimed about what Explorer *draws* — shell icon caching was not touched.

The website favicon is unchanged and survives the production build.

### Frozen proofs

`SpeakLinkStoreSetup.exe` bundles `store_kit_settings_password`.
`SpeakLinkReceiver.exe` does **not** — an independent, packaging-level proof
that Receiver playback has no dependency on the Settings Password. The frozen
Receiver runs from an isolated copy: `--version` → 0, audio-device enumeration
works.

### The bug the GUI journeys caught

`authorize_settings` called `datetime.now()` with no module-level import, so
the first time an operator typed a **correct** password it would have raised
`NameError`. Every core test passed because they construct the authorization
directly. Only the operator's real path reached it.

### Totals

Store Kit 223 · journeys 18 · branding 15 · Goal A regression 122 · backend
**2966 passed** · frontend 112 · Playwright 249 · build clean.

`test_concurrent_redemption_enrols_exactly_one_device` failed in the full run
and passes 32/32 in isolation — the known pre-existing flake, reported both
ways.

### Not done in this checkpoint

No release candidate built or installed. Live RC18 untouched.

---

## Release candidate RC19 (2026-08-03) — built, verified, NOT installed

Built from `921543b` on `release/0.1.0-rc19`. Live RC18 remains installed.

### Versions, from the repository's own convention

Two independent lines, both taken from the existing artifact names and build
script defaults — not invented:

| Component | Previous | This candidate |
|---|---|---|
| HQ | `0.1.0-rc18` (installed) | **`0.1.0-rc19`** |
| Store Setup | `1.1.0-rc3` | **`1.1.0-rc4`** |
| Receiver | `1.0.0` (commit-stamped) | `1.0.0-921543b` |

### Packages

| Package | Size | Primary binary SHA-256 |
|---|---|---|
| `SpeakLinkHQ-0.1.0-rc19-921543b-20260803-113250` | 24.6 MB | `SpeakLinkHQRuntime.exe` `88ADE947…D016A9` |
| `SpeakLinkStoreSetup-1.1.0-rc4-921543b-20260803-113423` | 282.0 MB | `SpeakLinkStoreSetup.exe` `61144CA5…6089DE` |
| `SpeakLinkReceiver-1.0.0-921543b-20260803-113331` | 254.1 MB | — |

HQ package verified **31/31**. All four SpeakLink executables carry the icon
(16/32/48/192); `ffmpeg.exe` is third-party and deliberately untouched.

### Migration: additive only

One new table (`broadcast_store_leases`) and two new permission codes
(`broadcast.emergency_stop` relabelled, `broadcast.view_ownership` added).
Nothing is dropped or rewritten; `_reseed_permission_catalog` already rewrites
`role_permissions` from code on every start.

**Upgrade proven on an isolated copy** of an RC18-era database (via the SQLite
backup API, never the live file): 3 users, 45 Stores, 9 Devices and 10
credentials all preserved, leases table created, `integrity_check ok`.
Startup run twice — byte-identical outcome.

The simulation also confirmed a safety behaviour: startup **refuses** when the
Receiver key container is missing while Devices are enrolled, rather than
minting a new one and silently invalidating every credential.

### Rollback boundary

HQ rollback = reinstall the RC18 package. The persistent profile
(`persistent-lan-server`) is never written by the installer, so SQLite data,
logs and configuration survive. `broadcast_store_leases` is additive and
simply goes unused by RC18 — no downgrade migration is required.

Store rollback preserves the Device credential, `config.json`,
`settings-password.json` and logs; the uninstall script already preserves all
of them by default.

### Not done

Not installed anywhere. No Task Scheduler change, no Store deployment, no push.

---

## Active Broadcast Management (feature/active-broadcast-management)

Concurrent broadcasts were working, but a privileged operator could not see
which exact Stores each broadcaster was using, and the cross-user list sat
inside Broadcast Console where 20+ simultaneous broadcasts made the page
unusable.

Supervision moved to its own page at `/active-broadcasts`. Broadcast Console
keeps a single compact badge whose height does not change with the number of
live broadcasts. See `docs/ACTIVE_BROADCAST_MANAGEMENT.md`.

Three new permission codes — `broadcast.active_view`,
`broadcast.view_targets`, `broadcast.stop_any` — join the existing
`broadcast.view_ownership`, `broadcast.stop` and `broadcast.emergency_stop`.
None implies another: `stop_any` reveals no Store names and no owner
identity, and Emergency Stop All is untouched and still independent. OWNER and
ADMIN hold all three by default; BROADCASTER and VIEWER hold none. Per-user
ALLOW/DENY works through the existing rights editor with no SQLite editing.

Also closed here: `GET /api/broadcast/active` was returning
`sessions[].target_store_ids` to any `view_ownership` holder, which made
ownership visibility a back door to target visibility. Exact ids now require
`broadcast.view_targets`.

Additive only — no schema change, no migration. Live RC19 was not touched.

---

## Repo-native HQ runtime (refactor/repo-native-hq-runtime)

HQ no longer installs into `%LOCALAPPDATA%` and no longer starts from a Windows
Scheduled Task. The repository IS the installation: copy the folder, write one
`.env`, run `start.bat` (Windows) or `python tools/speaklink_server.py run`
(anywhere). Live data lives in `<repo>/data`, gitignored.

`tools/speaklink_server.py` owns every decision - configuration, dependency
bootstrap, PID ownership, health checks - so the `.bat` files are thin wrappers
and a POSIX host loses only the double-click. Stop is targeted: the recorded
pid is believed only when the state file also says this repository started it
and the process is still alive.

One Uvicorn worker now serves `/api`, the WebSocket routes and the built React
app on one origin, so production has no CORS. The frontend decides same-origin
at runtime by comparing the page's port with the API port, so one build works
from any hostname. Two-port development keeps its restricted CORS.

### Proven in an isolated clean room

Fresh tree with no data, no `.env`, no venv, no build: refuses to start with no
credentials and creates nothing; then bootstraps to READY, creating 25 tables,
the Receiver credential schema, 33 permission codes including the Active
Broadcast ones, the 44-store canonical catalog and one OWNER with a bcrypt
hash. Second start with a DIFFERENT `ADMIN_USERNAME`/`ADMIN_PASSWORD` changed
nothing - same single Owner, same hash, 44 stores, and the original credentials
still the only ones that work. Copied to a different path with a different
folder name and started there unchanged.

### Two defects found by that test

* `backend/requirements.txt` could not be installed on a fresh machine:
  `emergentintegrations==0.2.0` is not on PyPI and `litellm` was pinned to a
  third-party wheel URL. Neither is imported and neither was in the working
  virtual environment - template scaffold. Removed.
* The Receiver Credential Lifecycle tables were created only by running
  `migrations.py` by hand. Correct when every HQ was migrated from an older
  database; wrong for a new machine that creates its database on first start,
  where enrolment would have failed against tables that never existed. Now run
  at startup, idempotently.

### Broadcast History permanent delete

Fixed first, as its own commit. `broadcast_store_leases` referenced
`broadcast_sessions` and `delete_sessions_permanently` never deleted the lease
rows, so deleting any session that had actually been on air raised
IntegrityError. The browser reported a missing CORS header because Starlette's
ServerErrorMiddleware sits outside CORSMiddleware - the CORS configuration was
never at fault. Unhandled exceptions are now converted to a JSON 500 inside the
CORS layer so a backend fault cannot disguise itself as a transport problem.

### Legacy RC19

Still installed, still running, untouched. The AppData installer, repair,
verify and uninstall scripts plus `tools/hq_runtime.py` are marked LEGACY in
the README and kept as the rollback path. Migration of the live machine is a
separate, later checkpoint.

---

## True User permanent delete (feature/true-user-permanent-delete)

User Management showed accounts as "permanently deleted" that still had Rights,
Scope and Reset Password beside them, and their usernames could never be reused
— `"The username 'admin' is already in use."` for an account deleted months
earlier.

The old design tombstoned the row rather than deleting it, so the UNIQUE index
kept the name. Deletion is now real: the row goes, the username is released,
and the account's permission overrides and Store Scope go with it. Archive is
untouched and still reserves its username, because an archived account can come
back. See `docs/USER_ACCOUNT_DELETION.md`.

### The trap that shaped the design

`hq_users.id` was `INTEGER PRIMARY KEY` with no `AUTOINCREMENT`, so SQLite
reissues `max(id) + 1`. Live broadcast session #2 was started by user id 3
(`broadcaster`, tombstoned) — deleting that row and creating any new account
would have handed the new person id 3 and, with it, that broadcast. Two
defences now: ownership is an immutable snapshot on the broadcast row rather
than a join, and `hq_users` uses `AUTOINCREMENT` so a released id is never
reissued.

Historical references (`broadcast_sessions`, both audit tables,
`receiver_enrollment_codes`) were `NOT NULL`, so the migration relaxes them —
on SQLite via the documented table rebuild, bracketed by `foreign_keys=OFF`
for the DDL only and followed by a `foreign_key_check` that raises rather than
reporting success. Deletion itself runs with foreign keys fully on.

### Proven on a copy of the live database

Two tombstones (`admin` id 1, `broadcaster` id 3) purged; active account
untouched; archived accounts untouched (none present); 4 broadcast sessions,
11 targets, 12 scope-audit rows, 16 enrolment codes and 10 admin-audit rows all
preserved; `integrity_check ok`, `foreign_key_check` clean; idempotent on a
second run. Both usernames recreated successfully as **ids 4 and 5** — never
reusing 1 or 3 — with zero inherited overrides or scope, and no historical
session bound to a live account.

### Not deployed

Development and acceptance only. The live HQ was not restarted and its User
table was not migrated; the visible `admin`/`broadcaster` tombstones are still
present live and will be released by the one-time startup migration at the next
controlled restart checkpoint.

---

## True Store permanent delete (feature/true-store-permanent-delete)

The same defect as the User one, in a second place. An operator permanently
deleted the Store AYUSHK; it vanished from the list, and adding a Store with
code AYUSHK was then refused with `store_code already exists`.
`store_deletion.py` tombstoned the row and said so in its own docstring — the
code was "never handed out to a new Store afterward".

Deletion is now real: the row goes, the Store Code is released, and the
replacement is a different Store. See `docs/STORE_DELETION.md`.

### The trap, again

`stores.id` had no `AUTOINCREMENT`. The live tombstones were ids 58, 59 and 60
with **60 the maximum**, so deleting it and adding any Store would have handed
the replacement id 60 plus every history row pointing there. Fixed the same
way as Users: snapshot-based history plus `AUTOINCREMENT`.

The two SQLite schema operations both features need — `drop_not_null` and
`make_ids_never_reused` — are now in `sqlite_schema_surgery.py` rather than
duplicated. Extracting them exposed a latent bug: the temporary-table rename
used a plain string replace of `CREATE TABLE <name>` and silently missed the
tables SQLAlchemy emits with a **quoted** name, which would have half-applied
a migration.

### Receiver identity

A reused Store Code must not resurrect a Store's Receivers. Credentials are
revoked by *status* (what authentication filters on, not merely a timestamp),
Devices retired and detached with a code snapshot, enrolment codes backdated,
and the `receiver_token` dies with the row.

### Proven on a copy of the live database

3 tombstones purged (58 `AYUSHK`, 59 `test`, 60 `ayushk`), stores 47 → 44,
active 44 unchanged, archived unchanged. Device 5 detached and retired with
snapshot `AYUSHK`; 29 Receiver events and 2 enrolment codes detached with
history intact; audit preserved; idempotent. AYUSHK recreated as **id 61** —
never reusing 58, 59 or 60 — with zero inherited Devices, credentials,
enrolment codes, scopes or leases, its own `receiver_token`, and no historical
row bound to it. `integrity_check ok`, `foreign_key_check` clean.

### Not deployed

Development and acceptance only. The live HQ was not restarted and its Store
table was not migrated.

---

## RBAC permission boundaries — two operator-found bugs

Branch `fix/rbac-permission-boundaries`. Both defects had the same shape: a
permission that resolved correctly and then was not honoured at the boundary
that mattered.

### Bug 1 — Store Management visibility decided Broadcast target visibility

An operator without **View Store Management** (`menu.stores.view`) also lost
the Store list in Broadcast Console.

Root cause: the Console built its target table from `GET /api/stores`, which is
guarded by `menu.stores.view`. The fetch returned 403 and the table rendered
empty with nothing on screen to explain it. One administrative permission was
deciding an operational capability.

Neither obvious fix was acceptable. Widening `menu.stores.view` would have
granted broadcasters the full administrative Store representation; dropping the
guard on `/api/stores` would have done the same thing.

**Separation.** `GET /api/broadcast/target-stores` is a distinct catalog gated
on `menu.broadcast.view` — the permission that already governs the Console —
returning only the seven fields the Console draws:

    id, store_code, store_name, city, region, is_online_store, status

`is_active`, `lifecycle_state`, `created_at` and `last_seen` are deliberately
absent: a Store that is not targetable simply is not in the list, so no caller
has to interpret a lifecycle string and none can learn that an archived Store
exists.

Store Scope is applied by the same `resolve_store_scope` used by
`/api/stores`, including the `None`-vs-empty distinction, so an out-of-scope
Store is absent from the response rather than filtered in the client.
Targeting eligibility is unchanged: active only, archived excluded, deleted
excluded unconditionally.

**Bonus scope leak fixed.** Regions and cities now derive from the Stores in
the same response. `GET /api/stores/meta/regions-cities` applies no scope
filter at all, so a scoped broadcaster's dropdowns previously named regions
they could not broadcast to.

### Bug 2 — "Manage User Rights" did nothing

An OWNER granted an ADMIN **Manage User Rights** and the ADMIN still could not
manage them.

Root cause, in two independent places:

* `GET`/`PUT /api/users/{id}/permissions` required `require_super_admin` — a
  literal "is this account OWNER" test — not `users.permissions.manage`.
* `UserManagement.jsx` gated the Rights button on `myRole === "OWNER"`, so even
  a working endpoint would have been unreachable.

The original reasoning was right about the risk and wrong about the remedy. The
risk is that whoever edits rights can raise their own; the answer is to forbid
raising your own, not to forbid everyone but OWNER.

**Exact permission code:** `users.permissions.manage`. ADMIN does not hold it
by role default — `DEFAULT_ROLE_PERMISSIONS[ADMIN]` subtracts it — so it is
reached only through an explicit per-user ALLOW, which is the intended
delegation path.

**Escalation protections**, all server-side and all refusing the whole batch:

| Guard | Effect |
|---|---|
| `OwnerOverrideRefused` (pre-existing) | an OWNER's rights are never overridden |
| `SelfRightsEditRefused` | nobody edits their own rights |
| `GrantBeyondActorRefused` | nobody grants a permission they do not hold |
| `rbac.may_manage_role` | ADMIN manages BROADCASTER/VIEWER, never ADMIN or OWNER |

Revoking a permission the actor lacks is deliberately still allowed: taking
authority away cannot raise the actor's own.

Store Scope routes keep their OWNER-only reservation — out of scope for this
fix.

**Propagation.** Effective permissions are resolved from the database on every
backend request, so the grant took effect there immediately. Only the React
`AuthContext` Set was stale — fetched once at sign-in — which is what made a
working permission look broken until the operator signed out and back in. It is
now re-fetched when the tab regains focus.

### Tests

18 new backend tests, 9 new frontend unit tests, 8 new Playwright specs,
including the cross-bug regression matrix (User A / B / C). The RBAC endpoint
matrix records both the new route and the intentional permission change.

Totals: backend **3164 passed**, 89 skipped; frontend units **154 passed**;
Playwright **284 passed**; production build OK; `compileall` OK; `pip check`
clean.

### Not deployed

Development and acceptance only. The live HQ was not restarted and no database
migration was required — this change adds no column and no table.

---

## SpeakLink audio volume and mute

Branch `feature/audio-volume-controls`. Two independent controls: HQ microphone
gain in the broadcaster's browser, and per-Store SpeakLink output on each Store
PC.

### The audio path as found

    getUserMedia (mono, EC/NS/AGC)
      -> MediaRecorder(stream)  [webm/opus, 32 kbps, 250 ms chunks]
      -> broadcaster WebSocket
      -> backend AudioFanout, one bounded queue + pump task per Store
      -> Receiver WebSocket
      -> FFmpeg (-f webm -i pipe:0 -f s16le pipe:1)
      -> WindowsPcmSink -> sounddevice RawOutputStream -> selected endpoint

An `AudioContext` already existed but only fed a meter tapped off the raw
microphone. **FFmpeg never opens an audio device** — it decodes to raw PCM on
stdout and the sink owns the endpoint.

### A — HQ microphone

Gain node inserted between the source and a `MediaStreamDestination`, whose
stream MediaRecorder now records. Transport unchanged. Range 0–100 → 0.0–1.0,
default 100, **no boost above unity** (a gain node above 1.0 clips; make-up
gain needs compression and limiting).

Mute is a separate flag, so unmute restores the chosen level. It does not stop
the recorder, microphone track, socket, session or leases. Two meters — input
(pre-gain) and sent (post-gain) — with a `MUTED — STORES HEAR NOTHING` badge,
because the old single pre-gain meter kept moving while muted.

Per browser session, no server call, no stored value: concurrent broadcasters
cannot affect each other's microphone.

### B — Per-Store output: mechanism chosen

**Software gain on the decoded PCM inside `WindowsPcmSink`**, chosen over the
Windows endpoint volume and over FFmpeg filters because:

| | software gain (chosen) | Windows endpoint | FFmpeg filter |
|---|---|---|---|
| Affects other apps (LinkGuard, till) | **no** | yes | no |
| State to restore after a crash | **none** | yes | none |
| Restart to change | **no** | no | yes |

Because the system mixer is never read or written, **there is nothing to save
and nothing to restore** — a crashed Receiver leaves the machine as it found
it. A test asserts no `pycaw`/`IAudioEndpointVolume`/`waveOutSetVolume` symbol
appears in the Receiver source. It scales SpeakLink's own audio only, which is
exactly the claim being made.

100% passes the buffer through untouched; 0% emits silence of the same length
(an empty buffer would underrun and sound like a fault); samples are clamped
after scaling because rounding can produce 32768 and wrap to a click.

### Protocol

Downstream `set_audio_control {session_id, command_id, volume_percent, muted}`
— whole state, not a delta, which is what makes dropping a stale command safe.
Back: `audio_control` carrying **requested and applied separately** plus
`result` ∈ `applied | unsupported | failed`, `output_device`, `error_code`.
Applied values are read back from the sink, not echoed.

`audio_control` changes no readiness or playback status: a muted Store is still
`AUDIO_RECEIVING`. Capabilities ride on `receiver_ready`; **absence means an
older Receiver**, HQ then sends no command and shows the Store as unsupported.
Old Receivers keep working and are not re-enrolled.

### Permission

`store_audio.control` ("Control Store Output Volume"), BROADCASTER by default.
Session **ownership** is enforced on top, so it grants nothing over another
operator's broadcast; `broadcast.stop_any` and `broadcast.active_view` do
**not** reach it. Store Scope enforced server-side. Active Broadcast Management
stays read-only for now — editing remains in the owning Console.

Nothing in this protocol carries the Store Settings Password, a verifier or a
Device credential; a Playwright test asserts it on the wire.

### Persistence

Zero database writes per slider movement, asserted by a full-table row census
across 60 commands. No migration; no new table. A persistent per-Store
announcement default is documented as a follow-up.

### Tests

Backend **3234 passed**, 89 skipped. Frontend units **183 passed**. Playwright
**295 passed**. Production build, `compileall`, `pip check` all clean.

New: 28 registry, 15 API, 25 Receiver/protocol, 17 HQ mic, 12 store-control
hook, 11 Playwright.

### Not proven, and not deployed

No physical Store pilot has been run. Mock tests prove logic, protocol and
ordering; they prove nothing about amplifier loudness, Bluetooth routing or
audibility. The Store Kit has **not** been rebuilt, the live HQ was not
restarted, and no Store was deployed to. `SPEAKER_VERIFIED` remains reserved
for acoustic evidence and is not set by any of this.

---

## Audio control: load acceptance and Store Kit 1.3.0

Branch `feature/audio-volume-controls`, commit `6ff3fb2`.

### Harness

Reuses the existing synthetic-Receiver infrastructure rather than adding a
second framework: `tools/load_test_receivers.py`'s `SyntheticReceiver`
(extended to answer `set_audio_control`), the pilot's own loopback backend
bootstrap and `_pilot_environment`, and the existing CPU/RAM and per-Store
audio-queue sampling. New driver: `tools/audio_control_load.py`. Report:
`test_reports/audio-control-load-20260805.json`.

The estate is deliberately mixed — from 10 Stores upward it contains one older
Receiver reporting no capability, one whose output device refuses, and one that
answers 750 ms late.

### Results

| Stores | Requested | Transmitted | ACKs | ACK p95 | Max queue | Dropped | CPU s | RSS MB | Latest wins |
|---|---|---|---|---|---|---|---|---|---|
| 5  | 25  | 26  | 26  | 766 ms | 1 / 24 | 0 | 0.11 | 84.0 | 5/5 |
| 10 | 50  | 46  | 46  | 758 ms | 1 / 24 | 0 | 0.19 | 86.0 | 8/8 |
| 20 | 100 | 96  | 96  | 0.18 ms | 1 / 24 | 0 | 0.28 | 89.2 | 18/18 |
| 40 | 200 | 196 | 196 | 0.16 ms | 1 / 24 | 0 | 0.89 | 95.2 | 38/38 |

`transmitted < requested` is correct: the Store modelling an older Receiver is
never sent a command at all. ACK p95 at 5 and 10 Stores is dominated by the one
deliberately slow Store — at 20 and 40 it is a small fraction of the sample, so
p95 falls to the ~0.1 ms every other Store answers in. **That gap is the
slow-Store isolation result**: minimum ACK latency stayed at 0.07–0.08 ms while
one Store took ~760 ms.

Every queue stayed within capacity, `enqueued == delivered` at all four scales,
and all 21 audio chunks reached every Store — audio streaming was unharmed by
control traffic moving underneath it.

### Ordering

`replayed_stale_ack_accepted = 0` at every scale. One Store replays an
acknowledgement for an earlier command carrying 1%; the backend discards it and
the newer applied value stands. `stores_left_at_a_stale_value = 0` throughout.

### Failure cases (mid-broadcast)

Store outside the session **404**; unknown/finished session **409**; volume
outside the contract **422**; command after the broadcast ended **409**. No
crash, no cross-Store corruption, no broadcast stopped by a failed command.

### Concurrent isolation

Alice and Bob on disjoint halves. Alice's Store reached 70 and Bob's stayed
muted; every other Store in both sessions remained at the default. Each was
refused **403** when naming the other's session id exactly.

### Store Kit

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.3.0-6ff3fb2-20260805-073937.zip` |
| Size | 128,900,012 bytes (122.9 MB), 1061 entries |
| SHA-256 | `d0f811941f817dbdc511de870e7a317f021ef30b8c775b2231a403f47b4430f2` |
| Receiver | 1.1.0 (was 1.0.0) · Kit 1.3.0 (was 1.2.0) |
| Source commit | `6ff3fb2`, tree clean |
| Rollback kit | `SpeakLink-Store-Kit-1.2.0-1f4727c-20260803-085407.zip` **retained, untouched** |

Builder audit passed: no credential/key/database/log/developer path, SpeakLink
icon on every executable, background Receiver windowless, all three executables
present. Independent scan found no `.env`, `.db`, `.sqlite`, `device-credential`,
`settings-password.json`, `node_modules`, `.venv`, `.git`, `.log`, `.pem` or
`.key` entry.

**Capability verified by extracting the PyInstaller PYZ**, not by grepping the
EXE: a raw string search finds nothing because the archive is compressed —
including long-standing strings like `receiver_ready`, which is how that was
established as a measurement artefact rather than missing code. The bundled
`tools.audio_receiver_pilot` bytecode contains `set_audio_control`,
`_on_set_audio_control`, `_scaled`, `effective_percent`, `output_volume`,
`output_mute` and `capabilities`; `audio_protocol` contains both the builder
and the parser.

### Test totals

Backend **3233 passed, 90 skipped**. Frontend not re-run — no frontend source
was touched by this checkpoint.

One skip is new and deliberate: `test_a_copy_of_the_pilot_database_migrates_cleanly`
now skips when the local pilot database is already migrated. It had asserted the
pilot database carried no phase-one tables, which was never a claim about the
migration tool — it held only while the machine carried a database from a
release predating those tables, and `prepare()` now creates them itself.

### Not done, deliberately

Live HQ not restarted or deployed. No Store Receiver installed, no Store
re-enrolled, no credential changed. No physical amplifier test. Nothing here is
acoustic evidence and `SPEAKER_VERIFIED` remains unset.

---

## Live HQ deployment — audio volume controls (2026-08-05)

Source deployed: `feature/audio-volume-controls` @ `8c874d8`, tree clean and
equal to origin.

### What this deployment actually was

The live HQ runs **repo-native from this working tree**, and the operator had
already restarted it at 13:55 from the accepted commit — so the running build
already served `/api/broadcast/target-stores` and
`/api/broadcast/sessions/{id}/audio-control` before this checkpoint began. The
frontend was rebuilt from HEAD and produced the **identical content-hashed
bundle** `main.e5da7b46.js`, which is proof the served UI already corresponded
to `8c874d8` rather than an inference from timestamps.

The controlled stop/start was performed anyway, to prove the startup migrations
run cleanly against the live database and that Receiver credentials survive a
restart.

### A measurement error worth recording

The first baseline was read over a `mode=ro` SQLite connection, which could not
attach the `-shm` and therefore **silently omitted 4 MB of committed WAL**. It
under-reported broadcast_sessions/targets/leases as 6 (actually 9), system_logs
as 23 (actually 34) and receiver_events as 879 (actually 898) — and the backup
taken through that connection was incomplete for the same reason. A second,
WAL-complete backup was taken and verified before any further step. The
incomplete file is retained but superseded.

| | |
|---|---|
| Rollback backup | `_live-hq-deployment-backups/speaklink-audio-deploy-COMPLETE-20260805-140500.db` |
| SHA-256 | `8449688598bb0918bd38824284bc2242b54e787163c3031c7d714c83b95eee33` |
| Verified | `integrity_check ok`, `foreign_key_check` 0 rows, 9 sessions, 898 events |
| Superseded (WAL-incomplete) | `speaklink-before-audio-deploy-20260805-140000.db` |
| Receiver key rollback | `receiver-hmac-keys-before-audio-deploy-20260805.bin`, 470 bytes, sha256 `748a99f2…` |

### Result

Stop was clean (pid 7468, port released, both PIDs gone). Start produced pid
19616 with a single worker (`--workers 1`), `SpeakLink startup complete`,
and the log line *"Receiver key container present … reused unchanged"*.

**Every one of 16 tracked tables is byte-identical before and after** — no
migration had anything to do, because the User and Store tombstone migrations
had already run on 2026-08-03 and the live catalog already contained
`store_audio.control`. `integrity_check ok`, `foreign_key_check` 0 rows.

`store_audio.control` present with roles OWNER / ADMIN / BROADCASTER, matching
`DEFAULT_ROLE_PERMISSIONS` in the accepted source. Permission catalog unchanged
at 34 / 74.

UI `/` 200, `/console` 200 (direct React route), `/api/` 200, unknown `/api`
path 404 with a JSON body. No port 3000 dependency.

### Receiver continuity

One Store Receiver (`192.168.4.171`) reconnected after the restart and held an
open WebSocket for the whole observation. The server closes an idle Receiver
socket with code 4408 after 30 seconds, so a socket open for 6+ minutes is
positive evidence the Receiver is authenticated and heartbeating. **No
re-enrolment was performed or required.**

It did **not** register as the Store's primary: no `connected` event was
persisted and no Store transitioned to online, so READY is *not* claimed.
`stores.status='online'` for BP is stale pre-restart data from 08:32, not
evidence. Which connection path it took could not be determined without
authenticated API access, and no account was altered to obtain it.

### Not performed, deliberately

No on-air test broadcast: no Store is confirmed READY and no operator
authorisation for an audible announcement was given, so P15/P16/P17 live checks
are outstanding. No test account was created or modified for the RBAC checks —
the estate has exactly two real accounts and neither is disposable. No live
history was deleted. No Store Kit installed.

Stability: API 200 and worker alive across six samples over 2.5 minutes, no new
errors. Rollback not required.

---

## Live deployment — Receiver Device runtime disconnect (2026-08-05)

Deployed `fix/receiver-delete-runtime-disconnect` @ `6861b27` to the live
repo-native HQ. Clean stop (pid 14284, port released), start pid 12896 with one
worker, `SpeakLink startup complete`, Receiver key *"reused unchanged"*.

| | |
|---|---|
| Rollback backup | `_live-hq-deployment-backups/speaklink-before-runtime-disconnect-20260805-154400.db` |
| SHA-256 | `34f40716066cf4785bee6532515cbba85874bc681738f47d5dbb117489dacd05` |
| Verified | `integrity_check ok`, `foreign_key_check` 0 rows, 11 sessions / 10 devices / 11 credentials |
| Receiver key | 470 bytes, sha256 `748a99f2…`, unchanged and not rotated |

### A live broadcast was interrupted — operator impact

The P1 safety check ran at 15:43:30 and correctly reported zero live
broadcasts. Session 12 was created at **15:43:54**, twenty-four seconds later,
and reached `playback_confirmed` on BP. The stop was issued moments after, so
**an announcement that was audibly playing was cut off.**

The gate was point-in-time with no re-check immediately before the stop. Any
future restart must re-assert the live-broadcast check within seconds of
issuing the stop, or hold a guard that refuses the stop outright. Restart
reconciliation behaved correctly: session 12 was closed as `failed` with the
honest note *"Interrupted: HQ restarted while this broadcast was live"*, and
its Store lease was released.

### The fix is proven live, on real hardware

The operator exercised it on BP immediately after deployment:

    15:47:37  BP connects (Device 11)
    15:47:44  operator permanently deletes Device 11
    15:47:44  "Receiver Device cf20de99-… disconnected from runtime
               after permanent delete"
    15:47:44+ 192.168.4.171 "WebSocket /api/ws/receiver" 403

The socket was closed by the new code at the moment of deletion, and the
Receiver's reconnect with the revoked credential was **rejected 403**. Against
the defect this replaces — Device 6 deleted at 14:17:36, still reporting
`playback_confirmed` at 14:18:26 — the behaviour is now correct.

### Receiver continuity across the restart: NOT demonstrated

BP did not reconnect during the 25 seconds between startup and the operator
permanently deleting Device 10 at 15:44:29, so restart continuity on the
pre-existing credential was never observed. It cannot now be tested on that
Device: Devices 10 and 11 were both deleted by the operator and BP currently
has **no active Device** and one unredeemed enrolment code. Those deletions and
re-enrolments were operator actions (`actor_user_id=2`), recorded in
`device_deletion_events`; this checkpoint changed no credential and re-enrolled
nothing.

### Result

Full backend **3246 passed**, 90 skipped — the known order-dependent flake
`test_concurrent_redemption_enrols_exactly_one_device` did not recur. Targeted
Receiver/device/credential/broadcast selection: 2019 passed.

`integrity_check ok`, `foreign_key_check` 0 rows, no live sessions, no
unreleased leases. UI 200, `/console` 200, `/api/` 200. Rollback not required.

Store Kit 1.3.0 **not** installed. Dynamic live target work **not** started.
Legacy Store-token authentication **unchanged**.

---

## HQ Store volume now controls the Windows endpoint master

Branch `feature/windows-master-volume-control`, commits `357dc22` and `f363b1f`.

### The semantic change

**HQ per-Store Volume = the Store's Windows endpoint MASTER volume.**
**HQ per-Store Mute = the Store's Windows endpoint MASTER mute.**
HQ Microphone volume is unchanged: still a browser-local Web Audio `GainNode`.

This deliberately reverses the earlier design. Scaling SpeakLink's own PCM meant
nothing else on the machine moved and a crash left nothing behind — but a Store
user who muted Windows silenced every announcement while HQ reported
"Applied 100%".

### Accepted consequences

* **Other applications on the same endpoint are affected.** Chrome, LinkGuard,
  till audio — anything sharing that output gets louder, quieter or muted with
  SpeakLink. No per-app isolation in this release.
* The change is **persistent Windows state**, so it is captured before the first
  mutation, restored on every path that ends a broadcast, and recoverable after
  a crash.
* It is **not the amplifier**. Nothing here touches a physical knob, and no
  software result is acoustic evidence. `SPEAKER_VERIFIED` remains unset.

### Identity — the dangerous part

A PortAudio `index:N` is **not** an endpoint identity: the same output appears
under several host APIs, and indices renumber when hardware changes. Mutation
therefore requires the stable MMDevice endpoint id, resolved **once** while the
technician is selecting the output. An ambiguous name match is **refused**, not
resolved — a wrong answer there is permanent, silent, and points at real
hardware. Existing Stores have no id and report the control unsupported until
the output is re-selected.

### No double attenuation

The PCM path stays at unity. Applying the same percentage in both places would
make HQ 50% produce 25% of the signal. The scaling code is kept — it is the only
way to silence SpeakLink without touching a shared endpoint — but nothing moves
it today, and a test pins that.

### A replaced test, not a deleted one

`test_the_windows_endpoint_volume_is_never_touched` encoded the old property.
After this change it still **passed**, because the Core Audio calls simply moved
one module across — it would have gone on reassuring us about something no
longer true. Replaced by two narrower, accurate tests.

### Packaging

`pycaw==20240210` and `comtypes==1.4.6`, pinned and bundled. Bundling is not
proof: comtypes generates COM wrappers at runtime and often fails when frozen,
so `diagnose` probes Core Audio read-only. The **frozen executable reports
`core audio: reachable (3 active endpoint(s))`** — that is the packaging proof.

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.5.0-f363b1f-20260805-135058.zip` |
| SHA-256 | `68994ee2c651893f6649149b20fa64ef3bb207cd6d973a31e6e0b84a97c36814` |
| Size | 129,995,436 bytes, 1065 entries |
| Receiver **1.2.0** · Kit **1.5.0** | 1.2.0/1.3.0/1.4.0/1.4.1 all retained |

Backend **3314 passed**, 90 skipped. No forbidden content in the package.

### Not done

No live HQ deployment, no Store touched, no second Store. The one-Store
uninstall/reinstall and physical acceptance are outstanding and must use this
kit, not 1.4.1.

---

## Output-control state model, and master-volume load acceptance

Commits `84cda53` (+ docs). Store Kit **1.5.1**.

### Four states, not two

"Not supported by this Receiver" covered two different situations and only one
was this software's fault. The Receiver now reports which, because only it
knows — `capabilities.output_control_status`:

| status | HQ shows | remedy |
|---|---|---|
| `unknown` | Not supported by this Receiver | new Store Kit |
| `needs_output_selection` | Re-select the Store audio output | 30 s in Store Setup |
| `unavailable` | Store audio output unavailable | check the output device |
| `ready` | working slider and mute | — |

Defaults to `unknown`, so an older Receiver — which omits the capabilities
block entirely — keeps reading as genuinely unsupported rather than inheriting
a friendlier explanation it never earned. HQ never infers the reason itself.

### Load: 5 / 10 / 20 / 40 Stores, mocked endpoints

| Stores | Req | Sent | ACK | ACK p95 | Max queue | Dropped | CPU s | RSS MB | Latest wins | Prepared | Restored |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 5 | 25 | 26 | 26 | 764 ms | 1 / 24 | 0 | 0.28 | 84.3 | 5/5 | 5 | 5 |
| 10 | 50 | 41 | 41 | 759 ms | 1 / 24 | 0 | 0.34 | 86.3 | 7/8 | 8 | 8 |
| 20 | 100 | 91 | 91 | 755 ms | 1 / 24 | 0 | 0.58 | 89.6 | 17/18 | 18 | 18 |
| 40 | 200 | 191 | 191 | 0.14 ms | 1 / 24 | 0 | 1.67 | 95.7 | 37/38 | 38 | 38 |

`Sent < Req` is correct: the old-Receiver Store and the unselected-output Store
are never sent a command. The p95 fall at 40 Stores is the slow Store becoming
a smaller fraction of the sample — minimum ACK stayed at 0.07 ms while it took
~760 ms, which **is** the isolation result.

**Restoration held at every scale**: `endpoints_left_changed` empty, and every
Store finished at its original volume *and* original mute.
**`pcm_gain_left_at_unity` true at every scale** — no double attenuation.
Replayed stale ACKs rejected everywhere. Concurrent broadcasts isolated, each
operator refused 403 on the other's session.

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.5.1-84cda53-20260805-143704.zip` |
| SHA-256 | `71791e54b4fca424b153e9b37fce5bf635800e50668144ac9640d2c5a7bbede7` |
| Receiver **1.2.1** · Kit **1.5.1** | 1.2.0–1.5.0 retained |

Backend **3313 passed** (1 pre-existing order-dependent flake, reproduced on an
untouched base commit), frontend **211**, Playwright **300**, build OK.

### Live HQ restart IS required

Backend and frontend both changed. Not deployed here.

---

## Two-way Windows endpoint volume/mute synchronisation

Store-local changes now reach HQ. Store Kit **1.5.2**.

### The two values, and the line between them

HQ could set a Store's master volume but never see it. Somebody moving the
Windows slider at the till changed the shop and the Console went on displaying
its own last command — confidently wrong about the one number the operator was
reading.

There are now two distinct facts, and conflating them was the whole risk:

| | source | used for |
|---|---|---|
| **ORIGINAL** pre-broadcast state | captured once by PREPARE | **restoration — sole authority** |
| **CURRENT** endpoint state | live telemetry | what HQ displays |

**Option A restoration semantics are unchanged.** Live telemetry never writes
the snapshot. Original 10% muted, HQ sets 80, the till moves it to 30 and
unmutes — stop still puts back **10% muted**. That is asserted in
`test_live_telemetry_never_mutates_the_restoration_snapshot`, and again at
every load scale as `restored_to_original_not_live`.

### How a change gets from the till to HQ

`tools/windows_endpoint_observer.py` registers an `IAudioEndpointVolumeCallback`
against the endpoint **after** the snapshot is taken, and is detached **first**
on restore — a callback left attached would report a restoration into the next
broadcast as though a person had moved the slider.

It keeps **one slot, never a queue**. A drag emits a notification per step;
only where it stopped is true by the time HQ draws it, and a queue would let
one noisy Store grow without bound on a socket that is also carrying audio.
Feasibility was proven live before any of this was written: registered against
the real default endpoint, changed to 33% then 55%, the callback fired twice,
and the machine was put back to its original 47%.

`endpoint_state` is a new acknowledgement type: `state_sequence`,
`volume_percent`, `muted`. It carries **no command id** — nobody at HQ asked
for it — and **no credential, endpoint id or device secret**. It changes
**no** playback or readiness status: a quiet shop is not a broken shop.
Nothing is written to SQLite; `actual_*` is runtime-only, beside `requested_*`
and `applied_*`.

### No feedback loop

Incoming telemetry updates **displayed state only**. It must never issue a
command, or HQ would hear its own volume back and answer it for ever. Four
frontend regression tests hold that line, including
`incoming actual-state telemetry never issues a command` and its counterpart
proving an operator gesture still sends exactly one.

### Load: telemetry churn at 5 / 10 / 20 / 40 Stores

| | 5 | 10 | 20 | 40 |
|---|---|---|---|---|
| Local changes generated | 48 | 49 | 49 | 49 |
| Frames transmitted | 8 | 10 | 10 | 10 |
| Noisy Store: generated → sent | 43 → 4 | 43 → 4 | 43 → 4 | 43 → 4 |
| HQ actual matched the Store | yes | yes | yes | yes |
| Silent Stores' frames | 0 | 0 | 0 | 0 |
| Max queue / capacity | 1 / 24 | 1 / 24 | 1 / 24 | 1 / 24 |
| Audio chunks dropped | 0 | 0 | 0 | 0 |
| Restored to original, not live | yes | yes | yes | yes |

**43 local changes became 4 frames** and HQ still ended up on exactly the value
the drag stopped at (23%). Stores with no controllable endpoint — the old
build, the unselected output, the failing device — transmitted **nothing**;
HQ learns why from the capability status, not from silence. The stale-telemetry
Store replayed a reading claiming 1% with a `state_sequence` that went
backwards; HQ kept the newer 58 (`hq_ignored_stale_telemetry`). Concurrent
broadcasts stayed isolated with telemetry flowing: Alice's Store read 18, Bob's
64, no other Store in either session acquired a reading at all.

Telemetry volume does **not** scale with estate size here — the churn is
confined to fixed Store roles by design, so these figures are a coalescing and
isolation result, not a per-Store telemetry-rate result.

### Regression

Backend **3332 passed, 0 failed** on a second full run, frontend **222**,
Playwright **300**, production build OK, `compileall` clean, `pip check` clean,
secret scan clean.

The first full run showed two failures and neither survived investigation.
`test_concurrent_redemption_enrols_exactly_one_device` is a **pre-existing
flake**: it fails intermittently when run entirely **alone** (1 of 3
consecutive runs), a SQLite lock under its own 6-thread contention.
`test_the_primary_button_is_the_replacement` passed in isolation, pairwise and
on both later full runs. A run with only the new test file removed passed 3314,
and the second unmodified full run passed 3332 — so the change is not the
cause.

### Store Kit 1.5.3

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.5.3-117d409-20260805-164449.zip` |
| SHA-256 | `e0e9c42ac1507bf6691a234db2b9e256db7de40beaa9d724e077600d8c5fa362` |
| Receiver **1.2.3** · Kit **1.5.3** | 1.2.0–1.5.2 retained, none overwritten |

Verified **inside the shipped executable**, not on disk: the PYZ was extracted
and matched by substring, giving `pycaw` (14 modules), `comtypes` (44),
`tools.windows_endpoint_observer`, `windows_endpoint_volume`,
`windows_endpoint_restore`, and the `_endpoint_state_loop` and `endpoint_state`
symbols inside `tools.audio_receiver_pilot`.

The frozen Receiver was then asked directly, and answered:

```
windows master control:
  core audio     : reachable (3 active endpoint(s))
  change reports : yes - endpoint change notifications supported
```

That second line is new, and is the point: packaging `pycaw` proves nothing
about whether `comtypes` can still build a COM callback once frozen. The probe
registers and immediately unregisters, changing no volume on the machine.

### Not done

No live HQ deployment. No Store touched, BP included. No second Store. No
physical acceptance — **no software state equals SPEAKER_VERIFIED**.

Any future HQ restart must again check LIVE/PENDING == 0 **and** unreleased
leases == 0 in the same controlled operation immediately before the stop.

---

## Live HQ deployment — two-way volume sync (2026-08-06)

Source deployed: `feature/windows-volume-two-way-sync` @ `3d452ad`, tree clean
and equal to origin.

### Gate and stop, in one operation

The gate read 0 non-terminal sessions and 0 unreleased leases, and the stop was
issued **1 ms later from the same process**. The query counts sessions whose
status is **not** in the known-terminal set rather than matching `LIVE`/`PENDING`
literally — this database stores them lowercase (`ended`, `failed`,
`emergency_stopped`), so a literal match would have waved a live broadcast
through. The safe direction is to require a known-finished status.

| | |
|---|---|
| Rollback backup | `_live-hq-deployment-backups/speaklink-before-two-way-sync-20260806-114500.db` |
| Size / SHA-256 | 692,224 bytes · `a44ad5f916b6e5c62137afc81a029661a561442d256e33fd779b5c8b24737734` |
| Verified | `integrity_check ok`, `foreign_key_check` 0 rows, 25 tables |
| Receiver key | `748a99f2…`, 470 bytes — unchanged before and after |

Taken with SQLite's online-backup API, not a file copy: the live database
carried a **4.1 MB WAL**, and the backup contains 1009 `receiver_events` where
the stale `backend/speaklink_live.db` on the default path holds a July snapshot
of 13 demo Stores. The live database is `data/speaklink.db` (44 Stores), which
the launcher resolves — worth recording, because the default path looks
plausible and is wrong.

### Result

Stop clean (pid 12500, port 8000 released, both PIDs gone). Start produced pid
10452 with `--workers 1`, `Receiver key container present … reused unchanged`,
and `SpeakLink startup complete`.

**All 25 tables identical before and after**, no table added or lost, no
migration pending (`schema_migrations` 1 → 1). `integrity_check ok`,
`foreign_key_check` 0 rows.

`/` 200, `/console` 200, `/api/` 200 on both the LAN address and localhost.

The frontend was rebuilt from HEAD and produced the **identical content-hashed
bundle** `main.94afa172.js`. The bundle was then fetched **from the running
server** and its SHA-256 matched the file on disk byte for byte
(`d242e41a…`), carrying `actual_volume_percent`, `actual_muted`, `Currently `
and `Re-select the Store audio output`. `actual_state_sequence` is absent from
the bundle and should be: it is server-side ordering, and the frontend never
names it.

The live server's own OpenAPI declares `StoreAudioStateOut` with
`actual_volume_percent`, `actual_muted`, `actual_state_sequence` and
`actual_state_updated_at` — fields that exist only in this source, so the new
backend is genuinely the one answering.

### BP was already offline before this deployment

BP's Receiver did **not** reconnect, and the deployment did not cause that.
Its last connection ended at **2026-08-06 04:13:41 UTC**, about 1 h 55 m before
the stop at 06:08:40 UTC. Two independent observations agree: no
`192.168.4.171` socket existed at the pre-stop baseline, and `receiver_events`
1008/1009 record the connect and disconnect. `stores.status` for BP reads
`offline`.

BP's identity is untouched and intact: Device **13** `active`, credential
**14** `active`, `revoked_at` and `replaced_at` both null. Nothing was
re-enrolled, deleted, revoked or replaced.

So the "BP reconnects with its existing credential" acceptance item is
**unproven**, not failed. Proving it needs the Store PC, and this checkpoint is
forbidden to touch it.

### Not done

BP not touched. No second Store. Store Kit 1.5.3 **not** installed. No
broadcast started. No dynamic Store/Zone targeting.

No rollback is indicated — HQ is healthy and serving the accepted build.

---

## Master Volume panel, and Broadcast recording

Branch `feature/master-volume-panel-recording-history`.

### Master Volume is independent of the Broadcast Console

Its own navigation entry and its own page. Setting a shop's volume happens
before opening, after a complaint, or when somebody notices a Store has been
left muted since Friday - almost never with an announcement on air. Reaching a
slider used to require starting a broadcast, which made the most routine audio
task depend on the least routine one.

**Which Stores appear:** every Store with an installed, **active, primary**
Receiver Device, online or offline. Hiding an offline Store would hide exactly
the ones worth looking at. Retired, disabled and deleted Devices are excluded -
a Store whose Receiver was replaced has one current machine, and commanding a
historical Device id would target something that is not in the shop.

`Zone` is the existing `region` field surfaced under the name operators use.
No new column was invented.

### The three things the panel must never conflate

| | source | wording |
|---|---|---|
| what a Store **IS** | live reading, only while connected | "Currently 65%" |
| what it **WAS** | memory, once offline | "Last known", controls disabled |
| what we **WANT** | pending change | "Pending — will apply when Receiver reconnects" |

A Store that has never reported shows an em dash, not an invented zero.
**"Currently N%" may only ever come from the Receiver's readback** - a command
that has been sent is not a fact about a mixer, and echoing the request back
would make a Store that is ignoring HQ look obedient.

### Offline: pending on reconnect, never applied

Persisted, because outliving a disconnection is the whole point. `store_id` is
the PRIMARY KEY, so **latest wins is a property of the table** rather than a
rule the application remembers - 30 then 50 then 70 leaves one row saying 70,
and no command queue exists to replay at a shop when it wakes up.

The row carries Store, Device, requested state, actor and timestamp. No
credential, token, Settings Password or JWT.

On reconnect the change is applied only after the Device has authenticated,
been confirmed as this Store's current primary, and **reported what its mixer
actually is**. It is refused if the Device was replaced, and left alone if a
broadcast now owns the Store. The row is cleared **only on a confirmed apply**:
clearing on attempt would make failure indistinguishable from success.

### Single writer, always

There is never a second command channel. When a broadcast owns a Store the
panel routes through **that broadcast's own authority** if the caller is the
operator running it, and otherwise refuses with `CONTROLLED_BY_BROADCAST`.

### Observer lifecycle

Observation now runs whenever the Receiver is **connected with a stable
endpoint configured**, not between PREPARE and STOP. Readings carry a session
id only when a broadcast is running. On reconnect the Receiver reads and
reports its actual state before anything is applied - the observer reports only
*changes*, so a Store returning at the volume it left at would otherwise say
nothing and look stale.

Only the saved MMDevice endpoint is ever observed or controlled. No default
endpoint, no `index:N`, no friendly-name guess.

**Restoration is unchanged and remains authoritative.** Original 25% muted,
broadcast sets 80, someone at the till moves it to 40, HQ shows 40 - and STOP
still restores **25% muted**. Restoration no longer stops the observer, so the
restored state is reported back to the panel afterwards.

A gap the load harness found: capabilities were only reported in
`receiver_ready`, which happens at PREPARE, so a Store merely connected all day
sat at `unknown` and had its slider refused. A reading is now taken as evidence
the endpoint is readable - **promoted from `unknown` only**, so an explicit
`needs_output_selection` or `unavailable` is never overridden by inference.

### Broadcast recording

Records the bytes on the broadcaster's microphone socket: HQ's **outgoing**
audio, after the accepted gain and mute path, so muting the HQ microphone
records silence. Store ambient sound and LinkGuard cannot appear because they
never travel on that socket - a test asserts the recorder has exactly one call
site and that it is the broadcaster handler, because an absent wire is a
stronger guarantee than a filter.

**A recording never delays an announcement.** The fan-out only `put_nowait`s
into a bounded queue (240 chunks, ~1 minute) and a background task does the
file work. A full queue drops the chunk and the recording becomes PARTIAL.

Files live in `data/recordings/`, named `broadcast-000123.webm` - session id
and nothing else. Gitignored explicitly. **No audio in SQLite**; the row holds
status, codec, size, duration, counts and timestamps.

Written to `.part`, validated with ffprobe, then atomically renamed. Five
truthful states: `recording`, `available`, `partial`, `failed`, `missing`. On
restart an unfinished `.part` is **never** promoted to AVAILABLE.

Playback is an authenticated route with byte-range support, gated on the same
permission and Store Scope as Broadcast History. The recordings folder is never
a static mount, and no path or filename reaches the browser. Deleting history
removes the audio **first**, so a failure can at worst leave a row reported as
MISSING rather than an orphan file nothing points at.

### Load results

**Idle, no broadcast anywhere** (`tools/master_volume_load.py`):

| | 5 | 10 | 20 | 40 |
|---|---|---|---|---|
| Panel rows | 5 | 10 | 20 | 40 |
| Local changes generated | 47 | 57 | 77 | 117 |
| Frames transmitted | 8 | 18 | 38 | 78 |
| Noisy Store: generated → sent | 42 → 3 | 42 → 3 | 42 → 3 | 42 → 3 |
| HQ matched the Store | yes | yes | yes | yes |
| Unconfigured Store's frames | 0 | 0 | 0 | 0 |
| Commands accepted / refused | 3 / 1 | 8 / 1 | 18 / 1 | 38 / 1 |
| CPU s · RSS MB | 0.08 · 83.6 | 0.11 · 84.1 | 0.22 · 87.0 | 0.34 · 91.5 |

The single refusal at every scale is the Store with no selected output - a 409,
which is correct. The offline Store was listed, marked stale, and took a
pending change of 70 after 30 and 70 were sent (latest wins).

**Broadcast with recording enabled** (`tools/audio_control_load.py`):

| | 5 | 10 | 20 | 40 |
|---|---|---|---|---|
| Recording status | available | available | available | available |
| Codec · duration | opus · 4.008 s | opus · 4.008 s | opus · 4.008 s | opus · 4.008 s |
| Chunks written / dropped | 21 / 0 | 21 / 0 | 21 / 0 | 21 / 0 |
| Audio chunks dropped | 0 | 0 | 0 | 0 |
| Max audio queue / capacity | 1 / 24 | 1 / 24 | 1 / 24 | 1 / 24 |
| Restored to original | yes | yes | yes | yes |
| CPU s · RSS MB | 0.25 · 85.7 | 0.47 · 87.1 | 0.73 · 90.1 | 1.3 · 96.5 |

### Regression

Backend **3430 passed, 0 failed**, frontend **260**, Playwright **300**,
production build OK, `compileall` clean, `pip check` clean, `git diff --check`
clean, secret scan clean.

Three real defects were found by the suite and fixed rather than worked around:

* Adding the change-notification line to Receiver diagnostics killed the whole
  test process with a **Windows access violation** in an unrelated file.
  `comtypes` releases an interface from `__del__`, so the probe's COM pointers
  were released whenever the collector next ran - by then on another thread.
  They are now dropped and collected inside the probe call.
* The Master Volume page, the audio summary card and the recording player all
  imported `@/lib/api` as a **default export**, which it does not have. The
  production build caught it; the unit tests had not, because their mocks
  invented a default export instead of describing the real module.
* `RecordingWriter.start()` created its directory outside its own guard, so a
  file in the way of the recordings folder would have raised into the path that
  starts a broadcast.

### Store Kit 1.6.0

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.6.0-eb20c17-20260806-090532.zip` |
| SHA-256 | `ce7eb3be12b34e419c60185af78a06d42deca20c8d1dd5378e60998a2b79460c` |
| Receiver **1.3.0** · Kit **1.6.0** | 1.2.0-1.5.3 retained, none overwritten |

Verified inside the shipped executable by extracting the PYZ: `pycaw` (14
modules), `comtypes` (44), the observer, and `ensure_endpoint_observer`,
`stop_endpoint_observer`, `read_endpoint_state_now`,
`_report_endpoint_state_now` and `endpoint_state` in the Receiver itself.

Package audit found no database, credential, key, `.env`, log, `.git`,
`.venv`, `node_modules`, recording or developer path. The frozen Receiver was
asked directly and answered:

```
core audio     : reachable (3 active endpoint(s))
change reports : yes - endpoint change notifications supported
```

### Not done

No live HQ deployment. No Store touched, BP included. No second Store. Store
Kit 1.6.0 **not installed anywhere**. No physical acceptance.
**No software state equals SPEAKER_VERIFIED.**

---

## Master Volume: persistent desired state

The controls are no longer disabled by a Store being offline.

### The defect

An offline Store showed a greyed-out slider, a greyed-out Mute and the words
*"Immediate control unavailable"*. That answered a question the operator had
not asked. They were not trying to move a mixer that second; they were trying
to say what the shop should be set to. Letting the connection decide what the
intention was allowed to be meant a manager could not set a Store's level until
somebody switched a PC on.

### Three facts, never merged

| | where it lives | who writes it |
|---|---|---|
| **DESIRED** what HQ wants | persisted, `store_audio_desired_state` | an operator, only |
| **ACTUAL** what Windows reports | runtime only | the Receiver's readback, only |
| **CONNECTION** can it be applied now | runtime only | the socket |

Every misleading thing this feature could say comes from collapsing two of
those. *"Applied 70%"* is DESIRED wearing ACTUAL's clothes, so it is never
said; a number is called **Current** only when a Receiver read it, and **Last
reported** otherwise. A Store that has never reported says *"Current Windows
state: Unknown"* and can still be given a setting.

### Sync state, derived and never stored

`SYNCED` · `APPLYING` · `OUT_OF_SYNC` · `WAITING_FOR_SYNC` · `SYNC_FAILED` ·
`NO_DESIRED_STATE`

Computed on every read from the three facts above. A stored status would be a
fourth thing to keep in step and would go stale the moment a Store reported
anything.

A Store its own staff turned down reads **OUT_OF_SYNC**, not APPLYING: nothing
is being applied, and HQ deliberately does not answer a local change with a
corrective command. Store-local telemetry updates ACTUAL and never DESIRED, so
there is no feedback fight with the people in the shop. The operator can
re-assert the level if they want it enforced.

**APPLYING now means a command is genuinely on the wire.** A readback matching
the desired state clears it - that is proof of arrival whether or not the
acknowledgement came - and an unanswered command stops claiming APPLYING after
ten seconds rather than promising something nobody is keeping.

### Persistence

`store_id` is the PRIMARY KEY, so 20 → 40 → 50 → 70 leaves **one row saying
70**. Latest-wins is a schema property; no queue can exist. A partial
instruction merges: pressing Mute says nothing about the level chosen earlier.
Rows carry Store, Device, levels, actor and timestamp - no credential, token or
JWT. Legacy `store_audio_pending_commands` rows are carried over once at
startup so intent recorded before the rework is not silently dropped.

The desired state **survives a successful apply**. It is a standing intention,
and keeping it is what makes it possible to notice tomorrow that a shop has
drifted. Clearing it is an explicit operator action that changes no mixer.

### Reconnect order

authenticate → confirm active primary Device → confirm Store → confirm the
stable MMDevice endpoint → **read ACTUAL first** → report it → compare with
DESIRED → apply only a genuine difference → read back → report. A Store already
where HQ wants it is sent nothing at all.

Refused, honestly and without losing the intention, when the Device was
replaced (`SYNC_FAILED`, "the Receiver Device changed"), when a broadcast owns
the Store, or when there is no controllable output.

### Broadcast ownership unchanged

Single writer. A broadcast owning a Store refuses a non-owner with 409 and
records **nothing** - no desired state is quietly banked to fire the moment the
announcement ends. Restoration remains authoritative: original 25% muted,
desired 70, broadcast at 80, STOP restores **25% muted**, and the panel then
reads 25% ACTUAL / 70% DESIRED / OUT_OF_SYNC. The desired state is never
applied in the restoration path.

### Results

Backend **3435 passed** (50 in the Master Volume suite), frontend **265**,
Playwright **300**, production build OK.

Idle load, no broadcast, 5 → 40 Stores: offline Store listed, stale, accepted a
desired state (**200**, `WAITING_FOR_SYNC`, latest of 70/30/70 = 70), noisy
Store 42 local changes → 3 frames, HQ matched the Store, **no command refused
for being unreachable**, CPU 0.16 s and RSS 91.4 MB at 40.

Broadcast load with recording, 5 and 40 Stores: recording `available`, opus,
21 chunks written / 0 dropped, **0 audio chunks dropped**, max queue 1/24,
restoration held.

### Target enforcement

The panel is a permanent Store audio configuration console. The persisted value
is the **TARGET**, and the operator sets it whenever they like.

While a Store is online and no broadcast owns it, the Target is
**authoritative**: if somebody at the till turns a shop down, SpeakLink puts it
back. The naive form of that is a fight with a person holding a mouse -
endpoint notifications arrive in milliseconds, so the loop would run as fast as
the socket allows. Four bounds prevent it:

| bound | default | what it prevents |
|---|---|---|
| Debounce | 20 s | interrupting somebody mid-adjustment; collapses a drag into one command |
| Cooldown | 15 s | a command and its own readback triggering the next one |
| Attempt budget | 3 | an endless argument; ends in `ENFORCEMENT_SUSPENDED` |
| Post-broadcast quiet | 30 s | STOP's restoration being dragged away in the same breath |

The policy is a pure decision function with time passed in, so all four are
tested by calling it rather than by waiting. Every refusal returns a *reason*,
so a test asserts WHY nothing happened rather than only that nothing did.

`ENFORCEMENT_SUSPENDED` is deliberate: a shop where somebody keeps disagreeing
must end in a fact an operator can act on. Setting a new Target, or seeing the
Store match, clears it.

### A real bug this found

Both enforcement paths gated on `capabilities.output_volume`, which only ever
arrives in `receiver_ready` — and that happens at broadcast PREPARE. A Store
that had simply never been broadcast to could therefore never be brought to its
Target, however plainly it was reporting its own mixer. The endpoint status is
now the single gate, which already means either the Receiver said so or it
demonstrably read its endpoint.

### Receiver reconnect: audited, already sufficient

No change was needed, and the requirements are already covered by tests:

* **starts with Windows** — `-AtLogOn` trigger for the installing user;
* **recovers if it dies** — a separate periodic trigger with
  `MultipleInstances IgnoreNew`, because `RestartCount` applies when a task
  fails to *start*, not when the program it started exits;
* **survives HQ restarts and LAN faults** — bounded, jittered exponential
  backoff, 1 s to a 60 s cap with 25% jitter, reset after a stable connection;
* **no manual Store interaction** — `StartWhenAvailable`,
  `AllowStartIfOnBatteries`, `DontStopIfGoingOnBatteries`, no execution time
  limit, and it runs as the interactive user with no stored password.

The jitter matters at 44 Stores: when a shared link returns, the estate must
not reconnect in the same millisecond and knock it over again.

### Load with enforcement

| | 5 | 10 | 20 | 40 |
|---|---|---|---|---|
| Local changes fighting the Target | 12 | 12 | 12 | 12 |
| Enforcement commands sent | **1** | **1** | **1** | **1** |
| Final sync state | SYNCED | SYNCED | SYNCED | SYNCED |
| Offline Store accepted a Target | 200 | 200 | 200 | 200 |
| Noisy Store 42 changes → frames | 3 | 3 | 3 | 3 |
| CPU s · RSS MB | 0.09 · 83.2 | 0.06 · 84.5 | 0.12 · 86.8 | 0.31 · 91.7 |

Twelve deliberate local changes produce **one** correction and the Store ends
SYNCED. The load harness shortens the bounds through
`SPEAKLINK_TARGET_*_SECONDS` so a run lasting seconds exercises the real
mechanism; an override can only ever *shorten* a wait, and there is no way to
switch a bound off.

Broadcast load with recording, 5 and 40 Stores: recording `available`, 0 chunks
dropped, 0 audio chunks dropped, max queue 1/24, restoration held.

### No new Store Kit

The Receiver is unchanged by this work - Target state, sync state, enforcement
and the refusal rules are all HQ-side. **Store Kit 1.6.0 remains current**, and
its SHA-256 is unchanged.

### Not done

No live HQ deployment. No Store touched, BP included. No second Store.
**No software state equals SPEAKER_VERIFIED.**

---

## Scope correction: Broadcast-only Store volume

Branch `refactor/broadcast-only-store-volume`.

### What was removed, and why

The dedicated always-on Master Volume console is gone: the page, its navigation
entry, its route, the dashboard summary card, the persistent Target state, the
offline pending commands and the background enforcement sweep.

**Store Windows master-volume control now exists only while that Store is part
of an active broadcast.** With nothing on air SpeakLink does not observe or
touch a shop's mixer at all, and Store staff use Windows normally.

Endpoint observation is broadcast-scoped again: it starts at PREPARE, after the
original state has been captured, and is detached at restoration. A callback
left attached could report a restoration into the *next* broadcast as though
somebody had moved the slider. After STOP, SpeakLink leaves the mixer alone.

`endpoint_state` and `audio_control` require a session id again, and a control
command must name the broadcast the Receiver is running - so a sessionless
command is refused by construction rather than by a check.

### A finding worth recording

The live HQ is **repo-native**, so a restart picks up whatever branch is
checked out. It was restarted while the Master Volume branch was checked out,
and a real operator used the feature: `store_audio_pending_commands` and
`store_audio_target_state` both hold a genuine row against BP, and a real
3-minute broadcast (session 1, 10:57-11:00 UTC) was recorded to
`data/recordings/broadcast-000001.webm`, 634 KB.

Those two tables are therefore **already deployed with real data**. They are
left **dormant and documented** rather than dropped: nothing creates, reads or
writes them any more, and `test_broadcast_only_volume.py` asserts the
application no longer names them. Dropping live tables to tidy up a reverted
feature is not worth the risk.

Also found and fixed: the pilot environment scoped the database but not the
data directory, so load runs wrote recordings into `data/recordings/` - the
folder the live HQ uses - leaving orphan `.part` files for sessions 2-4 with no
metadata row. The harness is now scoped; **the orphan files are still there and
should be cleared by hand**, since deleting anything from live data is not this
task's to do.

### A real defect this refactor caused, and how it surfaced

Removing the always-on block also removed `_start_recording`,
`_finish_recording` and the writer registry, which sat in the same span of
`server.py`. Their call sites survived, so every broadcast start raised
`NameError` - **46 failures and 60 errors**. Restored verbatim from the previous
commit, with the removed feature's symbols asserted absent from what came back.

### Broadcast-scoped volume, unchanged

PREPARE captures the original volume and mute and persists the crash-recovery
record before any mutation. HQ steers the real Windows endpoint during the
broadcast; Store-local changes flow back and the Console follows them, with no
corrective command generated. STOP restores the exact pre-broadcast state -
never the announcement level and never a level somebody set mid-broadcast.

PCM gain stays at unity, so a 50% slider is 50% at the Windows master and not
50% x 50%.

### Recording, completed

Play **and** Download, both authenticated, both applying Broadcast History's
permission and Store Scope. Download shares playback's route body so the
authorization cannot drift, differing only in `Content-Disposition`. The
filename is the session id alone. Permanent deletion removes the audio first,
then the rows, so a failure can at worst leave a row reported as MISSING rather
than an orphan file nothing points at; deleting one broadcast leaves the others
untouched and the directory is never removed.

### Results

Backend **3407 passed, 0 failed**. Frontend **237**. Playwright **300**.
Production build OK, `compileall`, `pip check`, `git diff --check` and the
secret scan all clean.

Load with recording enabled:

| | 5 | 10 | 20 | 40 |
|---|---|---|---|---|
| Ready Receivers | 5 | 10 | 20 | 40 |
| Audio chunks dropped | 0 | 0 | 0 | 0 |
| Max audio queue / capacity | 1/24 | 1/24 | 1/24 | 1/24 |
| Recording | available | available | available | available |
| Recording chunks written / dropped | 21/0 | 21/0 | 21/0 | 21/0 |
| Endpoints restored | 5 | 8 | 18 | 38 |
| PCM gain left at unity | yes | yes | yes | yes |
| CPU s · RSS MB | 0.47 · 84.6 | 0.44 · 86.6 | 0.69 · 89.7 | 1.28 · 96.6 |

A stale load-harness expectation was corrected rather than papered over: the
synthetic Receiver now answers commands with a readback, so the
stale-telemetry Store no longer ends on the value that assertion named. What it
was always proving - that HQ never adopted the replayed 1% - is what it asserts
now, and it reports the value HQ actually holds (70).

### Store Kit 1.7.0

Rebuilt **because the Receiver source changed** - observation is
broadcast-scoped again and the protocol requires a session id.

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.7.0-0f2491c-20260806-115716.zip` |
| SHA-256 | `98e80348192f07feee357e430115317d7914def269bdc10d722f156489645f75` |
| Receiver **1.4.0** · Kit **1.7.0** | 1.2.0-1.6.0 retained |

Verified inside the shipped executable: `_start_endpoint_observer` present,
`ensure_endpoint_observer`, `read_endpoint_state_now` and
`_report_endpoint_state_now` all absent, pycaw present. Package audit found no
database, credential, key, `.env`, log, `.git`, `.venv`, `node_modules` or
recording. The frozen Receiver answers `core audio: reachable` and
`change reports: yes`.

### Not done

No live HQ deployment. No Store touched, BP included. No second Store.
**No software state equals SPEAKER_VERIFIED.**

---

## Fix: Broadcast History showed "No recording" for recorded broadcasts

Branch `fix/broadcast-history-recording-association`.

### The symptom, and what it was not

Seven completed broadcasts all displayed **No recording**, while
`data/recordings/` visibly held audio. None of the obvious explanations was
true: the audio was written, the files were finalized, the metadata rows
existed and said `available`, and the frontend bundle being served was current.

### Root cause, proven before any change

Broadcast History in the browser reads **`/api/broadcast/history/search`** -
the paginated endpoint added by the admin search work - and **not**
`/api/broadcast/history`. Recording metadata was attached only to the second.

Both endpoints were queried against the running HQ:

| endpoint | used by the page | `recording` |
|---|---|---|
| `/api/broadcast/history` | no | populated for all 7 |
| `/api/broadcast/history/search` | **yes** | `null` for all 7 |

Two endpoints returning the same shape had drifted, and only one of them was
ever looked at. The fix attaches recordings through the same shared helper so
they cannot drift again; the regression test fails without it.

### A second defect found while diagnosing

Recordings resolve from `SPEAKLINK_DATA_DIR`, falling back to the repository's
own `data/` - which on this machine is the **live** directory. `conftest` had
scoped the database per worker since the beginning but never the data
directory, so **any backend test that started a broadcast wrote its `.part`
file into the folder holding real announcement audio**. Three zero-byte
orphans appeared there during this task's own regression run. Now scoped, with
a test asserting recordings can never resolve inside the repository.

### Sessions 1-7: what was recoverable

| session | metadata | file | verdict |
|---|---|---|---|
| 1 | `available`, 634,557 B | only a 0-byte `.part` | **not recoverable** |
| 2 | `failed` (WinError 32 on rename) | 0-byte `.part` | **not recoverable** |
| 3-7 | `available` | valid Opus/Matroska | **recoverable** |

Sessions 3-7 were confirmed valid by read-only `ffprobe`. Nothing was renamed,
reconciled or deleted during diagnosis.

**The operator then deleted sessions 1-6 themselves** through the UI at
12:27-12:28 UTC, between the inspection and the restart -
`BROADCAST_HISTORY_DELETED by=superadmin`, recorded in `admin_deletion_events`.
That was not this task and not the restart, and it produced live proof of the
delete-cleanup path: those sessions' audio files are gone, session 7's
survives, and the recordings directory itself is intact.

### Live verification after the controlled restart

Gate read 0 non-terminal sessions and 0 unreleased leases, with the stop issued
**1 ms later from the same process**. Restart produced one worker; `/`,
`/console`, `/history` and `/api/` all 200.

On the running server, session 7 now returns `recording.status = available`
from the paginated endpoint, and:

* **Play** - HTTP 200, 87,047 bytes, `inline`
* **Range** - HTTP 206, 100 bytes, `Content-Range: bytes 0-99/87047`
* **Download** - HTTP 200, 87,047 bytes, `attachment; filename="broadcast-000007.webm"`
* the downloaded file passes `ffprobe`: `opus`, `matroska,webm`

### Known limitation

`duration_seconds` is null for these recordings. MediaRecorder writes a
streaming WebM header with no duration, so `ffprobe` reports `N/A` and the
metadata honestly stores nothing rather than inventing a figure. Broadcast
duration is shown separately in History from the session's own timestamps.

### Not done

BP not touched. No second Store. No Store Kit rebuilt - the Receiver source is
unchanged by this fix, so **Kit 1.7.0 remains current**. No controlled test
broadcast: that needs a person at the microphone.

---

## Live web audience: the two foundational slices

Branch `feature/live-web-audience-dynamic-targets`. This is backend and HQ
frontend only. No Receiver source changed, no Store Kit was rebuilt, nothing was
exposed to the public internet, and no live HQ was restarted.

### Why arbitrary MediaRecorder chunks are unsafe

A listener joining a Broadcast in progress needs an initialization segment and a
resume point its decoder can start from. Chromium's MediaRecorder gives neither
for free. Measured over a real 34-second capture at the 250 ms timeslice HQ
already uses, **0 of 113** non-initial chunks began with a Cluster identifier: a
`dataavailable` boundary is not a container boundary.

Forwarding whole timeslice chunks from an arbitrary point therefore hands the
decoder a partial cluster. Chromium accepts that only **intermittently** - across
repeated runs over identical captured bytes the 30-second join decoded on some
runs and failed with an append error on others. Intermittent is not support, and
measured once it would have looked like success.

Resuming from a genuine Cluster boundary, with the initialization segment sent
first, decoded and advanced at every offset and on every repeat. That is the
accepted architecture.

### One Cluster-aware relay per Broadcast

`backend/webm_stream.py` frames the live byte stream into an initialization
segment and whole Clusters. It finds a Cluster's end by walking its children,
not by scanning for the Cluster identifier - those four bytes occur inside Opus
payload, so a scan can split a cluster mid-block and cause the very corruption
the module prevents. Clusters carry an unknown size; their children carry known
sizes; the walk is deterministic.

Framing does not depend on how bytes arrive: fed one byte at a time, in one
blob, at timeslice boundaries or at random splits, it emits byte-identical
frames, and what it emits is an exact prefix of the input. The buffer never
holds more than the Cluster being assembled, and an init segment or Cluster that
never ends fails closed rather than growing.

`backend/web_audience.py` owns **one framer per Broadcast, not per listener** -
framing depends on the stream, not the audience. `BroadcastRuntime` creates the
relay with the session and closes it with the session, so no buffer outlives a
Broadcast and one Broadcast's bytes can never reach another's listener.

Measured: cluster media duration is a uniform **300 ms**, so waiting for a
boundary adds at most one cluster of latency. That is announcement latency, not
radio delay.

### Bounded per-listener queues, and the slow-listener policy

Each listener has an independent bounded queue and its own sender task. No
listener socket is reachable from the broadcaster's read loop; Store fanout, the
recording and web delivery are three siblings that share only the bytes.

A slow listener is **disconnected, not degraded**. A Store queue drops its oldest
chunk to stay live, which is right for a continuous decode. A listener is fed
structured Clusters, and silently dropping one leaves a hole in a timeline the
decoder is still tracking - so it is disconnected and may rejoin, receiving a
fresh bootstrap at the live edge. A gap the listener knows about beats one it
does not.

With 250 listeners the whole offer loop for the stream costs 25 ms and peak
queue depth is 1. At 40 Stores + 100 listeners + recording, with one Store that
never drains and one browser that never reads, the audio path cost 17 ms for 37
chunks, the recording saw every chunk, healthy Stores and listeners were
unaffected, and no queue exceeded capacity.

### New permission: Broadcast to Stores / Zones

`broadcast.store_delivery`. A Broadcast can now reach a web audience with no
Store targets, so "may broadcast" and "may put sound into a shop" are different
questions.

**A blank Store Scope does not bypass a missing physical permission.** Scope
answers WHICH Stores and treats blank as unrestricted; using it to mean "no
Stores at all" would overload a field whose empty value means the opposite. The
new permission denies every physical target regardless of Scope, and Scope
continues to narrow the accounts that hold it.

Enforced at the single point through which every physical target is resolved,
plus the Store inventory endpoint and live Store volume control. Existing
operators keep the capability: the code sits in the broadcast role defaults, so
OWNER, ADMIN and BROADCASTER hold it after the upgrade and VIEWER does not. The
catalog reseeds on every start and reseeding twice leaves one row.

### What is still missing

None of the listener-facing product exists yet:

* the web room, public Broadcast ID and join password
* join requests, approval, denial and Auto Approve
* the public `/listen` page and listener session cookie
* the participant panel, participant states and Kick
* the full Only With Link target mode and its zero-target session lifecycle
* dynamic live Store targeting: Add, Pause, Resume, Remove and Zone bulk actions

The relay is currently fed by every live Broadcast and has no listeners, because
there is no way to become one yet. That is the next slice.

---

## Web audience rooms, listener admission and Only With Link

Branch `feature/live-web-audience-dynamic-targets`. HQ backend, HQ frontend and
a new public browser page. No Receiver source changed, no Store Kit rebuilt,
nothing exposed to the public internet, no live HQ restarted.

### One room per Broadcast

Every Broadcast gets exactly one web room, created **with the session** rather
than at start, so the operator can copy and share the link before going live.
A listener admitted early hears nothing until the microphone is on - the socket
refuses until the Broadcast is live, so pending audio cannot leak.

Selected Stores, Zone and every other physical mode keep their Stores **and**
get a room. Only With Link is the mode with no physical destination at all.
Web listener count zero is perfectly valid and changes nothing about Store
delivery.

### Public identity and secrets

The public code (`EC-…`) is random, drawn from an alphabet with `0/O/1/I/L`
removed so it survives being read off one screen and typed into another. It is
**never** the session id - that is a small consecutive integer, and publishing
it would let one shared link enumerate every other Broadcast. Uniqueness is a
database constraint; a collision is redrawn.

The join password is **bcrypt-hashed only**. The plaintext is returned exactly
once, at generation, and there is no column it could be read back from. After a
refresh the console says *Password configured* and offers **Generate New
Password** rather than showing a masked placeholder that would imply SpeakLink
knows it. Rotation replaces the future password and **does not eject the
audience** - stopping new arrivals and removing current listeners are different
decisions.

Listener session tokens are stored only as a SHA-256 hash, travel in an
**HttpOnly** cookie, and never appear in a URL. `Secure` is the default;
`SPEAKLINK_LAN_HTTP_LISTENERS=1` is an explicit named opt-in for the LAN pilot
rather than a silent weakening of the production default.

### Admission

A correct password admits immediately and is unaffected by Auto Approve -
knowing the password *is* the authorisation. A wrong password is refused **as a
wrong password** and is never quietly converted into a join request; Request
Access is a separate, explicit action. Auto Approve defaults OFF and is read
inside the same transaction that creates the participant row, so a toggle
racing a request produces one participant in one state. Approval is guarded on
current status, so two broadcaster tabs clicking at once mint exactly one
listener session. Denial is terminal.

A waiting browser polls **its own** state with a signed pending claim that
authorises nothing, so Approve reaches it without a refresh and nobody can poll
by guessing a participant id.

### Participant state, and what LISTENING does not mean

Persisted: REQUESTED, APPROVED, PASSWORD_ADMITTED, DENIED, KICKED, LEFT,
ROOM_ENDED. Runtime only: connected, last heartbeat, READY_TO_PLAY, BUFFERING,
LISTENING, PAUSED, DISCONNECTED. There is **no database write per heartbeat**.

**LISTENING means the listener's browser reported that its playback pipeline is
running, from real media events.** It does not mean their volume is above zero,
that headphones are connected, or that anyone can hear anything. It is client
telemetry, not proof, and `SPEAKER_VERIFIED` is deliberately not reused.

Counts stay separate - waiting, connected, listening - because
approved-but-not-connected is not connected and connected is not listening.

### Kick

Invalidates the session **before** closing the socket, so a reconnect racing
the close has nothing valid to present. It removes a **session, not a person**:
somebody who clears their browser and asks again is a new participant and a new
decision. There is no fingerprinting and no person-level ban.

### The listener socket

Authenticated by cookie only. The listener may send exactly one message type -
a heartbeat carrying its own playback state. Audio, Store commands, unknown
types, unparseable frames and oversized frames all **close** the connection
rather than being ignored, so a client cannot probe for a tolerated message.

The attach is atomic and anchored to a Cluster index, so no Cluster is lost or
duplicated however the join is timed - proved at all 36 attach points of a real
capture.

### Room end and restart

The room ends wherever the Broadcast ends - Stop, Emergency Stop, a dropped
microphone, cleanup - clearing every listener token in the same transaction.
The public code stops resolving, so a shared link does not linger. Listener
socket state is runtime only and does not survive an HQ restart; a reconnect
gets a fresh bootstrap at the live edge, and **no claim is made of
uninterrupted audio across a restart**.

### Public internet is NOT enabled

This is LAN/pilot only. Before internet-wide listening: public DNS, HTTPS, WSS,
valid TLS, firewall and reverse-proxy review, production rate limits, a CORS
and cookie review, and a public threat-model review. No ports were opened, no
domain configured, no CORS widened.

### Still to come — the next slice

Dynamic live Store targeting: Add, Pause, Resume and Remove individual Stores
mid-Broadcast, plus Zone bulk actions, with original-mixer snapshots captured
at add time and leases held across Pause.

---

## Active Broadcast web audience supervision, and the Finished progress bar

### Room credentials follow the broadcaster's identity

`broadcast.view_ownership` now governs whether a supervisor may see a live
Broadcast's **web room**. The public code is a credential — anyone holding it
can attempt to join, and with Auto Approve on they are in — so it travels with
the broadcaster's identity rather than with permission to open the page.

For a caller without it the `web_room` key is **absent from the JSON entirely**,
not present-and-null. An absent key cannot be un-hidden by a frontend; a null
one invites somebody to try. Redaction happens in `ActiveRow.serialize`, the
single existing seam, so the list and the detail routes cannot disagree.

The list carries only a **compact summary** — code, status, auto-approve,
waiting/connected/listening counts. Participants live behind their own route,
for the same reason the Stores do: fifty sessions multiplied by every listener
is the payload this page exists to avoid.

### A separate permission for touching somebody else's audience

`broadcast.manage_web_audience` — *Active Broadcasts — Manage Web Audience*.

Seeing who is broadcasting and removing a person from their audience are
different powers, and the second happens where the owning operator cannot see
it. So reading the page never confers it. **OWNER and ADMIN** hold it by role
default; **BROADCASTER and VIEWER** do not, which falls out of the existing
matrix without editing it. Reseed is idempotent.

It covers Approve, Deny, Kick and Auto Approve. **Password rotation stays
owner-only**: it replaces a credential the owner has already shared with an
audience, and a supervisor doing that silently would lock the owner out of
their own room's future joins.

### Owner vs cross-owner

The **owner** manages their own room from the Console exactly as before — no
supervision permission, no regression, and rotation remains theirs.

A **cross-owner** supervisor is subject to the same Store Scope containment
rule as a cross-owner stop: a Broadcast reaching Stores this account may not
supervise cannot be reached through its audience either. Without that the panel
would be a way into a Broadcast whose physical half is out of bounds. **Only
With Link** has no Stores, so containment is vacuous and authorization rests
entirely on the explicit permission — which is the point of having one.

One authorization function serves every audience route, so the endpoint written
last cannot disagree with the one written first. Room lifecycle is called, never
reimplemented. A cross-owner Kick is logged.

### The UI

A **Web Audience** button sits beside View Stores. It is not gated on
`view_targets`: a Link Only Broadcast has zero Stores, shows no View Stores
button, and its audience is the whole point. The panel renders from
server-supplied capability flags rather than role names.

### Recording player — Finished fills exactly

**Root cause, measured.** The seek control was a native range with an accent
colour. Chromium paints that fill up to the **thumb centre**, whose travel is
inset by half the thumb width, so at `value === max` it stopped about half a
thumb short. That fill is painted inside the widget — **no DOM node represented
it**, so it could be neither corrected nor measured, and a test asserting
`value === max` would have passed the whole time the operator saw the gap. A
second, smaller contribution: the last `timeupdate` fires before the end, so
`currentTime` at `ended` is routinely tens of milliseconds below `duration`.

**Two further defects, found by manual acceptance and also measured.** The fill
reached the end but the *point* still stopped short, because what was visible
was still the **native thumb** — whose centre travels inside an inset equal to
half its width (6 px on a 12 px thumb), which nothing outside the widget can
change. And progress advanced in visible **steps**, because it was driven only
by `timeupdate`, which fires roughly every **265 ms** (eight events in two
seconds, measured in this player).

The bar is now four layers: custom background, custom **fill**, custom **visual
thumb**, and the real range on top with its own track and thumb made
transparent. The fill and the thumb consume **one** `visualProgressPercent` —
separate calculations are how a line and a point drift apart — and the thumb is
centred with `translateX(-50%)`, so 100 % puts its **centre** exactly on the
track edge and the circle overhangs by half its width. That overhang is correct:
the centre is the position.

A single `requestAnimationFrame` loop samples `audio.currentTime`. **The media
element remains the clock** — nothing advances by wall time, so a throttled tab
or a stalled stream stops the bar rather than running ahead of the audio.
Exactly one loop exists; it starts on play and is cancelled on pause, ended,
error, unmount and recording switch, and re-entering Play cannot schedule a
second. Cleanup belongs to the global player above the router, so changing page
stops neither the audio nor the bar.

The range stays real and interactive — keyboard, focus, pointer, `aria-label` —
and is neither hidden nor `pointer-events: none`; only the painted layers ignore
the pointer. Seeking **jumps** rather than animating, and leaves the Finished
state. `position` and `duration` stay exactly what the element reports, and
Finished still comes **only** from the real `ended` event: being 100 % of the way
through is not the same fact as having ended.

Proved by **rendered pixels** in real Chromium on 2-second and 13-second
finalized recordings: fill right edge **and thumb centre** each within 1 px of
the track edge at Finished; fill and thumb within 1 px of each other mid-play;
at least five distinct positions per second during playback; no drift while
paused; immediate seek; replay that resets and advances; one-click A→B switch;
and progress that survives navigating to another page.

### Still the next milestone

Dynamic live Store targeting: Add / Pause / Resume / Remove mid-Broadcast, with
original-mixer snapshots at add time and leases held across Pause, plus Zone
bulk actions. Unchanged by this work.

---

## Pre-Broadcast target resolution, and the Store picker

### What ONLINE means, and what it used to mean

**Authority: the live Receiver connection inventory** (`manager.online_store_ids()`),
the same source the target list already uses to paint each row's `status`.

It previously filtered on `Store.is_online_store` — the column Store Management
edits with a checkbox labelled **Online / Physical**. That is an e-commerce
classification, defaults to `False`, and says nothing about reachability. So
*Online Stores Only* targeted the e-commerce stores and excluded every physical
shop whose Receiver was connected: a console showing **BP ONLINE** resolved zero
targets, which is exactly the 0/0/0 the operator reported.

ONLINE means **currently reachable**. It does not mean READY,
PLAYBACK_CONFIRMED or SPEAKER_VERIFIED.

### Online Stores Only

Targets every Store the operator may physically broadcast to **and** whose
Receiver is connected right now. Store Scope and `broadcast.store_delivery`
both still apply.

**Start re-resolves it.** The set stored at creation is a preview — a Store may
have dropped or reconnected since, and the browser's copy is older still. So a
stale page cannot start a broadcast to a Receiver that has gone, and a Store
that came back is not excluded for having been offline a moment ago. Target
rows, leases and PREPARE all describe that one re-resolved set.

**Then it freezes.** Once Start succeeds a Store connecting later is *not*
added: joining an announcement half way through is a decision an operator makes,
not something a heartbeat does. Add/Pause/Resume/Remove remains its own
milestone.

**Zero connected Stores refuses**, with its own message rather than the generic
"no Stores match". No silent fallback to All Stores, to the offline ones, or to
Only With Link.

A stale `selected_store_ids` from Selected mode can neither narrow nor widen the
automatic mode — proved both ways, including by crafted request.

### Target counts

The first card is **TARGETS**, not SELECTED — nobody selects anything in the
automatic modes, and "Selected 0" beside a live Zone broadcast reads as a fault.
In Online Stores Only the third card counts **Excluded** rather than Offline,
since those are Stores left out rather than targets chosen.

### The Store picker

Client-side filtering and pagination over the existing `/broadcast/target-stores`
response, which already returns the operator's authorised Stores and is
deliberately small. **Store Scope and physical permission stay server-enforced**;
no second Store-query system was introduced.

Page size 10/20/50. Filters: search (code, name, city, Zone), Zone, City,
connection status, and Clear filters. Filtering returns to page one.

**Selection belongs to the broadcast, not to the visible page.** It survives
paging, filtering, being hidden, and clearing filters; only *Clear selection*
removes it, and a refresh does not discard it — a Store going offline is a status
change, not a reason to deselect it.

Two named bulk actions: **Select page** (visible rows) and **Select all N
filtered** (every match across pages), both additive. A single "Select all"
beside a paginated table means one thing to somebody seeing ten rows and another
to the code.

**Zone FILTER ≠ Zone TARGET MODE** — filtering changes which rows are visible and
never what is targeted. Tested in both Jest and Chromium.

The result count reports **authorised** Stores only, so a scoped operator is never
told how much fleet they cannot see. Only With Link renders no picker at all, and
an account without `broadcast.store_delivery` never requests the inventory.

### Still the next milestone

Dynamic mid-Broadcast targeting: Add / Pause / Resume / Remove and Zone bulk
actions. Unchanged by this work.

---

## Public listener acceptance, and the live Console layout

### The LAN Secure-cookie defect

Manual LAN testing found two release-blocking failures **while the entire mocked
Playwright suite was green**. One cause: the listener cookie carried `Secure`,
and Chromium refuses a `Secure` cookie from an untrustworthy origin — which
`http://192.168.4.134:8000` is. The browser stored nothing, so every listener
request arrived anonymous.

It never reproduced in the suite because **`http://localhost` is trustworthy**
and keeps Secure cookies. The tests were right about the code and wrong about
the world.

**Two symptoms, one cause:**
- *Request → Approve → "Broadcast Ended"* — the pending cookie was dropped,
  `/listen/me` returned 401, and the client mapped 401 → `ENDED`.
- *Password → Buffering for ever* — the socket handshake was refused for want of
  a cookie, and **refusing before `accept()` cannot deliver an application close
  code**: the browser sees only `1006`, indistinguishable from a dropped
  network, so the client retried behind Buffering.

### Cookie policy

Decided **per request from the scheme the browser actually used**:

| | HttpOnly | SameSite | Secure |
|---|---|---|---|
| LAN over HTTP | yes | Lax | **no** |
| Production HTTPS | yes | Lax | **yes** |

`X-Forwarded-Proto` is honoured **only** under the existing trusted-proxy
setting. Path stays `/api/listen`. No global switch to forget, and production is
never weakened to make a pilot work.

### Listener states

`ENDED` means the Broadcast is over. `LOST` means this browser has no valid
listener session. `WAITING_BROADCAST` means admitted before the microphone
opened. Conflating the first two is what told an approved listener their
Broadcast had finished.

The socket **accepts before refusing** so it can say `not_admitted` or
`not_started` rather than leaving the page to guess from `1006`.

**Buffering is bounded:** connected but `currentTime` unmoved for 8 s — long
enough that a 300 ms Cluster and a two-Cluster bootstrap have had every chance —
reports the failure and re-bootstraps. **LISTENING** still requires real
playback progress.

### Real-backend E2E: 8/8

Nothing mocked — real FastAPI on a temp database and its own port (never 8000,
which is the live HQ), real admission rows, the real `Set-Cookie`, real listener
socket, relay and framer, real audio through the actual broadcaster socket, real
Chromium MediaSource.

A password → LISTENING · B request→approve → LISTENING · C wrong password ·
**D autoplay blocked → tap → LISTENING** · E real Stop → Ended ·
**F disconnect → reconnect → LISTENING** · G Kick · H rotation.

### Console layout

Controls **4/12** · Web Audience **5/12** · Targets **3/12**, then the Stores
below. The audience used to sit under the Store table, which meant scrolling
past forty Stores mid-broadcast. Same `WebAudiencePanel`, reused not forked; in
the row its waiting and listener lists are bounded and internally scrollable.
Asserted geometrically — order, aligned tops, relative widths, Stores below,
card under 900 px with a large audience, and clean stacking at 800 px.

### Sidebar

The reported scroll-away **could not be reproduced on this branch**: measured at
1920/1440/1280/1024/900/820/768/700 px the aside holds viewport top 0 and the
document never scrolls. No production change was made; the missing regression
tests were added instead. The live HQ at `192.168.4.134:8000` is an **older
deployment** and is not evidence about this branch.

### Preserved

Online Stores Only from Receiver connectivity, Start-time revalidation, target
freeze, zero-online refusal, the Store picker (search/Zone/City/status,
10/20/50, Select Page, Select All Filtered, selection persistence), and the
Recording Player (smooth RAF progress, exact thumb, one-click A→B, route
persistence).

### Next milestone

Dynamic mid-Broadcast Store targeting: Add / Pause / Resume / Remove and Zone
bulk actions. Still pending.

---

## The HQ navigation shell — final contract

**Desktop: `position: fixed`.** Not sticky. Not "it stays because main happens
to scroll".

It was `md:sticky md:top-0`, which keeps the element in normal flow and leaves
its position dependent on which ancestor scrolls — correct at the time, and
quietly dependent on a layout relationship any future page could change. Fixed
takes it out of flow and anchors it to the viewport, so no page, live Broadcast
state or modal can move it.

| | |
|---|---|
| Sidebar | `fixed inset-y-0 left-0 z-40 w-64 h-screen flex flex-col` |
| Main shell | `md:ml-64` — matches `w-64` exactly |
| Root shell | `h-screen overflow-hidden` |
| Main content | `flex-1 min-h-0 overflow-y-auto overflow-x-hidden` |

**Scroll ownership:** `<main>` only. `document.documentElement.scrollTop` and
`window.scrollY` stay **0** on desktop — measured, not assumed. `min-h-0` is
what makes the flex child actually own its overflow.

**Three zones:** brand `shrink-0`, nav `flex-1 min-h-0 overflow-y-auto`, account
`shrink-0`. On a short viewport **only the nav list** scrolls — the logo stays at
the top and Log out stays on screen.

**Mobile** keeps its off-canvas drawer below `md`, with no desktop offset forced
onto the content and no horizontal overflow.

**Modals** may block sidebar *clicks* via the backdrop (`z-50` over `z-40`).
They may not move it — asserted separately.

**Recording player** starts exactly where the sidebar ends (`md:left-64`, the
same width) and covers neither the sidebar nor Log out — proved by rectangle
intersection, not by comparing vertical edges, which would call two elements in
different columns a collision.

**Positioning lives in `Layout.jsx` only.** No page carries sidebar CSS, and no
JavaScript scroll handler is involved.

### Measured geometry

At 1920×1080, 1600×900, 1440×900, 1366×768, 1280×720 and 1024×768 the sidebar
reports `top=0 left=0 position=fixed`, height equal to the viewport, before and
after scrolling main to its end; `mainScrollTop` was 239/465/503/635/727/691
respectively. Verified on all nine authenticated routes, in idle, live Selected
Stores and live Only With Link Consoles (sampled at 0/25/50/75/100 % of the
scroll), with the confirmation modal open, on a short 1366×600 viewport, with
the recording player open, and on a 500 px phone.

### Still pending

Store → HQ Windows volume telemetry, the listener lifecycle items (tests I and
J), and dynamic mid-Broadcast Store targeting. Unchanged by this work.

### One scroll surface, not two

The reported symptom was **two right-hand scrollbars**: scrolling the outer one
carried the whole UI, header included, upward.

**Cause:** `html`, `body` and `#root` carried no height or overflow rules at all
— `height: auto`, `overflow: visible` — so they were free to grow past the
viewport and present a second scroller beside main's own. The fixed sidebar was
unaffected (it is viewport-anchored), which is why only the header and content
moved.

**Contract now, in `index.css` rather than any page:**

```
html, body, #root { height: 100%; overflow: hidden; }
```

plus `min-h-0` and `overflow-hidden` on the main shell, so `<main>` genuinely
owns its overflow rather than expanding its parent.

This makes the outer scroll **impossible**, not merely invisible. No scrollbar
is hidden with `::-webkit-scrollbar` or `scrollbar-width` — there is one scroll
container, not two with one painted out.

**Measured at 1366×768** (live Selected Stores Console): html 768/768, body
768/768, #root 768/768, main 704/907; `window.scrollY` 0 throughout; main
reaches its maximum 1185 and **stays** there through repeated further wheeling,
with the sidebar and header rects unchanged.

Proved with **real `page.mouse.wheel` input**, not by setting `scrollTop` —
which would only show that main *can* scroll, not that the wheel reaches it —
and by continuing to wheel past the end, which is where the outer scroller used
to take over. Verified across six desktop viewports, all authenticated routes,
idle / Selected-live / Link-only-live Consoles, a 40-listener audience, the
confirmation modal, the recording player, and a phone.

Component-local scrollers (the Web Audience lists, the sidebar nav on a short
viewport) are bounded areas and are deliberately not counted as page scrollers.

## Development builds no longer touch the live HQ frontend

Port 8000 serves `frontend/build` **straight from disk**, so an ordinary
`craco build` - run only to check that the code still compiles - replaced the
bundle the live HQ was serving. No restart, no deploy step, nothing to notice.
That happened repeatedly during this branch's work before it was spotted.

Verification now uses:

```
cd frontend && npm run build:isolated      # writes frontend/build-dev
```

It is a Node wrapper (`frontend/scripts/build-isolated.js`) rather than an
inline `BUILD_PATH=...` prefix, because npm runs scripts through cmd.exe on
Windows where that syntax is a missing command and not a variable assignment.
Building into `frontend/build` is refused outright.

Proved by hashing `build/index.html`, its main bundle and the bytes port 8000
actually serves, before and after: all five unchanged, live mtime untouched, a
fresh artifact in `build-dev`.

## A Kick removes a participant from ONE Broadcast

Manual testing found that after being kicked from Broadcast A a listener could
not ask to join A again, and could not join a completely different Broadcast B
either. The removal had become a property of the browser.

**Cause:** `/listen/me` had no idea which room the browser was looking at. A
kicked listener's pending-claim cookie still resolved - and that cookie was
never cleared when the claim was spent - so the endpoint answered for whatever
room this browser had last touched. Opening B returned A's KICKED row, and the
page said "You were removed from this Broadcast" about a Broadcast the listener
had never joined. The client compounded it by never comparing the answer's
`public_code` with the one in the URL.

**No schema change.** The participant model was already right: a kick clears
the session token, both join paths always insert a new participant, and
`_participant_in_room` already refuses across rooms. The defect was entirely in
session-to-room resolution.

**Contract now:**

- Kick is scoped to one room and one participant. It is not a ban - not by
  browser, cookie, display name, IP or device. There is no global Web Audience
  ban feature.
- `/listen/me?public_code=...` answers only for THAT Broadcast. State belonging
  to another room answers exactly as if there were no session at all, and never
  names the other room.
- A kick terminates the current admission immediately: socket closed, audio
  stopped, runtime removed, participant KICKED, session invalid, and no
  automatic reconnect.
- Returning requires explicit user action. **Join Again** calls
  `POST /api/listen/forget`, which only discards this browser's own listener
  cookies - it admits nobody. The listener still needs the current password or
  a fresh Request Access, and the broadcaster still decides.
- A rejoin is a NEW participant. The KICKED row stays KICKED as audit truth and
  is never mutated back into an admission. Nothing is deleted.
- Counts: a KICKED row counts as neither connected, listening nor waiting. A
  fresh request raises waiting; a fresh admission's connected state follows the
  new runtime connection only.
- A late teardown from the kicked socket cannot disturb its replacement: the
  registry is keyed by participant, the rejoin is a different participant, and
  `detach` is additionally guarded by socket identity.

Covered by `backend/tests/test_kick_is_room_scoped.py` and real-Chromium tests
Q-X in `frontend/e2e/real-listener-e2e.spec.js`.

### Known separate defect: the Listening count

The Web Audience `listening` count does not reach 1 even for an ordinary first
join, though the browser really is playing. Real-backend test I fails this way
and did so before this work; it is a heartbeat-reporting defect, not a kick
one, and is deliberately left for the listener-lifecycle milestone rather than
asserted in the kick tests.

## A Deny refuses ONE admission attempt in ONE Broadcast

Reported with the same shape as the Kick defect: denied in Broadcast A, then
Broadcast B claimed to have denied them too.

**Cause, and it was only half where the Kick's was.** The server was already
right: `/listen/me` had been made room-scoped for the Kick, and twelve new
route-level tests confirmed a denial in A already answered 401 for B. The leak
that remained was in the page. `Listen.jsx` scoped its BOOTSTRAP call but its
admission POLL still asked `/listen/me` with no room attached, and took
whatever came back - so a leftover session or claim from another Broadcast
could answer, and the page would show that room's denial, removal or admission
as if it were this one's. Both calls are scoped now.

**Contract:**

- Deny is scoped to one room and one admission attempt. It is not a ban - not
  by browser, cookie, display name, IP or device.
- A DENIED row stays DENIED as audit truth and is never mutated back into a
  request. Nothing is deleted.
- Refreshing the denied Broadcast reports the denial again and never resubmits.
- **Request again** calls `POST /api/listen/forget`, which only discards this
  browser's own cookies. It requests nothing and admits nobody: the listener
  then chooses password join or Request Access, and the broadcaster decides
  again. Retry after a denial and Join Again after a removal are the same
  action - one attempt is over, and another may be made.
- A retry is a NEW participant. A denial in A has zero effect in B, in either
  direction: an A session neither authorises nor denies B, and asking about B
  creates nothing in B and reveals nothing about A.
- Counts: a DENIED row counts as neither waiting, connected, listening nor
  buffering. Room A's counts never move because something happened in B.

Covered by `backend/tests/test_deny_is_room_scoped.py` (Y, Z, AA-AG) and real
Chromium tests AH-AL. Kick Q-X re-run green alongside them.

The heartbeat / Listening-count defect recorded above is untouched and remains
the next milestone.

## Live HQ deployment - 2026-08-08 13:33 - room-scoped listener fixes

Deployed `feature/live-web-audience-dynamic-targets` at **39df157** to port
8000. Authorised deployment; nothing was added or changed for it.

**Why it was needed.** The Kick and Deny fixes had been on disk and pushed for
some time, but port 8000 serves `frontend/build` from disk and that directory
still held a bundle predating both. A string search settled it rather than a
filename comparison: the served bundle contained neither `listen-request-again`
nor `listen-join-again`. The operator's manual failures were against code that
did not contain the fix.

**Gate before stopping.** 0 live sessions, 0 starting, 0 unreleased Store
leases, 0 targets playing. The single non-`ended` session is
`emergency_stopped`, which is terminal. Four web rooms are still OPEN, all
belonging to sessions that FAILED on 2026-08-07 - stale rows from a failed
lifecycle, deliberately left alone: a deployment is not the place for a data
change.

**Backup.** `_live-hq-deployment-backups/speaklink-20260808-132655-pre-deploy-39df157.db`,
724992 bytes, sha256 `6c05d944289c03004bf303076ba10b48bb21953e28ec6a5d52f64340db5ed084`,
`integrity_check ok`, `foreign_key_check` 0 rows, 31 tables, 53 sessions, 44
stores, 15 receiver devices. Taken with the SQLite online backup API rather
than a file copy: the live database had 4MB of committed pages in its WAL, and
copying the main file alone would have produced a backup missing all of them.

The previous frontend is kept whole at
`_live-hq-deployment-backups/frontend-build-20260808-132655-pre-deploy` (14
files, verified byte-identical) and at `frontend/build-retired-20260808-132655`.

| | before | after |
|---|---|---|
| index.html | `22c601c3…` | `716816ea…` |
| main bundle | `main.a6ab3e77.js` | `main.0ff18242.js` |
| bundle sha256 | `0e0afebf…` | `6ba8a2f4…` |

Deployed by two directory renames rather than a file-by-file copy: an index
names hashed assets from its own build, so a half-replaced directory would have
served an index pointing at a bundle that was not there yet.

**Restart.** Repo-native `tools/speaklink_server.py stop` then `start`. Old pids
22676 and 6808 both confirmed exited; new pid 13612 with socket owner 19336,
`--workers 1`, the same two-process shape as before. `/`, `/console`,
`/history`, `/api/` and a listener SPA route all 200.

**Proof the fix is live.** The served bundle is byte-identical to
`frontend/build` and contains `listen-request-again`, `listen-join-again`,
`listen/forget` and `public_code`. The running process's own OpenAPI declares
`/api/listen/forget` and `/api/listen/me` **with a `public_code` query
parameter** - the room scoping is in the process, not merely on disk.

**Data and Receivers.** `integrity_check ok`, `foreign_key_check` 0 rows, 31
tables before and after, 53 sessions / 44 stores / 15 devices unchanged. No
migration ran. Receiver HMAC key sha256 `748a99f2…` unchanged. No device was
deleted, re-enrolled or issued a credential. BP untouched, no Store Kit build,
no Broadcast started.

**Still pending:** operator manual acceptance of Deny and Kick on port 8000;
the heartbeat / LISTENING runtime-truth defect; Store to HQ volume telemetry;
dynamic Store targeting.

## Store to HQ volume telemetry - the wiring, not the feature

Web Audience runtime truth is **manually accepted on the real HQ**: Connected,
Listening, Disconnected, Deny/Retry, Kick/Rejoin and cross-Broadcast listener
isolation all confirmed by the operator. That milestone is closed.

### Root cause

HQ could set a Store's master volume and never see a change made at the till.
The first broken hop was the **last** one on the Receiver:

    Core Audio endpoint          EXISTS
    endpoint volume/mute callback EXISTS  (tools/windows_endpoint_observer.py)
    coalescing to one slot        EXISTS
    sequence counter              EXISTS
    endpoint_state message        EXISTS
    the loop that SENDS it        EXISTS - and was never started
    backend validation + runtime  EXISTS  (observe_endpoint_state)
    Console polling and slider    EXISTS  (follows actual_*)

`_endpoint_state_loop` was written, correct, and scheduled by nothing. Each
reading was observed, coalesced into the observer's single slot, and
overwritten by the next one. Every existing test passed throughout, because
they tested the parts and never the wiring.

`create_task` alongside the heartbeat fixes it. The loop also had to stop
returning when the observer is not running: connecting happens before PREPARE,
so that was the state it found on its first pass - it would have ended
immediately and nothing would have started another.

**No backend or frontend change was needed.** Both were already right.

### Contract, unchanged and now actually exercised

- **requested_** = what HQ last asked for. **actual_** = what the endpoint
  reports. The slider follows ACTUAL, falling back to requested only until the
  first reading arrives and while a command is in flight.
- Telemetry updates the ACTUAL fields only and allocates **no command id**, so
  observing 25 after a request of 80 cannot send 80 back. That feedback loop is
  the removed Master Target enforcement and must never return.
- Session-scoped, Store-scoped, sequence-ordered; older, duplicate, wrong-
  session and wrong-Store readings are discarded.
- Runtime state is in memory. **No database write per event** - proved with 500
  readings against the file's mtime.
- The pre-Broadcast restoration snapshot is a separate authority and telemetry
  never touches it. Stop restores the ORIGINAL, never the last observed value.

### Bounds, measured

One gesture is 60 notifications per Store.

| Stores | notifications | messages | coalescing | time | queue depth |
|---|---|---|---|---|---|
| 5 | 300 | 5 | 60x | 0.7 ms | 1 per Store |
| 10 | 600 | 10 | 60x | 1.4 ms | 1 per Store |
| 20 | 1200 | 20 | 60x | 2.7 ms | 1 per Store |
| 40 | 2400 | 40 | 60x | 5.6 ms | 1 per Store |

Linear, and the bound is structural: the observer holds a slot, not a queue.
Where the gesture stopped is always what gets reported - coalescing drops the
journey, never the destination.

### Receiver and Store Kit

Receiver source changed, so: **Receiver 1.5.0, Store Kit 1.8.0**.

| | |
|---|---|
| ZIP | `artifacts/SpeakLink-Store-Kit-1.8.0-81aa464-20260808-083245.zip` |
| SHA-256 | `8af59d31eeb2694b3677eae437ae4c33602838737a6cf072d26e2317499168a9` |
| Size | 124.0 MB, 1065 files |

Frozen executable verified: starts with no traceback, `core audio: reachable
(3 active endpoints)`, `change reports: yes`, and now `agent version: 1.5.0`.

`AGENT_VERSION` was a separate defect found on the way: the packaging script
took a `-Version` and named the package with it, but nothing wrote it into the
agent, so every Receiver ever built announced **1.0.0** to HQ whatever its
package said. HQ stored that as each device's software version, making 1.4.0
and the first build indistinguishable in the fleet list.

**Not installed anywhere.** No Store touched, BP included.

### Still true

No live HQ deployment in this milestone; port 8000 untouched and its bundle
unchanged. Dynamic Store targeting - Add, Pause, Resume, Remove, Zone bulk
actions - remains the next milestone. **No software state equals
SPEAKER_VERIFIED**: an endpoint level is control truth, not proof anybody heard
anything.

## Two-way Store volume: PHYSICALLY ACCEPTED on BP

The operator physically tested the whole loop on BP and it passed: HQ to BP
Windows master, BP local change back to HQ, no force-back, mute and unmute both
ways, HQ able to take control again after a local change, and Stop restoring
the exact pre-Broadcast state.

Baseline is now **Receiver 1.5.0 / Store Kit 1.8.0**.

## Dynamic single-Store targeting: audited, and BLOCKED on two design conflicts

Branch `feature/dynamic-store-targeting`. Phase 1 (audit) is complete; no
mutation code was written, because the audit found two guarantees in the
requested contract that the current architecture cannot express without a
Receiver change - and the instruction was to surface exactly that before
weakening anything.

### What exists

| | |
|---|---|
| Target model | `broadcast_targets`, one row per (session, store), `backend/models.py:181` |
| Lease | `broadcast_store_leases` + partial unique index on `(store_id) WHERE released_at IS NULL`, atomic all-or-nothing acquire, `backend/broadcast_reservation.py:162` |
| Per-Store queue | `StoreAudioQueue`, bounded at 24 chunks (~6 s), drop-oldest with counters, `backend/audio_streaming.py:56` |
| Fanout | `AudioFanout.start_store` / `stop_store` already exist, `backend/audio_streaming.py:200` |
| Receiver protocol | `prepare`, `play`, `stop`, `set_audio_control`; acks carry `session_id` and are validated against the active session |
| Late-join | Exists for the WEB path only: `WebmStreamFramer` + `WebAudienceRelay` cached init segment and Cluster ring |
| RBAC | `broadcast.store_delivery` + Store Scope, single choke point `_require_physical_delivery` |

### What does not exist

No per-target generation. No per-Store lease release. No per-Store
enable/disable in fanout (`target_store_ids` is a frozenset fixed at start). No
add/pause/resume/remove operation of any kind - `backend/server.py:2850` states
the set is frozen for the life of the broadcast by design.

### BLOCKER 1 - the Receiver cannot be paused and resumed

`tools/audio_receiver_pilot.py:808` - `stop` is **terminal**:

```
elif kind == "stop":
    await self._on_stop(connection, payload)
    return          # leaves _session_loop; run() then closes the socket
```

`_on_stop` closes the decoder, the PCM sink and the queue and restores the
Windows endpoint. There is no primitive that stands a Receiver down for one
participation segment and brings it back inside the same live session.

Pause as specified needs exactly that: stop this Store's playback, restore its
Windows state, leave its mixer alone, and later rejoin at the live edge. Simply
not sending audio from HQ does not satisfy it - the decoder stays open, the
endpoint stays under SpeakLink control, and the mixer is not left alone.

**A new Receiver primitive is genuinely required**, and with it Receiver 1.6.0
and Store Kit 1.9.0 - which this milestone's instruction pre-emptively
discouraged. That is the decision to take.

### BLOCKER 2 - per-participation-segment restoration cannot be expressed

The endpoint snapshot is captured once, in `_on_prepare`, keyed by session id,
into a **single-slot record file per install**
(`tools/audio_receiver_pilot.py:916`, `tools/windows_endpoint_restore.py:68`).

The requested contract needs a NEW baseline captured on every Resume, so that a
change the Store made while paused is not overwritten hours later by a value
from before the Broadcast began. That is a second snapshot lifecycle in the
Receiver, not a backend change.

### NOT a blocker, but real work

**Store late-join.** Stores receive raw broadcaster bytes with no framing -
`backend/server.py:5287` fans out the untouched socket bytes, and the framer
runs only on the web path afterwards. Correctness today rests entirely on every
Store being prepared before the first byte. A Store added mid-Broadcast would
receive a headerless mid-stream run and its FFmpeg would have nothing to
decode. This is fixable **HQ-side** by reusing `WebmStreamFramer` and the
cached init segment for Store queues, with no Receiver change.

**Per-Store lease release** is safe to add as a `(session_id, store_id)`
release. The existing docstring warns against a `store_id`-only release, which
is a different and genuinely dangerous thing.

### Pre-existing defect found during the audit

`backend/broadcast_runtime.py:248` plus `backend/audio_streaming.py:230`:
`_started_stores` is add-only and a Store's pump exits permanently on its first
send failure. A Receiver that drops and reconnects mid-Broadcast is back in the
target set but never gets a new pump - its queue keeps accepting and
drop-oldest-ing with no consumer, and HQ still sends it a `play`. Any Add or
Resume lands directly on this code path and should fix it.

### Consequence

**Add alone can be built with no Receiver change** (late-join framing, lease,
generation, fanout mutation). **Pause, Resume and Remove-with-restoration
cannot.** Zone bulk actions remain out of scope and untouched.
