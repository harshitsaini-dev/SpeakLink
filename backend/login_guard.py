"""Slow down, then stop, password guessing against the HQ login.

Two defences that fail in different ways, deliberately kept separate:

* :class:`LoginRateLimiter` - a short-window, in-process burst limiter keyed by
  client address and by username. It is cheap, needs no storage, and is lost on
  restart. It is also *flushable*: an attacker who floods it with distinct keys
  can evict their own entry, because the store is bounded on purpose.

* The persistent account lock - consecutive failures counted in the database
  against a username that really exists. It survives a restart, cannot be
  flushed by noise, and is what actually stops sustained guessing.

Neither stores a password, a hash, a token or a request body.

Scope, stated plainly: the limiter lives in one process. That is correct for
the current deployment, which runs exactly one Uvicorn worker because Receiver
connection state is already process-local. Running several workers, or several
machines, would give each its own limiter and multiply the effective burst
allowance - that deployment needs shared rate-limit storage, which this module
deliberately does not attempt.
"""

from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from models import HQUser, LoginSecurityState


# Bounds, not preferences. Anything outside these is a configuration mistake
# that would either disable the defence or turn it into a denial of service.
MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS = 1, 3600
MIN_ATTEMPTS, MAX_ATTEMPTS = 1, 1000
MIN_FAILURES, MAX_FAILURES = 1, 100
MIN_LOCKOUT_SECONDS, MAX_LOCKOUT_SECONDS = 1, 86400
MIN_ENTRIES, MAX_ENTRIES = 1, 1_000_000


class LoginGuardConfigError(ValueError):
    """The guard configuration would not actually guard anything."""


@dataclass(frozen=True)
class LoginGuardConfig:
    """How hard to guess before the door closes.

    ``max_attempts`` within ``window_seconds`` throttles a burst.
    ``max_failures`` consecutive failures locks a real account for
    ``lockout_seconds``. ``max_entries`` bounds the limiter's memory.
    """

    max_attempts: int = 10
    window_seconds: int = 60
    max_failures: int = 5
    lockout_seconds: int = 900
    max_entries: int = 4096

    def __post_init__(self) -> None:
        self._require("max_attempts", self.max_attempts, MIN_ATTEMPTS, MAX_ATTEMPTS)
        self._require("window_seconds", self.window_seconds, MIN_WINDOW_SECONDS, MAX_WINDOW_SECONDS)
        self._require("max_failures", self.max_failures, MIN_FAILURES, MAX_FAILURES)
        self._require(
            "lockout_seconds", self.lockout_seconds, MIN_LOCKOUT_SECONDS, MAX_LOCKOUT_SECONDS
        )
        self._require("max_entries", self.max_entries, MIN_ENTRIES, MAX_ENTRIES)

    @staticmethod
    def _require(name: str, value, low: int, high: int) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise LoginGuardConfigError(f"{name} must be a whole number")
        if value < low or value > high:
            raise LoginGuardConfigError(
                f"{name} must be between {low} and {high}; {value} would leave the "
                "login either unprotected or permanently unusable"
            )


ENVIRONMENT_KEYS = {
    "max_attempts": "LOGIN_MAX_ATTEMPTS",
    "window_seconds": "LOGIN_WINDOW_SECONDS",
    "max_failures": "LOGIN_MAX_FAILURES",
    "lockout_seconds": "LOGIN_LOCKOUT_SECONDS",
    "max_entries": "LOGIN_LIMITER_MAX_ENTRIES",
}
TRUST_PROXY_ENV = "TRUST_PROXY_HEADERS"


def config_from_environment(environment=None) -> LoginGuardConfig:
    """Build the configuration, refusing unusable values at startup.

    Defaults are deliberate rather than absent: an operator who sets nothing
    still gets a guarded login.
    """
    import os

    source = os.environ if environment is None else environment
    values = {}
    for field, key in ENVIRONMENT_KEYS.items():
        raw = source.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            values[field] = int(str(raw).strip())
        except ValueError:
            raise LoginGuardConfigError(
                f"{key} must be a whole number of "
                f"{'seconds' if key.endswith('SECONDS') else 'units'}"
            ) from None
    return LoginGuardConfig(**values)


def proxy_headers_trusted(environment=None) -> bool:
    """Off unless deliberately switched on. See :func:`client_identifier`."""
    import os

    source = os.environ if environment is None else environment
    return str(source.get(TRUST_PROXY_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


_DUMMY_HASH: str | None = None
_DUMMY_LOCK = threading.Lock()


def burn_password_comparison(password: str) -> None:
    """Spend the same time on an unknown username as on a real one.

    Without this, an unknown name skips bcrypt entirely and answers in
    microseconds while a real one pays for a full comparison. The message was
    always identical; the clock was not, and that is enough to enumerate
    accounts before guessing a single password.
    """
    global _DUMMY_HASH
    from auth import hash_password, verify_password

    if _DUMMY_HASH is None:
        with _DUMMY_LOCK:
            if _DUMMY_HASH is None:
                import secrets

                _DUMMY_HASH = hash_password(secrets.token_urlsafe(32))
    verify_password(password or "", _DUMMY_HASH)


def normalise_username(username: str) -> str:
    """One account, one key. Case and padding are not separate budgets."""
    return (username or "").strip().casefold()


def client_identifier(client_host, forwarded_for, trust_proxy: bool) -> str:
    """A stable key for the caller.

    X-Forwarded-For is ignored unless a proxy is explicitly trusted. Honouring
    it by default would let any caller invent a fresh identity per request and
    walk straight past the limiter.
    """
    if trust_proxy and forwarded_for:
        candidate = forwarded_for.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
            return f"ip:{candidate}"
        except ValueError:
            pass  # malformed header: fall back to the socket address

    if not client_host:
        return "ip:unknown"
    return f"ip:{client_host}"


class LoginRateLimiter:
    """Bounded sliding-window counter. Safe to call from several threads."""

    def __init__(self, config: LoginGuardConfig, clock=time.monotonic) -> None:
        self._config = config
        self._clock = clock
        self._lock = threading.Lock()
        # key -> list of attempt timestamps, ordered by last activity so the
        # quietest entry is the one evicted when the store is full.
        self._attempts: "OrderedDict[str, list[float]]" = OrderedDict()

    def _prune(self, now: float) -> None:
        cutoff = now - self._config.window_seconds
        for key in list(self._attempts):
            kept = [stamp for stamp in self._attempts[key] if stamp > cutoff]
            if kept:
                self._attempts[key] = kept
            else:
                del self._attempts[key]

    def retry_after(self, key: str) -> int | None:
        """Seconds to wait, or None when the caller may proceed."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            stamps = self._attempts.get(key)
            if not stamps or len(stamps) < self._config.max_attempts:
                return None
            # The window clears when the oldest attempt in it ages out.
            oldest = min(stamps)
            remaining = (oldest + self._config.window_seconds) - now
            return max(1, int(math.ceil(remaining)))

    def record_attempt(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            stamps = self._attempts.pop(key, [])
            stamps.append(now)
            # Re-inserting moves this key to the most-recently-active end.
            self._attempts[key] = stamps
            while len(self._attempts) > self._config.max_entries:
                self._attempts.popitem(last=False)

    def forget(self, key: str) -> None:
        """Called after a successful sign-in, so one good login clears the slate."""
        with self._lock:
            self._attempts.pop(key, None)

    def seen(self, key: str) -> bool:
        with self._lock:
            return key in self._attempts

    def tracked(self) -> int:
        with self._lock:
            self._prune(self._clock())
            return len(self._attempts)


# ---------------------------------------------------------------------------
# Persistent account lock
# ---------------------------------------------------------------------------
def _existing_username(db: Session, username: str) -> str | None:
    """The stored username for a real account, or None.

    Compared case-insensitively so that a differently-cased spelling is the
    same account rather than a fresh budget.
    """
    normalised = normalise_username(username)
    if not normalised:
        return None
    for user in db.query(HQUser).all():
        if normalise_username(user.username) == normalised:
            return normalised
    return None


def _state(db: Session, normalised: str) -> LoginSecurityState | None:
    return (
        db.query(LoginSecurityState)
        .filter(LoginSecurityState.username == normalised)
        .first()
    )


def lockout_retry_after(
    db: Session, username: str, config: LoginGuardConfig, now: float
) -> int | None:
    """Seconds remaining on an active lock, or None.

    Returns None for an unknown username: saying "locked" about a name that
    does not exist would confirm that it does.
    """
    normalised = _existing_username(db, username)
    if normalised is None:
        return None

    row = _state(db, normalised)
    if row is None or not row.locked_until_epoch:
        return None
    remaining = row.locked_until_epoch - now
    if remaining <= 0:
        return None
    return max(1, int(math.ceil(remaining)))


def register_failed_login(
    db: Session, username: str, config: LoginGuardConfig, now: float
) -> None:
    """Count one failure against a real account, locking it at the threshold.

    An unknown username is ignored entirely, so invented names can never become
    rows.
    """
    normalised = _existing_username(db, username)
    if normalised is None:
        return

    row = _state(db, normalised)
    if row is None:
        row = LoginSecurityState(username=normalised, failed_count=0)
        db.add(row)

    row.failed_count = (row.failed_count or 0) + 1
    row.last_failed_epoch = now
    if row.failed_count >= config.max_failures:
        row.locked_until_epoch = now + config.lockout_seconds
    db.commit()


def clear_failed_logins(db: Session, username: str) -> None:
    """A successful sign-in resets that account, and only that account."""
    normalised = _existing_username(db, username)
    if normalised is None:
        return
    row = _state(db, normalised)
    if row is None:
        return
    row.failed_count = 0
    row.locked_until_epoch = None
    db.commit()
