"""A dialect-portable definition of every table this codebase has, until now,
only ever created with raw SQLite ``CREATE TABLE`` strings.

WHY THIS FILE EXISTS

``models.py`` already works unchanged on both SQLite and PostgreSQL, because
it declares its tables through SQLAlchemy ORM ``Column``/``Integer``/
``DateTime`` types - SQLAlchemy's dialect compiler already translates those
into the right DDL for whichever engine is connected. Nothing there needed
to change for Supabase.

The tables in ``migrations.py``, ``receiver_primary_device.py``,
``permission_catalog.py``, ``store_scope.py`` and ``store_deletion.py``
were instead created with ``connection.exec_driver_sql("CREATE TABLE ...")``
- plain strings written for SQLite specifically (``AUTOINCREMENT``, and one
``GLOB`` pattern check that has no PostgreSQL equivalent at all). Rewriting
each of those twenty-odd raw-SQL call sites to branch on dialect would be
exactly the "scattered `if postgres:` everywhere" the migration brief
explicitly asked to avoid.

Instead, this module declares the SAME tables once, as SQLAlchemy Core
``Table`` objects built from portable ``Column``/``ForeignKeyConstraint``/
``CheckConstraint`` primitives. SQLAlchemy's compiler turns
``Integer, primary_key=True, autoincrement=True`` into ``SERIAL``/identity
appropriately per dialect, and the substr()-based UTC-format checks are
standard SQL that both dialects already support. The one thing this file
deliberately does NOT reproduce is the SQLite-only ``public_id`` GLOB/format
CHECK constraint (a hex-with-dashes shape check) - that format is already
validated in Python before any INSERT (every public_id is minted by
``uuid.uuid4()`` and never accepted from outside input), so the database
constraint was defense in depth, not the only guard; PostgreSQL gets the
same defense in depth via a portable regex CHECK (``~`` is standard
PostgreSQL, and this schema is never created on SQLite - see below).

This module is used for exactly one purpose: creating a fresh PostgreSQL
production schema (via the migration tool, or in a dedicated Postgres test).
It is NEVER called against the SQLite engine - SQLite continues to get its
tables from the existing raw-SQL ``ensure_*_schema``/``run_receiver_
credential_phase_one`` functions, unchanged, exactly as documented in each of
those modules' own docstrings.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Engine

# A ForeignKeyConstraint written as a string ("stores.id") only resolves at
# DDL-compile time by looking up that table name inside its OWN Table's
# MetaData - so every table this schema references by FK has to live in the
# same MetaData as the tables declared below.
#
# That MetaData must NOT be ``models.Base.metadata`` itself, even though
# that already has stores/hq_users registered on it. ``Base.metadata`` is a
# single shared, mutable, process-global object - every SQLite test in this
# codebase calls ``Base.metadata.create_all(...)`` expecting it to create
# ONLY the ORM-declared tables. Registering receiver_devices/receiver_
# credentials/etc onto it here would mean that the instant this module is
# imported anywhere in a process (a single pytest worker collects every test
# file up front), every one of those ordinary SQLite tests starts trying to
# create receiver_devices too - colliding with migrations.py's own raw
# ``CREATE TABLE receiver_devices`` and failing with "table already exists".
# That collision is exactly what a first, incorrect version of this file did
# before this comment - see docs/learning-guide.md.
#
# The fix: a genuinely separate MetaData, populated with COPIES of the ORM
# tables (via SQLAlchemy's own ``Table.to_metadata``) rather than the
# originals - so this module's FK graph resolves correctly without ever
# mutating the real ``Base.metadata`` singleton.
from sqlalchemy import MetaData

from db import Base  # noqa: E402
import models  # noqa: E402,F401  (registers Store/HQUser/etc onto Base.metadata)

metadata = MetaData()
for _orm_table in Base.metadata.tables.values():
    _orm_table.to_metadata(metadata)
del _orm_table

# ---------------------------------------------------------------------------
# Receiver Devices / credentials (migrations.py, SQLite raw SQL today)
# ---------------------------------------------------------------------------
receiver_devices = Table(
    "receiver_devices", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", String(36), nullable=False, unique=True),
    Column("store_id", Integer, nullable=False),
    Column("display_name", String(200), nullable=False),
    Column("status", String(20), nullable=False, server_default="active"),
    Column("enrolled_at", String(40), nullable=False),
    Column("disabled_at", String(40)),
    Column("archived_at", String(40)),
    Column("deleted_at", String(40)),
    Column("deleted_by", Integer),
    Column("created_by", Integer),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="RESTRICT",
                         name="fk_receiver_devices_store"),
    ForeignKeyConstraint(["created_by"], ["hq_users.id"], ondelete="SET NULL",
                         name="fk_receiver_devices_creator"),
    CheckConstraint("status IN ('active', 'disabled', 'retired')",
                    name="ck_receiver_devices_status"),
)

receiver_credentials = Table(
    "receiver_credentials", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", String(36), nullable=False, unique=True),
    Column("device_id", Integer, nullable=False),
    Column("credential_version", Integer, nullable=False),
    Column("token_format", String(24), nullable=False),
    Column("token_hash", String(128), nullable=False, unique=True),
    Column("hash_key_version", Integer, nullable=False),
    Column("status", String(20), nullable=False, server_default="active"),
    Column("expiry_policy", String(20), nullable=False, server_default="non_expiring"),
    Column("issued_at", String(40), nullable=False),
    Column("expires_at", String(40)),
    Column("revoked_at", String(40)),
    Column("replaced_at", String(40)),
    Column("accept_until", String(40)),
    Column("last_used_at", String(40)),
    Column("created_by", Integer),
    Column("replaces_credential_id", Integer),
    Column("created_at", String(40), nullable=False),
    UniqueConstraint("device_id", "credential_version",
                     name="uq_receiver_credentials_device_version"),
    ForeignKeyConstraint(["device_id"], ["receiver_devices.id"], ondelete="RESTRICT",
                         name="fk_receiver_credentials_device"),
    ForeignKeyConstraint(["created_by"], ["hq_users.id"], ondelete="SET NULL",
                         name="fk_receiver_credentials_creator"),
    ForeignKeyConstraint(["replaces_credential_id"], ["receiver_credentials.id"],
                         ondelete="SET NULL", name="fk_receiver_credentials_replaces"),
    CheckConstraint("token_format IN ('legacy_uuid_hex', 'speaklink_rcv')",
                    name="ck_receiver_credentials_format"),
    CheckConstraint("status IN ('active', 'superseded', 'revoked', 'expired')",
                    name="ck_receiver_credentials_status"),
    CheckConstraint("status != 'revoked' OR revoked_at IS NOT NULL",
                    name="ck_receiver_credentials_revocation"),
)

receiver_credential_events = Table(
    "receiver_credential_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("public_id", String(36), nullable=False, unique=True),
    Column("event_type", String(40), nullable=False),
    Column("outcome", String(20), nullable=False),
    Column("store_id", Integer),
    Column("device_id", Integer),
    Column("credential_id", Integer),
    Column("actor_user_id", Integer),
    Column("event_at", String(40), nullable=False),
    Column("reason_code", String(64)),
    Column("correlation_id", String(64)),
    Column("metadata_json", Text),
    ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="SET NULL",
                         name="fk_receiver_credential_events_store"),
    ForeignKeyConstraint(["device_id"], ["receiver_devices.id"], ondelete="SET NULL",
                         name="fk_receiver_credential_events_device"),
    ForeignKeyConstraint(["credential_id"], ["receiver_credentials.id"], ondelete="SET NULL",
                         name="fk_receiver_credential_events_credential"),
    ForeignKeyConstraint(["actor_user_id"], ["hq_users.id"], ondelete="SET NULL",
                         name="fk_receiver_credential_events_actor"),
    CheckConstraint(
        "event_type IN ('device_enrolled', 'device_enabled', 'device_disabled', "
        "'credential_issued', 'credential_rotated', 'credential_revoked', "
        "'authentication_succeeded', 'authentication_failed', "
        "'migration_state_changed')",
        name="ck_receiver_credential_events_type",
    ),
    CheckConstraint("outcome IN ('success', 'rejected', 'failed')",
                    name="ck_receiver_credential_events_outcome"),
)

# ---------------------------------------------------------------------------
# Primary Device mapping (receiver_primary_device.py)
# ---------------------------------------------------------------------------
receiver_store_primary_device = Table(
    "receiver_store_primary_device", metadata,
    Column("store_id", Integer, primary_key=True),
    Column("device_id", Integer, nullable=False, unique=True),
    Column("promoted_at", String(40), nullable=False),
    Column("promoted_by", Integer),
    ForeignKeyConstraint(["store_id"], ["stores.id"], ondelete="CASCADE",
                         name="fk_primary_device_store"),
    ForeignKeyConstraint(["device_id"], ["receiver_devices.id"], ondelete="CASCADE",
                         name="fk_primary_device_device"),
    ForeignKeyConstraint(["promoted_by"], ["hq_users.id"], ondelete="SET NULL",
                         name="fk_primary_device_actor"),
)

# ---------------------------------------------------------------------------
# RBAC (permission_catalog.py)
# ---------------------------------------------------------------------------
permissions = Table(
    "permissions", metadata,
    Column("code", String(100), primary_key=True),
    Column("permission_group", String(50), nullable=False),
    Column("label", String(200), nullable=False),
)

role_permissions = Table(
    "role_permissions", metadata,
    Column("role", String(30), primary_key=True),
    Column("permission_code", String(100), primary_key=True),
    ForeignKeyConstraint(["permission_code"], ["permissions.code"],
                         name="fk_role_permissions_code"),
)

user_permission_overrides = Table(
    "user_permission_overrides", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("permission_code", String(100), nullable=False),
    Column("effect", String(10), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    UniqueConstraint("user_id", "permission_code", name="uq_user_permission_overrides"),
    ForeignKeyConstraint(["user_id"], ["hq_users.id"], name="fk_user_permission_overrides_user"),
    ForeignKeyConstraint(["permission_code"], ["permissions.code"],
                         name="fk_user_permission_overrides_code"),
    CheckConstraint("effect IN ('ALLOW', 'DENY')", name="ck_user_permission_overrides_effect"),
)

permission_audit_events = Table(
    "permission_audit_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("target_user_id", Integer, nullable=False),
    Column("permission_code", String(100), nullable=False),
    Column("old_value", String(20), nullable=False),
    Column("new_value", String(20), nullable=False),
    Column("created_at", String(40), nullable=False),
)

# ---------------------------------------------------------------------------
# Store/City/Zone scope (store_scope.py)
# ---------------------------------------------------------------------------
user_store_scope = Table(
    "user_store_scope", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, nullable=False),
    Column("scope_type", String(10), nullable=False),
    Column("store_id", Integer),
    Column("scope_value", String(100)),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(["user_id"], ["hq_users.id"], name="fk_user_store_scope_user"),
    ForeignKeyConstraint(["store_id"], ["stores.id"], name="fk_user_store_scope_store"),
    CheckConstraint("scope_type IN ('STORE', 'CITY', 'REGION')", name="ck_user_store_scope_type"),
    CheckConstraint(
        "(scope_type = 'STORE' AND store_id IS NOT NULL AND scope_value IS NULL) OR "
        "(scope_type IN ('CITY', 'REGION') AND store_id IS NULL AND scope_value IS NOT NULL)",
        name="ck_user_store_scope_shape",
    ),
)

store_scope_audit_events = Table(
    "store_scope_audit_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("target_user_id", Integer, nullable=False),
    Column("scope_type", String(10), nullable=False),
    Column("scope_label", String(200), nullable=False),
    Column("action", String(10), nullable=False),
    Column("created_at", String(40), nullable=False),
    ForeignKeyConstraint(["actor_user_id"], ["hq_users.id"], name="fk_store_scope_audit_actor"),
    ForeignKeyConstraint(["target_user_id"], ["hq_users.id"], name="fk_store_scope_audit_target"),
    CheckConstraint("scope_type IN ('STORE', 'CITY', 'REGION')", name="ck_store_scope_audit_type"),
    CheckConstraint("action IN ('ADDED', 'REMOVED')", name="ck_store_scope_audit_action"),
)

# ---------------------------------------------------------------------------
# Store tombstone audit (store_deletion.py)
# ---------------------------------------------------------------------------
store_deletion_events = Table(
    "store_deletion_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("store_id", Integer, nullable=False),
    Column("store_code", String(50), nullable=False),
    Column("store_name", String(200), nullable=False),
    Column("dependency_counts_json", Text, nullable=False),
    Column("device_public_ids_json", Text, nullable=False),
    Column("enrollment_codes_revoked", Integer, nullable=False),
    Column("credentials_revoked", Integer, nullable=False),
    Column("deleted_at", String(40), nullable=False),
)


#: Which live Broadcast Session currently holds which Store
#: (broadcast_reservation.py).
#:
#: One Store must never process two live broadcasts at once - two
#: announcements over one set of speakers is worse than either alone. That rule
#: is enforced by a PARTIAL UNIQUE INDEX on store_id WHERE released_at IS NULL,
#: created alongside this table, and not by the application logic in front of
#: it. A released row is kept rather than deleted, which is why the uniqueness
#: has to be partial: a plain UNIQUE(store_id) would let a Store be broadcast
#: to exactly once, ever.
#:
#: Written only at session lifecycle boundaries. Never per audio chunk.
broadcast_store_leases = Table(
    "broadcast_store_leases", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", Integer, nullable=False),
    Column("store_id", Integer, nullable=False),
    Column("acquired_at", String(40), nullable=False),
    Column("released_at", String(40)),
    ForeignKeyConstraint(["session_id"], ["broadcast_sessions.id"],
                         name="fk_broadcast_store_lease_session"),
    ForeignKeyConstraint(["store_id"], ["stores.id"],
                         name="fk_broadcast_store_lease_store"),
)


#: Irreversible User deletion audit (user_deletion.py). Mirrors
#: store_deletion_events: the actor, what was destroyed, and how much history
#: still points at it - never a password, a hash or a token.
user_deletion_events = Table(
    "user_deletion_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("user_id", Integer, nullable=False),
    Column("username", String(100), nullable=False),
    Column("role", String(30), nullable=False),
    Column("history_counts_json", Text, nullable=False),
    Column("deleted_at", String(40), nullable=False),
)


#: Irreversible Device deletion audit (device_deletion.py).
device_deletion_events = Table(
    "device_deletion_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("device_id", Integer, nullable=False),
    Column("public_id", String(36), nullable=False),
    Column("store_id", Integer, nullable=False),
    Column("display_name", String(200), nullable=False),
    Column("credentials_revoked", Integer, nullable=False),
    Column("was_primary", Boolean, nullable=False),
    Column("deleted_at", String(40), nullable=False),
)


#: Immutable administrative deletion audit (admin_records.py). Deliberately a
#: SEPARATE table from system_logs: a purge of system_logs must never be able
#: to erase the record of the purge. Records who/when/how many/by-what-filter -
#: never the deleted content, which would defeat the delete itself.
admin_deletion_events = Table(
    "admin_deletion_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("actor_user_id", Integer, nullable=False),
    Column("record_type", String(40), nullable=False),
    Column("action", String(20), nullable=False),
    Column("affected_count", Integer, nullable=False),
    Column("filter_json", Text, nullable=False),
    Column("created_at", String(40), nullable=False),
    CheckConstraint("record_type IN ('broadcast_session', 'system_log')",
                    name="ck_admin_deletion_events_type"),
    CheckConstraint("action IN ('archived', 'unarchived', 'deleted')",
                    name="ck_admin_deletion_events_action"),
)


# ---------------------------------------------------------------------------
# Receiver credential migration state (migrations.py)
# ---------------------------------------------------------------------------
# These two tables are read by receiver_auth_service on EVERY Receiver
# handshake. They were absent here until RC15, which meant a PostgreSQL
# deployment could authenticate nobody.
#
# WHY THEY ARE MIGRATED RATHER THAN RE-DERIVED
#
# ``run_receiver_credential_phase_one`` creates them fresh at
# ``state='legacy_only'`` with ``legacy_verification_enabled=1``. The live
# fleet is at ``hash_only`` with legacy verification OFF - meaning a Store's
# old shared token is no longer accepted anywhere. Re-deriving on a
# PostgreSQL start-up would therefore silently REACTIVATE legacy Store-token
# authentication.
#
# That is the dangerous direction of a security regression: it authenticates
# MORE, not less, so nothing fails, nothing alerts, and the fleet quietly
# returns to a posture it was deliberately migrated off. Copying the rows
# preserves the decision that was actually made.
schema_migrations = Table(
    "schema_migrations", metadata,
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("name", String(200), nullable=False, unique=True),
    Column("applied_at", String(40), nullable=False),
    # The SQLite original spells this with substr(); PostgreSQL takes the same
    # meaning as a portable pattern check. Both say "a UTC timestamp".
    CheckConstraint(
        "char_length(applied_at) BETWEEN 20 AND 40 "
        "AND (applied_at LIKE '%+00:00' OR applied_at LIKE '%Z')",
        name="ck_schema_migrations_applied_at_utc"),
)

receiver_credential_migration_state = Table(
    "receiver_credential_migration_state", metadata,
    Column("id", Integer, primary_key=True, autoincrement=False),
    Column("schema_version", Integer, nullable=False),
    Column("state", String(24), nullable=False),
    # INTEGER 0/1 rather than BOOLEAN, deliberately: the SQLite column is an
    # INTEGER with a CHECK IN (0,1), the migration tool copies the value
    # verbatim, and receiver_auth_service compares it against the integers in
    # _EXPECTED_LEGACY_FLAG. Making it BOOLEAN here would mean the copied 0
    # arrives as False and the comparison 'False != 0' is... equal in Python,
    # but the copy itself would have to coerce. Keeping the storage type the
    # same on both engines removes the question entirely.
    Column("legacy_verification_enabled", Integer, nullable=False, server_default="1"),
    Column("updated_at", String(40), nullable=False),
    CheckConstraint("id = 1", name="ck_receiver_credential_migration_state_singleton"),
    CheckConstraint("schema_version > 0",
                    name="ck_receiver_credential_migration_state_version"),
    CheckConstraint(
        "state IN ('legacy_only', 'backfilled', 'dual_verify', 'hash_only', "
        "'raw_neutralized')",
        name="ck_receiver_credential_migration_state_state"),
    CheckConstraint("legacy_verification_enabled IN (0, 1)",
                    name="ck_receiver_credential_migration_state_legacy_flag"),
    CheckConstraint(
        "updated_at LIKE '%+00:00' OR updated_at LIKE '%Z'",
        name="ck_receiver_credential_migration_state_updated_at_utc"),
)


# ---------------------------------------------------------------------------
# The indexes Receiver authentication REQUIRES
# ---------------------------------------------------------------------------
# receiver_auth_service._REQUIRED_INDEXES names these four and refuses to
# authenticate anybody if one is missing. On SQLite they are created by
# run_receiver_credential_phase_one; they were never declared here, so a
# PostgreSQL deployment failed that check for the whole fleet.
#
# The check is not bureaucratic. ix_receiver_credentials_auth_lookup is what
# turns the per-handshake credential lookup into an index probe instead of a
# scan of every credential in the fleet - and the moment that matters most is
# a mass reconnect after an outage, i.e. exactly when the system is already
# under stress.
Index("ix_receiver_devices_store_status",
      receiver_devices.c.store_id, receiver_devices.c.status)
Index("ix_receiver_credentials_public_id",
      receiver_credentials.c.public_id, unique=True)
Index("ix_receiver_credentials_auth_lookup",
      receiver_credentials.c.hash_key_version, receiver_credentials.c.token_hash)
Index("ix_receiver_credentials_device_status",
      receiver_credentials.c.device_id, receiver_credentials.c.status)


def create_all(engine: Engine) -> None:
    """Create the complete PostgreSQL production schema - every ORM table in
    models.py (Store, HQUser, BroadcastSession, ...) AND every table
    declared in this module, in one call, correctly FK-ordered.

    Idempotent (``checkfirst`` is the default for ``create_all``) - safe to
    call at the start of a migration run without first checking what
    already exists.
    """
    metadata.create_all(engine, checkfirst=True)


__all__ = [
    "admin_deletion_events",
    "create_all",
    "receiver_credential_migration_state",
    "schema_migrations",
    "device_deletion_events",
    "metadata",
    "permission_audit_events",
    "permissions",
    "receiver_credential_events",
    "receiver_credentials",
    "receiver_devices",
    "receiver_store_primary_device",
    "role_permissions",
    "store_deletion_events",
    "store_scope_audit_events",
    "user_deletion_events",
    "user_permission_overrides",
    "user_store_scope",
]
