"""Which one computer in a Store is actually playing the announcement.

A Store is the broadcast target. A Device is a physical Windows computer. Once a
Store can hold more than one Device, something has to decide which of them plays,
and getting it wrong is audible: two Devices on one amplifier is an echo, and
nobody on the shop floor can tell which computer to go and look at.

The policy is deliberately dull.

**Exactly one primary per Store, enforced by the schema.** ``store_id`` is the
primary key of the mapping table, so two primaries cannot exist even if two
administrators click Promote in the same second. Application-level "check, then
write" has a window between the two; a primary key does not.

**Standbys connect, heartbeat and report health, and receive no audio.** A
standby that received chunks "just in case" would be the echo. Selecting the
audio target is one function here, so there is one place to be right.

**Promotion is explicit, and losing a primary does not cause a failover.** When a
primary is disabled or revoked, the Store is left with no primary until an
administrator promotes another. Automatic failover sounds helpful and is not: it
moves the announcement onto a computer nobody has confirmed is connected to the
amplifier, and it does it silently, so the first anyone knows is a Store that
went quiet or a Store that unexpectedly did not.

**A legacy Receiver is never given a Device identity.** During the migration a
Store may still be served by a computer holding the shared Store token. It is
shown honestly as a legacy connection; inventing a Device id for it would make
the dashboard lie about what is connected.

The schema change is additive and idempotent - a new table, no ALTER on anything
that already exists - so it can run at startup on every boot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Engine


PRIMARY_TABLE = "receiver_store_primary_device"

#: The Device states that may hold or receive a broadcast.
PROMOTABLE_DEVICE_STATUS = "active"


class DeviceRole(str, Enum):
    PRIMARY = "PRIMARY"
    STANDBY = "STANDBY"


class PrimaryDeviceError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


class DeviceNotPromotableError(PrimaryDeviceError):
    """No such Device, or it is not in a state that may play announcements.

    One class for "unknown", "disabled" and "retired" on purpose: promoting a
    Device an administrator has just switched off would quietly undo the
    switching off, and would put the announcement on it.
    """


class NoPrimaryDeviceError(PrimaryDeviceError):
    """This Store has no primary, so there is nothing to send audio to."""


def _require_utc(value: datetime | None) -> datetime:
    moment = value or datetime.now(timezone.utc)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise PrimaryDeviceError("timestamps must be timezone-aware UTC")
    return moment


def ensure_primary_device_schema(engine: Engine) -> None:
    """Create the mapping table if it is not there. Safe to call on every boot.

    Purely additive: a new table, and no ALTER on ``receiver_devices``. Nothing
    that already works can be broken by applying this, which is what makes it
    safe to run at startup rather than in a maintenance window.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {PRIMARY_TABLE} (
                -- store_id is the PRIMARY KEY, and that is the whole point:
                -- "at most one primary per Store" is then a fact about the
                -- database rather than a rule the application has to remember.
                store_id INTEGER PRIMARY KEY,
                device_id INTEGER NOT NULL UNIQUE,
                promoted_at VARCHAR(40) NOT NULL,
                promoted_by INTEGER,
                CONSTRAINT fk_primary_device_store
                    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE,
                CONSTRAINT fk_primary_device_device
                    FOREIGN KEY (device_id) REFERENCES receiver_devices(id) ON DELETE CASCADE,
                CONSTRAINT fk_primary_device_actor
                    FOREIGN KEY (promoted_by) REFERENCES hq_users(id) ON DELETE SET NULL,
                CONSTRAINT ck_primary_device_utc CHECK (
                    substr(promoted_at, -6) = '+00:00' OR substr(promoted_at, -1) = 'Z'
                )
            )
            """
        )
        # Every audio chunk asks "who is this Store's primary?". The PRIMARY KEY
        # already indexes store_id; this one covers the reverse lookup used when
        # a Device is disabled or revoked.
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{PRIMARY_TABLE}_device "
            f"ON {PRIMARY_TABLE}(device_id)"
        )


def promote_device(
    engine: Engine,
    *,
    device_public_id: str,
    actor_user_id: int | None,
    now: datetime | None = None,
) -> dict:
    """Make one Device its Store's primary, in one transaction.

    Promoting the Device that is already primary is deliberately harmless: an
    operator double-clicking must not produce two rows, or an error that looks
    like something went wrong.
    """
    if not isinstance(device_public_id, str) or not device_public_id.strip():
        raise DeviceNotPromotableError("a Receiver Device identifier is required")
    moment = _require_utc(now)
    public_id = device_public_id.strip()

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            device = connection.execute(
                text(
                    "SELECT d.id, d.store_id, d.status, s.is_active "
                    "FROM receiver_devices d JOIN stores s ON s.id = d.store_id "
                    "WHERE d.public_id = :public_id"
                ),
                {"public_id": public_id},
            ).one_or_none()
            if device is None:
                raise DeviceNotPromotableError("no Receiver Device with that identifier")
            if device.status != PROMOTABLE_DEVICE_STATUS:
                raise DeviceNotPromotableError(
                    "only an active Receiver Device can be promoted to primary"
                )
            if not bool(device.is_active):
                raise DeviceNotPromotableError("that Store is not active")

            # Delete-then-insert rather than upsert: the Device may currently be
            # another row's primary, and both rows have to go before this one can
            # exist without tripping the UNIQUE on device_id.
            connection.execute(
                text(f"DELETE FROM {PRIMARY_TABLE} WHERE store_id = :store_id OR device_id = :device_id"),
                {"store_id": device.store_id, "device_id": device.id},
            )
            connection.execute(
                text(
                    f"INSERT INTO {PRIMARY_TABLE} (store_id, device_id, promoted_at, promoted_by) "
                    "VALUES (:store_id, :device_id, :now, :actor)"
                ),
                {
                    "store_id": device.store_id,
                    "device_id": device.id,
                    "now": moment.isoformat(),
                    "actor": actor_user_id,
                },
            )
            connection.commit()
        except PrimaryDeviceError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise PrimaryDeviceError(
                "the promotion was rolled back; the previous primary is unchanged"
            ) from None

    return {
        "device_public_id": public_id,
        "store_id": device.store_id,
        "promoted_at": moment.isoformat(),
    }


def primary_device_id(engine: Engine, *, store_id: int) -> int | None:
    """The Device row id that may receive this Store's audio, or None."""
    with engine.connect() as connection:
        return connection.execute(
            text(f"SELECT device_id FROM {PRIMARY_TABLE} WHERE store_id = :store_id"),
            {"store_id": store_id},
        ).scalar_one_or_none()


def clear_primary_for_device(engine: Engine, *, device_id: int) -> bool:
    """Remove this Device's primary role. Deliberately promotes nothing.

    Called when a primary is disabled or revoked. The Store is then left with no
    primary until an administrator chooses one, which is the point: a silent
    failover puts the announcement on a computer nobody has checked.
    """
    with engine.begin() as connection:
        removed = connection.execute(
            text(f"DELETE FROM {PRIMARY_TABLE} WHERE device_id = :device_id"),
            {"device_id": device_id},
        ).rowcount
    return bool(removed)


def describe_store_devices(engine: Engine, *, store_id: int) -> list[dict]:
    """Every Device of one Store, each with its role. No credential material."""
    with engine.connect() as connection:
        columns = {c[1] for c in connection.exec_driver_sql("PRAGMA table_info(receiver_devices)")}
        archived_select = "d.archived_at" if "archived_at" in columns else "NULL AS archived_at"
        rows = connection.execute(
            text(
                f"SELECT d.public_id, d.display_name, d.status, d.enrolled_at, "
                f"       d.disabled_at, {archived_select}, "
                "       p.device_id AS primary_device_id, d.id AS device_id, "
                "       p.promoted_at "
                f"FROM receiver_devices d LEFT JOIN {PRIMARY_TABLE} p "
                "  ON p.store_id = d.store_id AND p.device_id = d.id "
                "WHERE d.store_id = :store_id ORDER BY d.id"
            ),
            {"store_id": store_id},
        ).all()
    return [
        {
            "public_id": row.public_id,
            "display_name": row.display_name,
            "status": row.status,
            "enrolled_at": row.enrolled_at,
            "disabled_at": row.disabled_at,
            "archived_at": row.archived_at,
            "role": DeviceRole.PRIMARY if row.primary_device_id else DeviceRole.STANDBY,
            "promoted_at": row.promoted_at,
        }
        for row in rows
    ]


def select_audio_target_device(engine: Engine, *, store_id: int) -> int | None:
    """The one Device id that may receive live audio for this Store.

    One function, so there is one place to be right about it. It never falls back
    to "any connected Device": a Store with no primary receives no audio, and the
    dashboard says so, rather than the announcement appearing on whichever
    computer happened to connect first.
    """
    return primary_device_id(engine, store_id=store_id)


def connection_roles(
    *,
    recorded_primary_device_id: int | None,
    connecting_device_id: int | None,
) -> tuple[bool, bool]:
    """Decide what an incoming Receiver socket is: ``(is_primary, demote_old)``.

    Three cases, and the middle one is the subtle decision.

    * **A legacy Receiver** has no Device identity and keeps the old
      one-connection-per-Store behaviour untouched.
    * **A Store with no recorded primary** is not yet under the policy, so an
      enrolled Device behaves exactly as a legacy Receiver did. The alternative -
      "no primary, no audio" - would mean that enrolling a Device took its Store
      off the air until somebody clicked Promote, which is not an upgrade anybody
      survives across 44 Stores. Nothing is written here: the Store stays without
      a primary, it is simply not yet governed by one.
    * **A Store with a recorded primary** enforces it: that Device receives audio
      and every other Device is a standby.

    ``demote_old`` is true only for a genuine promotion. It is never inferred
    from the device ids, because under ``dual_verify`` a legacy Store token
    authenticates through the backfilled Device, so an ordinary reconnect also
    presents two different ids while plainly meaning "replace me".
    """
    if connecting_device_id is None or recorded_primary_device_id is None:
        return True, False
    is_primary = recorded_primary_device_id == connecting_device_id
    return is_primary, is_primary


#: How honest the Store-level answer has to be. A Store is not READY because one
#: of its computers is; it is READY when the computer that will actually play the
#: announcement is.
_NO_PRIMARY = "NO_PRIMARY"
_LEGACY_ONLY = "LEGACY_ONLY"


def store_aggregate_state(
    engine: Engine,
    *,
    store_id: int,
    device_states: dict,
    legacy_connection_state: str | None = None,
) -> dict:
    """Derive one Store-level answer from per-Device state, without overclaiming.

    The aggregate follows the **primary**, not the best of its Devices. A standby
    reporting READY says nothing about whether this Store can play an
    announcement, and reporting the maximum would turn "the spare is fine" into
    "the Store is fine".

    Nothing here can produce SPEAKER_VERIFIED. A Device knows a sound card
    accepted frames; it cannot know an amplifier was on or anybody heard
    anything. Only EchoGuard acoustic detection may say that.
    """
    devices = describe_store_devices(engine, store_id=store_id)
    primary = next((device for device in devices if device["role"] == DeviceRole.PRIMARY), None)
    standby_count = sum(
        1
        for device in devices
        if device["role"] == DeviceRole.STANDBY and device["status"] == PROMOTABLE_DEVICE_STATUS
    )

    if primary is not None:
        state = device_states.get(primary["public_id"], "OFFLINE")
    elif legacy_connection_state is not None:
        # Honest: a Store still served by the shared token is running, but it has
        # no Device, so it has no primary either.
        state = _LEGACY_ONLY
    else:
        state = _NO_PRIMARY

    return {
        "store_id": store_id,
        "state": state,
        "has_primary": primary is not None,
        "primary_device_public_id": primary["public_id"] if primary else None,
        "primary_state": device_states.get(primary["public_id"]) if primary else None,
        "standby_count": standby_count,
        "legacy_connection": legacy_connection_state,
        "devices": devices,
        # Never derived from a Receiver. EchoGuard's to give, and only EchoGuard's.
        "speaker_verified": False,
    }


__all__ = [
    "PRIMARY_TABLE",
    "DeviceNotPromotableError",
    "DeviceRole",
    "NoPrimaryDeviceError",
    "PrimaryDeviceError",
    "clear_primary_for_device",
    "connection_roles",
    "describe_store_devices",
    "ensure_primary_device_schema",
    "primary_device_id",
    "promote_device",
    "select_audio_target_device",
    "store_aggregate_state",
]
