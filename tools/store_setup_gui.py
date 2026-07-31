"""EchoCastStoreSetup.exe: the window. Every decision it shows lives in
``store_setup_core`` - this module only asks for one and paints the result.

Long operations (connection test, enrolment, install) run on a background
thread so the Tk main loop never blocks and the window never looks hung.
"""

from __future__ import annotations

import socket
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from tools import resource_paths  # noqa: E402
from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import (  # noqa: E402
    DeviceCredentialProtector,
    default_credential_path,
)
from tools.store_enrolment_state import (  # noqa: E402
    STALE_LOCAL_FILES,
    EnrolmentVerdict,
    assess,
    replace_local_enrolment,
)
from tkinter import messagebox  # noqa: E402


def core_stale_files():
    """The two files a replacement removes. Named through one export so the GUI
    and the tests cannot disagree about what "only the local identity" means."""
    return STALE_LOCAL_FILES


def confirm_removal(parent) -> bool:
    """A normal Yes/No dialog - no confirmation word.

    Making a beginner type REMOVE trains people to type it without reading, and
    this project has already shipped a "confirmation" that fired its own default
    button in a headless run. This is a genuine two-button question, and it is
    indirected through a module-level function so a test can answer it without a
    real dialog.
    """
    return bool(messagebox.askyesno(
        "Remove old enrolment",
        "Remove this old Store-PC enrolment and continue?",
        parent=parent, default="no"))


def stop_receiver_task() -> None:
    """Stop only the EchoCast Store Receiver, never anything else on the PC."""
    try:
        core.stop_receiver()
    except Exception:
        # A Receiver that is already stopped, or a task that was never
        # installed, is not a reason to refuse to clean up a stale identity.
        pass

DEFAULT_HQ_URL = "http://192.168.4.134:8000"
WINDOW_TITLE = "EchoCast Store Setup"


class StoreSetupApp(tk.Tk):
    """The whole wizard: one window, one frame swapped in at a time.

    ``protector`` and ``credential_path`` are constructor arguments so tests
    can build this window against a temporary, fake-protected state directory
    rather than the real DPAPI store on this machine.
    """

    def __init__(self, *, credential_path=None, protector=None,
                 assessment=None, state_root=None):
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("560x420")
        self.resizable(False, False)

        self.credential_path = Path(credential_path) if credential_path else default_credential_path()
        self.protector = protector or DeviceCredentialProtector()

        self.state_data: dict = {
            "backend_url": DEFAULT_HQ_URL,
            "expected_hq_host": None,
            "allow_insecure_private_lan": False,
            "device_name": socket.gethostname(),
            "output_device": None,
            "enrolment_outcome": None,
        }

        self._container = ttk.Frame(self)
        self._container.pack(fill="both", expand=True)
        self._current: "ttk.Frame | None" = None

        self.state_root = Path(state_root) if state_root else self.credential_path.parent

        # A package that cannot install is reported ONCE, in words, before the
        # operator invests any time in it - rather than failing halfway through
        # an installation with a path nobody can act on.
        self.package_missing = resource_paths.missing_resources()

        existing = core.detect_existing_installation(
            credential_path=self.credential_path, protector=self.protector)
        self.existing = existing

        # THE ROUTING THAT WAS MISSING. A sealed credential proves only that this
        # computer once enrolled SOMEWHERE. Which screen opens depends on what the
        # CURRENT HQ says about it, not on the file existing.
        self.assessment = assessment
        if assessment is None and existing.is_installed:
            assessment = self._assess_existing(existing)
            self.assessment = assessment

        if assessment is not None and assessment.verdict in (
                EnrolmentVerdict.OLD_ENROLMENT_DETECTED, EnrolmentVerdict.ARCHIVED_STORE):
            self._show(OldEnrolmentScreen(self._container, self, assessment))
        elif existing.is_installed or (assessment is not None and assessment.local_enrolled):
            self._show(RerunScreen(self._container, self, existing,
                                   assessment=assessment))
        else:
            self._show(WelcomeScreen(self._container, self))

    def _assess_existing(self, existing):
        """Ask the current HQ about this credential, without blocking start-up.

        A failure here yields HQ_UNREACHABLE, never OLD_ENROLMENT_DETECTED: a
        credential cannot be judged stale by a server that never answered, and
        offering to delete an identity because the network is down is the one
        outcome worse than showing nothing.
        """
        status = {"enrolled": True,
                  "device_public_id": existing.device_public_id,
                  "store_id": existing.store_id}
        try:
            return assess(local_status=status,
                          hq_address=self.state_data.get("backend_url", DEFAULT_HQ_URL),
                          device_lookup=core.lookup_device_at_hq)
        except Exception:
            return assess(local_status=status,
                          hq_address=self.state_data.get("backend_url", DEFAULT_HQ_URL),
                          service_check=lambda _a: False)

    def go_to_welcome(self) -> None:
        self.existing = core.detect_existing_installation(
            credential_path=self.credential_path, protector=self.protector)
        self.assessment = None
        self._show(WelcomeScreen(self._container, self))

    def go_to_result(self, checks: dict) -> None:
        self._show(ResultScreen(self._container, self, checks=checks))

    def _show(self, frame: ttk.Frame) -> None:
        if self._current is not None:
            self._current.destroy()
        self._current = frame
        frame.pack(fill="both", expand=True)

    def go_to_enrolment(self) -> None:
        self._show(EnrolmentScreen(self._container, self))

    def go_to_audio(self) -> None:
        self._show(AudioScreen(self._container, self))

    def go_to_install(self) -> None:
        self._show(InstallScreen(self._container, self))

    def go_to_connection(self) -> None:
        self._show(ConnectionScreen(self._container, self))


def _run_in_background(target, on_done) -> None:
    """Run ``target`` off the Tk thread; marshal the result back via ``after``.

    An exception in ``target`` is caught and re-raised on the Tk thread rather
    than left to crash a background thread silently - a wizard that hangs on
    "INSTALLING..." forever with no error anywhere is worse than one that
    shows a traceback.
    """
    outcome: dict = {}

    def worker():
        try:
            outcome["result"] = target()
        except Exception as failure:  # noqa: BLE001 - re-raised below, not swallowed
            outcome["error"] = failure

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    def poll(root: tk.Misc):
        if thread.is_alive():
            root.after(100, lambda: poll(root))
        elif "error" in outcome:
            # Handed to the callback, NOT re-raised. Raising inside an `after`
            # callback goes to Tk's error handler, which prints to stderr - and
            # the packaged wizard is built with disable_windowed_traceback=True
            # and has no stderr, so the operator would see the screen freeze on
            # "INSTALLING..." with the reason nowhere at all.
            on_done(outcome["error"])
        else:
            on_done(outcome["result"])
    return poll


class ConnectionScreen(ttk.Frame):
    """Screen 1 - HQ Connection."""

    def __init__(self, parent, app: StoreSetupApp):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Connect to HQ", font=("Segoe UI", 14, "bold")).pack(pady=12)
        ttk.Label(self, text="HQ URL").pack(anchor="w", padx=24)
        self.url_var = tk.StringVar(master=self, value=app.state_data["backend_url"])
        ttk.Entry(self, textvariable=self.url_var, width=48).pack(padx=24, pady=4)

        self.advanced_visible = tk.BooleanVar(master=self, value=False)
        ttk.Checkbutton(self, text="Advanced settings", variable=self.advanced_visible,
                        command=self._toggle_advanced).pack(anchor="w", padx=24)

        self.advanced_frame = ttk.Frame(self)
        ttk.Label(self.advanced_frame, text="Expected HQ host (private LAN only)").pack(anchor="w")
        self.host_var = tk.StringVar(master=self)
        ttk.Entry(self.advanced_frame, textvariable=self.host_var, width=32).pack(anchor="w")
        self.allow_lan_var = tk.BooleanVar(master=self, value=False)
        ttk.Checkbutton(self.advanced_frame,
                        text="Allow plain HTTP to this private LAN address",
                        variable=self.allow_lan_var).pack(anchor="w", pady=4)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=480).pack(pady=8, padx=24)

        button_row = ttk.Frame(self)
        button_row.pack(pady=12)
        self.test_button = ttk.Button(button_row, text="Test Connection",
                                      command=self._test_connection)
        self.test_button.pack(side="left", padx=6)
        self.next_button = ttk.Button(button_row, text="Next", state="disabled",
                                      command=self._next)
        self.next_button.pack(side="left", padx=6)

    def _toggle_advanced(self) -> None:
        if self.advanced_visible.get():
            self.advanced_frame.pack(padx=24, pady=4, anchor="w")
        else:
            self.advanced_frame.pack_forget()

    def _test_connection(self) -> None:
        self.status_var.set("CONNECTING...")
        self.test_button.config(state="disabled")
        url = self.url_var.get()
        host = self.host_var.get() or None
        allow_lan = self.allow_lan_var.get()

        def work():
            return core.test_hq_connection(
                url, expected_hq_host=host, allow_insecure_private_lan=allow_lan)

        def done(result: "core.ConnectionResult"):
            self.test_button.config(state="normal")
            self.status_var.set(f"{result.state.value}: {result.detail}")
            ok = result.state in (core.ConnectionState.CONNECTED_TO_HQ,
                                  core.ConnectionState.PRIVATE_LAN_WARNING)
            self.next_button.config(state="normal" if ok else "disabled")
            if ok:
                self.app.state_data["backend_url"] = result.base_url
                self.app.state_data["expected_hq_host"] = host
                self.app.state_data["allow_insecure_private_lan"] = allow_lan

        poll = _run_in_background(work, done)
        poll(self)

    def _next(self) -> None:
        self.app.go_to_enrolment()


class EnrolmentScreen(ttk.Frame):
    """Screen 2 - Enrollment. The code is never on a command line, in a URL,
    or logged; it lives only in this Entry widget until redemption succeeds."""

    def __init__(self, parent, app: StoreSetupApp):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Enroll this computer", font=("Segoe UI", 14, "bold")).pack(pady=12)
        ttk.Label(self, text="Enrollment code").pack(anchor="w", padx=24)

        code_row = ttk.Frame(self)
        code_row.pack(padx=24, pady=4, anchor="w")
        self.code_var = tk.StringVar(master=self)
        self.code_entry = ttk.Entry(code_row, textvariable=self.code_var, width=32, show="*")
        self.code_entry.pack(side="left")
        self.show_var = tk.BooleanVar(master=self, value=False)
        ttk.Checkbutton(code_row, text="Show", variable=self.show_var,
                        command=self._toggle_show).pack(side="left", padx=6)

        ttk.Label(self, text="Device name").pack(anchor="w", padx=24, pady=(8, 0))
        self.device_name_var = tk.StringVar(master=self, value=app.state_data["device_name"])
        ttk.Entry(self, textvariable=self.device_name_var, width=32).pack(padx=24, pady=4)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=480).pack(pady=8, padx=24)

        button_row = ttk.Frame(self)
        button_row.pack(pady=12)
        self.enroll_button = ttk.Button(button_row, text="Enroll", command=self._enroll)
        self.enroll_button.pack(side="left", padx=6)
        self.next_button = ttk.Button(button_row, text="Next", state="disabled",
                                      command=lambda: app.go_to_audio())
        self.next_button.pack(side="left", padx=6)

    def _toggle_show(self) -> None:
        self.code_entry.config(show="" if self.show_var.get() else "*")

    def _enroll(self) -> None:
        # Every Tk variable is read HERE, on the main thread, before the
        # background thread starts. Tkinter variables raise "main thread is
        # not in main loop" if touched off-thread - reading them inside work()
        # would have made this fail only under real timing, never in a quick
        # manual click-through.
        code = self.code_var.get()
        device_name = self.device_name_var.get()
        backend_url = self.app.state_data["backend_url"]
        allow_lan = self.app.state_data["allow_insecure_private_lan"]
        expected_host = self.app.state_data["expected_hq_host"]
        credential_path = self.app.credential_path
        protector = self.app.protector

        self.status_var.set("ENROLLING...")
        self.enroll_button.config(state="disabled")

        def work():
            return core.redeem_enrollment(
                backend_url=backend_url,
                code=code,
                device_name=device_name,
                hostname=socket.gethostname(),
                credential_path=credential_path,
                protector=protector,
                allow_insecure_loopback=True,
                allow_insecure_private_lan=allow_lan,
                expected_hq_host=expected_host,
            )

        def done(result: "core.EnrolmentUiResult"):
            self.enroll_button.config(state="normal")
            # The raw code never persists in this widget or anywhere else once
            # redemption has been attempted - success or failure.
            self.code_var.set("")
            if result.state is core.EnrolmentUiState.ENROLLED:
                self.status_var.set(
                    f"Enrolled. Device: {result.outcome.device_public_id}  "
                    f"Store: {result.outcome.store_id}"
                )
                self.app.state_data["enrolment_outcome"] = result.outcome
                self.next_button.config(state="normal")
            else:
                self.status_var.set(result.detail)

        poll = _run_in_background(work, done)
        poll(self)


class AudioScreen(ttk.Frame):
    """Screen 3 - Audio Output. Nothing here selects a device automatically."""

    def __init__(self, parent, app: StoreSetupApp):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Choose the Store's audio output",
                 font=("Segoe UI", 14, "bold")).pack(pady=12)

        try:
            self.outputs = core.list_classified_outputs()
        except Exception as failure:  # noqa: BLE001 - shown, not swallowed
            self.outputs = []
            ttk.Label(self, text=f"Could not list audio devices: {failure}",
                     wraplength=480).pack(pady=8)

        self.selected = tk.StringVar(master=self)
        for classified in self.outputs:
            label = f"[{classified.kind.value}] {classified.device.name}"
            ttk.Radiobutton(self, text=label, variable=self.selected,
                           value=classified.device.selector).pack(anchor="w", padx=24)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=480).pack(pady=8, padx=24)

        self.heard_var = tk.BooleanVar(master=self, value=False)
        self.heard_check = ttk.Checkbutton(
            self, text="I heard the test sound from the intended Store output",
            variable=self.heard_var, state="disabled", command=self._on_heard_toggle)
        self.heard_check.pack(pady=6, padx=24, anchor="w")

        button_row = ttk.Frame(self)
        button_row.pack(pady=12)
        ttk.Button(button_row, text="Test Sound", command=self._test_sound).pack(side="left", padx=6)
        self.next_button = ttk.Button(button_row, text="Next", state="disabled",
                                      command=lambda: app.go_to_install())
        self.next_button.pack(side="left", padx=6)

    def _selected_device(self):
        selector = self.selected.get()
        for classified in self.outputs:
            if classified.device.selector == selector:
                return classified.device
        return None

    def _test_sound(self) -> None:
        device = self._selected_device()
        if device is None:
            self.status_var.set("Choose an output device first.")
            return
        self.status_var.set("PLAYING...")
        self.heard_check.config(state="disabled")
        self.heard_var.set(False)

        def work():
            return core.play_test_tone(device)

        def done(result: "core.TestSoundResult"):
            self.status_var.set(f"{result.state.value}: {result.detail}")
            if result.state is core.TestSoundState.PLAYED:
                self.heard_check.config(state="normal")
            # This confirmation is manual audible proof, never SPEAKER_VERIFIED -
            # that requires EchoGuard acoustic evidence, asked for nowhere here.

        poll = _run_in_background(work, done)
        poll(self)

    def _on_heard_toggle(self) -> None:
        if self.heard_var.get():
            self.app.state_data["output_device"] = self._selected_device()
            self.next_button.config(state="normal")
        else:
            self.next_button.config(state="disabled")


class InstallScreen(ttk.Frame):
    """Screen 4 - Install. Success is claimed only after real CONNECTED
    evidence, never merely because a process was started."""

    def __init__(self, parent, app: StoreSetupApp):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Install the Receiver",
                 font=("Segoe UI", 14, "bold")).pack(pady=12)
        self.status_var = tk.StringVar(master=self, value="Ready to install.")
        ttk.Label(self, textvariable=self.status_var, wraplength=480,
                 justify="left").pack(pady=8, padx=24)

        self.install_button = ttk.Button(self, text="Install", command=self._install)
        self.install_button.pack(pady=12)

    def _install(self) -> None:
        self.install_button.config(state="disabled")
        self.status_var.set("INSTALLING...")
        device = self.app.state_data["output_device"]
        outcome = self.app.state_data["enrolment_outcome"]

        def work():
            try:
                # Never a hardcoded placeholder: the newest EchoCastReceiver-*
                # package under artifacts/ that independently re-verifies -
                # PE subsystems, every file hash, no forbidden file - rather
                # than a path trusted because it sorts first or is named right.
                package_path = core.locate_verified_receiver_package()
            except core.NoVerifiedReceiverPackage as failure:
                return core.InstallResult(state=core.InstallState.INSTALL_FAILED,
                                          detail=str(failure))
            arguments = [
                "-PackagePath", str(package_path),
                "-BackendUrl", self.app.state_data["backend_url"],
                "-AudioOutputDevice", device.verified_selector if device else "",
            ]
            if self.app.state_data.get("expected_hq_host"):
                arguments += ["-ExpectedHqHost", self.app.state_data["expected_hq_host"]]
            install_result = core.run_receiver_installer(arguments)
            if install_result.returncode != 0:
                return core.InstallResult(state=core.InstallState.INSTALL_FAILED,
                                          detail=install_result.stdout[-2000:] +
                                                 install_result.stderr[-500:])
            return core.wait_for_connected(timeout_seconds=45.0)

        def done(result: "core.InstallResult"):
            self.install_button.config(state="normal")
            lines = [f"{result.state.value}: {result.detail}"]
            if outcome is not None:
                lines.append(f"Device: {outcome.device_public_id}  Store: {outcome.store_id}")
            if result.log_path is not None:
                lines.append(f"Log: {result.log_path}")
            self.status_var.set("\n".join(lines))

        poll = _run_in_background(work, done)
        poll(self)


class RerunScreen(ttk.Frame):
    """Detected an existing installation. Never silently re-enrols.

    Every button below calls store_setup_core directly - none is a
    placeholder. Long-running ones (Repair, Restart, Change Audio Output) run
    on a background thread through _run_in_background, exactly like the
    first-run screens, and each shows a loading/success/error state in
    self.status_var rather than only a Windows return code.
    """

    def __init__(self, parent, app: StoreSetupApp, existing: "core.ExistingInstallation",
                 *, assessment=None):
        super().__init__(parent)
        self.app = app
        self.existing = existing

        self.assessment = assessment

        # A shop has a NAME. "Store: 1" is what made the original report
        # unreadable to the person standing in the shop, and the number means
        # nothing without the database that issued it.
        if assessment is not None and assessment.store_name:
            heading = f"{assessment.store_name} ({assessment.store_code})"
            body = (f"Zone: {assessment.zone}\n"
                    f"Device name: {assessment.device_name or 'not recorded'}\n"
                    f"Device ID: {existing.device_public_id}\n"
                    f"HQ: {assessment.hq_address}\n\n{assessment.message}")
        elif assessment is not None and assessment.verdict is EnrolmentVerdict.HQ_UNREACHABLE:
            heading = "This computer has an EchoCast setup"
            body = (f"Device ID: {existing.device_public_id}\n"
                    f"HQ: {assessment.hq_address}\n\n{assessment.message}")
        else:
            heading = "This computer is already enrolled"
            body = (f"Device: {existing.device_public_id}\n"
                    f"Store: {existing.store_id}\n\n{existing.detail}")

        ttk.Label(self, text=heading, font=("Segoe UI", 14, "bold")).pack(pady=12)
        ttk.Label(self, text=body, wraplength=480,
                  justify="left").pack(pady=8, padx=24)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=480,
                 justify="left").pack(pady=6, padx=24)

        # The gate for the two destructive actions, INLINE rather than in a
        # modal dialog. A modal was not a gate here at all: in an automated or
        # headless session the dialog's default button fires on its own, so the
        # confirmation returned "confirmed" with nothing typed - a destructive
        # confirmation that did not confirm. Inline, the typed text is read from
        # this widget on the main thread and handed to store_setup_core as data,
        # so core's comparison is the single real check.
        confirm_row = ttk.Frame(self)
        confirm_row.pack(padx=24, pady=(4, 0), anchor="w")
        ttk.Label(confirm_row,
                 text=f"To Uninstall or Replace Device Identity, type the "
                      f"confirmation word first:").pack(anchor="w")
        self.confirm_var = tk.StringVar(master=self, value="")
        ttk.Entry(confirm_row, textvariable=self.confirm_var, width=24).pack(anchor="w", pady=2)

        button_row = ttk.Frame(self)
        button_row.pack(padx=24, anchor="w")
        for column, (label, handler) in enumerate((
            ("Status", self._status),
            ("Repair", self._repair),
            ("Change Audio Output", self._change_audio_output),
            ("Test Sound", self._test_sound),
            ("Restart Receiver", self._restart),
            ("Stop Receiver", self._stop),
            ("Redacted Diagnostics", self._diagnostics),
            ("Export Redacted Diagnostics", self._export_diagnostics),
            ("Open Log Folder", self._open_log_folder),
            ("Uninstall Application", self._uninstall),
        )):
            ttk.Button(button_row, text=label, command=handler, width=26).grid(
                row=column // 2, column=column % 2, padx=4, pady=2, sticky="w")

        ttk.Button(self, text="Replace Device Identity (requires a fresh code)",
                  command=self._replace_identity).pack(pady=8, padx=24, anchor="w")

    # -- helpers --------------------------------------------------------------
    def _busy(self, message: str) -> None:
        self.status_var.set(message)

    def _run(self, work, done) -> None:
        poll = _run_in_background(work, done)
        poll(self)

    # -- Status -----------------------------------------------------------
    def _status(self) -> None:
        self._busy("Reading status...")

        def work():
            return core.get_status_snapshot(
                credential_path=self.app.credential_path, protector=self.app.protector)

        def done(snapshot: "core.StatusSnapshot"):
            task = snapshot.task
            lines = [
                f"Device: {snapshot.device_public_id}  Store: {snapshot.store_id}",
                f"Backend: {snapshot.backend_origin}",
                f"Audio: {snapshot.audio_sink} / {snapshot.audio_output_device}",
                f"Task: {'registered, ours' if task and task.is_ours else task.detail if task else 'unknown'}"
                + (f" ({task.state}, {task.process_count} process(es))" if task and task.is_ours else ""),
                f"Receiver status: {snapshot.receiver_state or '<none yet>'} - {snapshot.receiver_detail}",
                f"HQ reachable: {snapshot.hq_reachable}",
            ]
            self.status_var.set("\n".join(lines))

        self._run(work, done)

    # -- Repair -------------------------------------------------------------
    def _repair(self) -> None:
        self._busy("REPAIRING...")

        def work():
            try:
                package_path = core.locate_verified_receiver_package()
            except core.NoVerifiedReceiverPackage as failure:
                return core.RepairResult(ok=False, detail=str(failure))
            return core.repair_installation(package_path=package_path)

        def done(result: "core.RepairResult"):
            self.status_var.set(("REPAIRED: " if result.ok else "REPAIR FAILED: ") + result.detail)

        self._run(work, done)

    # -- Change Audio Output --------------------------------------------------
    def _change_audio_output(self) -> None:
        # Deliberately NOT wait_window: this dialog is driven by the operator
        # (select, Test Sound, tick "I heard it", Save) and closes itself. A
        # blocking wait here would freeze the Tk loop for as long as the
        # operator takes, and made an automated run hang for tens of seconds.
        self._audio_dialog = _AudioOutputDialog(self, self.app)

    # -- Test Sound (current selector) ---------------------------------------
    def _test_sound(self) -> None:
        self._busy("PLAYING...")

        def work():
            config = core.load_config(core.default_config_path())
            if config is None or not config.audio_output_device:
                return core.TestSoundResult(state=core.TestSoundState.DEVICE_ERROR,
                                            detail="no audio output is configured")
            from tools.windows_audio_devices import resolve_output_device

            device = resolve_output_device(config.audio_output_device)
            return core.play_test_tone(device)

        def done(result: "core.TestSoundResult"):
            self.status_var.set(f"{result.state.value}: {result.detail}")

        self._run(work, done)

    # -- Restart / Stop -------------------------------------------------------
    def _restart(self) -> None:
        self._busy("RESTARTING...")

        def work():
            return core.restart_receiver()

        def done(result: "core.TaskActionResult"):
            self.status_var.set(f"{result.state.value}: {result.detail}")

        self._run(work, done)

    def _stop(self) -> None:
        self._busy("STOPPING...")

        def work():
            return core.stop_receiver()

        def done(result: "core.StopResult"):
            self.status_var.set(("STOPPED" if result.ok else "STOP FAILED") + f": {result.detail}")

        self._run(work, done)

    # -- Diagnostics -----------------------------------------------------
    def _diagnostics(self) -> None:
        self._busy("Building diagnostics...")

        def work():
            return core.build_redacted_diagnostics(credential_path=self.app.credential_path)

        def done(text: str):
            self.status_var.set(text)

        self._run(work, done)

    def _export_diagnostics(self) -> None:
        self._busy("Exporting diagnostics...")

        def work():
            text = core.build_redacted_diagnostics(credential_path=self.app.credential_path)
            return core.export_diagnostics(text)

        def done(path: Path):
            self.status_var.set(f"Diagnostics exported to {path}")

        self._run(work, done)

    def _open_log_folder(self) -> None:
        try:
            core.open_log_folder()
            self.status_var.set(f"Opened {core.default_log_directory()}")
        except core.UntrustedLogPath as failure:
            self.status_var.set(str(failure))

    # -- Uninstall ------------------------------------------------------
    #: Typed in the inline confirmation field before Uninstall will run.
    UNINSTALL_CONFIRMATION = "UNINSTALL"

    def _uninstall(self) -> None:
        typed = self.confirm_var.get().strip()
        if typed != self.UNINSTALL_CONFIRMATION:
            self.status_var.set(
                f"Uninstall removes the Receiver application and its Scheduled Task. "
                f"The Device credential, configuration and logs are PRESERVED, and the "
                f"Device is NOT revoked at HQ - an administrator must do that separately "
                f"if this computer is being retired.\n\n"
                f"Type {self.UNINSTALL_CONFIRMATION} in the confirmation field, then "
                f"press Uninstall Application again."
            )
            return
        self.confirm_var.set("")
        self._busy("UNINSTALLING...")

        def work():
            return core.uninstall_receiver()

        def done(result: "core.UninstallResult"):
            self.status_var.set(result.detail)

        self._run(work, done)

    # -- Replace Device Identity ----------------------------------------
    def _replace_identity(self) -> None:
        # The operator's own text goes to core, which owns the comparison. The
        # GUI must never supply the expected word itself: passing
        # core.CONFIRMATION_WORD here made core's check receive the right answer
        # no matter what was typed, so a real check could never fail.
        typed = self.confirm_var.get().strip()
        if not typed:
            self.status_var.set(
                "Replacing the Device identity deletes this computer's LOCAL Receiver "
                "credential. The existing Device at HQ is NOT revoked - ask an "
                "administrator to revoke it separately, or it stays listed as enrolled "
                "while never connecting again. A fresh enrollment code will be needed.\n\n"
                f"Type {core.CONFIRMATION_WORD} in the confirmation field, then press "
                "Replace Device Identity again."
            )
            return
        self.confirm_var.set("")
        removed = core.replace_device_identity(
            credential_path=self.app.credential_path, confirmation_word=typed)
        if removed:
            self.app.go_to_connection()
        else:
            self.status_var.set(
                f"That confirmation word was not correct. Nothing was changed - the "
                f"credential and Device identity are untouched. Type "
                f"{core.CONFIRMATION_WORD} exactly to proceed."
            )


class _AudioOutputDialog(tk.Toplevel):
    """Change Audio Output: select, Test Sound, require the heard checkbox,
    save only after confirmation, then restart and wait for CONNECTED."""

    def __init__(self, parent, app: StoreSetupApp):
        super().__init__(parent)
        self.app = app
        self.title("Change Audio Output")
        self.transient(parent)

        try:
            self.outputs = core.list_classified_outputs()
        except Exception as failure:  # noqa: BLE001 - shown, not swallowed
            self.outputs = []
            ttk.Label(self, text=f"Could not list audio devices: {failure}",
                     wraplength=420).pack(pady=8)

        self.selected = tk.StringVar(master=self)
        for classified in self.outputs:
            label = f"[{classified.kind.value}] {classified.device.name}"
            ttk.Radiobutton(self, text=label, variable=self.selected,
                           value=classified.device.selector).pack(anchor="w", padx=16)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=420).pack(pady=6, padx=16)

        self.heard_var = tk.BooleanVar(master=self, value=False)
        self.heard_check = ttk.Checkbutton(
            self, text="I heard the test sound from the intended Store output",
            variable=self.heard_var, state="disabled")
        self.heard_check.pack(pady=6, padx=16, anchor="w")

        button_row = ttk.Frame(self)
        button_row.pack(pady=8)
        ttk.Button(button_row, text="Test Sound", command=self._test_sound).pack(side="left", padx=6)
        self.save_button = ttk.Button(button_row, text="Save and Restart",
                                      state="disabled", command=self._save_and_restart)
        self.save_button.pack(side="left", padx=6)
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="left", padx=6)

    def _selected_device(self):
        for classified in self.outputs:
            if classified.device.selector == self.selected.get():
                return classified.device
        return None

    def _test_sound(self) -> None:
        device = self._selected_device()
        if device is None:
            self.status_var.set("Choose an output device first.")
            return
        self.status_var.set("PLAYING...")
        self.heard_var.set(False)
        self.heard_check.config(state="disabled")
        self.save_button.config(state="disabled")

        def work():
            return core.play_test_tone(device)

        def done(result: "core.TestSoundResult"):
            self.status_var.set(f"{result.state.value}: {result.detail}")
            if result.state is core.TestSoundState.PLAYED:
                self.heard_check.config(state="normal", command=self._on_heard_toggle)

        poll = _run_in_background(work, done)
        poll(self)

    def _on_heard_toggle(self) -> None:
        self.save_button.config(state="normal" if self.heard_var.get() else "disabled")

    def _save_and_restart(self) -> None:
        device = self._selected_device()
        self.status_var.set("SAVING AND RESTARTING...")
        self.save_button.config(state="disabled")

        def work():
            return core.change_audio_output(device=device)

        def done(result: "core.TaskActionResult"):
            self.status_var.set(f"{result.state.value}: {result.detail}")

        poll = _run_in_background(work, done)
        poll(self)


class WelcomeScreen(ttk.Frame):
    """Page 1. What this is, in the words a shop assistant uses."""

    def __init__(self, parent, app: "StoreSetupApp"):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="EchoCast Store Receiver Setup",
                  font=("Segoe UI", 15, "bold")).pack(pady=(18, 6))
        ttk.Label(self, text=(
            "This installs this Store computer so it can receive live "
            "announcements from HQ."
        ), wraplength=470, justify="left").pack(pady=6, padx=24)

        version = _package_version()
        if version:
            ttk.Label(self, text=f"Package version: {version}",
                      foreground="#555").pack(pady=(0, 8))

        # An incomplete package is said out loud HERE, before the operator spends
        # any time on it. The wizard cannot install a Receiver it does not have,
        # and that is exactly what shipped once.
        if app.package_missing:
            listed = ", ".join(app.package_missing[:4])
            ttk.Label(self, text=(
                "This copy of EchoCast Store Setup is INCOMPLETE and cannot "
                "install anything.\nMissing: " + listed +
                "\nAsk HQ for the full package."
            ), wraplength=470, justify="left", foreground="#a00").pack(pady=8, padx=24)

        buttons = ttk.Frame(self)
        buttons.pack(pady=14)
        start = ttk.Button(buttons, text="Start Setup", width=26,
                           command=app.go_to_connection)
        start.grid(row=0, column=0, padx=6)
        if app.package_missing:
            start.config(state="disabled")

        # Offered only for an installation the CURRENT HQ still accepts. A stale
        # identity never reaches this screen, and a working one should not be
        # dragged through the whole wizard to look at its own status.
        current = (app.assessment is not None
                   and app.assessment.verdict is EnrolmentVerdict.CURRENT)
        if app.existing.is_installed and current:
            ttk.Button(buttons, text="Open Receiver Status", width=26,
                       command=self._open_status).grid(row=0, column=1, padx=6)

    def _open_status(self) -> None:
        self.app._show(RerunScreen(self.app._container, self.app, self.app.existing,
                                   assessment=self.app.assessment))


class OldEnrolmentScreen(ttk.Frame):
    """The second-PC case, made recoverable in one click.

    A Store PC reported "already enrolled - Store: 1" while being unable to
    receive anything: the credential had been minted against a throwaway pilot
    database where Store 1 was "LAN pilot Store". This screen says that in words,
    shows the identifiers an operator needs to recognise their own machine, and
    offers exactly one action.
    """

    def __init__(self, parent, app: "StoreSetupApp", assessment):
        super().__init__(parent)
        self.app = app
        self.assessment = assessment

        archived = assessment.verdict is EnrolmentVerdict.ARCHIVED_STORE
        heading = ("This Device belongs to an archived Store"
                   if archived else "Old EchoCast enrolment detected")
        explanation = (
            "This Device belongs to a Store that has been retired at HQ, so this "
            "computer must be enrolled again."
            if archived else
            "This computer was enrolled to an earlier EchoCast pilot server.\n"
            "The current HQ does not recognise this Device, so it cannot receive "
            "announcements until it is set up again."
        )

        ttk.Label(self, text=heading, font=("Segoe UI", 14, "bold")).pack(pady=(16, 6))
        ttk.Label(self, text=explanation, wraplength=470,
                  justify="left").pack(pady=4, padx=24)

        # Identifiers, not secrets. The operator has to be able to tell that this
        # is their machine; the sealed credential never appears here or anywhere.
        detail = ttk.LabelFrame(self, text="Details for HQ")
        detail.pack(fill="x", padx=24, pady=12)
        rows = [
            ("Old Device ID", assessment.device_public_id or "unknown"),
            ("Old Store number",
             str(assessment.store_id) if assessment.store_id is not None else "unknown"),
            ("Current HQ address", assessment.hq_address or "unknown"),
            ("HQ reachable", "Yes" if assessment.hq_reachable else "No"),
            ("Device recognised by HQ", "Yes" if assessment.hq_authenticated else "No"),
        ]
        if archived and assessment.store_name:
            rows.insert(2, ("Archived Store",
                            f"{assessment.store_name} ({assessment.store_code})"))
        for index, (label, value) in enumerate(rows):
            ttk.Label(detail, text=label + ":").grid(row=index, column=0,
                                                     sticky="w", padx=8, pady=1)
            ttk.Label(detail, text=value).grid(row=index, column=1,
                                               sticky="w", padx=8, pady=1)
        ttk.Label(detail, text=("The old Store number is only useful to HQ. It does "
                                "not name a shop in the current system."),
                  wraplength=430, foreground="#555",
                  justify="left").grid(row=len(rows), column=0, columnspan=2,
                                       sticky="w", padx=8, pady=(4, 6))

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=470,
                  justify="left").pack(pady=4, padx=24)

        self.remove_button = ttk.Button(
            self, text="Remove Old Enrolment and Set Up Again",
            command=self._remove_old_enrolment)
        self.remove_button.pack(pady=10)

        ttk.Label(self, text=(
            "This removes only this computer's old setup. Your HQ Stores, and the "
            "log files on this computer, are not changed."
        ), wraplength=470, foreground="#555", justify="left").pack(padx=24)

    def _remove_old_enrolment(self) -> None:
        if not confirm_removal(self):
            self.status_var.set("Nothing was changed.")
            return
        self.remove_button.config(state="disabled")
        self.status_var.set("Removing the old setup...")
        state_root = self.app.state_root
        assessment = self.assessment

        def work():
            # Stopped first: a running Receiver holding the old credential would
            # keep trying to authenticate against an HQ that already rejected it.
            stop_receiver_task()
            return replace_local_enrolment(state_root=state_root,
                                           assessment=assessment)

        def done(result):
            if isinstance(result, Exception):
                self.remove_button.config(state="normal")
                self.status_var.set(f"Could not remove the old setup: {result}")
                return
            if not getattr(result, "ok", False):
                self.remove_button.config(state="normal")
                self.status_var.set(f"Could not remove the old setup: {result.detail}")
                return
            self.app.go_to_welcome()

        poll = _run_in_background(work, done)
        poll(self)


class ResultScreen(ttk.Frame):
    """Page 6. Eight facts, never one green tick.

    Collapsing these is how "the process started" became "the Store is playing
    announcements" - the original defect this whole project was built to stop.
    """

    ROWS = (
        "Files installed",
        "Scheduled Task installed",
        "Receiver process running",
        "HQ reachable",
        "Device authenticated",
        "WebSocket connected",
        "Receiver ready",
        "Test sound heard by operator",
    )

    def __init__(self, parent, app: "StoreSetupApp", *, checks: dict):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Setup result",
                  font=("Segoe UI", 14, "bold")).pack(pady=(16, 4))
        ttk.Label(self, text=("Each line is checked separately. A line that is not "
                              "confirmed is not a failure of the others."),
                  wraplength=470, justify="left",
                  foreground="#555").pack(pady=(0, 8), padx=24)

        grid = ttk.Frame(self)
        grid.pack(fill="x", padx=28)
        for index, name in enumerate(self.ROWS):
            passed = bool(checks.get(name))
            ttk.Label(grid, text=name).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Label(grid, text="OK" if passed else "Not confirmed",
                      foreground="#0a0" if passed else "#a60").grid(
                row=index, column=1, sticky="w", padx=14)

        # Deliberately absent: any sentence claiming the speakers work. An
        # operator hearing a chime is evidence a human heard something. It is not
        # SPEAKER_VERIFIED, which needs EchoGuard acoustic evidence.
        ttk.Label(self, text=("'Test sound heard by operator' records what a person "
                              "heard. It is not acoustic verification."),
                  wraplength=470, justify="left",
                  foreground="#555").pack(pady=10, padx=24)

        buttons = ttk.Frame(self)
        buttons.pack(pady=12)
        ttk.Button(buttons, text="Open Status", width=22,
                   command=self._open_status).grid(row=0, column=0, padx=5)
        ttk.Button(buttons, text="Export Redacted Diagnostics", width=26,
                   command=self._export).grid(row=0, column=1, padx=5)
        ttk.Button(buttons, text="Close", width=14,
                   command=self.app.destroy).grid(row=0, column=2, padx=5)

        self.status_var = tk.StringVar(master=self, value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=470,
                  justify="left").pack(padx=24)

    def _open_status(self) -> None:
        existing = core.detect_existing_installation(
            credential_path=self.app.credential_path, protector=self.app.protector)
        self.app._show(RerunScreen(self.app._container, self.app, existing,
                                   assessment=self.app.assessment))

    def _export(self) -> None:
        def work():
            return core.export_diagnostics()

        def done(result):
            if isinstance(result, Exception):
                self.status_var.set(f"Could not export diagnostics: {result}")
            else:
                self.status_var.set(f"Diagnostics written to: {result}")

        poll = _run_in_background(work, done)
        poll(self)


def _package_version() -> str:
    """The version from the packaged manifest, or empty in a checkout."""
    import json

    manifest = resource_paths.resource_root() / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
    except Exception:
        return ""



def main(argv=None) -> int:
    app = StoreSetupApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
