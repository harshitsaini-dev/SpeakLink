"""The admin-management round against REAL PostgreSQL.

``test_postgres_integration.py`` covers the pre-existing application
behaviour. This file covers what the admin-management round added, because
that work introduced exactly the kinds of thing SQLite forgives and
PostgreSQL does not:

* ``ALTER TABLE ... ADD COLUMN`` migrations run through the Inspector rather
  than ``PRAGMA``;
* a tombstone that must survive a foreign key pointing at it;
* case-insensitive search, which is ``LIKE`` on SQLite and needs ``ILIKE``
  on PostgreSQL - SQLite's ``LIKE`` is case-insensitive for ASCII by
  default, so a query that passes there can silently match nothing here;
* dates compared against VARCHAR timestamp columns;
* ``EXISTS`` subqueries used to avoid returning one row per matching target;
* ``CREATE INDEX IF NOT EXISTS``, which SQLite and PostgreSQL both accept
  but with different behaviour when the table was created moments earlier.

WHAT THIS FILE DELIBERATELY DOES NOT DO

It does not drive the FastAPI HTTP layer. Those routes are proven by the
SQLite suite; re-running them here would need an application engine pointed
at this project, and that engine would resolve to ``public`` rather than to
the generated test schema. Pointing it there is precisely the accident this
whole fixture exists to make impossible. So the service modules and the
query semantics are exercised directly, inside the isolated schema.

Isolation, cleanup and the ``TEST_POSTGRES_URL`` skip all come from
``test_postgres_schema.pg_engine`` - one implementation of the safety
property, not a second that can drift.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

import admin_records  # noqa: E402
import device_deletion  # noqa: E402
import postgres_schema  # noqa: E402
import user_deletion  # noqa: E402
from admin_search import (  # noqa: E402
    BulkSelectionError, like_term, normalize_paging, parse_date, resolve_bulk_selection,
)
from sqlalchemy import inspect, text  # noqa: E402

from tests.test_postgres_schema import pg_engine, pg_required  # noqa: E402,F401


def _iso(offset_days: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def pg(pg_engine):
    """An isolated PostgreSQL schema carrying the full admin-management schema.

    The three ``ensure_*`` migrations run in the same order the server runs
    them at start-up, so this also proves they are ordering-safe and
    idempotent on PostgreSQL rather than only on SQLite.
    """
    postgres_schema.create_all(pg_engine)
    user_deletion.ensure_user_deletion_schema(pg_engine)
    device_deletion.ensure_device_deletion_schema(pg_engine)
    admin_records.ensure_admin_records_schema(pg_engine)
    # Running them twice proves idempotence: a second HQ start-up, or a
    # restart mid-migration, must not fail or duplicate anything.
    user_deletion.ensure_user_deletion_schema(pg_engine)
    device_deletion.ensure_device_deletion_schema(pg_engine)
    admin_records.ensure_admin_records_schema(pg_engine)
    return pg_engine


# ---------------------------------------------------------------- builders
def _store(c, code="BP", name="Bindapur", city="DELHI", region="NORTH"):
    return c.execute(text(
        "INSERT INTO stores (store_code, store_name, city, region, is_online_store, "
        "receiver_token, is_active, lifecycle_state, status, created_at, updated_at) "
        "VALUES (:c, :n, :ci, :r, false, :t, true, 'active', 'offline', now(), now()) "
        "RETURNING id"), {"c": code, "n": name, "ci": city, "r": region,
                          "t": uuid.uuid4().hex}).scalar_one()


def _user(c, username="founder", role="OWNER"):
    return c.execute(text(
        "INSERT INTO hq_users (username, password_hash, role, is_active, session_version, "
        "created_at, lifecycle_state) VALUES (:u, :h, :r, true, 1, now(), 'active') "
        "RETURNING id"), {"u": username, "h": "not-a-real-hash-" + uuid.uuid4().hex,
                          "r": role}).scalar_one()


def _device(c, store_id, status="active"):
    public_id = str(uuid.uuid4())
    now = _iso()
    device_id = c.execute(text(
        "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
        "enrolled_at, created_at, updated_at) VALUES (:p, :s, 'Till 1', :st, :n, :n, :n) "
        "RETURNING id"), {"p": public_id, "s": store_id, "st": status,
                          "n": now}).scalar_one()
    return device_id, public_id


def _credential(c, device_id):
    now = _iso()
    return c.execute(text(
        "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
        "token_format, token_hash, hash_key_version, status, expiry_policy, issued_at, "
        "created_at) VALUES (:p, :d, 1, 'echocast_rcv', :h, 1, 'active', "
        "'non_expiring', :n, :n) RETURNING id"),
        {"p": str(uuid.uuid4()), "d": device_id,
         "h": "hash-" + uuid.uuid4().hex, "n": now}).scalar_one()


def _make_primary(c, store_id, device_id):
    c.execute(text(
        "INSERT INTO receiver_store_primary_device (store_id, device_id, promoted_at) "
        "VALUES (:s, :d, :n)"), {"s": store_id, "d": device_id, "n": _iso()})


def _session(c, user_id, name="Morning offer", status="completed", created=None):
    return c.execute(text(
        "INSERT INTO broadcast_sessions (campaign_name, started_by, status, target_mode, "
        "selected_store_count, online_store_count, offline_store_count, created_at) "
        "VALUES (:n, :u, :s, 'selected', 1, 1, 0, :c) RETURNING id"),
        {"n": name, "u": user_id, "s": status,
         "c": created or datetime.now(timezone.utc)}).scalar_one()


def _target(c, session_id, store_id):
    return c.execute(text(
        "INSERT INTO broadcast_targets (session_id, store_id, play_status) "
        "VALUES (:s, :st, 'pending') RETURNING id"),
        {"s": session_id, "st": store_id}).scalar_one()


def _log(c, message="Broadcast started", level="info", actor=None, store=None,
         device=None, created=None):
    return c.execute(text(
        "INSERT INTO system_logs (level, message, created_at, actor_user_id, store_id, "
        "device_public_id) VALUES (:l, :m, :c, :a, :s, :d) RETURNING id"),
        {"l": level, "m": message, "c": created or datetime.now(timezone.utc),
         "a": actor, "s": store, "d": device}).scalar_one()


# ===========================================================================
# Schema: the migrations themselves
# ===========================================================================
@pg_required
def test_lifecycle_and_tombstone_columns_exist_on_postgresql(pg):
    """The columns the whole round depends on, added by ALTER TABLE.

    Every one of these was added by a migration written with the Inspector
    rather than PRAGMA, precisely so it could run here unchanged.
    """
    inspector = inspect(pg)
    users = {c["name"] for c in inspector.get_columns("hq_users")}
    assert {"lifecycle_state", "deleted_at", "deleted_by", "session_version"} <= users

    devices = {c["name"] for c in inspector.get_columns("receiver_devices")}
    assert {"archived_at", "deleted_at"} <= devices

    sessions = {c["name"] for c in inspector.get_columns("broadcast_sessions")}
    assert "archived_at" in sessions

    logs = {c["name"] for c in inspector.get_columns("system_logs")}
    assert {"archived_at", "actor_user_id", "store_id", "device_public_id"} <= logs

    stores = {c["name"] for c in inspector.get_columns("stores")}
    assert {"lifecycle_state", "deleted_at"} <= stores


@pg_required
def test_the_admin_audit_tables_exist_and_are_separate_from_what_they_audit(pg):
    """The audit lives in its own table on purpose.

    History deletion is REAL deletion - the rows are the history. If the
    record of the purge lived in the table being purged, a purge could erase
    its own evidence. These tables are what makes that impossible.
    """
    tables = set(inspect(pg).get_table_names())
    assert "admin_deletion_events" in tables
    assert "user_deletion_events" in tables
    assert "device_deletion_events" in tables


@pg_required
def test_the_filter_indexes_were_created(pg):
    """Indexes for the paths the six admin screens actually narrow on."""
    with pg.connect() as c:
        names = set(c.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
        )).scalars().all())
    for expected in (
        "ix_broadcast_sessions_archived", "ix_system_logs_archived",
        "ix_system_logs_created", "ix_broadcast_sessions_created",
    ):
        assert expected in names, f"missing index {expected}: {sorted(names)}"


# ===========================================================================
# User tombstone
# ===========================================================================
@pg_required
def test_a_user_with_history_is_tombstoned_not_deleted(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        victim = _user(c, "caster", "BROADCASTER")
        store = _store(c)
        session_id = _session(c, victim)
        _target(c, session_id, store)

    result = user_deletion.permanently_delete_user_with_history(
        pg, user_id=victim, typed_confirmation="caster", actor_user_id=owner)
    assert result.username == "caster"

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT lifecycle_state, is_active, session_version, deleted_at, deleted_by, "
            "username, password_hash FROM hq_users WHERE id = :i"), {"i": victim}).one()
        assert row.lifecycle_state == "deleted"
        assert row.is_active is False
        assert row.session_version > 1, "the token must be invalidated immediately"
        assert row.deleted_at is not None and row.deleted_by == owner
        assert row.username == "caster", "the username stays reserved"
        assert "not-a-real-hash" not in row.password_hash, "the hash must be replaced"

        # The whole point: the history still names this account.
        assert c.execute(text("SELECT started_by FROM broadcast_sessions WHERE id = :i"),
                         {"i": session_id}).scalar_one() == victim


@pg_required
def test_login_is_refused_after_deletion_by_state_and_by_session_version(pg):
    """Two independent refusals, because one is not enough.

    ``is_active``/``lifecycle_state`` stop a NEW sign-in. ``session_version``
    stops a token that was already issued - it is compared on every request,
    so a live session ends at once rather than when the JWT happens to expire.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        victim = _user(c, "livesession", "ADMIN")
        before = c.execute(text("SELECT session_version FROM hq_users WHERE id = :i"),
                           {"i": victim}).scalar_one()

    user_deletion.permanently_delete_user_with_history(
        pg, user_id=victim, typed_confirmation="livesession", actor_user_id=owner)

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT is_active, lifecycle_state, session_version FROM hq_users WHERE id = :i"),
            {"i": victim}).one()
    assert row.is_active is False            # refuses a new sign-in
    assert row.lifecycle_state == "deleted"  # and is not merely disabled
    assert row.session_version == before + 1  # refuses an existing token


@pg_required
def test_a_wrong_typed_confirmation_changes_nothing_on_postgresql(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        victim = _user(c, "careful", "ADMIN")

    with pytest.raises(user_deletion.UserDeletionRefused):
        user_deletion.permanently_delete_user_with_history(
            pg, user_id=victim, typed_confirmation="WRONG", actor_user_id=owner)

    with pg.connect() as c:
        assert c.execute(text("SELECT lifecycle_state FROM hq_users WHERE id = :i"),
                         {"i": victim}).scalar_one() == "active"


@pg_required
def test_the_user_deletion_is_audited(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        victim = _user(c, "audited", "ADMIN")

    user_deletion.permanently_delete_user_with_history(
        pg, user_id=victim, typed_confirmation="audited", actor_user_id=owner)

    events = user_deletion.list_user_deletion_events(pg, user_id=victim)
    assert len(events) == 1
    assert events[0]["username"] == "audited"
    assert events[0]["actor_user_id"] == owner
    # An audit that leaked a secret would be worse than no audit.
    blob = repr(events).lower()
    for forbidden in ("password", "hash", "bearer", "secret"):
        assert forbidden not in blob


# ===========================================================================
# Device tombstone, credential revocation, primary removal
# ===========================================================================
@pg_required
def test_a_device_is_tombstoned_and_its_credentials_revoked(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c)
        device_id, public_id = _device(c, store)
        credential = _credential(c, device_id)

    result = device_deletion.permanently_delete_device_with_history(
        pg, public_id=public_id, typed_confirmation=public_id, actor_user_id=owner)
    assert result.public_id == public_id

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT status, deleted_at, disabled_at FROM receiver_devices WHERE id = :i"),
            {"i": device_id}).one()
        # 'retired', not 'deleted': ck_receiver_devices_status allows only
        # active/disabled/retired, and PostgreSQL enforces a CHECK that
        # SQLite would also have enforced - the tombstone is deleted_at.
        assert row.status == "retired"
        assert row.deleted_at is not None
        assert row.disabled_at is not None, "a retired Device requires disabled_at"

        revoked = c.execute(text(
            "SELECT revoked_at FROM receiver_credentials WHERE id = :i"),
            {"i": credential}).scalar_one()
        assert revoked is not None, "a deleted Device must not keep a usable credential"


@pg_required
def test_deleting_the_primary_device_removes_the_primary_assignment(pg):
    """Losing a primary never auto-promotes.

    The Store is left with no primary until an administrator chooses one.
    A spare machine promoting itself is how a Store ends up playing
    announcements on the wrong speakers.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c)
        device_id, public_id = _device(c, store)
        standby_id, _ = _device(c, store)
        _make_primary(c, store, device_id)

    device_deletion.permanently_delete_device_with_history(
        pg, public_id=public_id, typed_confirmation=public_id, actor_user_id=owner)

    with pg.connect() as c:
        remaining = c.execute(text(
            "SELECT count(*) FROM receiver_store_primary_device WHERE store_id = :s"),
            {"s": store}).scalar_one()
        assert remaining == 0, "the Store must be left with NO primary, not a new one"
        assert c.execute(text("SELECT status FROM receiver_devices WHERE id = :i"),
                         {"i": standby_id}).scalar_one() == "active"


@pg_required
def test_a_tombstoned_device_cannot_be_tombstoned_twice(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c)
        _, public_id = _device(c, store)

    device_deletion.permanently_delete_device_with_history(
        pg, public_id=public_id, typed_confirmation=public_id, actor_user_id=owner)
    with pytest.raises(device_deletion.DeviceDeletionRefused):
        device_deletion.permanently_delete_device_with_history(
            pg, public_id=public_id, typed_confirmation=public_id, actor_user_id=owner)


# ===========================================================================
# Foreign-key behaviour - the reason tombstones exist at all
# ===========================================================================
@pg_required
def test_postgresql_refuses_to_orphan_history_that_names_a_user(pg):
    """PostgreSQL enforces this always; SQLite only with PRAGMA foreign_keys=ON.

    This is the fact the whole tombstone design is built on: the row cannot
    simply be removed, so it is emptied of operational meaning instead.
    """
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        user_id = _user(c, "historic", "BROADCASTER")
        _session(c, user_id)

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text("DELETE FROM hq_users WHERE id = :i"), {"i": user_id})


@pg_required
def test_postgresql_refuses_to_orphan_a_target_that_names_a_store(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        user_id = _user(c)
        store = _store(c)
        session_id = _session(c, user_id)
        _target(c, session_id, store)

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text("DELETE FROM stores WHERE id = :i"), {"i": store})


# ===========================================================================
# Broadcast History: archive / unarchive / permanent delete
# ===========================================================================
@pg_required
def test_sessions_archive_unarchive_and_delete_permanently(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c)
        keep = _session(c, owner, "kept")
        hide = _session(c, owner, "hidden")
        _target(c, hide, store)

    archived = admin_records.archive_sessions(pg, session_ids=[hide], actor_user_id=owner)
    assert archived.affected == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT archived_at FROM broadcast_sessions WHERE id = :i"),
                         {"i": hide}).scalar_one() is not None
        assert c.execute(text("SELECT archived_at FROM broadcast_sessions WHERE id = :i"),
                         {"i": keep}).scalar_one() is None

    restored = admin_records.archive_sessions(pg, session_ids=[hide], actor_user_id=owner,
                                              archived=False)
    assert restored.affected == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT archived_at FROM broadcast_sessions WHERE id = :i"),
                         {"i": hide}).scalar_one() is None

    # Real deletion: History IS the history, so this genuinely removes rows -
    # and must take the child targets with it rather than orphaning them.
    removed = admin_records.delete_sessions_permanently(
        pg, session_ids=[hide], actor_user_id=owner)
    assert removed.affected == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM broadcast_sessions WHERE id = :i"),
                         {"i": hide}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM broadcast_targets WHERE session_id = :i"),
                         {"i": hide}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM broadcast_sessions WHERE id = :i"),
                         {"i": keep}).scalar_one() == 1


@pg_required
def test_logs_archive_and_delete_permanently(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        keep = _log(c, "kept entry")
        hide = _log(c, "hidden entry")

    assert admin_records.archive_logs(pg, log_ids=[hide], actor_user_id=owner).affected == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT archived_at FROM system_logs WHERE id = :i"),
                         {"i": hide}).scalar_one() is not None

    assert admin_records.delete_logs_permanently(
        pg, log_ids=[hide], actor_user_id=owner).affected == 1
    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM system_logs WHERE id = :i"),
                         {"i": hide}).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM system_logs WHERE id = :i"),
                         {"i": keep}).scalar_one() == 1


@pg_required
def test_the_purge_audit_survives_the_purge_it_records(pg):
    """The property the separate audit table exists for.

    Delete every log there is, then ask what happened. If the answer were
    stored in system_logs, deleting the logs would delete the answer.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        first = _log(c, "one")
        second = _log(c, "two")

    admin_records.delete_logs_permanently(pg, log_ids=[first, second], actor_user_id=owner)

    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM system_logs")).scalar_one() == 0

    events = admin_records.list_admin_deletion_events(pg, record_type="system_log")
    assert events, "the audit must outlive the rows it describes"
    # The vocabulary is past tense - 'deleted', not 'delete'. Pinned here so a
    # reader of the audit table knows which word to filter on.
    assert any(e["action"] == "deleted" for e in events)
    assert all(e["actor_user_id"] == owner for e in events)
    assert sum(e["affected_count"] for e in events if e["action"] == "deleted") == 2


@pg_required
def test_structured_log_entity_fields_round_trip_on_postgresql(pg):
    """The columns the User/Store/Device log filters are built on.

    They are nullable and never back-filled: older rows legitimately hold
    NULL, and a filter must narrow rather than invent a relationship.
    """
    with pg.begin() as c:
        actor = _user(c, "operator", "ADMIN")
        store = _store(c)
        _, public_id = _device(c, store)
        structured = _log(c, "structured entry", actor=actor, store=store, device=public_id)
        legacy = _log(c, "older free-text entry")

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT actor_user_id, store_id, device_public_id FROM system_logs WHERE id = :i"),
            {"i": structured}).one()
        assert (row.actor_user_id, row.store_id, row.device_public_id) == (actor, store, public_id)

        old = c.execute(text(
            "SELECT actor_user_id, store_id, device_public_id FROM system_logs WHERE id = :i"),
            {"i": legacy}).one()
        assert old.actor_user_id is None and old.store_id is None and old.device_public_id is None

        # A Store filter must not sweep in the unattributed row.
        matched = c.execute(text(
            "SELECT count(*) FROM system_logs WHERE store_id = :s"), {"s": store}).scalar_one()
        assert matched == 1


# ===========================================================================
# Search and filter, per screen
# ===========================================================================
@pg_required
def test_case_insensitive_search_needs_ilike_and_gets_it(pg):
    """The single most likely SQLite-to-PostgreSQL search regression.

    SQLite's LIKE is case-insensitive for ASCII by default, so a search
    written with LIKE passes there and quietly matches nothing here. The
    endpoints use ilike; this pins that they must.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        _session(c, owner, "Morning Offer")

    term = like_term("morning")
    with pg.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM broadcast_sessions WHERE campaign_name ILIKE :t"),
            {"t": term}).scalar_one() == 1
        assert c.execute(text(
            "SELECT count(*) FROM broadcast_sessions WHERE campaign_name LIKE :t"),
            {"t": term}).scalar_one() == 0, (
            "LIKE is case-SENSITIVE on PostgreSQL - this is why the code uses ILIKE")


@pg_required
def test_receiver_status_search_filters_by_zone_city_store_and_primary(pg):
    with pg.begin() as c:
        north = _store(c, "BP", "Bindapur", "DELHI", "NORTH")
        west = _store(c, "AW", "Andheri West", "MUMBAI", "WEST")
        device_id, _ = _device(c, north)
        _make_primary(c, north, device_id)

    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM stores WHERE region = :r"),
                         {"r": "NORTH"}).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM stores WHERE city = :c"),
                         {"c": "MUMBAI"}).scalar_one() == 1
        assert c.execute(text(
            "SELECT count(*) FROM stores s WHERE EXISTS (SELECT 1 FROM "
            "receiver_store_primary_device p WHERE p.store_id = s.id)")).scalar_one() == 1
        assert c.execute(text(
            "SELECT count(*) FROM stores s WHERE NOT EXISTS (SELECT 1 FROM "
            "receiver_store_primary_device p WHERE p.store_id = s.id)")).scalar_one() == 1
        assert c.execute(text(
            "SELECT count(*) FROM stores WHERE store_code ILIKE :t OR store_name ILIKE :t"),
            {"t": like_term("bindapur")}).scalar_one() == 1
        assert west is not None


@pg_required
def test_user_search_filters_by_role_state_and_hides_deleted_by_default(pg):
    from user_lifecycle import list_users

    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        _user(c, "priya", "ADMIN")
        gone = _user(c, "rahul", "BROADCASTER")

    user_deletion.permanently_delete_user_with_history(
        pg, user_id=gone, typed_confirmation="rahul", actor_user_id=owner)

    visible = list_users(pg)
    assert "rahul" not in {u["username"] for u in visible}
    assert {"founder", "priya"} <= {u["username"] for u in visible}

    everything = list_users(pg, include_deleted=True)
    deleted = [u for u in everything if u["username"] == "rahul"]
    assert deleted and deleted[0]["lifecycle_state"] == "deleted"

    assert [u for u in visible if u["role"] == "ADMIN"][0]["username"] == "priya"


@pg_required
def test_receiver_device_search_separates_archived_from_deleted(pg):
    """Archived and deleted must never be one state wearing two labels.

    Archived is reversible. Deleted is a tombstone. A query that treated
    them alike would offer Restore on something that can never come back.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c)
        _, live = _device(c, store)
        archived_id, archived = _device(c, store)
        _, purged = _device(c, store)
        c.execute(text("UPDATE receiver_devices SET archived_at = :n, status = 'disabled', "
                       "disabled_at = :n WHERE id = :i"), {"n": _iso(), "i": archived_id})

    device_deletion.permanently_delete_device_with_history(
        pg, public_id=purged, typed_confirmation=purged, actor_user_id=owner)

    with pg.connect() as c:
        active = c.execute(text(
            "SELECT public_id FROM receiver_devices WHERE archived_at IS NULL "
            "AND deleted_at IS NULL")).scalars().all()
        assert active == [live]

        only_archived = c.execute(text(
            "SELECT public_id FROM receiver_devices WHERE archived_at IS NOT NULL "
            "AND deleted_at IS NULL")).scalars().all()
        assert only_archived == [archived]

        only_deleted = c.execute(text(
            "SELECT public_id FROM receiver_devices WHERE deleted_at IS NOT NULL"
        )).scalars().all()
        assert only_deleted == [purged]


@pg_required
def test_history_search_matches_a_session_once_per_store_filter_not_once_per_target(pg):
    """The EXISTS subquery, which is the whole reason it is written that way.

    A multi-target session joined to its targets returns one row per matching
    target. The list would show the same broadcast three times and the result
    count would be a lie.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        a = _store(c, "A", "Store A", "DELHI", "NORTH")
        b = _store(c, "B", "Store B", "DELHI", "NORTH")
        c_store = _store(c, "C", "Store C", "DELHI", "NORTH")
        session_id = _session(c, owner, "all three")
        for store in (a, b, c_store):
            _target(c, session_id, store)

    with pg.connect() as c:
        naive = c.execute(text(
            "SELECT count(*) FROM broadcast_sessions s JOIN broadcast_targets t "
            "ON t.session_id = s.id JOIN stores st ON st.id = t.store_id "
            "WHERE st.city = :c"), {"c": "DELHI"}).scalar_one()
        assert naive == 3, "the naive join really does duplicate - that is the trap"

        correct = c.execute(text(
            "SELECT count(*) FROM broadcast_sessions s WHERE EXISTS ("
            "SELECT 1 FROM broadcast_targets t JOIN stores st ON st.id = t.store_id "
            "WHERE t.session_id = s.id AND st.city = :c)"), {"c": "DELHI"}).scalar_one()
        assert correct == 1


@pg_required
def test_log_search_filters_by_level_date_range_and_archived_state(pg):
    with pg.begin() as c:
        old = _log(c, "old entry", "info",
                   created=datetime.now(timezone.utc) - timedelta(days=10))
        recent = _log(c, "recent failure", "error")
        archived = _log(c, "archived entry", "warn")
        c.execute(text("UPDATE system_logs SET archived_at = :n WHERE id = :i"),
                  {"n": _iso(), "i": archived})

    start = parse_date((datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    end = parse_date(datetime.now(timezone.utc).date().isoformat(), end_of_day=True)

    with pg.connect() as c:
        assert c.execute(text("SELECT count(*) FROM system_logs WHERE level = :l"),
                         {"l": "error"}).scalar_one() == 1
        in_window = c.execute(text(
            "SELECT id FROM system_logs WHERE created_at >= :s AND created_at <= :e "
            "ORDER BY id"), {"s": start, "e": end}).scalars().all()
        assert old not in in_window and recent in in_window

        assert c.execute(text(
            "SELECT count(*) FROM system_logs WHERE archived_at IS NULL")).scalar_one() == 2
        assert c.execute(text(
            "SELECT count(*) FROM system_logs WHERE archived_at IS NOT NULL")).scalar_one() == 1


@pg_required
def test_paging_is_stable_and_bounded_on_postgresql(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        for index in range(120):
            _log(c, f"entry number {index}")

    page, size = normalize_paging(2, 50)
    assert (page, size) == (2, 50)
    assert normalize_paging(0, 10_000)[1] <= 200, "page_size must stay bounded"

    with pg.connect() as c:
        total = c.execute(text("SELECT count(*) FROM system_logs")).scalar_one()
        first = c.execute(text(
            "SELECT id FROM system_logs ORDER BY id DESC LIMIT 50 OFFSET 0")).scalars().all()
        second = c.execute(text(
            "SELECT id FROM system_logs ORDER BY id DESC LIMIT 50 OFFSET 50")).scalars().all()
    assert total == 120
    assert len(first) == 50 and len(second) == 50
    assert not (set(first) & set(second)), "pages must not overlap"
    assert owner is not None


# ===========================================================================
# Select All Filtered - resolved by the BACKEND, from the filter
# ===========================================================================
@pg_required
def test_select_all_filtered_resolves_the_filter_against_postgresql(pg):
    """The property the whole bulk design turns on.

    The client sends the FILTER, never an enumerated id list, and the
    backend resolves the matched set itself - inside the caller's own scope,
    using the same query the list used. This proves that resolution works on
    PostgreSQL and that the resulting ids are what actually get acted on.
    """
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        for index in range(60):
            _log(c, f"entry {index}", "error" if index % 2 == 0 else "info")

    def resolver(filters):
        with pg.connect() as c:
            return c.execute(text("SELECT id FROM system_logs WHERE level = :l ORDER BY id"),
                             {"l": filters["level"]}).scalars().all()

    ids, matched = resolve_bulk_selection("filtered", None, {"level": "error"},
                                          resolver=resolver)
    assert matched == 30 and len(ids) == 30

    result = admin_records.delete_logs_permanently(pg, log_ids=list(ids), actor_user_id=owner)
    assert result.affected == 30

    with pg.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM system_logs WHERE level = 'error'")).scalar_one() == 0
        assert c.execute(text(
            "SELECT count(*) FROM system_logs WHERE level = 'info'")).scalar_one() == 30


@pg_required
def test_explicit_ids_mode_acts_on_exactly_those_rows(pg):
    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        first = _log(c, "one")
        second = _log(c, "two")
        third = _log(c, "three")

    ids, matched = resolve_bulk_selection("ids", [first, third], None,
                                          resolver=lambda f: [])
    assert matched == 2 and set(ids) == {first, third}

    admin_records.delete_logs_permanently(pg, log_ids=list(ids), actor_user_id=owner)
    with pg.connect() as c:
        remaining = c.execute(text("SELECT id FROM system_logs ORDER BY id")).scalars().all()
    assert remaining == [second]


@pg_required
def test_an_unknown_bulk_mode_is_refused_rather_than_guessed(pg):
    with pytest.raises(BulkSelectionError):
        resolve_bulk_selection("everything", None, None, resolver=lambda f: [])


# ===========================================================================
# Boolean columns - the defect class this round actually found
# ===========================================================================
# SQLite has no boolean type and happily compares `is_active = 1`. PostgreSQL
# refuses: "column is of type boolean but expression is of type integer". Four
# raw-SQL sites carried that literal, and every one of them would have failed
# only in production. These tests exercise each site on PostgreSQL so the
# literal cannot come back.
@pg_required
def test_store_permanent_deletion_writes_a_boolean_not_an_integer(pg):
    import store_deletion

    with pg.begin() as c:
        owner = _user(c, "founder", "OWNER")
        store = _store(c, "DEAD", "Doomed Store")

    store_deletion.permanently_delete_store_with_history(
        pg, store_id=store, typed_confirmation="DEAD", actor_user_id=owner)

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT lifecycle_state, is_active, deleted_at FROM stores WHERE id = :i"),
            {"i": store}).one()
    assert row.lifecycle_state == "deleted"
    assert row.is_active is False
    assert row.deleted_at is not None


@pg_required
def test_the_store_lifecycle_backfill_runs_on_postgresql(pg):
    """This migration runs at EVERY start-up.

    With the integer literal it carried, HQ would not have booted at all
    against PostgreSQL - the failure would have been total rather than
    confined to one feature.
    """
    from store_lifecycle import ensure_store_lifecycle_schema

    with pg.begin() as c:
        active = _store(c, "ACT", "Active Store")
        idle = _store(c, "IDL", "Idle Store")
        c.execute(text("UPDATE stores SET is_active = :flag WHERE id = :i"),
                  {"flag": False, "i": idle})
        c.execute(text("UPDATE stores SET lifecycle_state = NULL"))

    ensure_store_lifecycle_schema(pg)   # must not raise

    with pg.connect() as c:
        states = dict(c.execute(text(
            "SELECT id, lifecycle_state FROM stores ORDER BY id")).all())
    assert states[active] == "active"
    assert states[idle] == "disabled"


@pg_required
def test_the_active_device_query_used_by_the_migration_service_runs(pg):
    from receiver_migration_transition_service import _active_device_rows

    with pg.begin() as c:
        live_store = _store(c, "LIVE", "Live Store")
        idle_store = _store(c, "IDLE", "Idle Store")
        live_device, _ = _device(c, live_store)
        _device(c, idle_store)
        c.execute(text("UPDATE stores SET is_active = :flag WHERE id = :i"),
                  {"flag": False, "i": idle_store})

    with pg.connect() as c:
        rows = _active_device_rows(c)      # must not raise
    assert [r.device_id for r in rows] == [live_device], (
        "only Devices in an active Store are returned")
