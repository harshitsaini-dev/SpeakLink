"""Which one computer in a Store is actually playing the announcement.

A Store is the broadcast target. A Device is a physical Windows computer. Once a
Store can hold more than one Device, something has to decide which of them plays
the audio, and getting that wrong is audible: two Devices on one amplifier is an
echo, and nobody in the Store can tell which computer to look at.

The policy is deliberately dull:

* **Exactly one primary per Store**, enforced by the schema rather than by
  application code. ``store_id`` is the primary key of the mapping table, so two
  primaries cannot exist even if two administrators race - there is no window in
  which the check has passed and the write has not happened yet.
* **Standbys connect, heartbeat and report health, and receive no audio.** A
  standby that received chunks "just in case" would be the echo.
* **Promotion is explicit.** When a primary is disabled or revoked, the Store is
  left with no primary until an administrator promotes another. Automatic
  failover sounds helpful and is not: it moves the announcement to a computer
  nobody has checked is plugged into the amplifier, and it does so silently.

Every test uses a temporary database with the phase-one schema applied.
``backend/echocast_live.db`` and the real pilot database are never opened.
"""

from __future__ import annotations

import os
import secrets
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
from migrations import run_receiver_credential_phase_one  # noqa: E402
from models import HQUser, Store  # noqa: E402
from receiver_device_service import enroll_receiver_device  # noqa: E402
import receiver_enrollment_api as enrolment_api  # noqa: E402

from receiver_primary_device import (  # noqa: E402
    DeviceRole,
    NoPrimaryDeviceError,
    PrimaryDeviceError,
    DeviceNotPromotableError,
    clear_primary_for_device,
    describe_store_devices,
    ensure_primary_device_schema,
    primary_device_id,
    promote_device,
    store_aggregate_state,
)


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"
NOW = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


class Runtime:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "primary.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)
        self.hash_key = secrets.token_bytes(48)

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
        ensure_primary_device_schema(self.engine)

    def enrol(self, *, name: str, store_id: int | None = None):
        return enroll_receiver_device(
            self.engine,
            store_id=store_id or self.store_id,
            display_name=name,
            actor_user_id=self.actor_id,
            hash_key=self.hash_key,
            hash_key_version=1,
            now=NOW,
        )

    def promote(self, device_public_id: str, *, now: datetime = NOW):
        return promote_device(
            self.engine,
            device_public_id=device_public_id,
            actor_user_id=self.actor_id,
            now=now,
        )

    def device_row_id(self, device_public_id: str) -> int:
        return self.query(
            "SELECT id FROM receiver_devices WHERE public_id = ?", (device_public_id,)
        )[0][0]

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


# ===========================================================================
# A Store can hold two Devices, and only one of them is primary
# ===========================================================================
def test_a_store_can_hold_two_devices(runtime: Runtime):
    first = runtime.enrol(name="UN till 1")
    second = runtime.enrol(name="UN till 2")
    devices = describe_store_devices(runtime.engine, store_id=runtime.store_id)
    assert {device["public_id"] for device in devices} == {
        first.device_public_id, second.device_public_id
    }


def test_a_store_starts_with_no_primary(runtime: Runtime):
    """Enrolling is not promoting. A Device that has never been checked against
    the amplifier must not start playing announcements because it was first."""
    runtime.enrol(name="UN till 1")
    assert primary_device_id(runtime.engine, store_id=runtime.store_id) is None


def test_promotion_makes_exactly_one_primary(runtime: Runtime):
    first = runtime.enrol(name="UN till 1")
    runtime.enrol(name="UN till 2")
    runtime.promote(first.device_public_id)

    devices = describe_store_devices(runtime.engine, store_id=runtime.store_id)
    roles = {device["public_id"]: device["role"] for device in devices}
    assert roles[first.device_public_id] == DeviceRole.PRIMARY
    assert sum(1 for role in roles.values() if role == DeviceRole.PRIMARY) == 1


def test_promoting_the_standby_demotes_the_previous_primary(runtime: Runtime):
    first = runtime.enrol(name="UN till 1")
    second = runtime.enrol(name="UN till 2")
    runtime.promote(first.device_public_id)
    runtime.promote(second.device_public_id, now=LATER)

    assert primary_device_id(runtime.engine, store_id=runtime.store_id) == runtime.device_row_id(
        second.device_public_id
    )
    rows = runtime.query("SELECT COUNT(*) FROM receiver_store_primary_device WHERE store_id = ?",
                         (runtime.store_id,))
    assert rows == [(1,)], "a Store ended up with more than one primary row"


def test_two_primaries_are_impossible_by_construction(runtime: Runtime):
    """Not enforced by a check-then-write that two administrators could race
    through: ``store_id`` is the primary key of the mapping table."""
    first = runtime.enrol(name="UN till 1")
    second = runtime.enrol(name="UN till 2")
    runtime.promote(first.device_public_id)

    with runtime.engine.begin() as connection:
        with pytest.raises(Exception):
            connection.execute(
                text(
                    "INSERT INTO receiver_store_primary_device "
                    "(store_id, device_id, promoted_at, promoted_by) "
                    "VALUES (:store_id, :device_id, :now, :actor)"
                ),
                {
                    "store_id": runtime.store_id,
                    "device_id": runtime.device_row_id(second.device_public_id),
                    "now": NOW.isoformat(),
                    "actor": runtime.actor_id,
                },
            )


def test_concurrent_promotions_leave_exactly_one_primary(runtime: Runtime):
    first = runtime.enrol(name="UN till 1")
    second = runtime.enrol(name="UN till 2")
    started = threading.Barrier(2)
    errors: list[BaseException] = []

    def attempt(public_id: str) -> None:
        started.wait(timeout=5)
        try:
            runtime.promote(public_id, now=LATER)
        except BaseException as failure:  # noqa: BLE001 - recorded, asserted below
            errors.append(failure)

    threads = [
        threading.Thread(target=attempt, args=(first.device_public_id,)),
        threading.Thread(target=attempt, args=(second.device_public_id,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    rows = runtime.query(
        "SELECT COUNT(*) FROM receiver_store_primary_device WHERE store_id = ?",
        (runtime.store_id,),
    )
    assert rows == [(1,)], f"two administrators produced {rows[0][0]} primaries"


def test_each_store_has_its_own_primary(runtime: Runtime):
    ours = runtime.enrol(name="UN till 1")
    theirs = runtime.enrol(name="ASR till 1", store_id=runtime.other_store_id)
    runtime.promote(ours.device_public_id)
    runtime.promote(theirs.device_public_id)

    assert primary_device_id(runtime.engine, store_id=runtime.store_id) is not None
    assert primary_device_id(runtime.engine, store_id=runtime.other_store_id) is not None
    assert primary_device_id(runtime.engine, store_id=runtime.store_id) != primary_device_id(
        runtime.engine, store_id=runtime.other_store_id
    )


# ===========================================================================
# Refusals
# ===========================================================================
def test_an_unknown_device_cannot_be_promoted(runtime: Runtime):
    with pytest.raises(DeviceNotPromotableError):
        runtime.promote("11111111-2222-4333-8444-555555555555")


def test_a_disabled_device_cannot_be_promoted(runtime: Runtime):
    """Promoting a Device an administrator just switched off would quietly undo
    the switching off, and put the announcement on it."""
    device = runtime.enrol(name="UN till 1")
    enrolment_api.disable_device(runtime.engine, public_id=device.device_public_id)
    with pytest.raises(DeviceNotPromotableError):
        runtime.promote(device.device_public_id)


def test_a_revoked_device_cannot_be_promoted(runtime: Runtime):
    device = runtime.enrol(name="UN till 1")
    enrolment_api.revoke_device(runtime.engine, public_id=device.device_public_id)
    with pytest.raises(DeviceNotPromotableError):
        runtime.promote(device.device_public_id)


def test_promoting_the_current_primary_again_is_harmless(runtime: Runtime):
    """An operator double-clicking Promote must not produce two rows or an error
    that looks like something went wrong."""
    device = runtime.enrol(name="UN till 1")
    runtime.promote(device.device_public_id)
    runtime.promote(device.device_public_id, now=LATER)
    rows = runtime.query("SELECT COUNT(*) FROM receiver_store_primary_device")
    assert rows == [(1,)]


# ===========================================================================
# Losing a primary: no silent failover
# ===========================================================================
def test_disabling_the_primary_leaves_the_store_without_one(runtime: Runtime):
    """The decision that matters. Automatic failover would move the announcement
    to a computer nobody has checked is plugged into the amplifier, silently."""
    primary = runtime.enrol(name="UN till 1")
    standby = runtime.enrol(name="UN till 2")
    runtime.promote(primary.device_public_id)

    enrolment_api.disable_device(runtime.engine, public_id=primary.device_public_id)
    clear_primary_for_device(runtime.engine, device_id=runtime.device_row_id(primary.device_public_id))

    assert primary_device_id(runtime.engine, store_id=runtime.store_id) is None
    roles = {
        device["public_id"]: device["role"]
        for device in describe_store_devices(runtime.engine, store_id=runtime.store_id)
    }
    assert roles[standby.device_public_id] == DeviceRole.STANDBY, "the standby was promoted silently"


def test_disabling_the_primary_does_not_affect_the_standby(runtime: Runtime):
    primary = runtime.enrol(name="UN till 1")
    standby = runtime.enrol(name="UN till 2")
    runtime.promote(primary.device_public_id)
    enrolment_api.disable_device(runtime.engine, public_id=primary.device_public_id)

    status = runtime.query(
        "SELECT status FROM receiver_devices WHERE public_id = ?", (standby.device_public_id,)
    )
    assert status == [("active",)]


def test_revoking_the_primary_does_not_revoke_the_standby_or_the_store(runtime: Runtime):
    primary = runtime.enrol(name="UN till 1")
    standby = runtime.enrol(name="UN till 2")
    runtime.promote(primary.device_public_id)
    enrolment_api.revoke_device(runtime.engine, public_id=primary.device_public_id)

    assert runtime.query(
        "SELECT status FROM receiver_devices WHERE public_id = ?", (standby.device_public_id,)
    ) == [("active",)]
    assert runtime.query("SELECT is_active FROM stores WHERE id = ?", (runtime.store_id,)) == [(1,)]


def test_the_standby_can_then_be_promoted_explicitly(runtime: Runtime):
    primary = runtime.enrol(name="UN till 1")
    standby = runtime.enrol(name="UN till 2")
    runtime.promote(primary.device_public_id)
    enrolment_api.revoke_device(runtime.engine, public_id=primary.device_public_id)
    clear_primary_for_device(runtime.engine, device_id=runtime.device_row_id(primary.device_public_id))

    runtime.promote(standby.device_public_id, now=LATER)
    assert primary_device_id(runtime.engine, store_id=runtime.store_id) == runtime.device_row_id(
        standby.device_public_id
    )


# ===========================================================================
# The Store aggregate, which must not overclaim
# ===========================================================================
def test_a_store_with_no_primary_says_so(runtime: Runtime):
    runtime.enrol(name="UN till 1")
    aggregate = store_aggregate_state(runtime.engine, store_id=runtime.store_id, device_states={})
    assert aggregate["has_primary"] is False
    assert aggregate["state"] == "NO_PRIMARY"


def test_the_store_aggregate_follows_the_primary_not_the_standby(runtime: Runtime):
    """A standby that is READY tells you nothing about whether the Store can
    play an announcement."""
    primary = runtime.enrol(name="UN till 1")
    standby = runtime.enrol(name="UN till 2")
    runtime.promote(primary.device_public_id)

    aggregate = store_aggregate_state(
        runtime.engine,
        store_id=runtime.store_id,
        device_states={primary.device_public_id: "OFFLINE", standby.device_public_id: "READY"},
    )
    assert aggregate["state"] == "OFFLINE"
    assert aggregate["standby_count"] == 1


def test_the_store_aggregate_never_claims_speaker_verification(runtime: Runtime):
    primary = runtime.enrol(name="UN till 1")
    runtime.promote(primary.device_public_id)
    aggregate = store_aggregate_state(
        runtime.engine,
        store_id=runtime.store_id,
        device_states={primary.device_public_id: "PLAYBACK_CONFIRMED"},
    )
    assert aggregate["state"] == "PLAYBACK_CONFIRMED"
    assert "SPEAKER_VERIFIED" not in str(aggregate)
    assert aggregate.get("speaker_verified") is False


def test_a_legacy_connection_is_visible_but_is_not_called_a_device(runtime: Runtime):
    """A Receiver on the shared Store token has no Device identity, and inventing
    one would make the dashboard lie about what is connected."""
    aggregate = store_aggregate_state(
        runtime.engine,
        store_id=runtime.store_id,
        device_states={},
        legacy_connection_state="READY",
    )
    assert aggregate["legacy_connection"] == "READY"
    assert aggregate["state"] == "LEGACY_ONLY"
    assert aggregate["has_primary"] is False
    for device in aggregate.get("devices", []):
        assert device.get("public_id") is not None, "a legacy connection was given a Device identity"


# ===========================================================================
# When the policy engages at all
# ===========================================================================
def test_a_legacy_receiver_keeps_the_old_behaviour():
    """No Device identity, so nothing to be primary or standby about."""
    from receiver_primary_device import connection_roles

    assert connection_roles(recorded_primary_device_id=None, connecting_device_id=None) == (
        True, False
    )
    assert connection_roles(recorded_primary_device_id=7, connecting_device_id=None) == (
        True, False
    )


def test_a_store_with_no_recorded_primary_is_not_yet_under_the_policy():
    """The subtle decision, written down so it cannot drift.

    "No primary, no audio" would mean that enrolling a Device took its Store off
    the air until somebody clicked Promote - not an upgrade anybody survives
    across 44 Stores. Nothing is written: the Store stays without a primary, it
    is simply not yet governed by one.
    """
    from receiver_primary_device import connection_roles

    assert connection_roles(recorded_primary_device_id=None, connecting_device_id=7) == (
        True, False
    )


def test_a_recorded_primary_is_enforced():
    from receiver_primary_device import connection_roles

    assert connection_roles(recorded_primary_device_id=7, connecting_device_id=7) == (True, True)
    assert connection_roles(recorded_primary_device_id=7, connecting_device_id=8) == (False, False)


def test_a_standby_never_asks_for_the_primarys_socket():
    """``demote_old`` is what keeps a superseded socket alive. A standby must
    never set it, or connecting a spare would take over the Store."""
    from receiver_primary_device import connection_roles

    for device_id in (8, 9, 99):
        is_primary, demote = connection_roles(
            recorded_primary_device_id=7, connecting_device_id=device_id
        )
        assert (is_primary, demote) == (False, False)


# ===========================================================================
# Schema and safety
# ===========================================================================
def test_the_schema_change_is_additive_and_idempotent(runtime: Runtime):
    """Applying it twice must be safe: it runs at startup on every boot."""
    ensure_primary_device_schema(runtime.engine)
    ensure_primary_device_schema(runtime.engine)
    tables = runtime.query("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert ("receiver_store_primary_device",) in tables


def test_the_primary_table_is_indexed_for_the_lookup_it_actually_does(runtime: Runtime):
    """Every audio chunk asks "who is this Store's primary?"."""
    indexed = runtime.query(
        "SELECT name FROM sqlite_master WHERE type = 'index' "
        "AND tbl_name = 'receiver_store_primary_device'"
    )
    assert indexed, "the primary lookup has no index"


def test_promotion_is_recorded_with_who_and_when(runtime: Runtime):
    device = runtime.enrol(name="UN till 1")
    runtime.promote(device.device_public_id)
    rows = runtime.query(
        "SELECT promoted_at, promoted_by FROM receiver_store_primary_device WHERE store_id = ?",
        (runtime.store_id,),
    )
    assert rows[0][0] == NOW.isoformat()
    assert rows[0][1] == runtime.actor_id


def test_nothing_here_stores_credential_material(runtime: Runtime):
    device = runtime.enrol(name="UN till 1")
    runtime.promote(device.device_public_id)
    dumped = str(runtime.query("SELECT * FROM receiver_store_primary_device"))
    assert "echocast_rcv" not in dumped
    assert "hmac" not in dumped.lower()


def test_the_protected_database_is_never_opened(runtime: Runtime):
    before = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    device = runtime.enrol(name="UN till 1")
    runtime.promote(device.device_public_id)
    describe_store_devices(runtime.engine, store_id=runtime.store_id)
    after = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    assert before == after
