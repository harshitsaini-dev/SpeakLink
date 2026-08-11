"""The one-click Store Kit installer, read and driven as a script.

An installer is the one piece of this system that runs on a machine nobody at
HQ can see, operated by somebody who is not going to read a runbook. So the
properties worth testing are not "does it copy files" but the four decisions it
makes on their behalf:

  * install vs upgrade is decided by LOOKING at the machine, not by asking;
  * a program with no scheduled task is a REPAIR, not an upgrade - otherwise a
    broken Store gets a version bump and stays broken;
  * the Device credential survives install, upgrade and repair, so an update is
    never an accidental re-enrolment;
  * uninstall keeps the credential unless it is explicitly told not to,
    because removing the software and revoking the Store's identity are
    different decisions.

PowerShell is driven for real where Windows allows it - the -DryRun path
touches nothing - and the rest is asserted by reading the script, which is
what a reviewer would do.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
INSTALLER = SCRIPTS / "SpeakLink-StoreKit.ps1"
LAUNCHER = SCRIPTS / "SpeakLink-StoreKit.cmd"

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")
windows_only = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None,
    reason="the Store installer is a Windows PowerShell script")


@pytest.fixture(scope="module")
def source() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def run_installer(tmp_path: Path, *args: str, package: Path | None = None):
    """Run the installer against directories inside tmp_path only."""
    payload = package or (tmp_path / "kit")
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "SpeakLinkReceiverBackground.exe").write_bytes(b"MZ fake")
    (payload / "SpeakLinkReceiver.exe").write_bytes(b"MZ fake")

    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(INSTALLER),
        "-PackagePath", str(payload),
        "-InstallRoot", str(tmp_path / "app"),
        "-StateRoot", str(tmp_path / "state"),
        "-TaskName", "SpeakLink Test Receiver Do Not Use",
        *args,
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=180)


# ===========================================================================
# The decision it makes for you
# ===========================================================================

@windows_only
def test_a_clean_machine_is_an_install(tmp_path):
    result = run_installer(tmp_path, "-Action", "Auto", "-DryRun",
                           "-BackendUrl", "http://hq.invalid:8000")
    assert result.returncode == 0, result.stderr
    assert "Action: Install" in result.stdout
    # A dry run means what it says.
    assert not (tmp_path / "app").exists()


@windows_only
def test_a_machine_with_the_program_and_a_missing_task_is_a_repair(tmp_path):
    """The case that matters most: the Store LOOKS installed and does nothing.

    Treating this as an upgrade would work, and would hide a broken machine
    behind a version bump.
    """
    app = tmp_path / "app"
    app.mkdir(parents=True)
    (app / "SpeakLinkReceiverBackground.exe").write_bytes(b"MZ fake")

    result = run_installer(tmp_path, "-Action", "Auto", "-DryRun")
    assert result.returncode == 0, result.stderr
    assert "Action: Repair" in result.stdout


@windows_only
def test_an_explicit_action_overrides_what_the_machine_looks_like(tmp_path):
    result = run_installer(tmp_path, "-Action", "Uninstall", "-DryRun")
    assert result.returncode == 0, result.stderr
    assert "Action: Uninstall" in result.stdout


@windows_only
def test_it_reports_what_it_found_before_doing_anything(tmp_path):
    """An installer that acts silently is one nobody can debug over a phone."""
    result = run_installer(tmp_path, "-Action", "Auto", "-DryRun",
                           "-BackendUrl", "http://hq.invalid:8000")
    assert "This machine:" in result.stdout
    assert "program absent" in result.stdout
    assert "enrolled no" in result.stdout


@windows_only
def test_a_kit_with_no_payload_says_so_instead_of_half_installing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    command = [
        POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(INSTALLER), "-PackagePath", str(empty),
        "-InstallRoot", str(tmp_path / "app"), "-StateRoot", str(tmp_path / "state"),
        "-Action", "Install",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    assert result.returncode != 0
    assert "No Receiver payload found" in (result.stderr + result.stdout)


@windows_only
def test_an_install_with_no_settings_and_no_backend_url_is_refused(tmp_path):
    """Better to refuse than to install a Receiver that has nowhere to call."""
    result = run_installer(tmp_path, "-Action", "Install")
    assert result.returncode != 0
    assert "-BackendUrl is required" in (result.stderr + result.stdout)


# ===========================================================================
# What the four verbs promise, read from the script
# ===========================================================================

def test_upgrade_and_repair_never_delete_the_credential(source):
    """Asserted by reading, because the alternative is running an uninstall on
    a real machine to prove it. The credential path is removed in exactly one
    place, and that place is behind -RemoveCredential."""
    removals = [line for line in source.splitlines()
                if "Remove-Item" in line and "StateRoot" in line]
    assert len(removals) == 1, (
        f"the state directory is removed in {len(removals)} places; it must be "
        "removable in exactly one, under -RemoveCredential")

    guarded = source.split("if ($RemoveCredential)")[1]
    assert "Remove-Item -LiteralPath $StateRoot" in guarded


def test_uninstall_keeps_the_credential_by_default(source):
    assert "RemoveCredential" in source
    assert "[switch]$RemoveCredential" in source, "it must default to off"
    assert "The Device credential and settings were KEPT" in source


def test_uninstall_does_not_pretend_to_revoke_anything_at_hq(source):
    """A machine cannot decide that a Device is no longer trusted; HQ does."""
    assert "The Device still exists at HQ" in source


def test_the_copy_retries_because_antivirus_holds_new_binaries_open(source):
    """Three files out of forty-four failed this way once, all PE binaries, and
    the installer reported a generic IO error."""
    assert "foreach ($attempt in 1..5)" in source
    assert "sharing violation" in source or "holding them open" in source


def test_a_partial_copy_fails_loudly_and_names_the_files(source):
    assert "could not be written" in source
    assert "$failures -join" in source


def test_an_upgrade_preserves_settings_it_was_not_asked_to_change(source):
    """Losing the HQ address or the chosen audio device turns a working shop
    into a silent one, which is the worst possible outcome of an update."""
    assert "if ($BackendUrl)        { $values['backend_url'] = $BackendUrl }" in source
    assert "$config = if ($Existing) { $Existing }" in source


def test_the_runtime_is_stopped_before_files_are_replaced(source):
    install = source.split("function Invoke-InstallOrUpgrade")[1]
    assert install.index("Stop-Runtime") < install.index("Copy-Payload"), (
        "files are replaced before the process using them is stopped")


# ===========================================================================
# The thing somebody actually double-clicks
# ===========================================================================

def test_the_launcher_offers_repair_and_uninstall(tmp_path):
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "-Action Repair" in text
    assert "-Action Uninstall" in text
    assert "-RemoveCredential" in text


def test_forgetting_a_store_needs_a_typed_confirmation(tmp_path):
    text = LAUNCHER.read_text(encoding="utf-8")
    forget = text.split(":confirmforget")[1]
    assert "Type YES to continue" in forget
    assert "enrolled again" in forget


def test_the_launcher_bypasses_execution_policy_for_itself_only(tmp_path):
    """A Store PC with a locked-down policy would otherwise fail with a red
    wall of text that reads like the kit is broken."""
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass -File" in text
    assert "Set-ExecutionPolicy" not in text, "the machine's policy must not be changed"


# ===========================================================================
# The one-file installer (tools/store_installer.py)
# ===========================================================================

def installer_module():
    """The installer's logic, imported without running its window."""
    import importlib
    tools = str(REPOSITORY_ROOT / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    return importlib.import_module("store_installer")


def test_the_installer_decides_from_what_is_on_the_machine(tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(module, "_task_exists", lambda name=module.TASK_NAME: False)

    assert module.read_state().suggested_action() == "install"

    # Program present, task gone: a Store that LOOKS installed and does
    # nothing. Calling that an upgrade would hide a broken machine behind a
    # version bump.
    program = tmp_path / "app" / module.BACKGROUND_EXE
    program.parent.mkdir(parents=True, exist_ok=True)
    program.write_bytes(b"MZ")
    assert module.read_state().suggested_action() == "repair"

    monkeypatch.setattr(module, "_task_exists", lambda name=module.TASK_NAME: True)
    assert module.read_state().suggested_action() == "upgrade"


def test_uninstall_keeps_the_credential_unless_told_otherwise(tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "anything.dll").write_bytes(b"x")
    (tmp_path / "state").mkdir()
    credential = tmp_path / "state" / module.CREDENTIAL_FILE
    credential.write_bytes(b"sealed")

    said = []
    outcome = module.do_uninstall(said.append)
    assert not (tmp_path / "app").exists(), "the program files stayed"
    assert credential.exists(), "an ordinary uninstall removed the credential"
    assert "KEPT" in outcome

    module.do_uninstall(said.append, remove_credential=True)
    assert not credential.exists()


def test_an_upgrade_adopts_an_older_installation_instead_of_running_beside_it(
        tmp_path, monkeypatch):
    """Two Receivers on one machine authenticate as the same Device and fight
    over the audio endpoint - from HQ it looks like a Store that reconnects
    every few seconds."""
    module = installer_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(module, "_task_exists", lambda name=module.TASK_NAME: False)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    old_app = tmp_path / "local" / "SpeakLink" / "receiver-agent"
    (old_app / "Receiver").mkdir(parents=True)
    (old_app / module.BACKGROUND_EXE).write_bytes(b"MZ old")
    old_credential = old_app / module.CREDENTIAL_FILE
    old_credential.write_bytes(b"sealed by the old kit")

    said = []
    module.adopt_legacy_installation(said.append)

    carried = tmp_path / "state" / module.CREDENTIAL_FILE
    assert carried.exists(), "the upgrade would have re-enrolled the Store"
    assert carried.read_bytes() == b"sealed by the old kit"
    assert not old_app.exists(), "the old installation was left running beside the new one"


def test_an_existing_credential_is_never_replaced_by_an_older_one(tmp_path, monkeypatch):
    """The credential in the current location is the one HQ knows about."""
    module = installer_module()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(module, "_task_exists", lambda name=module.TASK_NAME: False)
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))

    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / module.CREDENTIAL_FILE).write_bytes(b"current")
    old_app = tmp_path / "local" / "SpeakLink" / "receiver-agent"
    (old_app / "Receiver").mkdir(parents=True)
    (old_app / module.BACKGROUND_EXE).write_bytes(b"MZ old")
    (old_app / module.CREDENTIAL_FILE).write_bytes(b"stale")

    module.adopt_legacy_installation(lambda _: None)
    assert (tmp_path / "state" / module.CREDENTIAL_FILE).read_bytes() == b"current"


def test_an_installer_without_its_payload_refuses_rather_than_installing_nothing(
        tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setattr(module, "payload_path", lambda: tmp_path / "absent.zip")
    with pytest.raises(RuntimeError) as refusal:
        module.unpack_payload(lambda _: None, tmp_path / "app")
    assert "without its payload" in str(refusal.value)


def test_the_installer_never_pretends_to_revoke_anything_at_hq():
    source = (REPOSITORY_ROOT / "tools" / "store_installer.py").read_text(encoding="utf-8")
    assert "The Device still exists at" in source and "revoke it there" in source


def test_the_installer_is_not_tied_to_the_old_product_name():
    """It is its own thing now; nothing here should carry the old name."""
    source = (REPOSITORY_ROOT / "tools" / "store_installer.py").read_text(encoding="utf-8")
    assert "echocast" not in source.lower()


# ===========================================================================
# Administrator rights: asked for once, at the step that needs them
# ===========================================================================

def test_elevation_is_asked_for_only_when_the_task_is_refused(tmp_path, monkeypatch):
    """An installer that demanded elevation to START would be one somebody
    clicks through without reading, on every run, including the ones that only
    check. The prompt belongs at the step that cannot proceed without it."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(module, "ALREADY_ELEVATED", False)

    asked = []
    monkeypatch.setattr(module, "relaunch_elevated",
                        lambda *a, **k: asked.append(a) or 0)

    # A task that registers normally must not prompt.
    monkeypatch.setattr(module, "register_task", lambda report: None)
    assert module._register_task_or_fall_back(lambda _: None) is None
    assert asked == []


def test_access_denied_from_schtasks_is_named_for_what_it_is(tmp_path, monkeypatch):
    """The raw "Access is denied" sends people looking at file permissions."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "ERROR: Access is denied."))

    with pytest.raises(PermissionError) as refusal:
        module.register_task(lambda _: None)
    assert "administrator rights" in str(refusal.value)


def test_a_refused_prompt_still_leaves_a_working_store(tmp_path, monkeypatch):
    """A Store that installed and then starts nothing at sign-in is worse than
    one running the weaker arrangement - and the operator is told which."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(module, "ALREADY_ELEVATED", False)
    monkeypatch.setattr(module, "stop_runtime", lambda report: None)
    monkeypatch.setattr(module, "unpack_payload",
                        lambda report, root, on_progress=None: None)
    monkeypatch.setattr(module, "adopt_legacy_installation", lambda report: None)
    monkeypatch.setattr(module, "start_runtime", lambda report: None)

    def refuse(report):
        raise PermissionError("Creating the logon task on this machine needs "
                              "administrator rights.")
    monkeypatch.setattr(module, "register_task", refuse)
    # The person says No to the UAC prompt.
    monkeypatch.setattr(module, "relaunch_elevated", lambda *a, **k: -5)

    said = []
    outcome = module.do_install_or_upgrade(said.append,
                                           backend_url="http://hq.invalid:8000")

    launcher = (tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "SpeakLink Store Receiver.cmd")
    assert launcher.exists(), "nothing would start the Receiver at sign-in"
    assert "Startup folder" in outcome
    assert "will not restart it" in outcome, (
        "the weaker arrangement was not described as weaker")


def test_an_elevated_run_that_is_still_refused_does_not_prompt_again(tmp_path, monkeypatch):
    """Elevated and still refused is policy, not privilege. Another prompt
    would be a loop."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(module, "ALREADY_ELEVATED", True)
    monkeypatch.setattr(module, "register_task", lambda report: (_ for _ in ()).throw(
        PermissionError("needs administrator rights")))
    prompts = []
    monkeypatch.setattr(module, "relaunch_elevated",
                        lambda *a, **k: prompts.append(a) or 0)

    note = module._register_task_or_fall_back(lambda _: None)
    assert prompts == []
    assert "Startup folder" in note


def test_uninstall_removes_the_startup_fallback_too(tmp_path, monkeypatch):
    """Otherwise a machine that fell back keeps launching a Receiver that is
    no longer installed."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    module.register_startup_shortcut(lambda _: None)
    launcher = (tmp_path / "roaming" / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "SpeakLink Store Receiver.cmd")
    assert launcher.exists()

    module.do_uninstall(lambda _: None)
    assert not launcher.exists()


# ===========================================================================
# What the person at the till sees and gets
# ===========================================================================

def test_unpacking_reports_progress_as_it_goes(tmp_path, monkeypatch):
    """A window with nothing moving reads as a hung installer, and the next
    move is to close it halfway through writing a runtime."""
    import zipfile
    module = installer_module()
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        for index in range(120):
            zipped.writestr(f"file-{index}.bin", b"x" * 16)
    monkeypatch.setattr(module, "payload_path", lambda: archive)

    seen = []
    module.unpack_payload(lambda _: None, tmp_path / "app",
                          lambda done, total: seen.append((done, total)))

    assert seen, "the installer unpacked a thousand files in silence"
    assert seen[-1] == (120, 120), "progress never reached the end"
    assert all(done <= total for done, total in seen)


def test_the_desktop_shortcut_points_at_enrolment_not_at_the_installer(
        tmp_path, monkeypatch):
    """A desktop icon that reinstalls software is an invitation to do it by
    accident. Enrolment is the step that actually happens later."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    (tmp_path / "app").mkdir(parents=True)
    (tmp_path / "app" / module.SETUP_WIZARD).write_bytes(b"MZ wizard")

    commands = []
    def record(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(module.subprocess, "run", record)

    assert module.create_desktop_shortcut(lambda _: None) is True
    script = " ".join(commands[0])
    assert module.SETUP_WIZARD in script
    assert "SpeakLinkStoreInstaller" not in script


def test_no_wizard_means_no_shortcut_rather_than_a_broken_one(tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    said = []
    assert module.create_desktop_shortcut(said.append) is False
    assert any("not in this payload" in line for line in said)


def test_uninstall_takes_the_desktop_shortcut_with_it(tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "app"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    desktop = tmp_path / "user" / "Desktop"
    desktop.mkdir(parents=True)
    link = desktop / "SpeakLink Store Setup.lnk"
    link.write_bytes(b"shortcut")

    module.do_uninstall(lambda _: None)
    assert not link.exists(), "a shortcut to software that is gone was left behind"


def test_the_chosen_install_path_travels_to_the_elevated_copy(tmp_path, monkeypatch):
    """Without this the elevated copy uses the defaults and installs a second
    copy somewhere else - which looks like it worked, and leaves the logon task
    pointing at a runtime nobody chose."""
    module = installer_module()
    monkeypatch.setenv("SPEAKLINK_INSTALL_ROOT", str(tmp_path / "chosen"))
    monkeypatch.setenv("SPEAKLINK_STATE_ROOT", str(tmp_path / "state"))

    captured = {}
    class FakeShell:
        def ShellExecuteW(self, _hwnd, _verb, _file, parameters, _dir, _show):
            captured["parameters"] = parameters
            return 42
    class FakeWindll:
        shell32 = FakeShell()
    monkeypatch.setattr(module, "sys", module.sys)
    import ctypes
    monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)

    assert module.relaunch_elevated("install", backend_url="http://hq:8000") == 0
    assert str(tmp_path / "chosen") in captured["parameters"]
    assert "--already-elevated" in captured["parameters"]


def test_the_cli_accepts_the_paths_it_is_relaunched_with(tmp_path, monkeypatch):
    module = installer_module()
    monkeypatch.setattr(module, "read_state", lambda: module.MachineState(
        has_program=False, has_task=False, has_credential=False, version=None))

    code = module.run_cli(["check",
                           "--install-root", str(tmp_path / "app"),
                           "--state-root", str(tmp_path / "state")])
    assert code == 0
    assert os.environ["SPEAKLINK_INSTALL_ROOT"] == str(tmp_path / "app")
    assert os.environ["SPEAKLINK_STATE_ROOT"] == str(tmp_path / "state")
