"""The six operator journeys, driven through the real Tkinter surface.

WHY THESE ARE SEPARATE FROM THE CORE TESTS

The core tests prove the gate refuses. They cannot prove an operator can
actually get past it, or that the refusal reaches a screen. A gate nobody can
open is a locked door with no keyhole - which is exactly the state this
feature was in one checkpoint ago, when the dialogs existed and no button
called them.

So these drive the GUI-facing functions: the real screen handlers, the real
module-level password helpers, the real widgets. The dialog PROMPTS are
stubbed, because a modal askstring cannot be answered headlessly - but every
decision after the prompt is the shipped code.

Nothing here touches the real Store profile. Every journey builds its own
temporary credential path, and the verifier lives beside it.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools import store_kit_settings_password as settings_password  # noqa: E402
from tools import store_setup_core as core  # noqa: E402

tk = pytest.importorskip("tkinter")

PASSWORD = "a-store-settings-password"
NEW_PASSWORD = "a-replacement-store-password"


class FakeProtector:
    """The credential protector, without DPAPI."""

    def __init__(self, computer="test-computer"):
        self.computer = computer

    def protect(self, payload: bytes) -> bytes:
        return b"sealed:" + payload

    def unprotect(self, payload: bytes) -> bytes:
        return payload.replace(b"sealed:", b"", 1)


@pytest.fixture()
def app(tmp_path):
    """A real StoreSetupApp against a temporary profile."""
    from tools import store_setup_gui as gui

    try:
        window = gui.StoreSetupApp(
            credential_path=tmp_path / "receiver-credential.bin",
            protector=FakeProtector(),
            state_root=tmp_path,
        )
    except tk.TclError:  # pragma: no cover - headless CI without a display
        pytest.skip("no Tk display available")
    try:
        yield window
    finally:
        window.destroy()


@pytest.fixture()
def answers(monkeypatch):
    """Queue answers for the modal password prompts.

    A modal askstring cannot be answered headlessly, so the PROMPT is stubbed
    and everything after it is the shipped code - including which prompt is
    asked, in what order, and what the handler does with the answer.
    """
    from tkinter import simpledialog

    queued: list = []
    asked: list = []

    def fake_askstring(title, prompt, **kwargs):
        asked.append((title, prompt))
        return queued.pop(0) if queued else None

    monkeypatch.setattr(simpledialog, "askstring", fake_askstring)
    monkeypatch.setattr("tkinter.messagebox.showinfo", lambda *a, **k: None)
    monkeypatch.setattr("tkinter.messagebox.showerror", lambda *a, **k: None)
    return {"queue": queued, "asked": asked}


def _verifier(app) -> Path:
    return Path(app.settings_password_path)


# ===========================================================================
# JOURNEY A - a fresh Store sets a password before anything can change
# ===========================================================================
def test_journey_a_fresh_store_sets_a_password(app, answers):
    from tools import store_setup_gui as gui

    assert not settings_password.is_configured(_verifier(app))

    answers["queue"].extend([PASSWORD, PASSWORD])
    assert gui.set_settings_password(app, app) is True

    assert settings_password.is_configured(_verifier(app))
    assert settings_password.verify_password(_verifier(app), PASSWORD) is True
    # Two prompts: the password and its confirmation.
    assert len(answers["asked"]) == 2


def test_journey_a_a_mismatch_establishes_nothing(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, "a-different-answer"])
    assert gui.set_settings_password(app, app) is False
    assert not settings_password.is_configured(_verifier(app))


def test_journey_a_a_short_password_establishes_nothing(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend(["short", "short"])
    assert gui.set_settings_password(app, app) is False
    assert not settings_password.is_configured(_verifier(app))


def test_journey_a_enrolment_is_refused_until_a_password_exists(app, answers):
    """The order that matters: no password, no identity change."""
    from tools import store_setup_gui as gui

    assert gui._ask_settings_authorization(app, app, "Enrolling") is None
    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.redeem_enrollment(
            backend_url="https://hq.example", code="ABC", device_name="till",
            hostname="till", credential_path=app.credential_path,
            protector=app.protector)


# ===========================================================================
# JOURNEY B - an existing Store upgrades
# ===========================================================================
def test_journey_b_an_existing_store_without_a_verifier_still_reads_status(app):
    """No password on disk, and observation still works."""
    assert not settings_password.is_configured(_verifier(app))
    described = settings_password.read_verifier(_verifier(app))
    assert described["configured"] is False
    # The read-only helper takes no authorization at all.
    import inspect

    assert "authorization" not in inspect.signature(
        core.get_status_snapshot).parameters


def test_journey_b_a_protected_change_directs_the_operator_to_set_one(app, answers,
                                                                      monkeypatch):
    from tools import store_setup_gui as gui

    shown: list = []
    monkeypatch.setattr("tkinter.messagebox.showinfo",
                        lambda title, message, **k: shown.append(message))

    assert gui._ask_settings_authorization(app, app, "Changing the output") is None
    assert shown, "the operator was told nothing"
    assert "Settings Password" in shown[0]
    # No password prompt was even offered - there is nothing to verify against.
    assert answers["asked"] == []


def test_journey_b_after_establishing_the_protected_flow_proceeds(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, PASSWORD])
    assert gui.set_settings_password(app, app) is True

    answers["queue"].append(PASSWORD)
    authorization = gui._ask_settings_authorization(app, app, "Changing the output")
    assert isinstance(authorization, core.SettingsAuthorization)


def test_journey_b_establishing_touches_no_config_or_credential(app, answers, tmp_path):
    from tools import store_setup_gui as gui

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"backend_url": "https://old.example"}),
                      encoding="utf-8")
    credential = Path(app.credential_path)
    credential.write_bytes(b"sealed-credential")
    config_before, credential_before = config.read_bytes(), credential.read_bytes()

    answers["queue"].extend([PASSWORD, PASSWORD])
    assert gui.set_settings_password(app, app) is True

    assert config.read_bytes() == config_before
    assert credential.read_bytes() == credential_before


# ===========================================================================
# JOURNEY C - a wrong password changes nothing
# ===========================================================================
def test_journey_c_a_wrong_password_is_refused_and_changes_nothing(app, answers,
                                                                   tmp_path,
                                                                   monkeypatch):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, PASSWORD])
    gui.set_settings_password(app, app)
    verifier_before = _verifier(app).read_bytes()

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"audio_output_device": "Speakers"}),
                      encoding="utf-8")
    config_before = config.read_bytes()

    errors: list = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    answers["queue"].append("the-wrong-password")
    assert gui._ask_settings_authorization(app, app, "Changing the output") is None

    assert errors, "the operator saw no refusal"
    assert "the-wrong-password" not in errors[0]
    assert PASSWORD not in errors[0]
    assert config.read_bytes() == config_before
    assert _verifier(app).read_bytes() == verifier_before
    # And the real password still works.
    assert settings_password.verify_password(_verifier(app), PASSWORD) is True


# ===========================================================================
# JOURNEY D - a corrupt verifier
# ===========================================================================
def test_journey_d_a_corrupt_verifier_blocks_changes_and_offers_no_reset(app,
                                                                        answers,
                                                                        monkeypatch):
    from tools import store_setup_gui as gui

    _verifier(app).parent.mkdir(parents=True, exist_ok=True)
    _verifier(app).write_text("{broken", encoding="utf-8")
    before = _verifier(app).read_bytes()

    errors: list = []
    monkeypatch.setattr("tkinter.messagebox.showerror",
                        lambda title, message, **k: errors.append(message))

    assert gui._ask_settings_authorization(app, app, "Changing the output") is None
    assert errors and "cannot be read" in errors[0]
    # No prompt was offered, so there is nothing to type past.
    assert answers["asked"] == []
    # The file is neither repaired nor replaced.
    assert _verifier(app).read_bytes() == before


def test_journey_d_establishing_over_a_corrupt_verifier_is_refused(app, answers):
    """Otherwise corrupting the file would be the way past the password."""
    from tools import store_setup_gui as gui

    _verifier(app).parent.mkdir(parents=True, exist_ok=True)
    _verifier(app).write_text("{broken", encoding="utf-8")

    answers["queue"].extend([NEW_PASSWORD, NEW_PASSWORD])
    assert gui.set_settings_password(app, app) is False
    assert _verifier(app).read_text(encoding="utf-8") == "{broken"


def test_journey_d_the_gui_presents_no_reset_or_forgot_action():
    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(
        encoding="utf-8").lower()
    for forbidden in ("forgot password", "reset password", "master password",
                      "recovery code", "security question"):
        assert forbidden not in source


# ===========================================================================
# JOURNEY E - changing the password
# ===========================================================================
def test_journey_e_change_swaps_which_password_works(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, PASSWORD])
    gui.set_settings_password(app, app)

    answers["queue"].extend([PASSWORD, NEW_PASSWORD, NEW_PASSWORD])
    assert gui.change_settings_password(app, app) is True

    assert settings_password.verify_password(_verifier(app), NEW_PASSWORD) is True
    assert settings_password.verify_password(_verifier(app), PASSWORD) is False


def test_journey_e_a_wrong_current_password_changes_nothing(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, PASSWORD])
    gui.set_settings_password(app, app)
    before = _verifier(app).read_bytes()

    answers["queue"].extend(["not-the-current-one", NEW_PASSWORD, NEW_PASSWORD])
    assert gui.change_settings_password(app, app) is False

    assert _verifier(app).read_bytes() == before
    assert settings_password.verify_password(_verifier(app), PASSWORD) is True


def test_journey_e_change_touches_no_credential_or_config(app, answers, tmp_path):
    from tools import store_setup_gui as gui

    credential = Path(app.credential_path)
    credential.write_bytes(b"sealed-credential")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    credential_before, config_before = credential.read_bytes(), config.read_bytes()

    answers["queue"].extend([PASSWORD, PASSWORD])
    gui.set_settings_password(app, app)
    answers["queue"].extend([PASSWORD, NEW_PASSWORD, NEW_PASSWORD])
    assert gui.change_settings_password(app, app) is True

    assert credential.read_bytes() == credential_before
    assert config.read_bytes() == config_before


# ===========================================================================
# JOURNEY F - re-enrolling an existing Store
# ===========================================================================
def test_journey_f_re_enrolment_without_authorization_preserves_the_identity(app):
    credential = Path(app.credential_path)
    credential.write_bytes(b"sealed-existing-identity")
    before = credential.read_bytes()

    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.redeem_enrollment(
            backend_url="https://evil.example", code="STOLEN",
            device_name="till", hostname="till",
            credential_path=credential, protector=app.protector)

    assert credential.read_bytes() == before


def test_journey_f_re_enrolment_asks_for_the_password_once_configured(app, answers):
    from tools import store_setup_gui as gui

    answers["queue"].extend([PASSWORD, PASSWORD])
    gui.set_settings_password(app, app)

    answers["queue"].append(PASSWORD)
    authorization = gui._ask_settings_authorization(
        app, app, "Enrolling this computer")
    assert isinstance(authorization, core.SettingsAuthorization)


# ===========================================================================
# The button that makes all of this reachable
# ===========================================================================
def test_the_settings_password_action_is_offered_on_the_maintenance_screen():
    """A gate nobody can open is a locked door with no keyhole."""
    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(
        encoding="utf-8")
    assert "SETTINGS_PASSWORD_BUTTON_LABEL" in source
    assert "self._settings_password" in source
    assert "(SETTINGS_PASSWORD_BUTTON_LABEL, self._settings_password)" in source
