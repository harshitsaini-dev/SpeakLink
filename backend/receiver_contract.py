"""Pure receiver acknowledgement schemas and status transition functions.

This module is intentionally independent of FastAPI, WebSockets, SQLAlchemy,
and the runtime connection manager. It defines the contract only.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


PROTOCOL_VERSION = "1.0"
HEARTBEAT_INTERVAL_SECONDS = 5
STALE_AFTER_SECONDS = 15
OFFLINE_AFTER_SECONDS = 30
MAX_DEDUPLICATION_IDS = 1024


class ConnectionState(str, Enum):
    OFFLINE = "OFFLINE"
    CONNECTED = "CONNECTED"
    NETWORK_ERROR = "NETWORK_ERROR"


class ReadinessState(str, Enum):
    UNKNOWN = "UNKNOWN"
    READY = "READY"
    DEVICE_ERROR = "DEVICE_ERROR"


class PlaybackState(str, Enum):
    STOPPED = "STOPPED"
    AUDIO_RECEIVING = "AUDIO_RECEIVING"
    PLAYBACK_CONFIRMED = "PLAYBACK_CONFIRMED"
    PLAYBACK_ERROR = "PLAYBACK_ERROR"


class AcousticState(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    SPEAKER_VERIFIED = "SPEAKER_VERIFIED"


class ReceiverContractError(Exception):
    """Base class for explicit receiver contract rejections."""


class InvalidTransitionError(ReceiverContractError):
    """Raised when an acknowledgement would skip or violate the state model."""


class DuplicateMessageError(ReceiverContractError):
    """Raised when a message identifier has already been processed."""


class NonMonotonicSequenceError(ReceiverContractError):
    """Raised when a receiver sequence does not strictly increase."""


class WrongSessionError(ReceiverContractError):
    """Raised when a session-scoped acknowledgement targets another session."""


class InvalidServerTimestampError(ReceiverContractError):
    """Raised when a server timestamp is naive, non-UTC, or moves backwards."""


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _require_server_utc(value: datetime, field_name: str) -> datetime:
    if not _is_utc(value):
        raise InvalidServerTimestampError(f"{field_name} must be timezone-aware UTC")
    return value


def _reject_control_characters(value: str) -> str:
    if not value.isprintable():
        raise ValueError("text must not contain control characters")
    return value


class AcknowledgementBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    message_id: UUID
    occurred_at: datetime
    sequence: int = Field(ge=0)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_utc(cls, value: datetime) -> datetime:
        if not _is_utc(value):
            raise ValueError("occurred_at must be timezone-aware UTC")
        return value


class ReceiverCapabilities(BaseModel):
    """What this Receiver build can actually do beyond the 1.0 baseline.

    Optional, and absent from every Receiver built before audio control
    existed. That absence is the compatibility signal: no capabilities block
    means a Receiver that will play audio perfectly well and ignore a
    ``set_audio_control`` it does not understand, so HQ must not send it one
    and must not present its Stores as controllable. Defaulting these to True
    would invert that and make every old Store look controllable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_volume: bool = False
    output_mute: bool = False
    #: WHY output_volume is false, when it is.
    #:
    #: "Not supported by this Receiver" was true for two completely different
    #: situations and only one of them was the Receiver's fault: a build that
    #: predates master control, and a current build whose Store has simply not
    #: re-selected its audio output since upgrading. The first needs a new
    #: Store Kit; the second needs thirty seconds in Store Setup. Telling an
    #: operator the wrong one sends them to rebuild software that was already
    #: correct.
    #:
    #: Defaults to "unknown" so an older Receiver - which omits the whole
    #: capabilities block - keeps reading as genuinely unsupported rather than
    #: inheriting a friendlier explanation it never earned.
    output_control_status: Literal[
        "ready", "needs_output_selection", "unavailable", "unknown"
    ] = "unknown"


class ReceiverReadyAcknowledgement(AcknowledgementBase):
    type: Literal["receiver_ready"]
    software_checks_passed: Literal[True]
    output_device_checks_passed: Literal[True]
    #: None means "an older Receiver that never mentioned capabilities" - a
    #: different fact from "a Receiver that reported it cannot do this", and
    #: the UI is allowed to say so.
    capabilities: ReceiverCapabilities | None = None


class SessionAcknowledgement(AcknowledgementBase):
    session_id: int = Field(gt=0)


class AudioReceivingAcknowledgement(SessionAcknowledgement):
    type: Literal["audio_receiving"]


class PlaybackConfirmedAcknowledgement(SessionAcknowledgement):
    type: Literal["playback_confirmed"]


class ErrorAcknowledgement(AcknowledgementBase):
    error_code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Z0-9][A-Z0-9_.-]*$")
    details: str = Field(min_length=1, max_length=512)
    recoverable: bool = False

    _validate_details = field_validator("details")(_reject_control_characters)


class PlaybackErrorAcknowledgement(ErrorAcknowledgement):
    type: Literal["playback_error"]
    session_id: int = Field(gt=0)


class DeviceErrorAcknowledgement(ErrorAcknowledgement):
    type: Literal["device_error"]


class StoppedAcknowledgement(SessionAcknowledgement):
    type: Literal["stopped"]
    reason: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        return _reject_control_characters(value) if value is not None else None


class HeartbeatAcknowledgement(AcknowledgementBase):
    type: Literal["heartbeat"]


class AudioControlAcknowledgement(SessionAcknowledgement):
    """What the Store ACTUALLY did with a ``set_audio_control`` command.

    The requested and applied values are both carried, and they are allowed to
    differ. "HQ sent 50" and "the Store is at 50" are separate facts, and the
    entire point of this message is that the operator sees the second one
    rather than inferring it from a successful WebSocket send.

    ``result`` distinguishes three genuinely different outcomes:

        applied      - the output level really changed
        unsupported  - this Receiver has no mechanism for it
        failed       - it tried and the device refused

    ``output_device`` is the selected device's stable selector, never a
    credential and never an identity: it is here so an operator can tell which
    endpoint a failure refers to when a Store has more than one.
    """

    type: Literal["audio_control"]
    command_id: int = Field(gt=0)
    requested_volume_percent: int = Field(ge=0, le=100)
    requested_muted: bool
    applied_volume_percent: int | None = Field(default=None, ge=0, le=100)
    applied_muted: bool | None = None
    result: Literal["applied", "unsupported", "failed"]
    output_device: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Z0-9][A-Z0-9_.-]*$"
    )
    details: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("details", "output_device")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return _reject_control_characters(value) if value is not None else None


ReceiverAcknowledgement = Annotated[
    Union[
        ReceiverReadyAcknowledgement,
        AudioReceivingAcknowledgement,
        PlaybackConfirmedAcknowledgement,
        PlaybackErrorAcknowledgement,
        DeviceErrorAcknowledgement,
        StoppedAcknowledgement,
        HeartbeatAcknowledgement,
        AudioControlAcknowledgement,
    ],
    Field(discriminator="type"),
]


class TrustedSpeakerVerifiedEvent(AcknowledgementBase):
    type: Literal["speaker_verified"]
    session_id: int = Field(gt=0)
    source: Literal["linkguard"]


_receiver_ack_adapter = TypeAdapter(ReceiverAcknowledgement)
_trusted_verifier_adapter = TypeAdapter(TrustedSpeakerVerifiedEvent)


def parse_receiver_ack(payload: object) -> ReceiverAcknowledgement:
    """Parse an ordinary receiver message; trusted-verifier events are excluded."""
    return _receiver_ack_adapter.validate_python(payload)


def parse_trusted_verifier_event(payload: object) -> TrustedSpeakerVerifiedEvent:
    """Parse the separate LinkGuard/acoustic-verifier message contract."""
    return _trusted_verifier_adapter.validate_python(payload)


@dataclass(frozen=True, slots=True)
class ReceiverSnapshot:
    connection: ConnectionState = ConnectionState.OFFLINE
    readiness: ReadinessState = ReadinessState.UNKNOWN
    playback: PlaybackState = PlaybackState.STOPPED
    acoustic: AcousticState = AcousticState.UNVERIFIED
    active_session_id: int | None = None
    last_received_at: datetime | None = None
    last_sequence: int | None = None
    seen_message_ids: tuple[UUID, ...] = ()
    seen_verifier_message_ids: tuple[UUID, ...] = ()
    requires_ready: bool = True
    #: Last reported capabilities, or None for a Receiver that has never
    #: mentioned them. Kept on the snapshot rather than looked up per request
    #: so HQ can answer "is this Store controllable" from the same state it
    #: already uses for online/ready, and cannot drift from it.
    capabilities: ReceiverCapabilities | None = None


def _reset_health(snapshot: ReceiverSnapshot) -> ReceiverSnapshot:
    return replace(
        snapshot,
        readiness=ReadinessState.UNKNOWN,
        playback=PlaybackState.STOPPED,
        acoustic=AcousticState.UNVERIFIED,
        requires_ready=True,
        capabilities=None,
    )


def mark_connected(snapshot: ReceiverSnapshot, received_at: datetime) -> ReceiverSnapshot:
    """Record server-authenticated WebSocket acceptance."""
    _require_server_utc(received_at, "received_at")
    if snapshot.connection not in {ConnectionState.OFFLINE, ConnectionState.NETWORK_ERROR}:
        raise InvalidTransitionError("connection can become CONNECTED only from OFFLINE or NETWORK_ERROR")

    result = replace(snapshot, connection=ConnectionState.CONNECTED, last_received_at=received_at)
    if snapshot.connection is ConnectionState.OFFLINE:
        result = replace(
            _reset_health(result),
            last_sequence=None,
            seen_message_ids=(),
            seen_verifier_message_ids=(),
        )
    return result


def mark_disconnected(snapshot: ReceiverSnapshot, received_at: datetime) -> ReceiverSnapshot:
    """Record a server-observed disconnect and clear all health claims."""
    _require_server_utc(received_at, "received_at")
    if snapshot.connection not in {ConnectionState.CONNECTED, ConnectionState.NETWORK_ERROR}:
        raise InvalidTransitionError("only a connected or stale receiver can become OFFLINE")
    return replace(
        _reset_health(snapshot),
        connection=ConnectionState.OFFLINE,
        last_received_at=received_at,
        active_session_id=None,
    )


def evaluate_freshness(snapshot: ReceiverSnapshot, now: datetime) -> ReceiverSnapshot:
    """Apply deterministic server-receipt-time stale and offline boundaries."""
    _require_server_utc(now, "now")
    if snapshot.connection is ConnectionState.OFFLINE:
        return snapshot
    if snapshot.last_received_at is None:
        raise InvalidServerTimestampError("connected snapshots require last_received_at")
    if now < snapshot.last_received_at:
        raise InvalidServerTimestampError("now must not precede last_received_at")

    age_seconds = (now - snapshot.last_received_at).total_seconds()
    if age_seconds >= OFFLINE_AFTER_SECONDS:
        return replace(
            _reset_health(snapshot),
            connection=ConnectionState.OFFLINE,
            active_session_id=None,
        )
    if age_seconds >= STALE_AFTER_SECONDS:
        return replace(_reset_health(snapshot), connection=ConnectionState.NETWORK_ERROR)
    return snapshot


def dispatch_command(snapshot: ReceiverSnapshot, command: Literal["play", "start"]) -> ReceiverSnapshot:
    """Represent command dispatch without claiming any receiver-side state."""
    if command not in {"play", "start"}:
        raise ValueError("command must be play or start")
    return snapshot


def activate_session(snapshot: ReceiverSnapshot, session_id: int) -> ReceiverSnapshot:
    """Attach the server-selected session without claiming receiver playback."""
    if session_id <= 0:
        raise ValueError("session_id must be positive")
    return dispatch_command(replace(snapshot, active_session_id=session_id), "play")


def _bounded_append(values: tuple[UUID, ...], value: UUID) -> tuple[UUID, ...]:
    return (*values, value)[-MAX_DEDUPLICATION_IDS:]


def _validate_receiver_order(snapshot: ReceiverSnapshot, ack: ReceiverAcknowledgement) -> None:
    if ack.message_id in snapshot.seen_message_ids:
        raise DuplicateMessageError("receiver message_id has already been processed")
    if snapshot.last_sequence is not None and ack.sequence <= snapshot.last_sequence:
        raise NonMonotonicSequenceError("receiver sequence must strictly increase")


def _validate_active_session(snapshot: ReceiverSnapshot, session_id: int) -> None:
    if snapshot.active_session_id != session_id:
        raise WrongSessionError("acknowledgement does not match the active session")


def apply_receiver_ack(
    snapshot: ReceiverSnapshot,
    ack: ReceiverAcknowledgement,
    received_at: datetime,
) -> ReceiverSnapshot:
    """Apply one validated ordinary-receiver acknowledgement to a snapshot."""
    _require_server_utc(received_at, "received_at")
    if snapshot.connection is ConnectionState.OFFLINE:
        raise InvalidTransitionError("an OFFLINE receiver cannot acknowledge messages")
    if snapshot.last_received_at is not None and received_at < snapshot.last_received_at:
        raise InvalidServerTimestampError("received_at must not move backwards")
    _validate_receiver_order(snapshot, ack)

    result = snapshot
    if result.connection is ConnectionState.NETWORK_ERROR:
        result = replace(_reset_health(result), connection=ConnectionState.CONNECTED)

    if isinstance(ack, HeartbeatAcknowledgement):
        pass
    elif isinstance(ack, ReceiverReadyAcknowledgement):
        if result.readiness not in {ReadinessState.UNKNOWN, ReadinessState.DEVICE_ERROR}:
            raise InvalidTransitionError("receiver_ready requires UNKNOWN or DEVICE_ERROR readiness")
        # Capabilities ride on receiver_ready because that is the message a
        # Receiver sends after it has finished checking what it can actually
        # do - including opening its output device. Anything reported earlier
        # would be a claim rather than a check.
        result = replace(result, readiness=ReadinessState.READY,
                         requires_ready=False, capabilities=ack.capabilities)
    elif isinstance(ack, DeviceErrorAcknowledgement):
        if result.readiness not in {ReadinessState.UNKNOWN, ReadinessState.READY}:
            raise InvalidTransitionError("device_error requires UNKNOWN or READY readiness")
        result = replace(result, readiness=ReadinessState.DEVICE_ERROR, requires_ready=True)
    elif isinstance(ack, AudioReceivingAcknowledgement):
        _validate_active_session(result, ack.session_id)
        if result.readiness is not ReadinessState.READY or result.requires_ready:
            raise InvalidTransitionError("audio_receiving requires a fresh READY acknowledgement")
        if result.playback is not PlaybackState.STOPPED:
            raise InvalidTransitionError("audio_receiving requires STOPPED playback")
        result = replace(result, playback=PlaybackState.AUDIO_RECEIVING)
    elif isinstance(ack, PlaybackConfirmedAcknowledgement):
        _validate_active_session(result, ack.session_id)
        if result.playback is not PlaybackState.AUDIO_RECEIVING:
            raise InvalidTransitionError("playback_confirmed requires AUDIO_RECEIVING")
        result = replace(result, playback=PlaybackState.PLAYBACK_CONFIRMED)
    elif isinstance(ack, PlaybackErrorAcknowledgement):
        _validate_active_session(result, ack.session_id)
        if result.playback not in {
            PlaybackState.AUDIO_RECEIVING,
            PlaybackState.PLAYBACK_CONFIRMED,
        }:
            raise InvalidTransitionError("playback_error requires an active playback state")
        result = replace(
            result,
            readiness=ReadinessState.UNKNOWN,
            playback=PlaybackState.PLAYBACK_ERROR,
            requires_ready=True,
        )
    elif isinstance(ack, StoppedAcknowledgement):
        _validate_active_session(result, ack.session_id)
        result = replace(result, playback=PlaybackState.STOPPED)
    elif isinstance(ack, AudioControlAcknowledgement):
        # Deliberately changes NO status. How loud a Store is playing is
        # orthogonal to whether it is connected, ready, or playing: a muted
        # Store is still AUDIO_RECEIVING and still PLAYBACK_CONFIRMED, because
        # it is still receiving and still processing the stream. Folding volume
        # into the status model would make "quiet" indistinguishable from
        # "broken" on the very screen an operator uses to tell them apart.
        #
        # The session check stays, so a command answered against a finished
        # broadcast is rejected here as it is everywhere else.
        _validate_active_session(result, ack.session_id)
    else:  # pragma: no cover - protected by the discriminated union
        raise InvalidTransitionError("unsupported receiver acknowledgement")

    return replace(
        result,
        last_received_at=received_at,
        last_sequence=ack.sequence,
        seen_message_ids=_bounded_append(result.seen_message_ids, ack.message_id),
    )


def apply_trusted_speaker_verified(
    snapshot: ReceiverSnapshot,
    event: TrustedSpeakerVerifiedEvent,
    received_at: datetime,
) -> ReceiverSnapshot:
    """Apply a trusted verifier event through a path unavailable to receiver parsing."""
    _require_server_utc(received_at, "received_at")
    if snapshot.connection is not ConnectionState.CONNECTED:
        raise InvalidTransitionError("speaker verification requires a connected receiver")
    _validate_active_session(snapshot, event.session_id)
    if event.message_id in snapshot.seen_verifier_message_ids:
        raise DuplicateMessageError("verifier message_id has already been processed")
    if snapshot.acoustic is not AcousticState.UNVERIFIED:
        raise InvalidTransitionError("speaker verification is already recorded")

    return replace(
        snapshot,
        acoustic=AcousticState.SPEAKER_VERIFIED,
        seen_verifier_message_ids=_bounded_append(
            snapshot.seen_verifier_message_ids,
            event.message_id,
        ),
    )
