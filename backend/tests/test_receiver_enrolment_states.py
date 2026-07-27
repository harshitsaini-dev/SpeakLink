"""When a Device may be enrolled, and how many one Store may hold.

Two rules were measured rather than assumed, found to contradict each other, and
have now been changed together by an explicit architecture decision.

**Enrolment used to be pinned to ``legacy_only``.** That was right for a
migration rehearsal and wrong for production: ``receiver_auth_service`` verifies
hashed Device credentials only in ``dual_verify``, ``hash_only`` and
``raw_neutralized``. The two sets were disjoint, so a server that had cut over
could never enrol a new till - the Device would be created and could never
connect. Enrolment now follows the states in which the credential it issues can
actually be used.

``backfilled`` stays refused, and that is the interesting half of the rule.
Hashed credentials are not verified there either, so enrolling into it would hand
an operator a credential that silently cannot authenticate - the same trap, one
state along. Failing closed is the whole point.

**The per-Store limit rose from two to three.** Legacy backfill creates a Device
of its own, so a backfilled Store had room for exactly one enrolled Device, and
primary-plus-standby was arithmetically impossible. Three is a migration-period
number: one legacy backfilled Device, one primary, one standby.

Every test uses a temporary database with the phase-one schema applied.
``backend/echocast_live.db`` and the real pilot database are never opened.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from auth import hash_password  # noqa: E402
from db import Base  # noqa: E402
from migrations import run_receiver_credential_phase_one  # noqa: E402
from models import HQUser, Store  # noqa: E402
from receiver_auth_service import (  # noqa: E402
    ReceiverAuthenticationError,
    authenticate_receiver_credential,
)
from receiver_credential_backfill import rehearse_legacy_receiver_backfill  # noqa: E402
from receiver_credentials import MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE  # noqa: E402
from receiver_device_service import (  # noqa: E402
    ENROLLABLE_STATES,
    DeviceLimitExceededError,
    MigrationNotReadyError,
    enroll_receiver_device,
)


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)

#: Every state the migration state machine knows, so a new one cannot be added
#: without a decision about whether enrolling into it is safe.
ALL_STATES = (
    ("legacy_only", 1),
    ("backfilled", 1),
    ("dual_verify", 1),
    ("hash_only", 0),
    ("raw_neutralized", 0),
)


class Runtime:
    """One Store, phase-one schema, and a key ring - no backfill unless asked."""

    def __init__(self, tmp_path: Path, *, backfill: bool = False) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.path = tmp_path / "enrolment-states.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.hash_key = secrets.token_bytes(48)
        self.backfill_key = secrets.token_bytes(48)

        with self.Session() as db:
            db.add(HQUser(username="pilot-operator", password_hash=hash_password("x"), role="admin"))
            db.add(Store(store_code="UN", store_name="Uttam Nagar Old", city="UN ZONE",
                         region="UN ZONE", receiver_token="a" * 32))
            db.commit()
            self.store_id = db.query(Store).one().id
            self.actor_id = db.query(HQUser).one().id

        run_receiver_credential_phase_one(self.engine)
        if backfill:
            rehearse_legacy_receiver_backfill(
                self.engine, hash_key=self.backfill_key, hash_key_version=1, now=NOW
            )
        self.set_state("legacy_only", 1)

    def set_state(self, state: str, legacy_enabled: int) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE receiver_credential_migration_state SET state = :state, "
                    "legacy_verification_enabled = :enabled, updated_at = :now WHERE id = 1"
                ),
                {"state": state, "enabled": legacy_enabled, "now": NOW.isoformat()},
            )

    def enrol(self, *, name: str = "UN till 1", key_version: int = 2):
        return enroll_receiver_device(
            self.engine,
            store_id=self.store_id,
            display_name=name,
            actor_user_id=self.actor_id,
            hash_key=self.hash_key,
            hash_key_version=key_version,
            now=NOW,
        )

    def authenticate(self, credential: str):
        return authenticate_receiver_credential(
            self.engine,
            presented_token=credential,
            hash_keys={1: self.backfill_key, 2: self.hash_key},
            now=NOW,
        )

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        import sqlite3

        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()


@pytest.fixture()
def runtime(tmp_path) -> Runtime:
    made = Runtime(tmp_path)
    yield made
    made.engine.dispose()


@pytest.fixture()
def backfilled_runtime(tmp_path) -> Runtime:
    made = Runtime(tmp_path, backfill=True)
    yield made
    made.engine.dispose()


# ===========================================================================
# Which states allow enrolment
# ===========================================================================
def test_enrolment_is_allowed_exactly_where_the_credential_would_work(tmp_path):
    """The approved rule, stated once and checked against every known state."""
    allowed, refused = [], []
    for index, (state, legacy_enabled) in enumerate(ALL_STATES):
        runtime = Runtime(tmp_path / f"state-{index}")
        try:
            runtime.set_state(state, legacy_enabled)
            try:
                runtime.enrol(name=f"till in {state}")
                allowed.append(state)
            except MigrationNotReadyError:
                refused.append(state)
        finally:
            runtime.engine.dispose()

    assert allowed == ["legacy_only", "dual_verify", "hash_only", "raw_neutralized"]
    assert refused == ["backfilled"]


def test_the_allowed_states_are_the_states_that_can_verify_a_credential(tmp_path):
    """The rule is not a list somebody remembered to update.

    ``legacy_only`` is the deliberate exception: credentials issued during the
    rehearsal window must survive cutover rather than being born dead.
    """
    from receiver_auth_service import _HASH_STATES

    assert ENROLLABLE_STATES == _HASH_STATES | {"legacy_only"}
    assert "backfilled" not in ENROLLABLE_STATES


def test_rotation_and_enrolment_share_one_state_rule():
    """Issuing a credential is issuing a credential. If a state is unsafe to
    enrol into, it is equally unsafe to rotate into, and two lists that merely
    agree today would eventually stop agreeing."""
    from receiver_rotation_service import ROTATABLE_STATES

    assert ROTATABLE_STATES is ENROLLABLE_STATES


def test_an_unknown_migration_state_still_fails_closed(runtime: Runtime):
    """Relaxing the rule must not turn it into "anything goes"."""
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            text("UPDATE receiver_credential_migration_state SET state = 'unexpected'")
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(MigrationNotReadyError):
        runtime.enrol()


def test_a_database_without_the_phase_one_schema_still_fails_closed(tmp_path):
    bare = tmp_path / "bare.db"
    engine = create_engine(f"sqlite:///{bare.as_posix()}")
    Base.metadata.create_all(bind=engine)
    try:
        with pytest.raises(MigrationNotReadyError):
            enroll_receiver_device(
                engine, store_id=1, display_name="x", actor_user_id=1,
                hash_key=b"k" * 48, hash_key_version=1, now=NOW,
            )
    finally:
        engine.dispose()


def test_enrolling_into_backfilled_is_refused_before_anything_is_written(
    backfilled_runtime: Runtime,
):
    """A refusal that still created a Device row would be the worst of both."""
    backfilled_runtime.set_state("backfilled", 1)
    before = backfilled_runtime.query("SELECT COUNT(*) FROM receiver_devices")
    with pytest.raises(MigrationNotReadyError):
        backfilled_runtime.enrol()
    after = backfilled_runtime.query("SELECT COUNT(*) FROM receiver_devices")
    assert before == after


# ===========================================================================
# The property the whole change exists for
# ===========================================================================
def test_a_device_enrolled_in_dual_verify_can_authenticate_in_dual_verify(
    backfilled_runtime: Runtime,
):
    """The end-to-end property that was impossible before: enrol and connect on
    the same server, in the same state, without an operator moving a lever."""
    backfilled_runtime.set_state("dual_verify", 1)
    enrolled = backfilled_runtime.enrol(name="UN till 1")
    credential = enrolled.take_raw_credential()

    identity = backfilled_runtime.authenticate(credential)
    assert identity.store_id == backfilled_runtime.store_id
    assert identity.device_id is not None
    assert identity.verification_source.value == "hashed_device_credential"


def test_a_device_enrolled_in_hash_only_can_authenticate_in_hash_only(runtime: Runtime):
    runtime.set_state("hash_only", 0)
    credential = runtime.enrol().take_raw_credential()
    identity = runtime.authenticate(credential)
    assert identity.device_id is not None


def test_the_legacy_store_token_still_works_in_dual_verify(backfilled_runtime: Runtime):
    """The migration period is the point: a Store on a shared token must keep
    broadcasting while its tills are enrolled one at a time."""
    backfilled_runtime.set_state("dual_verify", 1)
    backfilled_runtime.enrol(name="UN till 1")
    identity = backfilled_runtime.authenticate("a" * 32)
    assert identity.store_id == backfilled_runtime.store_id


def test_enrolling_in_dual_verify_does_not_disturb_the_legacy_device(
    backfilled_runtime: Runtime,
):
    """``_validate_backfilled_fleet`` requires exactly one legacy-format
    credential per Store. Enrolling must not add a second or the whole Store
    stops authenticating."""
    backfilled_runtime.set_state("dual_verify", 1)
    backfilled_runtime.enrol(name="UN till 1")
    legacy_rows = backfilled_runtime.query(
        "SELECT COUNT(*) FROM receiver_credentials WHERE token_format = 'legacy_uuid_hex'"
    )
    assert legacy_rows == [(1,)]


# ===========================================================================
# Three Devices: legacy + primary + standby
# ===========================================================================
def test_the_approved_per_store_limit_is_three():
    assert MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE == 3


def test_a_backfilled_store_can_hold_a_legacy_device_a_primary_and_a_standby(
    backfilled_runtime: Runtime,
):
    """The arithmetic that used to make primary-plus-standby impossible: legacy
    backfill takes one of the slots."""
    backfilled_runtime.set_state("dual_verify", 1)
    primary = backfilled_runtime.enrol(name="UN till 1 (primary)")
    standby = backfilled_runtime.enrol(name="UN till 2 (standby)")

    devices = backfilled_runtime.query(
        "SELECT display_name, status FROM receiver_devices ORDER BY id"
    )
    assert len(devices) == 3
    assert [row[1] for row in devices] == ["active", "active", "active"]

    for issued in (primary, standby):
        assert backfilled_runtime.authenticate(issued.take_raw_credential()).device_id


def test_a_fourth_active_device_is_still_refused(backfilled_runtime: Runtime):
    """Three is a bound, not an invitation. Without one, a stuck script turns a
    Store into a pile of live credentials nobody can account for."""
    backfilled_runtime.set_state("dual_verify", 1)
    backfilled_runtime.enrol(name="UN till 1")
    backfilled_runtime.enrol(name="UN till 2")
    with pytest.raises(DeviceLimitExceededError):
        backfilled_runtime.enrol(name="UN till 3")


def test_a_store_without_backfill_can_hold_three_enrolled_devices(runtime: Runtime):
    runtime.set_state("hash_only", 0)
    for name in ("till 1", "till 2", "till 3"):
        runtime.enrol(name=name)
    with pytest.raises(DeviceLimitExceededError):
        runtime.enrol(name="till 4")


def test_a_disabled_device_frees_its_slot(backfilled_runtime: Runtime):
    """Otherwise a Store that replaced a till twice could never enrol again."""
    backfilled_runtime.set_state("dual_verify", 1)
    backfilled_runtime.enrol(name="UN till 1")
    second = backfilled_runtime.enrol(name="UN till 2")

    import receiver_enrollment_api as enrolment_api

    enrolment_api.disable_device(backfilled_runtime.engine, public_id=second.device_public_id)
    replacement = backfilled_runtime.enrol(name="UN till 2 replacement")
    assert replacement.device_public_id != second.device_public_id


def test_a_revoked_device_frees_its_slot(backfilled_runtime: Runtime):
    backfilled_runtime.set_state("dual_verify", 1)
    first = backfilled_runtime.enrol(name="UN till 1")

    import receiver_enrollment_api as enrolment_api

    enrolment_api.revoke_device(backfilled_runtime.engine, public_id=first.device_public_id)
    backfilled_runtime.enrol(name="UN till 1 replacement")
    backfilled_runtime.enrol(name="UN till 2")
    with pytest.raises(DeviceLimitExceededError):
        backfilled_runtime.enrol(name="UN till 3")


def test_a_revoked_devices_credential_stops_working(backfilled_runtime: Runtime):
    backfilled_runtime.set_state("dual_verify", 1)
    enrolled = backfilled_runtime.enrol(name="UN till 1")
    credential = enrolled.take_raw_credential()
    assert backfilled_runtime.authenticate(credential).device_id is not None

    import receiver_enrollment_api as enrolment_api

    enrolment_api.revoke_device(backfilled_runtime.engine, public_id=enrolled.device_public_id)
    with pytest.raises(ReceiverAuthenticationError):
        backfilled_runtime.authenticate(credential)


def test_a_disabled_devices_credential_stops_working(backfilled_runtime: Runtime):
    backfilled_runtime.set_state("dual_verify", 1)
    enrolled = backfilled_runtime.enrol(name="UN till 1")
    credential = enrolled.take_raw_credential()

    import receiver_enrollment_api as enrolment_api

    enrolment_api.disable_device(backfilled_runtime.engine, public_id=enrolled.device_public_id)
    with pytest.raises(ReceiverAuthenticationError):
        backfilled_runtime.authenticate(credential)


# ===========================================================================
# The protected database
# ===========================================================================
def test_the_protected_database_is_never_opened(backfilled_runtime: Runtime):
    before = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    backfilled_runtime.set_state("dual_verify", 1)
    credential = backfilled_runtime.enrol().take_raw_credential()
    backfilled_runtime.authenticate(credential)
    after = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    assert before == after
