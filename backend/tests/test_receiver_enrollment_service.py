"""Joining a one-time code to a Device credential, without losing either.

Two halves already existed and had never been introduced to each other:
``receiver_enrollment_codes`` decides *who may ask*, and
``receiver_device_service.enroll_receiver_device`` issues the credential. This
is the join, and the join is where the interesting failures live.

The hard part is that they do not share a transaction. The code lives in the
ORM session; the device enrolment opens its own connection and runs its own
``BEGIN IMMEDIATE``. So the order matters:

* claim the code first, or two computers racing with the same code both enrol;
* if the enrolment then fails, release the claim, or a valid code is burnt by a
  server-side fault and the operator has to issue another for no reason.

Both are tested here, including the release path.

Every test uses a temporary database with the phase-one schema applied.
``backend/echocast_live.db`` and the real pilot database are never opened.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine
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
from models import HQUser, ReceiverEnrollmentCode, Store  # noqa: E402
# Referenced through the module on purpose. test_receiver_enrollment_endpoints
# pops this module from sys.modules to build an isolated runtime, which creates
# two class identities - a `from ... import api.EnrollmentRefused` would bind the old
# class while the code raises the new one, and pytest.raises would miss it.
import receiver_enrollment_api as api  # noqa: E402

MAX_OUTSTANDING_CODES_PER_STORE = api.MAX_OUTSTANDING_CODES_PER_STORE


PROTECTED_DATABASE = BACKEND_ROOT / "echocast_live.db"
DEVICE_NAME = "UN counter PC"
HOSTNAME = "UN-RECEIVER-01"
VERSION = "0.9.0-pilot"


class Runtime:
    """A migrated temporary database plus a key ring, ready to enrol into."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "enrol.db"
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

    def enrol(self, code: str, *, name: str = DEVICE_NAME):
        # Called through the module rather than a module-level import, so a test
        # that monkeypatches receiver_enrollment_api affects this call. A direct
        # `from ... import api.redeem_and_enroll` binds the function object once and
        # would quietly ignore the patch.
        import receiver_enrollment_api

        with self.Session() as db:
            return receiver_enrollment_api.redeem_and_enroll(
                db, self.engine, code=code, device_name=name, hostname=HOSTNAME,
                software_version=VERSION, key_ring=self.key_ring, actor_user_id=self.actor_id,
            )

    def new_code(self, store_id: int | None = None) -> str:
        with self.Session() as db:
            return api.create_enrollment_code(
                db, store_id=store_id or self.store_id, actor_user_id=self.actor_id
            ).code

    def rows(self, table: str) -> list[tuple]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return connection.execute(f"SELECT * FROM {table}").fetchall()
        finally:
            connection.close()


@pytest.fixture()
def runtime(tmp_path) -> Runtime:
    made = Runtime(tmp_path)
    yield made
    made.engine.dispose()


# ---------------------------------------------------------------------------
# Creating codes
# ---------------------------------------------------------------------------
def test_a_code_is_created_for_an_active_store(runtime: Runtime):
    with runtime.Session() as db:
        issued = api.create_enrollment_code(db, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert issued.code
    assert issued.store_id == runtime.store_id


def test_outstanding_codes_are_bounded_per_store(runtime: Runtime):
    """Otherwise a stuck script mints codes until the table is a haystack of
    live credentials."""
    for _ in range(MAX_OUTSTANDING_CODES_PER_STORE):
        runtime.new_code()
    with runtime.Session() as db:
        with pytest.raises(api.TooManyOutstandingCodes):
            api.create_enrollment_code(db, store_id=runtime.store_id, actor_user_id=runtime.actor_id)


def test_the_bound_is_per_store_not_global(runtime: Runtime):
    for _ in range(MAX_OUTSTANDING_CODES_PER_STORE):
        runtime.new_code()
    assert runtime.new_code(store_id=runtime.other_store_id)


def test_redeeming_a_code_frees_room_for_another(runtime: Runtime):
    codes = [runtime.new_code() for _ in range(MAX_OUTSTANDING_CODES_PER_STORE)]
    runtime.enrol(codes[0])
    assert runtime.new_code()


def test_an_inactive_store_gets_no_code(runtime: Runtime):
    with runtime.Session() as db:
        db.query(Store).filter(Store.id == runtime.store_id).one().is_active = False
        db.commit()
    with runtime.Session() as db:
        with pytest.raises(api.EnrollmentRefused):
            api.create_enrollment_code(db, store_id=runtime.store_id, actor_user_id=runtime.actor_id)


# ---------------------------------------------------------------------------
# Redeeming
# ---------------------------------------------------------------------------
def test_a_valid_code_enrols_one_device_and_returns_one_credential(runtime: Runtime):
    result = runtime.enrol(runtime.new_code())
    assert result.device_public_id
    credential = result.take_raw_credential()
    assert credential.startswith("echocast_rcv_v")
    assert len(runtime.rows("receiver_devices")) == 1


def test_the_raw_credential_is_delivered_exactly_once(runtime: Runtime):
    result = runtime.enrol(runtime.new_code())
    result.take_raw_credential()
    with pytest.raises(Exception):
        result.take_raw_credential()


def test_the_raw_credential_is_never_stored(runtime: Runtime):
    result = runtime.enrol(runtime.new_code())
    credential = result.take_raw_credential()
    for table in ("receiver_devices", "receiver_credentials", "receiver_enrollment_codes"):
        rendered = " ".join(str(cell) for row in runtime.rows(table) for cell in row)
        assert credential not in rendered, f"the raw credential reached {table}"


def test_two_devices_for_one_store_get_different_credentials(runtime: Runtime):
    first = runtime.enrol(runtime.new_code(), name="UN counter PC").take_raw_credential()
    second = runtime.enrol(runtime.new_code(), name="UN back office PC").take_raw_credential()
    assert first != second
    assert len(runtime.rows("receiver_devices")) == 2


def test_a_reused_code_is_rejected(runtime: Runtime):
    code = runtime.new_code()
    runtime.enrol(code)
    with pytest.raises(api.EnrollmentRefused):
        runtime.enrol(code)
    assert len(runtime.rows("receiver_devices")) == 1


def test_an_invented_code_is_rejected(runtime: Runtime):
    with pytest.raises(api.EnrollmentRefused):
        runtime.enrol("not-a-real-code")
    assert runtime.rows("receiver_devices") == []


def test_a_store_disabled_between_issue_and_redemption_is_rejected(runtime: Runtime):
    code = runtime.new_code()
    with runtime.Session() as db:
        db.query(Store).filter(Store.id == runtime.store_id).one().is_active = False
        db.commit()
    with pytest.raises(api.EnrollmentRefused):
        runtime.enrol(code)
    assert runtime.rows("receiver_devices") == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("device_name", ""),
        ("device_name", "   "),
        ("device_name", "x" * 500),
        ("hostname", "x" * 500),
        ("software_version", "x" * 500),
    ],
)
def test_device_metadata_is_validated_and_bounded(runtime: Runtime, field: str, value: str):
    payload = {"device_name": DEVICE_NAME, "hostname": HOSTNAME, "software_version": VERSION}
    payload[field] = value
    code = runtime.new_code()
    with runtime.Session() as db:
        with pytest.raises(api.EnrollmentRefused):
            api.redeem_and_enroll(db, runtime.engine, code=code, key_ring=runtime.key_ring,
                              actor_user_id=runtime.actor_id, **payload)


# ---------------------------------------------------------------------------
# The join: a failure must not burn a valid code
# ---------------------------------------------------------------------------
def test_a_failed_enrolment_releases_the_code(runtime: Runtime, monkeypatch):
    """The code is claimed before the device is created, so a race cannot enrol
    twice. That means a server-side failure afterwards has to give the code
    back, or the operator issues a replacement for no reason."""
    import receiver_enrollment_api

    def explode(*args, **kwargs):
        raise RuntimeError("simulated credential failure")

    monkeypatch.setattr(receiver_enrollment_api, "enroll_receiver_device", explode)

    code = runtime.new_code()
    with pytest.raises(Exception):
        runtime.enrol(code)

    assert runtime.rows("receiver_devices") == []
    monkeypatch.undo()
    # The same code still works, which is the whole point.
    assert runtime.enrol(code).device_public_id


def test_concurrent_redemption_enrols_exactly_one_device(runtime: Runtime):
    code = runtime.new_code()
    winners: list[str] = []
    losers: list[str] = []
    barrier = threading.Barrier(6)

    def attempt():
        barrier.wait()
        try:
            winners.append(runtime.enrol(code).device_public_id)
        except api.EnrollmentRefused as refusal:
            losers.append(type(refusal).__name__)
        except Exception as unexpected:
            losers.append(f"UNEXPECTED:{type(unexpected).__name__}")

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"{len(winners)} devices enrolled from one code"
    assert not [entry for entry in losers if entry.startswith("UNEXPECTED")], losers
    assert len(runtime.rows("receiver_devices")) == 1


# ---------------------------------------------------------------------------
# Failing closed when the platform is not ready
# ---------------------------------------------------------------------------
def test_enrolment_fails_closed_without_the_phase_one_schema(tmp_path):
    path = tmp_path / "unmigrated.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(HQUser(username="op", password_hash=hash_password("x"), role="admin"))
        db.add(Store(store_code="UN", store_name="UN", city="Z", region="Z", receiver_token="c" * 32))
        db.commit()
        store_id = db.query(Store).one().id
        actor_id = db.query(HQUser).one().id
        code = api.create_enrollment_code(db, store_id=store_id, actor_user_id=actor_id).code

    container = tmp_path / "k.bin"
    create_key_container(container, protector=FakeProtector())
    ring = load_key_ring(container, protector=FakeProtector())

    with Session() as db:
        with pytest.raises(api.EnrollmentUnavailable):
            api.redeem_and_enroll(db, engine, code=code, device_name=DEVICE_NAME, hostname=HOSTNAME,
                              software_version=VERSION, key_ring=ring, actor_user_id=actor_id)
    engine.dispose()


def test_enrolment_fails_closed_without_a_key_ring(runtime: Runtime):
    code = runtime.new_code()
    with runtime.Session() as db:
        with pytest.raises(api.EnrollmentUnavailable):
            api.redeem_and_enroll(db, runtime.engine, code=code, device_name=DEVICE_NAME,
                              hostname=HOSTNAME, software_version=VERSION, key_ring=None,
                              actor_user_id=runtime.actor_id)
    assert runtime.rows("receiver_devices") == []


# ---------------------------------------------------------------------------
# Enrolling must not create more shared secrets
# ---------------------------------------------------------------------------
def test_enrolling_never_issues_a_new_legacy_store_token(runtime: Runtime):
    """The shared per-Store token is what Devices exist to replace. Minting or
    rotating one during enrolment would quietly extend the thing being retired."""
    with runtime.Session() as db:
        before = {
            store.id: store.receiver_token
            for store in db.query(Store).all()
        }

    runtime.enrol(runtime.new_code())
    runtime.enrol(runtime.new_code(), name="second device")

    with runtime.Session() as db:
        after = {store.id: store.receiver_token for store in db.query(Store).all()}

    assert after == before, "enrolment changed a Store's legacy receiver_token"
    assert len(after) == 2, "enrolment created or removed a Store"


def test_a_device_credential_is_not_the_stores_legacy_token(runtime: Runtime):
    credential = runtime.enrol(runtime.new_code()).take_raw_credential()
    with runtime.Session() as db:
        tokens = {store.receiver_token for store in db.query(Store).all()}
    assert credential not in tokens
    assert credential.startswith("echocast_rcv_v"), "the two formats must stay distinguishable"


# ---------------------------------------------------------------------------
# Device administration
# ---------------------------------------------------------------------------
def test_devices_are_listed_per_store_without_any_secret(runtime: Runtime):
    runtime.enrol(runtime.new_code())
    devices = api.list_devices(runtime.engine, store_id=runtime.store_id)

    assert len(devices) == 1
    device = devices[0]
    assert device["display_name"] == DEVICE_NAME
    assert device["status"] == "active"
    rendered = " ".join(str(value) for value in device.values())
    assert "echocast_rcv_v" not in rendered
    assert "hmac-sha256" not in rendered
    for secret_key in ("token_hash", "raw_credential", "secret", "credential"):
        assert secret_key not in device


def test_one_stores_devices_are_not_listed_for_another(runtime: Runtime):
    runtime.enrol(runtime.new_code())
    assert api.list_devices(runtime.engine, store_id=runtime.other_store_id) == []


def test_a_device_can_be_read_by_public_id(runtime: Runtime):
    public_id = runtime.enrol(runtime.new_code()).device_public_id
    device = api.read_device(runtime.engine, public_id=public_id)
    assert device["public_id"] == public_id
    assert device["store_id"] == runtime.store_id


def test_an_unknown_device_is_a_named_refusal(runtime: Runtime):
    with pytest.raises(api.DeviceNotFound):
        api.read_device(runtime.engine, public_id="00000000-0000-4000-8000-000000000000")


def test_disabling_a_device_marks_it_and_leaves_the_store_active(runtime: Runtime):
    public_id = runtime.enrol(runtime.new_code()).device_public_id
    api.disable_device(runtime.engine, public_id=public_id)

    assert api.read_device(runtime.engine, public_id=public_id)["status"] == "disabled"
    with runtime.Session() as db:
        assert db.query(Store).filter(Store.id == runtime.store_id).one().is_active is True


def test_revoking_one_device_leaves_the_other_active(runtime: Runtime):
    first = runtime.enrol(runtime.new_code(), name="primary").device_public_id
    second = runtime.enrol(runtime.new_code(), name="standby").device_public_id

    api.revoke_device(runtime.engine, public_id=first)

    assert api.read_device(runtime.engine, public_id=first)["status"] == "retired"
    assert api.read_device(runtime.engine, public_id=second)["status"] == "active"


def test_revoking_a_device_does_not_disable_its_store(runtime: Runtime):
    public_id = runtime.enrol(runtime.new_code()).device_public_id
    api.revoke_device(runtime.engine, public_id=public_id)
    with runtime.Session() as db:
        assert db.query(Store).filter(Store.id == runtime.store_id).one().is_active is True


def test_disabling_is_idempotent(runtime: Runtime):
    public_id = runtime.enrol(runtime.new_code()).device_public_id
    api.disable_device(runtime.engine, public_id=public_id)
    api.disable_device(runtime.engine, public_id=public_id)
    assert api.read_device(runtime.engine, public_id=public_id)["status"] == "disabled"


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(runtime: Runtime):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    runtime.enrol(runtime.new_code())
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()
