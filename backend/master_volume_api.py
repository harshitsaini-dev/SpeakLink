"""The Master Volume panel's view of the estate, and its control rules.

WHY THIS IS NOT PART OF THE BROADCAST CONSOLE

Setting a shop's volume is not a broadcasting activity. It is done before the
day starts, after a complaint, or when somebody notices a store has been left
muted since Friday - almost never with an announcement on air. Requiring an
operator to start a broadcast in order to reach a volume slider made the most
routine audio task depend on the least routine one.

WHICH STORES APPEAR

Every Store with an installed, active, primary Receiver Device - **online or
not**. Hiding an offline Store would hide precisely the ones worth looking at:
a shop whose PC is off is a shop that cannot be heard, and the operator needs
to see that rather than an absence.

Retired, disabled and deleted Devices are excluded. A Store whose Receiver was
replaced last month has exactly one current machine, and sending mixer
commands to a historical Device id would target something that is not there.

CONTROL OWNERSHIP

There must never be two independent writers racing the same Windows endpoint.
When a broadcast owns a Store, this panel does not open a second command
channel: it either routes through that broadcast's existing authority (when
the caller is the operator running it) or refuses with
``CONTROLLED_BY_BROADCAST``. It never sends its own competing command.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

#: Reported to the UI instead of a boolean, because the operator's next action
#: differs completely between them. These names are also what the tests assert
#: on, so a rename cannot silently change what an operator is told.
STATUS_ONLINE = "ONLINE"
STATUS_OFFLINE = "OFFLINE"
STATUS_NEEDS_OUTPUT_SELECTION = "NEEDS_OUTPUT_SELECTION"
STATUS_OUTPUT_UNAVAILABLE = "OUTPUT_UNAVAILABLE"
STATUS_CONTROLLED_BY_BROADCAST = "CONTROLLED_BY_BROADCAST"


@dataclass(frozen=True)
class InstalledStore:
    """A Store that has a real machine in it, whether or not it is switched on."""

    store_id: int
    store_code: str
    store_name: str
    #: The estate has no separate `zone` column; `region` is what operators
    #: call a zone and is what Store Management already edits. Surfacing it
    #: under the name the operator uses beats inventing a column that would
    #: then need to be kept in step with the one that already exists.
    zone: str | None
    device_id: int
    device_public_id: str


def installed_stores(engine: Engine, *, store_ids=None) -> list[InstalledStore]:
    """Stores with an active primary Receiver Device.

    ``store_ids`` is the caller's Store Scope. ``None`` means unrestricted; an
    EMPTY collection means scoped to nothing and must stay nothing - the
    distinction matters, because treating empty as "no filter" would hand a
    scoped-to-nothing account the whole estate.
    """
    clauses = [
        "s.is_active = 1",
        "(s.lifecycle_state IS NULL OR s.lifecycle_state NOT IN ('archived', 'deleted'))",
        # The authoritative current machine, not any machine this Store has
        # ever had. Without this join a replaced Device would still appear and
        # could be sent commands.
        "d.status = 'active'",
        "d.deleted_at IS NULL",
        "d.archived_at IS NULL",
        "d.disabled_at IS NULL",
    ]
    parameters: dict = {}
    if store_ids is not None:
        ids = list(store_ids)
        if not ids:
            return []
        clauses.append("s.id IN :store_ids")
        parameters["store_ids"] = tuple(ids)

    statement = text(
        "SELECT s.id, s.store_code, s.store_name, s.region, d.id, d.public_id "
        "FROM stores s "
        "JOIN receiver_store_primary_device p ON p.store_id = s.id "
        "JOIN receiver_devices d ON d.id = p.device_id "
        "WHERE " + " AND ".join(clauses) + " ORDER BY s.store_code"
    )
    if "store_ids" in parameters:
        statement = statement.bindparams(bindparam("store_ids", expanding=True))

    with engine.connect() as connection:
        rows = connection.execute(statement, parameters).fetchall()

    return [
        InstalledStore(store_id=row[0], store_code=row[1], store_name=row[2],
                       zone=row[3], device_id=row[4], device_public_id=row[5])
        for row in rows
    ]


def classify_volume(volume_percent: int | None, *, low: int = 20, high: int = 85) -> str:
    """A visual hint only. The number stays authoritative.

    These thresholds are UI defaults, not business rules, and deliberately
    conservative. A shop is not "too quiet" at 19% in any way this code could
    know - the right level depends on the building - so this classifies for
    scanning a list of forty Stores and nothing else. The percentage is always
    displayed next to it precisely so the label can never be mistaken for a
    judgement the system is qualified to make.
    """
    if volume_percent is None:
        return "unknown"
    if volume_percent <= low:
        return "low"
    if volume_percent >= high:
        return "high"
    return "normal"


def control_status_for(*, online: bool, endpoint_status: str,
                       owned_by_broadcast: bool) -> str:
    """The single status an operator reads, in refusal-first order.

    Order matters. A Store that is offline cannot be helped by re-selecting its
    audio output, so OFFLINE has to win over NEEDS_OUTPUT_SELECTION; and a
    Store a broadcast is steering must say so rather than appear freely
    controllable.
    """
    if not online:
        return STATUS_OFFLINE
    if endpoint_status == "needs_output_selection":
        return STATUS_NEEDS_OUTPUT_SELECTION
    if endpoint_status == "unavailable":
        return STATUS_OUTPUT_UNAVAILABLE
    if endpoint_status != "ready":
        # "unknown" is a Receiver too old to report capabilities at all. It is
        # not controllable and must not present a working-looking slider.
        return STATUS_NEEDS_OUTPUT_SELECTION
    if owned_by_broadcast:
        return STATUS_CONTROLLED_BY_BROADCAST
    return STATUS_ONLINE
