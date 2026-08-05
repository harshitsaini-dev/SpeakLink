"""A failed Store Setup step must say so, not freeze the wizard.

THE REPORTED DEFECT

An operator pressed Uninstall Application. The wizard changed to
"UNINSTALLING..." and stayed there indefinitely.

`_run_in_background` already caught the exception and handed it to the
callback rather than re-raising it - deliberately, and its comment says why.
But nine of the eleven callbacks on the rerun screen were written expecting a
result object and read `.detail` off it immediately. An exception has no
`.detail`, so the AttributeError was raised inside a Tk `after` callback, which
goes to Tk's error handler, which writes to a stderr the packaged wizard does
not have (`disable_windowed_traceback=True`). The failure existed, was caught,
and then vanished - leaving the screen on "UNINSTALLING..." with the reason
nowhere at all.

These tests cover the dispatch and the message, not Tk: the defect was in what
happens to an exception between the worker and the label, and that is ordinary
Python. Whether the underlying PowerShell uninstall itself blocks on a Store PC
is a separate question this cannot answer.
"""

from __future__ import annotations

import os
import subprocess
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

pytest.importorskip("tkinter", reason="the Store Setup wizard needs tkinter")

from tools.store_setup_gui import _failure_message  # noqa: E402


# ===========================================================================
# The message an operator actually reads
# ===========================================================================
def test_a_timed_out_step_says_it_was_stopped_and_what_to_do():
    message = _failure_message(
        subprocess.TimeoutExpired(cmd="powershell", timeout=120))
    assert "did not finish in time" in message
    # It must not leave the technician wondering whether something is still
    # grinding away in the background.
    assert "nothing is still running" in message.replace("\n", " ")
    assert "Status" in message


def test_a_permission_failure_names_administrator_rights():
    message = _failure_message(PermissionError(13, "Access is denied"))
    assert "administrator" in message.lower()
    assert "Run as administrator" in message


def test_a_missing_file_points_at_Repair():
    message = _failure_message(FileNotFoundError(2, "No such file"))
    assert "Repair" in message


def test_an_unexpected_failure_still_produces_a_sentence():
    message = _failure_message(RuntimeError("the task refused to stop"))
    assert "the task refused to stop" in message
    assert message.strip()


def test_an_exception_with_no_text_still_produces_a_sentence():
    """`str(SomeError())` is empty, and an empty status box says nothing."""
    class Nameless(Exception):
        pass

    message = _failure_message(Nameless())
    assert "Nameless" in message


def test_no_message_leaks_a_password_or_a_credential():
    """A status line is read aloud over the phone to support."""
    failures = [
        subprocess.TimeoutExpired(cmd="powershell -Password hunter2", timeout=1),
        PermissionError(13, "Access is denied"),
        RuntimeError("settings-password.json verifier mismatch"),
    ]
    for failure in failures:
        message = _failure_message(failure).lower()
        for leak in ("hunter2", "verifier", "echocast_rcv_v1", "bearer"):
            assert leak not in message, (failure, leak)


def test_a_traceback_is_never_shown():
    try:
        raise ValueError("inner detail")
    except ValueError as failure:
        message = _failure_message(failure)
    assert "Traceback" not in message
    assert "  File \"" not in message


# ===========================================================================
# Dispatch: a failure must reach the status line instead of the callback
# ===========================================================================
class FakeScreen:
    """The two things `_run` touches on a real screen."""

    def __init__(self) -> None:
        self.status = None
        self.callback_results = []

        class Var:
            def __init__(self, outer): self._outer = outer
            def set(self, value): self._outer.status = value

        self.status_var = Var(self)

    def after(self, _delay, callback):
        callback()


def make_run(screen):
    """`_run` bound to a fake screen, without importing Tk widgets."""
    from tools.store_setup_gui import RerunScreen
    return lambda work, done: RerunScreen._run(screen, work, done)


def test_a_failing_step_reports_it_instead_of_calling_the_callback():
    screen = FakeScreen()
    run = make_run(screen)

    def work():
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=120)

    def done(result):
        screen.callback_results.append(result)

    run(work, done)

    # The whole defect: `done` used to be called WITH the exception and then
    # read `.detail` off it.
    assert screen.callback_results == []
    assert screen.status is not None
    assert "did not finish in time" in screen.status


def test_a_successful_step_still_reaches_its_callback_unchanged():
    screen = FakeScreen()
    run = make_run(screen)

    class Result:
        ok = True
        detail = "uninstalled; credential, config and logs preserved"

    run(lambda: Result(), lambda result: screen.callback_results.append(result))

    assert len(screen.callback_results) == 1
    assert screen.callback_results[0].detail.startswith("uninstalled")
    assert screen.status is None, "a success must not be overwritten by an error"


def test_every_failure_type_reaches_the_status_line():
    for failure in (subprocess.TimeoutExpired(cmd="x", timeout=1),
                    PermissionError(13, "Access is denied"),
                    FileNotFoundError(2, "missing"),
                    RuntimeError("something else")):
        screen = FakeScreen()
        run = make_run(screen)
        run(lambda f=failure: (_ for _ in ()).throw(f),
            lambda result: screen.callback_results.append(result))
        assert screen.callback_results == [], failure
        assert screen.status, failure


# ===========================================================================
# The uninstall contract itself
# ===========================================================================
def test_uninstall_is_bounded_by_a_timeout():
    """No unbounded wait on an external process."""
    import inspect

    from tools import store_setup_core

    source = inspect.getsource(store_setup_core.uninstall_receiver)
    assert "timeout=" in source, "uninstall must pass a bounded timeout"


def test_uninstall_preserves_identity_and_never_revokes_at_hq():
    """Uninstalling local software is not a Device deletion.

    The two are separate product actions, and conflating them would let a
    technician tidying up a till silently retire the Store's HQ identity.
    """
    import inspect

    from tools import store_setup_core

    # Whitespace-normalised: the sentence wraps across lines in the source.
    source = " ".join(inspect.getsource(store_setup_core.uninstall_receiver).split())
    assert "never revokes the HQ Device" in source
    # Nothing in the uninstall path may call an HQ device-lifecycle endpoint.
    for forbidden in ("receiver-devices", "delete-permanently", "/revoke"):
        assert forbidden not in source, forbidden


def test_uninstall_requires_the_settings_password_authorization():
    import inspect

    from tools import store_setup_core

    source = inspect.getsource(store_setup_core.uninstall_receiver)
    assert "_require_authorization" in source


# ===========================================================================
# Why HQ says "Not supported by this Receiver"
# ===========================================================================
def _snapshot(**overrides):
    from tools.store_setup_core import StatusSnapshot
    base = dict(is_installed=True, device_public_id="d", store_id=31,
                audio_sink=None, audio_output_device=None)
    base.update(overrides)
    return StatusSnapshot(**base)


def test_a_windows_sink_with_a_device_supports_output_control():
    snapshot = _snapshot(audio_sink="windows", audio_output_device="index:3")
    assert snapshot.output_control_supported is True
    assert "index:3" in snapshot.output_control_detail


def test_no_audio_configured_is_reported_as_the_null_sink():
    """The live symptom: HQ says unsupported and the shop hears nothing.

    A Receiver with no audio output still decodes, so HQ sees
    PLAYBACK_CONFIRMED - which is exactly why this needs saying out loud on
    the Store PC rather than being inferred from the HQ console.
    """
    snapshot = _snapshot()
    assert snapshot.output_control_supported is False
    detail = snapshot.output_control_detail
    assert "null sink" in detail
    assert "Not supported by this Receiver" in detail
    assert "Change Audio Output Device" in detail


def test_a_windows_sink_with_no_device_is_still_unsupported():
    snapshot = _snapshot(audio_sink="windows", audio_output_device="")
    assert snapshot.output_control_supported is False
    assert "no output device is selected" in snapshot.output_control_detail


def test_a_non_windows_sink_is_named_in_the_explanation():
    snapshot = _snapshot(audio_sink="null", audio_output_device="index:3")
    assert snapshot.output_control_supported is False
    assert "'null'" in snapshot.output_control_detail


def test_the_explanation_never_contains_a_credential():
    for snapshot in (_snapshot(), _snapshot(audio_sink="windows",
                                            audio_output_device="index:3")):
        detail = snapshot.output_control_detail.lower()
        for leak in ("credential", "token", "password", "bearer"):
            assert leak not in detail


# ===========================================================================
# The uninstall script's own ordering and bounds
# ===========================================================================
def _uninstall_script() -> str:
    return (REPOSITORY_ROOT / "scripts" / "Uninstall-EchoCastStoreReceiver.ps1"
            ).read_text(encoding="utf-8")


def test_the_scheduled_task_is_removed_before_processes_are_stopped():
    """Auto-start off first, or Windows relaunches what was just stopped."""
    source = _uninstall_script()
    assert source.index("Unregister-ScheduledTask") < source.index("Stop-Process")


def test_the_background_receiver_is_stopped_before_the_console_one():
    source = _uninstall_script()
    assert "EchoCastReceiverBackground.exe' ) { 0 }" in source.replace("'", "' ")


def test_the_wait_for_exit_is_bounded_and_not_a_flat_sleep():
    source = _uninstall_script()
    assert "AddSeconds(20)" in source, "the wait must have a deadline"
    assert "still running after 20 seconds" in source
    # The old flat two-second guess must be gone: too long when the process
    # died instantly, too short when Windows took longer.
    assert "Start-Sleep -Seconds 2\n" not in source


def test_a_still_running_receiver_deletes_nothing():
    source = _uninstall_script()
    assert "Nothing has been deleted" in source


def test_file_removal_is_retried_and_then_explained():
    source = _uninstall_script()
    assert "foreach ($attempt in 1..5)" in source
    assert "still in use" in source
    assert "restart this computer" in source


def test_an_absent_task_and_an_absent_install_are_both_accepted():
    """Idempotency: a second uninstall must not fail."""
    source = _uninstall_script()
    assert "no scheduled task named" in source
    assert "nothing installed at the install root" in source
    assert "no Receiver running from the install root" in source


def test_the_credential_survives_unless_explicitly_removed():
    source = _uninstall_script()
    assert "Device credential KEPT - this Store stays enrolled" in source
    assert "The Device is NOT revoked at HQ" in source
