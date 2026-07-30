"""The StoreSetup window: what can be driven, and what can only be read.

The four screens are exercised by constructing them directly against a fake
protector and a temporary credential path - no real DPAPI, no real network, no
real audio device. Long operations still spawn a background thread in these
tests; each test drains it with a bounded wait rather than trusting timing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402

try:
    import tkinter as tk

    tk.Tk().destroy()
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TK_AVAILABLE, reason="no Tk display available")


def _drain(root, predicate, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def app(tmp_path):
    from tools import store_setup_gui as gui

    application = gui.StoreSetupApp(
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
    )
    yield application
    application.destroy()


def test_a_fresh_computer_opens_on_the_connection_screen(app):
    from tools.store_setup_gui import ConnectionScreen

    assert isinstance(app._current, ConnectionScreen)


def test_an_already_enrolled_computer_opens_on_the_rerun_screen(tmp_path):
    from tools import receiver_agent, store_setup_gui as gui

    protector = FakeCredentialProtector("test-computer")
    credential_path = tmp_path / "cred.bin"
    valid = "speaklink_rcv_v1.11111111-1111-1111-1111-111111111111." + "A" * 43

    class _Transport:
        def post_json(self, url, payload, *, timeout):
            return 201, {"device_public_id": "dev-9", "store_id": 4,
                        "credential": valid}

    receiver_agent.enrol(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=protector,
        transport=_Transport(),
    )

    application = gui.StoreSetupApp(credential_path=credential_path, protector=protector)
    try:
        from tools.store_setup_gui import RerunScreen

        assert isinstance(application._current, RerunScreen)
    finally:
        application.destroy()


def test_the_connection_screen_defaults_to_the_documented_url(app):
    assert app.state_data["backend_url"] == "http://192.168.4.134:8000"


def test_test_connection_enables_next_only_on_success(app, monkeypatch):
    from tools import store_setup_core as core_module

    screen = app._current
    monkeypatch.setattr(core_module, "test_hq_connection", lambda *a, **k: core_module.ConnectionResult(
        state=core_module.ConnectionState.CONNECTED_TO_HQ, detail="ok",
        base_url="https://hq.example.com"))
    assert str(screen.next_button["state"]) == "disabled"
    screen._test_connection()
    assert _drain(app, lambda: str(screen.next_button["state"]) == "normal")


def test_test_connection_leaves_next_disabled_on_failure(app, monkeypatch):
    from tools import store_setup_core as core_module

    screen = app._current
    monkeypatch.setattr(core_module, "test_hq_connection", lambda *a, **k: core_module.ConnectionResult(
        state=core_module.ConnectionState.CONNECTION_FAILED, detail="no"))
    screen._test_connection()
    _drain(app, lambda: screen.status_var.get() != "CONNECTING...")
    assert str(screen.next_button["state"]) == "disabled"


def test_the_enrolment_code_field_is_masked_until_show_is_checked(app):
    app.go_to_enrolment()
    screen = app._current
    assert screen.code_entry["show"] == "*"
    screen.show_var.set(True)
    screen._toggle_show()
    assert screen.code_entry["show"] == ""


def test_a_generic_failure_never_leaks_the_backend_detail(app, monkeypatch):
    from tools import store_setup_core as core_module

    app.go_to_enrolment()
    screen = app._current
    monkeypatch.setattr(core_module, "redeem_enrollment", lambda **k: core_module.EnrolmentUiResult(
        state=core_module.EnrolmentUiState.REFUSED, detail=core_module.GENERIC_ENROLMENT_FAILURE))
    screen.code_var.set("ECHO-SOMETHING")
    screen._enroll()
    _drain(app, lambda: screen.status_var.get() != "ENROLLING...")
    assert screen.status_var.get() == core_module.GENERIC_ENROLMENT_FAILURE
    assert screen.code_var.get() == "", "the code must not remain in the field after an attempt"


def test_the_code_is_cleared_from_the_widget_after_success(app, monkeypatch):
    from tools import store_setup_core as core_module

    app.go_to_enrolment()
    screen = app._current
    outcome = core_module.EnrolmentOutcome(device_public_id="dev-1", store_id=3)
    monkeypatch.setattr(core_module, "redeem_enrollment", lambda **k: core_module.EnrolmentUiResult(
        state=core_module.EnrolmentUiState.ENROLLED, detail="ok", outcome=outcome))
    screen.code_var.set("ECHO-A-CODE")
    screen._enroll()
    _drain(app, lambda: screen.status_var.get() != "ENROLLING...")
    assert screen.code_var.get() == ""
    assert str(screen.next_button["state"]) == "normal"


def test_the_heard_checkbox_starts_disabled(app):
    app.go_to_enrolment()
    app.go_to_audio()
    screen = app._current
    assert str(screen.heard_check["state"]) == "disabled"


def test_next_is_disabled_until_the_operator_confirms_hearing_it(app, monkeypatch):
    from tools import store_setup_core as core_module
    from tools.windows_audio_devices import OutputDevice

    app.go_to_enrolment()
    app.go_to_audio()
    screen = app._current
    device = OutputDevice(index=0, name="Realtek(R) Audio", host_api="MME",
                          max_output_channels=2, default_samplerate=48000, is_default=False)
    screen.outputs = [core_module.ClassifiedOutput(device=device, kind=core_module.OutputKind.WIRED)]
    screen.selected.set(device.selector)
    monkeypatch.setattr(core_module, "play_test_tone", lambda *a, **k: core_module.TestSoundResult(
        state=core_module.TestSoundState.PLAYED, detail="ok"))

    screen._test_sound()
    _drain(app, lambda: str(screen.heard_check["state"]) == "normal")
    assert str(screen.next_button["state"]) == "disabled"
    screen.heard_var.set(True)
    screen._on_heard_toggle()
    assert str(screen.next_button["state"]) == "normal"


def test_a_device_error_never_enables_the_heard_checkbox(app, monkeypatch):
    from tools import store_setup_core as core_module
    from tools.windows_audio_devices import OutputDevice

    app.go_to_enrolment()
    app.go_to_audio()
    screen = app._current
    device = OutputDevice(index=0, name="Realtek(R) Audio", host_api="MME",
                          max_output_channels=2, default_samplerate=48000, is_default=False)
    screen.outputs = [core_module.ClassifiedOutput(device=device, kind=core_module.OutputKind.WIRED)]
    screen.selected.set(device.selector)
    monkeypatch.setattr(core_module, "play_test_tone", lambda *a, **k: core_module.TestSoundResult(
        state=core_module.TestSoundState.DEVICE_ERROR, detail="no device"))

    screen._test_sound()
    _drain(app, lambda: screen.status_var.get() != "PLAYING...")
    assert str(screen.heard_check["state"]) == "disabled"


def test_the_rerun_screen_offers_every_required_action(tmp_path):
    """Menu items may not all be wired yet, but every one required by the
    sprint brief must at least exist and be visible - a silently missing
    button is worse than an honestly unwired one."""
    from tools import receiver_agent, store_setup_gui as gui

    protector = FakeCredentialProtector("test-computer")
    credential_path = tmp_path / "cred.bin"
    valid = "speaklink_rcv_v1.11111111-1111-1111-1111-111111111111." + "A" * 43

    class _Transport:
        def post_json(self, url, payload, *, timeout):
            return 201, {"device_public_id": "dev-9", "store_id": 4, "credential": valid}

    receiver_agent.enrol(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=protector, transport=_Transport(),
    )
    application = gui.StoreSetupApp(credential_path=credential_path, protector=protector)
    try:
        texts = _all_button_texts(application._current)
        for required in ("Status", "Repair", "Change Audio Output", "Test Sound",
                         "Restart Receiver", "Stop Receiver", "Redacted Diagnostics",
                         "Export Redacted Diagnostics", "Open Log Folder",
                         "Uninstall Application", "Replace Device Identity"):
            assert any(required in text for text in texts), f"{required} button is missing"
    finally:
        application.destroy()


def _all_button_texts(widget) -> "list[str]":
    found = []
    for child in widget.winfo_children():
        if child.winfo_class() == "TButton":
            found.append(child.cget("text"))
        found.extend(_all_button_texts(child))
    return found


def test_replace_device_identity_requires_a_fresh_code_not_silent_reenrolment(tmp_path):
    """It must route through the connection/enrolment screens again - never
    reuse the existing credential path without a new code being entered."""
    from tools import receiver_agent, store_setup_gui as gui

    protector = FakeCredentialProtector("test-computer")
    credential_path = tmp_path / "cred.bin"
    valid = "speaklink_rcv_v1.11111111-1111-1111-1111-111111111111." + "A" * 43

    class _Transport:
        def post_json(self, url, payload, *, timeout):
            return 201, {"device_public_id": "dev-9", "store_id": 4, "credential": valid}

    receiver_agent.enrol(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=protector, transport=_Transport(),
    )
    application = gui.StoreSetupApp(credential_path=credential_path, protector=protector)
    try:
        application._current._replace_identity()
        from tools.store_setup_gui import ConnectionScreen

        assert isinstance(application._current, ConnectionScreen)
    finally:
        application.destroy()
