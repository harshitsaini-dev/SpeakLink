"""What HQ wants each Store's Windows mixer to be. Always, not only offline.

THE CHANGE THIS REPLACES

This started life as a queue of commands for Stores that happened to be
switched off. That framing made the controls themselves conditional: a shop
whose PC was off got a greyed-out slider and the words "immediate control
unavailable", which answered a question the operator had not asked. They were
not trying to change a mixer *this second*; they were trying to say what the
shop should be set to.

So TARGET STATE is now a first-class, always-present fact about a Store, and
the operator may set it at any time. Whether it has reached the machine yet is
a **separate** question, answered by comparing it with what the Receiver last
actually read.

THE THREE FACTS, WHICH ARE NEVER MERGED

    TARGET     what HQ wants        - persisted here, set by an operator
    ACTUAL      what Windows reports - runtime only, from the Receiver
    CONNECTION  can it be applied now - runtime only, from the socket

Every misleading thing this feature could say comes from collapsing two of
those. "Applied 70%" is TARGET wearing ACTUAL's clothes. A disabled slider is
CONNECTION deciding what TARGET is allowed to be.

ONE ROW PER STORE

``store_id`` is the PRIMARY KEY, so an operator who sets 20, 40, 50 and 70 has
expressed one intention - 70 - not a four-command queue to replay at a shop
when it wakes up. Latest-wins is a property of the table rather than a rule the
application has to remember, which is why no unbounded queue can exist.

NO SECRETS

Store, Device, the requested levels, who asked and when. No credential, token,
Settings Password or JWT: this row is read by the panel, quoted in audit trails
and dumped in diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

TARGET_TABLE = "store_audio_target_state"

#: The legacy offline-only table. Read once at startup so intent captured
#: before this rework is not silently dropped, then left alone.
LEGACY_TABLE = "store_audio_pending_commands"

#: How the TARGET state relates to the ACTUAL one. Derived, never stored - a
#: stored status would go stale the moment a Store reported anything.
SYNC_SYNCED = "SYNCED"
SYNC_APPLYING = "APPLYING"
#: Online, reachable, and genuinely different from what HQ wants -
#: normally because somebody at the till moved the slider. HQ does not
#: fight them; the operator can re-assert the desired level if they want.
SYNC_OUT_OF_SYNC = "OUT_OF_SYNC"
SYNC_WAITING = "WAITING_FOR_SYNC"
SYNC_FAILED = "SYNC_FAILED"
SYNC_UNKNOWN = "UNKNOWN"
SYNC_NO_TARGET = "NO_TARGET_STATE"


class StoreAudioTargetError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


class EmptyTargetStateError(StoreAudioTargetError):
    """Neither a volume nor a mute was asked for."""


class InvalidTargetVolumeError(StoreAudioTargetError):
    def __init__(self, value: object) -> None:
        super().__init__(f"volume must be an integer 0-100, got {value!r}")


@dataclass(frozen=True)
class TargetAudioState:
    store_id: int
    device_id: int
    volume_percent: int | None
    muted: bool | None
    created_by: int | None
    updated_at: str
    #: Set only when an attempt to apply this target state actually failed.
    #: Cleared by any fresh instruction, because the operator is asking for
    #: something new rather than retrying the old one.
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "device_id": self.device_id,
            "volume_percent": self.volume_percent,
            "muted": self.muted,
            "created_by": self.created_by,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_target_audio_schema(engine: Engine) -> None:
    """Create the table if absent, and carry over any legacy intent.

    Purely additive - a new table, no ALTER of anything that already works -
    so it is safe on every boot rather than in a maintenance window.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
                -- PRIMARY KEY on store_id IS the latest-wins rule. There is
                -- therefore no such thing as a queue of pending commands.
                store_id INTEGER PRIMARY KEY,
                device_id INTEGER NOT NULL,
                volume_percent INTEGER,
                muted INTEGER,
                created_by INTEGER,
                updated_at VARCHAR(40) NOT NULL,
                last_error VARCHAR(200),
                CONSTRAINT fk_target_audio_store
                    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
                CONSTRAINT fk_target_audio_device
                    FOREIGN KEY (device_id) REFERENCES receiver_devices(id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_target_audio_actor
                    FOREIGN KEY (created_by) REFERENCES hq_users(id) ON DELETE SET NULL,
                CONSTRAINT ck_target_audio_volume CHECK (
                    volume_percent IS NULL
                    OR (volume_percent >= 0 AND volume_percent <= 100)
                ),
                -- A row that asks for nothing would render as a target state
                -- that desires nothing.
                CONSTRAINT ck_target_audio_not_empty CHECK (
                    volume_percent IS NOT NULL OR muted IS NOT NULL
                ),
                CONSTRAINT ck_target_audio_utc CHECK (
                    substr(updated_at, -6) = '+00:00' OR substr(updated_at, -1) = 'Z'
                )
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{TARGET_TABLE}_device "
            f"ON {TARGET_TABLE}(device_id)"
        )

        # Carry over the older offline-only rows exactly once. An operator's
        # instruction should not be lost because the feature it was typed into
        # was renamed underneath them.
        legacy = connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name = ?",
            (LEGACY_TABLE,)).fetchone()
        if legacy:
            connection.exec_driver_sql(
                f"""
                INSERT OR IGNORE INTO {TARGET_TABLE}
                    (store_id, device_id, volume_percent, muted, created_by,
                     updated_at, last_error)
                SELECT store_id, device_id, volume_percent, muted, created_by,
                       created_at, last_error
                FROM {LEGACY_TABLE}
                """
            )


def _validate_volume(value: object) -> int | None:
    if value is None:
        return None
    # bool is an int in Python, and True would quietly become 1%.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTargetVolumeError(value)
    if not 0 <= value <= 100:
        raise InvalidTargetVolumeError(value)
    return value


def set_target(engine: Engine, *, store_id: int, device_id: int,
                volume_percent: int | None = None, muted: bool | None = None,
                created_by: int | None = None) -> TargetAudioState:
    """Record what HQ wants. Replaces any previous intention for this Store.

    A partial instruction MERGES with what is already desired: an operator who
    only presses Mute has not said anything about the volume, and blanking the
    level they set five minutes ago would be inventing an instruction they
    never gave.
    """
    volume_percent = _validate_volume(volume_percent)
    if volume_percent is None and muted is None:
        raise EmptyTargetStateError(
            "a target state must request a volume, a mute, or both")

    existing = get_target(engine, store_id=store_id)
    if existing is not None:
        if volume_percent is None:
            volume_percent = existing.volume_percent
        if muted is None:
            muted = existing.muted

    updated_at = _utc_now_text()
    with engine.begin() as connection:
        connection.execute(
            text(f"""
                INSERT INTO {TARGET_TABLE}
                    (store_id, device_id, volume_percent, muted, created_by,
                     updated_at, last_error)
                VALUES
                    (:store_id, :device_id, :volume_percent, :muted, :created_by,
                     :updated_at, NULL)
                ON CONFLICT(store_id) DO UPDATE SET
                    device_id = excluded.device_id,
                    volume_percent = excluded.volume_percent,
                    muted = excluded.muted,
                    created_by = excluded.created_by,
                    updated_at = excluded.updated_at,
                    -- A fresh instruction clears the previous failure. The
                    -- operator is asking for something new, not retrying.
                    last_error = NULL
            """),
            {"store_id": store_id, "device_id": device_id,
             "volume_percent": volume_percent,
             "muted": None if muted is None else int(bool(muted)),
             "created_by": created_by, "updated_at": updated_at},
        )
    return TargetAudioState(
        store_id=store_id, device_id=device_id, volume_percent=volume_percent,
        muted=muted, created_by=created_by, updated_at=updated_at)


_SELECT = (
    "SELECT store_id, device_id, volume_percent, muted, created_by, "
    f"updated_at, last_error FROM {TARGET_TABLE}"
)


def _row_to_state(row) -> TargetAudioState:
    return TargetAudioState(
        store_id=row[0], device_id=row[1], volume_percent=row[2],
        muted=None if row[3] is None else bool(row[3]),
        created_by=row[4], updated_at=row[5], last_error=row[6])


def get_target(engine: Engine, *, store_id: int) -> TargetAudioState | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"{_SELECT} WHERE store_id = :store_id"),
            {"store_id": store_id}).fetchone()
    return _row_to_state(row) if row else None


def all_target(engine: Engine) -> dict[int, TargetAudioState]:
    with engine.connect() as connection:
        rows = connection.execute(text(_SELECT)).fetchall()
    return {row[0]: _row_to_state(row) for row in rows}


def clear_target(engine: Engine, *, store_id: int) -> bool:
    """Forget what HQ wanted, leaving the Store to whatever it is doing.

    Deliberately NOT called after a successful apply. Target state is a
    standing intention, not a one-shot command: knowing a shop is *meant* to be
    at 70% is exactly what makes it possible to notice later that it is not.
    """
    with engine.begin() as connection:
        result = connection.execute(
            text(f"DELETE FROM {TARGET_TABLE} WHERE store_id = :store_id"),
            {"store_id": store_id})
    return bool(result.rowcount)


def mark_sync_failed(engine: Engine, *, store_id: int, error: str) -> None:
    """Record why the target state could not be reached.

    The target state itself SURVIVES. What the operator asked for has not
    stopped being what they want just because one attempt failed, and deleting
    it here would make a failure look exactly like a success.
    """
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {TARGET_TABLE} SET last_error = :error "
                 "WHERE store_id = :store_id"),
            {"error": str(error)[:200], "store_id": store_id})


def clear_sync_error(engine: Engine, *, store_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {TARGET_TABLE} SET last_error = NULL "
                 "WHERE store_id = :store_id"),
            {"store_id": store_id})


def sync_state(*, desired, actual_volume_percent, actual_muted, online: bool,
               applying: bool = False) -> str:
    """How TARGET currently relates to ACTUAL. Derived on every read.

    Never stored. A stored status would be a fourth thing to keep in step with
    the three facts it is computed from, and it would go stale the moment a
    Store reported anything.
    """
    if desired is None:
        return SYNC_NO_TARGET
    if desired.last_error:
        # An attempt was made and failed. That is worth saying even while the
        # Store is offline again, because the operator's instruction is still
        # outstanding for a reason they can act on.
        return SYNC_FAILED
    if actual_volume_percent is None and actual_muted is None:
        # Nothing has ever been heard from this Store, so there is nothing to
        # compare against. Not "out of sync" - simply unknown.
        return SYNC_WAITING if not online else SYNC_APPLYING
    matches = (
        (desired.volume_percent is None
         or desired.volume_percent == actual_volume_percent)
        and (desired.muted is None or bool(desired.muted) == bool(actual_muted))
    )
    if matches:
        return SYNC_SYNCED
    if not online:
        # A real difference that cannot be resolved until the machine is back.
        return SYNC_WAITING
    if applying:
        return SYNC_APPLYING
    # Online, reachable, and genuinely different - which is the normal result
    # of somebody at the till moving the slider. HQ deliberately does NOT
    # answer that with another command, so this is a standing observation
    # rather than a transient state, and it must not be called APPLYING when
    # nothing is being applied.
    return SYNC_OUT_OF_SYNC
