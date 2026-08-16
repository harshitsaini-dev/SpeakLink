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

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

import announcement_schedule
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
    daily = announcement_schedule.describe(row.get("daily_start"),
                                           row.get("daily_end"),
                                           row.get("daily_days"))
    if expires and daily:
        return f"live until {expires}, {daily}"
    if expires:
        return f"live until {expires}"
    if daily:
        return f"live, {daily}"
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
        "confirmed_kind": None, "confirmed_at": None, "confirmed_error": "",
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


def stop(engine: Engine, *, store_id: int,
         actor_id: int | None = None) -> dict[str, Any]:
    """Stop one Store: silent, back to the beginning, still assigned.

    STOP IS NOT PAUSE, AND IT IS NOT UNASSIGN.

    Pause holds the campaign where it is, so Play carries on from that point.
    Stop ends this run - the next Play starts the recording from its
    beginning - but the Store KEEPS the template it was given.

    I built the first version so that stopping also cleared the assignment,
    and that was wrong: the console then showed "nothing chosen" for shops
    that were still very much part of a live campaign, and getting them back
    meant re-targeting the whole template. Which template a shop belongs to is
    a decision somebody made on the Templates page; a transport button has no
    business undoing it. Removing a Store from a campaign is that page's job.
    """
    now = _now()
    previous = get_playback(engine, store_id=store_id)
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id,
                        state=STATE_STOPPED, ducked_from=None,
                        started_at=None,
                        updated_by=actor_id, updated_at=now)
    # A stop closes the open history row, exactly as set_state does for every
    # other way of leaving PLAYING - so a campaign that was stopped is not the
    # one entry in the record that never ended.
    try:
        if previous["state"] == STATE_PLAYING:
            close_history(engine, store_id=store_id, reason="stopped",
                          actor_id=actor_id)
    except Exception:  # noqa: BLE001 - history must never fail a live action
        pass
    return get_playback(engine, store_id=store_id)


def retire(engine: Engine, *, store_id: int,
           actor_id: int | None = None) -> dict[str, Any]:
    """The campaign is over: silent, and no longer chosen.

    RETIRE IS NOT STOP.

    Stop ends a run and keeps the assignment, because somebody will press Play
    again - that distinction was itself a correction from the estate. Expiry
    is the opposite: the promotion has finished, its end date was decided in
    advance, and a shop still listed as playing it is a shop somebody will ask
    about. So this clears the choice as well as the sound.

    It is the only thing in this file that clears an assignment, and it is
    driven by the clock rather than by a person - which is exactly why it can
    be trusted to: nobody is going to be surprised by an end date they set.
    """
    now = _now()
    previous = get_playback(engine, store_id=store_id)
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id,
                        state=STATE_STOPPED, ducked_from=None,
                        template_id=None, audio_id=None,
                        started_at=None,
                        confirmed_kind=None, confirmed_at=None,
                        confirmed_error="",
                        updated_by=actor_id, updated_at=now)
    try:
        if previous["state"] == STATE_PLAYING:
            close_history(engine, store_id=store_id, reason="expired",
                          actor_id=actor_id)
    except Exception:  # noqa: BLE001 - history must never fail a live action
        pass
    return get_playback(engine, store_id=store_id)


def set_state(engine: Engine, *, store_id: int, state: str,
              template_id: int | None = None, audio_id: int | None = None,
              ducked_from: str | None = None, actor_id: int | None = None,
              volume_percent: int | None = None,
              reachable: bool = True) -> dict[str, Any]:
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
        # HISTORY IS WHAT PLAYED, NOT WHAT WAS SENT.
        #
        # Opening a row for a shop with no Receiver connected put minutes -
        # and then hours - of "playing" into the record for shops that were
        # silent the whole time. Nothing confirmed those runs started, and
        # with nothing connected nothing would ever close them, so every
        # report built on this table drifted further from the truth the longer
        # the shop stayed offline.
        #
        # The state is still recorded, and the shop still picks it up when it
        # reconnects - at which point that IS a run, and gets its own row.
        if state == STATE_PLAYING and not reachable:
            pass
        elif state == STATE_PLAYING and previous["state"] != STATE_PLAYING:
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


def record_acknowledgement(engine: Engine, *, store_id: int, kind: str,
                           error: str = "") -> None:
    """What the Store said back about the announcement it was sent.

    WHY HQ HAS TO KEEP THIS

    Everything the console showed about announcements came from what HQ had
    SENT. The Receiver has always answered - announcement_playing,
    announcement_failed - and nothing here read those answers, so a shop whose
    decoder could not start, or whose speaker was not open, appeared on the
    console as PLAYING for as long as anybody looked at it. Two separate real
    faults hid behind that for a day.

    Stored on the playback row rather than in memory: the answer has to
    outlive a restart of HQ, because the shop's state did.
    """
    # `updated_at` travels with it because the row may not exist yet - a Store
    # can answer about a command that arrived before HQ ever wrote a playback
    # row for it - and the column is NOT NULL. This is an acknowledgement, so
    # it deliberately does not touch `state` or `updated_by`: what HQ asked
    # for and what the shop reports are different facts and must not overwrite
    # each other.
    with engine.begin() as connection:
        _write_playback(connection, store_id=store_id,
                        confirmed_kind=kind,
                        confirmed_at=_now(),
                        confirmed_error=(error or "")[:500],
                        updated_at=_now())


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
                state: str = "", store_id=None,
                connected_store_ids=None) -> list[dict[str, Any]]:
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
                   p.confirmed_kind, p.confirmed_at, p.confirmed_error,
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

    # PLAYING is a claim about a shop's speaker, and HQ can only make it for a
    # shop it is actually connected to. Where nothing is connected, what is
    # true is that HQ ASKED - so `reachable` carries that, and the table says
    # "asked to play" instead of asserting audio nobody can hear.
    #
    # None means the caller did not tell us who is connected. Marking every
    # row unreachable in that case would be its own lie, so the flag stays
    # True and nothing changes.
    if connected_store_ids is not None:
        connected = set(connected_store_ids)
        for row in rows:
            row["reachable"] = row["store_id"] in connected
    else:
        for row in rows:
            row["reachable"] = True

    # WHAT THIS SHOP WOULD PLAY, for the shops that are not playing anything.
    #
    # The console reads the PLAYBACK row, which does not exist until somebody
    # presses Play - so a shop that a template was built for showed "nothing
    # chosen", and the only way to find out what it was going to play was to
    # play it. That is the opposite of the promise this feature makes: decide
    # once, then only press play and pause.
    #
    # Kept separate from `template_name` on purpose. What a shop IS playing
    # and what it WOULD play are different facts, and collapsing them is how a
    # console starts describing intentions as sound.
    assignments: dict[int, dict[str, Any]] = {}
    for template in list_templates(engine, status="active"):
        if not template_is_live(template):
            continue
        first = (template.get("items") or [None])[0] or {}
        # `targeted`, NOT `store_id`.
        #
        # `store_id` is this function's own parameter - the filter somebody
        # passed for one shop - and using it as a loop variable left it
        # pointing at the last store of the last template. The filter below
        # then narrowed 46 rows to 1, and the console showed a single shop.
        # Nothing failed; the list was simply wrong, which is the shape of
        # bug shadowing always makes.
        for targeted in stores_for_template(engine, template_id=template["id"]):
            assignments.setdefault(targeted, {
                "assigned_template_id": template["id"],
                "assigned_template_name": template["name"],
                "assigned_audio_title": first.get("audio_title"),
            })

    for row in rows:
        if row.get("template_id") is None:
            row.update(assignments.get(row["store_id"], {}))

    for row in rows:
        # THREE DIFFERENT FACTS, and the console needs all three.
        #
        #   state            what HQ asked this shop to do
        #   reachable        whether HQ is connected to it at all
        #   confirmed        whether the Store answered that it is doing it
        #
        # Only the first of those was ever shown. The Receiver has always
        # replied - announcement_playing, announcement_failed - and nothing
        # read the replies, so a shop whose decoder could not start appeared
        # as PLAYING for as long as anybody looked at it.
        row["confirmed"] = (row.get("confirmed_kind") == "announcement_playing")
        row["confirm_error"] = row.get("confirmed_error") or ""

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


def _elapsed_seconds(started, ended):
    """Seconds between two stored timestamps, or None if it is still running.

    Tolerant of the stored shape - these are written as ISO strings and read
    back as strings or datetimes depending on the driver - and returns None
    rather than raising when a value cannot be read, because a history page
    must not fail over one unparseable row.
    """
    if not started or not ended:
        return None
    try:
        first = started if isinstance(started, datetime) else datetime.fromisoformat(str(started))
        last = ended if isinstance(ended, datetime) else datetime.fromisoformat(str(ended))
    except (TypeError, ValueError):
        return None
    return max(0, int((last - first).total_seconds()))


def list_history(engine: Engine, *, connected_store_ids=None,
                 search: str = "", zone: str = "",
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

    # How long each run lasted, computed once here rather than in the browser.
    # A row that is still open has no duration - putting "0", or the time so
    # far, under a column headed Duration would read as a finished run of that
    # length.
    #
    # And whether HQ is connected to that shop at all. An open row for a shop
    # nothing is connected to is NOT "still playing": nothing confirmed it
    # started and nothing will ever close it, so the page would go on claiming
    # sound in a shop for as long as anybody looked at it.
    connected = None if connected_store_ids is None else set(connected_store_ids)
    for row in rows:
        row["duration_seconds"] = _elapsed_seconds(row.get("started_at"),
                                                   row.get("ended_at"))
        row["reachable"] = (True if connected is None
                            else row.get("store_id") in connected)
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
