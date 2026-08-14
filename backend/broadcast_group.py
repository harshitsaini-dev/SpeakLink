"""More than one voice on one broadcast.

TWO WAYS IN, AND THE DIFFERENCE IS A PERMISSION

An account holding ``broadcast.group_join`` joins a live broadcast directly.
Nobody is asked, because the estate has already decided this person may speak
on air - asking the host again would be a second approval for a decision that
was made when the right was granted.

An account without it may still ASK. The request goes to the host, who is the
only person who can hear what is happening on that broadcast right now and is
therefore the only one who can judge whether a second voice is wanted this
minute. Until they answer, the requester is not on air.

WHAT "ON AIR" MEANS HERE, EXACTLY

``JOINED`` and nothing else. The microphone socket admits a participant only
when this table says JOINED for that account and that session, re-read at the
handshake rather than taken from anything the browser sent. That check is the
whole feature: everything else - the request, the approval, the list - is
bookkeeping around it, and a REQUESTED or DENIED account that could still push
audio would make all of it decoration.

WHY A DENIAL IS REMEMBERED

A denied request stays DENIED rather than being deleted. Deleting it would let
the same account ask again immediately, and again, and the host would be
answering the same question all through a broadcast they are trying to run.
Asking again is possible - the host can be asked properly, out of band - but
it is not one click away.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

TABLE = "broadcast_group_participants"

STATE_REQUESTED = "REQUESTED"
STATE_JOINED = "JOINED"
STATE_DENIED = "DENIED"
STATE_LEFT = "LEFT"
STATES = (STATE_REQUESTED, STATE_JOINED, STATE_DENIED, STATE_LEFT)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_group_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                state VARCHAR(16) NOT NULL,
                requested_at VARCHAR(40),
                decided_at VARCHAR(40),
                decided_by INTEGER,
                -- Kept so a log reader can tell a colleague who walked in from
                -- one the host had to think about. The two look identical
                -- afterwards and they are not the same event.
                joined_by_right INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # One row per account per broadcast, enforced by the schema. Without
        # this, a double-clicked Join is two rows and a host is asked twice.
        connection.exec_driver_sql(
            f"CREATE UNIQUE INDEX IF NOT EXISTS ix_group_participant_unique "
            f"ON {TABLE}(session_id, user_id)")
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_group_participant_state "
            f"ON {TABLE}(session_id, state)")


class GroupRefused(ValueError):
    """A refusal with a sentence the person reading it can act on."""


def _write(engine: Engine, *, session_id: int, user_id: int, **fields) -> None:
    columns = ["session_id", "user_id", *fields.keys()]
    placeholders = [f":{name}" for name in columns]
    assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with engine.begin() as connection:
        connection.execute(text(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT(session_id, user_id) DO UPDATE SET {assignments}"),
            {"session_id": session_id, "user_id": user_id, **fields})


def get_participant(engine: Engine, *, session_id: int, user_id: int) -> dict | None:
    with engine.connect() as connection:
        found = connection.execute(text(
            f"SELECT * FROM {TABLE} WHERE session_id = :session_id "
            "AND user_id = :user_id"),
            {"session_id": session_id, "user_id": user_id}).first()
    return dict(found._mapping) if found else None


def list_participants(engine: Engine, *, session_id: int) -> list[dict]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            f"SELECT p.*, u.username, u.display_name FROM {TABLE} p "
            "LEFT JOIN hq_users u ON u.id = p.user_id "
            "WHERE p.session_id = :session_id ORDER BY p.id"),
            {"session_id": session_id}).fetchall()
    return [dict(row._mapping) for row in rows]


def is_on_air(engine: Engine, *, session_id: int, user_id: int) -> bool:
    """The only question the microphone socket asks.

    Deliberately not "has a row" or "is not denied": JOINED and nothing else.
    A default that admitted anything but an explicit refusal would put a
    requester on air the moment they asked.
    """
    participant = get_participant(engine, session_id=session_id, user_id=user_id)
    return bool(participant and participant["state"] == STATE_JOINED)


def join_or_request(engine: Engine, *, session_id: int, user_id: int,
                    may_join_directly: bool) -> dict:
    """Join if the estate has already said so; otherwise ask the host."""
    existing = get_participant(engine, session_id=session_id, user_id=user_id)
    if existing and existing["state"] == STATE_JOINED:
        return existing
    if existing and existing["state"] == STATE_DENIED and not may_join_directly:
        # Remembered on purpose. Letting a denial be re-asked with one click
        # means a host answers the same question all through a broadcast they
        # are trying to run.
        raise GroupRefused(
            "The host has already declined this request for this broadcast. "
            "Ask them directly if that has changed.")

    if may_join_directly:
        _write(engine, session_id=session_id, user_id=user_id,
               state=STATE_JOINED, decided_at=utcnow(), decided_by=user_id,
               joined_by_right=1,
               requested_at=(existing or {}).get("requested_at") or utcnow())
    else:
        _write(engine, session_id=session_id, user_id=user_id,
               state=STATE_REQUESTED, requested_at=utcnow(),
               decided_at=None, decided_by=None, joined_by_right=0)
    return get_participant(engine, session_id=session_id, user_id=user_id)


def decide(engine: Engine, *, session_id: int, user_id: int, approve: bool,
           decided_by: int) -> dict:
    participant = get_participant(engine, session_id=session_id, user_id=user_id)
    if participant is None or participant["state"] != STATE_REQUESTED:
        raise GroupRefused("There is no request from that account to answer.")
    _write(engine, session_id=session_id, user_id=user_id,
           state=STATE_JOINED if approve else STATE_DENIED,
           decided_at=utcnow(), decided_by=decided_by)
    return get_participant(engine, session_id=session_id, user_id=user_id)


def leave(engine: Engine, *, session_id: int, user_id: int) -> dict | None:
    participant = get_participant(engine, session_id=session_id, user_id=user_id)
    if participant is None:
        return None
    _write(engine, session_id=session_id, user_id=user_id,
           state=STATE_LEFT, decided_at=utcnow(), decided_by=user_id)
    return get_participant(engine, session_id=session_id, user_id=user_id)


def describe(participant: dict | None) -> str:
    if participant is None:
        return "not on this broadcast"
    return {
        STATE_JOINED: "on air",
        STATE_REQUESTED: "waiting for the host to answer",
        STATE_DENIED: "the host declined this request",
        STATE_LEFT: "left this broadcast",
    }.get(participant["state"], participant["state"])
