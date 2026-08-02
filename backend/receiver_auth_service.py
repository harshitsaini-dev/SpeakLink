"""Isolated, read-only Receiver Credential authentication service.

The service is deliberately not integrated with FastAPI, WebSockets, the
application engine, or runtime receiver snapshots. Callers inject a SQLite
Engine and an external HMAC key ring.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
import secrets

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from migrations import PHASE_ONE_NAME, PHASE_ONE_VERSION, PROTECTED_DATABASE_PATH
from receiver_credentials import (
    MIN_HASH_KEY_BYTES,
    CredentialState,
    InvalidCredentialError,
    credential_is_usable,
    hash_legacy_receiver_token,
    hash_receiver_token,
    parse_receiver_token,
    verify_legacy_receiver_token,
    verify_receiver_token,
)


AUTHENTICATION_FAILURE_MESSAGE = "Receiver authentication failed"
CONFIGURATION_FAILURE_MESSAGE = "Receiver authentication configuration is not ready"
MAX_PRESENTED_TOKEN_LENGTH = 256
MAX_HASH_KEY_VERSIONS = 16
MAX_LEGACY_STORE_CANDIDATES = 1000

LEGACY_TOKEN_FORMAT = "legacy_uuid_hex"
NEW_TOKEN_FORMAT = "echocast_rcv"
_KNOWN_STATES = {
    "legacy_only",
    "backfilled",
    "dual_verify",
    "hash_only",
    "raw_neutralized",
}
_LEGACY_STATES = {"legacy_only", "backfilled", "dual_verify"}
_HASH_STATES = {"dual_verify", "hash_only", "raw_neutralized"}
_EXPECTED_LEGACY_FLAG = {
    "legacy_only": 1,
    "backfilled": 1,
    "dual_verify": 1,
    "hash_only": 0,
    "raw_neutralized": 0,
}
_REQUIRED_COLUMNS = {
    "stores": {"id", "store_code", "receiver_token", "is_active"},
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
    "receiver_credential_migration_state": {
        "id",
        "schema_version",
        "state",
        "legacy_verification_enabled",
    },
    "schema_migrations": {"version", "name"},
}
_REQUIRED_INDEXES = {
    "ix_receiver_devices_store_status",
    "ix_receiver_credentials_public_id",
    "ix_receiver_credentials_auth_lookup",
    "ix_receiver_credentials_device_status",
}


class ReceiverAuthenticationError(RuntimeError):
    """A generic credential rejection that reveals no lookup detail."""

    def __init__(self) -> None:
        super().__init__(AUTHENTICATION_FAILURE_MESSAGE)


class ReceiverAuthenticationConfigurationError(RuntimeError):
    """A fixed failure for missing or inconsistent verifier configuration."""

    def __init__(self) -> None:
        super().__init__(CONFIGURATION_FAILURE_MESSAGE)


class VerificationSource(str, Enum):
    LEGACY_STORE_TOKEN = "legacy_store_token"
    HASHED_DEVICE_CREDENTIAL = "hashed_device_credential"


@dataclass(frozen=True, slots=True, repr=False)
class ReceiverAuthenticationResult:
    store_id: int
    store_code: str
    device_id: int | None
    device_public_id: str | None
    credential_id: int | None
    credential_public_id: str | None
    credential_version: int | None
    migration_state: str
    verification_source: VerificationSource

    def __repr__(self) -> str:
        return (
            "ReceiverAuthenticationResult("
            f"store_id={self.store_id}, store_code={self.store_code!r}, "
            f"device_id={self.device_id!r}, device_public_id={self.device_public_id!r}, "
            f"credential_id={self.credential_id!r}, "
            f"credential_public_id={self.credential_public_id!r}, "
            f"credential_version={self.credential_version!r}, "
            f"migration_state={self.migration_state!r}, "
            f"verification_source={self.verification_source.value!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class _Identity:
    store_id: int
    store_code: str
    device_id: int | None = None
    device_public_id: str | None = None
    credential_id: int | None = None
    credential_public_id: str | None = None
    credential_version: int | None = None


def _configuration_failure() -> ReceiverAuthenticationConfigurationError:
    return ReceiverAuthenticationConfigurationError()


def _authentication_failure() -> ReceiverAuthenticationError:
    return ReceiverAuthenticationError()


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


#: The engines this service will authenticate against. SQLite is the local
#: HQ database; PostgreSQL is Supabase.
#:
#: Until RC15 this read ``!= "sqlite"`` and refused everything else. That was
#: correct when SQLite was the only database and became a total outage the
#: moment a second one existed: HQ would boot, report READY, serve every admin
#: screen, and authenticate no Receiver at all. The list is explicit rather
#: than open so an unexpected dialect is still refused rather than attempted.
SUPPORTED_DIALECTS = frozenset({"sqlite", "postgresql"})


def _validate_inputs(
    engine: Engine,
    presented_token: object,
    hash_keys: object,
    now: datetime | None,
) -> tuple[str, dict[int, bytes], datetime]:
    dialect = engine.dialect.name
    if dialect not in SUPPORTED_DIALECTS:
        raise _configuration_failure()
    # The protected-database guard is a FILE PATH check, so it only means
    # anything on SQLite. On PostgreSQL there is no path to protect and
    # ``engine.url.database`` is the database name, which would never match -
    # so the check is skipped rather than evaluated into a false negative.
    if dialect == "sqlite" and _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
        raise _configuration_failure()
    if not isinstance(hash_keys, Mapping) or not 1 <= len(hash_keys) <= MAX_HASH_KEY_VERSIONS:
        raise _configuration_failure()
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
            raise _configuration_failure()
        validated_keys[version] = key

    validated_now = now or datetime.now(timezone.utc)
    if (
        not isinstance(validated_now, datetime)
        or validated_now.tzinfo is None
        or validated_now.utcoffset() != timedelta(0)
    ):
        raise _configuration_failure()
    if (
        not isinstance(presented_token, str)
        or not 1 <= len(presented_token) <= MAX_PRESENTED_TOKEN_LENGTH
        or not presented_token.isascii()
        or not presented_token.isprintable()
    ):
        raise _authentication_failure()
    return presented_token, validated_keys, validated_now


def _inspector(connection: Connection):
    """SQLAlchemy's own introspection, which answers on either dialect.

    This replaces ``PRAGMA table_info`` and two ``sqlite_master`` queries. The
    questions being asked - which tables exist, which columns they have, which
    indexes are defined - are identical; only the SQLite-specific way of
    asking them was ever the problem.
    """
    from sqlalchemy import inspect

    return inspect(connection)


def _columns(connection: Connection, table: str) -> set[str]:
    return {column["name"] for column in _inspector(connection).get_columns(table)}


def _index_names(connection: Connection) -> set[str]:
    """Every index name across the tables this service depends on.

    The Inspector reports indexes per table rather than globally, which is
    the one real difference from the old ``sqlite_master`` sweep. Asking only
    about the tables in _REQUIRED_COLUMNS is not a narrowing of the check -
    every name in _REQUIRED_INDEXES belongs to one of them - and it avoids
    enumerating unrelated tables on a shared database.

    A UNIQUE constraint is reported separately from an index by some
    dialects, so those names are folded in too: on PostgreSQL a unique index
    created by ``Index(..., unique=True)`` appears as an index, but one
    created by a UniqueConstraint appears as a constraint, and the service
    cares that the lookup is indexed, not which DDL spelled it.
    """
    inspector = _inspector(connection)
    found: set[str] = set()
    for table in _REQUIRED_COLUMNS:
        try:
            found.update(index["name"] for index in inspector.get_indexes(table)
                         if index.get("name"))
            found.update(
                constraint["name"]
                for constraint in inspector.get_unique_constraints(table)
                if constraint.get("name")
            )
        except Exception:
            # A table that cannot be introspected is already fatal below; not
            # raising here keeps the failure attributable to the missing
            # table rather than to introspection.
            continue
    return found


def _validate_schema_and_state(connection: Connection) -> str:
    inspector = _inspector(connection)
    tables = set(inspector.get_table_names())
    if not set(_REQUIRED_COLUMNS) <= tables:
        raise _configuration_failure()
    for table, required in _REQUIRED_COLUMNS.items():
        if not required <= _columns(connection, table):
            raise _configuration_failure()
    if not _REQUIRED_INDEXES <= _index_names(connection):
        raise _configuration_failure()
    ledger = connection.execute(
        text("SELECT name FROM schema_migrations WHERE version = :version"),
        {"version": PHASE_ONE_VERSION},
    ).scalar_one_or_none()
    if ledger != PHASE_ONE_NAME:
        raise _configuration_failure()
    state_row = connection.execute(
        text(
            "SELECT schema_version, state, legacy_verification_enabled "
            "FROM receiver_credential_migration_state WHERE id = 1"
        )
    ).one_or_none()
    if (
        state_row is None
        or state_row.schema_version != PHASE_ONE_VERSION
        or state_row.state not in _KNOWN_STATES
        or state_row.legacy_verification_enabled != _EXPECTED_LEGACY_FLAG.get(state_row.state)
    ):
        raise _configuration_failure()
    # PRAGMA foreign_key_check sweeps the whole database for rows whose
    # foreign key points at nothing. It exists because SQLite will HAPPILY
    # store such a row when enforcement is off, so an installation can be
    # sitting on broken references and look fine.
    #
    # PostgreSQL cannot reach that state: every FK is checked as the
    # statement runs and a violating row is rejected at write time. There is
    # no equivalent sweep because there is nothing to sweep for. Skipping it
    # is not a relaxed check on PostgreSQL - it is the same guarantee,
    # enforced earlier.
    if connection.dialect.name == "sqlite":
        if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
            raise _configuration_failure()
    return state_row.state


def _token_format(token: str) -> str:
    if len(token) == 32 and token.isascii() and token == token.lower():
        try:
            int(token, 16)
        except ValueError:
            pass
        else:
            return LEGACY_TOKEN_FORMAT
    try:
        parse_receiver_token(token)
    except InvalidCredentialError:
        raise _authentication_failure() from None
    return NEW_TOKEN_FORMAT


def _validate_backfilled_fleet(connection: Connection) -> None:
    rows = connection.execute(
        text(
            """
            SELECT
                s.id AS store_id, s.is_active AS store_active,
                d.id AS device_id, d.status AS device_status,
                c.id AS credential_id
            FROM stores s
            LEFT JOIN receiver_devices d ON d.store_id = s.id
            LEFT JOIN receiver_credentials c
              ON c.device_id = d.id AND c.token_format = :token_format
            ORDER BY s.id, d.id, c.id
            """
        ),
        {"token_format": LEGACY_TOKEN_FORMAT},
    ).all()
    grouped: dict[int, list] = {}
    for row in rows:
        if row.credential_id is not None:
            grouped.setdefault(row.store_id, []).append(row)
        else:
            grouped.setdefault(row.store_id, [])
    if not grouped:
        raise _authentication_failure()
    for store_rows in grouped.values():
        if len(store_rows) != 1:
            raise _authentication_failure()
        row = store_rows[0]
        expected_status = "active" if bool(row.store_active) else "disabled"
        if row.device_status != expected_status:
            raise _authentication_failure()


def _legacy_store_identity(connection: Connection, token: str) -> _Identity | None:
    rows = connection.execute(
        text(
            "SELECT id, store_code, receiver_token, is_active FROM stores "
            "ORDER BY id LIMIT :limit"
        ),
        {"limit": MAX_LEGACY_STORE_CANDIDATES + 1},
    ).all()
    if len(rows) > MAX_LEGACY_STORE_CANDIDATES:
        raise _configuration_failure()
    candidate = token.encode("ascii")
    matches = []
    for row in rows:
        try:
            expected = row.receiver_token.encode("ascii")
        except (AttributeError, UnicodeEncodeError):
            raise _configuration_failure() from None
        if secrets.compare_digest(expected, candidate):
            matches.append(row)
    if len(matches) != 1 or not bool(matches[0].is_active):
        return None
    row = matches[0]
    return _Identity(store_id=row.id, store_code=row.store_code)


def _legacy_mapping(connection: Connection, store_id: int, *, require_usable: bool, now: datetime) -> _Identity:
    rows = connection.execute(
        text(
            """
            SELECT
                s.id AS store_id, s.store_code, s.is_active AS store_active,
                d.id AS device_id, d.public_id AS device_public_id, d.status AS device_status,
                c.id AS credential_id, c.public_id AS credential_public_id,
                c.credential_version, c.token_format, c.status AS credential_status,
                c.expiry_policy, c.issued_at, c.expires_at, c.revoked_at,
                c.replaced_at, c.accept_until
            FROM stores s
            JOIN receiver_devices d ON d.store_id = s.id
            JOIN receiver_credentials c ON c.device_id = d.id
            WHERE s.id = :store_id AND c.token_format = :token_format
            ORDER BY d.id, c.id
            """
        ),
        {"store_id": store_id, "token_format": LEGACY_TOKEN_FORMAT},
    ).all()
    if len(rows) != 1:
        raise _authentication_failure()
    row = rows[0]
    if not bool(row.store_active) or row.device_status != "active":
        raise _authentication_failure()
    if require_usable and not _credential_usable(row, now):
        raise _authentication_failure()
    return _Identity(
        store_id=row.store_id,
        store_code=row.store_code,
        device_id=row.device_id,
        device_public_id=row.device_public_id,
        credential_id=row.credential_id,
        credential_public_id=row.credential_public_id,
        credential_version=row.credential_version,
    )


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _authentication_failure()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _authentication_failure() from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _authentication_failure()
    return parsed


def _credential_usable(row, now: datetime) -> bool:
    if row.credential_status not in {"active", "superseded", "revoked", "expired"}:
        return False
    expires_at = _parse_utc(row.expires_at)
    if (row.expiry_policy == "non_expiring" and expires_at is not None) or (
        row.expiry_policy == "expires_at" and expires_at is None
    ):
        return False
    if row.expiry_policy not in {"non_expiring", "expires_at"}:
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


def _hash_candidates(
    connection: Connection,
    token: str,
    token_format: str,
    hash_keys: Mapping[int, bytes],
) -> list:
    parsed_public_id = None
    if token_format == NEW_TOKEN_FORMAT:
        parsed_public_id = parse_receiver_token(token).public_id
    matches: dict[int, object] = {}
    for key_version, key in hash_keys.items():
        try:
            encoded = (
                hash_legacy_receiver_token(token, key, key_version=key_version)
                if token_format == LEGACY_TOKEN_FORMAT
                else hash_receiver_token(token, key, key_version=key_version)
            )
        except (InvalidCredentialError, ValueError):
            continue
        parameters = {
            "token_format": token_format,
            "key_version": key_version,
            "token_hash": encoded,
            "public_id": parsed_public_id,
        }
        rows = connection.execute(
            text(
                """
                SELECT
                    s.id AS store_id, s.store_code, s.is_active AS store_active,
                    d.id AS device_id, d.public_id AS device_public_id,
                    d.status AS device_status,
                    c.id AS credential_id, c.public_id AS credential_public_id,
                    c.credential_version, c.token_format,
                    c.token_hash, c.hash_key_version,
                    c.status AS credential_status, c.expiry_policy,
                    c.issued_at, c.expires_at, c.revoked_at,
                    c.replaced_at, c.accept_until
                FROM receiver_credentials c
                JOIN receiver_devices d ON d.id = c.device_id
                JOIN stores s ON s.id = d.store_id
                WHERE c.token_format = :token_format
                  AND c.hash_key_version = :key_version
                  AND c.token_hash = :token_hash
                  -- CAST, because PostgreSQL cannot infer a bare parameter's
                  -- type when its only use is "$n IS NULL" and refuses with
                  -- "could not determine data type of parameter". SQLite
                  -- infers per value and accepted it, so this was invisible
                  -- until the first real PostgreSQL handshake.
                  AND (CAST(:public_id AS VARCHAR) IS NULL
                       OR c.public_id = CAST(:public_id AS VARCHAR))
                LIMIT 2
                """
            ),
            parameters,
        ).all()
        for row in rows:
            matches[row.credential_id] = row
    return list(matches.values())


def _hashed_identity(
    connection: Connection,
    token: str,
    token_format: str,
    hash_keys: Mapping[int, bytes],
    now: datetime,
) -> _Identity | None:
    rows = _hash_candidates(connection, token, token_format, hash_keys)
    if not rows:
        return None
    if len(rows) != 1:
        raise _authentication_failure()
    row = rows[0]
    parsed = parse_receiver_token(token) if token_format == NEW_TOKEN_FORMAT else None
    key = hash_keys.get(row.hash_key_version)
    verified = (
        verify_legacy_receiver_token(token, row.token_hash, key)
        if token_format == LEGACY_TOKEN_FORMAT and key is not None
        else verify_receiver_token(token, row.token_hash, key)
        if token_format == NEW_TOKEN_FORMAT and key is not None
        else False
    )
    if (
        not verified
        or row.token_format != token_format
        or (parsed is not None and parsed.version != row.credential_version)
        or not bool(row.store_active)
        or row.device_status != "active"
        or not _credential_usable(row, now)
    ):
        raise _authentication_failure()
    identity = _Identity(
        store_id=row.store_id,
        store_code=row.store_code,
        device_id=row.device_id,
        device_public_id=row.device_public_id,
        credential_id=row.credential_id,
        credential_public_id=row.credential_public_id,
        credential_version=row.credential_version,
    )
    if token_format == LEGACY_TOKEN_FORMAT:
        mapped = _legacy_mapping(connection, row.store_id, require_usable=True, now=now)
        if mapped != identity:
            raise _authentication_failure()
    return identity


def _result(identity: _Identity, state: str, source: VerificationSource) -> ReceiverAuthenticationResult:
    return ReceiverAuthenticationResult(
        store_id=identity.store_id,
        store_code=identity.store_code,
        device_id=identity.device_id,
        device_public_id=identity.device_public_id,
        credential_id=identity.credential_id,
        credential_public_id=identity.credential_public_id,
        credential_version=identity.credential_version,
        migration_state=state,
        verification_source=source,
    )


def authenticate_receiver_credential(
    engine: Engine,
    *,
    presented_token: str,
    hash_keys: Mapping[int, bytes],
    now: datetime | None = None,
) -> ReceiverAuthenticationResult:
    """Authenticate one receiver identity without changing persistent state."""
    token, validated_keys, validated_now = _validate_inputs(
        engine, presented_token, hash_keys, now
    )
    try:
        with engine.connect() as connection:
            # SQLite has to be ASKED to enforce foreign keys, per connection,
            # and this service refuses to authenticate on a connection where
            # the request did not take effect. PostgreSQL enforces them
            # unconditionally and rejects the PRAGMA as a syntax error, so
            # asking would turn a guarantee into a crash.
            if connection.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
                    raise _configuration_failure()
            state = _validate_schema_and_state(connection)
            if state in {"backfilled", "dual_verify"}:
                _validate_backfilled_fleet(connection)
            token_format = _token_format(token)

            legacy_identity = None
            if state in _LEGACY_STATES and token_format == LEGACY_TOKEN_FORMAT:
                legacy_identity = _legacy_store_identity(connection, token)
                if legacy_identity is not None and state in {"backfilled", "dual_verify"}:
                    legacy_identity = _legacy_mapping(
                        connection,
                        legacy_identity.store_id,
                        require_usable=state == "dual_verify",
                        now=validated_now,
                    )
                    if state == "backfilled":
                        validated_mapping = _hashed_identity(
                            connection,
                            token,
                            token_format,
                            validated_keys,
                            validated_now,
                        )
                        if validated_mapping is None or validated_mapping != legacy_identity:
                            raise _authentication_failure()

            hashed_identity = None
            if state in _HASH_STATES:
                hashed_identity = _hashed_identity(
                    connection,
                    token,
                    token_format,
                    validated_keys,
                    validated_now,
                )

            if state == "dual_verify" and token_format == LEGACY_TOKEN_FORMAT:
                if legacy_identity is None or hashed_identity is None or legacy_identity != hashed_identity:
                    raise _authentication_failure()
                return _result(
                    hashed_identity,
                    state,
                    VerificationSource.HASHED_DEVICE_CREDENTIAL,
                )
            if legacy_identity is not None:
                return _result(legacy_identity, state, VerificationSource.LEGACY_STORE_TOKEN)
            if hashed_identity is not None:
                return _result(
                    hashed_identity,
                    state,
                    VerificationSource.HASHED_DEVICE_CREDENTIAL,
                )
            raise _authentication_failure()
    except (ReceiverAuthenticationError, ReceiverAuthenticationConfigurationError):
        raise
    except Exception:
        raise _configuration_failure() from None
