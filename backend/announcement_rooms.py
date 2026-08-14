"""A listening link for a recorded announcement.

WHY THIS IS NOT THE BROADCAST ROOM

A broadcast room exists only while somebody is holding a microphone: it is
created when the broadcast starts and ends when it ends. An announcement is
the opposite - it runs for days with nobody present, and it is DUCKED for the
whole of any broadcast. Hanging a listener link off the broadcast room would
mean the link only worked at the exact times the announcement was silent.

So this is its own room, with its own life:

  * created deliberately from HQ, not as a side effect of anything;
  * bound to a TEMPLATE, because that is the thing that has a plan and an
    expiry - a link to a bare recording would outlive the campaign it belongs
    to;
  * open until somebody closes it, or until the template's window ends.

WHAT A LISTENER ACTUALLY HEARS

The recording, played by their own browser, from its own beginning. NOT a
mirror of what a particular shop's speaker is doing at that instant, and the
interface says so: two people opening the link a minute apart are a minute
apart in the audio, and there is no way to make a file downloaded twice be in
sync without streaming it, which is a different product.

What the link DOES follow is whether the announcement is running at all. A
paused template pauses the page, because the alternative - a link that keeps
playing a campaign HQ has stopped - is the one failure that would embarrass
somebody in front of a customer.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

TABLE = "announcement_rooms"

#: The same shapes the broadcast room uses, so a code read out over a phone
#: looks like the codes people here already read out over a phone.
PUBLIC_CODE_PREFIX = "AN-"
PUBLIC_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PUBLIC_CODE_LENGTH = 6
PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD_GROUP_LENGTH = 4
PASSWORD_GROUPS = 2
LISTENER_TOKEN_BYTES = 32

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_announcement_room_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id INTEGER NOT NULL,
                -- Random and public. Never derived from the template id: a
                -- guessable link is a link anybody can guess.
                public_code VARCHAR(24) NOT NULL UNIQUE,
                -- bcrypt. There is no column the plaintext could be read from,
                -- which is why creating a room returns it once and a later
                -- page cannot show it again.
                password_hash VARCHAR(255) NOT NULL,
                label VARCHAR(120) NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                created_by INTEGER,
                created_at VARCHAR(40) NOT NULL,
                closed_at VARCHAR(40),
                closed_by INTEGER
            )
            """
        )
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE}_listeners (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                -- SHA-256, not bcrypt: a listener token is already 32 bytes
                -- from secrets, so there is nothing to guess and no work
                -- factor to justify on a value checked on every poll.
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                display_name VARCHAR(80) NOT NULL DEFAULT '',
                joined_at VARCHAR(40) NOT NULL,
                last_seen_at VARCHAR(40)
            )
            """
        )
        for statement in (
            f"CREATE INDEX IF NOT EXISTS ix_announcement_rooms_template "
            f"ON {TABLE}(template_id)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_rooms_status "
            f"ON {TABLE}(status)",
            f"CREATE INDEX IF NOT EXISTS ix_announcement_room_listeners_room "
            f"ON {TABLE}_listeners(room_id)",
        ):
            connection.exec_driver_sql(statement)


class RoomRefused(ValueError):
    """A refusal with a sentence the person reading it can act on."""


def generate_public_code() -> str:
    body = "".join(secrets.choice(PUBLIC_CODE_ALPHABET)
                   for _ in range(PUBLIC_CODE_LENGTH))
    return f"{PUBLIC_CODE_PREFIX}{body}"


def generate_join_password() -> str:
    """Grouped so it can be read out over a phone without spelling every
    character twice."""
    return "-".join("".join(secrets.choice(PASSWORD_ALPHABET)
                            for _ in range(PASSWORD_GROUP_LENGTH))
                    for _ in range(PASSWORD_GROUPS))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row(engine: Engine, sql: str, **parameters):
    with engine.connect() as connection:
        found = connection.execute(text(sql), parameters).first()
    return dict(found._mapping) if found else None


def create_room(engine: Engine, *, template_id: int, label: str,
                created_by: int, hash_password) -> tuple[dict, str]:
    """Open a link for this template. Returns the room and the ONE-TIME password.

    The plaintext is returned exactly once and never stored. A page that could
    show it again would make "who has this link" unanswerable - and the honest
    answer to "I lost the password" is a new one, which is why closing and
    reopening is cheap.
    """
    password = generate_join_password()
    code = generate_public_code()
    with engine.begin() as connection:
        result = connection.execute(text(
            f"INSERT INTO {TABLE} (template_id, public_code, password_hash, "
            "label, status, created_by, created_at) "
            "VALUES (:template_id, :code, :password_hash, :label, :status, "
            "        :created_by, :now)"),
            {"template_id": template_id, "code": code,
             "password_hash": hash_password(password), "label": label[:120],
             "status": STATUS_OPEN, "created_by": created_by, "now": utcnow()})
        room_id = result.lastrowid
    return get_room(engine, room_id=room_id), password


def get_room(engine: Engine, *, room_id: int) -> dict | None:
    return _row(engine, f"SELECT * FROM {TABLE} WHERE id = :id", id=room_id)


def get_room_by_code(engine: Engine, *, code: str) -> dict | None:
    return _row(engine, f"SELECT * FROM {TABLE} WHERE public_code = :code",
                code=(code or "").strip().upper())


def list_rooms(engine: Engine, *, template_id: int | None = None) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"SELECT r.*, t.name AS template_name, "
            f"       (SELECT COUNT(*) FROM {TABLE}_listeners l "
            "         WHERE l.room_id = r.id) AS listener_count "
            f"FROM {TABLE} r "
            "LEFT JOIN announcement_templates t ON t.id = r.template_id "
            "ORDER BY r.id DESC")).fetchall()
    found = [dict(row._mapping) for row in rows]
    if template_id is not None:
        found = [row for row in found if row["template_id"] == template_id]
    return found


def close_room(engine: Engine, *, room_id: int, closed_by: int) -> dict | None:
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {TABLE} SET status = :status, closed_at = :now, "
            "closed_by = :closed_by WHERE id = :id"),
            {"status": STATUS_CLOSED, "now": utcnow(), "closed_by": closed_by,
             "id": room_id})
        # Every admitted listener is turned away at the same moment. A closed
        # room whose existing listeners kept playing would be a link that
        # cannot actually be withdrawn.
        connection.execute(text(
            f"DELETE FROM {TABLE}_listeners WHERE room_id = :id"), {"id": room_id})
    return get_room(engine, room_id=room_id)


def admit(engine: Engine, *, code: str, password: str, display_name: str,
          verify_password) -> tuple[dict, str]:
    """Check the code and password, and hand back a listener token.

    One refusal for a wrong code and a wrong password, deliberately: telling a
    stranger that a code exists but the password is wrong is telling them
    which half to keep guessing at.
    """
    room = get_room_by_code(engine, code=code)
    refusal = RoomRefused(
        "That listening ID or password is not right. Check both with whoever "
        "sent you the link.")
    if room is None or room["status"] != STATUS_OPEN:
        raise refusal
    if not verify_password(password or "", room["password_hash"]):
        raise refusal

    token = secrets.token_urlsafe(LISTENER_TOKEN_BYTES)
    with engine.begin() as connection:
        connection.execute(text(
            f"INSERT INTO {TABLE}_listeners (room_id, token_hash, display_name, "
            "joined_at, last_seen_at) "
            "VALUES (:room_id, :token_hash, :display_name, :now, :now)"),
            {"room_id": room["id"], "token_hash": token_hash(token),
             "display_name": (display_name or "").strip()[:80] or "Listener",
             "now": utcnow()})
    return room, token


def listener_for_token(engine: Engine, *, token: str) -> dict | None:
    """The listener AND their room, or None. Touches last_seen_at.

    A single lookup rather than two, because "the token is valid but the room
    has been closed" must not be a state any caller can accidentally treat as
    admitted.
    """
    if not token:
        return None
    row = _row(engine,
               f"SELECT l.*, r.status AS room_status, r.template_id, "
               f"       r.public_code, r.label "
               f"FROM {TABLE}_listeners l JOIN {TABLE} r ON r.id = l.room_id "
               "WHERE l.token_hash = :token_hash",
               token_hash=token_hash(token))
    if row is None or row["room_status"] != STATUS_OPEN:
        return None
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {TABLE}_listeners SET last_seen_at = :now WHERE id = :id"),
            {"now": utcnow(), "id": row["id"]})
    return row


def leave(engine: Engine, *, token: str) -> None:
    with engine.begin() as connection:
        connection.execute(text(
            f"DELETE FROM {TABLE}_listeners WHERE token_hash = :token_hash"),
            {"token_hash": token_hash(token)})
