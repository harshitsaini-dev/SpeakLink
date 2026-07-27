"""Replacing one Device's credential without taking its Store off the air.

Rotation is the operation you reach for when something has gone wrong: a laptop
was stolen, a technician left, a credential was pasted into a chat window. That
shapes every decision here.

**No overlap.** The old credential stops working the instant the new one is
issued. A grace window is a real pattern and the primitives support one, but it
is exactly wrong for this: the reason you are rotating is usually that somebody
else may have a copy, and a fifteen-minute window is fifteen minutes of that copy
still working. The Device is offline until an operator carries the new credential
to it, which is honest - the alternative is pretending it is safe when it is not.

**One Device, not a Store.** The Store keeps broadcasting, its other Device keeps
working, and the legacy Store token is untouched. If rotating one till took a
Store off the air, nobody would ever do it.

**Failure must not destroy the working credential.** A rotation that dies halfway
and leaves a Device with no usable credential is worse than never rotating: the
computer cannot authenticate and cannot re-enrol without a physical visit. The
whole thing is one transaction, and a test kills it mid-way to prove the old
credential still authenticates afterwards.

Every test uses a temporary database with the phase-one schema applied.
``backend/echocast_live.db`` and the real pilot database are never opened.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
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
from key_custody import FakeProtector, create_key_container, load_key_ring  # noqa: E402
from migrations import run_receiver_credential_phase_one  # noqa: E402
from models import HQUser, Store  # noqa: E402
from receiver_auth_service import (  # noqa: E402
    ReceiverAuthenticationError,
    authenticate_receiver_credential,
)
from receiver_device_service import (  # noqa: E402
    ReceiverDeviceServiceError,
    enroll_receiver_device,
)
import receiver_enrollment_api as api  # noqa: E402

from receiver_rotation_service import (  # noqa: E402
    CredentialAlreadyDeliveredError,
    DeviceNotRotatableError,
    RotationPersistenceError,
    rotate_receiver_device_credential,
)


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"
NOW = datetime(2026, 7, 27, 11, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


class Runtime:
    """A migrated temporary database with one Store and one enrolled Device."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "rotate.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        with self.Session() as db:
            db.add(HQUser(username="pilot-operator", password_hash=hash_password("x"), role="admin"))
            db.add(Store(store_code="UN", store_name="Uttam Nagar Old", city="UN ZONE",
                         region="UN ZONE", receiver_token="a" * 32))
            db.add(Store(store_code="ASR", store_name="Uttam Nagar ASR", city="UN ZONE",
                         region="UN ZONE", receiver_token="b" * 32))
            db.commit()
            self.store_id = db.query(Store).filter(Store.store_code == "UN").one().id
            self.other_store_id = db.query(Store).filter(Store.store_code == "ASR").one().id
            self.actor_id = db.query(HQUser).one().id

        run_receiver_credential_phase_one(self.engine)

        container = tmp_path / "keys.bin"
        create_key_container(container, protector=FakeProtector())
        self.key_ring = load_key_ring(container, protector=FakeProtector())
        self.set_state("legacy_only", 1)

    def set_state(self, state: str, legacy_enabled: int) -> None:
        """Move the migration state.

        This is not decoration. ``receiver_auth_service`` verifies hashed Device
        credentials only in ``dual_verify``, ``hash_only`` and
        ``raw_neutralized``, so a fixture that stayed in ``legacy_only`` would
        prove nothing about whether a rotated credential actually works.
        ``hash_only`` is the post-cutover state these credentials exist for, and
        unlike ``dual_verify`` it needs no backfilled legacy Device.
        """
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE receiver_credential_migration_state SET state = :state, "
                    "legacy_verification_enabled = :enabled, updated_at = :now WHERE id = 1"
                ),
                {"state": state, "enabled": legacy_enabled, "now": NOW.isoformat()},
            )

    def cut_over(self) -> None:
        """Advance to the state in which Device credentials are verified."""
        self.set_state("hash_only", 0)

    def enrol(self, *, store_id: int | None = None, name: str = "UN till 1"):
        version, key = self.key_ring.signing_key()
        return enroll_receiver_device(
            self.engine,
            store_id=store_id or self.store_id,
            display_name=name,
            actor_user_id=self.actor_id,
            hash_key=key,
            hash_key_version=version,
            now=NOW,
        )

    def rotate(self, device_public_id: str, *, now: datetime = LATER, **kwargs):
        version, key = self.key_ring.signing_key()
        return rotate_receiver_device_credential(
            self.engine,
            device_public_id=device_public_id,
            actor_user_id=self.actor_id,
            hash_key=key,
            hash_key_version=version,
            now=now,
            **kwargs,
        )

    def authenticate(self, credential: str, *, now: datetime = LATER):
        # Every authentication in this file means "would this credential work on a
        # server that has cut over?", so the cutover happens here rather than being
        # repeated in twenty tests.
        self.cut_over()
        return authenticate_receiver_credential(
            self.engine,
            presented_token=credential,
            hash_keys=self.key_ring.as_mapping(),
            now=now,
        )

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
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


# ---------------------------------------------------------------------------
# The happy path, and the one property that makes it worth doing
# ---------------------------------------------------------------------------
def test_rotation_issues_exactly_one_new_credential(runtime: Runtime):
    device = runtime.enrol()
    rotated = runtime.rotate(device.device_public_id)

    assert rotated.device_public_id == device.device_public_id
    assert rotated.credential_version == device.credential_version + 1
    credential = rotated.take_raw_credential()
    assert credential.startswith("echocast_rcv_v")


def test_the_raw_credential_is_delivered_only_once(runtime: Runtime):
    """The server keeps a verifier, not the value. A second read cannot succeed,
    so a caller that loses it has to rotate again rather than quietly retry."""
    rotated = runtime.rotate(runtime.enrol().device_public_id)
    rotated.take_raw_credential()
    with pytest.raises(CredentialAlreadyDeliveredError):
        rotated.take_raw_credential()


def test_the_new_credential_authenticates(runtime: Runtime):
    device = runtime.enrol()
    new_credential = runtime.rotate(device.device_public_id).take_raw_credential()

    identity = runtime.authenticate(new_credential)
    assert identity.store_id == runtime.store_id
    assert identity.device_id is not None


def test_the_old_credential_stops_working_immediately(runtime: Runtime):
    """No grace window, deliberately. You rotate because somebody may have a copy;
    fifteen minutes of overlap is fifteen minutes of that copy still working."""
    device = runtime.enrol()
    old_credential = device.take_raw_credential()
    assert runtime.authenticate(old_credential, now=NOW).store_id == runtime.store_id

    runtime.rotate(device.device_public_id)

    with pytest.raises(ReceiverAuthenticationError):
        runtime.authenticate(old_credential, now=LATER)


def test_the_old_credential_is_dead_at_the_very_instant_of_rotation(runtime: Runtime):
    """Not one second later. ``accept_until`` equals the rotation moment, and
    usability is `now >= accept_until`, so there is no window at all."""
    device = runtime.enrol()
    old_credential = device.take_raw_credential()
    runtime.rotate(device.device_public_id, now=LATER)
    with pytest.raises(ReceiverAuthenticationError):
        runtime.authenticate(old_credential, now=LATER)


# ---------------------------------------------------------------------------
# Blast radius: one Device
# ---------------------------------------------------------------------------
def test_the_store_stays_active(runtime: Runtime):
    device = runtime.enrol()
    runtime.rotate(device.device_public_id)
    active = runtime.query("SELECT is_active FROM stores WHERE id = ?", (runtime.store_id,))
    assert active[0][0] == 1


def test_the_stores_legacy_token_is_untouched(runtime: Runtime):
    """Rotating a Device must not disturb the shared token other computers in
    that Store may still be using during the migration period."""
    before = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    runtime.rotate(runtime.enrol().device_public_id)
    after = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    assert before == after


def test_the_other_device_in_the_same_store_is_unaffected(runtime: Runtime):
    first = runtime.enrol(name="UN till 1")
    second = runtime.enrol(name="UN till 2")
    second_credential = second.take_raw_credential()

    runtime.rotate(first.device_public_id)

    assert runtime.authenticate(second_credential).store_id == runtime.store_id
    statuses = runtime.query(
        "SELECT status FROM receiver_devices WHERE public_id = ?", (second.device_public_id,)
    )
    assert statuses[0][0] == "active"


def test_a_device_in_another_store_is_unaffected(runtime: Runtime):
    ours = runtime.enrol()
    theirs = runtime.enrol(store_id=runtime.other_store_id, name="ASR till 1")
    their_credential = theirs.take_raw_credential()

    runtime.rotate(ours.device_public_id)

    assert runtime.authenticate(their_credential).store_id == runtime.other_store_id


def test_the_device_itself_stays_active_and_keeps_its_identity(runtime: Runtime):
    """Rotation changes the secret, not which computer this is. Its public id is
    what the dashboard, the audit log and the operator all refer to."""
    device = runtime.enrol()
    runtime.rotate(device.device_public_id)
    rows = runtime.query(
        "SELECT status, display_name FROM receiver_devices WHERE public_id = ?",
        (device.device_public_id,),
    )
    assert rows == [("active", "UN till 1")]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------
def test_an_unknown_device_is_refused(runtime: Runtime):
    with pytest.raises(DeviceNotRotatableError):
        runtime.rotate("11111111-2222-4333-8444-555555555555")


def test_a_disabled_device_cannot_be_rotated(runtime: Runtime):
    """Handing a fresh credential to a Device an administrator just switched off
    would quietly undo the switching off."""
    device = runtime.enrol()
    api.disable_device(runtime.engine, public_id=device.device_public_id)
    with pytest.raises(DeviceNotRotatableError):
        runtime.rotate(device.device_public_id)


def test_a_revoked_device_cannot_be_rotated(runtime: Runtime):
    device = runtime.enrol()
    api.revoke_device(runtime.engine, public_id=device.device_public_id)
    with pytest.raises(DeviceNotRotatableError):
        runtime.rotate(device.device_public_id)


def test_rotating_a_device_whose_store_went_inactive_is_refused(runtime: Runtime):
    device = runtime.enrol()
    with runtime.Session() as db:
        db.query(Store).filter(Store.id == runtime.store_id).update({Store.is_active: False})
        db.commit()
    with pytest.raises(ReceiverDeviceServiceError):
        runtime.rotate(device.device_public_id)


# ---------------------------------------------------------------------------
# Failure must not destroy the working credential
# ---------------------------------------------------------------------------
def test_a_rotation_that_fails_halfway_leaves_the_old_credential_working(runtime: Runtime):
    """The failure that would otherwise need a van: the Device ends up with no
    credential it can use and no way to enrol without someone driving there."""
    device = runtime.enrol()
    old_credential = device.take_raw_credential()

    def collapse(step: str) -> None:
        if step == "after_credential_insert":
            raise RuntimeError("the disk went away mid-rotation")

    with pytest.raises((RotationPersistenceError, RuntimeError)):
        runtime.rotate(device.device_public_id, step_hook=collapse)

    assert runtime.authenticate(old_credential, now=LATER).store_id == runtime.store_id


def test_a_failed_rotation_leaves_no_half_written_credential_row(runtime: Runtime):
    device = runtime.enrol()

    def collapse(step: str) -> None:
        if step == "after_credential_insert":
            raise RuntimeError("no")

    before = runtime.query("SELECT COUNT(*) FROM receiver_credentials")
    with pytest.raises((RotationPersistenceError, RuntimeError)):
        runtime.rotate(device.device_public_id, step_hook=collapse)
    after = runtime.query("SELECT COUNT(*) FROM receiver_credentials")
    assert before == after


def test_a_failed_rotation_does_not_supersede_the_old_credential(runtime: Runtime):
    device = runtime.enrol()

    def collapse(step: str) -> None:
        if step == "after_supersede":
            raise RuntimeError("no")

    with pytest.raises((RotationPersistenceError, RuntimeError)):
        runtime.rotate(device.device_public_id, step_hook=collapse)

    statuses = runtime.query(
        "SELECT status FROM receiver_credentials WHERE device_id = "
        "(SELECT id FROM receiver_devices WHERE public_id = ?)",
        (device.device_public_id,),
    )
    assert [row[0] for row in statuses] == ["active"]


# ---------------------------------------------------------------------------
# Nothing secret is written down
# ---------------------------------------------------------------------------
def test_the_raw_credential_is_absent_from_every_table(runtime: Runtime):
    device = runtime.enrol()
    credential = runtime.rotate(device.device_public_id).take_raw_credential()
    secret = credential.rsplit(".", 1)[-1]

    connection = sqlite3.connect(f"file:{runtime.path}?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            dumped = str(connection.execute(f"SELECT * FROM {table}").fetchall())
            assert credential not in dumped, f"the credential is stored in {table}"
            assert secret not in dumped, f"the credential's secret is stored in {table}"
    finally:
        connection.close()


def test_the_rotation_result_never_renders_the_credential(runtime: Runtime):
    rotated = runtime.rotate(runtime.enrol().device_public_id)
    rendering = repr(rotated)
    assert "redacted" in rendering
    credential = rotated.take_raw_credential()
    assert credential not in rendering


def test_device_listings_never_carry_credential_material(runtime: Runtime):
    device = runtime.enrol()
    runtime.rotate(device.device_public_id)
    listed = api.list_devices(runtime.engine, store_id=runtime.store_id)
    rendered = str(listed)
    for forbidden in ("credential", "token", "hash", "secret"):
        assert forbidden not in rendered.lower(), f"a device listing exposed {forbidden}"


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------
def test_the_new_credential_records_the_key_version_it_was_hashed_with(runtime: Runtime):
    """Without this, rotating the HMAC key ring would make it impossible to know
    which credentials can still be verified."""
    device = runtime.enrol()
    runtime.rotate(device.device_public_id)
    versions = runtime.query(
        "SELECT hash_key_version FROM receiver_credentials WHERE status = 'active' "
        "AND device_id = (SELECT id FROM receiver_devices WHERE public_id = ?)",
        (device.device_public_id,),
    )
    assert versions == [(runtime.key_ring.active_version,)]


def test_the_new_credential_points_back_at_the_one_it_replaced(runtime: Runtime):
    device = runtime.enrol()
    runtime.rotate(device.device_public_id)
    rows = runtime.query(
        "SELECT status, replaces_credential_id FROM receiver_credentials "
        "WHERE device_id = (SELECT id FROM receiver_devices WHERE public_id = ?) "
        "ORDER BY credential_version",
        (device.device_public_id,),
    )
    assert rows[0][0] == "superseded"
    assert rows[1][0] == "active"
    assert rows[1][1] is not None, "the audit trail does not join the two credentials"


def test_rotation_writes_a_credential_event(runtime: Runtime):
    device = runtime.enrol()
    before = runtime.query("SELECT COUNT(*) FROM receiver_credential_events")[0][0]
    runtime.rotate(device.device_public_id)
    events = runtime.query(
        "SELECT event_type, outcome, actor_user_id, metadata_json "
        "FROM receiver_credential_events ORDER BY id"
    )
    assert len(events) > before
    event_type, outcome, actor, metadata = events[-1]
    assert "rotat" in event_type.lower()
    assert outcome == "success"
    assert actor == runtime.actor_id
    assert "echocast_rcv_v" not in (metadata or "")


def test_the_superseded_credential_records_when_it_died(runtime: Runtime):
    device = runtime.enrol()
    runtime.rotate(device.device_public_id, now=LATER)
    rows = runtime.query(
        "SELECT replaced_at, accept_until FROM receiver_credentials "
        "WHERE status = 'superseded' AND device_id = "
        "(SELECT id FROM receiver_devices WHERE public_id = ?)",
        (device.device_public_id,),
    )
    replaced_at, accept_until = rows[0]
    assert replaced_at == LATER.isoformat()
    assert accept_until == LATER.isoformat(), "a grace window was granted that nobody asked for"


# ---------------------------------------------------------------------------
# Two administrators clicking at once
# ---------------------------------------------------------------------------
def test_concurrent_rotations_produce_exactly_one_new_active_credential(runtime: Runtime):
    """Two administrators, one Device, the same second. Whatever happens, the
    Device must not end up with two live credentials or none."""
    device = runtime.enrol()
    started = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def attempt() -> None:
        started.wait(timeout=5)
        try:
            results.append(runtime.rotate(device.device_public_id))
        except BaseException as failure:  # noqa: BLE001 - recorded, then asserted on
            errors.append(failure)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    active = runtime.query(
        "SELECT COUNT(*) FROM receiver_credentials WHERE status = 'active' "
        "AND device_id = (SELECT id FROM receiver_devices WHERE public_id = ?)",
        (device.device_public_id,),
    )
    assert active == [(1,)], f"the Device has {active[0][0]} live credentials"
    assert len(results) + len(errors) == 2
    assert len(results) >= 1, "both rotations failed; the Device would be stranded"


def test_every_delivered_credential_authenticates_or_was_refused(runtime: Runtime):
    """A rotation that returns a credential which does not work is the worst
    outcome of all: the operator types it in and the Device never comes back."""
    device = runtime.enrol()
    delivered = runtime.rotate(device.device_public_id).take_raw_credential()
    assert runtime.authenticate(delivered).device_id is not None

    second = runtime.rotate(device.device_public_id, now=LATER + timedelta(minutes=1))
    assert runtime.authenticate(
        second.take_raw_credential(), now=LATER + timedelta(minutes=2)
    ).device_id is not None


def test_rotating_twice_leaves_only_the_newest_credential_alive(runtime: Runtime):
    device = runtime.enrol()
    first = runtime.rotate(device.device_public_id, now=LATER).take_raw_credential()
    second_moment = LATER + timedelta(minutes=1)
    runtime.rotate(device.device_public_id, now=second_moment)

    with pytest.raises(ReceiverAuthenticationError):
        runtime.authenticate(first, now=second_moment)


# ---------------------------------------------------------------------------
# The migration state, which is where the sharp edge is
# ---------------------------------------------------------------------------
def test_rotation_is_allowed_exactly_where_the_credential_would_work(runtime: Runtime):
    """Rotation issues a credential, so it follows the same rule as enrolment.

    Both are allowed exactly where ``receiver_auth_service`` can verify the
    result, plus ``legacy_only`` so a credential issued during the rehearsal
    window survives cutover. ``backfilled`` is refused in both: hashed
    credentials are not verified there, so a credential issued into it could not
    authenticate.
    """
    from receiver_device_service import MigrationNotReadyError

    device = runtime.enrol()
    allowed, refused = [], []
    for state, legacy_enabled in (
        ("legacy_only", 1), ("backfilled", 1), ("dual_verify", 1),
        ("hash_only", 0), ("raw_neutralized", 0),
    ):
        runtime.set_state(state, legacy_enabled)
        try:
            runtime.rotate(device.device_public_id, now=LATER)
            allowed.append(state)
        except MigrationNotReadyError:
            refused.append(state)

    assert allowed == ["legacy_only", "dual_verify", "hash_only", "raw_neutralized"]
    assert refused == ["backfilled"], (
        "rotating in 'backfilled' would hand an operator a credential that cannot "
        "authenticate, which is the failure this gate exists to prevent"
    )


def test_a_credential_rotated_in_the_rehearsal_window_still_works_after_cutover(
    runtime: Runtime,
):
    """Rotations done during ``legacy_only`` must not become dead on cutover, or
    an operator's careful pre-cutover rotation would silently strand a Device."""
    device = runtime.enrol()
    new_credential = runtime.rotate(device.device_public_id).take_raw_credential()
    runtime.cut_over()
    assert runtime.authenticate(new_credential).device_id is not None


def test_rotation_still_refuses_a_database_without_the_phase_one_schema(tmp_path):
    """The state rule is relaxed; the schema rule is not."""
    from receiver_device_service import MigrationNotReadyError

    bare = tmp_path / "bare.db"
    engine = create_engine(f"sqlite:///{bare.as_posix()}")
    Base.metadata.create_all(bind=engine)
    try:
        with pytest.raises((MigrationNotReadyError, ReceiverDeviceServiceError)):
            rotate_receiver_device_credential(
                engine,
                device_public_id="11111111-2222-4333-8444-555555555555",
                actor_user_id=1,
                hash_key=b"k" * 48,
                hash_key_version=1,
                now=LATER,
            )
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The protected database
# ---------------------------------------------------------------------------
def test_the_protected_database_is_never_opened(runtime: Runtime):
    before = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    runtime.rotate(runtime.enrol().device_public_id)
    after = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    assert before == after
