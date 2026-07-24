# EchoCast Receiver HMAC Key Custody Policy

Status: design for review; not configured or approved for production use

This document does not authorize or execute a production cutover. No real
secret is included, generated, loaded, or configured by this policy. Final
hosting, secret storage, operator identities, and approval systems remain
deployment decisions.

## Key purpose

Receiver HMAC keys protect credential hashes stored in SQLite. A key lets the
backend verify a presented Receiver credential against its stored HMAC digest.
It is not itself a Receiver credential and cannot identify a Store without the
corresponding database records.

Keys must remain separate from SQLite and Git. Code, database rows, audit
metadata, logs, documentation, shell history, test output, and connection
inventory must never contain key bytes. A database copy and its HMAC keys are
two different high-value assets and require separate custody.

## Key versioning

- Key versions are positive, non-boolean integers.
- Every `receiver_credentials` row records its exact `hash_key_version`.
- Multiple versions may coexist only during a controlled issue or rotation
  window.
- Unknown key versions fail closed. No verifier may silently try a different
  version as a substitute for the row's declared version.
- The deployed key ring must be bounded and contain only reviewed versions.
- Version numbers are routing metadata, not secrets. Key bytes remain secret.
- Retiring a version requires proof that no usable credential row references
  it and that rollback requirements have expired or moved to a retained,
  separately controlled recovery artifact.

## Storage choices

The final platform has not been selected. The deployment review must choose one
approved Windows-compatible mechanism; the application must not silently read
an unreviewed fallback.

### Storage decision matrix

| Choice | Security | Operational complexity | Backup/recovery | Rotation | Access auditing | Windows compatibility | Single-server pilot suitability |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Deployment mechanism injects a service-process secret | Strong when injection and service identity are controlled; weak if inherited by unrelated processes | Medium | Requires a separate encrypted recovery export and documented reinjection | Usually straightforward but service restart may be required | Depends on deployment platform | Good when the service manager supports protected secret injection | Good if access and recovery are independently reviewed |
| Windows-protected secret file or credential store restricted to the backend service identity | Strong when ACLs, encryption, service identity, and backup handling are correct | Medium to high | Requires an encrypted, access-controlled backup outside the database set | Requires atomic protected replacement and restart/reload planning | Windows auditing can record access and ACL changes | Native and practical | Strong candidate, but the exact store and identity must be selected later |
| Managed external secret service | Strong centralized policy, rotation, and audit potential | High for a local pilot; depends on network and platform availability | Provider-specific recovery and break-glass procedure | Usually strongest lifecycle support | Usually strongest centralized audit | Depends on future hosting and connectivity | Appropriate only if the hosting platform already supports it reliably |

Selection criteria must include service-start behavior when the secret service
is unavailable, least-privilege access, audited recovery, rotation rollback,
and whether operators can validate version presence without reading key bytes.

## Access-control roles

Named people or approved service identities must fill these roles in the change
record. Separation should be used where practical; one person must not silently
perform every high-risk step.

| Role | Responsibility | Must not do alone |
| --- | --- | --- |
| Security approver | Approves storage mechanism, versions, access list, rotation, compromise response | Inject a new production key and approve their own action |
| Deployment operator | Deploys the reviewed artifact and configures the approved key reference | Display, export, or independently approve key material |
| Database backup operator | Creates and verifies the database/WAL/SHM backup set | Place HMAC keys inside the database backup |
| Cutover observer | Records counts, states, source inventory, stop conditions, and outcomes | Change state without the authorized operator |
| Rollback approver | Owns the go/no-go and rollback decision | Assume successful authentication proves audio health |

Broad HQ API authorization is outside this document. Production procedures
must identify the operator, approver, observer, ticket, UTC time, and approved
scope without recording credentials.

## Key handling

- Never print or log key bytes.
- Never place keys directly in command arguments or shell history.
- Never place keys in documentation, tickets, chat, source control, SQLite,
  audit metadata, crash reports, or test fixtures that could reach Git.
- If a redacted comparison identifier is required, use an approved one-way
  fingerprint and label it as a fingerprint, not a key.
- Validate that `<KEY_VERSION>` is available to the backend service without
  displaying its bytes.
- Use an approved protected channel for initial provisioning and recovery.
- Disable command echo and transcript capture only through a reviewed operator
  procedure; do not improvise secret handling at the prompt.
- Avoid the clipboard. If the approved mechanism requires it, restrict the
  workstation and session, minimize dwell time, clear it through the approved
  procedure, and record that handling occurred without recording the value.
- Avoid temporary plaintext files. If an approved mechanism requires a staging
  file, create it only in a protected location with service-identity ACLs,
  delete it through the approved secure procedure after verification, and
  confirm it was not backed up, indexed, or committed.
- Key presence checks report only version, availability, and approved
  fingerprint—not bytes.

## Key backup and restore rehearsal

Key backup must be encrypted and access-controlled. It must remain separate from the database backup.
Database/WAL/SHM backup operators should not automatically receive key
backup access. Both assets are required for complete recovery:

- Database backup without key backup preserves hashes that cannot be verified.
- Key backup without database backup lacks credential, Device, Store, version,
  revocation, and migration-state records.

The recovery record must identify the authorized key-backup location, version
inventory, encryption/custody owner, restore approver, and last successful
rehearsal. It must not contain key material.

A restore rehearsal uses an isolated copied database at
`<APPROVED_DATABASE_COPY>`, never the application default database path. An
authorized process supplies the restored key version through the selected
secret mechanism. The rehearsal verifies a controlled non-secret check or
generated rehearsal credential, records success/failure and version only, then
removes the isolated environment through an approved cleanup procedure.

## Key loss and compromise

### Loss

Losing a key makes credentials for its versions unverifiable. Do not attempt to
derive the key from stored HMACs or raw Store tokens. Stop narrowing transitions,
preserve database state, locate the authorized backup, and invoke the recovery
owner. If recovery is impossible, affected Receiver credentials require
controlled replacement.

### Compromise

Exposure weakens protection substantially if an attacker also obtains the
database. Treat suspected exposure as an incident:

1. Stop key distribution and credential lifecycle changes.
2. Preserve secret-free evidence and identify affected versions.
3. Restrict access to database and key backups.
4. Obtain security and rollback approval.
5. Add a new version through the approved mechanism.
6. Reissue or rotate affected credentials under controlled Store cohorts.
7. Retire the compromised version only when no usable rows depend on it and
   recovery policy permits removal.

Do not regenerate every Receiver credential automatically. Active socket
disconnect, Receiver replacement delivery, and operational rollback require a
separate approved plan.

## Rotation design

Rotation is designed here but not implemented.

1. Approve a new positive `<KEY_VERSION>` and provision its key through the
   selected custody mechanism.
2. Verify version presence without displaying bytes.
3. Make new credentials use the new version.
4. Retain required prior versions temporarily so existing credentials and
   rollback remain usable.
5. Replace credentials or rehash only when a raw credential is presented and
   successfully verified. Stored hashes cannot be converted directly into a
   new-key hash.
6. For planned credential replacement, respect the approved maximum 15-minute
   grace period. Compromise replacement has no grace.
7. Audit version addition, credential replacement, grace decision, rollback,
   and version retirement without key bytes, raw tokens, or token hashes.
8. Remove the old key only after a query proves no usable credential row
   requires it, the observation period has passed, and rollback/recovery owners
   approve.

Rotation rollback restores the prior key-ring configuration only while the old
version and its credentials remain approved and uncompromised. A compromised
key must not be restored merely to make authentication succeed.

## Validation record

For each deployment or rehearsal, record only:

- change ticket and UTC timestamp
- approved key versions and redacted fingerprints if policy permits
- secret mechanism name, not its secret locator when that locator is sensitive
- service identity and least-privilege review result
- database backup record and separate key-backup record
- isolated restore rehearsal result
- counts of credential rows by key version
- approver, operator, observer, and rollback owner

No real secret is included in this policy or its validation record.
