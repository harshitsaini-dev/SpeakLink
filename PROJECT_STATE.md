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
