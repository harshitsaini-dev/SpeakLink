"""Control messages for the one-Store live-audio software pilot.

This module deliberately adds **only** the HQ/backend -> Receiver control
messages that the existing protocol was missing (``prepare``), plus validation
of the audio format descriptor and of individual binary audio chunks.

The Receiver -> backend direction is **not** redefined here. It continues to
use the frozen ``receiver_contract`` acknowledgements (``receiver_ready``,
``audio_receiving``, ``playback_confirmed``, ``playback_error``,
``device_error``, ``stopped``), so there is exactly one Receiver protocol.

Status meaning is unchanged by this module. In particular ``receiver_ready``
still means "software checks passed and output-device checks passed"; for the
audio pilot that means the Receiver proved FFmpeg is available, the Opus/WebM
decode path is supported and its bounded queue is ready. A Receiver must not
send it otherwise.

Nothing here carries a credential: no Receiver token, Authorization header,
password or JWT ever appears in an audio control message or in a chunk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping


AUDIO_PROTOCOL_VERSION = "1.0"

# The milestone format. These values match what the existing React broadcaster
# already produces (MediaRecorder "audio/webm;codecs=opus", mono, 32 kbps,
# 250 ms timeslice), so no browser change is needed to satisfy them.
SUPPORTED_CONTAINERS = ("webm",)
SUPPORTED_CODECS = ("opus",)
SUPPORTED_MIME_TYPES = (
    "audio/webm;codecs=opus",
    "audio/webm",
)
DEFAULT_MIME_TYPE = "audio/webm;codecs=opus"

# A 250 ms mono Opus chunk at ~32 kbps is roughly 1 KB. The ceiling is set far
# above that so a legitimate keyframe-ish chunk is never rejected, while a
# runaway or hostile frame still is.
MAX_AUDIO_CHUNK_BYTES = 262_144

MIN_CHUNK_MS = 20
MAX_CHUNK_MS = 1000


class AudioProtocolError(ValueError):
    """Base class for controlled, secret-free audio protocol failures."""


class UnsupportedAudioFormatError(AudioProtocolError):
    """Raised when a container/codec/channel combination is not supported."""


class InvalidAudioChunkError(AudioProtocolError):
    """Raised when a binary audio frame is empty, oversized or not bytes."""


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AudioProtocolError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """The negotiated audio format for one broadcast session."""

    container: str = "webm"
    codec: str = "opus"
    channels: int = 1
    target_bitrate: int = 32_000
    expected_chunk_ms: int = 250

    def __post_init__(self) -> None:
        if self.container not in SUPPORTED_CONTAINERS:
            raise UnsupportedAudioFormatError(
                f"container {self.container!r} is not supported; "
                f"expected one of {SUPPORTED_CONTAINERS}"
            )
        if self.codec not in SUPPORTED_CODECS:
            raise UnsupportedAudioFormatError(
                f"codec {self.codec!r} is not supported; "
                f"expected one of {SUPPORTED_CODECS}"
            )
        if self.channels != 1:
            raise UnsupportedAudioFormatError(
                "this milestone supports mono audio only"
            )
        if isinstance(self.target_bitrate, bool) or not isinstance(self.target_bitrate, int):
            raise UnsupportedAudioFormatError("target_bitrate must be an integer")
        if not 8_000 <= self.target_bitrate <= 128_000:
            raise UnsupportedAudioFormatError("target_bitrate is outside the supported range")
        if isinstance(self.expected_chunk_ms, bool) or not isinstance(self.expected_chunk_ms, int):
            raise UnsupportedAudioFormatError("expected_chunk_ms must be an integer")
        if not MIN_CHUNK_MS <= self.expected_chunk_ms <= MAX_CHUNK_MS:
            raise UnsupportedAudioFormatError("expected_chunk_ms is outside the supported range")

    def as_dict(self) -> dict[str, Any]:
        return {
            "channels": self.channels,
            "codec": self.codec,
            "container": self.container,
            "expected_chunk_ms": self.expected_chunk_ms,
            "mime_type": DEFAULT_MIME_TYPE,
            "target_bitrate": self.target_bitrate,
        }


DEFAULT_AUDIO_FORMAT = AudioFormat()


@dataclass(frozen=True, slots=True)
class PrepareMessage:
    broadcast_session_id: int
    target_store_id: int
    audio_format: AudioFormat
    emergency: bool = False


def build_prepare_message(
    *,
    session_id: int,
    store_id: int,
    audio_format: AudioFormat | None = None,
    emergency: bool = False,
) -> dict[str, Any]:
    """Build the ``prepare`` control message sent to one Store Receiver."""
    validated_session = _positive_int(session_id, "session_id")
    validated_store = _positive_int(store_id, "store_id")
    selected = audio_format or DEFAULT_AUDIO_FORMAT
    if not isinstance(selected, AudioFormat):
        raise AudioProtocolError("audio_format must be an AudioFormat")
    if not isinstance(emergency, bool):
        raise AudioProtocolError("emergency must be a boolean")
    return {
        "type": "prepare",
        "protocol_version": AUDIO_PROTOCOL_VERSION,
        "broadcast_session_id": validated_session,
        "target_store_id": validated_store,
        "audio_format": selected.as_dict(),
        "emergency": emergency,
    }


def parse_prepare_message(payload: Any) -> PrepareMessage:
    """Validate an inbound ``prepare`` message. Fails closed on anything odd."""
    if not isinstance(payload, Mapping):
        raise AudioProtocolError("prepare message must be an object")
    if payload.get("type") != "prepare":
        raise AudioProtocolError("message type is not 'prepare'")
    if payload.get("protocol_version") != AUDIO_PROTOCOL_VERSION:
        raise AudioProtocolError("unsupported audio protocol version")

    session_id = _positive_int(payload.get("broadcast_session_id"), "broadcast_session_id")
    store_id = _positive_int(payload.get("target_store_id"), "target_store_id")

    raw_format = payload.get("audio_format")
    if not isinstance(raw_format, Mapping):
        raise AudioProtocolError("prepare message is missing audio_format")

    audio_format = AudioFormat(
        container=raw_format.get("container"),
        codec=raw_format.get("codec"),
        channels=raw_format.get("channels"),
        target_bitrate=raw_format.get("target_bitrate"),
        expected_chunk_ms=raw_format.get("expected_chunk_ms"),
    )

    emergency = payload.get("emergency", False)
    if not isinstance(emergency, bool):
        raise AudioProtocolError("emergency must be a boolean")

    return PrepareMessage(
        broadcast_session_id=session_id,
        target_store_id=store_id,
        audio_format=audio_format,
        emergency=emergency,
    )


def build_stop_message(*, session_id: int, reason: str = "operator_stop") -> dict[str, Any]:
    """Build the existing ``stop`` control message shape."""
    validated_session = _positive_int(session_id, "session_id")
    if not isinstance(reason, str) or not reason or len(reason) > 128:
        raise AudioProtocolError("reason must be short printable text")
    if not reason.isprintable():
        raise AudioProtocolError("reason must be printable")
    return {"type": "stop", "session_id": validated_session, "reason": reason}


#: Output volume is a whole percent of the decoded signal, 0-100.
#:
#: The ceiling is 100 on purpose and is enforced here rather than only in the
#: UI: above unity a software gain clips, and clipping on a shop's PA is worse
#: than being quiet. Amplification belongs in a later audio-processing feature
#: with the headroom and limiting to do it properly.
MIN_OUTPUT_VOLUME_PERCENT = 0
MAX_OUTPUT_VOLUME_PERCENT = 100


def build_set_audio_control_message(
    *,
    session_id: int | None,
    command_id: int,
    volume_percent: int,
    muted: bool,
) -> dict[str, Any]:
    """Build the ``set_audio_control`` control message for one Store.

    Named to match the existing downstream vocabulary (``prepare``, ``stop``):
    a verb the Receiver is being asked to perform, carrying the session it
    belongs to. It is deliberately NOT a delta - it carries the whole desired
    state - so a command that is lost or arrives out of order can never leave a
    Store at a level nobody asked for. The Receiver applies the newest
    command_id it has seen and ignores anything older.

    Volume and mute travel together, and mute is a separate flag rather than
    "volume 0". The operator's chosen level has to survive being muted, and
    collapsing the two would lose it: unmuting would have nothing to restore.
    """
    # None means "no broadcast owns this Store" - the Master Volume panel
    # controlling an idle shop. The Receiver applies it to the same endpoint
    # through the same code path; what changes is only that there is no session
    # for it to be checked against, because there is no session.
    validated_session = (
        None if session_id is None else _positive_int(session_id, "session_id")
    )
    validated_command = _positive_int(command_id, "command_id")
    if isinstance(volume_percent, bool) or not isinstance(volume_percent, int):
        raise AudioProtocolError("volume_percent must be a whole number")
    if not MIN_OUTPUT_VOLUME_PERCENT <= volume_percent <= MAX_OUTPUT_VOLUME_PERCENT:
        raise AudioProtocolError(
            f"volume_percent must be between {MIN_OUTPUT_VOLUME_PERCENT} and "
            f"{MAX_OUTPUT_VOLUME_PERCENT}"
        )
    if not isinstance(muted, bool):
        raise AudioProtocolError("muted must be true or false")
    return {
        "type": "set_audio_control",
        "session_id": validated_session,
        "command_id": validated_command,
        "volume_percent": volume_percent,
        "muted": muted,
    }


def parse_set_audio_control_message(payload: Any) -> dict[str, Any]:
    """Receiver-side counterpart. Rejects anything it does not recognise.

    A Receiver must never widen its own contract by tolerating extra keys: a
    field it silently ignores today is a field HQ believes is being honoured.
    """
    if not isinstance(payload, dict):
        raise AudioProtocolError("set_audio_control must be an object")
    if payload.get("type") != "set_audio_control":
        raise AudioProtocolError("not a set_audio_control message")
    unexpected = set(payload) - {
        "type", "session_id", "command_id", "volume_percent", "muted",
    }
    if unexpected:
        raise AudioProtocolError(f"unexpected fields: {sorted(unexpected)}")
    return build_set_audio_control_message(
        session_id=payload.get("session_id"),
        command_id=payload.get("command_id"),
        volume_percent=payload.get("volume_percent"),
        muted=payload.get("muted"),
    )


def validate_audio_chunk(chunk: Any, *, max_bytes: int = MAX_AUDIO_CHUNK_BYTES) -> bool:
    """Validate one binary audio frame. Never logs or returns the payload."""
    if not isinstance(chunk, (bytes, bytearray, memoryview)):
        raise InvalidAudioChunkError("an audio chunk must be binary data")
    size = len(chunk)
    if size == 0:
        raise InvalidAudioChunkError("an audio chunk must not be empty")
    if size > max_bytes:
        raise InvalidAudioChunkError(
            f"an audio chunk of {size} bytes exceeds the {max_bytes} byte limit"
        )
    return True


def with_chunk_ms(audio_format: AudioFormat, expected_chunk_ms: int) -> AudioFormat:
    """Return a copy of the format with a different chunk duration."""
    return replace(audio_format, expected_chunk_ms=expected_chunk_ms)
