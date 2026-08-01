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
