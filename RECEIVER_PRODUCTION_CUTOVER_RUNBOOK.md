# EchoCast Receiver Credential Production Cutover Runbook

Status: review-only plan; no production authorization

This document does not authorize or execute a production cutover. It does not
authorize a migration, backup, key load, Receiver connection, or authentication
change. Every production use requires a separately approved change record,
named operators, verified artifacts, maintenance window, and rollback owner.

The default EchoCast application remains legacy Store-token-only. The current
migration-aware authenticator, transition service, connection inventory, and
cutover coordinator are isolated infrastructure. No public cutover route,
automatic startup transition, or production HMAC-key configuration exists.

## Scope and invariants

The only reviewed transition path in this runbook is:

```text
backfilled -> dual_verify -> hash_only
hash_only -> dual_verify -> backfilled
```

`backfilled -> dual_verify` and `hash_only -> dual_verify` expand accepted
credential paths. `dual_verify -> hash_only` narrows acceptance and requires a
fresh summary with `legacy_authenticated_count = 0`. `dual_verify -> backfilled`
also narrows acceptance and requires a fresh summary with
`hashed_authenticated_count = 0`.

Changing migration state does not re-authenticate, relabel, or disconnect an
existing socket. A source change requires disconnect, authentication under the
new state, and a new connection ID. Exactly one Uvicorn worker is mandatory
because the active connection inventory is process-local.

Authentication success does not prove audible speaker output. CONNECTED,
heartbeat freshness, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED, and
SPEAKER_VERIFIED are independent evidence.

## Responsibilities and records

Before scheduling, name the security approver, deployment operator, database
backup operator, cutover observer, rollback approver, pilot Store owner, and
incident contact. Follow `RECEIVER_HMAC_KEY_CUSTODY.md`; never include a key,
token, hash, Authorization header, or password in the change record.

Every future operational command must be copied into the approved change
record using this format before execution:

| Field | Required content |
| --- | --- |
| Run From | Approved host, service identity, and directory |
| Exact Command or safe placeholder | Reviewed command with non-executable placeholders |
| Purpose | One bounded objective |
| Important arguments | Meaning and expected scope of each argument |
| Expected output | Secret-free success signal and counts |
| Common error | Safe failure interpretation and escalation |
| Safety level | Read-only, state-changing, or service-control |
| Mutation | Explicitly read-only or state-changing |

Examples below are specifications, not commands ready for execution. Values
such as `<APPROVED_DATABASE_COPY>`, `<APPROVED_BACKUP_DIRECTORY>`,
`<KEY_VERSION>`, and `<MAINTENANCE_OPT_IN>` must be resolved in a reviewed
change record and must never default to the live database.

## Phase A - Authorization and scheduling

1. Create a named change ticket/deployment record.
2. Record approved operator, independent observer, and rollback decision owner.
3. Define exact pilot scope: one named Store only.
4. Schedule a maintenance window with business and emergency-broadcast owners.
5. Define communication channels, update intervals, and incident escalation.
6. Record automatic rollback triggers and who may invoke them.
7. Confirm no all-Store deployment is authorized.

## Phase B - Preflight

Record these items without selecting or displaying credentials:

- exact approved Git commit and branch/tag
- verified clean deployment artifact and dependency lock/material
- Python, FastAPI, SQLAlchemy, Uvicorn, and OS versions
- exactly one Uvicorn worker
- current migration state and `legacy_verification_enabled` flag
- total, active, and inactive Store counts
- Receiver Device and Credential counts by non-secret status, format, and key
  version
- clean SQLite integrity and foreign-key checks
- available disk capacity for database/WAL/SHM backup plus restore copy
- required HMAC key versions available without displaying key bytes
- active Receiver connection inventory and fresh source counts
- broadcast session stopped and no active emergency announcement
- EchoGuard/acoustic-verifier interaction risk and owner
- approved rollback checkpoint accessible to the rollback operator

If any count cannot be reconciled or any output includes a secret, stop.

### Preflight command specification

| Field | Value |
| --- | --- |
| Run From | Isolated maintenance workspace on the approved backend host |
| Exact Command or safe placeholder | `<READ_ONLY_PREFLIGHT_TOOL> --database <APPROVED_DATABASE_COPY> --key-version <KEY_VERSION>` |
| Purpose | Report ledger, state, counts, integrity, foreign keys, and key-version presence |
| Important arguments | Copy path must not equal the default database; key version is metadata only |
| Expected output | Approved commit, one worker, clean checks, reconciled counts, redacted key availability |
| Common error | Unknown state, failed check, missing version, or mismatched counts means stop |
| Safety level | Read-only |
| Mutation | Read-only; must not start the application or change SQLite |

## Phase C - Stop and backup

Follow this exact safety order during an approved future window:

1. Stop accepting new HQ broadcasts.
2. Stop Uvicorn cleanly.
3. Confirm the backend process exited.
4. Confirm no process is actively writing SQLite.
5. Capture the database file and any present WAL and SHM files as one
   consistent backup set in `<APPROVED_BACKUP_DIRECTORY>`.
6. Preserve file metadata and record approved cryptographic checksums.
7. Back up HMAC keys separately from the database backup using the approved
   secret process.
8. Confirm authorized recovery operators can locate and access both backup
   records without displaying their contents.
9. Do not delete the original database.
10. Do not use destructive Git or filesystem cleanup as part of backup or
    recovery.

### Service-stop command specification

| Field | Value |
| --- | --- |
| Run From | Approved service-control console |
| Exact Command or safe placeholder | `<APPROVED_SERVICE_STOP_COMMAND>` |
| Purpose | Stop the one-worker backend cleanly before backup |
| Important arguments | Exact service identity and expected process ID |
| Expected output | Service stopped and no database writer remains |
| Common error | Timeout or remaining writer means abort; do not force cleanup blindly |
| Safety level | Service-control |
| Mutation | Changes service state, not database contents |

## Phase D - Backup verification

Restore the database/WAL/SHM backup into a separate authorized location. Never
point the application default database path at the copy.

1. Run SQLite integrity and foreign-key checks against
   `<APPROVED_DATABASE_COPY>`.
2. Confirm Store IDs and total/active/inactive counts.
3. Confirm `Store.receiver_token` values remain present before any future
   neutralization, recording only count and validity—not values.
4. Confirm `schema_migrations`, migration state, and flag.
5. Verify required HMAC key versions through approved non-secret test checks.
6. Record backup set checksum, isolated copy identity, checks, counts, and
   authorized reviewers.
7. Keep the verified backup and separate key backup through the pilot and
   rollback-retention period.

### Restore-verification command specification

| Field | Value |
| --- | --- |
| Run From | Isolated recovery workspace |
| Exact Command or safe placeholder | `<READ_ONLY_RESTORE_CHECK_TOOL> --database <APPROVED_DATABASE_COPY>` |
| Purpose | Verify restored integrity, foreign keys, ledger, state, and counts |
| Important arguments | Explicit isolated copy only; no default path |
| Expected output | `ok` checks and reconciled non-secret counts |
| Common error | Any failed check or unknown state blocks migration |
| Safety level | Read-only |
| Mutation | Read-only |

## Phase E - Additive schema migration

This phase is not executed by this document.

1. Require explicit `<MAINTENANCE_OPT_IN>` and the exact approved Phase 1
   migration version.
2. Do not casually bypass the protected-database guard.
3. Run the explicit migration runner in one transaction; do not substitute
   `Base.metadata.create_all`.
4. Validate tables, indexes, constraints, foreign keys, ledger, and
   `legacy_only` state after commit.
5. Run the idempotency check through the reviewed runner.
6. If any validation fails, stop and restore the verified backup under the
   rollback procedure.

### Migration command specification

| Field | Value |
| --- | --- |
| Run From | Approved maintenance artifact directory with backend stopped |
| Exact Command or safe placeholder | `<APPROVED_PHASE_ONE_RUNNER> --database <APPROVED_DATABASE_TARGET> --maintenance-opt-in <MAINTENANCE_OPT_IN>` |
| Purpose | Apply only the reviewed additive Receiver credential schema |
| Important arguments | Exact target, exact migration version, explicit maintenance authorization |
| Expected output | One applied ledger version, `legacy_only`, validation counts |
| Common error | Guard refusal, partial schema, or failed validation requires rollback; do not retry blindly |
| Safety level | State-changing |
| Mutation | Transactional schema and initial state change |

## Phase F - Legacy backfill

This phase is not executed by this document.

- Validate the complete Store fleet and every legacy token before writes.
- Map each active Store to one active Device.
- Map each inactive Store to one disabled Device with a UTC `disabled_at`.
- Create one hash-only legacy credential per Store, using its declared
  `hash_key_version`; never copy raw values into new tables or audit metadata.
- Preserve `Store.receiver_token` byte-for-byte.
- Abort the complete transaction if any Store token is invalid.
- Change state to `backfilled` only after counts, relationships, hashes,
  foreign keys, Store snapshots, and ledger are validated.
- Keep `legacy_verification_enabled = 1`.
- Expect two secret-free creation events per Store plus one state-change event.
- Create and verify a new backup checkpoint after successful validation.

Required reconciliation: Device count equals Store count; credential count
equals Store count; active/inactive mappings match; Store IDs, tokens,
operational fields, and `schema_migrations` remain unchanged.

## Phase G - Controlled dual verification pilot

1. Start the reviewed backend artifact with exactly one Uvicorn worker.
2. Explicitly configure the approved migration-aware authenticator and bounded
   key ring. Do not use a hidden environment toggle or implicit key discovery.
3. Confirm the persisted state is `backfilled` before transition.
4. Transition `backfilled -> dual_verify` through the controlled operator
   procedure. This expands acceptance and needs no connection drain.
5. Select one pilot Store and intentionally reconnect its Receiver.
6. Confirm the inventory records the canonical authentication source.
7. Verify CONNECTED separately from READY.
8. Verify AUDIO_RECEIVING separately from PLAYBACK_CONFIRMED.
9. Verify actual speaker output only through EchoGuard or another trusted
   acoustic path as SPEAKER_VERIFIED.
10. Observe authentication errors, connection freshness, reconnects, resource
    use, and redacted logs for the approved pilot observation duration.
11. Apply automatic rollback triggers immediately when met.

No client may choose its authentication source. State and consistent server
verification determine it. A state transition does not relabel existing
sockets.

## Phase H - Hash-only readiness

Do not proceed to `hash_only` until all are true:

- every active Store has at least one eligible active Device
- every required active Device has a usable credential
- every referenced HMAC key version is available and approved
- database integrity and foreign-key checks remain clean
- `legacy_authenticated_count = 0`
- the atomic connection summary is fresh under the reviewed 30-second policy
- no active Receiver socket depends on `legacy_store_token`
- rollback approval, verified backup, and separate key backup remain available
- pilot results and observation duration are accepted

Existing legacy-source sockets must disconnect and re-authenticate. The state
change itself does not disconnect them.

## Phase I - Hash-only transition

1. Stop or control new Receiver connection attempts during the transaction.
2. Capture one fresh atomic inventory summary.
3. Transactionally transition `dual_verify -> hash_only`.
4. Confirm `state = hash_only` and `legacy_verification_enabled = 0`.
5. Confirm exactly one secret-free transition audit event was appended.
6. Confirm Store, Device, Credential, and `schema_migrations` rows are unchanged.
7. Restart or intentionally reconnect only the selected pilot Receivers when
   the approved procedure requires it.
8. Confirm raw-only Store-token authentication fails and approved hash-backed
   authentication succeeds.
9. Monitor reconnect failures and all independent health axes.
10. Do not proceed fleet-wide immediately.

## Phase J - Rollback

### hash_only -> dual_verify

This expands accepted paths. It is allowed only while strict raw Store tokens
remain valid, raw storage has not been neutralized, mappings remain complete,
and rollback is approved. The transaction restores
`legacy_verification_enabled = 1`. Existing sockets retain their sources.

### dual_verify -> backfilled

This narrows acceptance by disabling hashed authentication. It requires a fresh
summary with `hashed_authenticated_count = 0`. Disconnect or intentionally
re-authenticate hashed-source sockets under the applicable rollback state
before transition. Hashed rows remain stored.

### Restore from backup

Use the approved database/WAL/SHM checkpoint and separate key backup when
schema or data validation fails, a required key is missing, state is unknown,
data is partially migrated, or transaction integrity cannot be trusted. Stop
the backend, preserve current artifacts, and follow the authorized restore
record. Never attempt to reconstruct raw credentials from hashes.

Rollback that restores authentication does not prove Receiver audio health or
audible speaker output.

## Phase K - Post-cutover validation

Do not combine these checks into one “online” or “healthy” result.

### Authentication

- Receiver identity succeeds under the intended source.
- Inventory records the correct canonical source.
- No unexpected legacy-source connections remain before hash-only.

### Connection health

- CONNECTED is present for the current connection ID.
- Heartbeat freshness remains inside policy.
- Reconnect time and replacement cleanup meet the pilot threshold.

### Readiness

- READY is reported independently.
- Required software, output-device, and FFmpeg availability checks pass.

### Playback

- AUDIO_RECEIVING is observed for the matching session.
- PLAYBACK_CONFIRMED follows the software pipeline acknowledgement.

### Acoustic

- SPEAKER_VERIFIED comes only from EchoGuard/trusted acoustic verification.
- Amplifier and physical speaker confirmation follow the operational procedure.

### Operational

- CPU, RAM, network, queue growth, dropped audio, and reconnect errors remain
  within approved thresholds.
- Database integrity, foreign keys, migration state, and audits remain valid.
- Logs are reviewed for redaction and contain no credentials or key material.

Authentication success does not prove audible speaker output.

## Phase L - Expansion policy

Each stage requires its own approved observation period, acceptance record, and
rollback criteria. A failed stage returns to the last accepted cohort.

Stage 1: 1 Store
Stage 2: 3 Stores
Stage 3: 5 Stores
Stage 4: 10 Stores
Stage 5: remaining Stores

No direct all-40-Store cutover is permitted. Cohort expansion stops on any
authentication regression, connection-source mismatch, readiness/playback/
acoustic failure, secret exposure, integrity issue, or unresolved rollback
risk.

## Raw-token neutralization exclusion

`raw_neutralized is not part of the first production pilot`. It requires a
separate reviewed migration, new authorization, verified backup, complete
hash-backed Receiver evidence, and proven HMAC-key recovery. Raw tokens cannot be reconstructed from hashes.
Neutralization is irreversible without a verified backup, and rollback expectations change after it.

This runbook contains no command that neutralizes raw tokens. Do not begin that
phase until every active Receiver is proven hash-backed and the destructive
scope has an independently reviewed recovery plan.

## Do not proceed

Stop before the next operation if any item applies:

- backup or isolated restore is not verified
- required HMAC key is unavailable, incorrect, unapproved, or the wrong version
- database integrity or foreign-key check fails
- Store, Device, Credential, ledger, or migration-state counts disagree
- migration state/flag is unknown or inconsistent
- deployment commit, artifact, operator, observer, or ticket is unapproved
- more than one Uvicorn worker is configured
- a fresh source count blocks a narrowing transition
- pilot Receiver cannot reconnect or has an unexpected source
- a secret appears in logs, output, tickets, or audit metadata
- Receiver is CONNECTED but READY, playback, or acoustic verification fails
- EchoGuard overlap or emergency-announcement risk is unresolved
- rollback owner or required backup operator is unavailable
- broadcast session is active or emergency announcement is in progress

## Emergency abort

1. Stop the transition operation.
2. Do not retry blindly.
3. Prevent new cutover actions while preserving current sockets unless the
   approved incident plan requires a controlled disconnect.
4. Preserve redacted logs and record current migration state, flag, ledger,
   Store/Device/Credential counts, and connection-source counts.
5. Do not delete the database, WAL, or SHM files.
6. Do not regenerate all Receiver credentials automatically.
7. Use the approved rollback checkpoint and separately held key backup.
8. Escalate to the named operator, security approver, observer, and rollback
   owner.
9. Resume broadcasts only after authentication and each independent audio-state
   check has been reviewed.

## Prohibited shortcuts

Do not prescribe or use hard reset, untracked-file cleanup, force push,
database deletion, raw-token neutralization, automatic fleet credential
regeneration, or `Base.metadata.create_all` as migration recovery. Protected
database guards require a reviewed maintenance mechanism; they are not errors
to bypass casually.
