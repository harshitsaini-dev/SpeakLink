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
                --
                -- NOT unique on its own. A code belongs to a link while that
                -- link is OPEN; once it is closed the code is free again, so
                -- the replacement link everybody has already been told about
                -- can carry the same ID. The rule is enforced exactly, by a
                -- partial unique index over the open rows, rather than
                -- approximately by a column constraint that also reserves
                -- every code ever withdrawn.
                public_code VARCHAR(24) NOT NULL,
                -- bcrypt. There is no column the plaintext could be read from,
                -- which is why creating a room returns it once and a later
                -- page cannot show it again.
                password_hash VARCHAR(255) NOT NULL,
                label VARCHAR(120) NOT NULL DEFAULT '',
                status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                created_by INTEGER,
                created_at VARCHAR(40) NOT NULL,
                closed_at VARCHAR(40),
                closed_by INTEGER,
                -- 0 means anybody with the URL can listen.
                --
                -- Its own column rather than an empty password, so that
                -- "no password" is a state somebody chose and a reader can
                -- see, not the accidental result of a blank field. Every
                -- other code path still compares against a real hash.
                requires_password INTEGER NOT NULL DEFAULT 1
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
        # Additive for a database created before password-free links existed.
        columns = {row[1] for row in connection.exec_driver_sql(
            f"PRAGMA table_info({TABLE})")} if engine.dialect.name == 'sqlite' else set()
        if columns and 'requires_password' not in columns:
            connection.exec_driver_sql(
                f"ALTER TABLE {TABLE} ADD COLUMN requires_password "
                "INTEGER NOT NULL DEFAULT 1")

        # A database created before closed codes could be reused still has
        # UNIQUE(public_code) baked into the table. SQLite cannot drop a
        # column constraint, so the table is rebuilt - carefully, because a
        # rebuild that forgets an index is how enrolment broke once before:
        # every index is recreated below, unconditionally, for both the fresh
        # and the rebuilt table.
        if engine.dialect.name == "sqlite":
            indexes = list(connection.exec_driver_sql(
                f"PRAGMA index_list({TABLE})"))
            has_unique_code = False
            for index in indexes:
                name, unique = index[1], index[2]
                if not unique:
                    continue
                columns_in_index = [row[2] for row in connection.exec_driver_sql(
                    f"PRAGMA index_info('{name}')")]
                # The auto-index for the column constraint covers exactly
                # public_code; the partial index this code creates is named,
                # and is skipped.
                if columns_in_index == ["public_code"] and name.startswith("sqlite_autoindex"):
                    has_unique_code = True
            if has_unique_code:
                connection.exec_driver_sql(f"ALTER TABLE {TABLE} RENAME TO {TABLE}_old")
                connection.exec_driver_sql(
                    f"""
                    CREATE TABLE {TABLE} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        template_id INTEGER NOT NULL,
                        public_code VARCHAR(24) NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        label VARCHAR(120) NOT NULL DEFAULT '',
                        status VARCHAR(16) NOT NULL DEFAULT 'OPEN',
                        created_by INTEGER,
                        created_at VARCHAR(40) NOT NULL,
                        closed_at VARCHAR(40),
                        closed_by INTEGER,
                        requires_password INTEGER NOT NULL DEFAULT 1
                    )
                    """
                )
                connection.exec_driver_sql(
                    f"INSERT INTO {TABLE} (id, template_id, public_code, "
                    "password_hash, label, status, created_by, created_at, "
                    "closed_at, closed_by, requires_password) "
                    "SELECT id, template_id, public_code, password_hash, "
                    "label, status, created_by, created_at, closed_at, "
                    f"closed_by, requires_password FROM {TABLE}_old")
                connection.exec_driver_sql(f"DROP TABLE {TABLE}_old")

        for statement in (
            # The real rule, stated exactly: one OPEN link per code, and no
            # claim at all on the codes of links that have been withdrawn.
            f"CREATE UNIQUE INDEX IF NOT EXISTS ux_announcement_rooms_open_code "
            f"ON {TABLE}(public_code) WHERE status = 'OPEN'",
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


def validate_chosen_code(code: str) -> str:
    """A code somebody typed themselves.

    Uppercased and checked, not merely accepted. Two reasons: a code is read
    out over a phone, so a lowercase l next to a 1 is a support call; and a
    code that can contain anything can contain a space, a slash or an entire
    URL, none of which survive being pasted into the ID field.
    """
    cleaned = (code or "").strip().upper()
    if not cleaned:
        raise RoomRefused("A listening ID cannot be empty.")
    if not cleaned.startswith(PUBLIC_CODE_PREFIX):
        cleaned = f"{PUBLIC_CODE_PREFIX}{cleaned}"
    body = cleaned[len(PUBLIC_CODE_PREFIX):]
    if not 3 <= len(body) <= 20:
        raise RoomRefused(
            "A listening ID needs between 3 and 20 characters after "
            f"{PUBLIC_CODE_PREFIX}.")
    # The full alphabet, not the generator's. PUBLIC_CODE_ALPHABET leaves out
    # I, O and L so a MACHINE never coins a code that gets misread; a person
    # who typed DIWALI meant DIWALI, and refusing it would be this code
    # imposing its own convenience on their word.
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
    if any(character not in allowed for character in body):
        raise RoomRefused(
            "A listening ID may use letters, numbers and hyphens only - it "
            "gets read out over the phone.")
    return cleaned


def validate_chosen_password(password: str) -> str:
    """A password somebody typed themselves.

    Short is allowed - this guards a recorded advertisement, not an account -
    but empty is not, because an empty password is a link with no password
    and that is a different thing with its own switch.
    """
    cleaned = (password or "").strip()
    if len(cleaned) < 4:
        raise RoomRefused(
            "A listening password needs at least 4 characters. To share "
            "something with no password at all, tick 'no password' instead - "
            "that is a decision worth making on purpose.")
    return cleaned


def create_room(engine: Engine, *, template_id: int, label: str,
                created_by: int, hash_password, code: str | None = None,
                password: str | None = None,
                no_password: bool = False) -> tuple[dict, str]:
    """Open a link for this template. Returns the room and the join password.

    A chosen ID and password are allowed, because a code somebody can say out
    loud is worth more than a random one nobody can. A generated one is still
    the default: it is unguessable, and most links are pasted rather than
    dictated.

    ``no_password`` opens a link anybody with the URL can use. It is a
    separate argument rather than an empty password so that it cannot happen
    by accident - see the column comment on requires_password.
    """
    if no_password:
        # Stored as a real random secret that nobody is told, so every code
        # path below still compares a hash against something. A sentinel like
        # "" would mean an empty submitted password matched, which is the
        # opposite of what a reader of this line expects.
        password = secrets.token_urlsafe(32)
    elif password is not None:
        password = validate_chosen_password(password)
    else:
        password = generate_join_password()
    code = validate_chosen_code(code) if code else generate_public_code()

    # Only an OPEN link holds its ID.
    #
    # A closed link is withdrawn: nobody can join it, its tokens are dead, and
    # its rows exist for the record. Letting it keep AN-DIWALI forever meant
    # that closing a link and opening the replacement everybody had already
    # been told about was impossible - which made the ID somebody chose a
    # one-use thing, exactly the opposite of why they chose it.
    clash = get_room_by_code(engine, code=code)
    if clash is not None and clash["status"] == STATUS_OPEN:
        raise RoomRefused(
            f"{code} is in use by a listening link that is still open. Close "
            "that one first, or choose a different ID.")
    with engine.begin() as connection:
        result = connection.execute(text(
            f"INSERT INTO {TABLE} (template_id, public_code, password_hash, "
            "label, status, created_by, created_at, requires_password) "
            "VALUES (:template_id, :code, :password_hash, :label, :status, "
            "        :created_by, :now, :requires_password)"),
            {"template_id": template_id, "code": code,
             "password_hash": hash_password(password), "label": label[:120],
             "status": STATUS_OPEN, "created_by": created_by, "now": utcnow(),
             "requires_password": 0 if no_password else 1})
        room_id = result.lastrowid
    return get_room(engine, room_id=room_id), password


def get_room(engine: Engine, *, room_id: int) -> dict | None:
    return _row(engine, f"SELECT * FROM {TABLE} WHERE id = :id", id=room_id)


def get_room_by_code(engine: Engine, *, code: str) -> dict | None:
    """The room this code names.

    Newest first, because a code can have been used before: an old CLOSED room
    and today's OPEN one can share it, and a listener presenting that code
    means the one that is open now.
    """
    return _row(engine,
                f"SELECT * FROM {TABLE} WHERE public_code = :code "
                "ORDER BY (status = 'OPEN') DESC, id DESC",
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
    # A link deliberately opened without a password admits whoever has the
    # URL. That is the whole point of the switch, and it is why the switch is
    # explicit rather than implied by an empty field.
    if room.get("requires_password", 1) and not verify_password(
            password or "", room["password_hash"]):
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


def list_listeners(engine: Engine, *, room_id: int) -> list[dict]:
    """Who is on this link right now.

    A count alone answers "is anybody there" and nothing else. The question
    people actually have about a link that left the building is WHO followed
    it - and the honest answer is: the name they typed, which is worth
    exactly what a self-declared name is worth. That is why the joined and
    last-seen times are here beside it: those, at least, this program
    observed.
    """
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"SELECT id, display_name, joined_at, last_seen_at "
            f"FROM {TABLE}_listeners WHERE room_id = :room_id "
            "ORDER BY joined_at"), {"room_id": room_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def remove_listener(engine: Engine, *, room_id: int, listener_id: int) -> bool:
    """Turn one listener away without withdrawing the link from everybody."""
    with engine.begin() as connection:
        result = connection.execute(text(
            f"DELETE FROM {TABLE}_listeners WHERE id = :id AND room_id = :room_id"),
            {"id": listener_id, "room_id": room_id})
    return result.rowcount > 0
