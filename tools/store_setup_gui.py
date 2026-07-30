"""SpeakLinkStoreSetup.exe: the window. Every decision it shows lives in
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

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import (  # noqa: E402
    DeviceCredentialProtector,
    default_credential_path,
)

DEFAULT_HQ_URL = "http://192.168.4.134:8000"
WINDOW_TITLE = "SpeakLink Store Setup"


class StoreSetupApp(tk.Tk):
    """The whole wizard: one window, one frame swapped in at a time.

    ``protector`` and ``credential_path`` are constructor arguments so tests
    can build this window against a temporary, fake-protected state directory
    rather than the real DPAPI store on this machine.
    """

    def __init__(self, *, credential_path=None, protector=None):
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

        existing = core.detect_existing_installation(
            credential_path=self.credential_path, protector=self.protector)
        if existing.is_installed:
            self._show(RerunScreen(self._container, self, existing))
        else:
            self._show(ConnectionScreen(self._container, self))

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
            raise outcome["error"]
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
        self.url_var = tk.StringVar(value=app.state_data["backend_url"])
        ttk.Entry(self, textvariable=self.url_var, width=48).pack(padx=24, pady=4)

        self.advanced_visible = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Advanced settings", variable=self.advanced_visible,
                        command=self._toggle_advanced).pack(anchor="w", padx=24)

        self.advanced_frame = ttk.Frame(self)
        ttk.Label(self.advanced_frame, text="Expected HQ host (private LAN only)").pack(anchor="w")
        self.host_var = tk.StringVar()
        ttk.Entry(self.advanced_frame, textvariable=self.host_var, width=32).pack(anchor="w")
        self.allow_lan_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.advanced_frame,
                        text="Allow plain HTTP to this private LAN address",
                        variable=self.allow_lan_var).pack(anchor="w", pady=4)

        self.status_var = tk.StringVar(value="")
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
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(code_row, textvariable=self.code_var, width=32, show="*")
        self.code_entry.pack(side="left")
        self.show_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(code_row, text="Show", variable=self.show_var,
                        command=self._toggle_show).pack(side="left", padx=6)

        ttk.Label(self, text="Device name").pack(anchor="w", padx=24, pady=(8, 0))
        self.device_name_var = tk.StringVar(value=app.state_data["device_name"])
        ttk.Entry(self, textvariable=self.device_name_var, width=32).pack(padx=24, pady=4)

        self.status_var = tk.StringVar(value="")
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

        self.selected = tk.StringVar()
        for classified in self.outputs:
            label = f"[{classified.kind.value}] {classified.device.name}"
            ttk.Radiobutton(self, text=label, variable=self.selected,
                           value=classified.device.selector).pack(anchor="w", padx=24)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=480).pack(pady=8, padx=24)

        self.heard_var = tk.BooleanVar(value=False)
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
            # that requires LinkGuard acoustic evidence, asked for nowhere here.

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
        self.status_var = tk.StringVar(value="Ready to install.")
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
                # Never a hardcoded placeholder: the newest SpeakLinkReceiver-*
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

    def __init__(self, parent, app: StoreSetupApp, existing: "core.ExistingInstallation"):
        super().__init__(parent)
        self.app = app
        self.existing = existing

        ttk.Label(self, text="This computer is already enrolled",
                 font=("Segoe UI", 14, "bold")).pack(pady=12)
        ttk.Label(self, text=(
            f"Device: {existing.device_public_id}\n"
            f"Store: {existing.store_id}\n\n{existing.detail}"
        ), wraplength=480, justify="left").pack(pady=8, padx=24)

        self.status_var = tk.StringVar(value="")
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
        self.confirm_var = tk.StringVar(value="")
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

        self.selected = tk.StringVar()
        for classified in self.outputs:
            label = f"[{classified.kind.value}] {classified.device.name}"
            ttk.Radiobutton(self, text=label, variable=self.selected,
                           value=classified.device.selector).pack(anchor="w", padx=16)

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=420).pack(pady=6, padx=16)

        self.heard_var = tk.BooleanVar(value=False)
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


def main(argv=None) -> int:
    app = StoreSetupApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
