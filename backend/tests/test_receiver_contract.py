"""Pure unit tests for the receiver status and acknowledgement contract."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from receiver_contract import (
    HEARTBEAT_INTERVAL_SECONDS,
    OFFLINE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    AcousticState,
    ConnectionState,
    DuplicateMessageError,
    InvalidTransitionError,
    NonMonotonicSequenceError,
    PlaybackState,
    ReadinessState,
    ReceiverSnapshot,
    WrongSessionError,
    apply_receiver_ack,
    apply_trusted_speaker_verified,
    dispatch_command,
    evaluate_freshness,
    mark_connected,
    mark_disconnected,
    parse_receiver_ack,
    parse_trusted_verifier_event,
)


UTC_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def payload(message_type, sequence=0, **extra):
    data = {
        "protocol_version": "1.0",
        "type": message_type,
        "message_id": str(uuid4()),
        "occurred_at": UTC_NOW.isoformat().replace("+00:00", "Z"),
        "sequence": sequence,
    }
    data.update(extra)
    return data


def connected_snapshot(session_id=42):
    snapshot = mark_connected(ReceiverSnapshot(), UTC_NOW)
    return replace(snapshot, active_session_id=session_id)


def apply(snapshot, message_type, sequence, received_offset=1, **extra):
    ack = parse_receiver_ack(payload(message_type, sequence, **extra))
    received_at = UTC_NOW + timedelta(seconds=received_offset)
    return apply_receiver_ack(snapshot, ack, received_at)


@pytest.mark.parametrize(
    ("message_type", "extra"),
    [
        (
            "receiver_ready",
            {"software_checks_passed": True, "output_device_checks_passed": True},
        ),
        ("audio_receiving", {"session_id": 42}),
        ("playback_confirmed", {"session_id": 42}),
        (
            "playback_error",
            {
                "session_id": 42,
                "error_code": "PIPELINE_FAILURE",
                "details": "Decoder could not process the frame.",
                "recoverable": True,
            },
        ),
        (
            "device_error",
            {
                "error_code": "OUTPUT_DEVICE_MISSING",
                "details": "Configured output device is unavailable.",
                "recoverable": True,
            },
        ),
        ("stopped", {"session_id": 42, "reason": "normal_stop"}),
        ("heartbeat", {}),
    ],
)
def test_every_supported_acknowledgement_parses(message_type, extra):
    acknowledgement = parse_receiver_ack(payload(message_type, **extra))
    assert acknowledgement.type == message_type
    assert acknowledgement.occurred_at.tzinfo == timezone.utc


def test_unknown_message_type_is_rejected():
    with pytest.raises(ValidationError):
        parse_receiver_ack(payload("made_up_event"))


def test_unexpected_extra_field_is_rejected():
    with pytest.raises(ValidationError):
        parse_receiver_ack(payload("heartbeat", receiver_token="must-not-be-accepted"))


def test_naive_timestamp_is_rejected():
    data = payload("heartbeat")
    data["occurred_at"] = "2026-07-23T12:00:00"
    with pytest.raises(ValidationError):
        parse_receiver_ack(data)


def test_non_utc_timestamp_is_rejected():
    data = payload("heartbeat")
    data["occurred_at"] = "2026-07-23T17:30:00+05:30"
    with pytest.raises(ValidationError):
        parse_receiver_ack(data)


@pytest.mark.parametrize(
    "message_type",
    ["audio_receiving", "playback_confirmed", "playback_error", "stopped"],
)
def test_media_acknowledgements_require_session_id(message_type):
    data = payload(message_type)
    if message_type == "playback_error":
        data.update(error_code="PIPELINE_FAILURE", details="Failure")
    with pytest.raises(ValidationError):
        parse_receiver_ack(data)


def test_error_code_and_details_are_bounded_and_sanitized():
    with pytest.raises(ValidationError):
        parse_receiver_ack(
            payload(
                "device_error",
                error_code="bad code with spaces",
                details="safe",
            )
        )
    with pytest.raises(ValidationError):
        parse_receiver_ack(
            payload(
                "device_error",
                error_code="DEVICE_FAILURE",
                details="unsafe\nlog injection",
            )
        )
    with pytest.raises(ValidationError):
        parse_receiver_ack(
            payload(
                "device_error",
                error_code="DEVICE_FAILURE",
                details="x" * 513,
            )
        )


def test_duplicate_message_id_is_rejected():
    snapshot = connected_snapshot()
    data = payload(
        "receiver_ready",
        sequence=1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    acknowledgement = parse_receiver_ack(data)
    snapshot = apply_receiver_ack(snapshot, acknowledgement, UTC_NOW + timedelta(seconds=1))
    with pytest.raises(DuplicateMessageError):
        apply_receiver_ack(snapshot, acknowledgement, UTC_NOW + timedelta(seconds=2))


def test_sequence_must_increase_monotonically_per_connection():
    snapshot = connected_snapshot()
    snapshot = apply(snapshot, "heartbeat", 4)
    with pytest.raises(NonMonotonicSequenceError):
        apply(snapshot, "heartbeat", 4, received_offset=2)
    with pytest.raises(NonMonotonicSequenceError):
        apply(snapshot, "heartbeat", 3, received_offset=2)


def test_sequence_must_be_non_negative():
    with pytest.raises(ValidationError):
        parse_receiver_ack(payload("heartbeat", sequence=-1))


def test_connection_allowed_transitions_and_disconnect_reset():
    snapshot = ReceiverSnapshot()
    snapshot = mark_connected(snapshot, UTC_NOW)
    assert snapshot.connection is ConnectionState.CONNECTED

    snapshot = evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=15))
    assert snapshot.connection is ConnectionState.NETWORK_ERROR

    snapshot = apply(snapshot, "heartbeat", 1, received_offset=16)
    assert snapshot.connection is ConnectionState.CONNECTED

    snapshot = mark_disconnected(snapshot, UTC_NOW + timedelta(seconds=17))
    assert snapshot.connection is ConnectionState.OFFLINE
    assert snapshot.readiness is ReadinessState.UNKNOWN
    assert snapshot.playback is PlaybackState.STOPPED
    assert snapshot.acoustic is AcousticState.UNVERIFIED


def test_network_error_can_transition_to_offline():
    snapshot = mark_connected(ReceiverSnapshot(), UTC_NOW)
    snapshot = evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=15))
    assert snapshot.connection is ConnectionState.NETWORK_ERROR
    snapshot = evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=30))
    assert snapshot.connection is ConnectionState.OFFLINE


def test_readiness_allowed_transitions():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    assert snapshot.readiness is ReadinessState.READY

    snapshot = apply(
        snapshot,
        "device_error",
        2,
        received_offset=2,
        error_code="OUTPUT_DEVICE_MISSING",
        details="Output device missing.",
        recoverable=True,
    )
    assert snapshot.readiness is ReadinessState.DEVICE_ERROR

    snapshot = apply(
        snapshot,
        "receiver_ready",
        3,
        received_offset=3,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    assert snapshot.readiness is ReadinessState.READY


def test_unknown_readiness_can_transition_directly_to_device_error():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "device_error",
        1,
        error_code="OUTPUT_DEVICE_MISSING",
        details="Output device missing.",
    )
    assert snapshot.readiness is ReadinessState.DEVICE_ERROR


def test_playback_allowed_transitions():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    snapshot = apply(snapshot, "audio_receiving", 2, received_offset=2, session_id=42)
    assert snapshot.playback is PlaybackState.AUDIO_RECEIVING

    snapshot = apply(snapshot, "playback_confirmed", 3, received_offset=3, session_id=42)
    assert snapshot.playback is PlaybackState.PLAYBACK_CONFIRMED

    snapshot = apply(
        snapshot,
        "playback_error",
        4,
        received_offset=4,
        session_id=42,
        error_code="PIPELINE_FAILURE",
        details="Playback pipeline failed.",
        recoverable=True,
    )
    assert snapshot.playback is PlaybackState.PLAYBACK_ERROR

    snapshot = apply(snapshot, "stopped", 5, received_offset=5, session_id=42)
    assert snapshot.playback is PlaybackState.STOPPED


def test_playback_error_is_allowed_directly_from_audio_receiving():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    snapshot = apply(snapshot, "audio_receiving", 2, received_offset=2, session_id=42)
    snapshot = apply(
        snapshot,
        "playback_error",
        3,
        received_offset=3,
        session_id=42,
        error_code="BUFFER_FAILURE",
        details="Audio buffer failed.",
    )
    assert snapshot.playback is PlaybackState.PLAYBACK_ERROR


@pytest.mark.parametrize(
    "playback_state",
    [
        PlaybackState.STOPPED,
        PlaybackState.AUDIO_RECEIVING,
        PlaybackState.PLAYBACK_CONFIRMED,
        PlaybackState.PLAYBACK_ERROR,
    ],
)
def test_matching_stopped_ack_is_allowed_from_every_playback_state(playback_state):
    snapshot = replace(connected_snapshot(), playback=playback_state)
    snapshot = apply(snapshot, "stopped", 1, session_id=42)
    assert snapshot.playback is PlaybackState.STOPPED


def test_invalid_skipped_playback_transition_is_rejected():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    with pytest.raises(InvalidTransitionError):
        apply(snapshot, "playback_confirmed", 2, received_offset=2, session_id=42)


def test_play_command_dispatch_causes_no_state_transition():
    snapshot = connected_snapshot()
    before_axes = (
        snapshot.connection,
        snapshot.readiness,
        snapshot.playback,
        snapshot.acoustic,
    )
    result = dispatch_command(snapshot, "play")
    after_axes = (
        result.connection,
        result.readiness,
        result.playback,
        result.acoustic,
    )
    assert after_axes == before_axes


def test_heartbeat_refreshes_only_connection_freshness():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    snapshot = replace(
        snapshot,
        playback=PlaybackState.PLAYBACK_CONFIRMED,
        acoustic=AcousticState.SPEAKER_VERIFIED,
    )
    before = (snapshot.readiness, snapshot.playback, snapshot.acoustic)
    snapshot = apply(snapshot, "heartbeat", 2, received_offset=2)
    assert (snapshot.readiness, snapshot.playback, snapshot.acoustic) == before


def test_stale_and_offline_boundaries_are_exact():
    assert HEARTBEAT_INTERVAL_SECONDS == 5
    assert STALE_AFTER_SECONDS == 15
    assert OFFLINE_AFTER_SECONDS == 30

    snapshot = mark_connected(ReceiverSnapshot(), UTC_NOW)
    assert evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=14, microseconds=999999)).connection is ConnectionState.CONNECTED
    assert evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=15)).connection is ConnectionState.NETWORK_ERROR
    assert evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=29, microseconds=999999)).connection is ConnectionState.NETWORK_ERROR
    assert evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=30)).connection is ConnectionState.OFFLINE


def test_stale_recovery_does_not_restore_health_states():
    snapshot = mark_connected(ReceiverSnapshot(), UTC_NOW)
    snapshot = replace(
        snapshot,
        readiness=ReadinessState.READY,
        playback=PlaybackState.PLAYBACK_CONFIRMED,
        acoustic=AcousticState.SPEAKER_VERIFIED,
    )
    snapshot = evaluate_freshness(snapshot, UTC_NOW + timedelta(seconds=15))
    assert snapshot.connection is ConnectionState.NETWORK_ERROR
    assert snapshot.readiness is ReadinessState.UNKNOWN
    assert snapshot.playback is PlaybackState.STOPPED
    assert snapshot.acoustic is AcousticState.UNVERIFIED

    snapshot = apply(snapshot, "heartbeat", 1, received_offset=16)
    assert snapshot.connection is ConnectionState.CONNECTED
    assert snapshot.readiness is ReadinessState.UNKNOWN
    assert snapshot.playback is PlaybackState.STOPPED
    assert snapshot.acoustic is AcousticState.UNVERIFIED


def test_receiver_originated_speaker_verified_is_rejected():
    with pytest.raises(ValidationError):
        parse_receiver_ack(payload("speaker_verified", session_id=42, source="echoguard"))


def test_trusted_speaker_verification_uses_separate_path():
    snapshot = connected_snapshot()
    event = parse_trusted_verifier_event(
        payload("speaker_verified", session_id=42, source="echoguard")
    )
    snapshot = apply_trusted_speaker_verified(
        snapshot,
        event,
        UTC_NOW + timedelta(seconds=1),
    )
    assert snapshot.acoustic is AcousticState.SPEAKER_VERIFIED


def test_wrong_session_acknowledgement_is_rejected():
    snapshot = connected_snapshot(session_id=42)
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    with pytest.raises(WrongSessionError):
        apply(snapshot, "audio_receiving", 2, received_offset=2, session_id=99)


def test_error_recovery_requires_fresh_receiver_ready():
    snapshot = connected_snapshot()
    snapshot = apply(
        snapshot,
        "receiver_ready",
        1,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    snapshot = apply(
        snapshot,
        "device_error",
        2,
        received_offset=2,
        error_code="OUTPUT_DEVICE_MISSING",
        details="Output device missing.",
    )
    snapshot = apply(snapshot, "heartbeat", 3, received_offset=3)
    assert snapshot.readiness is ReadinessState.DEVICE_ERROR

    with pytest.raises(InvalidTransitionError):
        apply(snapshot, "audio_receiving", 4, received_offset=4, session_id=42)

    snapshot = apply(
        snapshot,
        "receiver_ready",
        4,
        received_offset=4,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )
    snapshot = apply(snapshot, "audio_receiving", 5, received_offset=5, session_id=42)
    assert snapshot.playback is PlaybackState.AUDIO_RECEIVING
