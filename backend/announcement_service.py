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

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

import announcements
from admin_search import int_list, matches_any, value_list
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

    previous = get_playback(engine, store_id=store_id)
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id, **fields)

    # Written here rather than at each route. Six callers change a Store's
    # state - including the ducking hook inside broadcast start and stop - and
    # recording it per-caller means the one nobody remembers is the one that
    # leaves a gap in the history.
    try:
        if state == STATE_PLAYING and previous["state"] != STATE_PLAYING:
            open_history(engine, store_id=store_id,
                         template_id=fields.get("template_id",
                                                previous.get("template_id")),
                         audio_id=fields.get("audio_id", previous.get("audio_id")),
                         volume_percent=fields.get("volume_percent",
                                                   previous.get("volume_percent")),
                         actor_id=actor_id)
        elif state != STATE_PLAYING and previous["state"] == STATE_PLAYING:
            close_history(engine, store_id=store_id,
                          reason={"PAUSED": "paused", "DUCKED": "broadcast",
                                  "STOPPED": "stopped"}.get(state, state.lower()),
                          actor_id=actor_id)
    except Exception:  # noqa: BLE001 - history must never fail a live action
        pass
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
    if status and "all" not in value_list(status):
        rows = [row for row in rows
                if matches_any(row.get("status") or "active", status)]
    if needle:
        rows = [row for row in rows
                if needle in (row.get("title") or "").lower()
                or needle in (row.get("original_filename") or "").lower()]
    return rows


def list_templates(engine: Engine, *, search: str = "", status: str = "active",
                   zone: str = "", store_id=None,
                   window: str = "") -> list[dict[str, Any]]:
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
        # A readable label for the whole "plays in" column, so it can be
        # sorted and exported as the thing the reader sees. Built from the
        # first line: a template with several lines groups under the first
        # place it plays, which is the order somebody scanning for "the North
        # ones" actually wants.
        first = (template["items"] or [None])[0]
        template["plays_in"] = (
            (first.get("zone") or first.get("store_name")
             or (f"store {first.get('store_id')}" if first.get("store_id") else ""))
            if first else "")
        template["window"] = describe_template_window(template, now=now)
        if (status and "all" not in value_list(status)
                and not matches_any(template.get("status") or "active", status)):
            continue
        if needle and needle not in (template.get("name") or "").lower() \
                and needle not in (template.get("description") or "").lower():
            continue
        # A template matches if ANY of its lines matches ANY of the values the
        # filter names. "Show me the templates touching these six shops" is
        # the question; requiring every line to match would answer a different
        # one nobody asks.
        if zone and not any(matches_any(item.get("zone") or "", zone)
                            for item in template["items"]):
            continue
        wanted_stores = int_list(store_id)
        if wanted_stores and not any(item.get("store_id") in wanted_stores
                                     for item in template["items"]):
            continue
        # The window is the column people actually scan, so it is also the
        # filter they reach for: "which of these have already expired" is the
        # question behind most of the tidying that happens on this page.
        windows = value_list(window)
        if windows:
            described = template["window"]
            state = ("live" if template["is_live"]
                     else "scheduled" if described.startswith("scheduled")
                     else "expired" if described.startswith("expired")
                     else "archived")
            if state not in windows:
                continue
        result.append(template)
    return result


def live_status(engine: Engine, *, search: str = "", zone: str = "",
                state: str = "", store_id=None) -> list[dict[str, Any]]:
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
        rows = [row for row in rows if matches_any(row.get("zone") or "", zone)]
    if state:
        rows = [row for row in rows if matches_any(row.get("state"), state)]
    if store_id:
        wanted = int_list(store_id)
        rows = [row for row in rows if row["store_id"] in wanted]
    return rows


# ===========================================================================
# History: what played, where, and when it stopped
#
# Written as rows that OPEN and CLOSE rather than as events, because the
# question people actually ask is "what was this shop playing at four o'clock",
# and answering that from a stream of events means replaying them. A row with a
# start and an end answers it with a comparison.
#
# Every descriptive field is copied in, not joined. A history row has to stay
# readable after the template is archived and the recording deleted - and a
# JOIN to a row that no longer exists renders "unknown" for something that was
# perfectly well known at the time.
# ===========================================================================

def open_history(engine: Engine, *, store_id: int, template_id, audio_id,
                 volume_percent: int, actor_id) -> None:
    """A Store started playing. Closes any row left open for it first.

    Left-open rows are not hypothetical: HQ can be restarted while a shop is
    playing. Closing the previous one here means the history cannot accumulate
    two open rows for one Store, which is the state that makes every later
    "what was playing" answer ambiguous.
    """
    close_history(engine, store_id=store_id, reason="superseded", actor_id=actor_id)
    with engine.begin() as connection:
        descriptive = connection.execute(text(
            "SELECT s.store_code, s.store_name, s.region AS zone, "
            f"       t.name AS template_name, a.title AS audio_title "
            "FROM stores s "
            f"LEFT JOIN {TEMPLATE_TABLE} t ON t.id = :template_id "
            f"LEFT JOIN {AUDIO_TABLE} a ON a.id = :audio_id "
            "WHERE s.id = :store_id"),
            {"store_id": store_id, "template_id": template_id,
             "audio_id": audio_id}).first()
        row = dict(descriptive._mapping) if descriptive else {}
        connection.execute(text(
            f"INSERT INTO {announcements.HISTORY_TABLE} "
            "(store_id, template_id, audio_id, store_code, store_name, zone, "
            " template_name, audio_title, started_at, started_by, volume_percent) "
            "VALUES (:store_id, :template_id, :audio_id, :store_code, "
            "        :store_name, :zone, :template_name, :audio_title, "
            "        :started_at, :started_by, :volume_percent)"),
            {"store_id": store_id, "template_id": template_id,
             "audio_id": audio_id,
             "store_code": row.get("store_code"), "store_name": row.get("store_name"),
             "zone": row.get("zone"), "template_name": row.get("template_name"),
             "audio_title": row.get("audio_title"),
             "started_at": _now(), "started_by": actor_id,
             "volume_percent": volume_percent})


def close_history(engine: Engine, *, store_id: int, reason: str,
                  actor_id=None) -> None:
    """Whatever this Store had open, ended, with the reason written down.

    "It went quiet at 4pm" is only answerable if the reason was recorded at the
    time: paused by a person and ducked by a broadcast look identical
    afterwards and are not the same event.
    """
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {announcements.HISTORY_TABLE} "
            "SET ended_at = :now, ended_reason = :reason, ended_by = :actor "
            "WHERE store_id = :store_id AND ended_at IS NULL"),
            {"now": _now(), "reason": reason, "actor": actor_id,
             "store_id": store_id})


def list_history(engine: Engine, *, search: str = "", zone: str = "",
                 reason: str = "", store_id=None, template_id=None,
                 since: str = "", until: str = "",
                 include_archived: bool = False) -> list[dict]:
    with engine.connect() as connection:
        # The account names, joined here rather than resolved in the browser.
        #
        # "Paused by a person" was true and useless: the whole reason the
        # column exists is to answer "who did that", and an id would only move
        # the question. A LEFT JOIN because an account can be deleted and the
        # row must still say what it can - "paused by a deleted account" is a
        # worse answer than a name, and a better one than nothing.
        rows = _rows(connection,
                     f"SELECT h.*, "
                     "       s.username AS started_by_username, "
                     "       s.display_name AS started_by_name, "
                     "       e.username AS ended_by_username, "
                     "       e.display_name AS ended_by_name "
                     f"FROM {announcements.HISTORY_TABLE} h "
                     "LEFT JOIN hq_users s ON s.id = h.started_by "
                     "LEFT JOIN hq_users e ON e.id = h.ended_by "
                     "ORDER BY h.started_at DESC, h.id DESC")
    if not include_archived:
        rows = [row for row in rows if not row.get("archived_at")]
    needle = (search or "").strip().lower()
    if needle:
        rows = [row for row in rows
                if needle in (row.get("store_code") or "").lower()
                or needle in (row.get("store_name") or "").lower()
                or needle in (row.get("template_name") or "").lower()
                or needle in (row.get("audio_title") or "").lower()]
    if zone:
        rows = [row for row in rows if matches_any(row.get("zone") or "", zone)]
    if reason:
        # "still playing" is a filter people want and it is the ABSENCE of an
        # end, not a reason - so it is spelled here rather than left to
        # somebody constructing an empty-string query.
        wanted = value_list(reason)
        rows = [row for row in rows
                if ("open" in wanted and not row.get("ended_at"))
                or (row.get("ended_reason") in wanted)]
    if store_id:
        wanted_stores = int_list(store_id)
        rows = [row for row in rows if row.get("store_id") in wanted_stores]
    if template_id:
        wanted_templates = int_list(template_id)
        rows = [row for row in rows if row.get("template_id") in wanted_templates]
    if since:
        rows = [row for row in rows if (row.get("started_at") or "") >= since]
    if until:
        rows = [row for row in rows if (row.get("started_at") or "") <= until]
    return rows


def reconcile_playback(engine: Engine) -> list[int]:
    """Stop any Store pointed at a template that no longer applies.

    WHY THIS RUNS AT STARTUP

    Archiving a template used to leave the shops running it exactly where they
    were, and one estate carries the result: a Store still reporting a
    template that cannot be opened, with no row in the list to press Pause on.
    Fixing the archive path stops it happening again; it does nothing for the
    rows already like that, and those are the ones an operator is looking at.

    A missing template and an archived one are treated the same way here,
    because they mean the same thing to a shop: nobody can act on this from
    the templates list any more. The Store is stopped and its pointer cleared,
    which reads as "nothing chosen" - true, and something an operator can do
    something about.

    Returns the Store ids it changed, so a boot that quietly fixed six shops
    can say so rather than looking like it did nothing.
    """
    with engine.connect() as connection:
        rows = _rows(connection, f"""
            SELECT p.store_id, p.template_id, t.status AS template_status
            FROM {PLAYBACK_TABLE} p
            LEFT JOIN {TEMPLATE_TABLE} t ON t.id = p.template_id
            WHERE p.template_id IS NOT NULL
        """)
    stranded = [row["store_id"] for row in rows
                if row["template_status"] is None
                or row["template_status"] != "active"]
    if not stranded:
        return []
    now = _now()
    with engine.begin() as connection:
        connection.execute(text(
            f"UPDATE {PLAYBACK_TABLE} SET state = :stopped, template_id = NULL, "
            "audio_id = NULL, ducked_from = NULL, updated_at = :now "
            "WHERE store_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)),
            {"stopped": STATE_STOPPED, "now": now, "ids": stranded})
    return stranded
