# Receiver Phase-One Migration — Maintenance Runbook

Applying the Receiver Credential Lifecycle Phase 1 schema to a real database.

**This has never been run against a live database.** The migration and its
tooling are covered by 21 tests on temporary databases, including a copy of the
isolated pilot database. That is not the same as having done it.

`backend/speaklink_live.db` is refused by the tool with no override. Migrating it
is a maintenance-window decision with a named owner, not a command-line flag.

---

## What the migration does

Adds five tables and their indexes:

```
receiver_devices                      one enrolled Windows computer
receiver_credentials                  hashed device credentials, versioned
receiver_credential_events            audit trail
receiver_credential_migration_state   cutover bookkeeping
schema_migrations                     the applied-migration ledger
```

**It only adds.** No existing row is read, rewritten or deleted. No column on
`stores` or `hq_users` changes. `stores.receiver_token` and every HQ password
hash are left exactly as they are — tests compare them before and after and
assert they are byte-identical.

It runs in one SQLite transaction with foreign keys enforced.

---

## Before the window

| Check | Command |
| --- | --- |
| Who owns this change | named in the change record, not in this file |
| Backup destination has space | at least the size of the database, twice |
| Rollback rehearsed | on a copy, this week — see below |
| Nobody is broadcasting | Broadcast Console shows Idle |

Stop the backend. The migration takes `BEGIN IMMEDIATE`; a running backend
holding a write lock will make it fail rather than corrupt anything, but a
failed migration during business hours is still an incident.

---

## 1. Look, before touching

```powershell
.\backend\.venv\Scripts\python.exe tools\receiver_migration.py status `
    --database <path-to-database>
```

Reads only. Reports which Phase 1 tables exist, and the Store and HQ user
counts. Record those two numbers — they are what the verification compares
against afterwards.

## 2. Rehearse

```powershell
.\backend\.venv\Scripts\python.exe tools\receiver_migration.py preflight `
    --database <path-to-database>
```

Reads only. Reports exactly which tables would be created. If
`already_applied` is `true`, stop: there is nothing to do.

## 3. Rehearse on a copy, not on the original

Do this at least once before the real window.

```powershell
Copy-Item <path-to-database> <scratch>\rehearsal.db
.\backend\.venv\Scripts\python.exe tools\receiver_migration.py apply `
    --database <scratch>\rehearsal.db `
    --backup-dir <scratch>\backups
```

Confirm in the output that `verification` reports all four checks true, then
practise the rollback in section 6 against that copy. A rollback nobody has
performed is a plan, not a rollback.

## 4. Apply

```powershell
.\backend\.venv\Scripts\python.exe tools\receiver_migration.py apply `
    --database <path-to-database> `
    --backup-dir <backup-directory>
```

The tool, in this order:

1. refuses if the path is the protected database;
2. refuses if the file is missing or is not a readable SQLite database;
3. returns immediately if the migration is already applied;
4. takes a backup **before touching anything**, using SQLite's backup API rather
   than a file copy, so the snapshot is consistent even with WAL present;
5. verifies that backup by size, SHA-256, and reopening it as a database;
6. runs the migration in one transaction;
7. verifies the result.

Keep the printed JSON. It contains `backup_path`, `backup_bytes` and
`backup_sha256`, which is what a restore is checked against.

## 5. Verify

The tool's own `verification` block must show all four:

| Check | Meaning |
| --- | --- |
| `tables_present` | all five Phase 1 tables exist |
| `foreign_keys_enabled` | referential integrity is enforced |
| `indexes_present` | the receiver indexes were created |
| `row_counts_preserved` | `stores` and `hq_users` counts are unchanged |

If any is false the tool raises and names the backup to restore. Do that before
anything else.

Then confirm by hand that the two counts from step 1 are unchanged, and that the
backend starts and an operator can sign in.

## 6. Rollback

The migration only adds tables, so the fastest correct rollback is to restore
the backup.

```powershell
# 1. Stop the backend.
# 2. Move the current file aside - do not delete it. It is evidence.
Move-Item <path-to-database> <path-to-database>.failed-<timestamp>
# 3. Restore.
Copy-Item <backup-path> <path-to-database>
# 4. Confirm the restored file matches the recorded hash.
Get-FileHash <path-to-database> -Algorithm SHA256
# 5. Start the backend and confirm sign-in and Store list.
```

Compare that hash against `backup_sha256` from the apply output. If they differ,
stop and escalate — restoring an unverified file is how one incident becomes
two.

**Never delete a database to resolve a migration problem.**

## 7. After

Record in the change log: who ran it, when, the backup path and hash, the
row counts before and after, and the verification block.

The migration alone changes nothing an operator can see. Devices appear only
once the enrolment API exists and Receivers enrol — see
[`RECEIVER_ENROLMENT.md`](RECEIVER_ENROLMENT.md).

---

## Current state

| Database | Migrated |
| --- | --- |
| `backend/speaklink_live.db` (protected) | **No** — refused by the tool, no override |
| Isolated pilot database | **No** |
| Temporary test databases | Yes, in 21 automated tests |
| A copy of the pilot database | Yes, in one automated test; the original was asserted untouched |

`NOT_READY_FOR_PRODUCTION`.
