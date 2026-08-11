"""Application behaviour against REAL PostgreSQL, not just schema shape.

``test_postgres_schema.py`` proves the DDL compiles and the constraints
exist. That is necessary and not sufficient: a schema can be perfectly
valid while the application's actual reads and writes still behave
differently than they do on SQLite. This file exercises the operations
EchoCast actually performs - creating a User, editing a Store, tombstoning
one, resolving RBAC and Scope, enrolling a Device, recording a broadcast -
against a live PostgreSQL server.

WHAT THIS FILE DELIBERATELY DOES NOT DO

It does not run the ordinary SQLite suite against PostgreSQL. Those tests
assume SQLite-specific machinery (``PRAGMA``, the SQLite backup API, raw
``CREATE TABLE ... AUTOINCREMENT`` migrations) and rewriting them to be
dual-dialect would be a far larger change than the migration itself
warrants. These are dedicated integration tests instead.

ISOLATION

Every test runs inside a freshly generated ``echocast_test_*`` schema and
drops exactly that schema afterwards - see ``pg_engine`` in
``test_postgres_schema.py`` for the three properties that make the
confinement provable. Nothing here can reach ``public`` or any
Supabase-managed schema (``auth``, ``storage``, ``realtime``, ``vault``,
``extensions``).

Skipped entirely unless ``TEST_POSTGRES_URL`` is set, so an ordinary
offline ``pytest`` run is unaffected.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# Same value every module in this suite uses - see the note in
# test_postgres_schema.py about why this must not be a path of our choosing.
os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

import postgres_schema  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Reuse the isolated-schema fixture rather than defining a second one: one
# implementation of the safety property, not two that can drift apart.
from tests.test_postgres_schema import pg_engine, pg_required  # noqa: E402,F401


UTC_NOW = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731


@pytest.fixture()
def pg(pg_engine):
    """An isolated PostgreSQL schema with the full EchoCast schema created."""
    postgres_schema.create_all(pg_engine)
    return pg_engine


def _make_store(c, code="BP", name="Bindapur", city="UN ZONE", region="UN ZONE"):
    return c.execute(text(
        "INSERT INTO stores (store_code, store_name, city, region, is_online_store, "
        "receiver_token, is_active, lifecycle_state, status, created_at, updated_at) "
        "VALUES (:code, :name, :city, :region, false, :tok, true, 'active', 'offline', "
        "now(), now()) RETURNING id"
    ), {"code": code, "name": name, "city": city, "region": region,
        "tok": uuid.uuid4().hex}).scalar_one()


def _make_user(c, username="founder", role="OWNER"):
    return c.execute(text(
        "INSERT INTO hq_users (username, password_hash, role, is_active, "
        "session_version, created_at, lifecycle_state) "
        "VALUES (:u, :h, :r, true, 1, now(), 'active') RETURNING id"
    ), {"u": username, "h": "not-a-real-hash-" + uuid.uuid4().hex, "r": role}).scalar_one()


def _make_device(c, store_id, status="active"):
    public_id = str(uuid.uuid4())
    now = UTC_NOW()
    device_id = c.execute(text(
        "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
        "enrolled_at, created_at, updated_at) "
        "VALUES (:pid, :sid, 'Test Device', :st, :now, :now, :now) RETURNING id"
    ), {"pid": public_id, "sid": store_id, "st": status, "now": now}).scalar_one()
    return device_id, public_id


# ===========================================================================
# Users / auth-shaped operations
# ===========================================================================
@pg_required
def test_user_creation_and_unique_username_enforcement(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        user_id = _make_user(c, "founder", "OWNER")
        assert user_id > 0
        row = c.execute(text("SELECT username, role, is_active FROM hq_users WHERE id=:i"),
                        {"i": user_id}).first()
        assert row.username == "founder" and row.role == "OWNER" and row.is_active is True

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            _make_user(c, "founder", "ADMIN")


@pg_required
def test_password_hash_is_stored_and_never_defaulted_away(pg):
    with pg.begin() as c:
        user_id = _make_user(c, "hashcheck")
        stored = c.execute(text("SELECT password_hash FROM hq_users WHERE id=:i"),
                           {"i": user_id}).scalar_one()
    assert stored and stored.startswith("not-a-real-hash-")


# ===========================================================================
# Store CRUD / lifecycle / tombstone
# ===========================================================================
@pg_required
def test_store_crud_and_unique_store_code(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        store_id = _make_store(c, "BP", "Bindapur")
        c.execute(text("UPDATE stores SET store_name='Bindapur Renamed' WHERE id=:i"),
                  {"i": store_id})
        assert c.execute(text("SELECT store_name FROM stores WHERE id=:i"),
                         {"i": store_id}).scalar_one() == "Bindapur Renamed"

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            _make_store(c, "BP", "Duplicate Code")


@pg_required
def test_store_lifecycle_transitions_persist(pg):
    with pg.begin() as c:
        store_id = _make_store(c)
        for state, active in (("disabled", False), ("archived", False), ("active", True)):
            c.execute(text("UPDATE stores SET lifecycle_state=:s, is_active=:a WHERE id=:i"),
                      {"s": state, "a": active, "i": store_id})
            row = c.execute(text("SELECT lifecycle_state, is_active FROM stores WHERE id=:i"),
                            {"i": store_id}).first()
            assert row.lifecycle_state == state and row.is_active is active


@pg_required
def test_store_tombstone_fields_and_deletion_audit_survive(pg):
    """The history-preserving permanent delete: the Store row stays, its
    identity stays readable, and the audit row records what happened."""
    with pg.begin() as c:
        store_id = _make_store(c, "TESTSTORE", "TESTPC")
        session_id = c.execute(text(
            "INSERT INTO broadcast_sessions (campaign_name, started_by, status, "
            "target_mode, selected_store_count, created_at) "
            "VALUES ('history', :u, 'ended', 'selected', 1, now()) RETURNING id"
        ), {"u": _make_user(c, "actor")}).scalar_one()
        c.execute(text(
            "INSERT INTO broadcast_targets (session_id, store_id, play_status) "
            "VALUES (:s, :st, 'stopped')"), {"s": session_id, "st": store_id})

        now = UTC_NOW()
        c.execute(text(
            "UPDATE stores SET lifecycle_state='deleted', is_active=false, "
            "deleted_at=:now, deleted_by=1 WHERE id=:i"), {"now": now, "i": store_id})
        c.execute(text(
            "INSERT INTO store_deletion_events (actor_user_id, store_id, store_code, "
            "store_name, dependency_counts_json, device_public_ids_json, "
            "enrollment_codes_revoked, credentials_revoked, deleted_at) "
            "VALUES (1, :i, 'TESTSTORE', 'TESTPC', '{}', '[]', 0, 1, :now)"),
            {"i": store_id, "now": now})

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT store_code, store_name, lifecycle_state, deleted_at, deleted_by "
            "FROM stores WHERE id=:i"), {"i": store_id}).first()
        assert row.lifecycle_state == "deleted"
        assert row.store_code == "TESTSTORE" and row.store_name == "TESTPC"
        assert row.deleted_at is not None and row.deleted_by == 1

        # History still readable, still pointing at a real Store name.
        assert c.execute(text(
            "SELECT COUNT(*) FROM broadcast_targets WHERE store_id=:i"),
            {"i": store_id}).scalar_one() == 1
        assert c.execute(text(
            "SELECT store_code FROM store_deletion_events WHERE store_id=:i"),
            {"i": store_id}).scalar_one() == "TESTSTORE"


# ===========================================================================
# RBAC
# ===========================================================================
@pg_required
def test_rbac_permissions_roles_and_overrides(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        user_id = _make_user(c, "rbacuser", "BROADCASTER")
        for code in ("menu.stores.view", "stores.delete_permanently"):
            c.execute(text(
                "INSERT INTO permissions (code, permission_group, label) "
                "VALUES (:c, 'Stores', :c)"), {"c": code})
        c.execute(text(
            "INSERT INTO role_permissions (role, permission_code) "
            "VALUES ('BROADCASTER', 'menu.stores.view')"))
        c.execute(text(
            "INSERT INTO user_permission_overrides (user_id, permission_code, effect, "
            "created_at, updated_at) VALUES (:u, 'stores.delete_permanently', 'DENY', "
            ":now, :now)"), {"u": user_id, "now": UTC_NOW()})

    with pg.connect() as c:
        assert c.execute(text(
            "SELECT effect FROM user_permission_overrides WHERE user_id=:u"),
            {"u": user_id}).scalar_one() == "DENY"

    # The CHECK constraint on effect is real on PostgreSQL too.
    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text(
                "INSERT INTO user_permission_overrides (user_id, permission_code, effect, "
                "created_at, updated_at) VALUES (:u, 'menu.stores.view', 'MAYBE', "
                ":now, :now)"), {"u": user_id, "now": UTC_NOW()})


# ===========================================================================
# Store / City / Zone scope
# ===========================================================================
@pg_required
def test_store_scope_rows_and_their_shape_constraint(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        user_id = _make_user(c, "scopeduser", "ADMIN")
        store_id = _make_store(c)
        now = UTC_NOW()
        c.execute(text(
            "INSERT INTO user_store_scope (user_id, scope_type, store_id, scope_value, "
            "created_at) VALUES (:u, 'STORE', :s, NULL, :now)"),
            {"u": user_id, "s": store_id, "now": now})
        c.execute(text(
            "INSERT INTO user_store_scope (user_id, scope_type, store_id, scope_value, "
            "created_at) VALUES (:u, 'CITY', NULL, 'UN ZONE', :now)"),
            {"u": user_id, "now": now})

    with pg.connect() as c:
        assert c.execute(text(
            "SELECT COUNT(*) FROM user_store_scope WHERE user_id=:u"),
            {"u": user_id}).scalar_one() == 2

    # A STORE row carrying a scope_value violates the shape CHECK - the same
    # rule SQLite enforces, proven to survive the dialect change.
    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text(
                "INSERT INTO user_store_scope (user_id, scope_type, store_id, scope_value, "
                "created_at) VALUES (:u, 'STORE', :s, 'BOTH', :now)"),
                {"u": user_id, "s": store_id, "now": UTC_NOW()})


# ===========================================================================
# Receiver Devices / credentials / primary assignment
# ===========================================================================
@pg_required
def test_receiver_device_enrolment_and_status_constraint(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        store_id = _make_store(c)
        device_id, public_id = _make_device(c, store_id)
        assert device_id > 0

    with pg.connect() as c:
        row = c.execute(text(
            "SELECT public_id, status, archived_at FROM receiver_devices WHERE id=:i"),
            {"i": device_id}).first()
        assert row.public_id == public_id and row.status == "active"
        assert row.archived_at is None

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            _make_device(c, store_id, status="not-a-real-status")


@pg_required
def test_receiver_credential_uniqueness_and_revocation_rule(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        store_id = _make_store(c)
        device_id, _ = _make_device(c, store_id)
        now = UTC_NOW()
        c.execute(text(
            "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
            "token_format, token_hash, hash_key_version, status, issued_at, created_at) "
            "VALUES (:p, :d, 1, 'echocast_rcv', :h, 1, 'active', :now, :now)"),
            {"p": str(uuid.uuid4()), "d": device_id, "h": "a" * 64, "now": now})

    # (device_id, credential_version) is unique.
    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text(
                "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
                "token_format, token_hash, hash_key_version, status, issued_at, created_at) "
                "VALUES (:p, :d, 1, 'echocast_rcv', :h, 1, 'active', :now, :now)"),
                {"p": str(uuid.uuid4()), "d": device_id, "h": "b" * 64, "now": UTC_NOW()})

    # status='revoked' requires revoked_at - the CHECK survives the dialect.
    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text(
                "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
                "token_format, token_hash, hash_key_version, status, issued_at, created_at) "
                "VALUES (:p, :d, 2, 'echocast_rcv', :h, 1, 'revoked', :now, :now)"),
                {"p": str(uuid.uuid4()), "d": device_id, "h": "c" * 64, "now": UTC_NOW()})


@pg_required
def test_primary_assignment_is_at_most_one_per_store(pg):
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        store_id = _make_store(c)
        first_id, first_public = _make_device(c, store_id)
        second_id, _ = _make_device(c, store_id)
        c.execute(text(
            "INSERT INTO receiver_store_primary_device (store_id, device_id, promoted_at) "
            "VALUES (:s, :d, :now)"),
            {"s": store_id, "d": first_id, "now": UTC_NOW()})

    with pg.connect() as c:
        assert c.execute(text(
            "SELECT device_id FROM receiver_store_primary_device WHERE store_id=:s"),
            {"s": store_id}).scalar_one() == first_id

    # store_id is the PRIMARY KEY: a second primary for the same Store is
    # impossible at the database level, not merely discouraged.
    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text(
                "INSERT INTO receiver_store_primary_device (store_id, device_id, promoted_at) "
                "VALUES (:s, :d, :now)"),
                {"s": store_id, "d": second_id, "now": UTC_NOW()})


@pg_required
def test_a_devices_store_cannot_be_hard_deleted_out_from_under_it(pg):
    """receiver_devices -> stores is ON DELETE RESTRICT. That is what makes
    the tombstone model necessary rather than optional."""
    from sqlalchemy.exc import IntegrityError

    with pg.begin() as c:
        store_id = _make_store(c)
        _make_device(c, store_id)

    with pytest.raises(IntegrityError):
        with pg.begin() as c:
            c.execute(text("DELETE FROM stores WHERE id=:i"), {"i": store_id})


# ===========================================================================
# Broadcast history / Receiver events
# ===========================================================================
@pg_required
def test_broadcast_session_targets_and_receiver_events(pg):
    with pg.begin() as c:
        user_id = _make_user(c, "caster", "BROADCASTER")
        store_id = _make_store(c)
        session_id = c.execute(text(
            "INSERT INTO broadcast_sessions (campaign_name, started_by, status, "
            "target_mode, selected_store_count, online_store_count, offline_store_count, "
            "created_at) VALUES ('Evening announcement', :u, 'live', 'selected', 1, 1, 0, "
            "now()) RETURNING id"), {"u": user_id}).scalar_one()
        c.execute(text(
            "INSERT INTO broadcast_targets (session_id, store_id, play_status) "
            "VALUES (:s, :st, 'playing')"), {"s": session_id, "st": store_id})
        c.execute(text(
            "INSERT INTO receiver_events (store_id, event_type, event_time, details) "
            "VALUES (:st, 'connected', now(), NULL)"), {"st": store_id})

    with pg.connect() as c:
        assert c.execute(text(
            "SELECT campaign_name FROM broadcast_sessions WHERE id=:i"),
            {"i": session_id}).scalar_one() == "Evening announcement"
        assert c.execute(text(
            "SELECT play_status FROM broadcast_targets WHERE session_id=:i"),
            {"i": session_id}).scalar_one() == "playing"
        assert c.execute(text(
            "SELECT event_type FROM receiver_events WHERE store_id=:i"),
            {"i": store_id}).scalar_one() == "connected"


# ===========================================================================
# Timestamps
# ===========================================================================
@pg_required
def test_timestamps_round_trip_without_losing_utc(pg):
    """EchoCast stores UTC everywhere. A dialect change must not quietly
    shift a stored instant - a broadcast history off by 5h30m would be
    both wrong and very believable."""
    with pg.begin() as c:
        user_id = _make_user(c, "timecheck")
        written = datetime(2026, 8, 1, 13, 0, 54, tzinfo=timezone.utc)
        session_id = c.execute(text(
            "INSERT INTO broadcast_sessions (campaign_name, started_by, status, "
            "target_mode, selected_store_count, created_at, started_at) "
            "VALUES ('utc', :u, 'ended', 'all', 0, :ts, :ts) RETURNING id"),
            {"u": user_id, "ts": written}).scalar_one()

    with pg.connect() as c:
        read = c.execute(text("SELECT started_at FROM broadcast_sessions WHERE id=:i"),
                         {"i": session_id}).scalar_one()

    # The column is naive (TIMESTAMP WITHOUT TIME ZONE) on both dialects, so
    # compare the wall-clock fields: what went in is what comes out, with no
    # timezone conversion applied along the way.
    assert (read.year, read.month, read.day, read.hour, read.minute, read.second) == \
           (2026, 8, 1, 13, 0, 54)


@pg_required
def test_string_utc_timestamps_keep_their_offset_suffix(pg):
    """Device/credential tables store ISO-8601 strings with an explicit UTC
    offset, and a CHECK constraint enforces it. Proven to hold on
    PostgreSQL, where the column is VARCHAR exactly as it is on SQLite."""
    with pg.begin() as c:
        store_id = _make_store(c)
        _, public_id = _make_device(c, store_id)
        enrolled = c.execute(text(
            "SELECT enrolled_at FROM receiver_devices WHERE public_id=:p"),
            {"p": public_id}).scalar_one()
    assert enrolled.endswith("+00:00"), enrolled
