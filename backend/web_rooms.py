"""One web audience room per Broadcast, and who is allowed into it.

WHAT IS PERSISTED HERE, AND WHAT DELIBERATELY IS NOT

This module stores the facts that must survive a restart and that somebody may
later have to answer for: that a room existed, who asked to join it, who was
admitted and how, who was refused, and who was removed. Those are lifecycle
events, and there are a handful of them per participant.

It stores none of the fast-moving truth. Whether a listener's socket is
currently open, when its last heartbeat arrived, whether its browser is playing
or buffering - all of that changes every few seconds per listener and belongs in
runtime memory. Writing it here would mean a database write per heartbeat per
listener, which at a hundred listeners is a write every few tens of
milliseconds, to record something that is worthless the moment the process
restarts anyway.

WHAT IS NEVER STORED

The join password is stored only as a bcrypt hash, using the same helpers as HQ
accounts. The plaintext is returned exactly once, at the moment it is generated,
and then it is gone - there is no column it could be read back from. The
alternative, keeping it recoverable so the console could redisplay it after a
refresh, would mean a reversible secret in the database in exchange for a
convenience, and rotation gives the same convenience for nothing.

Listener session tokens are likewise stored only as a hash. A stolen database
must not hand over the ability to impersonate an admitted listener.

THE PUBLIC CODE IS NOT THE SESSION ID

``broadcast_sessions.id`` is a small consecutive integer. Publishing it as the
room identifier would let anyone who received one link enumerate every other
Broadcast by subtracting one. The public code is random, drawn from an alphabet
with the visually ambiguous characters removed so it survives being read aloud
or typed off a screen, and it is unique by database constraint rather than by
the application remembering to check.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from auth import hash_password, verify_password

__all__ = [
    "ROOM_TABLE",
    "PARTICIPANT_TABLE",
    "AdmissionStatus",
    "RoomStatus",
    "WebRoom",
    "WebParticipant",
    "ensure_web_room_schema",
    "generate_public_code",
    "generate_join_password",
    "create_room",
    "get_room_for_session",
    "get_room_by_id",
    "find_room_by_public_code",
    "rotate_password",
    "set_auto_approve",
    "end_room",
    "verify_join_password",
    "admit_with_password",
    "request_access",
    "approve_participant",
    "deny_participant",
    "kick_participant",
    "mark_participant_left",
    "get_participant",
    "list_participants",
    "authenticate_listener",
    "MAX_DISPLAY_NAME",
    "MIN_DISPLAY_NAME",
    "normalise_display_name",
    "InvalidDisplayNameError",
    "RoomNotOpenError",
    "ParticipantNotAdmissibleError",
]

ROOM_TABLE = "broadcast_web_rooms"
PARTICIPANT_TABLE = "broadcast_web_participants"

#: No 0/O, no 1/I/L. A code is read off one screen and typed into another, and
#: the pairs above are where that goes wrong.
PUBLIC_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
PUBLIC_CODE_PREFIX = "SL-"
PUBLIC_CODE_LENGTH = 6
#: 31^6 is about 887 million. A room is short-lived and the code is not the only
#: control - the password or an approval still stands behind it - so this is
#: sized to make guessing pointless rather than to stand alone as a secret.
PUBLIC_CODE_ATTEMPTS = 12

PASSWORD_ALPHABET = PUBLIC_CODE_ALPHABET
PASSWORD_GROUPS = 2
PASSWORD_GROUP_LENGTH = 4

MIN_DISPLAY_NAME = 1
MAX_DISPLAY_NAME = 40

LISTENER_TOKEN_BYTES = 32


class RoomStatus:
    OPEN = "OPEN"
    ENDED = "ENDED"


class AdmissionStatus:
    """What has happened to one participant, in the order it can happen.

    ``PASSWORD_ADMITTED`` and ``APPROVED`` are both admitted states and are kept
    distinct on purpose: the broadcaster should be able to see at a glance who
    knew the password and who they personally let in.
    """

    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    PASSWORD_ADMITTED = "PASSWORD_ADMITTED"
    DENIED = "DENIED"
    KICKED = "KICKED"
    LEFT = "LEFT"
    ROOM_ENDED = "ROOM_ENDED"

    #: The states in which a listener session may carry audio.
    ADMITTED = frozenset({APPROVED, PASSWORD_ADMITTED})
    #: Terminal: nothing moves a participant out of these.
    TERMINAL = frozenset({DENIED, KICKED, ROOM_ENDED})


class WebRoomError(RuntimeError):
    """Base class for controlled, secret-free room failures."""


class InvalidDisplayNameError(WebRoomError):
    """The listener's name is missing, too long, or not usable as a name."""


class RoomNotOpenError(WebRoomError):
    """The room has ended, so nobody else is joining it."""


class ParticipantNotAdmissibleError(WebRoomError):
    """The participant is in a state this transition cannot act on."""


@dataclass(frozen=True, slots=True)
class WebRoom:
    id: int
    session_id: int
    public_code: str
    auto_approve: bool
    status: str
    created_at: str
    password_rotated_at: str | None
    ended_at: str | None

    @property
    def is_open(self) -> bool:
        return self.status == RoomStatus.OPEN


@dataclass(frozen=True, slots=True)
class WebParticipant:
    id: int
    room_id: int
    display_name: str
    admission_status: str
    created_at: str
    requested_at: str | None
    approved_at: str | None
    denied_at: str | None
    joined_at: str | None
    kicked_at: str | None
    ended_at: str | None

    @property
    def is_admitted(self) -> bool:
        return self.admission_status in AdmissionStatus.ADMITTED

    def public_dict(self) -> dict[str, Any]:
        """What the BROADCASTER may see. Never a token, never a hash."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "admission_status": self.admission_status,
            "admitted_by": ("password"
                            if self.admission_status == AdmissionStatus.PASSWORD_ADMITTED
                            else "approval" if self.admission_status == AdmissionStatus.APPROVED
                            else None),
            "requested_at": self.requested_at,
            "approved_at": self.approved_at,
            "joined_at": self.joined_at,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_web_room_schema(engine: Engine) -> None:
    """Create the room tables if absent. Safe on every boot, purely additive."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {ROOM_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                -- One room per Broadcast, enforced by the schema rather than
                -- remembered by the application.
                session_id INTEGER NOT NULL UNIQUE,
                -- Random and public. Never the session id.
                public_code VARCHAR(24) NOT NULL UNIQUE,
                -- bcrypt. There is no column the plaintext could be read from.
                password_hash VARCHAR(255) NOT NULL,
                auto_approve INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                created_at VARCHAR(40) NOT NULL,
                password_rotated_at VARCHAR(40),
                ended_at VARCHAR(40),
                CONSTRAINT fk_web_room_session
                    FOREIGN KEY (session_id) REFERENCES broadcast_sessions(id)
                    ON DELETE CASCADE,
                CONSTRAINT ck_web_room_status CHECK (status IN ('OPEN', 'ENDED'))
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_web_room_code "
            f"ON {ROOM_TABLE} (public_code)"
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {PARTICIPANT_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                display_name VARCHAR({MAX_DISPLAY_NAME}) NOT NULL,
                admission_status VARCHAR(24) NOT NULL,
                -- Only a hash. A stolen database must not be able to
                -- impersonate an admitted listener.
                session_token_hash VARCHAR(64),
                created_at VARCHAR(40) NOT NULL,
                requested_at VARCHAR(40),
                approved_at VARCHAR(40),
                denied_at VARCHAR(40),
                joined_at VARCHAR(40),
                kicked_at VARCHAR(40),
                ended_at VARCHAR(40),
                CONSTRAINT fk_web_participant_room
                    FOREIGN KEY (room_id) REFERENCES {ROOM_TABLE} (id)
                    ON DELETE CASCADE,
                CONSTRAINT ck_web_participant_status CHECK (
                    admission_status IN ('REQUESTED', 'APPROVED',
                                         'PASSWORD_ADMITTED', 'DENIED',
                                         'KICKED', 'LEFT', 'ROOM_ENDED')
                )
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_web_participant_room "
            f"ON {PARTICIPANT_TABLE} (room_id, admission_status)"
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_web_participant_token "
            f"ON {PARTICIPANT_TABLE} (session_token_hash)"
        )


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def generate_public_code() -> str:
    """A random, readable room identifier. Never derived from anything."""
    body = "".join(secrets.choice(PUBLIC_CODE_ALPHABET)
                   for _ in range(PUBLIC_CODE_LENGTH))
    return f"{PUBLIC_CODE_PREFIX}{body}"


def generate_join_password() -> str:
    """A random join password, grouped so it can be read out over a phone."""
    groups = ["".join(secrets.choice(PASSWORD_ALPHABET)
                      for _ in range(PASSWORD_GROUP_LENGTH))
              for _ in range(PASSWORD_GROUPS)]
    return "-".join(groups)


def _token_hash(token: str) -> str:
    """SHA-256, not bcrypt.

    A listener token is already 32 bytes of entropy from ``secrets``, so it has
    nothing to guess and needs no work factor. It is also checked on every
    socket connection and every heartbeat, where bcrypt's cost would be a
    denial-of-service surface rather than a protection.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalise_display_name(raw: Any) -> str:
    """Trim, bound and sanity-check a listener's chosen name.

    The name is display text and nothing more - it is never an identifier, and
    two people called Harshit are two participants. React escapes it on render,
    so this is about it being a usable name rather than about injection.
    """
    if not isinstance(raw, str):
        raise InvalidDisplayNameError("a name is required")
    # Collapse whitespace, including the newlines a paste can carry in.
    name = " ".join(raw.split())
    if len(name) < MIN_DISPLAY_NAME:
        raise InvalidDisplayNameError("a name is required")
    if len(name) > MAX_DISPLAY_NAME:
        raise InvalidDisplayNameError(
            f"a name may be at most {MAX_DISPLAY_NAME} characters")
    # Control characters are not names, and would corrupt a console's layout.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise InvalidDisplayNameError("a name may not contain control characters")
    return name


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

def _room_from_row(row) -> WebRoom:
    return WebRoom(
        id=row[0], session_id=row[1], public_code=row[2],
        auto_approve=bool(row[3]), status=row[4], created_at=row[5],
        password_rotated_at=row[6], ended_at=row[7],
    )


_ROOM_COLUMNS = ("id, session_id, public_code, auto_approve, status, "
                 "created_at, password_rotated_at, ended_at")


def create_room(engine: Engine, *, session_id: int) -> tuple[WebRoom, str]:
    """Create this Broadcast's room. Returns the room and the ONE-TIME password.

    Idempotent per session by the unique constraint: if a room already exists
    this returns it with an empty password, because the plaintext of an existing
    room genuinely is not recoverable and inventing one would be a lie.
    """
    existing = get_room_for_session(engine, session_id=session_id)
    if existing is not None:
        return existing, ""

    password = generate_join_password()
    password_hash = hash_password(password)
    created = _now()

    # Retry on the astronomically unlikely collision rather than failing the
    # Broadcast. The uniqueness itself is the database's job, not a pre-check
    # that another request could race past.
    for _ in range(PUBLIC_CODE_ATTEMPTS):
        code = generate_public_code()
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"INSERT INTO {ROOM_TABLE} "
                    f"(session_id, public_code, password_hash, auto_approve, "
                    f" status, created_at) VALUES (?, ?, ?, 0, 'OPEN', ?)",
                    (session_id, code, password_hash, created),
                )
        except Exception:
            # A second request for the SAME session lost the race: return what
            # it created rather than raising.
            duplicate = get_room_for_session(engine, session_id=session_id)
            if duplicate is not None:
                return duplicate, ""
            continue          # a public_code collision: draw another
        room = get_room_for_session(engine, session_id=session_id)
        assert room is not None
        return room, password

    raise WebRoomError("could not allocate a unique public code")


def get_room_for_session(engine: Engine, *, session_id: int) -> WebRoom | None:
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT {_ROOM_COLUMNS} FROM {ROOM_TABLE} WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return _room_from_row(row) if row else None


def find_room_by_public_code(engine: Engine, *, public_code: str) -> WebRoom | None:
    """Look a room up by its public identifier.

    Case-insensitive, because the code is typed by hand off a shared link.
    """
    if not isinstance(public_code, str) or not public_code.strip():
        return None
    candidate = public_code.strip().upper()
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT {_ROOM_COLUMNS} FROM {ROOM_TABLE} "
            f"WHERE UPPER(public_code) = ?", (candidate,),
        ).fetchone()
    return _room_from_row(row) if row else None


def rotate_password(engine: Engine, *, session_id: int) -> str:
    """Replace the join password. Returns the new plaintext, once.

    Rotation deliberately does NOT disturb anybody already admitted. The
    password governs who may still come in; ejecting the audience is what Kick
    is for, and conflating the two would make an operator choose between
    stopping new arrivals and keeping the people already listening.
    """
    room = get_room_for_session(engine, session_id=session_id)
    if room is None:
        raise WebRoomError("this Broadcast has no web room")
    if not room.is_open:
        raise RoomNotOpenError("this room has ended")

    password = generate_join_password()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {ROOM_TABLE} SET password_hash = ?, password_rotated_at = ? "
            f"WHERE id = ?", (hash_password(password), _now(), room.id),
        )
    return password


def set_auto_approve(engine: Engine, *, session_id: int, enabled: bool) -> WebRoom:
    room = get_room_for_session(engine, session_id=session_id)
    if room is None:
        raise WebRoomError("this Broadcast has no web room")
    if not room.is_open:
        raise RoomNotOpenError("this room has ended")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {ROOM_TABLE} SET auto_approve = ? WHERE id = ?",
            (1 if enabled else 0, room.id),
        )
    updated = get_room_for_session(engine, session_id=session_id)
    assert updated is not None
    return updated


def end_room(engine: Engine, *, session_id: int) -> WebRoom | None:
    """Close the room with its Broadcast. Idempotent.

    Every participant who was still admitted is moved to ROOM_ENDED in the same
    transaction, so no listener session survives the Broadcast that authorised
    it - a token whose room has ended must not be usable, and leaving them
    APPROVED would mean exactly that.
    """
    room = get_room_for_session(engine, session_id=session_id)
    if room is None:
        return None
    if not room.is_open:
        return room

    ended = _now()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {ROOM_TABLE} SET status = 'ENDED', ended_at = ? WHERE id = ?",
            (ended, room.id),
        )
        connection.exec_driver_sql(
            f"UPDATE {PARTICIPANT_TABLE} "
            f"SET admission_status = 'ROOM_ENDED', ended_at = ?, "
            f"    session_token_hash = NULL "
            f"WHERE room_id = ? AND admission_status IN "
            f"      ('REQUESTED', 'APPROVED', 'PASSWORD_ADMITTED')",
            (ended, room.id),
        )
    return get_room_for_session(engine, session_id=session_id)


def verify_join_password(engine: Engine, *, room: WebRoom, password: Any) -> bool:
    """Check a submitted password. Never logs or returns what was submitted."""
    if not isinstance(password, str) or not password:
        return False
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT password_hash FROM {ROOM_TABLE} WHERE id = ?", (room.id,),
        ).fetchone()
    if not row:
        return False
    return verify_password(password, row[0])


# ---------------------------------------------------------------------------
# Participants
# ---------------------------------------------------------------------------

_PARTICIPANT_COLUMNS = ("id, room_id, display_name, admission_status, created_at, "
                        "requested_at, approved_at, denied_at, joined_at, "
                        "kicked_at, ended_at")


def _participant_from_row(row) -> WebParticipant:
    return WebParticipant(
        id=row[0], room_id=row[1], display_name=row[2], admission_status=row[3],
        created_at=row[4], requested_at=row[5], approved_at=row[6],
        denied_at=row[7], joined_at=row[8], kicked_at=row[9], ended_at=row[10],
    )


def get_participant(engine: Engine, *, participant_id: int) -> WebParticipant | None:
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM {PARTICIPANT_TABLE} WHERE id = ?",
            (participant_id,),
        ).fetchone()
    return _participant_from_row(row) if row else None


def list_participants(engine: Engine, *, room_id: int) -> list[WebParticipant]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM {PARTICIPANT_TABLE} "
            f"WHERE room_id = ? ORDER BY id", (room_id,),
        ).fetchall()
    return [_participant_from_row(row) for row in rows]


def _issue_token(connection, participant_id: int) -> str:
    token = secrets.token_urlsafe(LISTENER_TOKEN_BYTES)
    connection.exec_driver_sql(
        f"UPDATE {PARTICIPANT_TABLE} SET session_token_hash = ? WHERE id = ?",
        (_token_hash(token), participant_id),
    )
    return token


def _insert_participant(connection, *, room_id: int, display_name: str,
                        status: str, now: str) -> int:
    requested = now if status == AdmissionStatus.REQUESTED else None
    approved = now if status in AdmissionStatus.ADMITTED else None
    joined = now if status in AdmissionStatus.ADMITTED else None
    cursor = connection.exec_driver_sql(
        f"INSERT INTO {PARTICIPANT_TABLE} "
        f"(room_id, display_name, admission_status, created_at, requested_at, "
        f" approved_at, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (room_id, display_name, status, now, requested, approved, joined),
    )
    return int(cursor.lastrowid)


def admit_with_password(engine: Engine, *, room: WebRoom,
                        display_name: str) -> tuple[WebParticipant, str]:
    """Create an already-admitted participant. The caller checked the password.

    Password admission never creates a request, and is unaffected by Auto
    Approve: knowing the password IS the authorisation.
    """
    if not room.is_open:
        raise RoomNotOpenError("this room has ended")
    name = normalise_display_name(display_name)
    now = _now()
    with engine.begin() as connection:
        participant_id = _insert_participant(
            connection, room_id=room.id, display_name=name,
            status=AdmissionStatus.PASSWORD_ADMITTED, now=now)
        token = _issue_token(connection, participant_id)
    participant = get_participant(engine, participant_id=participant_id)
    assert participant is not None
    return participant, token


def request_access(engine: Engine, *, room: WebRoom,
                   display_name: str) -> tuple[WebParticipant, str | None]:
    """Ask to be let in without a password.

    Returns a token only when the room admitted immediately, which is what Auto
    Approve means. Auto Approve is read INSIDE the same transaction that creates
    the row, so a toggle racing a request produces one participant in one
    state - never a duplicate, and never a row that is approved twice.
    """
    if not room.is_open:
        raise RoomNotOpenError("this room has ended")
    name = normalise_display_name(display_name)
    now = _now()

    with engine.begin() as connection:
        # Re-read auto_approve and the room status here rather than trusting the
        # snapshot passed in: between that read and this write the broadcaster
        # may have toggled it or stopped the Broadcast.
        row = connection.exec_driver_sql(
            f"SELECT auto_approve, status FROM {ROOM_TABLE} WHERE id = ?",
            (room.id,),
        ).fetchone()
        if row is None or row[1] != RoomStatus.OPEN:
            raise RoomNotOpenError("this room has ended")
        auto_approve = bool(row[0])

        status = (AdmissionStatus.APPROVED if auto_approve
                  else AdmissionStatus.REQUESTED)
        participant_id = _insert_participant(
            connection, room_id=room.id, display_name=name,
            status=status, now=now)
        token = _issue_token(connection, participant_id) if auto_approve else None

    participant = get_participant(engine, participant_id=participant_id)
    assert participant is not None
    return participant, token


def approve_participant(engine: Engine, *, room_id: int,
                        participant_id: int) -> tuple[WebParticipant, str | None]:
    """Let a waiting participant in. Idempotent, and refuses terminal states.

    A second click returns the same participant without minting a second token:
    two tokens would mean two listener sessions for one person, and only one of
    them would ever be revoked by a Kick.

    DENIED is terminal. Re-approving somebody the broadcaster has already turned
    away would mean a mis-click could not be trusted to have stuck; if they are
    to be let in, they can ask again.
    """
    participant = _participant_in_room(engine, room_id=room_id,
                                       participant_id=participant_id)
    if participant.admission_status in AdmissionStatus.ADMITTED:
        return participant, None          # already in; no second token
    if participant.admission_status in AdmissionStatus.TERMINAL:
        raise ParticipantNotAdmissibleError(
            f"this participant is {participant.admission_status.lower()}")

    now = _now()
    with engine.begin() as connection:
        # Guarded by the current status so two concurrent approvals cannot both
        # mint a token.
        result = connection.exec_driver_sql(
            f"UPDATE {PARTICIPANT_TABLE} SET admission_status = 'APPROVED', "
            f"approved_at = ?, joined_at = ? "
            f"WHERE id = ? AND admission_status = 'REQUESTED'",
            (now, now, participant_id),
        )
        if result.rowcount == 0:
            token = None
        else:
            token = _issue_token(connection, participant_id)

    updated = get_participant(engine, participant_id=participant_id)
    assert updated is not None
    return updated, token


def deny_participant(engine: Engine, *, room_id: int,
                     participant_id: int) -> WebParticipant:
    """Refuse a waiting participant. Idempotent and terminal."""
    participant = _participant_in_room(engine, room_id=room_id,
                                       participant_id=participant_id)
    if participant.admission_status == AdmissionStatus.DENIED:
        return participant
    if participant.admission_status in (AdmissionStatus.KICKED,
                                        AdmissionStatus.ROOM_ENDED):
        raise ParticipantNotAdmissibleError(
            f"this participant is {participant.admission_status.lower()}")

    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {PARTICIPANT_TABLE} SET admission_status = 'DENIED', "
            f"denied_at = ?, session_token_hash = NULL WHERE id = ?",
            (_now(), participant_id),
        )
    updated = get_participant(engine, participant_id=participant_id)
    assert updated is not None
    return updated


def kick_participant(engine: Engine, *, room_id: int,
                     participant_id: int) -> WebParticipant:
    """Remove an admitted listener, and invalidate their session.

    Clearing the token hash is what makes this real: the socket is closed by the
    caller, but without this the same token would reconnect a second later.

    This removes a SESSION, not a person. Somebody who clears their browser and
    asks again arrives as a new participant and a new decision - there is no
    device fingerprinting here and no claim of a person-level ban.
    """
    participant = _participant_in_room(engine, room_id=room_id,
                                       participant_id=participant_id)
    if participant.admission_status == AdmissionStatus.KICKED:
        return participant

    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {PARTICIPANT_TABLE} SET admission_status = 'KICKED', "
            f"kicked_at = ?, session_token_hash = NULL WHERE id = ?",
            (_now(), participant_id),
        )
    updated = get_participant(engine, participant_id=participant_id)
    assert updated is not None
    return updated


def mark_participant_left(engine: Engine, *, participant_id: int) -> None:
    """Record a listener closing their own tab. Never overrides a terminal state."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"UPDATE {PARTICIPANT_TABLE} SET admission_status = 'LEFT', "
            f"ended_at = ?, session_token_hash = NULL "
            f"WHERE id = ? AND admission_status IN ('APPROVED', 'PASSWORD_ADMITTED')",
            (_now(), participant_id),
        )


def _participant_in_room(engine: Engine, *, room_id: int,
                         participant_id: int) -> WebParticipant:
    """Fetch a participant, refusing one that belongs to a different room.

    The room check is the isolation boundary: participant ids are small
    integers, so without it one broadcaster could approve or kick another's
    audience by guessing a number.
    """
    participant = get_participant(engine, participant_id=participant_id)
    if participant is None or participant.room_id != room_id:
        raise ParticipantNotAdmissibleError("no such participant in this room")
    return participant


def authenticate_listener(engine: Engine, *,
                          token: Any) -> tuple[WebRoom, WebParticipant] | None:
    """Resolve a listener session token to its room and participant.

    Returns None for anything that is not a currently-admitted listener of a
    currently-open room. Every reason - unknown token, kicked, denied, room
    ended, wrong room - fails the same way, because a caller learning WHICH is a
    caller learning something about a room they are not in.
    """
    if not isinstance(token, str) or not token:
        return None
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT {_PARTICIPANT_COLUMNS} FROM {PARTICIPANT_TABLE} "
            f"WHERE session_token_hash = ?", (_token_hash(token),),
        ).fetchone()
    if row is None:
        return None
    participant = _participant_from_row(row)
    if not participant.is_admitted:
        return None

    with engine.connect() as connection:
        room_row = connection.exec_driver_sql(
            f"SELECT {_ROOM_COLUMNS} FROM {ROOM_TABLE} WHERE id = ?",
            (participant.room_id,),
        ).fetchone()
    if room_row is None:
        return None
    room = _room_from_row(room_row)
    if not room.is_open:
        return None
    return room, participant


def get_room_by_id(engine: Engine, *, room_id: int) -> WebRoom | None:
    """Look a room up by its internal id.

    Used where a participant row is already in hand: the participant knows its
    room, and re-deriving that through the session would be a longer path to
    the same answer.
    """
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            f"SELECT {_ROOM_COLUMNS} FROM {ROOM_TABLE} WHERE id = ?", (room_id,),
        ).fetchone()
    return _room_from_row(row) if row else None
