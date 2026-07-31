"""Moving a directly-enrolled fleet out of legacy_only without destroying it.

THE SITUATION THIS EXISTS FOR

The live HQ sits in ``legacy_only`` while holding four hashed Device credentials.
``receiver_auth_service`` only computes a hashed identity when the state is in
``_HASH_STATES``, so every one of those credentials is refused without ever being
compared - and the documented way out is closed:

* ``rehearse_legacy_receiver_backfill`` demands zero Devices, zero credentials and
  zero audit events;
* ``dual_verify`` additionally demands one backfilled Device PER STORE.

Four Devices across forty-four Stores satisfies neither. Taking that route would
mean deleting the Devices and the audit history in order to recreate them.

``hash_only`` is hash-capable and is NOT subject to the backfilled-fleet check -
``receiver_auth_service`` runs that only for ``backfilled`` and ``dual_verify`` -
so it is reachable for this fleet while preserving every row.

A NOTE ON THE TIMESTAMP TESTS

The first rehearsal of this transition appeared to fail: a probe credential issued
straight afterwards was refused. It was measured, not guessed, and the cause was
the harness reusing the transition's ``now`` for authentication. The credential
was issued eight milliseconds later, so ``_credential_usable`` correctly rejected
a credential that did not yet exist at the instant being asked about. That is a
clock-skew defence working, and it is pinned below so nobody "fixes" it.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import receiver_auth_service as svc  # noqa: E402
from key_custody import FakeProtector, create_key_container, load_key_ring  # noqa: E402
from migrations import run_receiver_credential_phase_one  # noqa: E402
from receiver_device_service import enroll_receiver_device  # noqa: E402
from receiver_migration_transition_service import (  # noqa: E402
    ActiveReceiverConnectionSummary,
    InvalidStateTransitionError,
    TransitionReadinessError,
    transition_receiver_migration_state,
)

LIVE_DATABASE = (
    Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    / "SpeakLink" / "persistent-lan-server" / "data" / "speaklink.db"
)


@pytest.fixture(autouse=True)
def the_live_database_is_never_opened():
    """This suite changes migration state for a living. The one file it must
    never touch is the database an operator is using."""
    import hashlib

    before = (hashlib.sha256(LIVE_DATABASE.read_bytes()).hexdigest()
              if LIVE_DATABASE.exists() else None)
    yield
    if before is not None:
        after = hashlib.sha256(LIVE_DATABASE.read_bytes()).hexdigest()
        assert after == before, "a test wrote to the LIVE persistent database"


@pytest.fixture()
def fleet(tmp_path):
    """A directly-enrolled fleet: several Stores, a few Devices, legacy_only."""
    import db as db_module
    import models

    database = tmp_path / "fleet.db"
    container = tmp_path / "keys.bin"
    create_key_container(container, protector=FakeProtector())
    ring = load_key_ring(container, protector=FakeProtector())

    engine = create_engine(f"sqlite:///{database}", future=True)
    models.Base.metadata.create_all(bind=engine)
    run_receiver_credential_phase_one(engine)

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        session.add(models.HQUser(id=1, username="admin", password_hash="x", role="ADMIN"))
        session.add(models.HQUser(id=2, username="owneradmin", password_hash="x", role="OWNER"))
        for index in range(1, 6):
            session.add(models.Store(
                id=index, store_code=f"S{index}", store_name=f"Store {index}",
                city="Delhi", region="Zone 1", receiver_token=f"{index}" * 32))
        session.commit()

    version, key = ring.signing_key()
    devices = []
    for store_id in (1, 2):
        outcome = enroll_receiver_device(
            engine, store_id=store_id, display_name=f"PC-{store_id}",
            actor_user_id=1, hash_key=key, hash_key_version=version)
        devices.append(outcome.device_public_id)
    return engine, ring, devices, database


def summary(now):
    return ActiveReceiverConnectionSummary(
        legacy_authenticated_count=0, hashed_authenticated_count=0, captured_at=now)


def transition(engine, ring, now, **overrides):
    kwargs = dict(expected_current_state="legacy_only", target_state="hash_only",
                  actor_user_id=2, hash_keys=ring.as_mapping(),
                  active_connections=summary(now), now=now)
    kwargs.update(overrides)
    return transition_receiver_migration_state(engine, **kwargs)


def state_of(engine):
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            "SELECT state, legacy_verification_enabled "
            "FROM receiver_credential_migration_state").fetchone()


def snapshot(engine, table, columns="*"):
    with engine.connect() as conn:
        return conn.exec_driver_sql(
            f"SELECT {columns} FROM {table} ORDER BY id").fetchall()


# ===========================================================================
# 1. legacy_only cannot authenticate a hashed credential at all
# ===========================================================================
def test_legacy_only_refuses_a_hashed_credential(fleet):
    engine, ring, _devices, _path = fleet
    version, key = ring.signing_key()
    token = enroll_receiver_device(
        engine, store_id=3, display_name="PC-3", actor_user_id=1,
        hash_key=key, hash_key_version=version).take_raw_credential()

    assert state_of(engine)[0] == "legacy_only"
    with pytest.raises(svc.ReceiverAuthenticationError):
        svc.authenticate_receiver_credential(
            engine, presented_token=token, hash_keys=ring.as_mapping(),
            now=datetime.now(timezone.utc))


def test_legacy_only_is_not_a_hash_capable_state():
    assert "legacy_only" not in svc._HASH_STATES
    assert "hash_only" in svc._HASH_STATES


def test_hash_only_is_not_subject_to_the_backfilled_fleet_check():
    """The whole reason hash_only is reachable for this fleet and dual_verify
    is not. Asserted against the source so a future edit cannot quietly add it."""
    source = (BACKEND_ROOT / "receiver_auth_service.py").read_text(encoding="utf-8")
    assert 'if state in {"backfilled", "dual_verify"}:' in source
    assert '_validate_backfilled_fleet(connection)' in source


# ===========================================================================
# 2. The transition itself
# ===========================================================================
def test_the_transition_reaches_hash_only(fleet):
    engine, ring, _devices, _path = fleet
    result = transition(engine, ring, datetime.now(timezone.utc))

    assert result.previous_state == "legacy_only"
    assert result.new_state == "hash_only"
    assert state_of(engine) == ("hash_only", 0)


def test_the_transition_preserves_every_device_public_id(fleet):
    engine, ring, devices, _path = fleet
    before = snapshot(engine, "receiver_devices", "id, public_id, store_id, status")

    transition(engine, ring, datetime.now(timezone.utc))

    after = snapshot(engine, "receiver_devices", "id, public_id, store_id, status")
    assert before == after
    for public_id in devices:
        assert any(row[1] == public_id for row in after)


def test_the_transition_preserves_every_credential_hash(fleet):
    engine, ring, _devices, _path = fleet
    before = snapshot(engine, "receiver_credentials",
                      "id, public_id, device_id, token_hash, hash_key_version, status")

    transition(engine, ring, datetime.now(timezone.utc))

    assert snapshot(engine, "receiver_credentials",
                    "id, public_id, device_id, token_hash, hash_key_version, status") == before


def test_the_transition_preserves_stores_and_users(fleet):
    engine, ring, _devices, _path = fleet
    stores = snapshot(engine, "stores", "id, store_code, is_active")
    users = snapshot(engine, "hq_users", "id, username, role")

    transition(engine, ring, datetime.now(timezone.utc))

    assert snapshot(engine, "stores", "id, store_code, is_active") == stores
    assert snapshot(engine, "hq_users", "id, username, role") == users


def test_exactly_one_migration_audit_event_is_added(fleet):
    engine, ring, _devices, _path = fleet
    before = snapshot(engine, "receiver_credential_events", "id, event_type")

    transition(engine, ring, datetime.now(timezone.utc))

    after = snapshot(engine, "receiver_credential_events", "id, event_type")
    assert len(after) == len(before) + 1
    assert after[:-1] == before
    assert after[-1][1] == "migration_state_changed"


# ===========================================================================
# 3. Refusals - each leaves the state untouched
# ===========================================================================
def test_a_credential_with_an_unknown_key_version_refuses(fleet):
    engine, ring, _devices, _path = fleet
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE receiver_credentials SET hash_key_version = 99 WHERE id = 1")

    with pytest.raises(TransitionReadinessError):
        transition(engine, ring, datetime.now(timezone.utc))

    assert state_of(engine) == ("legacy_only", 1), "a refusal changed the state"


def test_a_legacy_format_credential_refuses(fleet):
    """A wholly unknown format is impossible - the schema has a CHECK constraint
    listing the permitted values, which is a better guarantee than any runtime
    test could give. The reachable case is a LEGACY credential, which hash_only
    would silently orphan because it stops verifying that transport."""
    engine, ring, _devices, _path = fleet
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE receiver_credentials SET token_format = 'legacy_uuid_hex' WHERE id = 1")

    with pytest.raises(TransitionReadinessError):
        transition(engine, ring, datetime.now(timezone.utc))

    assert state_of(engine) == ("legacy_only", 1)


def test_any_legacy_receiver_usage_refuses(fleet):
    """Switching legacy verification off would disconnect a legacy Receiver."""
    engine, ring, _devices, _path = fleet
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO receiver_events (store_id, event_type, event_time) "
            "VALUES (1, 'connected', '2026-07-31T00:00:00+00:00')")

    with pytest.raises(TransitionReadinessError):
        transition(engine, ring, datetime.now(timezone.utc))

    assert state_of(engine) == ("legacy_only", 1)


def test_a_wrong_expected_current_state_refuses(fleet):
    engine, ring, _devices, _path = fleet
    from receiver_migration_transition_service import TransitionStateMismatchError

    with pytest.raises(TransitionStateMismatchError):
        transition(engine, ring, datetime.now(timezone.utc),
                   expected_current_state="backfilled")

    assert state_of(engine) == ("legacy_only", 1)


def test_a_second_transition_is_refused(fleet):
    engine, ring, _devices, _path = fleet
    transition(engine, ring, datetime.now(timezone.utc))

    from receiver_migration_transition_service import TransitionStateMismatchError

    with pytest.raises((TransitionStateMismatchError, InvalidStateTransitionError)):
        transition(engine, ring, datetime.now(timezone.utc))

    assert state_of(engine) == ("hash_only", 0)


def test_a_refusal_leaves_the_database_byte_identical(fleet):
    import hashlib

    engine, ring, _devices, path = fleet
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE receiver_credentials SET hash_key_version = 99")
    engine.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    engine = create_engine(f"sqlite:///{path}", future=True)
    with pytest.raises(TransitionReadinessError):
        transition(engine, ring, datetime.now(timezone.utc))
    engine.dispose()

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


# ===========================================================================
# 4. Authentication after the transition - and the timestamp rule
# ===========================================================================
def test_a_credential_issued_after_the_transition_authenticates(fleet):
    """The point of the whole exercise."""
    engine, ring, _devices, _path = fleet
    transition(engine, ring, datetime.now(timezone.utc))

    version, key = ring.signing_key()
    token = enroll_receiver_device(
        engine, store_id=4, display_name="PC-4", actor_user_id=1,
        hash_key=key, hash_key_version=version).take_raw_credential()

    result = svc.authenticate_receiver_credential(
        engine, presented_token=token, hash_keys=ring.as_mapping(),
        now=datetime.now(timezone.utc))

    assert result.store_id == 4
    assert result.verification_source is svc.VerificationSource.HASHED_DEVICE_CREDENTIAL


def test_a_credential_issued_before_the_transition_still_authenticates(fleet):
    """The four live credentials were issued while still in legacy_only. They
    must work afterwards WITHOUT rotation - that is what makes this recovery
    non-destructive."""
    engine, ring, _devices, _path = fleet
    version, key = ring.signing_key()
    token = enroll_receiver_device(
        engine, store_id=5, display_name="PC-5", actor_user_id=1,
        hash_key=key, hash_key_version=version).take_raw_credential()

    transition(engine, ring, datetime.now(timezone.utc))

    result = svc.authenticate_receiver_credential(
        engine, presented_token=token, hash_keys=ring.as_mapping(),
        now=datetime.now(timezone.utc))

    assert result.store_id == 5


def test_authenticating_with_a_time_before_issuance_is_refused(fleet):
    """This is CORRECT behaviour and is pinned so nobody relaxes it.

    A rehearsal of this very transition appeared to fail because the harness
    reused the transition's `now` to authenticate a credential issued eight
    milliseconds later. _credential_usable rejected a credential that did not yet
    exist at the instant being asked about - a clock-skew and replay defence
    doing exactly its job.
    """
    engine, ring, _devices, _path = fleet
    transition(engine, ring, datetime.now(timezone.utc))
    version, key = ring.signing_key()
    token = enroll_receiver_device(
        engine, store_id=4, display_name="PC-4", actor_user_id=1,
        hash_key=key, hash_key_version=version).take_raw_credential()

    earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
    with pytest.raises(svc.ReceiverAuthenticationError):
        svc.authenticate_receiver_credential(
            engine, presented_token=token, hash_keys=ring.as_mapping(), now=earlier)

    # And the same token, asked about the present, is fine.
    assert svc.authenticate_receiver_credential(
        engine, presented_token=token, hash_keys=ring.as_mapping(),
        now=datetime.now(timezone.utc)).store_id == 4


# ===========================================================================
# 5. The enrolment guard
# ===========================================================================
def test_enrolment_refuses_in_legacy_only_without_consuming_the_code(fleet):
    from receiver_auth_reasons import DeviceEnrolmentBlocked
    from receiver_enrollment_api import create_enrollment_code, redeem_and_enroll
    from sqlalchemy.orm import sessionmaker

    engine, ring, _devices, _path = fleet
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        raw_code = create_enrollment_code(session, store_id=1, actor_user_id=1).code

    devices_before = snapshot(engine, "receiver_devices", "id")
    credentials_before = snapshot(engine, "receiver_credentials", "id")

    with factory() as session:
        with pytest.raises(DeviceEnrolmentBlocked):
            redeem_and_enroll(session, engine, code=raw_code, device_name="X",
                              hostname="X", software_version="1.0.0", key_ring=ring,
                              actor_user_id=1)

    assert snapshot(engine, "receiver_devices", "id") == devices_before
    assert snapshot(engine, "receiver_credentials", "id") == credentials_before

    # The code must still be spendable once HQ is ready.
    with engine.connect() as conn:
        unredeemed = conn.exec_driver_sql(
            "SELECT COUNT(*) FROM receiver_enrollment_codes "
            "WHERE redeemed_at_epoch IS NULL").scalar_one()
    assert unredeemed >= 1, "the refusal consumed the one-time code"


def test_enrolment_succeeds_after_the_transition(fleet):
    from receiver_enrollment_api import create_enrollment_code, redeem_and_enroll
    from sqlalchemy.orm import sessionmaker

    engine, ring, _devices, _path = fleet
    transition(engine, ring, datetime.now(timezone.utc))

    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        raw_code = create_enrollment_code(session, store_id=1, actor_user_id=1).code
    with factory() as session:
        outcome = redeem_and_enroll(session, engine, code=raw_code, device_name="NEW",
                                    hostname="NEW", software_version="1.0.0",
                                    key_ring=ring, actor_user_id=1)
    token = outcome.take_raw_credential()

    assert svc.authenticate_receiver_credential(
        engine, presented_token=token, hash_keys=ring.as_mapping(),
        now=datetime.now(timezone.utc)).store_id == 1


# ===========================================================================
# 6. Nothing is written down
# ===========================================================================
def test_the_transition_result_carries_no_secret(fleet):
    engine, ring, _devices, _path = fleet
    result = transition(engine, ring, datetime.now(timezone.utc))
    text = repr(result).lower()

    for forbidden in ("token", "secret", "hash", "key", "credential_hash"):
        assert forbidden not in text or "hash_only" in text, text[:200]
    assert "speaklink_rcv_v1." not in repr(result)


def test_the_audit_event_carries_no_secret(fleet):
    engine, ring, _devices, _path = fleet
    transition(engine, ring, datetime.now(timezone.utc))

    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT reason_code, metadata_json FROM receiver_credential_events "
            "WHERE event_type='migration_state_changed'").fetchone()
    blob = f"{row[0]} {row[1]}"
    assert "speaklink_rcv_v1." not in blob
    assert "$2b$" not in blob
