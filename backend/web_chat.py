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

4. DELETION REMOVES A MESSAGE FROM THE ROOM, IT DOES NOT ERASE IT. Listeners
   see a tombstone with no words and no picture. The host - and anyone
   entitled to read that Broadcast's history - still sees what was said,
   marked as removed, because "somebody posted something and I deleted it" is
   not an answer an operator can give a manager an hour later.

   The cost is real and is stated rather than hidden: a removed message is
   RETAINED until the Broadcast itself is deleted from history. Delete is not
   a way to make something unrecoverable; deleting the Broadcast is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

# Imported rather than repeated. The room tables are named broadcast_web_rooms
# and broadcast_web_participants, not web_rooms - a second copy of those names
# here is a second place to be wrong, and being wrong looks like "no such
# table" at boot rather than at review.
from web_rooms import PARTICIPANT_TABLE, ROOM_TABLE

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
    attachment_name: str | None = None
    attachment_mime: str | None = None
    attachment_bytes: int | None = None
    attachment_width: int | None = None
    attachment_height: int | None = None

    def public_dict(self, *, reveal_removed: bool = False) -> dict[str, Any]:
        """What goes over the wire. Never a token, never a participant's id
        beyond the one the client already knows is its own.

        ``reveal_removed`` is the difference between the two readers. A
        listener is given a tombstone - the message is gone from the room, and
        that is what removal means to them. The host is given the words,
        marked as removed, because they are the person who has to account for
        the removal afterwards.

        The flag is passed by the ROUTE, from the permission it already
        checked. It is deliberately not derived from anything in this object:
        a message cannot decide who is allowed to read it.
        """
        removed = bool(self.deleted_at)
        hidden = removed and not reveal_removed
        return {
            "id": self.id,
            "participant_id": self.participant_id,
            "author_kind": self.author_kind,
            "author_name": self.author_name,
            "body": None if hidden else self.body,
            "deleted": removed,
            "visibility": self.visibility,
            "created_at": self.created_at,
            # The bytes are never in here. A client that may see this message
            # fetches the image from its own endpoint, which applies the same
            # visibility rule - so a private photograph is not readable by
            # guessing a URL. A deleted message has no image left at all.
            "has_image": bool(self.attachment_name) and not hidden,
            "image_mime": None if hidden else self.attachment_mime,
            "image_width": None if hidden else self.attachment_width,
            "image_height": None if hidden else self.attachment_height,
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
                -- An image sent with (or instead of) the text. Stored as the
                -- file's random name plus what it is - never the caller's
                -- filename, and never the bytes: the database is not a blob
                -- store and a transcript query should not drag megabytes.
                attachment_name VARCHAR(80),
                attachment_mime VARCHAR(40),
                attachment_bytes INTEGER,
                attachment_width INTEGER,
                attachment_height INTEGER,
                deleted_at VARCHAR(40),
                deleted_by_user_id INTEGER,
                CONSTRAINT fk_chat_room
                    FOREIGN KEY (room_id) REFERENCES {ROOM_TABLE} (id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_chat_participant
                    FOREIGN KEY (participant_id) REFERENCES {PARTICIPANT_TABLE} (id)
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

        message_columns = {
            row[1] for row in connection.exec_driver_sql(
                f"PRAGMA table_info({MESSAGE_TABLE})")
        } if engine.dialect.name == "sqlite" else _columns(connection, MESSAGE_TABLE)
        for column, ddl in (("attachment_name", "VARCHAR(80)"),
                            ("attachment_mime", "VARCHAR(40)"),
                            ("attachment_bytes", "INTEGER"),
                            ("attachment_width", "INTEGER"),
                            ("attachment_height", "INTEGER")):
            if column not in message_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {column} {ddl}")

        existing = {
            row[1] for row in connection.exec_driver_sql(
                f"PRAGMA table_info({ROOM_TABLE})")
        } if engine.dialect.name == "sqlite" else _columns(connection, ROOM_TABLE)
        if "chat_enabled" not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE {ROOM_TABLE} ADD COLUMN chat_enabled INTEGER "
                "NOT NULL DEFAULT 1")
        if "chat_mode" not in existing:
            connection.exec_driver_sql(
                f"ALTER TABLE {ROOM_TABLE} ADD COLUMN chat_mode VARCHAR(16) "
                "NOT NULL DEFAULT 'PUBLIC'")

        participant_columns = {
            row[1] for row in connection.exec_driver_sql(
                f"PRAGMA table_info({PARTICIPANT_TABLE})")
        } if engine.dialect.name == "sqlite" else _columns(
            connection, PARTICIPANT_TABLE)
        if "chat_muted_at" not in participant_columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {PARTICIPANT_TABLE} ADD COLUMN chat_muted_at "
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
            f"SELECT chat_enabled, chat_mode FROM {ROOM_TABLE} WHERE id = :r"),
            {"r": room_id}).first()
    if row is None:
        return {"chat_enabled": False, "chat_mode": PUBLIC}
    return {"chat_enabled": bool(row[0]), "chat_mode": row[1] or PUBLIC}


def set_chat_enabled(engine: Engine, *, room_id: int, enabled: bool) -> dict:
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {ROOM_TABLE} SET chat_enabled = :v WHERE id = :r"),
            {"v": 1 if enabled else 0, "r": room_id})
    return get_settings(engine, room_id=room_id)


def set_chat_mode(engine: Engine, *, room_id: int, mode: str) -> dict:
    if mode not in CHAT_MODES:
        raise ChatRefused("Chat mode must be PUBLIC or PRIVATE.")
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {ROOM_TABLE} SET chat_mode = :m WHERE id = :r"),
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
            f"UPDATE {PARTICIPANT_TABLE} SET chat_muted_at = :v WHERE id = :p"),
            {"v": _now() if muted else None, "p": participant_id})
    return bool(result.rowcount)


def is_participant_muted(engine: Engine, *, participant_id: int) -> bool:
    with engine.connect() as connection:
        row = connection.execute(text(
            f"SELECT chat_muted_at FROM {PARTICIPANT_TABLE} WHERE id = :p"),
            {"p": participant_id}).first()
    return bool(row and row[0])


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _attachment_params(attachment: dict | None) -> dict:
    """The five attachment bind parameters, present whether or not there is one."""
    attachment = attachment or {}
    return {"aname": attachment.get("attachment_name"),
            "amime": attachment.get("attachment_mime"),
            "abytes": attachment.get("attachment_bytes"),
            "awidth": attachment.get("attachment_width"),
            "aheight": attachment.get("attachment_height")}


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
                          display_name: str, body: Any,
                          attachment: dict | None = None) -> ChatMessage:
    """Store one listener message, or refuse it with a reason.

    Every refusal here is also a refusal the client already knows about and
    should have prevented. It is repeated anyway, because a control that only
    exists in a browser is a suggestion.
    """
    # With an image, a caption is optional - the picture IS the message.
    # Without one, an empty message is still nothing to send.
    text_body = clean_body(body) if (body or not attachment) else None
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
            " visibility, created_at, attachment_name, attachment_mime, "
            " attachment_bytes, attachment_width, attachment_height) "
            "VALUES (:room, :participant, 'LISTENER', :name, :body, "
            "        :visibility, :created, :aname, :amime, :abytes, "
            "        :awidth, :aheight)"),
            {"room": room_id, "participant": participant_id,
             "name": display_name, "body": text_body,
             "visibility": visibility, "created": created,
             **_attachment_params(attachment)})
        message_id = result.lastrowid
    return ChatMessage(
        id=message_id, room_id=room_id, participant_id=participant_id,
        author_kind=LISTENER, author_name=display_name, body=text_body,
        visibility=visibility, created_at=created, deleted_at=None,
        **(attachment or {}))


def post_host_message(engine: Engine, *, room_id: int, display_name: str,
                      body: Any, attachment: dict | None = None) -> ChatMessage:
    """The host speaking to the room. Always public, never rate limited.

    Not subject to chat_enabled either: turning chat off stops the audience
    typing, and an operator may still need to answer the last question before
    the room goes quiet.
    """
    text_body = clean_body(body) if (body or not attachment) else None
    created = _now()
    with engine.begin() as connection:
        result = connection.execute(text(
            f"INSERT INTO {MESSAGE_TABLE} "
            "(room_id, participant_id, author_kind, author_name, body, "
            " visibility, created_at, attachment_name, attachment_mime, "
            " attachment_bytes, attachment_width, attachment_height) "
            "VALUES (:room, NULL, 'HOST', :name, :body, 'PUBLIC', :created, "
            "        :aname, :amime, :abytes, :awidth, :aheight)"),
            {"room": room_id, "name": display_name, "body": text_body,
             "created": created, **_attachment_params(attachment)})
        message_id = result.lastrowid
    return ChatMessage(
        id=message_id, room_id=room_id, participant_id=None, author_kind=HOST,
        author_name=display_name, body=text_body, visibility=PUBLIC,
        created_at=created, deleted_at=None, **(attachment or {}))


def get_message(engine: Engine, *, message_id: int,
                room_id: int) -> ChatMessage | None:
    """One message, by id, WITHIN one room.

    Scoped to the room deliberately: the image endpoints resolve a message
    this way, and a lookup by id alone would let somebody in one Broadcast
    fetch an attachment from another by guessing a number.
    """
    with engine.connect() as connection:
        row = connection.execute(text(
            f"{_SELECT} WHERE id = :id AND room_id = :room"),
            {"id": message_id, "room": room_id}).first()
    return _row_to_message(row) if row is not None else None


def delete_message(engine: Engine, *, message_id: int, room_id: int,
                   actor_user_id: int) -> bool:
    """Remove one message from the room.

    Nothing is erased. The body and the image stay on the row, and who removed
    it and when are recorded; what changes is who may read them - see
    ``public_dict``. The alternative, clearing the columns, left an operator
    unable to say what they had removed five minutes later.
    """
    with engine.begin() as connection:
        result = connection.execute(text(
            f"UPDATE {MESSAGE_TABLE} SET deleted_at = :now, "
            "deleted_by_user_id = :actor "
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
        deleted_at=row[8], attachment_name=row[9], attachment_mime=row[10],
        attachment_bytes=row[11], attachment_width=row[12],
        attachment_height=row[13])


_SELECT = (f"SELECT id, room_id, participant_id, author_kind, author_name, "
           f"body, visibility, created_at, deleted_at, attachment_name, "
           f"attachment_mime, attachment_bytes, attachment_width, "
           f"attachment_height FROM {MESSAGE_TABLE}")


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
