"""SpeakLinkStoreInstaller.exe - one file that installs, repairs or removes.

WHAT THIS IS AND WHY IT IS ONE EXECUTABLE

A Store PC gets one file. Not a zip to unpack into the right folder, not a
script that Windows opens in Notepad, not a runbook: a file somebody
double-clicks. Everything the Receiver needs - the runtime, FFmpeg, the
enrolment wizard - is carried inside it and unpacked by the installer itself,
so there is no step where a person can put half of it in the wrong place.

THE FOUR THINGS IT CAN DO, AND WHY THEY ARE SEPARATE

Install / Upgrade  Decided by looking at the machine, not by asking. An
                   upgrade replaces the program files and nothing else: the
                   DPAPI Device credential and the settings survive, because
                   an update that re-enrolled the Store would be re-enrolled
                   by whoever happened to be at the till.
Repair             The program is there and the Store still does not work -
                   the logon task was deleted, or a virus scanner ate half the
                   runtime. Rebuilds both, keeps the credential.
Uninstall          Stops the runtime, removes the task and the files, and
                   KEEPS the credential unless told otherwise. Removing the
                   software and revoking the Store's identity are different
                   decisions, and only one of them can be undone from here.
Uninstall + forget Also removes the credential. Says plainly that the Store
                   would need enrolling again, and requires the word FORGET.

WHAT IT NEVER DOES

It never revokes anything at HQ. A Device removed from a machine still exists
in HQ until an operator says otherwise - the machine is not in a position to
decide that, and a machine that could would be a machine an attacker could use
to delete Devices.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "SpeakLink Store Receiver"
TASK_NAME = "SpeakLink Store Receiver"
#: The Receiver sits under Receiver\ inside the payload, beside the enrolment
#: wizard - the same shape the built kit has, so what is installed on a Store
#: is byte-for-byte the tree the build produced rather than a rearrangement
#: this installer invented.
RECEIVER_SUBDIR = "Receiver"
BACKGROUND_EXE = "Receiver/SpeakLinkReceiverBackground.exe"
SETUP_WIZARD = "SpeakLinkStoreSetup.exe"

#: The payload, as packaged by store_installer.spec. Named rather than
#: discovered so a build that forgot to embed it fails loudly at start rather
#: than silently installing nothing.
PAYLOAD_NAME = "store-payload.zip"

#: The window icon, which is NOT the same thing as the executable's icon.
#: PyInstaller's icon= sets what Explorer draws on the file; a tkinter window
#: keeps the default feather until it is told otherwise, so a Store PC showed
#: the SpeakLink mark on the file and something else on the window that was
#: actually asking to change the machine.
ICON_NAME = "speaklink.ico"

#: EARLIER SPEAKLINK INSTALLATIONS, under the names and folders older kits
#: used. A machine set up by hand, or by a kit that predates this installer,
#: has its files and its scheduled task somewhere else - and if they are left
#: alone the machine ends up running TWO Receivers, both authenticating as the
#: same Device, which looks from HQ like a Store that reconnects constantly.
#:
#: So an upgrade adopts them: the credential and settings are carried over
#: (DPAPI seals them to this Windows account, so the file moves fine), the old
#: task is removed, and the old program directory goes with it.
LEGACY_TASK_NAMES = (
    "SpeakLink Receiver",
    "SpeakLink Store Agent",
    "SpeakLinkReceiver",
)
LEGACY_APP_DIRECTORIES = (
    ("LOCALAPPDATA", "SpeakLink/receiver-agent"),
    ("LOCALAPPDATA", "SpeakLink/agent"),
    ("PROGRAMDATA", "SpeakLink/receiver"),
    ("PROGRAMFILES", "SpeakLink/Receiver"),
)
LEGACY_STATE_DIRECTORIES = (
    ("LOCALAPPDATA", "SpeakLink/receiver-agent"),
    ("PROGRAMDATA", "SpeakLink/receiver"),
)
CREDENTIAL_FILE = "device-credential.bin"

#: Windows hides a console for a GUI build, so anything printed goes nowhere.
#: Every step reports through the callback the UI passes in.
Reporter = "callable(str) -> None"


def install_root() -> Path:
    return Path(os.environ.get("SPEAKLINK_INSTALL_ROOT")
                or (Path(os.environ["LOCALAPPDATA"]) / "SpeakLink" / "receiver-app"))


def state_root() -> Path:
    return Path(os.environ.get("SPEAKLINK_STATE_ROOT")
                or (Path(os.environ["LOCALAPPDATA"]) / "SpeakLink" / "receiver"))


def _bundled(name: str) -> Path:
    """A file packaged beside this program, frozen or running from source."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base / name


def payload_path() -> Path:
    """Where the embedded kit is, running frozen or from source."""
    return _bundled(PAYLOAD_NAME)


def icon_path() -> Path:
    frozen = _bundled(ICON_NAME)
    if frozen.exists():
        return frozen
    # Running from the repository, where the icon lives under assets/.
    return Path(__file__).resolve().parents[1] / "assets" / ICON_NAME


@dataclass(frozen=True)
class MachineState:
    has_program: bool
    has_task: bool
    has_credential: bool
    version: str | None

    def suggested_action(self) -> str:
        if not self.has_program:
            return "install"
        # Program present, task gone: the Store LOOKS installed and does
        # nothing. Calling that an upgrade would hide a broken machine behind
        # a version bump.
        if not self.has_task:
            return "repair"
        return "upgrade"

    def summary(self) -> str:
        return (f"program {'present' if self.has_program else 'absent'}, "
                f"logon task {'present' if self.has_task else 'absent'}, "
                f"enrolled {'yes' if self.has_credential else 'no'}"
                + (f", version {self.version}" if self.version else ""))


def _resolve(root_env: str, relative: str) -> Path | None:
    base = os.environ.get(root_env)
    return (Path(base) / relative) if base else None


def find_legacy_installations() -> list[Path]:
    """Program directories from an older kit that are still on this machine."""
    found = []
    for root_env, relative in LEGACY_APP_DIRECTORIES:
        path = _resolve(root_env, relative)
        if path and path != install_root() and (path / BACKGROUND_EXE).exists():
            found.append(path)
    # An older console-only build shipped no background executable, so its
    # directory is recognised by the console one instead.
    for root_env, relative in LEGACY_APP_DIRECTORIES:
        path = _resolve(root_env, relative)
        if (path and path != install_root() and path not in found
                and (path / "SpeakLinkReceiver.exe").exists()):
            found.append(path)
    return found


def find_legacy_tasks() -> list[str]:
    return [name for name in LEGACY_TASK_NAMES
            if name != TASK_NAME and _task_exists(name)]


def adopt_legacy_installation(report) -> None:
    """Carry an older installation forward instead of installing beside it.

    Two Receivers on one machine authenticate as the same Device and fight over
    the audio endpoint; from HQ it looks like a Store that reconnects every few
    seconds. The old one is stopped and removed, and its credential is kept -
    an upgrade must not turn into a re-enrolment just because the old kit used
    a different folder.
    """
    legacy_tasks = find_legacy_tasks()
    legacy_apps = find_legacy_installations()
    if not legacy_tasks and not legacy_apps:
        return

    report("An older installation is on this machine. Upgrading it rather than "
           "installing beside it.")

    for name in legacy_tasks:
        report(f"Stopping and removing the old task '{name}'…")
        for verb in ("/End", "/Delete"):
            command = ["schtasks", verb, "/TN", name]
            if verb == "/Delete":
                command.append("/F")
            subprocess.run(command, capture_output=True, text=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # The credential first, and only if this machine does not already have one:
    # an existing credential in the new location is the one HQ knows about.
    if not (state_root() / CREDENTIAL_FILE).exists():
        for root_env, relative in LEGACY_STATE_DIRECTORIES:
            old_state = _resolve(root_env, relative)
            if old_state and (old_state / CREDENTIAL_FILE).exists():
                state_root().mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_state / CREDENTIAL_FILE, state_root() / CREDENTIAL_FILE)
                report(f"Carried the Device credential over from {old_state}. "
                       "This Store stays enrolled.")
                old_config = old_state / "config.json"
                if old_config.exists() and not (state_root() / "config.json").exists():
                    shutil.copy2(old_config, state_root() / "config.json")
                    report("Carried the old settings over as well.")
                break

    for old_app in legacy_apps:
        report(f"Removing the old program files at {old_app}…")
        shutil.rmtree(old_app, ignore_errors=True)


def is_elevated() -> bool:
    """Whether this process can create a scheduled task on this machine."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_elevated(action: str, *, backend_url: str | None = None,
                      remove_credential: bool = False) -> int:
    """Ask Windows for administrator rights and do the work in a new process.

    WHY ELEVATION IS ASKED FOR AT ALL, AND ONLY HERE

    Registering a logon task is an administrator action on a managed machine -
    the same call that works on a home PC answers "Access is denied" on a shop
    till. Everything else this installer does is inside the Store user's own
    profile and needs nothing, so the prompt appears when the task is about to
    be written and at no other time. An installer that demanded elevation to
    START would be one somebody clicks through without reading, on every run,
    including the ones that only check.

    Returns the elevated process's exit code, or a negative number if the
    prompt was refused or could not be shown.
    """
    import ctypes

    arguments = [action]
    if backend_url:
        arguments += ["--backend-url", backend_url]
    # The chosen paths travel with the relaunch. Without this the elevated copy
    # would use the defaults and install a second copy somewhere else, which
    # looks like the install worked and leaves the task pointing at a runtime
    # nobody chose.
    arguments += ["--install-root", str(install_root()),
                  "--state-root", str(state_root())]
    if remove_credential:
        arguments.append("--remove-credential")
    # The elevated copy is told not to ask again: if IT cannot register the
    # task, the answer is a real failure rather than a second prompt loop.
    arguments.append("--already-elevated")

    quoted = " ".join(f'"{part}"' if " " in part else part for part in arguments)
    try:
        # 42 is an arbitrary "show the window" value; SW_SHOWNORMAL is 1 and is
        # what a person expects to see while a Store is being set up.
        handle = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable if not getattr(sys, "frozen", False)
            else sys.argv[0], quoted, None, 1)
        # ShellExecuteW returns > 32 on success. 5 is ERROR_ACCESS_DENIED,
        # which here means the person said No to the prompt.
        return 0 if int(handle) > 32 else -int(handle)
    except Exception:
        return -1


def _task_exists(task_name: str = TASK_NAME) -> bool:
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_state() -> MachineState:
    manifest = install_root() / "kit-manifest.json"
    version = None
    if manifest.exists():
        try:
            version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError):
            version = None
    return MachineState(
        has_program=((install_root() / BACKGROUND_EXE).exists()
                     or bool(find_legacy_installations())),
        has_task=_task_exists(),
        has_credential=(state_root() / "device-credential.bin").exists(),
        version=version,
    )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def stop_runtime(report) -> None:
    report("Stopping the Receiver…")
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME],
                   capture_output=True, text=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for image in ("SpeakLinkReceiverBackground.exe", "SpeakLinkReceiver.exe"):
        subprocess.run(["taskkill", "/IM", image, "/F"],
                       capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # A file a dying process still holds open cannot be replaced, and the
    # failure reads as a corrupt package rather than a timing problem.
    time.sleep(0.8)


def unpack_payload(report, destination: Path, on_progress=None) -> None:
    """Extract the embedded kit over the install root.

    Each file is retried, because antivirus scanning a freshly written
    executable holds it open for a moment and the copy fails with a sharing
    violation. Three files out of forty-four failed exactly this way once - all
    of them PE binaries - and the installer reported a generic IO error that
    told nobody anything.
    """
    archive_path = payload_path()
    if not archive_path.exists():
        raise RuntimeError(
            "This installer was built without its payload, so there is nothing "
            "to install. Ask HQ for a rebuilt installer.")

    destination.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        report(f"Unpacking {len(members)} files…")
        for index, member in enumerate(members, start=1):
            # Reported as it goes rather than at the end. Unpacking a thousand
            # files takes long enough that a window with nothing moving reads
            # as a hung installer, and the person's next move is to close it
            # halfway through writing a runtime.
            if on_progress is not None and (index % 25 == 0 or index == len(members)):
                on_progress(index, len(members))
            target = destination / member.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            written = False
            for attempt in range(1, 6):
                try:
                    with archive.open(member) as source, target.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    written = True
                    break
                except OSError:
                    time.sleep(0.2 * attempt)
            if not written:
                failures.append(member.filename)
    if failures:
        raise RuntimeError(
            "These files could not be written, most likely because antivirus "
            "was holding them open: " + ", ".join(failures[:8])
            + (f" (and {len(failures) - 8} more)" if len(failures) > 8 else ""))
    report("Files unpacked.")


def register_startup_shortcut(report) -> bool:
    """The fallback when a logon task cannot be created.

    A shortcut in the Startup folder needs no administrator rights, starts the
    Receiver at sign-in, and is worse in one specific way: Windows will not
    restart it if it dies, which the scheduled task does. So it is a fallback
    and is described as one - a Store running this way works, and an operator
    should know it is running the weaker arrangement.
    """
    try:
        startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
                   / "Start Menu" / "Programs" / "Startup")
        startup.mkdir(parents=True, exist_ok=True)
        target = install_root() / BACKGROUND_EXE
        # A .cmd rather than a .lnk: writing a shortcut needs COM, and a
        # one-line launcher does the same job with nothing to go wrong.
        launcher = startup / "SpeakLink Store Receiver.cmd"
        launcher.write_text(
            "@echo off\r\n" + f'start "" "{target}"\r\n', encoding="utf-8")
        report(f"Set the Receiver to start at sign-in via {launcher.name}.")
        return True
    except OSError as failure:
        report(f"The startup shortcut could not be written either: {failure}")
        return False


def remove_startup_shortcut() -> None:
    try:
        launcher = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
                    / "Start Menu" / "Programs" / "Startup"
                    / "SpeakLink Store Receiver.cmd")
        launcher.unlink(missing_ok=True)
    except OSError:
        pass


def setup_wizard_path() -> Path:
    return install_root() / SETUP_WIZARD


def create_desktop_shortcut(report) -> bool:
    """A shortcut to the enrolment wizard, on the desktop.

    Enrolment is the step that happens LATER - a code is read out over the
    phone, sometimes the next morning - and asking somebody to find an
    executable inside AppData at that moment is asking them to fail. The
    shortcut points at the wizard, not at this installer: installing again is
    not what a Store needs, and a desktop icon that reinstalls software is an
    invitation to do it by accident.

    Written through PowerShell's WScript.Shell because a real .lnk needs COM,
    and a .lnk is what carries the icon. A failure here is reported and
    otherwise ignored: no shortcut is a smaller problem than no Receiver.
    """
    target = setup_wizard_path()
    if not target.exists():
        report("The enrolment wizard is not in this payload, so no shortcut "
               "was created.")
        return False
    try:
        desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
        link = desktop / "SpeakLink Store Setup.lnk"
        script = (
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('"
            + str(link).replace("'", "''") + "');"
            "$s.TargetPath = '" + str(target).replace("'", "''") + "';"
            "$s.WorkingDirectory = '" + str(target.parent).replace("'", "''") + "';"
            "$s.IconLocation = '" + str(target).replace("'", "''") + ",0';"
            "$s.Description = 'Enrol this Store with SpeakLink HQ';"
            "$s.Save()")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", script],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0:
            report("The desktop shortcut could not be created: "
                   + (result.stderr or result.stdout).strip())
            return False
        report(f"Desktop shortcut created: {link.name}")
        return True
    except (OSError, KeyError, subprocess.SubprocessError) as failure:
        report(f"The desktop shortcut could not be created: {failure}")
        return False


def remove_desktop_shortcut() -> None:
    try:
        link = (Path(os.environ["USERPROFILE"]) / "Desktop"
                / "SpeakLink Store Setup.lnk")
        link.unlink(missing_ok=True)
    except (OSError, KeyError):
        pass


def launch_setup_wizard() -> bool:
    """Open the enrolment wizard. Returns whether it started."""
    target = setup_wizard_path()
    if not target.exists():
        return False
    try:
        subprocess.Popen([str(target)], cwd=str(target.parent))
        return True
    except OSError:
        return False


def register_task(report) -> None:
    """A LOGON task in the Store user's own session, not a Windows service.

    The Receiver plays audio through WASAPI, and a service runs in session 0,
    which has no audio endpoint - it would authenticate, decode, and write PCM
    into nothing. That silence is more convincing than the ordinary kind, which
    is why this is a scheduled task and why announcements need the Store user
    to be signed in.
    """
    exe = install_root() / BACKGROUND_EXE
    report("Registering the logon task…")
    result = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", f'"{exe}"',
         "/SC", "ONLOGON", "/RL", "LIMITED", "/F"],
        capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        # "Access is denied" from schtasks means this machine requires an
        # administrator to create a logon task. Said in those words rather than
        # passed through, because the raw message sends people looking at file
        # permissions.
        if "denied" in message.lower():
            raise PermissionError(
                "Creating the logon task on this machine needs administrator "
                "rights.")
        raise RuntimeError("The logon task could not be registered: " + message)
    report("Logon task registered.")


def start_runtime(report) -> None:
    result = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME],
                            capture_output=True, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode == 0:
        report("Receiver started.")
    else:
        report("The task was registered but did not start yet. It will start at "
               "the next sign-in.")


def write_settings(report, backend_url: str | None) -> None:
    """Keep every setting this run was not told to change.

    Losing the HQ address or the chosen audio device turns a working shop into
    a silent one, which is the worst outcome an update can have - so an upgrade
    reads what is there and puts back everything it is not replacing.
    """
    path = state_root() / "config.json"
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report("The existing settings could not be read; they will be rewritten.")
    if backend_url:
        existing["backend_url"] = backend_url
    if not existing.get("backend_url"):
        raise RuntimeError(
            "This machine has no HQ address yet. Enter it in the installer "
            "before installing, or run the enrolment wizard afterwards.")
    state_root().mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    report(f"Settings saved to {path}")


# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------

#: Set by the CLI when this process is the elevated copy. It stops a refused
#: prompt turning into an endless chain of prompts, and makes a failure in the
#: elevated process a real failure rather than another question.
ALREADY_ELEVATED = False


def _register_task_or_fall_back(report) -> str | None:
    """Create the logon task, elevating or falling back if it is refused.

    Returns a sentence to append to the outcome when something other than the
    ordinary path happened, or None when the task was created normally.
    """
    try:
        register_task(report)
        return None
    except PermissionError:
        if ALREADY_ELEVATED:
            # Elevated and still refused: this is policy, not privilege, and
            # another prompt would be a loop. Fall back and say so.
            report("Even with administrator rights this machine refused to "
                   "create a logon task.")
            if register_startup_shortcut(report):
                return ("The Receiver starts at sign-in through the Startup "
                        "folder. Windows will not restart it automatically if "
                        "it stops - tell HQ, the machine's policy blocks the "
                        "usual arrangement.")
            raise
        report("This machine needs administrator rights to create the logon "
               "task. Asking Windows for them…")
        return "ELEVATE"


def do_install_or_upgrade(report, *, backend_url: str | None = None,
                          on_progress=None) -> str:
    state = read_state()
    verb = state.suggested_action()
    report(f"This machine: {state.summary()}")
    report(f"Action: {verb}")

    stop_runtime(report)
    # Before anything is written: an older kit's task and files are adopted, so
    # the machine ends up with one Receiver rather than two fighting over the
    # same Device and the same audio endpoint.
    adopt_legacy_installation(report)
    unpack_payload(report, install_root(), on_progress)
    write_settings(report, backend_url)
    create_desktop_shortcut(report)

    note = _register_task_or_fall_back(report)
    if note == "ELEVATE":
        # The files are already in place, so the elevated copy only has to
        # finish the part that needed the rights. It re-runs the whole verb,
        # which is idempotent - unpacking the same payload twice costs seconds
        # and keeps this from becoming two half-installers.
        code = relaunch_elevated("install", backend_url=backend_url)
        if code == 0:
            return ("Windows is finishing the installation with administrator "
                    "rights. Watch the second window for the result.")
        report("Administrator rights were refused.")
        if register_startup_shortcut(report):
            note = ("The Receiver starts at sign-in through the Startup folder "
                    "instead of a logon task. It works; Windows just will not "
                    "restart it automatically if it stops.")
        else:
            raise RuntimeError(
                "The Receiver was installed but nothing will start it at "
                "sign-in. Run this installer again and allow the "
                "administrator prompt.")
    start_runtime(report)

    suffix = ("\n\n" + note) if note and note != "ELEVATE" else ""
    if verb == "upgrade":
        return ("Upgrade complete. This Store is still enrolled - the Device "
                "credential was not touched." + suffix)
    if state.has_credential:
        return ("Install complete, and the existing Device credential was kept."
                + suffix)
    return ("Install complete. This Store is not enrolled yet: run "
            "SpeakLinkStoreSetup.exe from "
            f"{install_root()} and enter the one-time code from HQ." + suffix)


def do_repair(report, *, on_progress=None) -> str:
    state = read_state()
    report(f"This machine: {state.summary()}")
    report("Repairing: the program files and the logon task are rebuilt. The "
           "Device credential and settings are kept.")
    stop_runtime(report)
    unpack_payload(report, install_root(), on_progress)
    create_desktop_shortcut(report)
    note = _register_task_or_fall_back(report)
    if note == "ELEVATE":
        code = relaunch_elevated("repair")
        if code == 0:
            return ("Windows is finishing the repair with administrator "
                    "rights. Watch the second window for the result.")
        report("Administrator rights were refused.")
        register_startup_shortcut(report)
    start_runtime(report)
    if not state.has_credential:
        return ("Repair complete, but this machine has no Device credential, so "
                "it is not enrolled. Run SpeakLinkStoreSetup.exe.")
    return "Repair complete. The Store is still enrolled."


def do_uninstall(report, *, remove_credential: bool = False) -> str:
    state = read_state()
    report(f"This machine: {state.summary()}")
    stop_runtime(report)

    report("Removing the logon task…")
    subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                   capture_output=True, text=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    remove_startup_shortcut()
    remove_desktop_shortcut()

    if install_root().exists():
        report(f"Removing {install_root()}…")
        shutil.rmtree(install_root(), ignore_errors=True)

    if remove_credential:
        report("Removing the Device credential and settings…")
        shutil.rmtree(state_root(), ignore_errors=True)
        return ("Removed. The credential is gone, so this machine would have to "
                "be enrolled again with a new code. The Device still exists at "
                "HQ - revoke it there if this machine is not coming back.")
    return ("Removed. The Device credential and settings were KEPT, so "
            "installing again will not need a new enrolment code.")


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def run_gui() -> int:  # pragma: no cover - exercised by hand on a Store PC
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    window = tk.Tk()
    window.title(f"{APP_NAME} - Installer")
    window.geometry("660x600")
    window.resizable(False, False)
    try:
        window.iconbitmap(str(icon_path()))
    except Exception:
        # A missing or unreadable icon is not a reason to refuse to install
        # anything. The window simply keeps the default one.
        pass

    state = read_state()

    tk.Label(window, text="SpeakLink Store Receiver",
             font=("Segoe UI", 15, "bold")).pack(pady=(14, 2))
    machine_label = tk.Label(window, text=state.summary(), fg="#475569",
                             font=("Segoe UI", 9))
    machine_label.pack()

    form = tk.Frame(window)
    form.pack(pady=8, padx=16, fill="x")

    tk.Label(form, text="HQ address", font=("Segoe UI", 9)).grid(
        row=0, column=0, sticky="e", padx=4, pady=3)
    backend = tk.Entry(form, width=52)
    existing_url = ""
    config = state_root() / "config.json"
    if config.exists():
        try:
            existing_url = json.loads(config.read_text(encoding="utf-8")).get("backend_url", "")
        except (OSError, ValueError):
            existing_url = ""
    backend.insert(0, existing_url or "http://192.168.4.134:8000")
    backend.grid(row=0, column=1, columnspan=2, sticky="we", padx=4, pady=3)

    # WHERE IT GOES, and why it is asked rather than assumed. The default is
    # inside the Store user's profile, which needs no rights and survives
    # Windows updates - but a shop with a small system drive, or an operator
    # who keeps applications on D:, has a real reason to put it elsewhere, and
    # discovering afterwards that 250MB went somewhere unexpected is worse
    # than one more field.
    tk.Label(form, text="Install to", font=("Segoe UI", 9)).grid(
        row=1, column=0, sticky="e", padx=4, pady=3)
    location = tk.Entry(form, width=44)
    location.insert(0, str(install_root()))
    location.grid(row=1, column=1, sticky="we", padx=4, pady=3)

    def browse():
        chosen = filedialog.askdirectory(
            title="Where should the Receiver be installed?",
            initialdir=str(Path(location.get()).parent
                           if location.get() else Path.home()))
        if chosen:
            location.delete(0, "end")
            location.insert(0, str(Path(chosen) / "SpeakLink" / "receiver-app")
                            if not chosen.rstrip("\\/").endswith("receiver-app")
                            else chosen)
    tk.Button(form, text="Browse…", command=browse).grid(
        row=1, column=2, padx=4, pady=3)
    form.grid_columnconfigure(1, weight=1)

    progress = ttk.Progressbar(window, mode="determinate", maximum=100, length=620)
    progress.pack(padx=16, pady=(6, 0))
    step_label = tk.Label(window, text="", fg="#475569", font=("Segoe UI", 9))
    step_label.pack()

    log = scrolledtext.ScrolledText(window, height=14, width=78,
                                    font=("Consolas", 9), state="disabled")
    log.pack(padx=16, pady=8)

    def report(message: str) -> None:
        log.configure(state="normal")
        log.insert("end", message + "\n")
        log.see("end")
        log.configure(state="disabled")
        step_label.configure(text=message[:90])
        window.update_idletasks()

    def show_progress(done: int, total: int) -> None:
        progress.configure(value=(done / total) * 100 if total else 0)
        step_label.configure(text=f"Unpacking {done} of {total} files…")
        window.update_idletasks()

    def apply_paths() -> None:
        """Whatever the person typed is what gets used, here and in the
        elevated copy - which is why it goes into the environment rather than
        being passed to one function."""
        chosen = location.get().strip()
        if chosen:
            os.environ["SPEAKLINK_INSTALL_ROOT"] = chosen

    def offer_enrolment() -> None:
        """Ask, rather than opening the wizard uninvited.

        Enrolment needs a one-time code from HQ, and an installer that opened a
        code prompt at somebody who does not have one yet teaches them to close
        it - which is the window they will need to find again later.
        """
        if not setup_wizard_path().exists():
            return
        if messagebox.askyesno(
                APP_NAME,
                "Open Store Setup now to enrol this Store?\n\n"
                "You will need the one-time code from HQ. If you do not have "
                "it yet, use the SpeakLink Store Setup shortcut on the desktop "
                "when you do."):
            if not launch_setup_wizard():
                messagebox.showwarning(
                    APP_NAME,
                    "Store Setup could not be started. Use the desktop "
                    "shortcut instead.")

    def run(action, *, enrol_after=False):
        for button in buttons:
            button.configure(state="disabled")
        progress.configure(value=0)
        try:
            outcome = action()
            progress.configure(value=100)
            report("")
            report(outcome)
            messagebox.showinfo(APP_NAME, outcome)
            machine_label.configure(text=read_state().summary())
            if enrol_after and not read_state().has_credential:
                offer_enrolment()
        except Exception as failure:
            report("")
            report(f"FAILED: {failure}")
            step_label.configure(text="Failed.")
            messagebox.showerror(APP_NAME, str(failure))
        finally:
            for button in buttons:
                button.configure(state="normal")

    def install():
        apply_paths()
        run(lambda: do_install_or_upgrade(report,
                                          backend_url=backend.get().strip(),
                                          on_progress=show_progress),
            enrol_after=True)

    def repair():
        apply_paths()
        run(lambda: do_repair(report, on_progress=show_progress))

    def forget():
        apply_paths()
        if not messagebox.askyesno(
                APP_NAME,
                "This removes the Device credential as well as the software.\n\n"
                "This Store would have to be enrolled again with a new code "
                "from HQ.\n\nContinue?"):
            return
        run(lambda: do_uninstall(report, remove_credential=True))

    row = tk.Frame(window)
    row.pack(pady=4)
    buttons = [
        tk.Button(row, text="Install / Upgrade", width=18, height=2, command=install),
        tk.Button(row, text="Repair", width=12, height=2, command=repair),
        tk.Button(row, text="Uninstall", width=12, height=2,
                  command=lambda: (apply_paths(),
                                   run(lambda: do_uninstall(report)))),
        tk.Button(row, text="Uninstall + forget", width=16, height=2, command=forget),
    ]
    for index, button in enumerate(buttons):
        button.grid(row=0, column=index, padx=4)

    report(f"Suggested action for this machine: {state.suggested_action()}")
    window.mainloop()
    return 0


def run_cli(argv: list[str]) -> int:
    """The same actions without a window, for a remote session or a test."""
    import argparse

    parser = argparse.ArgumentParser(description="SpeakLink Store Receiver installer")
    parser.add_argument("action", choices=["install", "upgrade", "repair",
                                           "uninstall", "check"])
    parser.add_argument("--backend-url")
    parser.add_argument("--remove-credential", action="store_true")
    parser.add_argument("--already-elevated", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--install-root", help="where the Receiver is installed")
    parser.add_argument("--state-root", help="where the credential and settings live")
    arguments = parser.parse_args(argv)

    # Set as environment rather than passed down: install_root() and
    # state_root() are read from a dozen places, and threading two paths
    # through all of them is how one of them ends up reading the default.
    if arguments.install_root:
        os.environ["SPEAKLINK_INSTALL_ROOT"] = arguments.install_root
    if arguments.state_root:
        os.environ["SPEAKLINK_STATE_ROOT"] = arguments.state_root

    global ALREADY_ELEVATED
    ALREADY_ELEVATED = arguments.already_elevated or is_elevated()

    def report(message: str) -> None:
        print(message, flush=True)

    try:
        if arguments.action == "check":
            state = read_state()
            report(f"This machine: {state.summary()}")
            report(f"Suggested action: {state.suggested_action()}")
            return 0
        if arguments.action in ("install", "upgrade"):
            report(do_install_or_upgrade(report, backend_url=arguments.backend_url))
        elif arguments.action == "repair":
            report(do_repair(report))
        else:
            report(do_uninstall(report, remove_credential=arguments.remove_credential))
        return 0
    except Exception as failure:
        report(f"FAILED: {failure}")
        return 1


def main() -> int:
    if len(sys.argv) > 1:
        return run_cli(sys.argv[1:])
    return run_gui()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
