"""Reading and changing announcement state.

The transitions themselves live in ``announcements`` and are pure functions
over a state string, so they can be reasoned about without a database. This
module is the part that touches rows: resolving which Stores a template
reaches, listing recordings for a page, and writing the result of a
transition down.

ONE PLACE DECIDES WHAT A TEMPLATE REACHES

``stores_for_template`` is the only function that turns a template into a list
of Store ids. Every caller - play, pause, volume, the status page, the ducking
hook - goes through it. A second implementation would eventually disagree with
this one, and the disagreement would show up as a shop that pauses but never
resumes, or resumes something it was never playing.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

import announcements
from announcements import (
    AUDIO_TABLE,
    ITEM_TABLE,
    PLAYBACK_TABLE,
    STATE_PLAYING,
    STATE_STOPPED,
    TEMPLATE_TABLE,
    AnnouncementRefused,
    utcnow,
)


def _now() -> str:
    return utcnow().isoformat()


def _rows(connection, sql: str, **parameters) -> list[dict[str, Any]]:
    result = connection.execute(text(sql), parameters)
    return [dict(row._mapping) for row in result]


# ===========================================================================
# Targeting
# ===========================================================================

def stores_for_template(engine: Engine, *, template_id: int) -> list[int]:
    """Every Store this template reaches, once each.

    A template can name a Store directly and also name the zone that Store is
    in. That is not a mistake to reject - "everything in the North, plus the
    flagship" is a real plan - but it must not make the flagship play twice or
    appear twice in a status list, so the result is a set.

    Only active Stores. A template written last month against a shop that has
    since been archived should quietly reach one fewer Store, not fail.
    """
    with engine.connect() as connection:
        direct = _rows(connection, f"""
            SELECT i.store_id AS store_id
            FROM {ITEM_TABLE} i
            JOIN stores s ON s.id = i.store_id
            WHERE i.template_id = :template_id
              AND i.store_id IS NOT NULL
              AND s.is_active = 1
        """, template_id=template_id)
        zoned = _rows(connection, f"""
            SELECT s.id AS store_id
            FROM {ITEM_TABLE} i
            JOIN stores s ON s.region = i.zone
            WHERE i.template_id = :template_id
              AND i.zone IS NOT NULL
              AND s.is_active = 1
        """, template_id=template_id)
    found = {row["store_id"] for row in direct} | {row["store_id"] for row in zoned}
    return sorted(found)


def templates_reaching_store(engine: Engine, *, store_id: int) -> list[int]:
    """The other direction, for the ducking hook: which templates touch this
    Store. Same rules, stated once."""
    with engine.connect() as connection:
        rows = _rows(connection, f"""
            SELECT DISTINCT i.template_id AS template_id
            FROM {ITEM_TABLE} i
            LEFT JOIN stores s ON s.region = i.zone
            WHERE i.store_id = :store_id
               OR (i.zone IS NOT NULL AND s.id = :store_id AND s.is_active = 1)
        """, store_id=store_id)
    return sorted(row["template_id"] for row in rows)


# ===========================================================================
# Expiry
#
# A template that has expired reaches nothing. Checked when it is used, not by
# a job that sweeps the table - a sweep that fails leaves expired jingles
# playing, and a shop cannot tell the difference between "the campaign is
# still on" and "nobody noticed it ended".
# ===========================================================================

def template_is_live(row: dict, *, now: str | None = None) -> bool:
    moment = now or _now()
    if (row.get("status") or "active") != "active":
        return False
    starts = row.get("starts_at")
    expires = row.get("expires_at")
    if starts and moment < starts:
        return False
    if expires and moment >= expires:
        return False
    return True


def describe_template_window(row: dict, *, now: str | None = None) -> str:
    """Why a template is not playing, in words, for the status column.

    "Not playing" with no reason is the state an operator rings up about.
    """
    moment = now or _now()
    if (row.get("status") or "active") != "active":
        return "archived"
    starts = row.get("starts_at")
    expires = row.get("expires_at")
    if starts and moment < starts:
        return f"scheduled - starts {starts}"
    if expires and moment >= expires:
        return f"expired {expires}"
    if expires:
        return f"live until {expires}"
    return "live - no end date"


# ===========================================================================
# Playback rows
# ===========================================================================

def get_playback(engine: Engine, *, store_id: int) -> dict[str, Any]:
    """The current state of one Store, inventing a resting row if there is none.

    A Store with no row has never had an announcement, which is STOPPED. That
    is returned rather than None so that every caller does not have to invent
    the same default and eventually invent a different one.
    """
    with engine.connect() as connection:
        rows = _rows(connection,
                     f"SELECT * FROM {PLAYBACK_TABLE} WHERE store_id = :store_id",
                     store_id=store_id)
    if rows:
        return rows[0]
    return {
        "store_id": store_id, "template_id": None, "audio_id": None,
        "state": STATE_STOPPED, "ducked_from": None,
        "volume_percent": announcements.DEFAULT_VOLUME,
        "updated_by": None, "updated_at": None, "started_at": None,
    }


def _write_playback(connection, *, store_id: int, **fields) -> None:
    """Insert or update the single row for this Store.

    UPSERT rather than "select, then insert or update". Two operators pressing
    Play on the same Store at the same moment is ordinary, and the UNIQUE index
    on store_id is what makes the race safe - but only if the write is one
    statement.
    """
    columns = ["store_id", *fields.keys()]
    placeholders = [f":{name}" for name in columns]
    assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
    connection.execute(text(f"""
        INSERT INTO {PLAYBACK_TABLE} ({", ".join(columns)})
        VALUES ({", ".join(placeholders)})
        ON CONFLICT(store_id) DO UPDATE SET {assignments}
    """), {"store_id": store_id, **fields})


def set_state(engine: Engine, *, store_id: int, state: str,
              template_id: int | None = None, audio_id: int | None = None,
              ducked_from: str | None = None, actor_id: int | None = None,
              volume_percent: int | None = None) -> dict[str, Any]:
    """Write one Store's new state. Callers pass a state the pure transition
    functions produced; this does not decide, only records."""
    if state not in announcements.PLAYBACK_STATES:
        raise AnnouncementRefused(f"{state} is not a playback state.")
    now = _now()
    fields: dict[str, Any] = {
        "state": state, "ducked_from": ducked_from,
        "updated_by": actor_id, "updated_at": now,
    }
    if template_id is not None:
        fields["template_id"] = template_id
    if audio_id is not None:
        fields["audio_id"] = audio_id
    if volume_percent is not None:
        fields["volume_percent"] = announcements.validate_volume(volume_percent)
    if state == STATE_PLAYING:
        fields["started_at"] = now
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id, **fields)
    return get_playback(engine, store_id=store_id)


def set_volume(engine: Engine, *, store_id: int, volume_percent: int,
               actor_id: int | None = None) -> dict[str, Any]:
    """Volume alone, without disturbing the state.

    Deliberately separate from set_state. Turning a jingle down must not start
    it, and must not stop it - and a combined call would eventually be used
    with a default state argument that did one of those by accident.
    """
    volume = announcements.validate_volume(volume_percent)
    current = get_playback(engine, store_id=store_id)
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id,
                        state=current["state"],
                        ducked_from=current.get("ducked_from"),
                        volume_percent=volume,
                        updated_by=actor_id, updated_at=_now())
    return get_playback(engine, store_id=store_id)


# ===========================================================================
# Ducking, applied to real Stores
# ===========================================================================

def duck_stores(engine: Engine, store_ids: Iterable[int]) -> list[int]:
    """A broadcast has started in these Stores. Returns the ones that moved."""
    moved = []
    for store_id in store_ids:
        current = get_playback(engine, store_id=store_id)
        state, remembered = announcements.duck(current["state"])
        if state == current["state"]:
            continue
        set_state(engine, store_id=store_id, state=state, ducked_from=remembered)
        moved.append(store_id)
    return moved


def unduck_stores(engine: Engine, store_ids: Iterable[int]) -> list[int]:
    """The broadcast has ended. Restores only what ducking moved."""
    resumed = []
    for store_id in store_ids:
        current = get_playback(engine, store_id=store_id)
        state, _ = announcements.unduck(current["state"], current.get("ducked_from"))
        if state == current["state"]:
            continue
        set_state(engine, store_id=store_id, state=state, ducked_from=None)
        resumed.append(store_id)
    return resumed


# ===========================================================================
# Listing, for the page
# ===========================================================================

def list_audio(engine: Engine, *, search: str = "", status: str = "active"
               ) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = _rows(connection, f"SELECT * FROM {AUDIO_TABLE} ORDER BY id DESC")
    needle = (search or "").strip().lower()
    if status and status != "all":
        rows = [row for row in rows if (row.get("status") or "active") == status]
    if needle:
        rows = [row for row in rows
                if needle in (row.get("title") or "").lower()
                or needle in (row.get("original_filename") or "").lower()]
    return rows


def list_templates(engine: Engine, *, search: str = "", status: str = "active",
                   zone: str = "", store_id: int | None = None
                   ) -> list[dict[str, Any]]:
    """Templates with their lines attached and their window described.

    The lines are fetched in ONE query for all templates rather than one query
    per template. With a few hundred templates the per-template version is a
    few hundred round trips to answer a page that shows twenty.
    """
    with engine.connect() as connection:
        templates = _rows(connection,
                          f"SELECT * FROM {TEMPLATE_TABLE} ORDER BY id DESC")
        items = _rows(connection, f"""
            SELECT i.*, a.title AS audio_title, s.store_name AS store_name,
                   s.store_code AS store_code
            FROM {ITEM_TABLE} i
            LEFT JOIN {AUDIO_TABLE} a ON a.id = i.audio_id
            LEFT JOIN stores s ON s.id = i.store_id
            ORDER BY i.template_id, i.position, i.id
        """)
    by_template: dict[int, list[dict]] = {}
    for item in items:
        by_template.setdefault(item["template_id"], []).append(item)

    now = _now()
    needle = (search or "").strip().lower()
    result = []
    for template in templates:
        template["items"] = by_template.get(template["id"], [])
        template["is_live"] = template_is_live(template, now=now)
        template["window"] = describe_template_window(template, now=now)
        if status and status != "all" and (template.get("status") or "active") != status:
            continue
        if needle and needle not in (template.get("name") or "").lower() \
                and needle not in (template.get("description") or "").lower():
            continue
        if zone and not any((item.get("zone") or "") == zone
                            for item in template["items"]):
            continue
        if store_id is not None and not any(item.get("store_id") == store_id
                                            for item in template["items"]):
            continue
        result.append(template)
    return result


def live_status(engine: Engine, *, search: str = "", zone: str = "",
                state: str = "") -> list[dict[str, Any]]:
    """What every Store is doing right now, for the status table.

    A LEFT JOIN from stores, not from the playback table: a Store that has
    never played anything must still appear, saying STOPPED. Listing only the
    Stores with a row would quietly hide exactly the shops somebody is looking
    for when they ask why a campaign is not running everywhere.
    """
    with engine.connect() as connection:
        rows = _rows(connection, f"""
            SELECT s.id AS store_id, s.store_code, s.store_name, s.region AS zone,
                   s.status AS store_status,
                   COALESCE(p.state, :stopped) AS state,
                   p.ducked_from, p.template_id, p.audio_id,
                   COALESCE(p.volume_percent, :default_volume) AS volume_percent,
                   p.updated_at, p.started_at,
                   t.name AS template_name, a.title AS audio_title
            FROM stores s
            LEFT JOIN {PLAYBACK_TABLE} p ON p.store_id = s.id
            LEFT JOIN {TEMPLATE_TABLE} t ON t.id = p.template_id
            LEFT JOIN {AUDIO_TABLE} a ON a.id = p.audio_id
            WHERE s.is_active = 1
            ORDER BY s.store_code
        """, stopped=STATE_STOPPED, default_volume=announcements.DEFAULT_VOLUME)

    needle = (search or "").strip().lower()
    if needle:
        rows = [row for row in rows
                if needle in (row.get("store_code") or "").lower()
                or needle in (row.get("store_name") or "").lower()
                or needle in (row.get("template_name") or "").lower()
                or needle in (row.get("audio_title") or "").lower()]
    if zone:
        rows = [row for row in rows if (row.get("zone") or "") == zone]
    if state:
        rows = [row for row in rows if row.get("state") == state]
    return rows
