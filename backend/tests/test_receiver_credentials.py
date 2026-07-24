"""Pure tests for the proposed receiver credential lifecycle helpers."""
from datetime import datetime, timedelta, timezone

import pytest

from receiver_credentials import (
    MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE,
    MAX_ROTATION_GRACE,
    CredentialState,
    InvalidCredentialError,
    UnsafeAuditPayloadError,
    credential_is_usable,
    generate_receiver_credential,
    hash_legacy_receiver_token,
    hash_receiver_token,
    parse_receiver_token,
    plan_rotation,
    sanitize_audit_payload,
    verify_legacy_receiver_token,
    verify_receiver_token,
    validate_active_receiver_device_count,
)


UTC_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
HASH_KEY = b"receiver-test-hash-key-material-32-bytes-minimum"


def test_generation_uses_unique_public_ids_and_high_entropy_secrets():
    first = generate_receiver_credential()
    second = generate_receiver_credential()

    assert first.raw_token != second.raw_token
    assert first.public_id != second.public_id
    assert first.version == 1
    parsed = parse_receiver_token(first.raw_token)
    assert parsed.version == first.version
    assert parsed.public_id == first.public_id
    assert len(parsed.secret) >= 43
    assert first.public_id in first.raw_token


def test_credential_representation_is_redacted():
    issued = generate_receiver_credential()
    rendered = repr(issued)
    assert issued.raw_token not in rendered
    assert issued.secret not in rendered
    assert "<redacted>" in rendered
    assert issued.public_id in rendered


def test_hmac_hashing_and_constant_time_verification():
    issued = generate_receiver_credential()
    encoded_hash = hash_receiver_token(issued.raw_token, HASH_KEY, key_version=3)

    assert issued.raw_token not in encoded_hash
    assert issued.secret not in encoded_hash
    assert encoded_hash.startswith("hmac-sha256$v3$")
    assert verify_receiver_token(issued.raw_token, encoded_hash, HASH_KEY)
    assert not verify_receiver_token(issued.raw_token + "x", encoded_hash, HASH_KEY)
    assert not verify_receiver_token(issued.raw_token, "malformed", HASH_KEY)


def test_hash_key_must_be_strong_and_token_must_be_well_formed():
    issued = generate_receiver_credential()
    with pytest.raises(ValueError):
        hash_receiver_token(issued.raw_token, b"short", key_version=1)
    with pytest.raises(InvalidCredentialError):
        parse_receiver_token("not-a-receiver-token")


def test_legacy_uuid_hex_can_be_hashed_only_through_explicit_migration_helpers():
    legacy_token = "0123456789abcdef0123456789abcdef"
    encoded_hash = hash_legacy_receiver_token(legacy_token, HASH_KEY, key_version=3)
    assert legacy_token not in encoded_hash
    assert verify_legacy_receiver_token(legacy_token, encoded_hash, HASH_KEY)
    assert not verify_legacy_receiver_token("f" * 32, encoded_hash, HASH_KEY)
    with pytest.raises(InvalidCredentialError):
        hash_legacy_receiver_token("not-legacy-uuid-hex", HASH_KEY, key_version=3)


def test_expiry_boundary_and_non_expiring_policy_are_explicit():
    expiring = CredentialState(
        issued_at=UTC_NOW,
        expires_at=UTC_NOW + timedelta(days=90),
    )
    assert credential_is_usable(expiring, UTC_NOW + timedelta(days=89))
    assert not credential_is_usable(expiring, UTC_NOW + timedelta(days=90))

    non_expiring = CredentialState(issued_at=UTC_NOW, expires_at=None)
    assert credential_is_usable(non_expiring, UTC_NOW + timedelta(days=3650))


def test_revocation_and_inactive_state_take_effect_immediately():
    revoked = CredentialState(
        issued_at=UTC_NOW,
        revoked_at=UTC_NOW + timedelta(hours=1),
    )
    assert credential_is_usable(revoked, UTC_NOW + timedelta(minutes=59))
    assert not credential_is_usable(revoked, UTC_NOW + timedelta(hours=1))
    assert not credential_is_usable(
        CredentialState(issued_at=UTC_NOW, is_active=False),
        UTC_NOW,
    )


def test_replaced_credential_obeys_rotation_overlap_boundary():
    state = CredentialState(
        issued_at=UTC_NOW - timedelta(days=1),
        replaced_at=UTC_NOW,
        accept_until=UTC_NOW + timedelta(minutes=10),
    )
    assert credential_is_usable(state, UTC_NOW + timedelta(minutes=9, seconds=59))
    assert not credential_is_usable(state, UTC_NOW + timedelta(minutes=10))


def test_rotation_plan_versions_and_bounds_grace_period():
    immediate = plan_rotation(4, UTC_NOW, timedelta(0))
    assert immediate.previous_version == 4
    assert immediate.new_version == 5
    assert immediate.previous_accept_until == UTC_NOW

    grace = plan_rotation(4, UTC_NOW, timedelta(minutes=15))
    assert grace.previous_accept_until == UTC_NOW + timedelta(minutes=15)

    with pytest.raises(ValueError):
        plan_rotation(4, UTC_NOW, MAX_ROTATION_GRACE + timedelta(seconds=1))
    with pytest.raises(ValueError):
        plan_rotation(0, UTC_NOW, timedelta(0))


def test_approved_device_and_rotation_limits_are_enforced():
    assert MAX_ACTIVE_RECEIVER_DEVICES_PER_STORE == 2
    assert MAX_ROTATION_GRACE == timedelta(minutes=15)
    validate_active_receiver_device_count(0)
    validate_active_receiver_device_count(2)
    with pytest.raises(ValueError):
        validate_active_receiver_device_count(3)


def test_naive_or_non_utc_lifecycle_timestamps_are_rejected():
    with pytest.raises(ValueError):
        CredentialState(issued_at=datetime(2026, 7, 23, 12, 0))
    with pytest.raises(ValueError):
        plan_rotation(1, datetime(2026, 7, 23, 12, 0), timedelta(0))


def test_audit_payload_is_allowlisted_bounded_and_secret_free():
    sanitized = sanitize_audit_payload(
        {
            "device_public_id": "dev_1234567890abcdef",
            "credential_public_id": "rcv_1234567890abcdef12345678",
            "credential_version": 2,
            "store_id": 7,
            "actor_user_id": 3,
            "reason": "scheduled rotation",
            "outcome": "success",
        }
    )
    assert sanitized["credential_version"] == 2
    assert sanitized["reason"] == "scheduled rotation"

    for unsafe in (
        {"token": "secret"},
        {"authorization": "Bearer secret"},
        {"token_hash": "digest"},
        {"password": "secret"},
        {"details": "unstructured"},
    ):
        with pytest.raises(UnsafeAuditPayloadError):
            sanitize_audit_payload(unsafe)


def test_audit_payload_rejects_control_characters_and_oversized_reason():
    with pytest.raises(UnsafeAuditPayloadError):
        sanitize_audit_payload({"reason": "line one\nline two"})
    with pytest.raises(UnsafeAuditPayloadError):
        sanitize_audit_payload({"reason": "x" * 257})
