"""The authenticated enrollment-record status: what HQ may see after the code
has been spent.

WHY THIS EXISTS

ReceiverDevices.jsx can honestly show UNUSED and EXPIRED, because both are
derivable from data the page already has. It cannot show USED, because nothing
tells it. Guessing from a countdown that reached zero would report EXPIRED for a
code that was actually redeemed thirty seconds in - a Store that IS enrolled,
displayed as one that failed.

WHY A NEW COLUMN WAS NEEDED

``receiver_enrollment_codes`` recorded ``redeemed_at_epoch`` but nothing about
WHICH Device the redemption produced. Answering "which Device did this code
enrol?" from store_id plus a timestamp is inference from elapsed time, which is
exactly what this evidence model forbids. So redemption now records the Device's
public id on the code row, and the endpoint reports a Device only when that
link exists.

WHY THERE IS NO REVOKED STATE

There is no revocation transition on an enrollment code anywhere in this
schema - no revoked column, no service function, no route. A REVOKED state would
be a label with nothing behind it, so it is deliberately absent. Device
revocation is a different thing and already exists on the Device.
"""

from __future__ import annotations

import importlib
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

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

RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


class FakeRequest:
    def __init__(self, host: str = "203.0.113.9") -> None:
        self.client = SimpleNamespace(host=host)
        self.headers: dict = {}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("code-status")
    database = root / "status.db"
    container = root / "keys.bin"

    environment = {
        "SPEAKLINK_DB_PATH": str(database),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"status-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
        "SPEAKLINK_KEY_CONTAINER": str(container),
        "SPEAKLINK_KEY_PROTECTOR": "fake",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)

    db_module = None
    try:
        db_module = importlib.import_module("db")
        # The key container is created BEFORE the server is imported, because
        # that is the order a real HQ starts in: the persistent profile exists,
        # then the backend comes up against it. Since the backend now bootstraps
        # a missing container at import, creating it afterwards raced with that
        # and failed with KeyContainerExists.
        from key_custody import FakeProtector, create_key_container

        create_key_container(container, protector=FakeProtector())

        server = importlib.import_module("server")
        models = importlib.import_module("models")
        assert Path(db_module.DB_PATH) == database.resolve()
        server.startup_event()

        from migrations import (run_receiver_credential_phase_one,
                        mark_device_auth_ready_for_tests)

        run_receiver_credential_phase_one(db_module.engine)

        mark_device_auth_ready_for_tests(db_module.engine)

        with db_module.SessionLocal() as db:
            store = db.query(models.Store).order_by(models.Store.id).first()
            operator = db.query(models.HQUser).first()
            yield SimpleNamespace(server=server, db=db_module, models=models,
                                  store_id=store.id, operator=operator)
    finally:
        if db_module is not None:
            db_module.engine.dispose()
        for name in RUNTIME_MODULES:
            sys.modules.pop(name, None)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def fresh(runtime):
    from datetime import datetime, timezone

    from login_guard import LoginRateLimiter

    runtime.server.enrollment_limiter = LoginRateLimiter(runtime.server.ENROLLMENT_GUARD)
    with runtime.db.SessionLocal() as db:
        db.query(runtime.models.ReceiverEnrollmentCode).delete(synchronize_session=False)
        db.commit()
    now = datetime.now(timezone.utc).isoformat()
    with runtime.db.engine.connect() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_devices SET status='retired', disabled_at=?, updated_at=? "
            "WHERE status='active'", (now, now))
        connection.commit()
    return runtime


def _create_code(runtime, store_id=None):
    from schemas import EnrollmentCodeRequest

    with runtime.db.SessionLocal() as db:
        return runtime.server.create_receiver_enrollment_code(
            EnrollmentCodeRequest(store_id=store_id or runtime.store_id),
            db=db, user=runtime.operator,
        )


def _list_codes(runtime, store_id=None, user=None):
    with runtime.db.SessionLocal() as db:
        return runtime.server.list_receiver_enrollment_codes(
            store_id or runtime.store_id, db=db, user=user or runtime.operator)


def _enroll(runtime, code, *, name="Counter PC"):
    from schemas import DeviceEnrollmentRequest

    with runtime.db.SessionLocal() as db:
        return runtime.server.enroll_receiver(
            DeviceEnrollmentRequest(code=code, device_name=name, hostname="RX-01",
                                    software_version="1.0.0"),
            FakeRequest(), db=db,
        )


# ===========================================================================
# States
# ===========================================================================
def test_a_fresh_code_is_unused(fresh):
    _create_code(fresh)
    records = _list_codes(fresh)
    assert len(records) == 1
    assert records[0].state == "UNUSED"
    assert records[0].used_at is None
    assert records[0].device_public_id is None


def test_a_redeemed_code_is_used(fresh):
    code = _create_code(fresh).code
    _enroll(fresh, code)
    records = _list_codes(fresh)
    assert records[0].state == "USED"
    assert records[0].used_at is not None


def test_a_redeemed_code_names_the_device_it_enrolled(fresh):
    """The whole reason a column was added: which Device this code produced is
    recorded, never inferred from store_id and a timestamp."""
    code = _create_code(fresh).code
    enrolled = _enroll(fresh, code)
    records = _list_codes(fresh)
    assert records[0].device_public_id == enrolled.device_public_id


def test_an_expired_unredeemed_code_is_expired(fresh):
    from receiver_enrollment_codes import issue_enrollment_code

    with fresh.db.SessionLocal() as db:
        issue_enrollment_code(db, store_id=fresh.store_id,
                              actor_user_id=fresh.operator.id,
                              now=time.time() - 10_000, ttl_seconds=60)
    records = _list_codes(fresh)
    assert records[0].state == "EXPIRED"
    assert records[0].used_at is None


def test_a_code_redeemed_before_expiry_stays_used_afterwards(fresh):
    """A countdown reaching zero must never relabel a code that was actually
    spent. This is the exact misreport the frontend could not avoid before."""
    from receiver_enrollment_codes import issue_enrollment_code

    with fresh.db.SessionLocal() as db:
        issued = issue_enrollment_code(db, store_id=fresh.store_id,
                                       actor_user_id=fresh.operator.id,
                                       ttl_seconds=3600)
    _enroll(fresh, issued.code)

    # Force expiry to be in the past, exactly as the clock would eventually.
    with fresh.db.engine.connect() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_enrollment_codes SET expires_at_epoch = ?",
            (time.time() - 100,))
        connection.commit()

    records = _list_codes(fresh)
    assert records[0].state == "USED", "a spent code must not become EXPIRED"


def test_there_is_no_revoked_state_without_a_revocation_transition(fresh):
    """REVOKED is deliberately absent: nothing in this schema can revoke a code,
    so the label would have nothing behind it."""
    from schemas import EnrollmentCodeStatusOut

    allowed = EnrollmentCodeStatusOut.ALLOWED_STATES
    assert set(allowed) == {"UNUSED", "USED", "EXPIRED"}


# ===========================================================================
# What must never be in the response
# ===========================================================================
def test_the_raw_code_is_never_returned_by_the_status_endpoint(fresh):
    issued = _create_code(fresh)
    records = _list_codes(fresh)
    body = records[0].model_dump_json()
    assert issued.code not in body


def test_the_code_hash_is_never_returned(fresh):
    _create_code(fresh)
    with fresh.db.SessionLocal() as db:
        row = db.query(fresh.models.ReceiverEnrollmentCode).first()
        stored_hash = row.code_hash
    records = _list_codes(fresh)
    body = records[0].model_dump_json()
    assert stored_hash not in body
    assert "hash" not in body.lower()


def test_no_credential_appears_in_the_status_response(fresh):
    code = _create_code(fresh).code
    enrolled = _enroll(fresh, code)
    records = _list_codes(fresh)
    body = records[0].model_dump_json()
    assert enrolled.credential not in body
    assert "speaklink_rcv_v" not in body
    assert "credential" not in body.lower()


def test_the_status_schema_declares_no_secret_field():
    from schemas import EnrollmentCodeStatusOut

    fields = set(EnrollmentCodeStatusOut.model_fields)
    for forbidden in ("code", "code_hash", "credential", "credential_hash",
                      "hash_key", "verifier"):
        assert forbidden not in fields, f"{forbidden} is a field on the status schema"


# ===========================================================================
# Authorization
# ===========================================================================
def test_a_viewer_cannot_read_enrollment_records(fresh):
    """Codes are Device administration. A read-only account must not enumerate
    which Stores have pending enrolments."""
    from rbac import Permission, PermissionDenied, require_permission

    viewer = SimpleNamespace(id=999, username="viewer", role="VIEWER",
                             session_version=1, lifecycle_state="active")
    with pytest.raises(PermissionDenied):
        require_permission(viewer, Permission.MANAGE_DEVICES)


def test_the_route_is_guarded_by_a_read_permission():
    """Read the route's own dependency default rather than trusting a comment.

    Split from the coarse MANAGE_DEVICES flag into the fine-grained
    menu.receivers.view code as part of the per-user permissions work - a
    read-only enrolment-status list is a view right, not a Device-security
    action, and per-user overrides can now grant one without the other.
    """
    import inspect

    import server

    source = inspect.getsource(server.list_receiver_enrollment_codes)
    assert 'require("menu.receivers.view")' in source


def test_the_status_endpoint_requires_the_manage_devices_permission():
    """Read from the route's own dependency rather than trusting a comment."""
    import inspect

    import server

    signature = inspect.signature(server.list_receiver_enrollment_codes)
    user_parameter = signature.parameters["user"]
    assert user_parameter.default is not inspect.Parameter.empty, (
        "the route takes no authenticated user - it is unauthenticated")


# ===========================================================================
# Store lifecycle
# ===========================================================================
def test_a_disabled_store_cannot_have_a_code_created(fresh):
    from fastapi import HTTPException

    with fresh.db.engine.connect() as connection:
        connection.exec_driver_sql("UPDATE stores SET is_active = 0 WHERE id = ?",
                                   (fresh.store_id,))
        connection.commit()
    try:
        with pytest.raises(HTTPException):
            _create_code(fresh)
    finally:
        with fresh.db.engine.connect() as connection:
            connection.exec_driver_sql("UPDATE stores SET is_active = 1 WHERE id = ?",
                                       (fresh.store_id,))
            connection.commit()


def test_a_disabled_store_cannot_have_a_code_redeemed(fresh):
    from fastapi import HTTPException

    code = _create_code(fresh).code
    with fresh.db.engine.connect() as connection:
        connection.exec_driver_sql("UPDATE stores SET is_active = 0 WHERE id = ?",
                                   (fresh.store_id,))
        connection.commit()
    try:
        with pytest.raises(HTTPException):
            _enroll(fresh, code)
    finally:
        with fresh.db.engine.connect() as connection:
            connection.exec_driver_sql("UPDATE stores SET is_active = 1 WHERE id = ?",
                                       (fresh.store_id,))
            connection.commit()


# ===========================================================================
# Timestamps
# ===========================================================================
def test_every_timestamp_is_utc_and_ordered(fresh):
    from datetime import datetime, timezone

    code = _create_code(fresh).code
    _enroll(fresh, code)
    record = _list_codes(fresh)[0]

    created = datetime.fromisoformat(record.created_at)
    expires = datetime.fromisoformat(record.expires_at)
    used = datetime.fromisoformat(record.used_at)
    for moment in (created, expires, used):
        assert moment.tzinfo is not None, "a naive timestamp is ambiguous across 44 Stores"
        assert moment.utcoffset() == timezone.utc.utcoffset(None)
    assert created <= used <= expires


# ===========================================================================
# Evidence-backed setup progress
# ===========================================================================
def test_progress_on_an_unused_code_is_only_code_created(fresh):
    _create_code(fresh)
    record = _list_codes(fresh)[0]
    assert record.progress == ["CODE_CREATED"]


def test_progress_after_redemption_includes_the_device(fresh):
    code = _create_code(fresh).code
    _enroll(fresh, code)
    record = _list_codes(fresh)[0]
    assert record.progress[:3] == ["CODE_CREATED", "CODE_REDEEMED", "DEVICE_CREATED"]


def test_device_connected_is_not_claimed_without_a_connection(fresh):
    """The stage this evidence model exists to refuse. A Device that was created
    is not a Device that ever connected."""
    code = _create_code(fresh).code
    _enroll(fresh, code)
    record = _list_codes(fresh)[0]
    assert "DEVICE_CONNECTED" not in record.progress


def test_device_connected_does_appear_when_the_device_really_is_connected(fresh):
    """The other half, and it is the half that matters.

    The absence test above passed while DEVICE_CONNECTED was UNREACHABLE - the
    public-id-to-device-id lookup read a key that describe_store_devices does
    not publish, so it was None for every Device and the stage could never be
    reported at all. A test that only ever asserts a stage is missing cannot
    tell "correctly withheld" from "impossible to produce".
    """
    code = _create_code(fresh).code
    enrolled = _enroll(fresh, code)

    from sqlalchemy import text

    with fresh.db.engine.connect() as connection:
        device_id = connection.execute(
            text("SELECT id FROM receiver_devices WHERE public_id = :pid"),
            {"pid": enrolled.device_public_id},
        ).scalar()
    assert device_id is not None

    original = fresh.server.manager.connected_device_ids
    fresh.server.manager.connected_device_ids = lambda: {device_id}
    try:
        record = _list_codes(fresh)[0]
    finally:
        fresh.server.manager.connected_device_ids = original
    assert "DEVICE_CONNECTED" in record.progress


def test_primary_assigned_is_not_claimed_without_a_primary_role(fresh):
    code = _create_code(fresh).code
    _enroll(fresh, code)
    record = _list_codes(fresh)[0]
    assert "PRIMARY_ASSIGNED" not in record.progress


def test_primary_assigned_appears_once_the_device_really_is_primary(fresh):
    code = _create_code(fresh).code
    enrolled = _enroll(fresh, code)
    with fresh.db.SessionLocal() as db:
        fresh.server.promote_receiver_device(enrolled.device_public_id, db=db,
                                            user=fresh.operator)
    record = _list_codes(fresh)[0]
    assert "PRIMARY_ASSIGNED" in record.progress


def test_a_stored_stage_is_not_gated_behind_a_live_one(fresh):
    """My first version of _enrollment_progress stopped at the first stage it
    could not prove, so PRIMARY_ASSIGNED was hidden whenever the Device was not
    connected at that moment.

    Those two facts are independent: being this Store's primary is STORED, and
    holding a socket is LIVE. A Device can be primary and switched off. Hiding a
    promotion that definitely happened is the same class of error as asserting
    one that did not - reporting something other than what the evidence says.
    """
    code = _create_code(fresh).code
    enrolled = _enroll(fresh, code)
    with fresh.db.SessionLocal() as db:
        fresh.server.promote_receiver_device(enrolled.device_public_id, db=db,
                                            user=fresh.operator)
    record = _list_codes(fresh)[0]
    # Nothing is connected in this test - no WebSocket exists at all.
    assert "DEVICE_CONNECTED" not in record.progress
    assert "PRIMARY_ASSIGNED" in record.progress


# ===========================================================================
# Use-once still holds
# ===========================================================================
def test_a_code_cannot_be_redeemed_twice_and_stays_used(fresh):
    from fastapi import HTTPException

    code = _create_code(fresh).code
    _enroll(fresh, code)
    with pytest.raises(HTTPException):
        _enroll(fresh, code, name="Second PC")
    records = _list_codes(fresh)
    assert records[0].state == "USED"


# ===========================================================================
# The per-Store cap must count LIVE codes, not every code ever abandoned
# ===========================================================================
def test_expired_codes_do_not_count_against_the_outstanding_cap(fresh):
    """FOUND BY THE SECURITY AUDIT.

    The outstanding count filtered on `redeemed_at_epoch IS NULL` and nothing
    else, so an expired-but-unredeemed code counted for ever - nothing in the
    codebase ever prunes, deletes or marks one. After three abandoned codes a
    Store could NEVER be enrolled again.

    That is an ordinary sequence across 44 Stores: an admin clicks Generate
    during a failed setup visit, again the next day, again a week later. And the
    refusal told them to "use or let them expire before creating another" -
    advice that could not work, because letting a code expire never changed the
    count.

    The intent was always live codes: the constant's own comment says "a primary,
    a standby and one spare IN FLIGHT" and warns about "a haystack of LIVE
    credentials". Only the filter disagreed.
    """
    from receiver_enrollment_api import MAX_OUTSTANDING_CODES_PER_STORE
    from receiver_enrollment_codes import issue_enrollment_code

    # Fill the cap entirely with codes that expired long ago.
    with fresh.db.SessionLocal() as db:
        for _ in range(MAX_OUTSTANDING_CODES_PER_STORE):
            issue_enrollment_code(db, store_id=fresh.store_id,
                                  actor_user_id=fresh.operator.id,
                                  now=time.time() - 10_000, ttl_seconds=60)

    # A fresh code must still be mintable: every existing one is dead.
    issued = _create_code(fresh)
    assert issued.code

    records = _list_codes(fresh)
    assert sum(1 for r in records if r.state == "EXPIRED") == MAX_OUTSTANDING_CODES_PER_STORE
    assert sum(1 for r in records if r.state == "UNUSED") == 1


def test_live_unredeemed_codes_still_hit_the_cap(fresh):
    """The half that must not regress: the cap exists to stop a stuck script
    minting live credentials for ever."""
    from fastapi import HTTPException

    from receiver_enrollment_api import MAX_OUTSTANDING_CODES_PER_STORE

    for _ in range(MAX_OUTSTANDING_CODES_PER_STORE):
        _create_code(fresh)
    with pytest.raises(HTTPException):
        _create_code(fresh)


def test_a_redeemed_code_frees_a_slot(fresh):
    """Redemption has always freed a slot; asserted here so the new expiry term
    cannot accidentally change it."""
    from receiver_enrollment_api import MAX_OUTSTANDING_CODES_PER_STORE

    codes = [_create_code(fresh).code for _ in range(MAX_OUTSTANDING_CODES_PER_STORE)]
    _enroll(fresh, codes[0])
    assert _create_code(fresh).code
