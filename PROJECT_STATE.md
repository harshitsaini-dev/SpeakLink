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
