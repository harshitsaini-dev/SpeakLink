"""Changing which speaker a Store plays through, from HQ.

THE THING THAT MAKES THIS DIFFERENT FROM EVERY OTHER SETTING

Until now the output device could only be chosen standing at the Store PC,
and that was not merely inconvenient - it was a real protection. The person
who could get it wrong was the person who could hear the result. They picked a
device, pressed Test Sound, and confirmed they heard it before anything was
saved.

Done from HQ, nobody can hear anything. A wrong selection produces a shop that
is silent, and silent is the one failure this system cannot detect on its own:
there is no error, no disconnection, no failed command. The Receiver reports
"applied" and the shop plays to nobody until a customer complains.

So the design is shaped entirely around not being able to hear:

  1. HQ can only offer devices the STORE has enumerated and reported. There is
     no free-text field. An operator cannot invent a selector, and cannot
     paste one that was valid on a different computer.

  2. The Store RESOLVES the selector before switching, and refuses if it is
     ambiguous or missing - the same resolver the wizard uses, so HQ and the
     Store cannot disagree about what a selector means.

  3. The Store reports back which device it ACTUALLY ended up on, by name, not
     merely that the command succeeded. "HQ sent index:3" and "the Store is
     playing through Realtek Speakers" are different facts and the operator
     needs the second one.

  4. The previous selector is recorded, so a change can be undone by somebody
     who was not the person who made it - which, given nobody can hear the
     result, is who will usually be undoing it.

WHAT THIS DOES NOT DO

It does not test the sound. A Store PC cannot prove a speaker is connected,
plugged into the right amplifier, or turned up. The honest thing is to say so
in the interface and keep a human in that loop - a "verified" flag this
program could not have earned would be worse than no flag.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

TABLE = "receiver_output_devices"

#: What a Store said about one switch attempt.
RESULT_APPLIED = "applied"
RESULT_REFUSED = "refused"
RESULT_UNSUPPORTED = "unsupported"
RESULTS = (RESULT_APPLIED, RESULT_REFUSED, RESULT_UNSUPPORTED)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_output_device_schema(engine: Engine) -> None:
    """One row per Store: what it can play through, and what it is on."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL UNIQUE,
                -- JSON, exactly as the Store enumerated it. Kept whole rather
                -- than split into rows: it is a snapshot of another computer
                -- at a moment, not data HQ owns, and HQ must not be able to
                -- edit half of it.
                devices_json TEXT NOT NULL DEFAULT '[]',
                reported_at VARCHAR(40),
                -- What HQ last asked for, and what the Store said it ended up
                -- on. Deliberately two columns: they are allowed to differ,
                -- and the difference is the whole point of the second one.
                requested_selector VARCHAR(255),
                applied_selector VARCHAR(255),
                applied_device_name VARCHAR(255),
                -- So a change can be undone by somebody who did not make it.
                previous_selector VARCHAR(255),
                last_result VARCHAR(20),
                last_error VARCHAR(500),
                changed_by INTEGER,
                changed_at VARCHAR(40)
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_receiver_output_devices_store "
            f"ON {TABLE}(store_id)")


class OutputDeviceRefused(ValueError):
    """A refusal with a sentence an operator can act on."""


def _row(engine: Engine, store_id: int) -> dict | None:
    with engine.connect() as connection:
        result = connection.execute(
            text(f"SELECT * FROM {TABLE} WHERE store_id = :store_id"),
            {"store_id": store_id})
        found = result.first()
    return dict(found._mapping) if found else None


def _write(engine: Engine, store_id: int, **fields) -> None:
    columns = ["store_id", *fields.keys()]
    placeholders = [f":{name}" for name in columns]
    assignments = ", ".join(f"{name} = excluded.{name}" for name in fields)
    with engine.begin() as connection:
        connection.execute(text(
            f"INSERT INTO {TABLE} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT(store_id) DO UPDATE SET {assignments}"),
            {"store_id": store_id, **fields})


def record_reported_devices(engine: Engine, *, store_id: int,
                            devices: list[dict]) -> None:
    """What this Store says it can play through.

    Replaced wholesale on every report rather than merged. A speaker that has
    been unplugged must DISAPPEAR from the list HQ offers; merging would leave
    it selectable for ever, and selecting it is precisely the mistake that
    makes a shop silent.
    """
    import json

    _write(engine, store_id,
           devices_json=json.dumps(devices), reported_at=utcnow())


def get_state(engine: Engine, *, store_id: int) -> dict:
    """Everything HQ knows about one Store's audio output."""
    import json

    row = _row(engine, store_id) or {}
    try:
        devices = json.loads(row.get("devices_json") or "[]")
    except ValueError:
        devices = []
    return {
        "store_id": store_id,
        "devices": devices,
        "reported_at": row.get("reported_at"),
        "requested_selector": row.get("requested_selector"),
        "applied_selector": row.get("applied_selector"),
        "applied_device_name": row.get("applied_device_name"),
        "previous_selector": row.get("previous_selector"),
        "last_result": row.get("last_result"),
        "last_error": row.get("last_error"),
        "changed_by": row.get("changed_by"),
        "changed_at": row.get("changed_at"),
    }


def validate_selector_is_one_the_store_offered(state: dict, selector: str) -> dict:
    """Refuse anything the Store did not itself report.

    There is no free-text field at HQ, and this is why: a selector typed by
    somebody who cannot hear the result, or pasted from a different computer,
    resolves to a device that may not exist or - worse - to a different device
    that does. The Store's own list is the only source.
    """
    if not isinstance(selector, str) or not selector.strip():
        raise OutputDeviceRefused("No speaker was chosen.")
    cleaned = selector.strip()
    offered = state.get("devices") or []
    if not offered:
        raise OutputDeviceRefused(
            "This Store has not reported which speakers it has yet. It has to "
            "be online at least once before its output can be changed from "
            "here.")
    for device in offered:
        if cleaned in (device.get("verified_selector"), device.get("selector")):
            return device
    names = ", ".join(str(device.get("name")) for device in offered[:5])
    raise OutputDeviceRefused(
        f"This Store did not report a speaker matching {cleaned!r}. It has: "
        f"{names}. Ask it to refresh if a speaker was just plugged in.")


def record_request(engine: Engine, *, store_id: int, selector: str,
                   actor_id: int | None) -> None:
    """HQ has asked. Nothing has changed in the shop yet, and the row says so:
    requested_selector moves, applied_selector does not."""
    state = get_state(engine, store_id=store_id)
    _write(engine, store_id,
           requested_selector=selector,
           previous_selector=state.get("applied_selector"),
           last_result=None, last_error=None,
           changed_by=actor_id, changed_at=utcnow())


def record_result(engine: Engine, *, store_id: int, result: str,
                  applied_selector: str | None = None,
                  applied_device_name: str | None = None,
                  error: str | None = None) -> None:
    """What the Store said actually happened.

    ``applied_selector`` is written only on success. Writing the requested
    value on failure would make the row claim a change that did not occur, and
    the next operator would read it as the truth about a silent shop.
    """
    if result not in RESULTS:
        raise OutputDeviceRefused(f"{result} is not a known outcome.")
    fields = {"last_result": result, "last_error": (error or "")[:500]}
    if result == RESULT_APPLIED:
        fields["applied_selector"] = applied_selector
        fields["applied_device_name"] = applied_device_name
    _write(engine, store_id, **fields)


def describe(state: dict) -> str:
    """One line for the Receiver Devices table.

    Says what is TRUE, not what was asked for. A pending change reads as
    pending; a failed one reads as failed and still names the device the shop
    is actually playing through, because that is the question somebody has
    when a shop reports silence.
    """
    applied = state.get("applied_device_name") or state.get("applied_selector")
    result = state.get("last_result")
    if result == RESULT_APPLIED and applied:
        return f"playing through {applied}"
    if result == RESULT_REFUSED:
        return (f"the last change was refused by the Store"
                + (f" ({state['last_error']})" if state.get("last_error") else "")
                + (f"; still on {applied}" if applied else ""))
    if result == RESULT_UNSUPPORTED:
        return "this Receiver cannot change its speaker remotely"
    if state.get("requested_selector") and not applied:
        return "a change has been sent and the Store has not answered yet"
    if applied:
        return f"playing through {applied}"
    return "not reported yet"
