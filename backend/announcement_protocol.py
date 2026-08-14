"""What HQ tells a Store Receiver about announcements, and what it hears back.

WHY THE AUDIO DOES NOT TRAVEL ON THIS SOCKET

The control socket carries commands: short, ordered, and every one of them
needs to arrive promptly. A recording is up to 25 MB. Pushing one through that
socket would block every other command to that Store behind it - including
``stop`` - so a shop could not be silenced while a jingle was still arriving.

So a command carries a REFERENCE, and the Receiver fetches the bytes over
HTTP with its own credential. Three consequences worth stating:

  * the fetch is retryable on its own, without HQ resending anything;
  * a Store that is already holding this recording fetches nothing, because
    the reference is a content hash and the cache is keyed by it;
  * a recording that was replaced has a different hash, so no cache anywhere
    can serve the old one by accident. This is the property that a filename or
    an id would not have given: re-uploading "diwali.mp3" with new audio and
    having half the estate keep playing last year's is exactly the failure.

WHY THE COMMANDS ARE SEPARATE FROM THE BROADCAST ONES

``play`` and ``stop`` mean the live broadcast. An announcement uses its own
verbs, so a Receiver built before this feature ignores what it does not
understand rather than mistaking an announcement for a broadcast - and so that
reading a Store's log never leaves anybody guessing which of the two a bare
"play" referred to.
"""

from __future__ import annotations

from typing import Any

#: HQ -> Receiver.
COMMAND_PLAY = "announcement_play"
COMMAND_PAUSE = "announcement_pause"
COMMAND_STOP = "announcement_stop"
COMMAND_SET_VOLUME = "announcement_set_volume"

#: Receiver -> HQ.
ACK_PLAYING = "announcement_playing"
ACK_PAUSED = "announcement_paused"
ACK_STOPPED = "announcement_stopped"
ACK_FAILED = "announcement_failed"

COMMANDS = (COMMAND_PLAY, COMMAND_PAUSE, COMMAND_STOP, COMMAND_SET_VOLUME)
ACKNOWLEDGEMENTS = (ACK_PLAYING, ACK_PAUSED, ACK_STOPPED, ACK_FAILED)


def play_command(*, audio_id: int, sha256: str, download_path: str,
                 volume_percent: int, template_id: int | None = None,
                 content_type: str = "audio/mpeg") -> dict[str, Any]:
    """Play this recording, at this level.

    The volume travels WITH the play command rather than as a separate message
    that follows it. Sent separately, a Store would play the first half-second
    at whatever level it happened to be holding - which, after a broadcast that
    had turned it down, is the wrong level in the direction people notice.
    """
    return {
        "type": COMMAND_PLAY,
        "audio_id": audio_id,
        "sha256": sha256,
        "download_path": download_path,
        "content_type": content_type,
        "volume_percent": volume_percent,
        "template_id": template_id,
    }


def pause_command(*, reason: str = "hq") -> dict[str, Any]:
    """Stop making sound, keep the recording.

    ``reason`` is carried so the Store's own log can distinguish a person
    pausing from a broadcast arriving. It changes nothing about what the
    Receiver does - the two are identical at the speaker - but a Store log
    that cannot tell them apart is a log nobody can use to answer "why did it
    go quiet at 4pm".
    """
    return {"type": COMMAND_PAUSE, "reason": reason}


def stop_command() -> dict[str, Any]:
    """Stop, and forget what was loaded. Sent when a template is withdrawn."""
    return {"type": COMMAND_STOP}


def set_volume_command(*, volume_percent: int) -> dict[str, Any]:
    return {"type": COMMAND_SET_VOLUME, "volume_percent": volume_percent}


def is_announcement_acknowledgement(message: dict) -> bool:
    return (message or {}).get("type") in ACKNOWLEDGEMENTS


def download_path(audio_id: int) -> str:
    return f"/api/receiver/announcements/{audio_id}/download"
