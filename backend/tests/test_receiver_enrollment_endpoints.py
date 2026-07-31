"""The enrolment HTTP surface: what it answers, and what it refuses to say.

The service layer is covered by test_receiver_enrollment_service.py. This file
is about the boundary - status codes, which failures are told apart and which
deliberately are not, and the rule that a credential leaves the server exactly
once and is never in a URL.

Starlette's TestClient needs httpx, which this project does not depend on, so
routes are called directly and the raised HTTPException is inspected. That
carries the same status, detail and headers FastAPI would serialise.
"""

from __future__ import annotations

import importlib
import os
import secrets
import sys
import tempfile
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

PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
# Only the modules that actually bind to SPEAKLINK_DB_PATH. db resolves the path
# once at import and models registers its tables on db.Base, so both must be
# fresh together or create_all builds nothing.
#
# receiver_enrollment_api, receiver_enrollment_codes, key_custody and
# login_guard are deliberately NOT reloaded. They take an engine or a session as
# an argument and hold no path state - and popping them gave every exception two
# class identities, so another test file's `pytest.raises(EnrollmentRefused)`
# silently stopped matching the class actually raised.
RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


class FakeRequest:
    def __init__(self, host: str = "203.0.113.9") -> None:
        self.client = SimpleNamespace(host=host)
        self.headers: dict = {}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    """A backend imported fresh against a migrated temporary database."""
    root = tmp_path_factory.mktemp("enrol-endpoints")
    database = root / "endpoints.db"
    container = root / "keys.bin"

    environment = {
        "SPEAKLINK_DB_PATH": str(database),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"enrol-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
        "SPEAKLINK_KEY_CONTAINER": str(container),
        "SPEAKLINK_KEY_PROTECTOR": "fake",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)

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
            yield SimpleNamespace(
                server=server, db=db_module, models=models,
                store_id=store.id, operator=operator, container=container,
            )
    finally:
        if "db_module" in dir():
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
    """A clean limiter and a clean code budget before each test.

    The outstanding-code bound is three per Store and it is deliberately real -
    the service tests prove it. These tests share one database, so without this
    reset the fourth test to mint a code would get a 409 from a limit that is
    working exactly as designed.
    """
    from login_guard import LoginRateLimiter

    runtime.server.enrollment_limiter = LoginRateLimiter(runtime.server.ENROLLMENT_GUARD)
    with runtime.db.SessionLocal() as db:
        db.query(runtime.models.ReceiverEnrollmentCode).filter(
            runtime.models.ReceiverEnrollmentCode.redeemed_at_epoch.is_(None)
        ).delete(synchronize_session=False)
        db.commit()

    # Retire Devices left by earlier tests. receiver_credentials caps a Store at
    # two ACTIVE Devices, which is also real and also has its own test - without
    # this, the third test to enrol would be refused by a limit doing its job.
    from datetime import datetime, timezone

    with runtime.db.engine.connect() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_devices SET status='retired', disabled_at=?, updated_at=? "
            "WHERE status='active'",
            (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    return runtime


def _create_code(runtime, store_id=None):
    from schemas import EnrollmentCodeRequest

    with runtime.db.SessionLocal() as db:
        return runtime.server.create_receiver_enrollment_code(
            EnrollmentCodeRequest(store_id=store_id or runtime.store_id),
            db=db,
            user=runtime.operator,
        )


def _enroll(runtime, code, *, name="Counter PC", host="203.0.113.9"):
    from fastapi import HTTPException
    from schemas import DeviceEnrollmentRequest

    with runtime.db.SessionLocal() as db:
        try:
            return runtime.server.enroll_receiver(
                DeviceEnrollmentRequest(
                    code=code, device_name=name, hostname="RX-01", software_version="0.9.0"
                ),
                FakeRequest(host=host),
                db=db,
            ), None
        except HTTPException as refusal:
            return None, refusal


# ---------------------------------------------------------------------------
# Creating a code
# ---------------------------------------------------------------------------
def test_an_administrator_receives_a_code_and_its_lifetime(fresh):
    response = _create_code(fresh)
    assert response.code
    assert response.store_id == fresh.store_id
    assert response.expires_in_seconds > 0


def test_the_code_is_not_written_to_the_system_log(fresh):
    response = _create_code(fresh)
    with fresh.db.SessionLocal() as db:
        messages = " ".join(row.message for row in db.query(fresh.models.SystemLog).all())
    assert response.code not in messages
    assert "enrollment_code_created" in messages


def test_an_unknown_store_is_a_client_error(fresh):
    from fastapi import HTTPException
    from schemas import EnrollmentCodeRequest

    with fresh.db.SessionLocal() as db:
        with pytest.raises(HTTPException) as raised:
            fresh.server.create_receiver_enrollment_code(
                EnrollmentCodeRequest(store_id=999999), db=db, user=fresh.operator
            )
    assert raised.value.status_code == 400


# ---------------------------------------------------------------------------
# Redeeming
# ---------------------------------------------------------------------------
def test_a_valid_code_returns_one_credential(fresh):
    response, refusal = _enroll(fresh, _create_code(fresh).code)
    assert refusal is None
    assert response.credential.startswith("speaklink_rcv_v")
    assert response.device_public_id
    assert response.store_id == fresh.store_id


def test_the_credential_is_absent_from_every_later_read(fresh):
    response, _ = _enroll(fresh, _create_code(fresh).code)
    credential = response.credential

    with fresh.db.SessionLocal() as db:
        device = fresh.server.read_receiver_device(
            response.device_public_id, db=db, user=fresh.operator
        )
        listed = fresh.server.list_receiver_devices(fresh.store_id, db=db, user=fresh.operator)

    assert credential not in device.model_dump_json()
    assert all(credential not in entry.model_dump_json() for entry in listed)
    for field in device.model_dump():
        assert "credential" not in field
        assert "hash" not in field


def test_the_credential_is_absent_from_the_system_log(fresh):
    response, _ = _enroll(fresh, _create_code(fresh).code)
    with fresh.db.SessionLocal() as db:
        messages = " ".join(row.message for row in db.query(fresh.models.SystemLog).all())
    assert response.credential not in messages
    assert "receiver_device_enrolled" in messages


@pytest.mark.parametrize("bad", ["not-a-real-code", "x" * 100])
def test_an_invalid_code_gets_one_generic_refusal(fresh, bad):
    _, refusal = _enroll(fresh, bad)
    assert refusal.status_code == 400
    assert refusal.detail == fresh.server.ENROLMENT_REFUSED


def test_a_reused_code_is_refused_identically_to_an_invented_one(fresh):
    """Telling them apart would let a caller map out which codes exist."""
    code = _create_code(fresh).code
    _enroll(fresh, code)
    _, reused = _enroll(fresh, code)
    _, invented = _enroll(fresh, "never-issued-at-all")

    assert reused.status_code == invented.status_code == 400
    assert reused.detail == invented.detail


def test_the_refusal_never_echoes_the_submitted_code(fresh):
    _, refusal = _enroll(fresh, "a-very-distinctive-invalid-code")
    assert "a-very-distinctive-invalid-code" not in str(refusal.detail)


def test_repeated_failures_are_rate_limited_with_retry_after(fresh):
    for _ in range(fresh.server.ENROLLMENT_GUARD.max_attempts):
        _enroll(fresh, "wrong")
    _, limited = _enroll(fresh, "wrong")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def test_a_successful_enrolment_clears_that_clients_budget(fresh):
    for _ in range(fresh.server.ENROLLMENT_GUARD.max_attempts - 1):
        _enroll(fresh, "wrong")
    response, refusal = _enroll(fresh, _create_code(fresh).code)
    assert refusal is None and response.credential
    # The budget was forgotten, so a fresh wrong attempt is a 400 not a 429.
    _, next_failure = _enroll(fresh, "wrong")
    assert next_failure.status_code == 400


# ---------------------------------------------------------------------------
# Failing closed is a server problem, not a bad code
# ---------------------------------------------------------------------------
def test_a_missing_key_container_answers_503_not_400(fresh, monkeypatch):
    monkeypatch.setattr(fresh.server, "receiver_key_ring", lambda: None)
    _, refusal = _enroll(fresh, _create_code(fresh).code)
    assert refusal.status_code == 503
    assert refusal.detail != fresh.server.ENROLMENT_REFUSED


# ---------------------------------------------------------------------------
# Device administration
# ---------------------------------------------------------------------------
def test_a_device_can_be_disabled_and_revoked_independently(fresh):
    first, _ = _enroll(fresh, _create_code(fresh).code, name="primary")
    second, _ = _enroll(fresh, _create_code(fresh).code, name="standby")

    with fresh.db.SessionLocal() as db:
        disabled = fresh.server.disable_receiver_device(
            first.device_public_id, db=db, user=fresh.operator
        )
        other = fresh.server.read_receiver_device(
            second.device_public_id, db=db, user=fresh.operator
        )
    assert disabled.status == "disabled"
    assert other.status == "active"

    with fresh.db.SessionLocal() as db:
        revoked = fresh.server.revoke_receiver_device(
            second.device_public_id, db=db, user=fresh.operator
        )
        store = db.query(fresh.models.Store).filter(
            fresh.models.Store.id == fresh.store_id
        ).one()
    assert revoked.status == "retired"
    assert store.is_active is True, "revoking a Device disabled its Store"


def test_an_unknown_device_is_a_404(fresh):
    from fastapi import HTTPException

    with fresh.db.SessionLocal() as db:
        with pytest.raises(HTTPException) as raised:
            fresh.server.read_receiver_device(
                "00000000-0000-4000-8000-000000000000", db=db, user=fresh.operator
            )
    assert raised.value.status_code == 404


def test_administrative_routes_require_authentication(fresh):
    """Every device route depends on get_current_user; the enrolment route
    deliberately does not, because the code is what proves the caller."""
    import inspect

    for name in (
        "create_receiver_enrollment_code", "list_receiver_devices",
        "read_receiver_device", "disable_receiver_device", "revoke_receiver_device",
    ):
        signature = inspect.signature(getattr(fresh.server, name))
        assert "user" in signature.parameters, f"{name} is not authenticated"

    assert "user" not in inspect.signature(fresh.server.enroll_receiver).parameters


def test_no_enrolment_route_takes_a_credential_from_the_url():
    import re

    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for match in re.finditer(
        r'@api\.(?:get|post)\("(/receiver-devices[^"]*)"\)\s*\n(?:def|async def) \w+\((.*?)\):',
        source, re.DOTALL,
    ):
        signature = match.group(2)
        for secret in ("code", "credential", "token"):
            assert not re.search(rf"\b{secret}\b\s*:\s*str\s*=\s*Query", signature), (
                f"{match.group(1)} accepts {secret} from the query string"
            )


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(fresh):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    _enroll(fresh, _create_code(fresh).code)
    assert metadata() == before
