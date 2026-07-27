from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import secrets
import os
import sys
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from migrations import PROTECTED_DATABASE_PATH, run_receiver_credential_phase_one
from receiver_credential_backfill import rehearse_legacy_receiver_backfill
from receiver_credentials import (
    generate_receiver_credential,
    hash_legacy_receiver_token,
    hash_receiver_token,
)
from receiver_device_service import enroll_receiver_device
from receiver_auth_service import (
    ReceiverAuthenticationConfigurationError,
    ReceiverAuthenticationError,
    VerificationSource,
    authenticate_receiver_credential,
)


UTC_NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
GENERIC_FAILURE = "Receiver authentication failed"
CONFIGURATION_FAILURE = "Receiver authentication configuration is not ready"


@dataclass(slots=True)
class AuthFixture:
    engine: Engine
    legacy_token: str
    inactive_token: str
    backfill_key: bytes
    enrollment_key: bytes
    enrollment_token: str | None = None

    @property
    def keys(self) -> dict[int, bytes]:
        return {1: self.backfill_key, 2: self.enrollment_key}


def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _create_legacy_schema(engine: Engine) -> tuple[str, str]:
    legacy_token = uuid.uuid4().hex
    inactive_token = uuid.uuid4().hex
    timestamp = UTC_NOW.isoformat()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE hq_users (
                id INTEGER PRIMARY KEY,
                username VARCHAR(80) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(120) NOT NULL,
                role VARCHAR(30) NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at VARCHAR(40) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE stores (
                id INTEGER PRIMARY KEY,
                store_code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(120) NOT NULL,
                city VARCHAR(80) NOT NULL,
                region VARCHAR(80) NOT NULL,
                is_online_store BOOLEAN NOT NULL DEFAULT 0,
                receiver_token VARCHAR(64) NOT NULL UNIQUE,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'offline',
                last_seen VARCHAR(40),
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL
            )
            """
        )
        connection.execute(
            text(
                "INSERT INTO hq_users "
                "(id, username, password_hash, full_name, role, is_active, created_at) "
                "VALUES (1, 'test-actor', 'not-a-real-hash', 'Test Actor', 'admin', 1, :now)"
            ),
            {"now": timestamp},
        )
        connection.execute(
            text(
                """
                INSERT INTO stores (
                    id, store_code, name, city, region, is_online_store,
                    receiver_token, is_active, status, last_seen, created_at, updated_at
                ) VALUES (
                    :id, :code, :name, 'Test City', 'Test Region', :online,
                    :token, :active, :status, :last_seen, :now, :now
                )
                """
            ),
            [
                {
                    "id": 11,
                    "code": "ACTIVE-11",
                    "name": "Active Test Store",
                    "online": 1,
                    "token": legacy_token,
                    "active": 1,
                    "status": "playing",
                    "last_seen": timestamp,
                    "now": timestamp,
                },
                {
                    "id": 12,
                    "code": "INACTIVE-12",
                    "name": "Inactive Test Store",
                    "online": 0,
                    "token": inactive_token,
                    "active": 0,
                    "status": "offline",
                    "last_seen": None,
                    "now": timestamp,
                },
            ],
        )
    return legacy_token, inactive_token


def _new_fixture(tmp_path: Path, name: str) -> AuthFixture:
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    event.listen(engine, "connect", _enable_foreign_keys)
    legacy_token, inactive_token = _create_legacy_schema(engine)
    run_receiver_credential_phase_one(engine)
    return AuthFixture(
        engine=engine,
        legacy_token=legacy_token,
        inactive_token=inactive_token,
        backfill_key=secrets.token_bytes(48),
        enrollment_key=secrets.token_bytes(48),
    )


def _set_state(engine: Engine, state: str, legacy_enabled: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state "
                "SET state = :state, legacy_verification_enabled = :enabled, updated_at = :now "
                "WHERE id = 1"
            ),
            {"state": state, "enabled": legacy_enabled, "now": UTC_NOW.isoformat()},
        )


def _backfill(auth_fixture: AuthFixture) -> None:
    rehearse_legacy_receiver_backfill(
        auth_fixture.engine,
        hash_key=auth_fixture.backfill_key,
        hash_key_version=1,
        now=UTC_NOW,
    )


def _add_enrollment(auth_fixture: AuthFixture) -> None:
    _set_state(auth_fixture.engine, "legacy_only", 1)
    result = enroll_receiver_device(
        auth_fixture.engine,
        store_id=11,
        display_name="Isolated test receiver",
        actor_user_id=1,
        hash_key=auth_fixture.enrollment_key,
        hash_key_version=2,
        now=UTC_NOW,
    )
    auth_fixture.enrollment_token = result.take_raw_credential()


def _snapshot(engine: Engine) -> dict[str, tuple[tuple, ...]]:
    tables = (
        "stores",
        "receiver_devices",
        "receiver_credentials",
        "receiver_credential_events",
        "receiver_credential_migration_state",
        "schema_migrations",
    )
    with engine.connect() as connection:
        return {
            table: tuple(connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1'))
            for table in tables
        }


def _authenticate(auth_fixture: AuthFixture, token: str, *, keys=None):
    return authenticate_receiver_credential(
        auth_fixture.engine,
        presented_token=token,
        hash_keys=auth_fixture.keys if keys is None else keys,
        now=UTC_NOW,
    )


def _assert_generic_failure(auth_fixture: AuthFixture, token: str, *, keys=None) -> None:
    before = _snapshot(auth_fixture.engine)
    with pytest.raises(ReceiverAuthenticationError, match=f"^{GENERIC_FAILURE}$"):
        _authenticate(auth_fixture, token, keys=keys)
    assert _snapshot(auth_fixture.engine) == before


def test_legacy_only_uses_active_store_token_and_not_device_credentials(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "legacy-only.db")
    _add_enrollment(auth_fixture)

    result = _authenticate(auth_fixture, auth_fixture.legacy_token)
    assert result.store_id == 11
    assert result.store_code == "ACTIVE-11"
    assert result.device_id is None
    assert result.credential_id is None
    assert result.verification_source is VerificationSource.LEGACY_STORE_TOKEN
    assert result.migration_state == "legacy_only"

    _assert_generic_failure(auth_fixture, auth_fixture.inactive_token)
    _assert_generic_failure(auth_fixture, "not-a-token")
    assert auth_fixture.enrollment_token is not None
    _assert_generic_failure(auth_fixture, auth_fixture.enrollment_token)


def test_backfilled_requires_complete_mapping_but_still_uses_only_legacy_path(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "backfilled.db")
    _backfill(auth_fixture)
    _add_enrollment(auth_fixture)
    _set_state(auth_fixture.engine, "backfilled", 1)

    result = _authenticate(auth_fixture, auth_fixture.legacy_token)
    assert result.verification_source is VerificationSource.LEGACY_STORE_TOKEN
    assert result.device_id is not None
    assert result.credential_id is not None
    assert auth_fixture.enrollment_token is not None
    _assert_generic_failure(auth_fixture, auth_fixture.enrollment_token)

    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_devices SET status = 'disabled', disabled_at = :now "
                "WHERE id = :device_id"
            ),
            {"device_id": result.device_id, "now": UTC_NOW.isoformat()},
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


def test_backfilled_rejects_a_legacy_mapping_with_the_wrong_hash(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "backfilled-wrong-hash.db")
    _backfill(auth_fixture)
    unrelated_token = uuid.uuid4().hex
    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credentials SET token_hash = :token_hash "
                "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
            ),
            {
                "token_hash": hash_legacy_receiver_token(
                    unrelated_token,
                    auth_fixture.backfill_key,
                    key_version=1,
                )
            },
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


def test_dual_verify_accepts_both_formats_and_canonicalizes_legacy_identity(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "dual.db")
    _backfill(auth_fixture)
    _add_enrollment(auth_fixture)
    _set_state(auth_fixture.engine, "dual_verify", 1)

    legacy_result = _authenticate(auth_fixture, auth_fixture.legacy_token)
    assert legacy_result.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL
    assert legacy_result.store_id == 11
    assert legacy_result.device_id is not None
    assert legacy_result.credential_id is not None

    assert auth_fixture.enrollment_token is not None
    enrollment_result = _authenticate(auth_fixture, auth_fixture.enrollment_token)
    assert enrollment_result.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL
    assert enrollment_result.store_id == legacy_result.store_id
    assert enrollment_result.device_id != legacy_result.device_id

    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE receiver_credentials SET credential_version = 2 WHERE id = :id"),
            {"id": enrollment_result.credential_id},
        )
    _assert_generic_failure(auth_fixture, auth_fixture.enrollment_token)

    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE receiver_devices SET store_id = 12 WHERE id = :id"),
            {"id": legacy_result.device_id},
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


@pytest.mark.parametrize("state", ["hash_only", "raw_neutralized"])
def test_hash_states_ignore_raw_store_values_and_accept_hash_backed_tokens(
    tmp_path: Path, state: str
):
    auth_fixture = _new_fixture(tmp_path, f"{state}.db")
    original_backfilled_token = auth_fixture.legacy_token
    _backfill(auth_fixture)
    _add_enrollment(auth_fixture)
    replacement_raw_only_token = uuid.uuid4().hex
    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text("UPDATE stores SET receiver_token = :token WHERE id = 11"),
            {"token": replacement_raw_only_token},
        )
    _set_state(auth_fixture.engine, state, 0)

    legacy_hash_result = _authenticate(auth_fixture, original_backfilled_token)
    assert legacy_hash_result.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL
    assert auth_fixture.enrollment_token is not None
    new_result = _authenticate(auth_fixture, auth_fixture.enrollment_token)
    assert new_result.verification_source is VerificationSource.HASHED_DEVICE_CREDENTIAL
    _assert_generic_failure(auth_fixture, replacement_raw_only_token)
    _assert_generic_failure(auth_fixture, auth_fixture.inactive_token)


@pytest.mark.parametrize(
    ("state", "enabled"),
    [
        ("legacy_only", 0),
        ("backfilled", 0),
        ("dual_verify", 0),
        ("hash_only", 1),
        ("raw_neutralized", 1),
    ],
)
def test_inconsistent_state_flag_fails_as_configuration(
    tmp_path: Path, state: str, enabled: int
):
    auth_fixture = _new_fixture(tmp_path, f"flag-{state}.db")
    _set_state(auth_fixture.engine, state, enabled)
    with pytest.raises(
        ReceiverAuthenticationConfigurationError,
        match=f"^{CONFIGURATION_FAILURE}$",
    ):
        _authenticate(auth_fixture, auth_fixture.legacy_token)


def test_missing_schema_unknown_state_and_broken_foreign_keys_fail_configuration(tmp_path: Path):
    missing = create_engine(f"sqlite:///{tmp_path / 'missing.db'}")
    with pytest.raises(ReceiverAuthenticationConfigurationError, match=CONFIGURATION_FAILURE):
        authenticate_receiver_credential(
            missing,
            presented_token=uuid.uuid4().hex,
            hash_keys={1: secrets.token_bytes(48)},
            now=UTC_NOW,
        )

    auth_fixture = _new_fixture(tmp_path, "configuration.db")
    with auth_fixture.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text("UPDATE receiver_credential_migration_state SET state = 'unexpected' WHERE id = 1")
        )
    with pytest.raises(ReceiverAuthenticationConfigurationError, match=CONFIGURATION_FAILURE):
        _authenticate(auth_fixture, auth_fixture.legacy_token)

    broken = _new_fixture(tmp_path, "broken-fk.db")
    _backfill(broken)
    with broken.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.execute(text("UPDATE receiver_devices SET store_id = 999 WHERE store_id = 11"))
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    with pytest.raises(ReceiverAuthenticationConfigurationError, match=CONFIGURATION_FAILURE):
        _authenticate(broken, broken.legacy_token)


@pytest.mark.parametrize("device_status", ["disabled", "retired"])
def test_hash_authentication_requires_active_store_and_device(tmp_path: Path, device_status: str):
    auth_fixture = _new_fixture(tmp_path, f"device-{device_status}.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_devices SET status = :status, disabled_at = :now "
                "WHERE store_id = 11"
            ),
            {"status": device_status, "now": UTC_NOW.isoformat()},
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


@pytest.mark.parametrize(
    "mutation",
    ["inactive", "revoked", "expired", "malformed_hash", "wrong_format", "unknown_key"],
)
def test_hash_authentication_enforces_credential_lifecycle(tmp_path: Path, mutation: str):
    auth_fixture = _new_fixture(tmp_path, f"credential-{mutation}.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    statements = {
        "inactive": ("UPDATE receiver_credentials SET status = 'expired' WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {}),
        "revoked": ("UPDATE receiver_credentials SET status = 'revoked', revoked_at = :now WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {"now": UTC_NOW.isoformat()}),
        "expired": ("UPDATE receiver_credentials SET expiry_policy = 'expires_at', expires_at = :now WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {"now": UTC_NOW.isoformat()}),
        "malformed_hash": ("UPDATE receiver_credentials SET token_hash = :value WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {"value": "x" * 64}),
        "wrong_format": ("UPDATE receiver_credentials SET token_format = 'echocast_rcv' WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {}),
        "unknown_key": ("UPDATE receiver_credentials SET hash_key_version = 99 WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)", {}),
    }
    statement, values = statements[mutation]
    with auth_fixture.engine.begin() as connection:
        connection.execute(text(statement), values)
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


def test_replacement_grace_and_expiry_boundaries_follow_lifecycle_helper(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "boundaries.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credentials SET status = 'superseded', "
                "replaced_at = :replaced, accept_until = :until "
                "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
            ),
            {
                "replaced": UTC_NOW.isoformat(),
                "until": (UTC_NOW + timedelta(seconds=1)).isoformat(),
            },
        )
    assert _authenticate(auth_fixture, auth_fixture.legacy_token).store_id == 11
    with auth_fixture.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credentials SET accept_until = :now "
                "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
            ),
            {"now": UTC_NOW.isoformat()},
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


@pytest.mark.parametrize(
    "bad_token",
    ["x" * 4096, "Ã©" * 32, "a" * 31 + "\n"],
)
def test_malformed_oversized_and_non_ascii_tokens_fail_safely(tmp_path: Path, bad_token: str):
    auth_fixture = _new_fixture(tmp_path, "unsafe-input.db")
    _assert_generic_failure(auth_fixture, bad_token)


def test_failure_messages_and_captured_output_never_echo_secret_material(
    tmp_path: Path, capsys, caplog
):
    auth_fixture = _new_fixture(tmp_path, "redaction.db")
    caplog.set_level(logging.DEBUG)
    rejected_token = auth_fixture.legacy_token[:-1] + (
        "0" if auth_fixture.legacy_token[-1] != "0" else "1"
    )
    with pytest.raises(ReceiverAuthenticationError) as captured:
        _authenticate(auth_fixture, rejected_token)
    output = capsys.readouterr()
    rendered = str(captured.value) + repr(captured.value) + output.out + output.err + caplog.text
    assert str(captured.value) == GENERIC_FAILURE
    assert auth_fixture.legacy_token not in rendered
    assert auth_fixture.backfill_key.hex() not in rendered
    assert "authorization" not in rendered.lower()


def test_focused_service_has_no_runtime_import_or_health_side_effect_surface(tmp_path):
    """Asked of a fresh interpreter, not of this one.

    The property is "importing receiver_auth_service does not drag in the
    runtime" - a fact about this module's imports. Reading the ambient
    sys.modules turned it into a fact about whatever else happened to run first
    in the same pytest worker, so any new test file that imports ws_manager
    failed it from a distance. Same repair as the one in
    test_receiver_migration_transition_service.py, for the same reason.
    """
    import subprocess

    probe = (
        "import sys; sys.path.insert(0, %r); "
        "import receiver_auth_service; "
        "print('server' in sys.modules, 'ws_manager' in sys.modules)"
        % str(Path(__file__).resolve().parents[1])
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ECHOCAST_DB_PATH": str(tmp_path / "probe.db")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False False", (
        f"the focused service pulled in the runtime: {completed.stdout.strip()}"
    )


def test_result_is_immutable_redacted_and_authentication_is_read_only(
    tmp_path: Path, capsys, caplog
):
    auth_fixture = _new_fixture(tmp_path, "read-only.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "dual_verify", 1)
    before = _snapshot(auth_fixture.engine)
    caplog.set_level(logging.DEBUG)
    result = _authenticate(auth_fixture, auth_fixture.legacy_token)
    after = _snapshot(auth_fixture.engine)

    assert after == before
    with pytest.raises(FrozenInstanceError):
        result.store_id = 99
    rendered = repr(result) + str(result) + capsys.readouterr().out + capsys.readouterr().err + caplog.text
    assert auth_fixture.legacy_token not in rendered
    assert result.credential_public_id in rendered
    assert not hasattr(result, "readiness")
    assert not hasattr(result, "playback")
    assert not hasattr(result, "speaker_verified")


def test_key_ring_is_bounded_validated_and_selects_stored_version(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "keys.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    assert _authenticate(auth_fixture, auth_fixture.legacy_token).store_id == 11
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token, keys={2: auth_fixture.enrollment_key})
    for invalid_keys in ({}, {0: secrets.token_bytes(48)}, {1: b"short"}, {i: secrets.token_bytes(48) for i in range(1, 18)}):
        with pytest.raises(ReceiverAuthenticationConfigurationError, match=CONFIGURATION_FAILURE):
            _authenticate(auth_fixture, auth_fixture.legacy_token, keys=invalid_keys)


def test_concurrent_authentication_is_deterministic_and_read_only(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "concurrent.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    before = _snapshot(auth_fixture.engine)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _index: _authenticate(auth_fixture, auth_fixture.legacy_token),
                range(12),
            )
        )
    assert {result.store_id for result in results} == {11}
    assert _snapshot(auth_fixture.engine) == before


def test_protected_real_database_is_refused_without_connecting(monkeypatch):
    protected_engine = create_engine(f"sqlite:///{PROTECTED_DATABASE_PATH}")
    connected = False

    def record_connection(*_args, **_kwargs):
        nonlocal connected
        connected = True

    event.listen(protected_engine, "connect", record_connection)
    with pytest.raises(ReceiverAuthenticationConfigurationError, match=CONFIGURATION_FAILURE):
        authenticate_receiver_credential(
            protected_engine,
            presented_token=uuid.uuid4().hex,
            hash_keys={1: secrets.token_bytes(48)},
            now=UTC_NOW,
        )
    assert connected is False


def test_duplicate_hash_mapping_fails_closed(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "ambiguous.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    with auth_fixture.engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT public_id, device_id, token_hash FROM receiver_credentials "
                "WHERE device_id IN (SELECT id FROM receiver_devices WHERE store_id = 11)"
            )
        ).one()
        connection.execute(
            text(
                "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
                "token_format, token_hash, hash_key_version, status, expiry_policy, issued_at, created_at) "
                "VALUES (:public_id, :device_id, 2, 'legacy_uuid_hex', :token_hash, 1, "
                "'active', 'non_expiring', :now, :now)"
            ),
            {
                "public_id": str(uuid.uuid4()),
                "device_id": row.device_id,
                "token_hash": hash_receiver_token(
                    generate_receiver_credential().raw_token,
                    auth_fixture.backfill_key,
                    key_version=1,
                ),
                "now": UTC_NOW.isoformat(),
            },
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)


def test_ambiguous_multiple_legacy_devices_fail_closed(tmp_path: Path):
    auth_fixture = _new_fixture(tmp_path, "ambiguous-devices.db")
    _backfill(auth_fixture)
    _set_state(auth_fixture.engine, "hash_only", 0)
    with auth_fixture.engine.begin() as connection:
        device_id = connection.execute(
            text(
                "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
                "enrolled_at, created_at, updated_at) VALUES "
                "(:public_id, 11, 'Ambiguous test mapping', 'active', :now, :now, :now) "
                "RETURNING id"
            ),
            {"public_id": str(uuid.uuid4()), "now": UTC_NOW.isoformat()},
        ).scalar_one()
        connection.execute(
            text(
                "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
                "token_format, token_hash, hash_key_version, status, expiry_policy, issued_at, created_at) "
                "VALUES (:public_id, :device_id, 1, 'legacy_uuid_hex', :token_hash, 1, "
                "'active', 'non_expiring', :now, :now)"
            ),
            {
                "public_id": str(uuid.uuid4()),
                "device_id": device_id,
                "token_hash": hash_legacy_receiver_token(
                    uuid.uuid4().hex,
                    auth_fixture.backfill_key,
                    key_version=1,
                ),
                "now": UTC_NOW.isoformat(),
            },
        )
    _assert_generic_failure(auth_fixture, auth_fixture.legacy_token)
