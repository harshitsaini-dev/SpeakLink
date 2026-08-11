"""Chat between a Broadcast's web audience and the operator hosting it.

WHAT THIS IS FOR

A web listener can hear the announcement but has no way to say "we cannot hear
you" or "repeat the last part". Chat is that channel, and nothing more: it is
attached to ONE Broadcast, it is readable for exactly as long as that
Broadcast's history is, and it dies with it.

FOUR DECISIONS WORTH READING BEFORE CHANGING ANYTHING HERE

1. RETENTION IS THE BROADCAST'S, NOT ITS OWN. Messages hang off the web room,
   which hangs off the broadcast session, both ON DELETE CASCADE. Delete the
   broadcast from history and the chat goes with it in the same transaction -
   there is no second cleanup job to forget to run, and no orphaned record of
   what someone typed surviving the thing it was about.

2. PRIVATE IS ENFORCED HERE, NOT IN THE UI. In PRIVATE mode a listener's
   message is addressed to the host alone. That is a property of the stored
   row (visibility), so a listener who reconnects, replays history, or calls
   the API directly still cannot read another listener's private message. A
   mode implemented only in the fanout would leak the moment anyone refetched.

3. THE HOST IS NEVER SILENCED. chat_enabled and per-listener mutes apply to
   listeners. An operator turning chat off is stopping the audience from
   typing, not gagging themselves - they may still need to answer the last
   question before the room goes quiet.

4. DELETION IS A TOMBSTONE, NOT A DELETE. A removed message keeps its row and
   its author, and loses its body. Everyone saw it; pretending it never
   existed would make the transcript a lie, and an operator who deletes
   something should still be able to say what they deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

MESSAGE_TABLE = "web_chat_messages"

#: Long enough for a real question, short enough that a paste of a novel is
#: refused rather than truncated - a truncated message changes what somebody
#: said, which is worse than declining it.
MAX_BODY = 500

#: Listener rate limit: how many messages in how many seconds. Deliberately
#: generous for a person and useless for a script. Enforced against stored
#: timestamps rather than in-memory counters, so restarting HQ is not a way to
#: reset it and a second worker cannot double the allowance.
RATE_LIMIT_MESSAGES = 5
RATE_LIMIT_SECONDS = 10

PUBLIC = "PUBLIC"
PRIVATE = "PRIVATE"
CHAT_MODES = (PUBLIC, PRIVATE)

HOST = "HOST"
LISTENER = "LISTENER"


class ChatRefused(RuntimeError):
    """A message that will not be stored, with a reason fit to show a person."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChatMessage:
    id: int
    room_id: int
    participant_id: int | None
    author_kind: str
    author_name: str
    body: str | None
    visibility: str
    created_at: str
    deleted_at: str | None

    def public_dict(self) -> dict[str, Any]:
        """What goes over the wire. Never a token, never a participant's id
        beyond the one the client already knows is its own."""
        return {
            "id": self.id,
            "participant_id": self.participant_id,
            "author_kind": self.author_kind,
            "author_name": self.author_name,
            # A deleted message keeps its place and its author and loses its
            # words. The client renders "removed by the host".
            "body": None if self.deleted_at else self.body,
            "deleted": bool(self.deleted_at),
            "visibility": self.visibility,
            "created_at": self.created_at,
        }


def ensure_chat_schema(engine: Engine) -> None:
    """Create the message table and the room/participant chat columns.

    Purely additive and safe on every boot. The columns live on the existing
    tables rather than in a settings table of their own: chat is a property of
    a room, and a second table would be a second thing to keep in step.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {MESSAGE_TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id INTEGER NOT NULL,
                -- NULL for the host: the operator is not a room participant.
                participant_id INTEGER,
                author_kind VARCHAR(16) NOT NULL,
                -- The name AS IT WAS when the message was sent. A listener who
                -- rejoins under another name must not retroactively rewrite
                -- who said what.
                author_name VARCHAR(80) NOT NULL,
                body VARCHAR({MAX_BODY}),
                visibility VARCHAR(16) NOT NULL DEFAULT 'PUBLIC',
                created_at VARCHAR(40) NOT NULL,
                deleted_at VARCHAR(40),
                deleted_by_user_id INTEGER,
                CONSTRAINT fk_chat_room
                    FOREIGN KEY (room_id) REFERENCES web_rooms (id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_chat_participant
                    FOREIGN KEY (participant_id) REFERENCES web_participants (id)
                    ON DELETE CASCADE,
                CONSTRAINT ck_chat_author CHECK (
                    author_kind IN ('HOST', 'LISTENER')),
                CONSTRAINT ck_chat_visibility CHECK (
                    visibility IN ('PUBLIC', 'PRIVATE'))
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_chat_room_created "
            f"ON {MESSAGE_TABLE} (room_id, id)"
        )

        existing = {
            row[1] for row in connection.exec_driver_sql(
                "PRAGMA table_info(web_rooms)")
        } if engine.dialect.name == "sqlite" else _columns(connection, "web_rooms")
        if "chat_enabled" not in existing:
            connection.exec_driver_sql(
                "ALTER TABLE web_rooms ADD COLUMN chat_enabled INTEGER "
                "NOT NULL DEFAULT 1")
        if "chat_mode" not in existing:
            connection.exec_driver_sql(
                "ALTER TABLE web_rooms ADD COLUMN chat_mode VARCHAR(16) "
                "NOT NULL DEFAULT 'PUBLIC'")

        participant_columns = {
            row[1] for row in connection.exec_driver_sql(
                "PRAGMA table_info(web_participants)")
        } if engine.dialect.name == "sqlite" else _columns(
            connection, "web_participants")
        if "chat_muted_at" not in participant_columns:
            connection.exec_driver_sql(
                "ALTER TABLE web_participants ADD COLUMN chat_muted_at "
                "VARCHAR(40)")


def _columns(connection, table: str) -> set[str]:
    rows = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :t"), {"t": table})
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Room settings
# ---------------------------------------------------------------------------

def get_settings(engine: Engine, *, room_id: int) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT chat_enabled, chat_mode FROM web_rooms WHERE id = :r"),
            {"r": room_id}).first()
    if row is None:
        return {"chat_enabled": False, "chat_mode": PUBLIC}
    return {"chat_enabled": bool(row[0]), "chat_mode": row[1] or PUBLIC}


def set_chat_enabled(engine: Engine, *, room_id: int, enabled: bool) -> dict:
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE web_rooms SET chat_enabled = :v WHERE id = :r"),
            {"v": 1 if enabled else 0, "r": room_id})
    return get_settings(engine, room_id=room_id)


def set_chat_mode(engine: Engine, *, room_id: int, mode: str) -> dict:
    if mode not in CHAT_MODES:
        raise ChatRefused("Chat mode must be PUBLIC or PRIVATE.")
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE web_rooms SET chat_mode = :m WHERE id = :r"),
            {"m": mode, "r": room_id})
    # Deliberately does NOT rewrite existing messages. A message sent while the
    # room was private was sent in confidence, and flipping the room to public
    # must not publish it after the fact. The reverse matters too: hiding what
    # was already said in public fools nobody who was in the room.
    return get_settings(engine, room_id=room_id)


def set_participant_muted(engine: Engine, *, participant_id: int,
                          muted: bool) -> bool:
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE web_participants SET chat_muted_at = :v WHERE id = :p"),
            {"v": _now() if muted else None, "p": participant_id})
    return bool(result.rowcount)


def is_participant_muted(engine: Engine, *, participant_id: int) -> bool:
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT chat_muted_at FROM web_participants WHERE id = :p"),
            {"p": participant_id}).first()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def clean_body(raw: Any) -> str:
    """Normalise a message, or refuse it.

    No HTML is stripped or escaped here. The body is stored as typed and the
    client renders it as TEXT - escaping on the way in would corrupt a message
    that legitimately contains < or &, and it is the rendering that decides
    whether markup executes. Control characters go, because they exist only to
    make a transcript lie about its own layout.
    """
    if not isinstance(raw, str):
        raise ChatRefused("A message must be text.")
    body = "".join(ch for ch in raw if ch == "\n" or ch >= " ")
    body = body.strip()
    if not body:
        raise ChatRefused("A message cannot be empty.")
    if len(body) > MAX_BODY:
        # Refused rather than truncated: a truncated message changes what
        # somebody said.
        raise ChatRefused(f"A message cannot be longer than {MAX_BODY} characters.")
    return body


def _rate_limited(connection, *, participant_id: int) -> bool:
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=RATE_LIMIT_SECONDS)).isoformat()
    recent = connection.execute(text(
        f"SELECT COUNT(*) FROM {MESSAGE_TABLE} "
        "WHERE participant_id = :p AND created_at >= :cutoff"),
        {"p": participant_id, "cutoff": cutoff}).scalar_one()
    return recent >= RATE_LIMIT_MESSAGES


def post_listener_message(engine: Engine, *, room_id: int, participant_id: int,
                          display_name: str, body: Any) -> ChatMessage:
    """Store one listener message, or refuse it with a reason.

    Every refusal here is also a refusal the client already knows about and
    should have prevented. It is repeated anyway, because a control that only
    exists in a browser is a suggestion.
    """
    text_body = clean_body(body)
    settings = get_settings(engine, room_id=room_id)
    if not settings["chat_enabled"]:
        raise ChatRefused("The host has turned chat off.")
    if is_participant_muted(engine, participant_id=participant_id):
        raise ChatRefused("The host has muted you in this chat.")

    with engine.begin() as connection:
        if _rate_limited(connection, participant_id=participant_id):
            raise ChatRefused(
                f"Too many messages. Wait a few seconds and try again.")
        visibility = PRIVATE if settings["chat_mode"] == PRIVATE else PUBLIC
        created = _now()
        result = connection.execute(text(
            f"INSERT INTO {MESSAGE_TABLE} "
            "(room_id, participant_id, author_kind, author_name, body, "
            " visibility, created_at) "
            "VALUES (:room, :participant, 'LISTENER', :name, :body, "
            "        :visibility, :created)"),
            {"room": room_id, "participant": participant_id,
             "name": display_name, "body": text_body,
             "visibility": visibility, "created": created})
        message_id = result.lastrowid
    return ChatMessage(
        id=message_id, room_id=room_id, participant_id=participant_id,
        author_kind=LISTENER, author_name=display_name, body=text_body,
        visibility=visibility, created_at=created, deleted_at=None)


def post_host_message(engine: Engine, *, room_id: int, display_name: str,
                      body: Any) -> ChatMessage:
    """The host speaking to the room. Always public, never rate limited.

    Not subject to chat_enabled either: turning chat off stops the audience
    typing, and an operator may still need to answer the last question before
    the room goes quiet.
    """
    text_body = clean_body(body)
    created = _now()
    with engine.begin() as connection:
        result = connection.execute(text(
            f"INSERT INTO {MESSAGE_TABLE} "
            "(room_id, participant_id, author_kind, author_name, body, "
            " visibility, created_at) "
            "VALUES (:room, NULL, 'HOST', :name, :body, 'PUBLIC', :created)"),
            {"room": room_id, "name": display_name, "body": text_body,
             "created": created})
        message_id = result.lastrowid
    return ChatMessage(
        id=message_id, room_id=room_id, participant_id=None, author_kind=HOST,
        author_name=display_name, body=text_body, visibility=PUBLIC,
        created_at=created, deleted_at=None)


def delete_message(engine: Engine, *, message_id: int, room_id: int,
                   actor_user_id: int) -> bool:
    """Tombstone one message. The row and the author stay; the words go."""
    with engine.begin() as connection:
        result = connection.execute(text(
            f"UPDATE {MESSAGE_TABLE} SET deleted_at = :now, "
            "deleted_by_user_id = :actor, body = NULL "
            "WHERE id = :id AND room_id = :room AND deleted_at IS NULL"),
            {"now": _now(), "actor": actor_user_id, "id": message_id,
             "room": room_id})
    return bool(result.rowcount)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _row_to_message(row) -> ChatMessage:
    return ChatMessage(
        id=row[0], room_id=row[1], participant_id=row[2], author_kind=row[3],
        author_name=row[4], body=row[5], visibility=row[6], created_at=row[7],
        deleted_at=row[8])


_SELECT = (f"SELECT id, room_id, participant_id, author_kind, author_name, "
           f"body, visibility, created_at, deleted_at FROM {MESSAGE_TABLE}")


def history_for_host(engine: Engine, *, room_id: int,
                     limit: int = 200) -> list[ChatMessage]:
    """Everything in the room. The host is the one person a private message
    was addressed TO, so private messages are theirs to read."""
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"{_SELECT} WHERE room_id = :room ORDER BY id DESC LIMIT :limit"),
            {"room": room_id, "limit": limit}).all()
    return [_row_to_message(row) for row in reversed(rows)]


def history_for_listener(engine: Engine, *, room_id: int, participant_id: int,
                         limit: int = 200) -> list[ChatMessage]:
    """What THIS listener is entitled to see.

    Public messages, plus their own private ones. Enforced in the query, not
    by filtering afterwards - a filter is one early return away from being
    skipped, and what it would leak is somebody else's private message.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"{_SELECT} WHERE room_id = :room AND ("
            "   visibility = 'PUBLIC' OR participant_id = :me) "
            "ORDER BY id DESC LIMIT :limit"),
            {"room": room_id, "me": participant_id, "limit": limit}).all()
    return [_row_to_message(row) for row in reversed(rows)]


def recipients_of(message: ChatMessage) -> str:
    """Who a stored message may be delivered to: 'ROOM' or 'HOST_AND_AUTHOR'.

    One place decides this, so the live fanout and the replayed history cannot
    disagree about who was allowed to see something.
    """
    return "ROOM" if message.visibility == PUBLIC else "HOST_AND_AUTHOR"
