# SpeakLink Store Catalog Reconciliation Runbook

Status: read-only reporting tool; it authorizes and performs no cleanup.

## Purpose

`seed_stores()` is a first-run bootstrap: it fills an empty `stores` table with
the approved catalog and does nothing at all to a database that already has
rows. An existing deployment can therefore still hold Store rows that predate
the canonical catalog, including rows from the retired 13-entry demo seed and
rows an operator created by hand.

This report answers one question, without changing anything:

> What is actually in this snapshot, compared with `backend/store_catalog.py`,
> and what would a human have to decide before anything is changed?

It is the safe first step before any future cleanup task.

## Read-only guarantee

- The snapshot is opened through a SQLite `mode=ro` URI, then
  `PRAGMA query_only = ON` is applied. Both the connection and the file handle
  reject writes; the test suite proves `UPDATE`, `DELETE` and `CREATE TABLE`
  all raise on this connection.
- No `INSERT`, `UPDATE`, `DELETE`, `ATTACH`, `VACUUM`, temporary table,
  journal-mode change, migration or seed call exists anywhere in the module.
- Columns are always listed explicitly. `SELECT *` is never used on `stores`,
  and the Store credential column is never selected, so credential material
  cannot reach a report, a log or a terminal.
- Tests assert the snapshot's SHA-256, byte size and modification timestamp are
  identical before and after a report, and that Store rows and dependent rows
  are unchanged.

## Protected-path refusal

`backend/speaklink_live.db` is refused **before any connection is opened**.
Refusal covers:

- the exact path,
- a relative path that resolves to it (including one reached through `..`),
- a path supplied relative to a different working directory,
- a same-file reference such as a hard link, detected with `os.path.samefile`.

Filename comparison alone is never used. If `samefile` is unavailable the
resolved-path comparison still applies.

The tool never copies the protected database. Producing a snapshot is an
operator action, deliberately outside this tool.

## Required isolated snapshot

Supply a quiesced copy that the operator produced under an approved procedure.
If a `-wal` or `-shm` file sits beside the snapshot the report **fails closed**
with a clear message, because the copy may be inconsistent. The tool never
merges write-ahead data and never repairs a database.

## Command syntax

```text
python -m store_catalog_reconciliation --database <ISOLATED_SNAPSHOT_PATH>
python -m store_catalog_reconciliation --database <ISOLATED_SNAPSHOT_PATH> --format json
```

Run it from the `backend` directory with the project virtual environment.
`--database` is required; there is no default and no implicit discovery.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | The snapshot exactly matches the canonical catalog |
| `2` | The report completed safely and differences were found |
| `1` | Input, schema or safety failure (including protected-path refusal) |

Exit `2` is a normal, successful run. It does not mean anything went wrong.

## Classifications

| Classification | Meaning |
| --- | --- |
| `EXACT_CANONICAL_MATCH` | Code, full name, Zone and City all match the catalog |
| `CANONICAL_FIELD_MISMATCH` | Identity matches, but Zone and/or City is wrong |
| `KNOWN_LEGACY_DEMO_EXACT_MATCH` | All five fingerprint fields exactly match a retired demo row |
| `CUSTOM_OR_UNKNOWN_NON_CANONICAL` | Not canonical and not an exact demo row |
| `AMBIGUOUS_IDENTITY_CONFLICT` | Duplicate code/full name, or code and full name disagree with the catalog |

The legacy-demo fingerprint is the exact tuple
`(store_code, store_name, city, region, is_online_store)`, recovered read-only
from `git show af168aa:backend/seed.py`. **All five fields must match exactly.**
Partial names, city alone, region alone, a code prefix, row position and
case-insensitive guesses are never used. If even one field differs, the row
stays `CUSTOM_OR_UNKNOWN_NON_CANONICAL`.

## Dependencies

For each Store the report counts references in every relationship proven from
the repository's models and migrations:

| Relationship | Source | Availability |
| --- | --- | --- |
| `broadcast_targets.store_id` | `models.py` | Always present |
| `receiver_events.store_id` | `models.py` | Always present |
| `receiver_devices.store_id` | `migrations.py` (Phase 1) | Only after the additive migration |
| `receiver_credential_events.store_id` | `migrations.py` (Phase 1) | Only after the additive migration |
| `receiver_credentials` via `receiver_devices` | `migrations.py` (Phase 1) | Indirect, only after the migration |

A table that is absent from the snapshot is reported as `n/a`, never as a
proven `0`. That distinction matters: "no rows" and "cannot tell" are different
answers, and only one of them is safe to act on.

## Recommendations

The report emits one of these per row and **never executes any of them**:

`NO_ACTION`, `ADD_MISSING_CANONICAL_STORE_LATER`, `REVIEW_FIELD_CORRECTION`,
`REVIEW_IDENTITY_CONFLICT`, `REVIEW_ARCHIVAL`, `REVIEW_TARGETED_DELETION`,
`BLOCKED_BY_DEPENDENCIES`, `HUMAN_REVIEW_REQUIRED`.

## Why non-canonical does not mean fake

A Store missing from the catalog is not automatically demo data. It may be a
genuine new store opened after the catalog was approved, a pilot or test site an
operator created deliberately, or a store recorded under a different code. The
report therefore never merges "not in the catalog" with "safe to remove". Rows
that are not proven demo rows stay `CUSTOM_OR_UNKNOWN_NON_CANONICAL` and route
to `HUMAN_REVIEW_REQUIRED`.

## Why a proven demo row can still be undeletable

Even a row that exactly matches the retired demo seed may be referenced by
Broadcast Targets, Receiver Events, Receiver Devices or credential audit
events. Deleting it would break historical records, and
`receiver_devices.store_id` uses `ON DELETE RESTRICT`, so the database itself
would refuse. A demo row that still has dependencies is therefore recommended
for `REVIEW_ARCHIVAL`, not deletion.

## What this report does not prove

It compares catalog data only. A Store appearing here proves only that HQ knows
about that Store. It is **not** evidence of:

- `CONNECTED` — an authenticated Receiver WebSocket
- `READY` — Receiver software and output-device checks
- `AUDIO_RECEIVING` — audio arriving at the Receiver
- `PLAYBACK_CONFIRMED` — the software pipeline processing audio
- `SPEAKER_VERIFIED` — LinkGuard acoustic confirmation of real sound

These remain independent evidence, exactly as defined in
`RECEIVER_STATUS_CONTRACT.md`.

## Future execution boundary

Applying any change to a real database is a separate, explicitly approved task.
Before that task may begin it needs, at minimum:

1. A verified backup of the database plus any WAL/SHM files, restored and
   checked in isolation.
2. A reviewed change record naming the operator, approver and rollback owner.
3. A dry run of the intended change against a snapshot.
4. An explicit human decision for every `HUMAN_REVIEW_REQUIRED`,
   `REVIEW_IDENTITY_CONFLICT` and `REVIEW_ARCHIVAL` row.
5. A decision on dependent history for every row that is not
   `BLOCKED_BY_DEPENDENCIES`-clear.

This runbook deliberately contains no cleanup SQL. Blanket statements against
the `stores` table are never an acceptable recovery or cleanup mechanism.
