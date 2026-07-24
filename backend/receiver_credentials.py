"""Pure receiver credential lifecycle primitives.

This module has no database, FastAPI, WebSocket, environment, or logging side
effects. Callers must supply hash keys explicitly and must return raw
credentials only from controlled one-time enrollment or rotation responses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import hmac
import re
import secrets
from typing import Mapping


TOKEN_SECRET_BYTES = 32
PUBLIC_ID_BYTES = 12
MIN_HASH_KEY_BYTES = 32
MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE = 2
MAX_ROTATION_GRACE = timedelta(minutes=15)
MAX_AUDIT_REASON_LENGTH = 256

_TOKEN_PATTERN = re.compile(
    r"^echocast_rcv_v(?P<version>[1-9][0-9]*)\."
    r"(?P<public_id>rcv_[a-f0-9]{24})\."
    r"(?P<secret>[A-Za-z0-9_-]{43,128})$"
)
_HASH_PATTERN = re.compile(
    r"^hmac-sha256\$v(?P<key_version>[1-9][0-9]*)\$(?P<digest>[a-f0-9]{64})$"
)
_LEGACY_TOKEN_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_PUBLIC_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class ReceiverCredentialError(Exception):
    """Base class for pure credential lifecycle errors."""


class InvalidCredentialError(ReceiverCredentialError):
    """Raised when a raw credential does not use the versioned format."""


class UnsafeAuditPayloadError(ReceiverCredentialError):
    """Raised when audit metadata is unbounded, unstructured, or secret-like."""


def validate_active_receiver_device_count(active_device_count: int) -> None:
    """Enforce the approved per-Store active Receiver Device limit."""
    if isinstance(active_device_count, bool) or not isinstance(active_device_count, int):
        raise ValueError("active receiver device count must be an integer")
    if not 0 <= active_device_count <= MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE:
        raise ValueError("active receiver device count exceeds the approved limit")


@dataclass(frozen=True, slots=True, repr=False)
class IssuedReceiverCredential:
    raw_token: str
    public_id: str
    version: int
    secret: str

    def __repr__(self) -> str:
        return (
            "IssuedReceiverCredential("
            f"public_id={self.public_id!r}, version={self.version}, "
            "raw_token=<redacted>, secret=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ParsedReceiverCredential:
    public_id: str
    version: int
    secret: str

    def __repr__(self) -> str:
        return (
            "ParsedReceiverCredential("
            f"public_id={self.public_id!r}, version={self.version}, "
            "secret=<redacted>)"
        )


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def generate_receiver_credential(version: int = 1) -> IssuedReceiverCredential:
    """Generate a versioned credential with an independent non-secret public ID."""
    if version <= 0:
        raise ValueError("credential version must be positive")
    public_id = f"rcv_{secrets.token_hex(PUBLIC_ID_BYTES)}"
    secret = secrets.token_urlsafe(TOKEN_SECRET_BYTES)
    raw_token = f"echocast_rcv_v{version}.{public_id}.{secret}"
    return IssuedReceiverCredential(raw_token, public_id, version, secret)


def parse_receiver_token(raw_token: str) -> ParsedReceiverCredential:
    if not isinstance(raw_token, str):
        raise InvalidCredentialError("receiver credential must be text")
    match = _TOKEN_PATTERN.fullmatch(raw_token)
    if match is None:
        raise InvalidCredentialError("receiver credential format is invalid")
    return ParsedReceiverCredential(
        public_id=match.group("public_id"),
        version=int(match.group("version")),
        secret=match.group("secret"),
    )


def _require_hash_key(hash_key: bytes) -> bytes:
    if not isinstance(hash_key, bytes) or len(hash_key) < MIN_HASH_KEY_BYTES:
        raise ValueError(f"receiver hash key must contain at least {MIN_HASH_KEY_BYTES} bytes")
    return hash_key


def hash_receiver_token(raw_token: str, hash_key: bytes, *, key_version: int) -> str:
    """Create a version-tagged HMAC-SHA-256 digest for storage."""
    parse_receiver_token(raw_token)
    return _hash_token(raw_token, hash_key, key_version)


def _hash_token(raw_token: str, hash_key: bytes, key_version: int) -> str:
    _require_hash_key(hash_key)
    if key_version <= 0:
        raise ValueError("hash key version must be positive")
    digest = hmac.new(hash_key, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"hmac-sha256$v{key_version}${digest}"


def hash_legacy_receiver_token(
    legacy_token: str,
    hash_key: bytes,
    *,
    key_version: int,
) -> str:
    """Hash only the UUID-hex format used by the reviewed migration source."""
    if not isinstance(legacy_token, str) or _LEGACY_TOKEN_PATTERN.fullmatch(legacy_token) is None:
        raise InvalidCredentialError("legacy receiver credential format is invalid")
    return _hash_token(legacy_token, hash_key, key_version)


def verify_receiver_token(raw_token: str, encoded_hash: str, hash_key: bytes) -> bool:
    """Verify a candidate without exposing parsing or digest details."""
    _require_hash_key(hash_key)
    if not isinstance(raw_token, str) or not isinstance(encoded_hash, str):
        return False
    try:
        parse_receiver_token(raw_token)
    except InvalidCredentialError:
        return False
    return _verify_token_digest(raw_token, encoded_hash, hash_key)


def _verify_token_digest(raw_token: str, encoded_hash: str, hash_key: bytes) -> bool:
    hash_match = _HASH_PATTERN.fullmatch(encoded_hash)
    if hash_match is None:
        return False
    candidate = hmac.new(hash_key, raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(candidate, hash_match.group("digest"))


def verify_legacy_receiver_token(
    legacy_token: str,
    encoded_hash: str,
    hash_key: bytes,
) -> bool:
    """Verify a reviewed legacy UUID-hex credential during migration only."""
    _require_hash_key(hash_key)
    if not isinstance(legacy_token, str) or _LEGACY_TOKEN_PATTERN.fullmatch(legacy_token) is None:
        return False
    return _verify_token_digest(legacy_token, encoded_hash, hash_key)


@dataclass(frozen=True, slots=True)
class CredentialState:
    issued_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    replaced_at: datetime | None = None
    accept_until: datetime | None = None
    is_active: bool = True

    def __post_init__(self) -> None:
        _require_utc(self.issued_at, "issued_at")
        for field_name in ("expires_at", "revoked_at", "replaced_at", "accept_until"):
            value = getattr(self, field_name)
            if value is not None:
                _require_utc(value, field_name)
                if value < self.issued_at:
                    raise ValueError(f"{field_name} must not precede issued_at")
        if self.accept_until is not None and self.replaced_at is None:
            raise ValueError("accept_until requires replaced_at")
        if (
            self.accept_until is not None
            and self.replaced_at is not None
            and self.accept_until < self.replaced_at
        ):
            raise ValueError("accept_until must not precede replaced_at")


def credential_is_usable(state: CredentialState, now: datetime) -> bool:
    """Evaluate active, issuance, expiry, revocation, and replacement boundaries."""
    _require_utc(now, "now")
    if not state.is_active or now < state.issued_at:
        return False
    if state.expires_at is not None and now >= state.expires_at:
        return False
    if state.revoked_at is not None and now >= state.revoked_at:
        return False
    if state.replaced_at is not None:
        accept_until = state.accept_until or state.replaced_at
        if now >= accept_until:
            return False
    return True


@dataclass(frozen=True, slots=True)
class RotationPlan:
    previous_version: int
    new_version: int
    rotated_at: datetime
    previous_accept_until: datetime


def plan_rotation(
    current_version: int,
    rotated_at: datetime,
    grace_period: timedelta,
) -> RotationPlan:
    """Plan immediate invalidation or a bounded dual-credential grace window."""
    if current_version <= 0:
        raise ValueError("current credential version must be positive")
    _require_utc(rotated_at, "rotated_at")
    if grace_period < timedelta(0) or grace_period > MAX_ROTATION_GRACE:
        raise ValueError("rotation grace period is outside the allowed boundary")
    return RotationPlan(
        previous_version=current_version,
        new_version=current_version + 1,
        rotated_at=rotated_at,
        previous_accept_until=rotated_at + grace_period,
    )


_AUDIT_INTEGER_FIELDS = {
    "credential_version",
    "store_id",
    "actor_user_id",
}
_AUDIT_PUBLIC_ID_FIELDS = {
    "device_public_id",
    "credential_public_id",
    "replacement_credential_public_id",
    "correlation_id",
}
_AUDIT_TEXT_LIMITS = {
    "reason": MAX_AUDIT_REASON_LENGTH,
    "outcome": 32,
    "migration_phase": 32,
}
_AUDIT_ALLOWED_FIELDS = (
    _AUDIT_INTEGER_FIELDS | _AUDIT_PUBLIC_ID_FIELDS | set(_AUDIT_TEXT_LIMITS)
)


def _safe_printable(value: str, maximum: int, field_name: str) -> str:
    if not value or len(value) > maximum or not value.isprintable():
        raise UnsafeAuditPayloadError(f"{field_name} is invalid")
    return value


def sanitize_audit_payload(payload: Mapping[str, object]) -> dict[str, int | str]:
    """Copy only bounded, non-secret credential audit metadata."""
    if not isinstance(payload, Mapping):
        raise UnsafeAuditPayloadError("audit payload must be a mapping")
    unknown = set(payload) - _AUDIT_ALLOWED_FIELDS
    if unknown:
        raise UnsafeAuditPayloadError("audit payload contains a forbidden field")

    sanitized: dict[str, int | str] = {}
    for field_name, value in payload.items():
        if field_name in _AUDIT_INTEGER_FIELDS:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise UnsafeAuditPayloadError(f"{field_name} must be a positive integer")
            sanitized[field_name] = value
        elif field_name in _AUDIT_PUBLIC_ID_FIELDS:
            if not isinstance(value, str):
                raise UnsafeAuditPayloadError(f"{field_name} must be text")
            safe_value = _safe_printable(value, 64, field_name)
            if _PUBLIC_VALUE_PATTERN.fullmatch(safe_value) is None:
                raise UnsafeAuditPayloadError(f"{field_name} contains unsafe characters")
            sanitized[field_name] = safe_value
        else:
            if not isinstance(value, str):
                raise UnsafeAuditPayloadError(f"{field_name} must be text")
            sanitized[field_name] = _safe_printable(
                value,
                _AUDIT_TEXT_LIMITS[field_name],
                field_name,
            )
    return sanitized
