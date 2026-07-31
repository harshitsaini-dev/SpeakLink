"""The wizard a Store person actually sees.

``store_enrolment_state`` proves the LOGIC of stale detection. This file proves it
is WIRED: that the screen opens, that the button exists, that pressing it calls
the safe replacement, and that the destructive path is not reachable for an
identity the current HQ accepts.

The distinction matters because the previous round shipped correct core logic
that no screen consulted, and the operator still saw "This computer is already
enrolled - Store: 1" on a machine enrolled to a server that no longer exists.

Nothing here touches the live database, the real credential location, or the
second PC. Every test builds its own temporary state directory.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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

from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402
from tools.store_enrolment_state import EnrolmentAssessment, EnrolmentVerdict  # noqa: E402

try:
    import tkinter as tk

    tk.Tk().destroy()
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TK_AVAILABLE, reason="no Tk display available")

STALE_DEVICE = "1f5a6c77-3d7d-4ce4-a915-b547ff174a93"
HQ = "http://192.168.4.134:8000"


def _drain(root, predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _texts(widget) -> str:
    """Every label's text in a screen, so assertions read like what a person sees."""
    found = []
    for child in widget.winfo_children():
        try:
            value = child.cget("text")
            if value:
                found.append(str(value))
        except Exception:
            pass
        found.append(_texts(child))
    return "\n".join(found)


def stale_assessment():
    return EnrolmentAssessment(
        verdict=EnrolmentVerdict.OLD_ENROLMENT_DETECTED,
        local_enrolled=True, hq_reachable=True, hq_authenticated=False,
        device_public_id=STALE_DEVICE, store_id=1, hq_address=HQ,
        should_replace=True,
        message=("Old SpeakLink enrolment detected.\n"
                 "This PC was enrolled to a previous SpeakLink pilot server."),
    )


def current_assessment():
    return EnrolmentAssessment(
        verdict=EnrolmentVerdict.CURRENT,
        local_enrolled=True, hq_reachable=True, hq_authenticated=True,
        device_public_id="ee6160cb-0216-4149-8a8c-14517c14163e", store_id=31,
        store_name="Bindapur", store_code="BP", zone="Zone 3",
        device_name="Store PC 1", hq_address=HQ, can_repair=True,
        message="This computer is set up for Bindapur (BP).",
    )


def unreachable_assessment():
    return EnrolmentAssessment(
        verdict=EnrolmentVerdict.HQ_UNREACHABLE,
        local_enrolled=True, device_public_id=STALE_DEVICE, store_id=1,
        hq_address=HQ,
        message="HQ could not be reached, so we cannot tell whether it still works.",
    )


@pytest.fixture()
def state_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    (root / "receiver-credential.bin").write_bytes(b"sealed-not-for-display")
    (root / "receiver-config.json").write_text("{}", encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    (logs / "receiver.log").write_text("an operational line", encoding="utf-8")
    return root


def build_app(tmp_path, *, assessment=None, state_root=None):
    from tools import store_setup_gui as gui

    return gui.StoreSetupApp(
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
        assessment=assessment,
        state_root=state_root,
    )


# ===========================================================================
# 1. Welcome
# ===========================================================================
def test_a_fresh_computer_opens_on_welcome(tmp_path):
    from tools.store_setup_gui import WelcomeScreen

    app = build_app(tmp_path)
    try:
        assert isinstance(app._current, WelcomeScreen)
        shown = _texts(app._current)
        assert "SpeakLink Store Receiver Setup" in shown
        assert "receive live announcements" in shown
        assert "Start Setup" in shown
    finally:
        app.destroy()


def test_welcome_does_not_offer_status_when_nothing_is_installed(tmp_path):
    app = build_app(tmp_path)
    try:
        assert "Open Receiver Status" not in _texts(app._current)
    finally:
        app.destroy()


# ===========================================================================
# 2. The stale second-PC identity
# ===========================================================================
def test_a_stale_credential_opens_the_old_enrolment_screen(tmp_path, state_root):
    from tools.store_setup_gui import OldEnrolmentScreen

    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        assert isinstance(app._current, OldEnrolmentScreen), (
            "a stale identity did not open the recovery screen; this is the defect"
        )
    finally:
        app.destroy()


def test_the_old_enrolment_screen_says_what_happened_in_plain_words(tmp_path, state_root):
    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        shown = _texts(app._current)
        assert "Old SpeakLink enrolment detected" in shown
        assert "earlier SpeakLink pilot server" in shown or "previous SpeakLink pilot server" in shown
        assert "does not recognise this Device" in shown or "does not know this Device" in shown
    finally:
        app.destroy()


def test_the_old_enrolment_screen_shows_the_diagnostics_but_no_secret(tmp_path, state_root):
    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        shown = _texts(app._current)
        assert STALE_DEVICE in shown, "the operator cannot recognise their own machine"
        assert "1" in shown
        assert HQ in shown
        assert "sealed-not-for-display" not in shown
        for forbidden in ("credential:", "token", "secret", "password", "hmac"):
            assert forbidden not in shown.lower()
    finally:
        app.destroy()


def test_the_primary_button_is_the_replacement(tmp_path, state_root):
    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        assert "Remove Old Enrolment and Set Up Again" in _texts(app._current)
    finally:
        app.destroy()


def test_no_confirmation_word_is_required(tmp_path, state_root):
    """Typing REMOVE trains people to type it without reading, and this project
    has already been bitten by a confirmation that did not confirm."""
    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        shown = _texts(app._current).lower()
        assert "type the confirmation word" not in shown
        assert "type remove" not in shown
    finally:
        app.destroy()


def test_removing_the_old_enrolment_calls_the_safe_replacement(tmp_path, state_root, monkeypatch):
    from tools import store_setup_gui as gui

    calls = {}

    def fake_replace(*, state_root, assessment, now=None):
        calls["state_root"] = state_root
        calls["verdict"] = assessment.verdict
        from tools.store_enrolment_state import ReplacementResult

        return ReplacementResult(removed=list(gui.core_stale_files()), ok=True,
                                 detail="removed")

    monkeypatch.setattr(gui, "replace_local_enrolment", fake_replace)
    monkeypatch.setattr(gui, "confirm_removal", lambda parent: True)
    monkeypatch.setattr(gui, "stop_receiver_task", lambda: None)

    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        app._current._remove_old_enrolment()
        assert _drain(app, lambda: "state_root" in calls)
        assert calls["state_root"] == state_root
        assert calls["verdict"] is EnrolmentVerdict.OLD_ENROLMENT_DETECTED
    finally:
        app.destroy()


def test_cancelling_the_dialog_removes_nothing(tmp_path, state_root, monkeypatch):
    from tools import store_setup_gui as gui

    called = {"replace": False}
    monkeypatch.setattr(gui, "replace_local_enrolment",
                        lambda **k: called.__setitem__("replace", True))
    monkeypatch.setattr(gui, "confirm_removal", lambda parent: False)

    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        app._current._remove_old_enrolment()
        app.update()
        assert called["replace"] is False
        assert (state_root / "receiver-credential.bin").exists()
    finally:
        app.destroy()


def test_after_removal_the_wizard_returns_to_welcome(tmp_path, state_root, monkeypatch):
    from tools import store_setup_gui as gui
    from tools.store_enrolment_state import ReplacementResult
    from tools.store_setup_gui import WelcomeScreen

    monkeypatch.setattr(gui, "replace_local_enrolment",
                        lambda **k: ReplacementResult(ok=True, detail="done"))
    monkeypatch.setattr(gui, "confirm_removal", lambda parent: True)
    monkeypatch.setattr(gui, "stop_receiver_task", lambda: None)

    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        app._current._remove_old_enrolment()
        assert _drain(app, lambda: isinstance(app._current, WelcomeScreen)), (
            "the wizard did not return to Welcome after the identity was removed"
        )
    finally:
        app.destroy()


def test_the_real_replacement_preserves_logs_and_removes_the_credential(tmp_path, state_root,
                                                                        monkeypatch):
    """Not a stub: the real function, through the real button."""
    from tools import store_setup_gui as gui

    monkeypatch.setattr(gui, "confirm_removal", lambda parent: True)
    monkeypatch.setattr(gui, "stop_receiver_task", lambda: None)

    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        app._current._remove_old_enrolment()
        assert _drain(app, lambda: not (state_root / "receiver-credential.bin").exists())
        assert not (state_root / "receiver-config.json").exists()
        assert (state_root / "logs" / "receiver.log").exists(), "the logs were deleted"
        assert (state_root / "logs" / "receiver.log").read_text(encoding="utf-8") == \
            "an operational line"
        exports = list((state_root / "diagnostics").glob("replaced-enrolment-*.json"))
        assert exports, "no redacted diagnostic was exported"
        assert "sealed-not-for-display" not in exports[0].read_text(encoding="utf-8")
    finally:
        app.destroy()


def test_the_stale_screen_offers_no_repair(tmp_path, state_root):
    """Repair would reinstall files around an identity the current HQ rejects."""
    app = build_app(tmp_path, assessment=stale_assessment(), state_root=state_root)
    try:
        assert "Repair" not in _texts(app._current)
    finally:
        app.destroy()


# ===========================================================================
# 3. A valid current identity is NOT sent down the destructive path
# ===========================================================================
def test_a_current_device_opens_the_status_screen(tmp_path, state_root):
    from tools.store_setup_gui import OldEnrolmentScreen, RerunScreen

    app = build_app(tmp_path, assessment=current_assessment(), state_root=state_root)
    try:
        assert isinstance(app._current, RerunScreen)
        assert not isinstance(app._current, OldEnrolmentScreen)
    finally:
        app.destroy()


def test_a_current_device_is_named_not_numbered(tmp_path, state_root):
    app = build_app(tmp_path, assessment=current_assessment(), state_root=state_root)
    try:
        shown = _texts(app._current)
        assert "Bindapur" in shown
        assert "BP" in shown
        assert "Zone 3" in shown
    finally:
        app.destroy()


def test_an_unreachable_hq_never_offers_the_destructive_action(tmp_path, state_root):
    """Telling an operator to re-enrol because their network is down would
    destroy a working identity."""
    app = build_app(tmp_path, assessment=unreachable_assessment(), state_root=state_root)
    try:
        shown = _texts(app._current)
        assert "Remove Old Enrolment and Set Up Again" not in shown
        assert "cannot tell" in shown.lower() or "could not be reached" in shown.lower()
    finally:
        app.destroy()


# ===========================================================================
# 4. The install result is eight separate facts
# ===========================================================================
REQUIRED_RESULT_ROWS = (
    "Files installed",
    "Scheduled Task installed",
    "Receiver process running",
    "HQ reachable",
    "Device authenticated",
    "WebSocket connected",
    "Receiver ready",
    "Test sound heard by operator",
)


def test_the_result_screen_shows_every_check_separately(tmp_path):
    from tools.store_setup_gui import ResultScreen

    app = build_app(tmp_path)
    try:
        checks = {name: False for name in REQUIRED_RESULT_ROWS}
        checks["Files installed"] = True
        screen = ResultScreen(app._container, app, checks=checks)
        shown = _texts(screen)
        for name in REQUIRED_RESULT_ROWS:
            assert name in shown, f"the result screen does not report {name!r}"
    finally:
        app.destroy()


def test_a_partial_install_is_not_reported_as_success(tmp_path):
    from tools.store_setup_gui import ResultScreen

    app = build_app(tmp_path)
    try:
        checks = {name: True for name in REQUIRED_RESULT_ROWS}
        checks["Receiver ready"] = False
        checks["Test sound heard by operator"] = False
        screen = ResultScreen(app._container, app, checks=checks)
        shown = _texts(screen).lower()
        assert "not confirmed" in shown or "no" in shown
        assert "everything is working" not in shown
    finally:
        app.destroy()


def test_test_sound_heard_is_never_speaker_verified(tmp_path):
    from tools.store_setup_gui import ResultScreen

    app = build_app(tmp_path)
    try:
        checks = {name: True for name in REQUIRED_RESULT_ROWS}
        screen = ResultScreen(app._container, app, checks=checks)
        shown = _texts(screen)
        assert "SPEAKER_VERIFIED" not in shown, (
            "an operator hearing a chime is not acoustic verification"
        )
    finally:
        app.destroy()


# ===========================================================================
# 5. Thread discipline
# ===========================================================================
def test_no_tk_variable_is_touched_from_a_worker_thread():
    """Tk is not thread-safe. Every worker must hand its result back through the
    poll callback that runs on the main thread."""
    import ast

    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "work"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                attr = getattr(inner.func, "attr", None)
                if attr in {"set", "config", "configure"}:
                    target = getattr(inner.func, "value", None)
                    name = getattr(target, "attr", "") or getattr(target, "id", "")
                    if "var" in str(name).lower() or "button" in str(name).lower():
                        offenders.append(f"line {inner.lineno}: {name}.{attr}()")
    assert offenders == [], (
        "a background worker touches a Tk widget or variable: " + "; ".join(offenders)
    )


def test_a_worker_exception_is_shown_and_not_swallowed(tmp_path):
    from tools import store_setup_gui as gui

    app = build_app(tmp_path)
    try:
        seen = {}

        def work():
            raise RuntimeError("the installer exploded")

        def done(result):
            seen["result"] = result

        poll = gui._run_in_background(work, done)
        poll(app)
        assert _drain(app, lambda: "result" in seen)
        assert isinstance(seen["result"], Exception), (
            "the worker's exception never reached the callback"
        )
        assert "exploded" in str(seen["result"])
    finally:
        app.destroy()


# ===========================================================================
# 6. An incomplete package is refused before anything is installed
# ===========================================================================
def test_a_package_missing_a_resource_is_refused_at_startup(tmp_path, monkeypatch):
    from tools import resource_paths

    monkeypatch.setenv(resource_paths.RESOURCE_ROOT_ENV, str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()

    missing = resource_paths.missing_resources()
    assert missing, "an empty package reported itself as complete"

    app = build_app(tmp_path)
    try:
        shown = _texts(app._current) + _texts(app)
        assert "incomplete" in shown.lower() or "missing" in shown.lower(), (
            "the wizard did not warn that its own package is incomplete"
        )
    finally:
        app.destroy()
