"""Receiver credential authentication against REAL PostgreSQL.

WHY THIS FILE EXISTS

RC14 shipped with `authenticate_receiver_credential` refusing every non-SQLite
engine on its first line. HQ would have booted against Supabase, reported
READY, served every admin screen - and authenticated zero Store Receivers.
2716 SQLite tests and 57 PostgreSQL tests all passed, because the SQLite suite
only ever satisfied the precondition and the PostgreSQL suite never called
this path.

So this file drives the authentication service itself, on PostgreSQL, with
real hashed credentials produced by the real hashing code. Nothing here
asserts on schema shape alone; every test authenticates or fails to
authenticate a credential.

WHAT IT DOES NOT COVER

The WebSocket handshake, which is proven separately in
`test_postgres_receiver_handshake.py`. Keeping them apart is deliberate: this
file answers "does the credential resolve", that one answers "does a Receiver
get CONNECTED".

Skipped entirely without ``TEST_POSTGRES_URL``.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import postgres_schema  # noqa: E402
import receiver_auth_service as auth  # noqa: E402
from migrations import PHASE_ONE_NAME, PHASE_ONE_VERSION  # noqa: E402
from receiver_credentials import (  # noqa: E402
    generate_receiver_credential, hash_receiver_token,
)
from sqlalchemy import text  # noqa: E402

from tests.test_postgres_schema import pg_engine, pg_required  # noqa: E402,F401


HASH_KEY_VERSION = 1
HASH_KEY = b"a-test-hmac-key-of-sufficient-length-for-the-validator"
HASH_KEYS = {HASH_KEY_VERSION: HASH_KEY}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def pg(pg_engine):
    """A PostgreSQL schema carrying everything Receiver auth requires.

    ``create_all`` alone is the point of the test: if the two state tables or
    the four auth indexes are missing from ``postgres_schema``, this fixture
    produces a database the service correctly refuses - which is exactly the
    RC14 defect.
    """
    postgres_schema.create_all(pg_engine)
    with pg_engine.begin() as c:
        c.execute(text(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (:v, :n, :t)"),
            {"v": PHASE_ONE_VERSION, "n": PHASE_ONE_NAME, "t": _utc()})
        # The live production state, preserved exactly: hash_only, with legacy
        # verification OFF. A test that quietly used legacy_only would be
        # testing a weaker security posture than the fleet actually runs.
        c.execute(text(
            "INSERT INTO receiver_credential_migration_state "
            "(id, schema_version, state, legacy_verification_enabled, updated_at) "
            "VALUES (1, :v, 'hash_only', 0, :t)"),
            {"v": PHASE_ONE_VERSION, "t": _utc()})
    return pg_engine


def _enrol(engine, *, store_code="BP", device_status="active",
           credential_status="active", revoked=False):
    """Create a Store, a Device and a real hashed credential.

    The token is produced and hashed by the production credential code, so
    this proves the real verification path rather than a fixture's idea of it.
    """
    now = _utc()
    with engine.begin() as c:
        store_id = c.execute(text(
            "INSERT INTO stores (store_code, store_name, city, region, is_online_store, "
            "receiver_token, is_active, lifecycle_state, status, created_at, updated_at) "
            "VALUES (:c, 'Bindapur', 'DELHI', 'NORTH', false, :t, true, 'active', "
            "'offline', now(), now()) RETURNING id"),
            {"c": store_code, "t": uuid.uuid4().hex}).scalar_one()
        device_public_id = str(uuid.uuid4())
        device_id = c.execute(text(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, created_at, updated_at, disabled_at) "
            "VALUES (:p, :s, 'Till 1', :st, :n, :n, :n, :d) RETURNING id"),
            {"p": device_public_id, "s": store_id, "st": device_status, "n": now,
             "d": now if device_status != "active" else None}).scalar_one()

        # The production generator, so the token and its hash are produced by
        # exactly the code a real enrolment runs.
        issued = generate_receiver_credential(version=1)
        credential_public_id, version, token = issued.public_id, issued.version, issued.raw_token
        token_hash = hash_receiver_token(token, HASH_KEY, key_version=HASH_KEY_VERSION)
        credential_id = c.execute(text(
            "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
            "token_format, token_hash, hash_key_version, status, expiry_policy, "
            "issued_at, created_at, revoked_at) "
            "VALUES (:p, :d, :v, 'speaklink_rcv', :h, :kv, :st, 'non_expiring', :n, :n, :r) "
            "RETURNING id"),
            {"p": credential_public_id, "d": device_id, "v": version, "h": token_hash,
             "kv": HASH_KEY_VERSION, "st": credential_status, "n": now,
             "r": now if revoked else None}).scalar_one()
    return {
        "token": token, "store_id": store_id, "device_id": device_id,
        "device_public_id": device_public_id, "credential_id": credential_id,
        "store_code": store_code,
    }


# ===========================================================================
# The defect RC14 shipped with
# ===========================================================================
@pg_required
def test_an_existing_hashed_credential_authenticates_on_postgresql(pg):
    """The whole round in one assertion.

    On RC14 this raises ReceiverAuthenticationConfigurationError from the
    first line of _validate_inputs, for every Receiver in the fleet.
    """
    enrolled = _enrol(pg)

    result = auth.authenticate_receiver_credential(
        pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)

    assert result.store_id == enrolled["store_id"]
    assert result.store_code == enrolled["store_code"]
    assert result.device_id == enrolled["device_id"]
    assert result.device_public_id == enrolled["device_public_id"]
    assert result.credential_id == enrolled["credential_id"]
    assert result.verification_source is auth.VerificationSource.HASHED_DEVICE_CREDENTIAL


@pg_required
def test_the_migration_state_is_hash_only_and_legacy_stays_disabled(pg):
    """The production posture must survive the port.

    hash_only with legacy_verification_enabled = 0 means a legacy Store token
    is no longer accepted. If the port silently landed on legacy_only, every
    Store's old shared token would start working again - a security
    regression that authenticates MORE, not less, so nothing would look
    broken.
    """
    enrolled = _enrol(pg)
    result = auth.authenticate_receiver_credential(
        pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)
    assert result.migration_state == "hash_only"

    with pg.connect() as c:
        flag = c.execute(text(
            "SELECT legacy_verification_enabled FROM receiver_credential_migration_state "
            "WHERE id = 1")).scalar_one()
    assert flag in (0, False), "legacy verification must remain OFF"


@pg_required
def test_a_legacy_store_token_is_refused_under_hash_only(pg):
    """Proves the state is enforced, not merely stored."""
    enrolled = _enrol(pg)
    with pg.connect() as c:
        legacy = c.execute(text("SELECT receiver_token FROM stores WHERE id = :i"),
                           {"i": enrolled["store_id"]}).scalar_one()

    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(pg, presented_token=legacy,
                                              hash_keys=HASH_KEYS)


# ===========================================================================
# Rejections - each for its own reason
# ===========================================================================
@pg_required
def test_a_wrong_credential_is_rejected(pg):
    _enrol(pg)
    other = generate_receiver_credential(version=1).raw_token
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(pg, presented_token=other,
                                              hash_keys=HASH_KEYS)


@pg_required
def test_a_revoked_credential_is_rejected(pg):
    enrolled = _enrol(pg, credential_status="revoked", revoked=True)
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_a_disabled_device_is_rejected(pg):
    enrolled = _enrol(pg, device_status="disabled")
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_a_retired_device_is_rejected(pg):
    """A permanently deleted Device is retired. It must never reconnect."""
    enrolled = _enrol(pg, device_status="retired")
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_an_inactive_store_is_rejected(pg):
    enrolled = _enrol(pg)
    with pg.begin() as c:
        c.execute(text("UPDATE stores SET is_active = :f WHERE id = :i"),
                  {"f": False, "i": enrolled["store_id"]})
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_a_wrong_hash_key_is_rejected(pg):
    """The credential exists, but this HQ cannot verify it. Refuse."""
    enrolled = _enrol(pg)
    with pytest.raises(auth.ReceiverAuthenticationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"],
            hash_keys={HASH_KEY_VERSION: b"a-different-key-of-sufficient-length!!"})


# ===========================================================================
# Configuration guards must still fail closed on PostgreSQL
# ===========================================================================
@pg_required
def test_a_missing_migration_ledger_is_still_refused(pg):
    """The guard must remain a guard. Porting it must not remove it."""
    enrolled = _enrol(pg)
    with pg.begin() as c:
        c.execute(text("DELETE FROM schema_migrations"))
    with pytest.raises(auth.ReceiverAuthenticationConfigurationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_a_missing_migration_state_row_is_still_refused(pg):
    enrolled = _enrol(pg)
    with pg.begin() as c:
        c.execute(text("DELETE FROM receiver_credential_migration_state"))
    with pytest.raises(auth.ReceiverAuthenticationConfigurationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_an_inconsistent_legacy_flag_is_still_refused(pg):
    """hash_only with legacy verification ON is a contradiction.

    It would mean the fleet believes legacy tokens are dead while the verifier
    still accepts them. Refusing is the only safe reading.
    """
    enrolled = _enrol(pg)
    with pg.begin() as c:
        c.execute(text("UPDATE receiver_credential_migration_state "
                       "SET legacy_verification_enabled = 1 WHERE id = 1"))
    with pytest.raises(auth.ReceiverAuthenticationConfigurationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_a_missing_required_index_is_still_refused(pg):
    """The auth-lookup indexes are a correctness requirement, not decoration.

    Without them the credential lookup degrades to a sequential scan of every
    credential on every handshake, which at 100 Stores reconnecting after an
    outage is a self-inflicted outage of its own.
    """
    enrolled = _enrol(pg)
    with pg.begin() as c:
        c.execute(text("DROP INDEX ix_receiver_credentials_auth_lookup"))
    with pytest.raises(auth.ReceiverAuthenticationConfigurationError):
        auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)


@pg_required
def test_no_sqlite_only_construct_is_executed_on_postgresql(pg):
    """Any PRAGMA or sqlite_master reaching PostgreSQL raises ProgrammingError.

    ProgrammingError is NOT the service's own refusal, so catching only the
    service's exceptions here would let a real dialect leak pass as a
    'rejection'. This asserts the successful path runs clean.
    """
    from sqlalchemy.exc import ProgrammingError

    enrolled = _enrol(pg)
    try:
        result = auth.authenticate_receiver_credential(
            pg, presented_token=enrolled["token"], hash_keys=HASH_KEYS)
    except ProgrammingError as leak:  # pragma: no cover - the failure we are pinning
        pytest.fail(f"a SQLite-only construct reached PostgreSQL: {leak}")
    assert result.store_id == enrolled["store_id"]

