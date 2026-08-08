"""A brand-new Store PC must be asked for a Settings Password before enrolling.

THE DEFECT

Enrolment needs a Settings Password, and ``EnrolmentScreen._enroll`` said so in
a comment: "on a fresh Store the wizard has already established a Settings
Password". It had not. ``establish_settings_password`` existed and was called
by nothing, so the fresh-Store path went Welcome -> Connection -> Enroll with
no password step anywhere in it.

The operator filled in the enrollment code and the device name, pressed Enroll,
and met "Settings Password required ... use the Set Settings Password action
first" - an action that first run never offers. Enrolment could not complete on
a new computer at all.

These tests hold the first-run path to asking first, and hold the existing
paths to being unchanged.
"""

from __future__ import annotations

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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402

try:
    import tkinter as tk

    tk.Tk().destroy()
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TK_AVAILABLE, reason="no Tk display available")

PASSWORD = "a-long-enough-settings-password"


@pytest.fixture()
def app(tmp_path):
    from tools import store_setup_gui as gui

    application = gui.StoreSetupApp(
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
    )
    yield application
    application.destroy()


def enrolment_screen(app):
    from tools import store_setup_gui as gui

    screen = gui.EnrolmentScreen(app._container, app)
    app._show(screen)
    app.update()
    return screen


def set_password(app, password=PASSWORD):
    from tools import store_kit_settings_password as settings_password

    settings_password.establish_password(
        app.settings_password_path, password, password)


# ===========================================================================
# First run
# ===========================================================================

def test_a_new_computer_is_asked_for_a_password_before_it_can_enroll(app):
    """The reported bug. Fails before the fix: the form was fully usable."""
    from tools import store_kit_settings_password as settings_password

    assert not settings_password.is_configured(app.settings_password_path), (
        "this fixture is not modelling a brand-new computer")

    screen = enrolment_screen(app)

    assert str(screen.enroll_button["state"]) == "disabled", (
        "a new computer could press Enroll with no Settings Password, which "
        "is the dead end this exists to prevent")
    assert str(screen.code_entry["state"]) == "disabled"
    assert str(screen.device_entry["state"]) == "disabled"
    assert screen._password_button is not None, (
        "no way to set the password was offered on the screen that needs it")
    assert "Settings Password" in screen.status_var.get()


def test_pressing_the_button_really_runs_and_sets_the_password(app, monkeypatch):
    """The button's OWN handler, invoked the way the operator invokes it.

    This is the test that was missing. The first version of this file set the
    password directly and then checked the gate, so it never executed the
    handler - which called a function that did not exist. The button raised
    NameError into a frozen GUI with no console, so it looked like a button
    that did nothing at all, and the tests were green throughout.
    """
    from tools import store_kit_settings_password as settings_password
    from tools import store_setup_gui as gui

    # The handler imports simpledialog inside itself, so the patch has to
    # land on tkinter's own module rather than on the GUI module.
    import tkinter.simpledialog as simpledialog

    typed = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(simpledialog, "askstring", lambda *a, **k: next(typed))
    monkeypatch.setattr(gui.messagebox, "showinfo", lambda *a, **k: None)

    screen = enrolment_screen(app)
    assert str(screen.enroll_button["state"]) == "disabled"

    # Exactly what Tk calls when the operator clicks it.
    screen._password_button.invoke()
    app.update()

    assert settings_password.is_configured(app.settings_password_path), (
        "pressing the button did not set a password")
    assert str(screen.enroll_button["state"]) == "normal"
    assert str(screen.code_entry["state"]) == "normal"
    assert str(screen.device_entry["state"]) == "normal"


def test_every_command_on_the_screen_is_actually_callable(app):
    """No button may be wired to a name that does not exist.

    A missing name inside a Tk callback surfaces as a silent dead button in a
    windowless build, so the wiring is checked rather than trusted.
    """
    screen = enrolment_screen(app)
    for widget in (screen._password_button, screen.enroll_button,
                   screen.next_button):
        if widget is None:
            continue
        command = str(widget.cget("command"))
        assert command, f"{widget} has no command bound"

    # And every global name the handler reaches for really resolves - the
    # exact failure that shipped, where it called a function nobody had
    # defined and the click died silently.
    import builtins

    handler = screen._set_settings_password
    for name in handler.__code__.co_names:
        if hasattr(builtins, name) or hasattr(screen, name):
            continue
        assert name in handler.__globals__, (
            f"_set_settings_password refers to '{name}', which exists nowhere "
            "- in a windowless build that is a button that does nothing")


def test_setting_the_password_opens_the_enrolment_form(app):
    screen = enrolment_screen(app)

    # Stand in for the operator typing it twice, using the real establishment
    # path - the same function every other screen uses.
    set_password(app)
    screen._apply_password_gate()
    app.update()

    assert str(screen.enroll_button["state"]) == "normal"
    assert str(screen.code_entry["state"]) == "normal"
    assert str(screen.device_entry["state"]) == "normal"


def test_the_password_is_stored_hashed_and_never_in_plain_text(app):
    """Reuses the existing mechanism, so this is a guard rather than a claim."""
    from tools import store_kit_settings_password as settings_password

    set_password(app)
    raw = Path(app.settings_password_path).read_text(encoding="utf-8")

    assert PASSWORD not in raw, "the Settings Password was stored in plain text"
    assert settings_password.verify_password(app.settings_password_path,
                                             PASSWORD) is True
    assert settings_password.verify_password(app.settings_password_path,
                                             "not-it") is False


# ===========================================================================
# Every later run
# ===========================================================================

def test_a_computer_that_already_has_a_password_is_not_asked_again(app):
    set_password(app)
    screen = enrolment_screen(app)

    assert screen._password_button is None, (
        "a Store with a Settings Password was asked to create another")
    assert str(screen.enroll_button["state"]) == "normal"
    assert str(screen.code_entry["state"]) == "normal"


def test_restarting_the_wizard_does_not_ask_a_second_time(app, tmp_path):
    """The same computer, the wizard opened again."""
    from tools import store_setup_gui as gui

    set_password(app)
    again = gui.StoreSetupApp(
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
    )
    try:
        screen = gui.EnrolmentScreen(again._container, again)
        again._show(screen)
        again.update()
        assert screen._password_button is None
        assert str(screen.enroll_button["state"]) == "normal"
    finally:
        again.destroy()


# ===========================================================================
# The existing rules still apply
# ===========================================================================

def test_the_existing_validation_rules_are_the_ones_that_apply(app):
    """No second password system: weak and mismatched are refused as before."""
    from tools import store_kit_settings_password as settings_password

    with pytest.raises(settings_password.SettingsPasswordError):
        settings_password.establish_password(app.settings_password_path,
                                             "short", "short")
    with pytest.raises(settings_password.SettingsPasswordError):
        settings_password.establish_password(app.settings_password_path,
                                             PASSWORD, PASSWORD + "-different")
    with pytest.raises(settings_password.SettingsPasswordError):
        settings_password.establish_password(app.settings_password_path, "", "")

    assert not settings_password.is_configured(app.settings_password_path), (
        "a refused attempt left a password behind")


def test_setting_a_password_never_becomes_a_reset(app):
    """Establish refuses over an existing verifier, so this cannot clear one."""
    from tools import store_kit_settings_password as settings_password

    set_password(app)
    with pytest.raises(settings_password.SettingsPasswordError):
        settings_password.establish_password(
            app.settings_password_path, "another-long-password",
            "another-long-password")
    assert settings_password.verify_password(app.settings_password_path,
                                             PASSWORD) is True


def test_the_gate_does_not_replace_the_backend_refusal(app):
    """The screen is a courtesy; core is the boundary and still refuses."""
    from tools import store_setup_core as core

    with pytest.raises(Exception) as refusal:
        core.redeem_enrollment(
            authorization=None,
            backend_url="http://127.0.0.1:1",
            code="X", device_name="X", hostname="X",
            credential_path=app.credential_path, protector=app.protector)
    assert "authoriz" in str(refusal.value).lower() or "password" in str(
        refusal.value).lower(), (
        f"enrolment without authorization was not refused by core: {refusal.value}")
