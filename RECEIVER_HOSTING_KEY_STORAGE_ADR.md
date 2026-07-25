# ADR: Receiver Production Hosting, Service Identity, and HMAC Key-Storage Baseline

## 1. Metadata

- Title: Receiver production hosting, service identity, and HMAC key-storage baseline
- Status: Proposed for pilot approval
- Decision owners: Security approver, Operations approver, Product owner (named at approval time; no names are invented by this document)
- Date: 2026-07-25
- Scope: Backend hosting model, operating-system/service execution model, dedicated service identity, HMAC-key storage mechanism, database location and permissions, backup/recovery ownership, TLS/WSS responsibility, and the one-Uvicorn-worker operational boundary for the initial EchoCast Receiver pilot.
- Related documents: `RECEIVER_CREDENTIAL_LIFECYCLE.md`, `RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md`, `RECEIVER_HMAC_KEY_CUSTODY.md`, `RECEIVER_STATUS_CONTRACT.md`, `RECEIVER_SECURITY_OPERATIONS_REVIEW.md`
- Superseded decisions: none

This ADR selects and documents a provisional pilot decision. It does not implement, configure, or execute it. No production HMAC key, service account, TLS configuration, or database operation is created, generated, loaded, or performed by this document.

## 2. Context

EchoCast AI is a live retail announcement system for approximately 40 Stores. HQ browsers reach a central FastAPI service over a React dashboard; Store Receivers reach the same service over a secure Receiver WebSocket. The verified baseline uses SQLite for persistence and holds all live WebSocket/connection state in process memory (`backend/ws_manager.py`, `backend/receiver_connection_inventory.py`), which is why the backend must run with exactly one Uvicorn worker until a shared, authoritative coordination design exists.

The repository already contains isolated, test-only infrastructure for a future hashed-credential cutover (`backend/receiver_auth_service.py`, `backend/receiver_migration_transition_service.py`, `backend/receiver_credential_backfill.py`, `backend/receiver_cutover_rehearsal.py`) and a review-only `RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md`. None of this is wired into the default application; the normal app still authenticates only `Store.receiver_token` (`backend/receiver_runtime_auth.py`). No production cutover has occurred.

The cutover runbook and `RECEIVER_HMAC_KEY_CUSTODY.md` already assume a future HMAC key must be recoverable, backed up separately from the database, and available to the backend service without ever being placed in Git, SQLite, or plaintext `.env`. HTTPS/WSS termination is required before any public deployment. The pilot must roll out to a controlled cohort of Stores (1, then 3, 5, 10, then the remainder), not all 40 at once. This ADR selects the provisional hosting, service-identity, and key-storage baseline that the rest of that design already assumes but has not yet named.

## 3. Decision drivers

- Security: least-privilege execution and secret isolation from Git, SQLite, and application source.
- Beginner-operable maintenance: the pilot must be operable without specialized infrastructure staff.
- Windows compatibility: the verified baseline, FFmpeg dependency, and existing tooling are Windows-first.
- Secret isolation: HMAC keys, TLS keys, JWT secrets, and Receiver credentials must never share custody.
- Backup and restore: database and key backups must be independently verifiable and recoverable.
- Auditability: access to secrets and state changes must be attributable to a role.
- Low operational complexity: appropriate for a single-host pilot, not a multi-region platform.
- Single-worker correctness: the chosen host/process model must preserve the one-Uvicorn-worker invariant.
- SQLite safety: the model must not introduce concurrent writers or network-exposed database access.
- Rollback ability: every choice should have a documented, non-destructive way back.
- Cost awareness without unsupported price claims: this ADR does not cite vendor cost figures.
- Future migration path: the choice should not foreclose a later move to Linux, a managed platform, or an external secret service.
- Approximately 40-Store scale: the design is sized for a small controlled fleet, not internet-scale load.

## 4. Options considered

### Hosting

- A. Dedicated Windows Server or Windows VM at HQ or an approved hosted environment.
- B. Dedicated Linux VM.
- C. Managed application/container platform.
- D. Developer workstation or shared office PC.

### Service identity

- A. Dedicated local Windows service identity.
- B. Domain-managed service account where available.
- C. Personal administrator account.
- D. LocalSystem or another highly privileged built-in identity.

### HMAC-key storage

- A. DPAPI-protected versioned secret file/container.
- B. Plaintext .env file.
- C. Windows Credential Manager or equivalent service-accessible credential facility.
- D. External managed secret service.
- E. SQLite database or source-code constant.

## 5. Decision matrix

Scores use a simple 1-5 scale (5 = best fit for this pilot). These are qualitative estimates for a single-host, ~40-Store pilot, not a scientific benchmark; they exist to make the reasoning transparent, not to replace judgment.

### Hosting

| Option | Secret protection | Least privilege | Windows compatibility | Operational simplicity | Backup/recovery clarity | Rotation support | Auditability | One-worker pilot suitability | SQLite compatibility | Future portability | Failure recovery |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A. Dedicated Windows Server/VM | 4 | 4 | 5 | 4 | 4 | 3 | 4 | 5 | 5 | 3 | 4 |
| B. Dedicated Linux VM | 4 | 4 | 2 | 3 | 4 | 3 | 4 | 5 | 5 | 4 | 4 |
| C. Managed application/container platform | 4 | 3 | 3 | 2 | 3 | 4 | 4 | 3 | 2 | 5 | 3 |
| D. Developer workstation/shared PC | 1 | 1 | 5 | 5 | 1 | 1 | 1 | 3 | 4 | 1 | 1 |

### Service identity

| Option | Secret protection | Least privilege | Windows compatibility | Operational simplicity | Auditability | Failure recovery |
| --- | --- | --- | --- | --- | --- | --- |
| A. Dedicated local Windows service identity | 4 | 5 | 5 | 4 | 4 | 4 |
| B. Domain-managed service account | 4 | 5 | 4 | 3 | 5 | 4 |
| C. Personal administrator account | 1 | 1 | 5 | 4 | 1 | 1 |
| D. LocalSystem/highly privileged built-in | 2 | 1 | 5 | 4 | 2 | 3 |

### HMAC-key storage

| Option | Secret protection | Least privilege | Windows compatibility | Operational simplicity | Backup/recovery clarity | Rotation support | Auditability | One-worker pilot suitability | SQLite compatibility | Future portability | Failure recovery |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A. DPAPI-protected versioned secret file/container | 4 | 4 | 5 | 3 | 4 | 3 | 3 | 5 | 5 | 3 | 3 |
| B. Plaintext .env file | 1 | 2 | 5 | 5 | 2 | 2 | 1 | 4 | 5 | 4 | 2 |
| C. Windows Credential Manager or equivalent | 4 | 4 | 5 | 3 | 3 | 3 | 3 | 5 | 5 | 3 | 3 |
| D. External managed secret service | 5 | 4 | 3 | 2 | 4 | 5 | 5 | 3 | 3 | 5 | 4 |
| E. SQLite database or source-code constant | 1 | 1 | 5 | 5 | 1 | 1 | 1 | 5 | 1 | 2 | 1 |

## 6. Selected decision

Unless a future inspection finds a blocking contradiction, the provisional pilot baseline is:

- Dedicated supported Windows Server/VM at HQ or an approved hosted environment. Not a developer laptop; not a direct all-Store deployment.
- One Uvicorn worker for the FastAPI/Uvicorn process, matching the existing process-local connection-inventory boundary.
- Dedicated non-admin Windows service identity used only for EchoCast backend execution, with interactive sign-in disabled where practical, and with only the filesystem, database, log, and secret access EchoCast requires. A domain-managed service account may replace it later if the approved environment supports one.
- DPAPI-protected versioned HMAC-key container outside Git and SQLite, restricted by ACL to the dedicated service identity and explicitly approved recovery operators. Only non-secret key-version metadata lives in normal application configuration.
- Separate encrypted key backup, distinct from the database backup, with its own recovery owner.
- Local SQLite with strict filesystem permissions and verified backups, remaining local to the backend host for this controlled pilot.
- HTTPS/WSS termination through a separately approved reverse-proxy or Windows-compatible HTTPS termination layer; no public database port.
- No multi-worker deployment.

## 7. Rejected options

- Developer workstation or shared office PC: fails secret protection, backup discipline, and failure recovery; convenient but not appropriate for any production pilot.
- Personal administrator account as the service identity: fails least privilege and auditability, and ties production execution to an individual's account lifecycle.
- LocalSystem by default: broader privilege than EchoCast needs; acceptable only later if a documented security review finds a specific requirement.
- Plaintext production key in `.env`: fails secret protection entirely if the host or a backup is exposed; explicitly rejected by `RECEIVER_HMAC_KEY_CUSTODY.md`.
- A key stored in SQLite: destroys the separation between "database compromise" and "key compromise" that the credential design depends on.
- A hard-coded key: cannot be rotated, versioned, or audited, and would ship secret material inside source control.
- Multiple Uvicorn workers: breaks the process-local connection inventory and one-current-connection-per-Store invariant documented in `RECEIVER_CREDENTIAL_LIFECYCLE.md`.
- Immediate managed-platform migration: adds operational complexity not justified at the current ~40-Store pilot scale; remains a valid future option.
- Immediate all-Store cutover: contradicts the staged 1/3/5/10/remaining rollout already defined in `RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md`.

None of these alternatives are forbidden permanently; they are not selected for this pilot's current scale and review state.

## 8. Consequences

Positive:

- A controlled, well-understood operational model for a single approved host.
- Clear ownership of the service identity and its access.
- Database and key material remain separable assets, matching the existing credential-lifecycle design.
- Compatible with the current one-Uvicorn-worker, process-local connection-inventory boundary.
- Lower complexity than a managed platform for a pilot of this size.
- Easier rollback because state lives in one host's SQLite file and one key container.

Negative:

- Host-specific DPAPI dependency makes cross-host key export/import a deliberate, reviewed step rather than a file copy.
- Single-host availability limitation: no built-in high availability.
- Manual recovery procedures for both database and key restoration.
- SQLite growth/concurrency limitations if the pilot scales past the initial cohort sizes.
- Multi-worker expansion is not supported without further architectural work.
- Ongoing service-identity and ACL administration is required.

## 9. Security boundaries

- The Receiver HMAC key is not a Receiver token; it only lets the backend verify stored credential hashes.
- The database plus the key together are more sensitive than either alone.
- The database alone does not provide direct raw-credential recovery once raw storage is neutralized.
- The key alone is insufficient without the corresponding credential hash rows.
- Host compromise may expose both assets if custody is not separated; this is why key and database backups must remain independent.
- TLS private keys must be managed separately from Receiver HMAC keys.
- HQ user passwords and JWT signing secrets must be separately managed from both.
- Logs must never contain secrets.
- Backups must preserve separation of duties between the database backup operator and the key backup/recovery owner.

## 10. Implementation prerequisites

Future work, none of it performed by this ADR:

- Approved Windows host selection.
- Named dedicated service identity.
- Selected TLS termination implementation.
- Secret provisioning tool selection.
- DPAPI protection-scope decision (per-user vs. per-machine key, and which principal unprotects it).
- Filesystem ACL design for application, data, log, secret, and backup directories.
- Log directory and rotation configuration.
- Database directory and backup tooling selection.
- Recovery rehearsal execution and sign-off.
- Operator authorization and named roles.
- Pilot Store selection for Stage 1.
- Production HMAC-key generation ceremony.
- Service installation/startup design (Windows service or approved equivalent).
- Monitoring and alerting implementation.

## 11. Revisit triggers

Re-review this ADR if any of the following occur:

- More than one Uvicorn worker is needed.
- SQLite becomes inadequate for the observed load.
- The backend moves to Linux.
- The backend moves to managed hosting.
- Active-active or high availability is required.
- Domain-managed identities become available for this environment.
- An external secret service is approved.
- Store count or traffic grows materially beyond the current ~40-Store pilot scope.
- Compliance requirements change.
- DPAPI recovery cannot be demonstrated in a rehearsal.
- Key-rotation requirements become more frequent than this design assumes.
- A security incident affects host or key custody.

## 12. Decision approval

| Role | Name | Date |
| --- | --- | --- |
| Security approver | _(pending)_ | _(pending)_ |
| Operations approver | _(pending)_ | _(pending)_ |
| Product owner | _(pending)_ | _(pending)_ |
| Database backup owner | _(pending)_ | _(pending)_ |
| Rollback owner | _(pending)_ | _(pending)_ |
| Approval date | _(pending)_ | |
| Review date | _(pending)_ | |
