# Supabase PostgreSQL production database - setup, migration, cutover, rollback

Scope of this document: the PRODUCTION DATABASE only. The Windows HQ
machine remains the production application and WebSocket server. Receiver
audio never passes through Supabase - it still flows entirely through the
existing HQ FastAPI/WebSocket -> Windows Receiver Agent -> Store speakers
path, unchanged. Supabase Auth, Realtime, Storage, Edge Functions and the
JavaScript client are not used anywhere in this design; this is managed
PostgreSQL and nothing else.

## 1. Two environments, two rules

| | Development | Production |
|---|---|---|
| `APP_ENV` | unset or `development` | `production` |
| Database | local SQLite (`backend/echocast_live.db` by default) | PostgreSQL (Supabase) |
| `DATABASE_URL` | optional (rarely needed) | **required** |
| Missing `DATABASE_URL` | uses SQLite, no error | **refuses to start** |

This is implemented in `backend/db_config.py::load_database_config()` and is
covered by `backend/tests/test_database_config.py` and
`backend/tests/test_hq_runtime.py`. Production never silently creates or
falls back to a local SQLite file - see `db_config.DatabaseConfigError`.

## 2. Getting the Supabase connection string

Supabase Dashboard -> your project -> **Connect** -> **Session pooler**
(NOT Direct connection, NOT Transaction pooler/6543 unless you have a
specifically proven reason to prefer it - the Session pooler is the
correct mode for a long-lived, persistent backend process like this one).

The string looks like:

```
postgresql://postgres.<project-ref>:<password>@<region>.pooler.supabase.com:5432/postgres
```

## 3. Where the URL lives on the Windows HQ machine

> **Do not put production settings in `backend/.env`.** `server.py` calls
> `load_dotenv(backend/.env)` at import, so anything in that file reaches
> every local process that imports the backend - including the entire test
> suite, which would then be pointed at the real production database while
> running destructive tests. `backend/tests/conftest.py` now forces
> `APP_ENV=development` and blanks `DATABASE_URL` before any test module is
> imported, so the suite is protected - but a dev server started by hand is
> not. The correct location is the protected file below.

Never in Git, never in `config/hq-runtime.json`, never printed anywhere.
It lives in exactly one file, the same protected-outside-Git shape already
used for `jwt-secret.txt`:

```
%LOCALAPPDATA%\EchoCast-AI\persistent-lan-server\keys\database-url.txt
```

Unlike `jwt-secret.txt`, this file is **never auto-created**. If
`config/hq-runtime.json` sets `"app_env": "production"` and this file does
not exist, `tools/hq_runtime.py` refuses to start with a clear error - see
`backend/tests/test_hq_runtime.py::test_production_without_a_database_url_file_is_refused`.

To configure it:

1. Create `config/hq-runtime.json` (if it does not already exist) in the
   persistent root with `{"app_env": "production"}`.
2. Put the exact Session Pooler URL as the entire contents of
   `keys\database-url.txt` (one line, no quotes).
3. Nothing else reads or logs this file. `tools/hq_runtime.py::
   child_environment()` reads it once per HQ start and hands it to the
   backend child process through its environment only - never a command
   line, never a log line.

### If the database password is ever exposed

Rotate it, and treat the rotation as the fix rather than deleting the file
it leaked into. Supabase Dashboard -> Project Settings -> Database ->
Reset database password, then rebuild the Session Pooler URL with the new
password and replace the entire contents of `keys\database-url.txt`.
Restart the HQ Scheduled Task afterwards - the URL is read once per start,
so a running HQ keeps using the old credential until it is restarted.

Nothing else needs changing: the Receiver HMAC key, the JWT secret, every
Device identity and every Receiver credential are all independent of the
database password, so a database rotation costs one HQ restart and no
Receiver re-enrolment.

## 4. Connectivity check (no secrets printed)

```
python tools/migrate_sqlite_to_postgres.py --sqlite-path <path> --dry-run
```

does not touch the destination at all. To confirm connectivity itself,
run a harmless check from a Python shell with `DATABASE_URL` exported in
that shell only:

```python
from sqlalchemy import create_engine, text
from db_config import load_database_config
config = load_database_config(app_env="production")
engine = create_engine(config.url)
with engine.connect() as c:
    print(c.execute(text("SELECT version(), current_database()")).first())
```

This prints the PostgreSQL server version and current database name -
never the URL or password.

## 5. Migration tool

```
python tools/migrate_sqlite_to_postgres.py --sqlite-path PATH --dry-run
python tools/migrate_sqlite_to_postgres.py --sqlite-path PATH
python tools/migrate_sqlite_to_postgres.py --sqlite-path PATH --verify
```

`DATABASE_URL` is read from the environment only, never a CLI argument.
The SQLite source is opened read-only (`file:...?mode=ro`) and is never
written to by this tool under any flag. The real (non-dry-run) migration
refuses to write into a destination that already has rows in any table it
would populate, unless `--force` is passed explicitly.

Table order (FK-safe, computed from the real schema graph via
SQLAlchemy's own `sort_tables`, asserted in
`test_the_migration_tools_table_order_is_a_valid_fk_topological_sort`):

```
stores -> hq_users -> login_security_state -> system_logs -> permissions
-> permission_audit_events -> store_deletion_events
-> receiver_enrollment_codes -> broadcast_sessions -> receiver_events
-> receiver_devices -> role_permissions -> user_permission_overrides
-> user_store_scope -> store_scope_audit_events -> broadcast_targets
-> receiver_credentials -> receiver_store_primary_device
-> receiver_credential_events
```

Existing integer primary keys are preserved exactly (explicit id columns
in every INSERT) - Broadcast History, Receiver events and every audit
table reference these ids by number, and renumbering would silently break
every one of those references. After copying, every SERIAL/IDENTITY
sequence is advanced past the highest migrated id
(`migrate_sqlite_to_postgres._repair_sequences`), so the next row created
through the running application never collides with migrated history.

## 6. What does NOT move to PostgreSQL

- The Receiver HMAC key (stays DPAPI-protected on the Windows HQ machine).
- The JWT signing secret (stays in `keys/jwt-secret.txt`).
- Any raw Receiver credential or enrollment code (only their HASH RECORDS,
  already all the database ever stored, migrate as ordinary rows).
- No Receiver re-enrollment, ever. Existing Device identities - including
  Bindapur's `3b1ff11f-0b18-4f56-b911-30f036cbddd9` - are preserved exactly
  because their id-preserving rows migrate unchanged.

## 7. Cutover procedure

1. Confirm no active broadcast (`broadcast_sessions` has no `status='live'`
   row, and the operator confirms nothing is currently announcing).
2. Take a final SQLite backup with the existing backup procedure (SQLite
   backup API, never a raw file copy).
3. Stop the `EchoCast HQ Runtime` Scheduled Task.
4. Run the migration tool for real (not `--dry-run`) if this is the first
   migration, or a delta/fresh migration if data changed since the last
   dry run.
5. Set `"app_env": "production"` in `config/hq-runtime.json`.
6. Put the Session Pooler URL in `keys/database-url.txt`.
7. Start the Scheduled Task.
8. Require `hq-runtime-status.json` -> `READY`.
9. Login test (SUPER ADMIN).
10. Store list test (GET /api/stores returns the expected count).
11. User/RBAC test (roles and permissions resolve correctly).
12. Scope test (a scoped account still sees only its assigned Stores).
13. Receiver Status (renders, no blank page).
14. Confirm Bindapur reconnects naturally - no Store PC action.
15. Run one short (5-10s) Bindapur-only broadcast.
16. Confirm `PLAYBACK_CONFIRMED` for that session.

Do not delete the old SQLite database after cutover - it is the rollback
evidence and the audit record of everything before the cutover moment.

## 8. Rollback

If any cutover step fails:

1. Stop the `EchoCast HQ Runtime` Scheduled Task.
2. Set `"app_env": "development"` in `config/hq-runtime.json` (or remove
   the key entirely) so the runtime falls back to the preserved SQLite
   database - no `database-url.txt` edit needed, since production mode is
   what required it, not development.
3. Start the Scheduled Task.
4. Require `READY`.
5. Confirm Bindapur reconnects naturally. No Receiver re-enrollment should
   ever be necessary for a rollback.

**Writes made to PostgreSQL after cutover and before a rollback are not
carried back to SQLite.** This design deliberately does not attempt
bidirectional synchronization - once PostgreSQL is authoritative, a
rollback is a return to the last-known-good SQLite snapshot, and anything
written only to PostgreSQL in between is lost from the rollback's point of
view (it still exists in PostgreSQL, available for manual reconciliation,
but is not automatically replayed). For this reason the first pilot
cutover window should be short and deliberately supervised - broadcast a
handful of test sessions, confirm everything above, and only then trust
the window to widen.

## 9. Region change: which Supabase project is the real one

Three Supabase projects have existed during this work. Only the third is
the production database. Non-secret fingerprints (sha256 of the
project-ref, first 16 hex) are recorded so the right one can be
identified without ever writing a URL, project-ref or password into Git:

| project-ref fingerprint | role |
|---|---|
| `94778c6f130c34c1` | **Disposable test infrastructure.** Polluted by an accidental seed and later used deliberately for real-PostgreSQL compatibility tests. Never production. |
| `faee6d157d5f03d3` | **Rejected — wrong region.** A full verified migration was performed into it before the region problem was noticed. It must not be used for cutover. |
| `e720ac35878a1d7b` | **The production database.** Correct region. Holds the verified snapshot. |

The host fingerprint is a weaker signal than the project-ref: two projects
in the same region share a pooler host, and the rejected project is the
one whose host differs. Identify a project by its **project-ref**
fingerprint.

`system_identifier` is NOT a discriminator on Supabase - projects are
provisioned from a common base image and share it. What distinguishes a
fresh project reliably is (a) the project-ref fingerprint and (b) the
absence of EchoCast statements in `extensions.pg_stat_statements` while
that view is otherwise populated with the project's own provisioning
queries.

### Snapshot verified in the production project

Migrated from a fresh SQLite backup of the live RC12 database. All 19
source-backed tables match exactly; the three tables that exist only on
the admin-management feature branch migrated as **NEW_SCHEMA_EMPTY** (0
rows), which is correct - the live RC12 database does not contain them.

Bindapur is Store id 31 with Device
`3b1ff11f-0b18-4f56-b911-30f036cbddd9` active and primary. RBAC roles,
permission overrides, scope audit history and the TESTSTORE tombstone (with
its deletion audit row and broadcast history) all survived. Zero orphans
across ten FK relationships, zero duplicate identities, and every
sequence resumes past its migrated MAX(id).

### Sequence verification, and a correction

Sequence state is inspected with `SELECT last_value, is_called FROM
<sequence>` - a plain read of the sequence relation. It does **not**
allocate a value.

An earlier round verified sequences by calling `nextval` inside a
transaction that was then rolled back, and described that as
non-destructive. That was wrong: **`nextval` is explicitly
non-transactional**, and a rolled-back transaction still consumes the
number. The consequence was harmless (one id skipped) but the claim was
not true, and the catalog read above is what should be used.

### Live HQ is still SQLite

RC12 + SQLite remains authoritative. The production Supabase project holds
a verified snapshot only. Anything written to RC12 after the snapshot is
not in PostgreSQL, so the cutover procedure in section 7 must re-run the
migration into a freshly emptied production database as its delta step.

## 10. Real-PostgreSQL validation of the admin-management round

Run against a **disposable** Supabase TEST project, never production. The
identity gate below is what makes that claim checkable rather than trusted.

### Which project, and how it is proved

Two independent signals, both required, before a single destructive
statement runs:

1. **Fingerprint.** `sha256("postgres.<project-ref>")[:16]`, compared against
   the documented production value `e720ac35878a1d7b`. Note the convention:
   it hashes the **full pooler username**, not the bare project-ref. Hashing
   the ref alone gives a different value and will look like a mismatch.
2. **Read-only inventory.** An empty `public` schema, no leftover
   `echocast_test_*` schemas, and no `public.stores`. A fingerprint says
   *which* project; only an inventory notices production data restored into a
   project that carries a test ref.

If either is ambiguous, stop. Do not run tests.

### Passing the URL to the tests

The test project's Session Pooler URI lives at:

```
%LOCALAPPDATA%\EchoCast-AI\persistent-lan-server\keys\test-postgres-url.txt
```

Read it into the test process's own environment as `TEST_POSTGRES_URL`.
Do **not** use `set TEST_POSTGRES_URL=... && pytest`: that puts the password
on a command line, visible to any process listing and recorded in shell
history. The machine environment is never modified.

**Build the test URI from the TEST project's own connection string.** Taking
the production URI and editing only the project-ref leaves the *production
password* in a second file - it cannot authenticate, and it duplicates the
production credential onto disk. That mistake has already been made once.

If a password contains `%`, `@`, `#`, `/` or `:` it must be percent-encoded
in a URI, or the parse silently produces a different username and password
than intended. Choosing an alphanumeric database password avoids the whole
class of problem.

### What is validated

`backend/tests/test_postgres_admin_management.py` (29 tests) covers the
User and Device tombstones, login refusal by both `is_active` and
`session_version`, credential revocation, primary-assignment removal with no
auto-promotion, History archive/unarchive/permanent delete, Logs
archive/permanent delete, the audit surviving the purge it records,
structured log entity fields, per-screen search and filter semantics,
filter-based Select All Filtered, the filter indexes, the lifecycle and
tombstone columns, and foreign-key behaviour.

It deliberately does not drive the FastAPI HTTP layer: those routes are
proven by the SQLite suite, and an application engine pointed at this
project would resolve to `public` rather than to the generated test schema -
which is the exact accident the fixture exists to prevent.

### Seven defects this round found

All PostgreSQL-only, all invisible to the 2716-test SQLite suite:

| File | Defect |
|---|---|
| `user_deletion.py` | `is_active = 1` in a WHERE, and `is_active = 0` in the tombstone UPDATE |
| `store_deletion.py` | `is_active = 0`; one bind parameter shared by a `VARCHAR` and a `TIMESTAMP`; `CREATE TABLE ... AUTOINCREMENT`; unguarded `PRAGMA foreign_key_check` |
| `store_lifecycle.py` | `PRAGMA table_info` |
| `user_lifecycle.py` | `PRAGMA table_info` |
| `receiver_migration_transition_service.py` | `s.is_active = 1` |

The two `PRAGMA table_info` sites are the serious ones: they run at every
start-up, so **HQ would not have booted against PostgreSQL at all**. The
cutover would have failed at boot rather than at a feature.

See `docs/learning-guide.md` for the full family of SQLite-vs-PostgreSQL
differences and why a bound Python `bool` is preferred over a dialect branch.

### Required cleanup evidence

After the run, all four must hold:

* `0` leaked `echocast_test_*` schemas;
* `0` tables in `public`;
* Supabase-managed schemas (`auth`, `storage`, `realtime`, `extensions`,
  `graphql`, `vault`) present and carrying no EchoCast tables;
* production untouched.

### Production check is READ-ONLY

Verify production separately and without writing: `SET TRANSACTION READ ONLY`,
then confirm the fingerprint, that the rotated credential connects, that the
snapshot still reads (22 tables = 19 source-backed + 3 feature-branch,
45 Stores, 3 Users, 5 Devices), that Store 31 (Bindapur) is present and
active, and that its primary Device identity is unchanged.

## 11. STOP: the Receiver authentication service is SQLite-only

**Do not attempt a cutover until this is fixed and shipped in a new RC.**

`receiver_auth_service.authenticate_receiver_credential` - the function every
Receiver WebSocket handshake goes through - opens with:

```python
if engine.dialect.name != "sqlite" or _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
    raise _configuration_failure()
```

It refuses any non-SQLite engine. It also uses `PRAGMA foreign_keys`,
`PRAGMA table_info`, `PRAGMA foreign_key_check` and `sqlite_master`
unconditionally, and it reads two tables the migration tool does not carry:
`schema_migrations` and `receiver_credential_migration_state`.

The consequence of ignoring this: HQ boots, `hq-runtime-status.json` reports
`READY`, the admin UI works, every screen loads - and **no Store Receiver can
authenticate**. The failure is invisible from HQ.

Required before retrying, in order:

1. make the authentication path dialect-aware, with real-PostgreSQL tests
   that drive an actual handshake rather than asserting on schema;
2. decide and implement how `schema_migrations` and
   `receiver_credential_migration_state` reach PostgreSQL - migrate them, or
   have start-up re-derive them;
3. build and verify a new RC (RC14 does not contain these fixes);
4. prove the whole path against the disposable TEST project - start-up,
   `READY`, and one successful Receiver handshake - before touching
   production.

### Resetting a stale destination

`tools/reset_postgres_destination.py` exists for the one job of emptying the
EchoCast tables in a destination that already holds an older snapshot, so the
migration tool (which has no delta mode, and whose `--force` only INSERTs)
can run into a clean destination. It checks the project fingerprint, defaults
to dry-run, requires a typed `RESET` confirmation, touches only the known
EchoCast table inventory in FK-safe reverse order, refuses outright if it
finds an unrecognised public table, never issues `DROP`, and never touches a
Supabase-managed schema. It is deliberately not a general-purpose utility.

## 12. RC15 - Receiver authentication now works on PostgreSQL

Section 11's blocker is fixed. **Use RC15 or later for the cutover; RC14
cannot authenticate a single Receiver against PostgreSQL.**

What changed:

* `receiver_auth_service` accepts `sqlite` and `postgresql` (an explicit list,
  so an unexpected dialect is still refused), introspects with SQLAlchemy's
  Inspector instead of `PRAGMA`/`sqlite_master`, keeps the SQLite `PRAGMA
  foreign_keys` guard on SQLite only, and casts the `public_id` parameter that
  PostgreSQL could not type-infer;
* `server.build_receiver_runtime_authenticator` probes for the Device tables
  with the Inspector - the old `sqlite_master` probe threw, was swallowed by a
  bare `except`, and silently degraded the fleet to legacy Store-token
  authentication;
* `postgres_schema` gained `schema_migrations`,
  `receiver_credential_migration_state`, and the four indexes Receiver
  authentication requires;
* `migrate_sqlite_to_postgres.TABLE_ORDER` carries both state tables;
* `ensure_permission_schema` reseeds the catalog on PostgreSQL.

### The state tables travel - do not let them be re-derived

`receiver_credential_migration_state` must arrive as it is in SQLite
(`hash_only`, `legacy_verification_enabled = 0`). If it is ever recreated
instead of copied, it comes back as `legacy_only` with legacy verification ON,
which silently re-enables Store shared-token authentication. Verify after
migration:

```sql
SELECT state, legacy_verification_enabled
FROM receiver_credential_migration_state WHERE id = 1;
```

Expected: `hash_only`, `0`. Anything else - stop.

### The migration source must be a FRESH backup

`echocast-FINAL-pre-supabase-cutover-20260802-091545.db` is rollback evidence
from the blocked attempt. **It is no longer a valid migration source**,
because live HQ resumed writing to SQLite afterwards. At the real cutover:
stop HQ, prove SQLite quiescent, take a NEW final backup, and migrate from
that one.

### Order for the next attempt

1. stop the `EchoCast HQ Runtime` Scheduled Task, and confirm no orphaned
   `uvicorn` survives it (one did last time, and it kept the database open);
2. prove quiescence - two samples, counters and file sizes unchanged;
3. NEW final backup via the SQLite backup API, with `integrity_check` and
   `foreign_key_check`;
4. fingerprint gate on production (`e720ac35878a1d7b`);
5. `tools/reset_postgres_destination.py` - dry-run, then the confirmed reset;
6. migrate from the new backup; `--verify`;
7. check `receiver_credential_migration_state` as above;
8. install **RC15**, set `app_env=production`, start the task, require READY;
9. confirm a Store Receiver reconnects with no re-enrolment.
