# EchoCast Receiver Security and Operations Review

Status: review-only; no production authorization. This review does not select, generate, load, or configure a real secret, and does not open, copy, back up, or modify `backend/echocast_live.db`.

## 1. Executive summary

Architecture and migration logic are strongly tested: the Receiver credential lifecycle, Phase 1 schema, isolated enrollment, legacy backfill rehearsal, migration-state transition service, connection-source inventory, dual-authentication runtime boundary, and controlled cutover rehearsal are all covered by isolated pytest suites using temporary SQLite databases and generated test-only keys.

Production deployment decisions are not yet implemented. No real migration or hashed cutover has happened against `backend/echocast_live.db`. No production cutover has occurred. No real HMAC key was loaded. No real database was opened, copied, or modified as part of this review or any prior task in this line of work. The project is not yet proven production-ready for an all-40-Store rollout: only one Uvicorn worker has been exercised, connection state is process-local, and no Windows Receiver Agent, FFmpeg output path, amplifier, or EchoGuard acoustic verification has been exercised end to end.

Authentication establishes only CONNECTED identity. It does not establish READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED, or SPEAKER_VERIFIED. Audio and speaker validation remain separate evidence from Authentication and must never be treated as proven by a successful login or handshake.

### Rejected and required practices

This review rejects: a plaintext production key in `.env`; a key stored in SQLite; a hard-coded key; a personal administrator account; developer-laptop production deployment; `LocalSystem` by default; multiple Uvicorn workers; a raw token in a URL; and database deletion as migration recovery.

This review requires: separate database and key backups; restricted ACLs; a dedicated service identity; HTTPS/WSS; restricted CORS; audit logs; secret redaction; a restore rehearsal; and a one-Store pilot before wider rollout.

## 2. Reviewed assets

- HMAC key material (future; none exists in this repository).
- `Store.receiver_token` values (current legacy credential).
- Receiver Device credentials (`receiver_credentials` rows, isolated schema only today).
- SQLite database (`backend/echocast_live.db`).
- Database backups (future; not yet created).
- HMAC-key backups (future; not yet created).
- TLS private keys (future; not yet selected).
- HQ user passwords (bcrypt-hashed, stored in `hq_users`).
- JWT/session signing secrets (`JWT_SECRET`, loaded from `backend/.env`).
- Audit logs (`receiver_credential_events`, isolated schema only today).
- Application logs (`system_logs`, Python `logging` output).
- Receiver Agent configuration (not implemented in this repository).
- Store audio path (FFmpeg / Windows audio / amplifier; not implemented or verified).
- EchoGuard evidence (`SPEAKER_VERIFIED`; separate trusted-verifier path, not yet integrated).

## 3. Trust boundaries

- HQ browser to FastAPI: JWT bearer authentication over HTTP/HTTPS and the `/api/ws/hq` WebSocket.
- FastAPI to Receiver WebSocket: `Authorization: Bearer` handshake authentication at `/api/ws/receiver`, currently legacy Store-token only.
- Backend process to SQLite: single-writer, WAL-mode, `PRAGMA foreign_keys=ON`, one Uvicorn worker.
- Backend process to secret storage: currently `backend/.env` (development only); production storage is the subject of the accompanying ADR.
- Backup operator to backup storage: distinct role from the security approver and rollback owner per `RECEIVER_HMAC_KEY_CUSTODY.md`.
- Receiver Agent to Windows audio: not implemented or verified in this repository.
- EchoCast to EchoGuard control: reserved trusted-verifier schema only; no transport exists yet.
- Store speakers to EchoGuard microphone: acoustic boundary, entirely outside this codebase's current scope.

## 4. Threat review

Ratings use qualitative Low/Medium/High/Critical labels; they are judgment calls based on repository evidence, not a quantitative risk score.

| # | Asset affected | Scenario | Existing control | Missing control | Severity | Likelihood | Recommended mitigation | Pilot blocker | Evidence source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Git/source archive | Repository or archive leak exposes source and design docs | `.gitignore` excludes `.env`, `*.db`, keys, credentials | None needed beyond current hygiene | Low | Low | Keep secret patterns in `.gitignore`; review before publishing archives | No | `.gitignore` |
| 2 | SQLite database | Database theft (file copy) | WAL/journal mode, local-only file | Encrypted-at-rest storage, restricted ACLs on data directory | High | Medium | Restrict filesystem ACLs to service identity; encrypt host disk if feasible | Yes | `db.py`, ADR |
| 3 | HMAC key | HMAC-key theft | None yet (no real key exists) | DPAPI-protected storage, ACL restriction | Critical | Medium | Adopt ADR-selected DPAPI storage; restrict to service identity and recovery role | Yes | `RECEIVER_HMAC_KEY_CUSTODY.md`, ADR |
| 4 | Database + key | Combined host compromise | Separate custody design (policy only) | Host hardening, separate backup custody | Critical | Low | Enforce separate database/key backup ownership; monitor host access | Yes | `RECEIVER_HMAC_KEY_CUSTODY.md` |
| 5 | HMAC key | Plaintext key exposure in `.env` or config | Custody policy forbids this | Enforced storage mechanism | Critical | Medium | Never use `.env` for production keys; use DPAPI container | Yes | ADR, custody policy |
| 6 | Logs | Log leakage of secrets/tokens | Bounded error codes only persisted (`server.py:_persist_receiver_ack`) | Structured log review, redaction audit | Medium | Low | Periodic log-content audit; structured logging | No | `server.py` |
| 7 | Shell history | Command-history leakage of a key or token | Custody policy forbids placing keys in arguments | Operator training, secure procedure | Medium | Low | Follow `RECEIVER_HMAC_KEY_CUSTODY.md` handling rules | No | custody policy |
| 8 | HQ/operator access | Unauthorized operator access | JWT auth on all HQ routes | Role-based authorization, MFA | Medium | Medium | Add authorization roles in a future task | No | `auth.py` |
| 9 | Service identity | Excessive service privileges (e.g. LocalSystem) | None yet (host not selected) | Dedicated non-admin identity | High | Medium | Adopt ADR-selected dedicated non-admin identity | Yes | ADR |
| 10 | Backups | Backup mismatch or missing key | Custody policy requires separate, verified backups | Actual backup tooling | High | Medium | Implement and rehearse backup/restore before cutover | Yes | custody policy, runbook |
| 11 | Backups | Broken restore procedure | Runbook requires restore verification (Phase D) | Executed rehearsal | High | Medium | Perform and record a restore rehearsal before Phase E | Yes | runbook |
| 12 | Connection inventory | Stale connection inventory after restart | Process-local inventory, documented limitation | Shared/authoritative inventory (future) | Medium | Medium | Keep one worker; treat empty inventory as inconclusive, not proof | No | `receiver_connection_inventory.py` |
| 13 | Runtime state | Multiple-worker divergence | One-worker requirement documented and enforced by design | Multi-worker coordination (not needed for pilot) | High | Low | Do not configure more than one Uvicorn worker | Yes | `ws_manager.py`, ADR |
| 14 | Migration state | Unauthorized migration-state transition | Transition service requires actor, key readiness, fresh summary | Production authorization/role enforcement | High | Low | Gate real transitions behind named approvers per runbook | Yes | `receiver_migration_transition_service.py` |
| 15 | Receiver credentials | Credential replay | HMAC comparison, constant-time verification | Rate limiting, replay-window telemetry | Medium | Low | Add authentication-failure aggregation (future) | No | `receiver_credentials.py` |
| 16 | Receiver credentials | Credential rotation failure | Pure rotation-planning helper with bounded grace | Executed rotation/replacement service | Medium | Medium | Build rotation execution before hash-only cutover | Yes | `receiver_credentials.py` |
| 17 | Network transport | TLS/WSS misconfiguration | Runbook requires HTTPS/WSS before public deployment | Selected/implemented TLS termination | Critical | Medium | Select and implement TLS termination before any non-loopback exposure | Yes | README, runbook |
| 18 | Network transport | CORS misconfiguration | `CORS_ORIGINS` env override exists; defaults to `*` | Restricted production origin list | Medium | Medium | Set explicit `CORS_ORIGINS` before production | Yes | `server.py` |
| 19 | Network transport | Rate-limit absence | None | Login/enrollment/administrative rate limiting | Medium | Medium | Add rate limiting in a future task | No | `server.py` |
| 20 | SQLite database | SQLite corruption | WAL mode, integrity/foreign-key checks documented | Automated integrity monitoring | High | Low | Schedule periodic `PRAGMA integrity_check` | No | `db.py`, runbook |
| 21 | SQLite database | Power loss during maintenance | Runbook requires stopping Uvicorn before backup | UPS/power protection (host-level) | Medium | Low | Perform maintenance only with verified power/backup | No | runbook |
| 22 | SQLite database | Accidental real-database testing | Protected-path guards refuse `echocast_live.db` in every isolated module | None needed beyond current guards | Low | Low | Keep protected-path guards in all future services | No | `migrations.py`, backfill/auth/transition modules |
| 23 | Receiver connections | Receiver reconnect storm | One-current-connection-per-Store replacement logic | Reconnect-rate monitoring | Medium | Medium | Add reconnect-rate alerting (future) | No | `ws_manager.py` |
| 24 | Store audio path | Bluetooth/output-device mismatch | Status contract models `DEVICE_ERROR` independently | Verified Receiver Agent/output-device testing | Medium | High | Complete Receiver Agent and output-device verification before pilot | Yes | `RECEIVER_STATUS_CONTRACT.md` |
| 25 | Operational trust | Authentication mistaken for speaker health | Independent status axes documented and enforced in contract | Dashboard/operator training | High | Medium | Never present CONNECTED/authentication as proof of audible sound | Yes | `RECEIVER_STATUS_CONTRACT.md` |

## 5. Least-privilege service identity review

The future EchoCast service identity should need only:

- Read/execute access to the application files.
- Read access to protected secret material.
- Read/write access to the SQLite database directory.
- Read/write access to the application log directory.
- The ability to bind only the approved local/network port.
- The ability to start a child FFmpeg process only if the backend later truly requires it.
- No interactive administration.
- No general access to user profiles or unrelated directories.

This identity must explicitly not have:

- Local administrator membership.
- Domain administrator rights.
- `LocalSystem` by default.
- Access to GitHub credentials.
- Access to Store Receiver tokens in logs.
- Access to unrelated backup locations.

## 6. Filesystem layout proposal

Non-executed placeholder locations only; no real path is created by this review:

```text
<APP_DIRECTORY>
<DATA_DIRECTORY>
<LOG_DIRECTORY>
<SECRET_DIRECTORY>
<BACKUP_DIRECTORY>
```

Requirements:

- Application, data, logs, secrets, and backups must be separated.
- No real local path is created by this document.
- The Git repository must not be used as the production writable data directory.
- SQLite and secrets must not sit beside source code when avoidable.

## 7. Network and TLS review

- HTTPS/WSS is required before any public deployment.
- Restricted CORS: set an explicit `CORS_ORIGINS` list; do not rely on the current `*` default in production.
- No raw token in a URL; the Receiver endpoint already uses `Authorization` header authentication only, and this must not regress.
- No debug mode in production.
- Rate limiting is required for login, enrollment, and administrative actions before production exposure.
- The firewall must expose only required ports.
- SQLite must never be network exposed.
- Administrative access must be restricted to named operators.
- TLS certificate and private-key ownership must be separate from Receiver HMAC keys.
- Certificate renewal failure monitoring is required once TLS is implemented.

This review does not select or install a specific proxy product.

## 8. Database operations review

- Single-writer SQLite considerations apply; only one Uvicorn worker may hold the write lock at a time.
- One Uvicorn worker remains mandatory while connection state is process-local.
- Explicit transactions (`BEGIN IMMEDIATE`) are already used by every migration/lifecycle module.
- Foreign-key enforcement (`PRAGMA foreign_keys=ON`) is already required and checked.
- Integrity checks (`PRAGMA integrity_check`, `PRAGMA foreign_key_check`) are already part of the migration and transition validation paths.
- WAL/SHM backup consistency: back up the database, WAL, and SHM as one consistent set, never the main file alone.
- Database deletion as migration recovery is rejected; restore from a verified backup instead.
- `Base.metadata.create_all` must not substitute for explicit, reviewed migrations of the Receiver credential schema.
- No database write should occur per audio chunk; only meaningful acknowledgement transitions are persisted (`server.py:_persist_receiver_ack`).
- Backup verification is required before any cutover phase.
- A restore rehearsal is required before Phase E of the production runbook.
- A future trigger exists for evaluating PostgreSQL or another server database if load or worker count grows.

## 9. Logging and audit review

Required practices:

- Structured logs with rotation.
- UTC timestamps throughout.
- No password, token, key, or JWT logging.
- Secret-free, fixed error messages (already the pattern in `receiver_auth_service.py` and `receiver_migration_transition_service.py`).
- Authentication and migration audits recorded in `receiver_credential_events`.
- No audio data in normal application logs.
- Bounded log retention.
- Protected log access, limited to the service identity and approved operators.
- An incident-preservation procedure for logs during an active investigation.
- Separate meanings for operational logs and security-audit events.

## 10. Monitoring and alerting proposal

Design only; no monitoring integration is created by this review.

- Backend process unavailable.
- Receiver heartbeat stale (beyond the 15-second boundary).
- Receiver reconnect failures.
- Authentication failure rate.
- Connection-inventory capacity.
- Uvicorn worker count (alert if more than one is ever configured).
- SQLite integrity-check failure.
- Backup age exceeding policy.
- Key-version availability.
- TLS certificate expiry.
- Queue growth.
- Dropped audio.
- CPU and RAM utilization.
- FFmpeg/output-device errors.
- EchoGuard overlap risk.
- `PLAYBACK_CONFIRMED` observed without `SPEAKER_VERIFIED` for an extended window.

## 11. Backup and recovery ownership

| Responsibility | Owner placeholder |
| --- | --- |
| Database backup | _(pending)_ |
| Key backup | _(pending)_ |
| Restore rehearsal | _(pending)_ |
| Backup integrity | _(pending)_ |
| Access approval | _(pending)_ |
| Emergency recovery | _(pending)_ |
| Post-restore credential validation | _(pending)_ |
| Backup retention | _(pending)_ |
| Destruction after retention expiry | _(pending)_ |

Database and key backups must remain separate, per `RECEIVER_HMAC_KEY_CUSTODY.md`; the database backup operator must not automatically receive key-backup access.

## 12. Incident-response outline

| Incident | Immediate containment | Evidence preservation | Decision owner | Rollback/recovery path | Credential impact | Communication | Follow-up review |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Suspected HMAC-key exposure | Stop key distribution/lifecycle changes | Preserve secret-free logs, note affected versions | Security approver | Add new key version; reissue affected credentials under controlled cohorts | Old version retired once no usable rows depend on it | Named operator/observer/rollback owner | Post-incident review of custody controls |
| Database theft | Restrict further access, preserve chain of custody | Secret-free access logs | Security approver | Rotate keys and credentials as needed; assess exposure with key custody | High if key also exposed | Escalate per runbook | Review filesystem ACLs |
| Host compromise | Isolate host, stop service | Preserve logs and forensic image if possible | Security approver | Rebuild host from approved baseline; restore from verified backups | Treat all local secrets as compromised | Escalate immediately | Full security review before redeploy |
| TLS-key compromise | Revoke/replace certificate | Preserve access logs | Security approver | Reissue certificate through approved process | No Receiver credential impact | Notify operators | Review certificate custody |
| Accidental secret logging | Rotate the exposed secret, purge logs per policy | Record incident details without the secret itself | Security approver | Reissue affected credentials/keys | Depends on secret type | Internal notification | Add/verify log redaction tests |
| Lost key backup | Attempt no key reconstruction from hashes | Record loss circumstances | Rollback approver | Replace affected credentials under controlled cohorts | Credentials for lost key versions become unverifiable | Escalate to recovery owner | Review backup redundancy |
| Corrupt database | Stop writer, do not delete the file | Preserve corrupted file for analysis | Database backup owner | Restore from verified backup | None if backup is clean | Notify operations | Review integrity-check cadence |
| Unknown migration state | Treat as authentication failure (fail closed) | Preserve state/audit snapshot | Security approver | Restore from verified backup or halt transitions | Authentication unavailable until resolved | Escalate immediately | Review state-consistency checks |
| Unauthorized state transition | Halt further transitions | Preserve audit event trail | Security approver | Roll back via approved adjacent transition if safe | Depends on transition | Escalate to rollback owner | Review authorization controls |
| Receiver credential misuse | Revoke affected credential | Preserve audit events | Security approver | Reissue credential to the affected Device | Single-device impact if isolated | Notify Store operator | Review credential monitoring |
| Reconnect storm during cutover | Pause transition, monitor inventory | Preserve connection-summary snapshots | Cutover observer | Delay narrowing transition until summary is clean | None if paused in time | Notify pilot owner | Review reconnect thresholds |
| Authentication functioning but audio failing | Treat as a Readiness/Playback/Acoustic issue, not an auth issue | Preserve independent status-axis snapshots | Pilot Store owner | Investigate FFmpeg/output device/EchoGuard separately | None (authentication is not the cause) | Notify Store operator | Review Receiver Agent verification |

## 13. Pilot readiness checklist

Ready or substantially prepared:

- Tested migration schemas.
- Backfill rehearsal.
- Authentication state matrix.
- Transition rollback.
- Connection-source inventory.
- Cutover rehearsal.
- Runbook.
- Key-custody design.

Not ready or unresolved:

- Actual production host.
- Real service identity.
- Real HMAC-key provisioning.
- TLS implementation.
- Backup tool.
- Restore rehearsal.
- Named operators.
- Real Receiver Agent rollout.
- FFmpeg/output-device verification.
- EchoGuard integration.
- One-Store pilot evidence.
- Security sign-off.
- Operational monitoring.

## 14. Risk acceptance

| Accepted risk | Owner placeholder | Expiry/review date placeholder | Pilot scope | Mitigation | Revisit trigger |
| --- | --- | --- | --- | --- | --- |
| Single-host availability | _(pending)_ | _(pending)_ | One approved host only | Documented restore procedure | Availability requirement changes |
| SQLite limitations | _(pending)_ | _(pending)_ | ~40-Store pilot scale | Integrity checks, one worker | Load or worker count grows |
| Process-local inventory | _(pending)_ | _(pending)_ | One Uvicorn worker | Treat restart as requiring reconciliation, not proof of safety | Multi-worker need arises |
| Manual key recovery | _(pending)_ | _(pending)_ | Initial pilot only | Documented recovery rehearsal | Recovery cannot be demonstrated |
| No automatic high availability | _(pending)_ | _(pending)_ | Initial pilot only | Manual restart procedure | HA requirement emerges |
| Limited pilot scope | _(pending)_ | _(pending)_ | 1/3/5/10-Store cohorts | Staged expansion policy | Cohort results are unacceptable |
| Manual rollback | _(pending)_ | _(pending)_ | Initial pilot only | Documented rollback transitions | Rollback cannot be demonstrated |

## 15. Final recommendation

Proceed to detailed implementation planning only after named security and operations reviewers approve the ADR and unresolved pilot blockers receive owners.

This review does not approve an actual production cutover.
