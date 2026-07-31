"""The Audio Output page has to work on a PC with many sound endpoints.

THE REAL DEFECT

The second PC reached "Choose the Store's audio output". It has a lot of Windows
endpoints - several Realtek entries and several Bluetooth headset entries - and
every radio button was packed straight into the screen frame. The list grew past
the bottom of the window, there was no scrollbar, and the Test Sound and Next
buttons were pushed off-screen. The operator could see devices they could not
reach and buttons they could not press.

Not a Receiver fault, not a network fault: a layout that assumed a short list.

These tests build 30+ fake endpoints, which no developer machine has, and assert
the things a person actually needs: a scrollbar exists, the LAST device can be
selected, and the footer stays visible while the list scrolls.
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402
from tools.windows_audio_devices import OutputDevice  # noqa: E402

try:
    import tkinter as tk

    tk.Tk().destroy()
    TK_AVAILABLE = True
except Exception:
    TK_AVAILABLE = False

pytestmark = pytest.mark.skipif(not TK_AVAILABLE, reason="no Tk display available")


def device(index, name, host_api="MME"):
    return OutputDevice(index=index, name=name, host_api=host_api,
                        max_output_channels=2, default_samplerate=48000.0,
                        is_default=False)


def many_outputs(count=30):
    """A machine like the second PC: duplicates and Bluetooth headsets."""
    built = []
    for n in range(count):
        if n % 3 == 0:
            built.append(device(n, "Speakers (Realtek(R) Audio)"))
        elif n % 3 == 1:
            built.append(device(n, "Headset (Bluetooth Hands-Free)"))
        else:
            built.append(device(n, f"USB Audio Device {n}"))
    return [core.ClassifiedOutput(device=d, kind=core.classify_output(d)) for d in built]


@pytest.fixture()
def audio(tmp_path, monkeypatch):
    """The Audio screen, with a long device list."""
    from tools import store_setup_gui as gui

    monkeypatch.setattr(core, "list_classified_outputs", lambda **k: many_outputs(30))
    app = gui.StoreSetupApp(credential_path=tmp_path / "cred.bin",
                            protector=FakeCredentialProtector("test-computer"))
    app.geometry("560x420")
    app.go_to_audio()
    app.update()
    yield app, app._current
    app.destroy()


def widget_tree(widget):
    found = [widget]
    for child in widget.winfo_children():
        found.extend(widget_tree(child))
    return found


def scrollbars(screen):
    from tkinter import ttk

    return [w for w in widget_tree(screen) if isinstance(w, (ttk.Scrollbar, tk.Scrollbar))]


def radiobuttons(screen):
    from tkinter import ttk

    return [w for w in widget_tree(screen) if isinstance(w, ttk.Radiobutton)]


# ===========================================================================
# 1. The scrollbar exists and the whole list is reachable
# ===========================================================================
def test_a_long_device_list_produces_a_vertical_scrollbar(audio):
    _, screen = audio
    bars = scrollbars(screen)
    assert bars, "30 devices and no scrollbar - this is the defect"
    assert any(str(bar.cget("orient")) == "vertical" for bar in bars)


def test_every_device_has_a_row(audio):
    _, screen = audio
    assert len(radiobuttons(screen)) == 30


def test_the_last_device_can_be_selected(audio):
    """The one that mattered: the operator could see rows they could not reach."""
    app, screen = audio
    last = screen.outputs[-1]

    screen.selected.set(last.device.selector)
    app.update()

    assert screen.selected.get() == last.device.selector
    assert screen._selected_device() is not None


def test_the_footer_controls_are_outside_the_scrolling_area(audio):
    """Test Sound and Next must not be pushed off-screen by a long list."""
    _, screen = audio
    canvas = screen.list_canvas
    for control in (screen.next_button, screen.heard_check, screen.test_button):
        parent = control
        inside = False
        while parent is not None and parent is not screen:
            if parent is canvas or parent is screen.list_frame:
                inside = True
                break
            parent = parent.master
        assert not inside, f"{control} scrolls with the list and can be pushed away"


def test_the_footer_stays_visible_after_scrolling_to_the_bottom(audio):
    app, screen = audio
    screen.list_canvas.yview_moveto(1.0)
    app.update()

    assert screen.next_button.winfo_ismapped()
    assert screen.test_button.winfo_ismapped()
    assert screen.heard_check.winfo_ismapped()


# ===========================================================================
# 2. Scrolling actually moves
# ===========================================================================
def test_the_canvas_scrolls_with_the_mouse_wheel(audio):
    app, screen = audio
    before = screen.list_canvas.yview()[0]

    screen._on_mousewheel(type("Event", (), {"delta": -360, "num": 5})())
    app.update()

    assert screen.list_canvas.yview()[0] > before, "the wheel did not move the list"


def test_page_down_reaches_later_entries(audio):
    app, screen = audio
    before = screen.list_canvas.yview()[0]

    screen._page(1)
    app.update()

    assert screen.list_canvas.yview()[0] > before


def test_the_wheel_binding_is_not_global(audio):
    """Binding the wheel to the whole application would steal scrolling from
    every other screen."""
    app, screen = audio
    assert not app.bind_all("<MouseWheel>"), (
        "the mouse wheel is bound application-wide"
    )


# ===========================================================================
# 3. Refresh
# ===========================================================================
def test_refresh_does_not_duplicate_rows(audio):
    app, screen = audio
    before = len(radiobuttons(screen))

    screen._refresh()
    app.update()

    assert len(radiobuttons(screen)) == before, "Refresh left the old rows behind"


def test_refresh_keeps_an_existing_selection_when_the_device_is_still_there(audio):
    app, screen = audio
    wanted = screen.outputs[5].device.selector
    screen.selected.set(wanted)

    screen._refresh()
    app.update()

    assert screen.selected.get() == wanted


def test_refresh_clears_a_selection_that_has_disappeared(audio, monkeypatch):
    app, screen = audio
    screen.selected.set(screen.outputs[-1].device.selector)
    monkeypatch.setattr(core, "list_classified_outputs", lambda **k: many_outputs(3))

    screen._refresh()
    app.update()

    assert screen.selected.get() == "", "a selector that no longer exists stayed selected"


def test_an_empty_device_list_says_so(audio, monkeypatch):
    app, screen = audio
    monkeypatch.setattr(core, "list_classified_outputs", lambda **k: [])

    screen._refresh()
    app.update()

    assert radiobuttons(screen) == []
    assert "no playback" in screen.status_var.get().lower() or \
           "no audio" in screen.status_var.get().lower()


# ===========================================================================
# 4. Readable, distinguishable rows
# ===========================================================================
def test_duplicate_friendly_names_are_separately_selectable(audio):
    _, screen = audio
    realtek = [c for c in screen.outputs if "Realtek" in c.device.name]
    assert len(realtek) > 1

    selectors = {c.device.selector for c in realtek}
    assert len(selectors) == len(realtek), "duplicate names collapsed into one selector"


def test_duplicate_names_are_disambiguated_in_the_label(audio):
    _, screen = audio
    labels = [str(w.cget("text")) for w in radiobuttons(screen) if "Realtek" in str(w.cget("text"))]
    assert len(labels) == len(set(labels)), f"two rows read identically: {labels}"


def test_a_raw_driver_path_is_never_the_primary_label(tmp_path, monkeypatch):
    from tools import store_setup_gui as gui

    ugly = device(1, r"@System32\drivers\bthhfenum.sys,-10102;%1 Hands-Free")
    monkeypatch.setattr(core, "list_classified_outputs",
                        lambda **k: [core.ClassifiedOutput(device=ugly,
                                                           kind=core.classify_output(ugly))])
    app = gui.StoreSetupApp(credential_path=tmp_path / "cred.bin",
                            protector=FakeCredentialProtector("test-computer"))
    try:
        app.go_to_audio()
        app.update()
        label = str(radiobuttons(app._current)[0].cget("text"))
        assert not label.startswith("[WIRED] @System32"), label
        assert "drivers" not in label or "Hands-Free" in label
    finally:
        app.destroy()


def test_wired_and_usb_sort_before_bluetooth_handsfree(audio):
    _, screen = audio
    kinds = [c.kind.value for c in screen.outputs]
    first_bluetooth = next((i for i, k in enumerate(kinds) if k == "BLUETOOTH"), len(kinds))
    last_wired = max((i for i, k in enumerate(kinds) if k == "WIRED"), default=-1)
    assert last_wired < first_bluetooth, f"ordering puts Bluetooth first: {kinds[:6]}"


def test_the_recommendation_is_shown_without_disparaging_bluetooth(audio):
    _, screen = audio
    text = "\n".join(str(w.cget("text")) for w in widget_tree(screen)
                     if hasattr(w, "cget") and _safe_text(w))
    assert "recommended" in text.lower()
    assert "disconnected" not in text.lower()


def _safe_text(widget):
    try:
        return widget.cget("text")
    except Exception:
        return ""


# ===========================================================================
# 5. Selection safety
# ===========================================================================
def test_nothing_is_selected_by_default(audio):
    _, screen = audio
    assert screen.selected.get() == ""


def test_next_starts_disabled(audio):
    _, screen = audio
    assert str(screen.next_button["state"]) == "disabled"


def test_changing_the_device_clears_the_heard_confirmation(audio):
    app, screen = audio
    screen.selected.set(screen.outputs[0].device.selector)
    screen.heard_check.config(state="normal")
    screen.heard_var.set(True)
    app.update()

    screen.selected.set(screen.outputs[1].device.selector)
    screen._on_selection_changed()
    app.update()

    assert screen.heard_var.get() is False, (
        "the operator's confirmation survived a change of speaker"
    )
    assert str(screen.next_button["state"]) == "disabled"


def test_test_sound_never_marks_speaker_verified(audio):
    _, screen = audio
    text = "\n".join(str(_safe_text(w)) for w in widget_tree(screen))
    assert "SPEAKER_VERIFIED" not in text


# ===========================================================================
# 6. It fits a normal Store screen
# ===========================================================================
def test_the_screen_fits_a_1366_by_768_window(audio):
    app, screen = audio
    app.geometry("1000x600")
    app.update()

    assert screen.list_canvas.winfo_reqheight() <= 600, (
        "the device list alone is taller than the window"
    )


def test_the_window_is_resizable(audio):
    app, _ = audio
    # Tk answers with ints, so compare truthiness rather than identity.
    width, height = app.resizable()
    assert bool(width) and bool(height), 'the window cannot be resized'
    assert app.minsize()[0] <= 620


# ===========================================================================
# 7. Thread discipline
# ===========================================================================
def test_no_tk_variable_is_touched_from_a_worker_thread():
    import ast

    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "work":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    attr = getattr(inner.func, "attr", None)
                    if attr in {"set", "config", "configure"}:
                        target = getattr(inner.func, "value", None)
                        name = getattr(target, "attr", "") or getattr(target, "id", "")
                        if "var" in str(name).lower() or "button" in str(name).lower():
                            offenders.append(f"line {inner.lineno}")
    assert offenders == []
