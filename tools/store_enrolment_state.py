"""What "this computer is enrolled" actually means, told apart honestly.

THE FAILURE THIS EXISTS TO PREVENT

A Store PC reported "This computer is already enrolled - Store: 1" and was, by
every meaningful measure, not working: Receiver process absent, HQ unreachable,
Receiver status none. The credential on disk was real. It had been minted against
a throwaway LAN-pilot database that no longer exists, where Store 1 was
"LAN pilot Store". In the current HQ, Store 1 is an archived demo Store, and the
Store the operator had in mind - Uttam Nagar Old - is id 14.

So "Store 1" meant three different shops in three different databases, and a
local file is in no position to know which. A sealed credential proves only that
*this computer once enrolled somewhere*.

FIVE FACTS, NEVER COLLAPSED

    LOCAL_ENROLLED     a sealed credential exists on this PC
    HQ_AUTHENTICATED   the CURRENT HQ accepts that credential
    PROCESS_RUNNING    a Receiver process exists
    CONNECTED          its WebSocket is connected
    READY              it passed its readiness checks

Only the first is knowable offline, and it is the weakest. The wizard must never
present it as success.

NOTHING HERE PRINTS A SECRET. The Device public id and the numeric Store id are
diagnostics an operator needs in order to recognise their own machine; the sealed
credential, its hash and the HMAC keys never leave the functions that use them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

#: The service endpoint that already exists. GET / is 404 BY DESIGN - the root
#: route is declared on a router with prefix="/api" - and treating that 404 as an
#: outage would report a healthy HQ as down.
SERVICE_PATH = "/api/"


class EnrolmentVerdict(str, Enum):
    NOT_ENROLLED = "NOT_ENROLLED"
    #: A credential exists and the current HQ accepts it.
    CURRENT = "CURRENT"
    #: A credential exists and the current HQ does not know this Device. The
    #: second-PC case: enrolled against a server that no longer exists.
    OLD_ENROLMENT_DETECTED = "OLD_ENROLMENT_DETECTED"
    #: Accepted, but the Store it belongs to is archived.
    ARCHIVED_STORE = "ARCHIVED_STORE"
    #: HQ could not be reached, so the credential could not be judged either way.
    HQ_UNREACHABLE = "HQ_UNREACHABLE"


@dataclass(frozen=True)
class EnrolmentAssessment:
    """Every fact separately, plus one sentence for a beginner."""

    verdict: EnrolmentVerdict
    local_enrolled: bool = False
    hq_reachable: bool = False
    hq_authenticated: bool = False
    device_public_id: "str | None" = None
    store_id: "int | None" = None
    store_name: "str | None" = None
    store_code: "str | None" = None
    zone: "str | None" = None
    device_name: "str | None" = None
    hq_address: "str | None" = None
    message: str = ""
    can_repair: bool = False
    should_replace: bool = False

    def to_safe_dict(self) -> dict:
        """Diagnostic-safe. Every field here is an identifier or a state name."""
        return {
            "verdict": self.verdict.value,
            "local_enrolled": self.local_enrolled,
            "hq_reachable": self.hq_reachable,
            "hq_authenticated": self.hq_authenticated,
            "device_public_id": self.device_public_id,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "store_code": self.store_code,
            "zone": self.zone,
            "device_name": self.device_name,
            "hq_address": self.hq_address,
            "can_repair": self.can_repair,
            "should_replace": self.should_replace,
        }


def service_is_answering(hq_address: str, *, opener=None, timeout: float = 5.0) -> bool:
    """Is the HQ backend answering?

    GET /api/ - not GET /. The root path returns 404 by design because the route
    is declared on a router with prefix="/api", and reading that as an outage
    would send an operator hunting a healthy server.
    """
    url = hq_address.rstrip("/") + SERVICE_PATH
    request = opener or urllib.request.urlopen
    try:
        with request(url, timeout=timeout) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return False
    return body.get("status") == "ok"


def assess(*, local_status: dict, hq_address: str, device_lookup=None,
           service_check=None) -> EnrolmentAssessment:
    """Decide what this PC's enrolment actually is.

    ``device_lookup(public_id)`` asks the CURRENT HQ about the Device and returns
    its record, or None when HQ does not know it. It is injected so this logic is
    testable without a server, and so the caller owns the authenticated transport
    rather than this module inventing one.
    """
    enrolled = bool(local_status.get("enrolled"))
    public_id = local_status.get("device_public_id")
    store_id = local_status.get("store_id")

    if not enrolled:
        return EnrolmentAssessment(
            verdict=EnrolmentVerdict.NOT_ENROLLED,
            hq_address=hq_address,
            message="This computer has not been set up yet.",
        )

    check = service_check or service_is_answering
    reachable = check(hq_address)
    if not reachable:
        # Deliberately NOT "old enrolment". A credential cannot be judged stale
        # by a server that never answered, and telling an operator to re-enrol
        # because their network is down would destroy a working identity.
        return EnrolmentAssessment(
            verdict=EnrolmentVerdict.HQ_UNREACHABLE,
            local_enrolled=True, device_public_id=public_id, store_id=store_id,
            hq_address=hq_address,
            message=("This computer has an EchoCast setup, but HQ could not be "
                     "reached, so we cannot tell whether it still works. Check "
                     "the network and the HQ address, then try again."),
        )

    record = (device_lookup or (lambda _pid: None))(public_id)
    if record is None:
        return EnrolmentAssessment(
            verdict=EnrolmentVerdict.OLD_ENROLMENT_DETECTED,
            local_enrolled=True, hq_reachable=True, hq_authenticated=False,
            device_public_id=public_id, store_id=store_id, hq_address=hq_address,
            should_replace=True,
            message=("Old EchoCast enrolment detected.\n"
                     "This PC was enrolled to a previous EchoCast pilot server. "
                     "The current HQ does not know this Device, so it cannot "
                     "receive announcements until it is set up again."),
        )

    if record.get("store_archived"):
        return EnrolmentAssessment(
            verdict=EnrolmentVerdict.ARCHIVED_STORE,
            local_enrolled=True, hq_reachable=True, hq_authenticated=True,
            device_public_id=public_id, store_id=record.get("store_id", store_id),
            store_name=record.get("store_name"), store_code=record.get("store_code"),
            zone=record.get("zone"), device_name=record.get("device_name"),
            hq_address=hq_address, should_replace=True,
            message=("This Device belongs to an archived Store and must be "
                     "enrolled again."),
        )

    return EnrolmentAssessment(
        verdict=EnrolmentVerdict.CURRENT,
        local_enrolled=True, hq_reachable=True, hq_authenticated=True,
        device_public_id=public_id, store_id=record.get("store_id", store_id),
        store_name=record.get("store_name"), store_code=record.get("store_code"),
        zone=record.get("zone"), device_name=record.get("device_name"),
        hq_address=hq_address, can_repair=True,
        message=(f"This computer is set up for "
                 f"{record.get('store_name')} ({record.get('store_code')})."),
    )


# ===========================================================================
# Replacing a stale local identity
# ===========================================================================
#: Removed. Everything else on disk stays.
STALE_LOCAL_FILES = ("receiver-credential.bin", "receiver-config.json")


@dataclass
class ReplacementResult:
    removed: list = field(default_factory=list)
    preserved: list = field(default_factory=list)
    diagnostics_path: "Path | None" = None
    ok: bool = True
    detail: str = ""


def replace_local_enrolment(*, state_root: Path, assessment: EnrolmentAssessment,
                            now=None) -> ReplacementResult:
    """Remove ONLY this PC's stale credential and config.

    What it deliberately does not do:

    * touch the HQ database - the stale Device is not in it, and even when one is,
      retiring a Device server-side is an OWNER decision, not a side effect of a
      Store PC being reinstalled;
    * delete logs - they are the only record of what this machine was doing, and
      they are what a diagnostic is built from;
    * ask a beginner to type a confirmation word. The GUI asks
      "Remove this old Store-PC enrolment and continue?" with Cancel and Remove.
      Typing REMOVE trains people to type it without reading.

    A redacted diagnostic is written FIRST, so the evidence outlives the identity.
    """
    state_root = Path(state_root)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    result = ReplacementResult()

    diagnostics = state_root / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    report = diagnostics / f"replaced-enrolment-{stamp}.json"
    try:
        report.write_text(
            json.dumps({"replaced_at": stamp, "assessment": assessment.to_safe_dict()},
                       indent=2),
            encoding="utf-8")
        result.diagnostics_path = report
    except OSError as failure:
        result.ok = False
        result.detail = f"the diagnostic could not be written ({failure.__class__.__name__})"
        return result

    for name in STALE_LOCAL_FILES:
        candidate = state_root / name
        if candidate.exists():
            try:
                candidate.unlink()
                result.removed.append(name)
            except OSError as failure:
                result.ok = False
                result.detail = f"{name} could not be removed ({failure.__class__.__name__})"
                return result

    for child in state_root.iterdir():
        if child.name not in STALE_LOCAL_FILES and child.name != "diagnostics":
            result.preserved.append(child.name)

    result.detail = "the old enrolment was removed; this PC can be set up again"
    return result
