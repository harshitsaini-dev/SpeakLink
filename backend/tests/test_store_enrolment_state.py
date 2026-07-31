"""A credential on disk is not a working Store PC.

THE REAL CASE THIS IS BUILT FROM

A second PC reported "This computer is already enrolled - Store: 1" while being,
by every meaningful measure, broken: Receiver process absent, HQ unreachable,
Receiver status none. The credential was genuine. It had been minted against a
throwaway LAN-pilot database where Store 1 was "LAN pilot Store". In the current
HQ, Store 1 is an archived demo Store, and the Store the operator meant - Uttam
Nagar Old - is id 14.

"Store 1" meant three different shops in three databases. A local file cannot
know which, so it must never be presented as success.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools.store_enrolment_state import (  # noqa: E402
    STALE_LOCAL_FILES,
    EnrolmentVerdict,
    assess,
    replace_local_enrolment,
    service_is_answering,
)


#: The actual Device from the second PC.
STALE_DEVICE = "1f5a6c77-3d7d-4ce4-a915-b547ff174a93"
HQ = "http://192.168.4.134:8000"

ENROLLED_LOCALLY = {"enrolled": True, "device_public_id": STALE_DEVICE, "store_id": 1}


def reachable(_address):
    return True


def unreachable(_address):
    return False


# ===========================================================================
# 1. The second-PC case
# ===========================================================================
def test_a_credential_the_current_hq_does_not_know_is_an_old_enrolment():
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: None, service_check=reachable)

    assert result.verdict is EnrolmentVerdict.OLD_ENROLMENT_DETECTED
    assert result.local_enrolled is True
    assert result.hq_authenticated is False


def test_the_old_enrolment_message_is_written_for_a_beginner():
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: None, service_check=reachable)

    assert "Old EchoCast enrolment detected." in result.message
    assert "previous EchoCast pilot server" in result.message
    # It must not imply the Store is broken or that HQ is at fault.
    assert "password" not in result.message.lower()


def test_an_old_enrolment_offers_replacement_and_refuses_repair():
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: None, service_check=reachable)

    assert result.should_replace is True
    assert result.can_repair is False, (
        "repair would reinstall files around an identity the current HQ rejects"
    )


def test_the_old_device_and_store_id_are_shown_as_diagnostics():
    """An operator has to be able to recognise their own machine. These are
    identifiers, not secrets."""
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: None, service_check=reachable)

    assert result.device_public_id == STALE_DEVICE
    assert result.store_id == 1


# ===========================================================================
# 2. HQ down is NOT the same as a stale credential
# ===========================================================================
def test_an_unreachable_hq_is_never_reported_as_an_old_enrolment():
    """The dangerous confusion. Telling an operator to re-enrol because their
    network is down destroys a perfectly good identity."""
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: None, service_check=unreachable)

    assert result.verdict is EnrolmentVerdict.HQ_UNREACHABLE
    assert result.should_replace is False
    assert "cannot tell" in result.message.lower()


def test_an_unreachable_hq_does_not_claim_authentication():
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    service_check=unreachable)
    assert result.hq_authenticated is False
    assert result.hq_reachable is False


# ===========================================================================
# 3. A current, valid Device
# ===========================================================================
CURRENT_RECORD = {
    "store_id": 31, "store_name": "Bindapur", "store_code": "BP",
    "zone": "Zone 3", "device_name": "Store PC 1", "store_archived": False,
}


def test_a_device_the_current_hq_accepts_is_current():
    result = assess(local_status={"enrolled": True, "device_public_id": "ee6160cb",
                                  "store_id": 31},
                    hq_address=HQ, device_lookup=lambda _pid: CURRENT_RECORD,
                    service_check=reachable)

    assert result.verdict is EnrolmentVerdict.CURRENT
    assert result.hq_authenticated is True
    assert result.can_repair is True
    assert result.should_replace is False


def test_a_current_device_is_described_by_name_not_by_number():
    """"Store: 1" is what made the original report unreadable. A shop has a name."""
    result = assess(local_status={"enrolled": True, "device_public_id": "ee6160cb",
                                  "store_id": 31},
                    hq_address=HQ, device_lookup=lambda _pid: CURRENT_RECORD,
                    service_check=reachable)

    assert result.store_name == "Bindapur"
    assert result.store_code == "BP"
    assert result.zone == "Zone 3"
    assert "Bindapur" in result.message


# ===========================================================================
# 4. An archived Store
# ===========================================================================
def test_a_device_on_an_archived_store_must_be_enrolled_again():
    archived = dict(CURRENT_RECORD, store_archived=True, store_name="Mumbai Andheri")
    result = assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                    device_lookup=lambda _pid: archived, service_check=reachable)

    assert result.verdict is EnrolmentVerdict.ARCHIVED_STORE
    assert result.can_repair is False
    assert result.should_replace is True
    assert "archived Store" in result.message


# ===========================================================================
# 5. Not enrolled at all
# ===========================================================================
def test_a_fresh_pc_is_simply_not_enrolled():
    result = assess(local_status={"enrolled": False}, hq_address=HQ)
    assert result.verdict is EnrolmentVerdict.NOT_ENROLLED
    assert result.should_replace is False
    assert result.can_repair is False


# ===========================================================================
# 6. The service check uses /api/, not /
# ===========================================================================
def test_the_service_check_asks_for_the_api_path():
    asked = {}

    class Response:
        status = 200

        def read(self):
            return b'{"service":"EchoCast Live","status":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout=None):
        asked["url"] = url
        return Response()

    assert service_is_answering(HQ, opener=opener) is True
    assert asked["url"] == "http://192.168.4.134:8000/api/", (
        "the check must use /api/; GET / is 404 BY DESIGN and reading that as an "
        "outage sends an operator hunting a healthy server"
    )


def test_a_404_on_the_root_is_not_consulted_at_all():
    """Belt and braces: the root path must never appear in the probe."""
    seen = []

    def opener(url, timeout=None):
        seen.append(url)
        raise OSError("not reached")

    service_is_answering(HQ, opener=opener)
    assert seen == ["http://192.168.4.134:8000/api/"]


def test_a_non_ok_body_is_not_treated_as_healthy():
    class Response:
        status = 200

        def read(self):
            return b'{"detail":"Not Found"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    assert service_is_answering(HQ, opener=lambda u, timeout=None: Response()) is False


# ===========================================================================
# 7. Replacing the local identity removes ONLY the local identity
# ===========================================================================
@pytest.fixture()
def state_root(tmp_path):
    for name in STALE_LOCAL_FILES:
        (tmp_path / name).write_bytes(b"sealed-bytes-that-must-not-be-printed")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "receiver.log").write_text("an operational line", encoding="utf-8")
    (tmp_path / "selected-output.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _stale():
    return assess(local_status=ENROLLED_LOCALLY, hq_address=HQ,
                  device_lookup=lambda _pid: None, service_check=reachable)


def test_replacement_removes_the_credential_and_the_config(state_root):
    result = replace_local_enrolment(state_root=state_root, assessment=_stale())

    assert result.ok is True
    assert sorted(result.removed) == sorted(STALE_LOCAL_FILES)
    for name in STALE_LOCAL_FILES:
        assert not (state_root / name).exists()


def test_replacement_preserves_the_logs(state_root):
    replace_local_enrolment(state_root=state_root, assessment=_stale())

    assert (state_root / "logs" / "receiver.log").exists()
    assert (state_root / "logs" / "receiver.log").read_text(encoding="utf-8") == \
        "an operational line"


def test_replacement_writes_a_diagnostic_before_removing_anything(state_root):
    result = replace_local_enrolment(state_root=state_root, assessment=_stale())

    assert result.diagnostics_path is not None
    assert result.diagnostics_path.exists()
    report = json.loads(result.diagnostics_path.read_text(encoding="utf-8"))
    assert report["assessment"]["device_public_id"] == STALE_DEVICE
    assert report["assessment"]["store_id"] == 1


def test_the_diagnostic_carries_no_credential_bytes(state_root):
    result = replace_local_enrolment(state_root=state_root, assessment=_stale())
    text = result.diagnostics_path.read_text(encoding="utf-8")

    assert "sealed-bytes-that-must-not-be-printed" not in text
    for forbidden in ("credential", "token", "secret", "password", "hmac"):
        assert forbidden not in text.lower(), f"the diagnostic mentions {forbidden}"


def test_replacement_touches_nothing_else_in_the_folder(state_root):
    result = replace_local_enrolment(state_root=state_root, assessment=_stale())

    assert "logs" in result.preserved
    assert "selected-output.json" in result.preserved
    assert (state_root / "selected-output.json").exists()


def test_a_second_replacement_is_harmless(state_root):
    replace_local_enrolment(state_root=state_root, assessment=_stale())
    second = replace_local_enrolment(state_root=state_root, assessment=_stale())

    assert second.ok is True
    assert second.removed == [], "nothing was left to remove"


def test_the_safe_dict_is_safe_to_export():
    detail = _stale().to_safe_dict()
    serialised = json.dumps(detail).lower()

    for forbidden in ("credential", "token", "secret", "password", "hmac", "jwt"):
        assert forbidden not in serialised
    assert detail["device_public_id"] == STALE_DEVICE
