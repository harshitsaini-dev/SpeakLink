"""Recorded promotional announcements: what exists, where it plays, and what
is playing right now.

WHY THIS IS NOT JUST "A BROADCAST WITH A FILE"

A live broadcast is one person speaking to a set of Stores for as long as they
hold the microphone. An announcement is the opposite in every way that matters
to the schema: nobody is present, the same recording runs in many Stores at
once for days, each Store is at a different point in it, and the whole thing
has to get out of the way the moment somebody does pick up a microphone - and
come back afterwards.

So this carries four tables rather than reusing BroadcastSession:

  announcement_audio            the recordings themselves
  announcement_templates        a named, reusable plan: which recording plays
                                in which Stores, and until when
  announcement_template_items   one line of that plan
  announcement_playback         what is actually happening in ONE Store now

THE STATE THAT MOST OF THIS EXISTS FOR

``announcement_playback.state`` distinguishes PAUSED from DUCKED, and that
distinction is the whole of requirement 4.

  PAUSED  a person pressed Pause at HQ.
  DUCKED  a live broadcast started, so the announcement stepped aside.

They must never be one state. If they were, then a broadcast ending would
resume an announcement that an operator had deliberately paused an hour
earlier - the shop would suddenly start talking on its own, and the person who
paused it would have no way to understand why. Auto-resume therefore only ever
resumes what auto-pause itself paused: DUCKED -> PLAYING, and PAUSED is left
exactly where it is.

The same reasoning is why ``ducked_from`` is stored. A Store that was PAUSED
when a broadcast arrived must still be PAUSED when it leaves, and a Store that
was STOPPED must still be STOPPED. Recording where it came from is the only way
to put it back honestly.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine

AUDIO_TABLE = "announcement_audio"
TEMPLATE_TABLE = "announcement_templates"
ITEM_TABLE = "announcement_template_items"
PLAYBACK_TABLE = "announcement_playback"
HISTORY_TABLE = "announcement_history"

#: A Store is in exactly one of these at any moment.
STATE_STOPPED = "STOPPED"
STATE_PLAYING = "PLAYING"
STATE_PAUSED = "PAUSED"
STATE_DUCKED = "DUCKED"
PLAYBACK_STATES = (STATE_STOPPED, STATE_PLAYING, STATE_PAUSED, STATE_DUCKED)

#: The states auto-resume will restore a DUCKED Store to. Anything else and the
#: Store stays where it was: see the module docstring.
RESUMABLE_FROM = STATE_PLAYING

#: Volume is a percentage, and the ceiling is deliberate. The Store Receiver
#: already clamps its own output; this stops HQ from sending a number that
#: would be clamped silently and then read back differently from what was set.
MIN_VOLUME = 0
MAX_VOLUME = 100
DEFAULT_VOLUME = 80

#: Recordings are bigger than chat images and are uploaded rarely, by an
#: administrator, over the LAN.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: Formats the Receiver's decoder already handles. An allowlist, not a
#: denylist: an unknown container that happens to decode today is a Store that
#: goes silent after an upgrade.
ALLOWED_AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def audio_directory(repository_root: Path | None = None) -> Path:
    """Where recordings live on disk.

    Beside the chat attachments and under the same override, so a deployment
    that moves its data directory moves all of it and not most of it.
    """
    configured = os.environ.get("SPEAKLINK_DATA_DIR", "").strip()
    if configured:
        base = Path(configured)
    else:
        root = repository_root or Path(__file__).resolve().parents[1]
        base = root / "data"
    directory = base / "announcements"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def new_storage_name(extension: str) -> str:
    """An unguessable filename.

    Never the uploaded name. An operator's file is called "Diwali Offer.mp3" in
    three different Stores' folders, and the uploaded name is also the one
    piece of an upload a stranger can choose - so it decides nothing about
    where the bytes land.
    """
    return f"{secrets.token_hex(16)}{extension}"


# ===========================================================================
# Schema
# ===========================================================================

def ensure_announcement_schema(engine: Engine) -> None:
    """Create the announcement tables if absent. Additive, safe on every boot."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {AUDIO_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title VARCHAR(200) NOT NULL,
                -- The name the operator uploaded, kept only to show back to
                -- them. Never used to build a path.
                original_filename VARCHAR(255) NOT NULL,
                -- The name on disk, which this program chose.
                storage_name VARCHAR(80) NOT NULL UNIQUE,
                content_type VARCHAR(60) NOT NULL,
                byte_size INTEGER NOT NULL,
                -- Null until something has decoded it. A duration nobody has
                -- measured is not a duration, and showing 0:00 would be a
                -- statement rather than an absence.
                duration_seconds REAL,
                sha256 VARCHAR(64) NOT NULL,
                uploaded_by INTEGER,
                uploaded_at VARCHAR(40) NOT NULL,
                -- active | archived. Archived recordings stay on disk because
                -- a template may still reference them and history must remain
                -- readable.
                status VARCHAR(16) NOT NULL DEFAULT 'active'
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TEMPLATE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(120) NOT NULL,
                description VARCHAR(500) NOT NULL DEFAULT '',
                created_by INTEGER,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                -- The point of a template: set it once, then only ever press
                -- play and pause. Expiry is part of the plan, not a reminder
                -- somebody has to keep.
                starts_at VARCHAR(40),
                expires_at VARCHAR(40),
                status VARCHAR(16) NOT NULL DEFAULT 'active'
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {ITEM_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL,
                audio_id INTEGER NOT NULL,
                -- Exactly one of these is set. A line that named both a Store
                -- and a zone would have to be resolved somewhere, and every
                -- reader would have to agree on the answer.
                store_id INTEGER,
                zone VARCHAR(100),
                -- The order recordings play in within one Store.
                position INTEGER NOT NULL DEFAULT 0,
                -- Per-line volume, so one loud jingle does not force the whole
                -- template down.
                volume_percent INTEGER NOT NULL DEFAULT 80
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {PLAYBACK_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- One live row per Store. Enforced by the schema rather than
                -- remembered by the application, which is what kept the
                -- broadcast lease table honest.
                store_id INTEGER NOT NULL UNIQUE,
                template_id INTEGER,
                audio_id INTEGER,
                state VARCHAR(16) NOT NULL DEFAULT 'STOPPED',
                -- Where a DUCKED Store came from, so a broadcast ending can
                -- put it back where it was instead of guessing PLAYING.
                ducked_from VARCHAR(16),
                volume_percent INTEGER NOT NULL DEFAULT 80,
                -- Who last changed it, and when. An announcement running in a
                -- shop with nobody able to say who started it is the thing
                -- this column exists to prevent.
                updated_by INTEGER,
                updated_at VARCHAR(40) NOT NULL,
                started_at VARCHAR(40)
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                template_id INTEGER,
                audio_id INTEGER,
                -- Denormalised on purpose. A history row must stay readable
                -- after the template is archived and the recording deleted,
                -- and a JOIN to a row that no longer exists reads as "unknown"
                -- for something that was perfectly well known at the time.
                store_code VARCHAR(50),
                store_name VARCHAR(200),
                zone VARCHAR(100),
                template_name VARCHAR(120),
                audio_title VARCHAR(200),
                started_at VARCHAR(40) NOT NULL,
                ended_at VARCHAR(40),
                started_by INTEGER,
                ended_by INTEGER,
                -- Why it stopped: paused | broadcast | stopped | superseded.
                -- "It went quiet at 4pm" is answerable only if the reason was
                -- written down at the time.
                ended_reason VARCHAR(20),
                volume_percent INTEGER,
                archived_at VARCHAR(40)
            )
            """
        )
        for statement in (
            f"CREATE INDEX IF NOT EXISTS ix_announcement_audio_status "
            f"ON {AUDIO_TABLE}(status)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_templates_status "
            f"ON {TEMPLATE_TABLE}(status)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_items_template "
            f"ON {ITEM_TABLE}(template_id, position)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_items_store "
            f"ON {ITEM_TABLE}(store_id)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_items_zone "
            f"ON {ITEM_TABLE}(zone)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_playback_state "
            f"ON {PLAYBACK_TABLE}(state)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_history_started "
            f"ON {HISTORY_TABLE}(started_at)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_history_store "
            f"ON {HISTORY_TABLE}(store_id, started_at)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_history_open "
            f"ON {HISTORY_TABLE}(store_id, ended_at)",
        ):
            connection.exec_driver_sql(statement)


# ===========================================================================
# Validation
# ===========================================================================

class AnnouncementRefused(ValueError):
    """A refusal with a sentence an operator can act on."""


def validate_volume(value) -> int:
    try:
        volume = int(value)
    except (TypeError, ValueError):
        raise AnnouncementRefused("Volume must be a whole number of percent.")
    if not MIN_VOLUME <= volume <= MAX_VOLUME:
        raise AnnouncementRefused(
            f"Volume must be between {MIN_VOLUME} and {MAX_VOLUME} percent; "
            f"{volume} was given.")
    return volume


def validate_upload(raw: bytes, content_type: str, filename: str) -> str:
    """Refuse before anything is written. Returns the extension to store under."""
    if not raw:
        raise AnnouncementRefused("That file is empty.")
    if len(raw) > MAX_AUDIO_BYTES:
        megabytes = MAX_AUDIO_BYTES // (1024 * 1024)
        raise AnnouncementRefused(
            f"That recording is {len(raw) / (1024 * 1024):.1f} MB. The limit is "
            f"{megabytes} MB - a promotional announcement well under a minute "
            "is normally a small fraction of that.")
    normalised = (content_type or "").split(";", 1)[0].strip().lower()
    if normalised not in ALLOWED_AUDIO_TYPES:
        readable = ", ".join(sorted({value.lstrip('.')
                                     for value in ALLOWED_AUDIO_TYPES.values()}))
        raise AnnouncementRefused(
            f"{filename or 'That file'} is not an audio format this system "
            f"plays. Use one of: {readable}.")
    return ALLOWED_AUDIO_TYPES[normalised]


def item_targets_exactly_one(store_id, zone) -> None:
    """A template line names a Store or a zone, never both and never neither."""
    named = [value for value in (store_id, zone) if value not in (None, "")]
    if len(named) != 1:
        raise AnnouncementRefused(
            "Each line of a template plays in one Store or one zone. "
            f"{'Both were given' if len(named) > 1 else 'Neither was given'}.")


# ===========================================================================
# The transitions
#
# Written as functions over a state rather than as UPDATE statements scattered
# through the API, so that the one rule that matters - auto-resume restores
# only what auto-pause paused - is stated once and can be tested without a
# database, an HTTP client or a Store.
# ===========================================================================

def next_state_for_play(current: str) -> str:
    """Pressing Play.

    A DUCKED Store does NOT start playing over a live broadcast. The operator
    is told why rather than being ignored; the request is recorded as the
    state to return to when the broadcast ends.
    """
    if current == STATE_DUCKED:
        raise AnnouncementRefused(
            "A live broadcast is playing in this Store, so the announcement "
            "is standing aside. It will resume by itself when the broadcast "
            "ends.")
    return STATE_PLAYING


def next_state_for_pause(current: str) -> str:
    """Pressing Pause.

    Pausing a DUCKED Store is meaningful and must be honoured: it says "do not
    come back when the broadcast ends". Recording it as PAUSED is exactly what
    makes auto-resume leave it alone.
    """
    if current == STATE_STOPPED:
        return STATE_STOPPED
    return STATE_PAUSED


def duck(current: str) -> tuple[str, str | None]:
    """A live broadcast has started in this Store.

    Returns the new state and what to remember. Only a PLAYING Store ducks;
    anything else is already silent and must be left exactly as it is, or the
    broadcast ending would start an announcement nobody asked for.
    """
    if current != STATE_PLAYING:
        return current, None
    return STATE_DUCKED, STATE_PLAYING


def unduck(current: str, ducked_from: str | None) -> tuple[str, None]:
    """The broadcast has ended.

    Restores only what ducking itself moved. A Store an operator paused during
    the broadcast is PAUSED, not DUCKED, and stays silent - which is the whole
    reason those are two states.
    """
    if current != STATE_DUCKED:
        return current, None
    return (ducked_from or STATE_PLAYING), None
