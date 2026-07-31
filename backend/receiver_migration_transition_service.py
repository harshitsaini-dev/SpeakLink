"""Isolated transactional Receiver Credential migration-state transitions.

This module is not integrated with startup, FastAPI, WebSockets, the default
database engine, or receiver snapshots. Callers inject a SQLite Engine, HMAC
key ring, and (for narrowing transitions) a trusted connection summary.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
import re
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from migrations import PHASE_ONE_NAME, PHASE_ONE_VERSION, PROTECTED_DATABASE_PATH
from receiver_credentials import (
    MIN_HASH_KEY_BYTES,
    CredentialState,
    credential_is_usable,
    sanitize_audit_payload,
    verify_legacy_receiver_token,
)


MAX_CONNECTION_SUMMARY_AGE = timedelta(seconds=30)
MAX_HASH_KEY_VERSIONS = 16
LEGACY_TOKEN_FORMAT = "legacy_uuid_hex"
NEW_TOKEN_FORMAT = "speaklink_rcv"

_RAW_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_HASH_PATTERN = re.compile(
    r"^hmac-sha256\$v(?P<key_version>[1-9][0-9]*)\$(?P<digest>[a-f0-9]{64})$"
)
_KNOWN_STATES = {
    "legacy_only",
    "backfilled",
    "dual_verify",
    "hash_only",
    "raw_neutralized",
}
_EXPECTED_FLAGS = {
    "legacy_only": 1,
    "backfilled": 1,
    "dual_verify": 1,
    "hash_only": 0,
    "raw_neutralized": 0,
}


class RuntimeAction(str, Enum):
    NONE = "none"
    LEGACY_CONNECTIONS_MUST_BE_ZERO = "legacy_connections_must_be_zero"
    HASHED_CONNECTIONS_MUST_BE_ZERO = "hashed_connections_must_be_zero"


@dataclass(frozen=True, slots=True)
class _TransitionPolicy:
    target_flag: int
    reason_code: str
    runtime_action: RuntimeAction
    require_hash_readiness: bool
    #: Which fleet shape this transition is allowed to validate.
    #:
    #: "backfilled" is the original assumption: one Device and one credential per
    #: Store, produced by the legacy backfill. "hashed_fleet" is a fleet that was
    #: enrolled directly and therefore has FEWER Devices than Stores - which the
    #: backfilled checks can never accept, and which is exactly the shape the
    #: live HQ is in.
    fleet: str = "backfilled"


_ALLOWED_TRANSITIONS = {
    # legacy_only -> hash_only, for a fleet that was enrolled directly.
    #
    # WHY THIS EXISTS. The live HQ sits in legacy_only while holding four
    # hashed Device credentials, so the runtime never computes a hashed identity
    # and refuses every one of them. The documented way out is
    # backfill -> backfilled -> dual_verify, and it cannot be taken: the backfill
    # demands zero Devices, zero credentials and zero audit events, and
    # dual_verify additionally demands one backfilled Device PER STORE. A fleet
    # of 4 Devices across 44 Stores satisfies neither, and forcing it to would
    # mean deleting the Devices and the audit history to recreate them.
    #
    # hash_only is hash-capable and is NOT subject to the backfilled-fleet check
    # (receiver_auth_service runs that only for backfilled and dual_verify), so
    # it is reachable for this fleet without destroying anything.
    #
    # Disabling legacy verification is safe here and is checked, not assumed:
    # LEGACY_CONNECTIONS_MUST_BE_ZERO plus a scan for any legacy Receiver usage.
    ("legacy_only", "hash_only"): _TransitionPolicy(
        0,
        "enable_hash_only_for_hashed_fleet",
        RuntimeAction.LEGACY_CONNECTIONS_MUST_BE_ZERO,
        False,
        fleet="hashed_fleet",
    ),
    ("backfilled", "dual_verify"): _TransitionPolicy(
        1, "enable_dual_verification", RuntimeAction.NONE, True
    ),
    ("dual_verify", "hash_only"): _TransitionPolicy(
        0,
        "enable_hash_only",
        RuntimeAction.LEGACY_CONNECTIONS_MUST_BE_ZERO,
        True,
    ),
    ("hash_only", "dual_verify"): _TransitionPolicy(
        1, "rollback_to_dual_verification", RuntimeAction.NONE, True
    ),
    ("dual_verify", "backfilled"): _TransitionPolicy(
        1,
        "rollback_to_backfilled",
        RuntimeAction.HASHED_CONNECTIONS_MUST_BE_ZERO,
        False,
    ),
}

_REQUIRED_COLUMNS = {
    "stores": {"id", "store_code", "receiver_token", "is_active"},
    "hq_users": {"id", "is_active"},
    "receiver_devices": {"id", "public_id", "store_id", "status"},
    "receiver_credentials": {
        "id",
        "public_id",
        "device_id",
        "credential_version",
        "token_format",
        "token_hash",
        "hash_key_version",
        "status",
        "expiry_policy",
        "issued_at",
        "expires_at",
        "revoked_at",
        "replaced_at",
        "accept_until",
    },
    "receiver_credential_events": {
        "id",
        "public_id",
        "event_type",
        "outcome",
        "actor_user_id",
        "event_at",
        "reason_code",
        "metadata_json",
    },
    "receiver_credential_migration_state": {
        "id",
        "schema_version",
        "state",
        "legacy_verification_enabled",
        "updated_at",
    },
    "schema_migrations": {"version", "name", "applied_at"},
}
_REQUIRED_INDEXES = {
    "ix_receiver_devices_store_status",
    "ix_receiver_credentials_auth_lookup",
    "ix_receiver_credentials_device_status",
    "ix_receiver_credential_events_type_time",
}


class ReceiverMigrationTransitionError(RuntimeError):
    """Base class for fixed, secret-free transition failures."""


class ProtectedDatabaseError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Protected Receiver database cannot be transitioned")


class TransitionMigrationNotReadyError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration configuration is not ready")


class InvalidStateTransitionError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration state transition is not allowed")


class TransitionStateMismatchError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration state changed before transition")


class TransitionReadinessError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration transition readiness failed")


class ActiveConnectionBlockerError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Active Receiver connections block this transition")


class StaleConnectionSummaryError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver connection summary is missing or stale")


class InvalidConnectionSummaryError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver connection summary is invalid")


class TransitionAlreadyAppliedError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration state is already applied")


class TransitionPersistenceError(ReceiverMigrationTransitionError):
    def __init__(self) -> None:
        super().__init__("Receiver migration state transition could not be persisted")


def _require_utc(value: object, error_type):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise error_type()
    return value


@dataclass(frozen=True, slots=True)
class ActiveReceiverConnectionSummary:
    legacy_authenticated_count: int
    hashed_authenticated_count: int
    captured_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.legacy_authenticated_count,
            self.hashed_authenticated_count,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InvalidConnectionSummaryError()
        _require_utc(self.captured_at, InvalidConnectionSummaryError)


@dataclass(frozen=True, slots=True, repr=False)
class MigrationTransitionResult:
    previous_state: str
    new_state: str
    legacy_verification_enabled: int
    transitioned_at: datetime
    store_count: int
    active_store_count: int
    active_device_count: int
    usable_credential_count: int
    runtime_action: RuntimeAction

    def __repr__(self) -> str:
        return (
            "MigrationTransitionResult("
            f"previous_state={self.previous_state!r}, new_state={self.new_state!r}, "
            f"legacy_verification_enabled={self.legacy_verification_enabled}, "
            f"transitioned_at={self.transitioned_at!r}, "
            f"store_count={self.store_count}, "
            f"active_store_count={self.active_store_count}, "
            f"active_device_count={self.active_device_count}, "
            f"usable_credential_count={self.usable_credential_count}, "
            f"runtime_action={self.runtime_action.value!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class _ReadinessCounts:
    store_count: int
    active_store_count: int
    active_device_count: int
    usable_credential_count: int


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _validate_inputs(
    engine: Engine,
    actor_user_id: object,
    hash_keys: object,
    now: datetime | None,
) -> tuple[int, dict[int, bytes], datetime]:
    if engine.dialect.name != "sqlite":
        raise TransitionMigrationNotReadyError()
    if _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
        raise ProtectedDatabaseError()
    if isinstance(actor_user_id, bool) or not isinstance(actor_user_id, int) or actor_user_id <= 0:
        raise TransitionReadinessError()
    if not isinstance(hash_keys, Mapping) or not 1 <= len(hash_keys) <= MAX_HASH_KEY_VERSIONS:
        raise TransitionMigrationNotReadyError()
    validated_keys: dict[int, bytes] = {}
    for version, key in hash_keys.items():
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
            or not isinstance(key, bytes)
            or len(key) < MIN_HASH_KEY_BYTES
            or version in validated_keys
        ):
            raise TransitionMigrationNotReadyError()
        validated_keys[version] = key
    validated_now = _require_utc(
        now or datetime.now(timezone.utc), TransitionReadinessError
    )
    return actor_user_id, validated_keys, validated_now


def _columns(connection: Connection, table: str) -> set[str]:
    return {row[1] for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')}


def _validate_schema(connection: Connection) -> None:
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if not set(_REQUIRED_COLUMNS) <= tables:
        raise TransitionMigrationNotReadyError()
    for table, required in _REQUIRED_COLUMNS.items():
        if not required <= _columns(connection, table):
            raise TransitionMigrationNotReadyError()
    indexes = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
    if not _REQUIRED_INDEXES <= indexes:
        raise TransitionMigrationNotReadyError()
    ledger = connection.execute(
        text("SELECT name FROM schema_migrations WHERE version = :version"),
        {"version": PHASE_ONE_VERSION},
    ).scalar_one_or_none()
    if ledger != PHASE_ONE_NAME:
        raise TransitionMigrationNotReadyError()
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise TransitionMigrationNotReadyError()


def _current_state(connection: Connection) -> tuple[str, int]:
    rows = connection.execute(
        text(
            "SELECT schema_version, state, legacy_verification_enabled "
            "FROM receiver_credential_migration_state WHERE id = 1"
        )
    ).all()
    if len(rows) != 1:
        raise TransitionMigrationNotReadyError()
    row = rows[0]
    if (
        row.schema_version != PHASE_ONE_VERSION
        or row.state not in _KNOWN_STATES
        or row.legacy_verification_enabled != _EXPECTED_FLAGS.get(row.state)
    ):
        raise TransitionMigrationNotReadyError()
    return row.state, row.legacy_verification_enabled


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TransitionReadinessError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise TransitionReadinessError() from None
    return _require_utc(parsed, TransitionReadinessError)


def _credential_is_usable(row, now: datetime) -> bool:
    if row.credential_status not in {"active", "superseded", "revoked", "expired"}:
        return False
    expires_at = _parse_utc(row.expires_at)
    if row.expiry_policy == "non_expiring":
        if expires_at is not None:
            return False
    elif row.expiry_policy == "expires_at":
        if expires_at is None:
            return False
    else:
        return False
    try:
        state = CredentialState(
            issued_at=_parse_utc(row.issued_at),
            expires_at=expires_at,
            revoked_at=_parse_utc(row.revoked_at),
            replaced_at=_parse_utc(row.replaced_at),
            accept_until=_parse_utc(row.accept_until),
            is_active=row.credential_status in {"active", "superseded"},
        )
    except (TypeError, ValueError):
        return False
    return credential_is_usable(state, now)


def _load_stores(connection: Connection):
    stores = connection.execute(
        text(
            "SELECT id, receiver_token, is_active FROM stores "
            "ORDER BY id"
        )
    ).all()
    if not stores:
        raise TransitionReadinessError()
    return stores


def _validate_actor(connection: Connection, actor_user_id: int) -> None:
    actor = connection.execute(
        text("SELECT is_active FROM hq_users WHERE id = :actor_id"),
        {"actor_id": actor_user_id},
    ).scalar_one_or_none()
    if actor is None or not bool(actor):
        raise TransitionReadinessError()


def _validate_raw_readiness(stores) -> None:
    seen: set[str] = set()
    for store in stores:
        token = store.receiver_token
        if not isinstance(token, str) or _RAW_TOKEN_PATTERN.fullmatch(token) is None:
            raise TransitionReadinessError()
        if token in seen:
            raise TransitionReadinessError()
        seen.add(token)


def _credential_rows(connection: Connection):
    return connection.execute(
        text(
            """
            SELECT
                s.id AS store_id, s.receiver_token, s.is_active AS store_active,
                d.id AS device_id, d.public_id AS device_public_id,
                d.status AS device_status,
                c.id AS credential_id, c.public_id AS credential_public_id,
                c.credential_version, c.token_format, c.token_hash,
                c.hash_key_version, c.status AS credential_status,
                c.expiry_policy, c.issued_at, c.expires_at, c.revoked_at,
                c.replaced_at, c.accept_until
            FROM stores s
            JOIN receiver_devices d ON d.store_id = s.id
            JOIN receiver_credentials c ON c.device_id = d.id
            ORDER BY s.id, d.id, c.id
            """
        )
    ).all()


def _active_device_rows(connection: Connection):
    return connection.execute(
        text(
            """
            SELECT d.id AS device_id, d.store_id
            FROM receiver_devices d
            JOIN stores s ON s.id = d.store_id
            WHERE s.is_active = 1 AND d.status = 'active'
            ORDER BY d.id
            """
        )
    ).all()


def _valid_hash_structure(row, hash_keys: Mapping[int, bytes]) -> bool:
    if row.token_format not in {LEGACY_TOKEN_FORMAT, NEW_TOKEN_FORMAT}:
        return False
    match = _HASH_PATTERN.fullmatch(row.token_hash or "")
    if match is None or int(match.group("key_version")) != row.hash_key_version:
        return False
    if row.hash_key_version not in hash_keys:
        return False
    try:
        public_id = str(uuid.UUID(row.credential_public_id))
    except (ValueError, TypeError, AttributeError):
        return False
    return public_id == row.credential_public_id and row.credential_version > 0


def _validate_backfilled_mapping(
    stores,
    credential_rows,
    hash_keys: Mapping[int, bytes],
    now: datetime,
) -> None:
    by_store: dict[int, list] = {store.id: [] for store in stores}
    for row in credential_rows:
        if row.token_format == LEGACY_TOKEN_FORMAT:
            by_store.setdefault(row.store_id, []).append(row)
    for store in stores:
        rows = by_store.get(store.id, [])
        if len(rows) != 1:
            raise TransitionReadinessError()
        row = rows[0]
        expected_status = "active" if bool(store.is_active) else "disabled"
        if row.device_status != expected_status or not _valid_hash_structure(row, hash_keys):
            raise TransitionReadinessError()
        key = hash_keys.get(row.hash_key_version)
        if key is None or not verify_legacy_receiver_token(
            store.receiver_token, row.token_hash, key
        ):
            raise TransitionReadinessError()
        if bool(store.is_active) and not _credential_is_usable(row, now):
            raise TransitionReadinessError()


def _validate_hash_readiness(
    stores,
    credential_rows,
    active_device_rows,
    hash_keys: Mapping[int, bytes],
    now: datetime,
) -> _ReadinessCounts:
    active_store_ids = {store.id for store in stores if bool(store.is_active)}
    active_devices: dict[int, list] = {}
    active_device_stores = {
        row.device_id: row.store_id for row in active_device_rows
    }
    usable_count = 0
    for row in credential_rows:
        if not bool(row.store_active) or row.device_status != "active":
            continue
        active_devices.setdefault(row.device_id, []).append(row)
    if active_store_ids - set(active_device_stores.values()):
        raise TransitionReadinessError()
    if set(active_device_stores) - set(active_devices):
        raise TransitionReadinessError()
    for rows in active_devices.values():
        usable_rows = []
        for row in rows:
            if not _valid_hash_structure(row, hash_keys):
                raise TransitionReadinessError()
            if _credential_is_usable(row, now):
                usable_rows.append(row)
        if not usable_rows:
            raise TransitionReadinessError()
        usable_count += len(usable_rows)
    return _ReadinessCounts(
        store_count=len(stores),
        active_store_count=len(active_store_ids),
        active_device_count=len(active_device_stores),
        usable_credential_count=usable_count,
    )


def _basic_counts(stores, credential_rows, active_device_rows, now: datetime) -> _ReadinessCounts:
    active_store_ids = {store.id for store in stores if bool(store.is_active)}
    active_devices = {row.device_id for row in active_device_rows}
    usable_credentials = {
        row.credential_id
        for row in credential_rows
        if bool(row.store_active)
        and row.device_status == "active"
        and _credential_is_usable(row, now)
    }
    return _ReadinessCounts(
        len(stores),
        len(active_store_ids),
        len(active_devices),
        len(usable_credentials),
    )


def _validate_connection_summary(
    policy: _TransitionPolicy,
    summary: ActiveReceiverConnectionSummary | None,
    now: datetime,
) -> None:
    if policy.runtime_action is RuntimeAction.NONE:
        return
    if not isinstance(summary, ActiveReceiverConnectionSummary):
        raise StaleConnectionSummaryError()
    age = now - summary.captured_at
    if age < timedelta(0) or age > MAX_CONNECTION_SUMMARY_AGE:
        raise StaleConnectionSummaryError()
    if (
        policy.runtime_action is RuntimeAction.LEGACY_CONNECTIONS_MUST_BE_ZERO
        and summary.legacy_authenticated_count != 0
    ):
        raise ActiveConnectionBlockerError()
    if (
        policy.runtime_action is RuntimeAction.HASHED_CONNECTIONS_MUST_BE_ZERO
        and summary.hashed_authenticated_count != 0
    ):
        raise ActiveConnectionBlockerError()


def _snapshot(connection: Connection, table: str) -> tuple[tuple, ...]:
    return tuple(connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1'))


def _audit_metadata(actor_user_id: int, target: str, reason: str) -> str:
    sanitized = sanitize_audit_payload(
        {
            "actor_user_id": actor_user_id,
            "migration_phase": target,
            "outcome": "success",
            "reason": reason,
        }
    )
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _notify(step_hook: Callable[[str], None] | None, step: str) -> None:
    if step_hook is not None:
        step_hook(step)



def _validate_hashed_fleet(
    connection: Connection,
    stores,
    credential_rows,
    active_device_rows,
    hash_keys,
    now: datetime,
) -> _ReadinessCounts:
    """Validate a directly-enrolled fleet, without assuming a backfill happened.

    The backfilled checks ask "is there exactly one Device per Store, mapped to
    that Store's raw token". A directly-enrolled fleet answers no to that and is
    still perfectly valid, so this asks the questions that actually matter before
    legacy verification is switched off:

    * every credential is in the hashed format this state will verify;
    * every hash_key_version is present in the ACTIVE key ring, so no credential
      becomes unverifiable the moment the state changes;
    * every Device belongs to a Store that exists and is active;
    * no legacy Receiver has ever connected, because turning legacy verification
      off would disconnect one that had.

    It deliberately does NOT require a Device per Store. Stores without a Receiver
    are simply Stores nobody has set up yet.
    """
    if not credential_rows:
        raise TransitionReadinessError()

    store_by_id = {row.id: row for row in stores}

    hashed = 0
    for row in credential_rows:
        if row.token_format != NEW_TOKEN_FORMAT:
            # A legacy row here would be silently orphaned by hash_only.
            raise TransitionReadinessError()
        if row.hash_key_version not in hash_keys:
            # Switching state would make this credential permanently unverifiable.
            raise TransitionReadinessError()
        hashed += 1

    for row in active_device_rows:
        store = store_by_id.get(row.store_id)
        if store is None or not bool(store.is_active):
            raise TransitionReadinessError()

    # Any Receiver traffic at all means a legacy Receiver may be in service, and
    # this transition disables the transport it uses.
    legacy_usage = connection.exec_driver_sql(
        "SELECT COUNT(*) FROM receiver_events"
    ).scalar_one()
    if legacy_usage:
        raise TransitionReadinessError()

    return _ReadinessCounts(
        store_count=len(stores),
        active_store_count=sum(1 for row in stores if bool(row.is_active)),
        active_device_count=len(active_device_rows),
        usable_credential_count=hashed,
    )

def transition_receiver_migration_state(
    engine: Engine,
    *,
    expected_current_state: str,
    target_state: str,
    actor_user_id: int,
    hash_keys: Mapping[int, bytes],
    active_connections: ActiveReceiverConnectionSummary | None,
    now: datetime | None = None,
    step_hook: Callable[[str], None] | None = None,
) -> MigrationTransitionResult:
    """Apply one approved adjacent migration-state transition atomically."""
    actor_id, validated_keys, transitioned_at = _validate_inputs(
        engine, actor_user_id, hash_keys, now
    )
    if (
        not isinstance(expected_current_state, str)
        or not isinstance(target_state, str)
        or expected_current_state not in _KNOWN_STATES
        or target_state not in _KNOWN_STATES
    ):
        raise InvalidStateTransitionError()

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise TransitionMigrationNotReadyError()
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _validate_schema(connection)
            current_state, current_flag = _current_state(connection)
            if current_state != expected_current_state:
                raise TransitionStateMismatchError()
            if current_state == target_state:
                raise TransitionAlreadyAppliedError()
            policy = _ALLOWED_TRANSITIONS.get((current_state, target_state))
            if policy is None:
                raise InvalidStateTransitionError()
            if current_flag != _EXPECTED_FLAGS[current_state]:
                raise TransitionMigrationNotReadyError()

            _validate_actor(connection, actor_id)
            stores = _load_stores(connection)
            _validate_raw_readiness(stores)
            credential_rows = _credential_rows(connection)
            active_device_rows = _active_device_rows(connection)
            if policy.fleet == "hashed_fleet":
                # A directly-enrolled fleet is validated on its own terms. The
                # backfilled mapping check assumes one Device per Store and would
                # reject this fleet for a shape it was never meant to have.
                counts = _validate_hashed_fleet(
                    connection,
                    stores,
                    credential_rows,
                    active_device_rows,
                    validated_keys,
                    transitioned_at,
                )
            else:
                _validate_backfilled_mapping(
                    stores, credential_rows, validated_keys, transitioned_at
                )
                if policy.require_hash_readiness:
                    counts = _validate_hash_readiness(
                        stores,
                        credential_rows,
                        active_device_rows,
                        validated_keys,
                        transitioned_at,
                    )
                else:
                    counts = _basic_counts(
                        stores, credential_rows, active_device_rows, transitioned_at
                    )
            _validate_connection_summary(policy, active_connections, transitioned_at)

            protected_before = {
                table: _snapshot(connection, table)
                for table in (
                    "stores",
                    "receiver_devices",
                    "receiver_credentials",
                    "schema_migrations",
                )
            }
            events_before = _snapshot(connection, "receiver_credential_events")

            _notify(step_hook, "before_state_update")
            update = connection.execute(
                text(
                    """
                    UPDATE receiver_credential_migration_state
                    SET state = :target, legacy_verification_enabled = :flag,
                        updated_at = :now
                    WHERE id = 1 AND state = :current
                      AND legacy_verification_enabled = :current_flag
                    """
                ),
                {
                    "target": target_state,
                    "flag": policy.target_flag,
                    "now": transitioned_at.isoformat(),
                    "current": current_state,
                    "current_flag": current_flag,
                },
            )
            if update.rowcount != 1:
                raise TransitionStateMismatchError()
            _notify(step_hook, "after_state_update")
            _notify(step_hook, "before_audit_insert")
            connection.execute(
                text(
                    """
                    INSERT INTO receiver_credential_events (
                        public_id, event_type, outcome, store_id, device_id,
                        credential_id, actor_user_id, event_at, reason_code,
                        correlation_id, metadata_json
                    ) VALUES (
                        :public_id, 'migration_state_changed', 'success', NULL, NULL,
                        NULL, :actor_id, :now, :reason_code, NULL, :metadata_json
                    )
                    """
                ),
                {
                    "public_id": str(uuid.uuid4()),
                    "actor_id": actor_id,
                    "now": transitioned_at.isoformat(),
                    "reason_code": policy.reason_code,
                    "metadata_json": _audit_metadata(
                        actor_id, target_state, policy.reason_code
                    ),
                },
            )
            _notify(step_hook, "after_audit_insert")

            final_state, final_flag = _current_state(connection)
            if final_state != target_state or final_flag != policy.target_flag:
                raise TransitionPersistenceError()
            for table, before in protected_before.items():
                if _snapshot(connection, table) != before:
                    raise TransitionPersistenceError()
            events_after = _snapshot(connection, "receiver_credential_events")
            if events_after[:-1] != events_before or len(events_after) != len(events_before) + 1:
                raise TransitionPersistenceError()
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
                raise TransitionPersistenceError()
            _notify(step_hook, "after_validation_before_commit")
            connection.commit()
        except ReceiverMigrationTransitionError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise TransitionPersistenceError() from None

    return MigrationTransitionResult(
        previous_state=current_state,
        new_state=target_state,
        legacy_verification_enabled=policy.target_flag,
        transitioned_at=transitioned_at,
        store_count=counts.store_count,
        active_store_count=counts.active_store_count,
        active_device_count=counts.active_device_count,
        usable_credential_count=counts.usable_credential_count,
        runtime_action=policy.runtime_action,
    )
