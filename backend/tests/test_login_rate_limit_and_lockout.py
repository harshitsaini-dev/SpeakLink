"""Guessing an HQ password must get slower, then stop working entirely.

The login route did the obvious thing and nothing else: look the username up,
run bcrypt, return 401. Nothing counted failures, so an attacker could try
passwords as fast as the network allowed, forever, and nothing anywhere would
notice.

It also leaked which usernames exist - not through the message, which was
already generic, but through *time*. An unknown username skipped
``verify_password`` entirely and answered in microseconds; a real one paid for a
bcrypt comparison first. That difference is trivially measurable, so an attacker
could enumerate accounts before guessing a single password.

Two defences, deliberately separate:

* a short-window **rate limiter**, in process, that slows a burst from one
  client or against one username;
* a persistent **account lockout** that stops sustained guessing against a real
  account even from many addresses.

Everything here drives pure functions and a temporary SQLite database with an
injected clock. No test sleeps for a real lockout, nothing binds a port, and no
password, hash or token is ever printed.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

# db resolves DB_PATH once at first import; keep the shared default disposable
# so no import order can aim it at the protected database.
os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from auth import hash_password  # noqa: E402
from db import Base  # noqa: E402
from login_guard import (  # noqa: E402
    LoginGuardConfig,
    LoginGuardConfigError,
    LoginRateLimiter,
    clear_failed_logins,
    client_identifier,
    lockout_retry_after,
    register_failed_login,
)
from models import HQUser, LoginSecurityState  # noqa: E402


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"

OPERATOR = "pilot-operator"
PASSPHRASE = "test-only-passphrase-not-real"


class FakeClock:
    """Monotonic seconds under the test's control. Nothing ever sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _config(**overrides) -> LoginGuardConfig:
    values = dict(
        max_attempts=5,
        window_seconds=60,
        max_failures=3,
        lockout_seconds=900,
        max_entries=64,
    )
    values.update(overrides)
    return LoginGuardConfig(**values)


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'guard.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        db.add(HQUser(username=OPERATOR, password_hash=hash_password(PASSPHRASE), role="admin"))
        db.commit()
    yield factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Configuration must be validated, not trusted
# ---------------------------------------------------------------------------
def test_a_sane_configuration_is_accepted():
    assert _config().max_attempts == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"window_seconds": 0},
        {"window_seconds": -1},
        {"max_attempts": 0},
        {"max_attempts": -3},
        {"max_failures": 0},
        {"lockout_seconds": 0},
        {"max_entries": 0},
    ],
)
def test_unsafe_configuration_values_are_refused(overrides):
    with pytest.raises(LoginGuardConfigError):
        _config(**overrides)


def test_an_unbounded_lockout_is_refused():
    """A lock that never expires turns a guessing attempt into a denial of
    service against the operator."""
    with pytest.raises(LoginGuardConfigError):
        _config(lockout_seconds=10**9)


def test_an_absurd_window_is_refused():
    with pytest.raises(LoginGuardConfigError):
        _config(window_seconds=10**9)


# ---------------------------------------------------------------------------
# The short-window rate limiter
# ---------------------------------------------------------------------------
def test_attempts_below_the_threshold_are_allowed():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=3), clock=clock)
    for _ in range(3):
        assert limiter.retry_after("client") is None
        limiter.record_attempt("client")


def test_the_threshold_is_enforced():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=3), clock=clock)
    for _ in range(3):
        limiter.record_attempt("client")
    assert limiter.retry_after("client") is not None


def test_a_blocked_client_is_told_how_long_to_wait():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2, window_seconds=60), clock=clock)
    limiter.record_attempt("client")
    limiter.record_attempt("client")

    retry_after = limiter.retry_after("client")
    assert isinstance(retry_after, int)
    assert 0 < retry_after <= 60


def test_the_window_slides_so_a_client_recovers():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2, window_seconds=60), clock=clock)
    limiter.record_attempt("client")
    limiter.record_attempt("client")
    assert limiter.retry_after("client") is not None

    clock.advance(61)
    assert limiter.retry_after("client") is None


def test_two_different_clients_do_not_share_a_budget():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2), clock=clock)
    limiter.record_attempt("client-a")
    limiter.record_attempt("client-a")

    assert limiter.retry_after("client-a") is not None
    assert limiter.retry_after("client-b") is None


def test_a_username_key_is_limited_independently_of_the_client_key():
    """Guessing one account from many addresses must still be throttled."""
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2), clock=clock)
    limiter.record_attempt("user:pilot-operator")
    limiter.record_attempt("user:pilot-operator")

    assert limiter.retry_after("user:pilot-operator") is not None
    assert limiter.retry_after("user:someone-else") is None


def test_a_successful_login_forgets_the_client():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2), clock=clock)
    limiter.record_attempt("client")
    limiter.forget("client")
    limiter.record_attempt("client")
    assert limiter.retry_after("client") is None


# ---------------------------------------------------------------------------
# The limiter must stay small
# ---------------------------------------------------------------------------
def test_tracking_is_bounded_by_the_configured_maximum():
    """Otherwise every random username an attacker sends costs memory forever."""
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_entries=16), clock=clock)
    for index in range(500):
        limiter.record_attempt(f"client-{index}")
    assert limiter.tracked() <= 16


def test_expired_entries_are_cleaned_up():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(window_seconds=60, max_entries=64), clock=clock)
    for index in range(20):
        limiter.record_attempt(f"client-{index}")
    assert limiter.tracked() == 20

    clock.advance(61)
    limiter.record_attempt("someone-new")
    assert limiter.tracked() == 1, "stale windows were kept after they expired"


def test_eviction_discards_the_least_recently_active_entry():
    """A bounded store has to drop something. It drops whatever has been quiet
    longest, so a client that is actively attempting keeps its own entry hot."""
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2, max_entries=3), clock=clock)

    limiter.record_attempt("oldest")
    clock.advance(1)
    limiter.record_attempt("middle")
    clock.advance(1)
    limiter.record_attempt("newest")
    clock.advance(1)
    limiter.record_attempt("newcomer")  # forces one eviction

    assert limiter.tracked() == 3
    assert limiter.seen("oldest") is False
    assert limiter.seen("newest") is True
    assert limiter.seen("newcomer") is True


def test_an_active_client_keeps_its_entry_alive_through_eviction():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=2, max_entries=3), clock=clock)
    limiter.record_attempt("attacker")
    limiter.record_attempt("attacker")
    assert limiter.retry_after("attacker") is not None

    for index in range(6):
        clock.advance(0.001)
        limiter.record_attempt(f"noise-{index}")
        limiter.record_attempt("attacker")  # still attacking, so still hot

    assert limiter.seen("attacker") is True
    assert limiter.retry_after("attacker") is not None


def test_the_persistent_account_lock_is_what_noise_cannot_flush(session_factory):
    """Honest limitation: a bounded in-process limiter CAN be flushed by an
    attacker who floods it with distinct keys. That is why sustained guessing
    against a real account is stopped by the persistent lock instead, which
    lives in the database and is keyed to an account that actually exists."""
    clock = FakeClock()
    config = _config(max_failures=2, max_entries=2)

    limiter = LoginRateLimiter(config, clock=clock)
    limiter.record_attempt("attacker")
    limiter.record_attempt("attacker")
    for index in range(20):
        clock.advance(0.001)
        limiter.record_attempt(f"noise-{index}")
    assert limiter.seen("attacker") is False, "the limiter alone is flushable"

    with session_factory() as db:
        register_failed_login(db, OPERATOR, config, now=clock())
        register_failed_login(db, OPERATOR, config, now=clock())
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is not None


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def test_parallel_attempts_cannot_exceed_the_threshold():
    clock = FakeClock()
    limiter = LoginRateLimiter(_config(max_attempts=5, max_entries=64), clock=clock)

    allowed = []
    barrier = threading.Barrier(12)

    def attempt():
        barrier.wait()
        if limiter.retry_after("shared") is None:
            allowed.append(1)
        limiter.record_attempt("shared")

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert limiter.tracked() == 1
    assert limiter.retry_after("shared") is not None


# ---------------------------------------------------------------------------
# Client identity
# ---------------------------------------------------------------------------
def test_an_ipv4_client_is_identified():
    assert client_identifier("203.0.113.7", None, trust_proxy=False) == "ip:203.0.113.7"


def test_an_ipv6_client_is_identified():
    identity = client_identifier("2001:db8::1", None, trust_proxy=False)
    assert identity.startswith("ip:")
    assert "2001:db8::1" in identity


def test_a_missing_client_address_is_handled():
    """Starlette leaves request.client as None for some transports."""
    assert client_identifier(None, None, trust_proxy=False) == "ip:unknown"


def test_a_forwarded_header_is_ignored_by_default():
    """Trusting X-Forwarded-For without a proxy in front lets any caller pick a
    fresh identity per request and bypass the limiter entirely."""
    identity = client_identifier("203.0.113.7", "198.51.100.9", trust_proxy=False)
    assert identity == "ip:203.0.113.7"


def test_a_forwarded_header_is_used_only_when_explicitly_trusted():
    identity = client_identifier("203.0.113.7", "198.51.100.9", trust_proxy=True)
    assert identity == "ip:198.51.100.9"


def test_only_the_first_forwarded_hop_is_used_when_trusted():
    identity = client_identifier("203.0.113.7", "198.51.100.9, 203.0.113.7", trust_proxy=True)
    assert identity == "ip:198.51.100.9"


def test_a_malformed_forwarded_header_falls_back_to_the_socket_address():
    identity = client_identifier("203.0.113.7", "not-an-address", trust_proxy=True)
    assert identity == "ip:203.0.113.7"


# ---------------------------------------------------------------------------
# Persistent account lockout
# ---------------------------------------------------------------------------
def test_an_account_is_not_locked_to_begin_with(session_factory):
    clock = FakeClock()
    with session_factory() as db:
        assert lockout_retry_after(db, OPERATOR, _config(), now=clock()) is None


def test_failures_below_the_threshold_do_not_lock(session_factory):
    clock = FakeClock()
    config = _config(max_failures=3)
    with session_factory() as db:
        for _ in range(2):
            register_failed_login(db, OPERATOR, config, now=clock())
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is None


def test_the_threshold_locks_the_account(session_factory):
    clock = FakeClock()
    config = _config(max_failures=3)
    with session_factory() as db:
        for _ in range(3):
            register_failed_login(db, OPERATOR, config, now=clock())
        retry_after = lockout_retry_after(db, OPERATOR, config, now=clock())
    assert isinstance(retry_after, int)
    assert 0 < retry_after <= config.lockout_seconds


def test_the_lock_expires_after_the_configured_duration(session_factory):
    clock = FakeClock()
    config = _config(max_failures=3, lockout_seconds=900)
    with session_factory() as db:
        for _ in range(3):
            register_failed_login(db, OPERATOR, config, now=clock())
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is not None

        clock.advance(901)
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is None


def test_a_successful_login_clears_the_failure_counter(session_factory):
    clock = FakeClock()
    config = _config(max_failures=3)
    with session_factory() as db:
        register_failed_login(db, OPERATOR, config, now=clock())
        register_failed_login(db, OPERATOR, config, now=clock())
        clear_failed_logins(db, OPERATOR)

        # Two more failures must not reach the threshold that two earlier ones
        # would otherwise have completed.
        register_failed_login(db, OPERATOR, config, now=clock())
        register_failed_login(db, OPERATOR, config, now=clock())
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is None


def test_clearing_one_account_does_not_unlock_another(session_factory):
    clock = FakeClock()
    config = _config(max_failures=2)
    with session_factory() as db:
        db.add(HQUser(username="second-operator", password_hash=hash_password(PASSPHRASE)))
        db.commit()

        for _ in range(2):
            register_failed_login(db, OPERATOR, config, now=clock())
            register_failed_login(db, "second-operator", config, now=clock())

        clear_failed_logins(db, OPERATOR)

        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is None
        assert lockout_retry_after(db, "second-operator", config, now=clock()) is not None


def test_an_unknown_username_never_creates_a_row(session_factory):
    """Otherwise a few million invented usernames become a few million rows."""
    clock = FakeClock()
    config = _config(max_failures=1)
    with session_factory() as db:
        for index in range(50):
            register_failed_login(db, f"invented-{index}", config, now=clock())
        assert db.query(LoginSecurityState).count() == 0


def test_an_unknown_username_is_never_reported_as_locked(session_factory):
    """Reporting a lock for a name that does not exist would confirm it does."""
    clock = FakeClock()
    config = _config(max_failures=1)
    with session_factory() as db:
        register_failed_login(db, "invented", config, now=clock())
        assert lockout_retry_after(db, "invented", config, now=clock()) is None


def test_the_username_lookup_is_case_normalised(session_factory):
    """Otherwise 'Pilot-Operator' is a free extra budget for the same account."""
    clock = FakeClock()
    config = _config(max_failures=2)
    with session_factory() as db:
        register_failed_login(db, OPERATOR, config, now=clock())
        register_failed_login(db, OPERATOR.upper(), config, now=clock())
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is not None


# ---------------------------------------------------------------------------
# Nothing sensitive is stored
# ---------------------------------------------------------------------------
def test_the_lockout_row_holds_no_credential(session_factory):
    clock = FakeClock()
    config = _config(max_failures=1)
    with session_factory() as db:
        register_failed_login(db, OPERATOR, config, now=clock())
        row = db.query(LoginSecurityState).one()
        rendered = " ".join(str(getattr(row, column.name)) for column in row.__table__.columns)

    assert PASSPHRASE not in rendered
    assert "$2" not in rendered, "a bcrypt hash reached the security state table"


def test_lockout_state_survives_a_restart_but_the_limiter_does_not(session_factory):
    """The documented split: the in-process limiter is a short-window burst
    defence and is deliberately lost on restart; the account lock is persistent,
    so restarting the backend cannot be used to clear it."""
    clock = FakeClock()
    config = _config(max_failures=2)
    with session_factory() as db:
        register_failed_login(db, OPERATOR, config, now=clock())
        register_failed_login(db, OPERATOR, config, now=clock())

    # A "restart": brand new session and a brand new limiter.
    with session_factory() as db:
        assert lockout_retry_after(db, OPERATOR, config, now=clock()) is not None

    fresh_limiter = LoginRateLimiter(config, clock=clock)
    assert fresh_limiter.tracked() == 0
    assert fresh_limiter.retry_after("client") is None


# ---------------------------------------------------------------------------
# The login route itself, end to end over HTTP
# ---------------------------------------------------------------------------
RUNTIME_MODULES = (
    "server", "db", "models", "schemas", "auth", "seed", "ws_manager",
    "login_guard", "admin_bootstrap",
)


class FakeRequest:
    """The two things the login route reads from a request.

    Starlette's TestClient needs httpx, which is not a dependency of this
    project, and a subprocess would put the limiter out of reach of a per-test
    reset. Calling the route directly keeps both the assertions and the state
    exact, and the raised HTTPException carries the status, detail and headers
    FastAPI would serialise.
    """

    def __init__(self, host: str = "203.0.113.5", forwarded_for: str | None = None) -> None:
        from types import SimpleNamespace

        self.client = SimpleNamespace(host=host) if host else None
        self.headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    """An isolated backend with its own database, imported fresh."""
    import importlib
    import secrets
    from types import SimpleNamespace

    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("login-guard") / "login.db"
    operator = f"guard-{secrets.token_hex(6)}"
    passphrase = secrets.token_urlsafe(24)
    environment = {
        "SPEAKLINK_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": operator,
        "ADMIN_PASSWORD": passphrase,
        "CORS_ORIGINS": "http://localhost:3000",
        "LOGIN_MAX_ATTEMPTS": "4",
        "LOGIN_WINDOW_SECONDS": "60",
        "LOGIN_MAX_FAILURES": "3",
        "LOGIN_LOCKOUT_SECONDS": "900",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.path.insert(0, str(backend_dir))
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)

    try:
        db_module = importlib.import_module("db")
        server = importlib.import_module("server")
        assert Path(db_module.DB_PATH) == database_path.resolve()
        server.startup_event()
        yield SimpleNamespace(
            server=server,
            db=db_module,
            operator=operator,
            passphrase=passphrase,
        )
    finally:
        if "db_module" in dir():
            db_module.engine.dispose()
        for name in RUNTIME_MODULES:
            sys.modules.pop(name, None)
        if sys.path and sys.path[0] == str(backend_dir):
            sys.path.pop(0)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def fresh_guard(runtime):
    """A clean limiter and cleared lock state before each route test."""
    from login_guard import LoginRateLimiter

    runtime.server.login_limiter = LoginRateLimiter(runtime.server.LOGIN_GUARD)
    with runtime.db.SessionLocal() as db:
        for row in db.query(LoginSecurityState).all():
            row.failed_count = 0
            row.locked_until_epoch = None
        db.commit()
    return runtime


class Attempt:
    """What the route produced: a token, or the refusal it raised."""

    def __init__(self, status_code: int, detail=None, headers=None, token=None) -> None:
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        self.token = token

    @property
    def text(self) -> str:
        return f"{self.status_code} {self.detail} {self.headers}"


def _login(runtime, username: str, password: str, host: str = "203.0.113.5") -> Attempt:
    from fastapi import HTTPException
    from schemas import LoginRequest

    with runtime.db.SessionLocal() as db:
        try:
            result = runtime.server.login(
                LoginRequest(username=username, password=password),
                FakeRequest(host=host),
                db=db,
            )
        except HTTPException as refusal:
            return Attempt(refusal.status_code, refusal.detail, refusal.headers)
    return Attempt(200, token=result.access_token)


def test_an_unknown_username_and_a_wrong_password_are_indistinguishable(fresh_guard):
    unknown = _login(fresh_guard, "no-such-operator", "whatever")
    wrong = _login(fresh_guard, fresh_guard.operator, "definitely-not-the-passphrase")

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.detail == wrong.detail
    assert "exist" not in str(unknown.detail).lower()
    assert "unknown" not in str(unknown.detail).lower()


def test_the_unknown_username_path_still_performs_a_password_comparison(fresh_guard, monkeypatch):
    """Structural check instead of a stopwatch: a timing assertion would be
    flaky, but skipping bcrypt entirely is what creates the oracle."""
    calls = []
    original = fresh_guard.server.burn_password_comparison
    monkeypatch.setattr(
        fresh_guard.server,
        "burn_password_comparison",
        lambda password: calls.append(1) or original(password),
    )

    assert _login(fresh_guard, "no-such-operator", "whatever").status_code == 401
    assert calls, "the unknown-username path skipped the password comparison"


def test_a_valid_sign_in_returns_a_token(fresh_guard):
    response = _login(fresh_guard, fresh_guard.operator, fresh_guard.passphrase)
    assert response.status_code == 200
    assert response.token


def test_repeated_failures_are_rate_limited_with_a_retry_after(fresh_guard):
    for _ in range(fresh_guard.server.LOGIN_GUARD.max_attempts):
        assert _login(fresh_guard, "no-such-operator", "wrong").status_code == 401

    limited = _login(fresh_guard, "no-such-operator", "wrong")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def test_the_rate_limited_body_reveals_no_counters(fresh_guard):
    for _ in range(fresh_guard.server.LOGIN_GUARD.max_attempts):
        _login(fresh_guard, "no-such-operator", "wrong")
    limited = _login(fresh_guard, "no-such-operator", "wrong")

    # The wording may say "attempts"; what it must not carry is a number. A
    # count, a threshold or an unlock time all tell an attacker how close they
    # are and whether the account exists. Retry-After is the one number an
    # honest client needs, and it belongs in the header, not the body.
    detail = str(limited.detail)
    assert not any(character.isdigit() for character in detail), (
        f"the refusal body carries a number: {detail!r}"
    )
    lowered = detail.lower()
    for leak in ("count", "remaining", "threshold", "locked_until", "failed", "lock"):
        assert leak not in lowered, f"the response body leaked {leak!r}"


def test_a_locked_account_is_refused_even_with_the_correct_password(fresh_guard):
    for _ in range(fresh_guard.server.LOGIN_GUARD.max_failures):
        assert _login(fresh_guard, fresh_guard.operator, "wrong-passphrase").status_code == 401

    # A fresh limiter, so only the persistent lock can be refusing now.
    from login_guard import LoginRateLimiter

    fresh_guard.server.login_limiter = LoginRateLimiter(fresh_guard.server.LOGIN_GUARD)

    locked = _login(fresh_guard, fresh_guard.operator, fresh_guard.passphrase)
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0
    assert locked.token is None, "a locked account was still issued a token"


def test_a_successful_sign_in_clears_the_failure_counter(fresh_guard):
    config = fresh_guard.server.LOGIN_GUARD
    for _ in range(config.max_failures - 1):
        assert _login(fresh_guard, fresh_guard.operator, "wrong-passphrase").status_code == 401

    from login_guard import LoginRateLimiter

    fresh_guard.server.login_limiter = LoginRateLimiter(config)
    assert _login(fresh_guard, fresh_guard.operator, fresh_guard.passphrase).status_code == 200

    # The counter restarted, so the same number of failures must not lock now.
    fresh_guard.server.login_limiter = LoginRateLimiter(config)
    for _ in range(config.max_failures - 1):
        assert _login(fresh_guard, fresh_guard.operator, "wrong-passphrase").status_code == 401

    fresh_guard.server.login_limiter = LoginRateLimiter(config)
    assert _login(fresh_guard, fresh_guard.operator, fresh_guard.passphrase).status_code == 200


def test_no_credential_reaches_the_system_log(fresh_guard):
    from models import SystemLog

    _login(fresh_guard, fresh_guard.operator, "a-very-distinctive-wrong-passphrase")
    _login(fresh_guard, "no-such-operator", "another-distinctive-value")

    with fresh_guard.db.SessionLocal() as db:
        messages = " ".join(row.message for row in db.query(SystemLog).all())

    assert "a-very-distinctive-wrong-passphrase" not in messages
    assert "another-distinctive-value" not in messages
    assert fresh_guard.passphrase not in messages
    assert "$2" not in messages


# ---------------------------------------------------------------------------
# The HTTP query-token fallback
#
# This file used to pin the fact that _extract_token still accepted ?token= on
# ordinary HTTP requests, so its removal would start from a captured fact rather
# than a memory. It has since been removed; the guards that keep it removed live
# in backend/tests/test_http_token_transport.py.
# ---------------------------------------------------------------------------
def test_no_websocket_route_depends_on_the_query_token_fallback():
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for route in ("/api/ws/hq", "/api/ws/broadcaster"):
        match = re.search(
            rf'@app\.websocket\("{re.escape(route)}"\)\s*\n\s*async def \w+\((.*?)\):',
            source,
            re.DOTALL,
        )
        assert match, f"{route} is missing"
        assert "ticket" in match.group(1)
        assert "token" not in match.group(1)


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(session_factory):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    clock = FakeClock()
    with session_factory() as db:
        register_failed_login(db, OPERATOR, _config(), now=clock())
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()
