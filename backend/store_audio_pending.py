"""What HQ wants an OFFLINE Store's mixer to be, once it comes back.

THE PROBLEM THIS SOLVES

A Store PC is switched off overnight. An operator knows it was left at 90% and
wants it at 30% before the shop opens. Refusing to accept that instruction
until the machine happens to be online makes the operator set an alarm; and
accepting it while pretending it was applied is a lie that shows up as an
80-decibel announcement at 7am.

So the instruction is accepted, stored, and labelled honestly: **pending on
reconnect**, never *applied*, and never *currently*.

WHY THIS IS PERSISTED WHEN LIVE MIXER STATE IS NOT

Live readings are runtime-only on purpose - a slider drag would otherwise write
a row per pixel. A pending instruction is the exact opposite: its entire
purpose is to outlive the disconnection, and an HQ restart at 3am must not
quietly discard it.

LATEST WINS, BY THE SCHEMA

``store_id`` is the PRIMARY KEY, so a Store can have at most **one** pending
instruction and the newest simply replaces the older one. An operator who
sends 30, then 50, then 70 to an offline Store has expressed one wish - 70 -
not a three-command queue to be replayed at the shop when it wakes up. Making
that a property of the table rather than a rule the application remembers is
what stops an unbounded command queue existing in the first place.

NO SECRETS

The row carries operational metadata only: which Store, which Device, the
requested state, who asked and when. No credential, no token, no Settings
Password, no JWT. This table is read by the panel, dumped in diagnostics and
quoted in audit trails, so anything secret in it would leak by design.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

PENDING_TABLE = "store_audio_pending_commands"

STATUS_PENDING = "pending"
STATUS_FAILED = "failed"


class StoreAudioPendingError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


class EmptyPendingCommandError(StoreAudioPendingError):
    """Neither a volume nor a mute was asked for.

    Storing a row that requests nothing would produce a "Pending on reconnect"
    badge that changes nothing when it applies, which is indistinguishable from
    a bug to whoever is watching.
    """


class InvalidPendingVolumeError(StoreAudioPendingError):
    def __init__(self, value: object) -> None:
        super().__init__(f"volume must be an integer 0-100, got {value!r}")


@dataclass(frozen=True)
class PendingAudioCommand:
    store_id: int
    device_id: int
    volume_percent: int | None
    muted: bool | None
    created_by: int | None
    created_at: str
    status: str
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "device_id": self.device_id,
            "volume_percent": self.volume_percent,
            "muted": self.muted,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status,
            "last_error": self.last_error,
        }


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_pending_audio_schema(engine: Engine) -> None:
    """Create the table if absent. Safe on every boot.

    Purely additive - a new table, no ALTER of anything that already works -
    which is what makes it safe to run at startup rather than in a maintenance
    window.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {PENDING_TABLE} (
                -- PRIMARY KEY on store_id is the "latest wins" rule, enforced
                -- by the database rather than remembered by the application.
                -- There is therefore no such thing as a command queue here.
                store_id INTEGER PRIMARY KEY,
                device_id INTEGER NOT NULL,
                volume_percent INTEGER,
                muted INTEGER,
                created_by INTEGER,
                created_at VARCHAR(40) NOT NULL,
                status VARCHAR(16) NOT NULL DEFAULT 'pending',
                last_error VARCHAR(200),
                CONSTRAINT fk_pending_audio_store
                    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
                CONSTRAINT fk_pending_audio_device
                    FOREIGN KEY (device_id) REFERENCES receiver_devices(id) ON DELETE CASCADE,
                CONSTRAINT fk_pending_audio_actor
                    FOREIGN KEY (created_by) REFERENCES hq_users(id) ON DELETE SET NULL,
                CONSTRAINT ck_pending_audio_volume CHECK (
                    volume_percent IS NULL
                    OR (volume_percent >= 0 AND volume_percent <= 100)
                ),
                -- A row that asks for nothing would render as a pending change
                -- that changes nothing.
                CONSTRAINT ck_pending_audio_not_empty CHECK (
                    volume_percent IS NOT NULL OR muted IS NOT NULL
                ),
                CONSTRAINT ck_pending_audio_utc CHECK (
                    substr(created_at, -6) = '+00:00' OR substr(created_at, -1) = 'Z'
                )
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{PENDING_TABLE}_device "
            f"ON {PENDING_TABLE}(device_id)"
        )


def _validate_volume(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPendingVolumeError(value)
    if not 0 <= value <= 100:
        raise InvalidPendingVolumeError(value)
    return value


def set_pending(engine: Engine, *, store_id: int, device_id: int,
                volume_percent: int | None = None, muted: bool | None = None,
                created_by: int | None = None) -> PendingAudioCommand:
    """Replace this Store's desired state. The newest instruction wins."""
    volume_percent = _validate_volume(volume_percent)
    if volume_percent is None and muted is None:
        raise EmptyPendingCommandError(
            "a pending change must request a volume, a mute, or both")

    created_at = _utc_now_text()
    with engine.begin() as connection:
        # A plain REPLACE would also work on SQLite, but the explicit upsert
        # says what is meant and keeps the row's identity (and its foreign
        # keys) rather than deleting and re-inserting underneath them.
        connection.execute(
            text(f"""
                INSERT INTO {PENDING_TABLE}
                    (store_id, device_id, volume_percent, muted, created_by,
                     created_at, status, last_error)
                VALUES
                    (:store_id, :device_id, :volume_percent, :muted, :created_by,
                     :created_at, :status, NULL)
                ON CONFLICT(store_id) DO UPDATE SET
                    device_id = excluded.device_id,
                    volume_percent = excluded.volume_percent,
                    muted = excluded.muted,
                    created_by = excluded.created_by,
                    created_at = excluded.created_at,
                    status = excluded.status,
                    -- A fresh instruction clears the previous failure: the
                    -- operator is not asking to retry the old one, they are
                    -- asking for something new.
                    last_error = NULL
            """),
            {"store_id": store_id, "device_id": device_id,
             "volume_percent": volume_percent,
             "muted": None if muted is None else int(bool(muted)),
             "created_by": created_by, "created_at": created_at,
             "status": STATUS_PENDING},
        )
    return PendingAudioCommand(
        store_id=store_id, device_id=device_id, volume_percent=volume_percent,
        muted=muted, created_by=created_by, created_at=created_at,
        status=STATUS_PENDING)


def _row_to_command(row) -> PendingAudioCommand:
    return PendingAudioCommand(
        store_id=row[0], device_id=row[1], volume_percent=row[2],
        muted=None if row[3] is None else bool(row[3]),
        created_by=row[4], created_at=row[5], status=row[6], last_error=row[7])


_SELECT = (
    "SELECT store_id, device_id, volume_percent, muted, created_by, "
    f"created_at, status, last_error FROM {PENDING_TABLE}"
)


def get_pending(engine: Engine, *, store_id: int) -> PendingAudioCommand | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"{_SELECT} WHERE store_id = :store_id"),
            {"store_id": store_id}).fetchone()
    return _row_to_command(row) if row else None


def all_pending(engine: Engine) -> dict[int, PendingAudioCommand]:
    with engine.connect() as connection:
        rows = connection.execute(text(_SELECT)).fetchall()
    return {row[0]: _row_to_command(row) for row in rows}


def clear_pending(engine: Engine, *, store_id: int) -> bool:
    """Remove the desired state. Used by Cancel, and after a SUCCESSFUL apply.

    Clearing only after a confirmed apply is deliberate: a pending change that
    is dropped on attempt would silently disappear when the attempt failed, and
    the operator would have no way to tell that from success.
    """
    with engine.begin() as connection:
        result = connection.execute(
            text(f"DELETE FROM {PENDING_TABLE} WHERE store_id = :store_id"),
            {"store_id": store_id})
    return bool(result.rowcount)


def mark_failed(engine: Engine, *, store_id: int, error: str) -> None:
    """Keep the instruction, record why it did not apply.

    The row survives on purpose. The operator's wish has not been granted, and
    deleting it here would make a failure look exactly like a success.
    """
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {PENDING_TABLE} SET status = :status, "
                 "last_error = :error WHERE store_id = :store_id"),
            {"status": STATUS_FAILED, "error": str(error)[:200],
             "store_id": store_id})
