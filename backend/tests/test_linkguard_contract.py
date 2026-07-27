"""Contract tests for an LinkGuard integration that does not exist yet.

A full search of this repository finds LinkGuard mentioned 21 times, every one
of them prose in a document. There is no code, executable, configuration or IPC
surface to integrate with, so nothing here integrates with one.

What these tests do instead is pin the contract, using a fake adapter that
behaves the way a real one must. When the real LinkGuard interface arrives, an
adapter that passes this file is a candidate; one that does not, is not.

The rule under all of it: a command sent is not a pause performed.
"""

from __future__ import annotations

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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from linkguard import (  # noqa: E402
    OVERLAP_RISK_OUTCOMES,
    LinkGuardAdapter,
    GuardOutcome,
    GuardResult,
    NullLinkGuard,
    describe_overlap_risk,
)


class FakeLinkGuard:
    """How a real adapter is required to behave. Not a real integration."""

    name = "fake"

    def __init__(self, *, acknowledge: bool = True, refuse: bool = False) -> None:
        self.paused = False
        self.calls: list[str] = []
        self._acknowledge = acknowledge
        self._refuse = refuse

    def pause(self, *, session_id: int, timeout_seconds: float) -> GuardResult:
        self.calls.append("pause")
        if self._refuse:
            return GuardResult(GuardOutcome.REFUSED, "refused by policy")
        if not self._acknowledge:
            return GuardResult(GuardOutcome.TIMED_OUT, "no acknowledgement")
        if self.paused:
            return GuardResult(GuardOutcome.ALREADY_IN_STATE)
        self.paused = True
        return GuardResult(GuardOutcome.PAUSED)

    def resume(self, *, session_id: int, timeout_seconds: float) -> GuardResult:
        self.calls.append("resume")
        if not self._acknowledge:
            return GuardResult(GuardOutcome.TIMED_OUT, "no acknowledgement")
        if not self.paused:
            return GuardResult(GuardOutcome.ALREADY_IN_STATE)
        self.paused = False
        return GuardResult(GuardOutcome.RESUMED)

    def status(self) -> GuardResult:
        return GuardResult(GuardOutcome.PAUSED if self.paused else GuardOutcome.RESUMED)


# ---------------------------------------------------------------------------
# There is genuinely nothing to integrate with
# ---------------------------------------------------------------------------
def test_no_pause_or_resume_interface_exists_to_integrate_with():
    """The gap this module documents.

    An acoustic-verification *message* contract does exist - see the next two
    tests - but nothing anywhere can be asked to pause or resume, so there is
    nothing for a real adapter to call.
    """
    import re

    callers = []
    for path in REPOSITORY_ROOT.rglob("*.py"):
        if any(part in {"node_modules", ".venv", ".git"} for part in path.parts):
            continue
        if path.name in {"linkguard.py", "test_linkguard_contract.py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(r"(?i)linkguard[_.]?(pause|resume|client|service|process)", text):
            line = text[: match.start()].count("\n") + 1
            callers.append(f"{path.relative_to(REPOSITORY_ROOT)}:{line}")

    assert not callers, (
        "something already talks to an LinkGuard pause/resume surface: "
        f"{callers}. Replace NullLinkGuard with a real adapter."
    )


def test_the_acoustic_verification_message_contract_already_exists():
    """Corrects an earlier claim that no LinkGuard contract existed at all.

    receiver_contract.py defines TrustedSpeakerVerifiedEvent with
    source: Literal["linkguard"]. It is a schema, not an implementation - but it
    is the shape a real LinkGuard must speak, and it was already decided.
    """
    from receiver_contract import TrustedSpeakerVerifiedEvent, parse_trusted_verifier_event

    assert callable(parse_trusted_verifier_event)
    assert "source" in TrustedSpeakerVerifiedEvent.model_fields


def test_a_receiver_can_never_claim_speaker_verified():
    """Why that contract is parsed by a separate adapter: the trusted event is
    deliberately excluded from the ordinary acknowledgement union, so a Receiver
    presenting one is rejected rather than believed."""
    from receiver_contract import parse_receiver_ack

    payload = {
        "protocol_version": "1.0",
        "type": "speaker_verified",
        "message_id": "00000000-0000-4000-8000-000000000000",
        "occurred_at": "2026-07-27T00:00:00+00:00",
        "sequence": 1,
        "session_id": 8,
        "source": "linkguard",
    }
    with pytest.raises(Exception):
        parse_receiver_ack(payload)


# ---------------------------------------------------------------------------
# The no-op tells the truth
# ---------------------------------------------------------------------------
def test_the_null_adapter_never_reports_a_pause():
    guard = NullLinkGuard()
    result = guard.pause(session_id=1, timeout_seconds=5)
    assert result.outcome is GuardOutcome.UNAVAILABLE
    assert result.succeeded is False


def test_the_null_adapter_always_flags_overlap_risk():
    guard = NullLinkGuard()
    for result in (
        guard.pause(session_id=1, timeout_seconds=5),
        guard.resume(session_id=1, timeout_seconds=5),
        guard.status(),
    ):
        assert result.overlap_risk is True


def test_the_null_adapter_satisfies_the_protocol():
    assert isinstance(NullLinkGuard(), LinkGuardAdapter)


def test_there_is_no_outcome_meaning_command_sent():
    """The whole failure mode this contract exists to prevent."""
    values = {outcome.value for outcome in GuardOutcome}
    for forbidden in ("sent", "issued", "requested", "dispatched", "ok", "success"):
        assert forbidden not in values


# ---------------------------------------------------------------------------
# What a real adapter must do
# ---------------------------------------------------------------------------
def test_a_pause_must_be_acknowledged_not_merely_attempted():
    guard = FakeLinkGuard(acknowledge=False)
    result = guard.pause(session_id=1, timeout_seconds=0.1)
    assert result.outcome is GuardOutcome.TIMED_OUT
    assert result.succeeded is False
    assert result.overlap_risk is True


def test_pausing_twice_is_idempotent():
    """A retry after a dropped response must be safe."""
    guard = FakeLinkGuard()
    assert guard.pause(session_id=1, timeout_seconds=5).outcome is GuardOutcome.PAUSED
    assert guard.pause(session_id=1, timeout_seconds=5).outcome is GuardOutcome.ALREADY_IN_STATE
    assert guard.paused is True


def test_resuming_is_idempotent_and_safe_after_a_restart():
    """Recovery calls resume without having issued the matching pause."""
    guard = FakeLinkGuard()
    assert guard.resume(session_id=1, timeout_seconds=5).outcome is GuardOutcome.ALREADY_IN_STATE
    assert guard.paused is False


def test_a_normal_stop_resumes_what_a_start_paused():
    guard = FakeLinkGuard()
    guard.pause(session_id=8, timeout_seconds=5)
    assert guard.resume(session_id=8, timeout_seconds=5).outcome is GuardOutcome.RESUMED
    assert guard.paused is False


def test_a_refusal_is_reported_as_a_refusal():
    guard = FakeLinkGuard(refuse=True)
    result = guard.pause(session_id=1, timeout_seconds=5)
    assert result.outcome is GuardOutcome.REFUSED
    assert result.overlap_risk is True


def test_a_fake_adapter_satisfies_the_protocol():
    assert isinstance(FakeLinkGuard(), LinkGuardAdapter)


# ---------------------------------------------------------------------------
# Overlap risk must reach the operator
# ---------------------------------------------------------------------------
def test_every_unconfirmed_outcome_counts_as_overlap_risk():
    assert OVERLAP_RISK_OUTCOMES == {
        GuardOutcome.TIMED_OUT,
        GuardOutcome.REFUSED,
        GuardOutcome.UNAVAILABLE,
    }


def test_a_successful_pause_carries_no_overlap_warning():
    guard = FakeLinkGuard()
    assert describe_overlap_risk(guard.pause(session_id=1, timeout_seconds=5)) == ""


def test_an_unconfirmed_pause_produces_an_operator_sentence():
    guard = FakeLinkGuard(acknowledge=False)
    sentence = describe_overlap_risk(guard.pause(session_id=1, timeout_seconds=0.1))
    assert "may have" in sentence.lower()
    assert "overlap" in sentence.lower()


def test_the_detail_field_is_bounded_and_carries_no_secret():
    """A real adapter must not pass remote free text straight through."""
    guard = NullLinkGuard()
    for result in (guard.pause(session_id=1, timeout_seconds=5), guard.status()):
        assert len(result.detail) < 200
        assert "token" not in result.detail.lower()
        assert "password" not in result.detail.lower()


# ---------------------------------------------------------------------------
# LinkGuard is the only thing that may ever produce SPEAKER_VERIFIED
# ---------------------------------------------------------------------------
def test_no_guard_outcome_can_be_mistaken_for_acoustic_verification():
    values = {outcome.value for outcome in GuardOutcome}
    assert "speaker_verified" not in values
    assert "verified" not in values


def test_nothing_in_this_module_claims_speaker_verification():
    source = (BACKEND_ROOT / "linkguard.py").read_text(encoding="utf-8")
    assert "SPEAKER_VERIFIED" not in source
