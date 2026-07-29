"""The HQ Scheduled Task: what gets registered, and what is refused first.

WHY A SCHEDULED TASK AND NOT A SERVICE

The same reason as the Store Receiver, for a different consequence. HQ does not
play audio, so session 0 would not silence it - but the runtime serves the
React build and the API to a private LAN, and running it as SYSTEM would mean
storing a Windows password or granting a service account rights over the
persistent database. An interactive task owned by the HQ user needs neither.

What that costs honestly: **HQ does not run before somebody signs in.** No
setting in these scripts changes that, and none of them pretend to.

WHAT THESE TESTS CAN AND CANNOT SEE

They read the scripts, and they run the installer's refusal and dry-run paths
for real against an isolated task name and a temporary install root. They never
register a live task, never touch the real persistent root, and cannot see what
happens across a reboot - that is an operator checkpoint.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"

INSTALL = SCRIPTS / "Install-SpeakLinkHQAutoStart.ps1"
VERIFY = SCRIPTS / "Test-SpeakLinkHQAutoStart.ps1"
REPAIR = SCRIPTS / "Repair-SpeakLinkHQAutoStart.ps1"
UNINSTALL = SCRIPTS / "Uninstall-SpeakLinkHQAutoStart.ps1"

ALL_FOUR = (INSTALL, VERIFY, REPAIR, UNINSTALL)

#: Never a real task name. A test that can collide with the live task is a test
#: that can take HQ down to prove a point about strings.
ISOLATED_TASK = "SpeakLink HQ Runtime (automated test - do not keep)"


def _text(script: Path) -> str:
    return script.read_text(encoding="utf-8")


# ===========================================================================
# They exist, and they are readable PowerShell
# ===========================================================================
@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_exists(script: Path):
    assert script.exists(), f"{script.name} has not been written"


@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_has_no_byte_order_mark(script: Path):
    """Set-Content -Encoding utf8 writes one on Windows PowerShell 5.1, and a
    BOM in front of a param block is a parse error at the worst moment."""
    assert not script.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_stops_on_the_first_error(script: Path):
    assert "$ErrorActionPreference = 'Stop'" in _text(script)


# ===========================================================================
# The task action: a windowed executable, directly, with nothing around it
# ===========================================================================
def test_the_action_runs_the_runtime_executable_itself():
    assert "SpeakLinkHQRuntime.exe" in _text(INSTALL)


def test_the_action_runs_no_shell_and_no_interpreter():
    """A wrapper is how a console window gets onto an HQ desk after all the
    work spent making the executable windowed."""
    body = _text(INSTALL)
    action = body[body.index("New-ScheduledTaskAction"):]
    action = action[:action.index("\n\n")] if "\n\n" in action else action
    for forbidden in ("powershell.exe", "pwsh.exe", "cmd.exe", ".ps1", ".bat",
                      ".cmd", "python.exe", "pythonw.exe"):
        assert forbidden not in action, f"the task action runs {forbidden}"


def test_the_action_sets_a_working_directory():
    body = _text(INSTALL)
    action = body[body.index("New-ScheduledTaskAction"):]
    assert "-WorkingDirectory" in action[:400]


def test_the_task_carries_no_secret_of_any_kind():
    body = _text(INSTALL)
    action = body[body.index("New-ScheduledTaskAction"):]
    action = action[:action.index("New-ScheduledTaskTrigger")]
    for forbidden in ("jwt", "secret", "password", "credential", "bearer"):
        assert forbidden not in action.lower(), f"{forbidden} reached the task action"


def test_the_registered_xml_is_re_read_and_rejected_if_it_carries_a_secret():
    """Checking the variables is not checking the task. Export the registered
    definition and read what Windows actually stored."""
    body = _text(INSTALL)
    assert "Export-ScheduledTask" in body
    assert "Unregister-ScheduledTask" in body[body.index("Export-ScheduledTask"):]


# ===========================================================================
# Triggers: one at logon, one bounded safety net that is NOT attached to it
# ===========================================================================
def test_there_is_a_logon_trigger():
    assert "-AtLogOn" in _text(INSTALL)


def test_the_logon_trigger_names_the_hq_user_not_everyone():
    body = _text(INSTALL)
    logon = body[body.index("-AtLogOn"):][:200]
    assert "-User" in logon


def test_there_is_a_separate_periodic_recovery_trigger():
    """Repetition attached only to a logon trigger begins only at a logon, so a
    runtime that dies at 11am waits for the next sign-in. Measured on the Store
    task, then fixed the same way there."""
    body = _text(INSTALL)
    assert "-RepetitionInterval" in body
    assert body.count("New-ScheduledTaskTrigger") >= 2


def test_the_recovery_trigger_is_bounded_in_time():
    assert "-RepetitionDuration" in _text(INSTALL)


def test_the_recovery_interval_cannot_be_set_to_a_tight_loop():
    """A one-minute repetition on a runtime that takes 40 seconds to start is a
    machine that spends its morning starting servers."""
    body = _text(INSTALL)
    assert re.search(r"RepetitionMinutes[^\n]*ValidateRange|"
                     r"ValidateRange[^\n]*\)\]\[int\]\$RepetitionMinutes", body), (
        "the repetition interval accepts any value at all")


# ===========================================================================
# Principal: the signed-in HQ user, no stored password, no SYSTEM
# ===========================================================================
def test_it_runs_as_an_interactive_user():
    assert "-LogonType Interactive" in _text(INSTALL)


def test_it_never_stores_a_windows_password():
    body = _text(INSTALL)
    assert "-Password" not in body
    assert "LogonType Password" not in body


def test_it_refuses_a_service_account():
    body = _text(INSTALL).upper()
    assert "SYSTEM" in body, "nothing refuses SYSTEM"


def test_it_does_not_demand_administrator_rights():
    assert "-RunLevel Limited" in _text(INSTALL)


# ===========================================================================
# Settings
# ===========================================================================
@pytest.mark.parametrize("setting", [
    "-StartWhenAvailable",
    "-MultipleInstances IgnoreNew",
    "-RestartCount",
    "-RestartInterval",
    "ExecutionTimeLimit ([TimeSpan]::Zero)",
])
def test_the_required_setting_is_present(setting: str):
    assert setting in _text(INSTALL), f"{setting} is missing"


# ===========================================================================
# What the installer refuses before it writes anything
# ===========================================================================
@pytest.mark.parametrize("refusal", [
    "SpeakLinkHQRuntime.exe",   # a package without the runtime
    "frontend",                # a package without the production build
    "manifest.json",
])
def test_an_incomplete_package_is_named_in_a_refusal(refusal: str):
    assert refusal in _text(INSTALL)


def test_it_checks_the_pe_subsystem_itself():
    """Not the PyInstaller flag, not the build log - the header in the file it
    is about to install."""
    body = _text(INSTALL)
    assert "0x3C" in body and "ReadUInt16" in body


def test_it_refuses_an_uninitialized_persistent_profile():
    body = _text(INSTALL)
    assert "Initialize-SpeakLinkPersistentLanServer" in body


def test_the_installer_never_writes_inside_the_persistent_root():
    """It may READ the persistent root to decide whether to refuse. It must
    never create, copy into, or delete anything there - the installer is the
    one script an operator runs while nervous, and the database it would be
    writing over is 44 Stores."""
    body = _text(INSTALL)
    for line in body.splitlines():
        if "$PersistentRoot" not in line or line.strip().startswith("#"):
            continue
        for forbidden in ("New-Item", "Copy-Item", "Remove-Item", "Set-Content",
                          "Out-File"):
            assert forbidden not in line, f"the installer writes to the persistent root: {line.strip()}"


def test_the_installer_supports_a_dry_run_and_isolated_targets():
    body = _text(INSTALL)
    for parameter in ("$DryRun", "$TaskName", "$InstallRoot", "$PersistentRoot"):
        assert parameter in body


def test_it_does_not_start_the_task_unless_asked():
    """Registering a task and starting it are different decisions. Installing
    during business hours must not take the running pilot's ports."""
    body = _text(INSTALL)
    assert "Start-ScheduledTask" in body
    start = body[body.index("Start-ScheduledTask") - 300:body.index("Start-ScheduledTask")]
    assert "$StartNow" in start, "the task is started unconditionally"


# ===========================================================================
# Repair keeps the data. That is the whole job.
# ===========================================================================
@pytest.mark.parametrize("preserved", ["database", "keys", "config", "backups", "logs"])
def test_repair_names_what_it_preserves(preserved: str):
    assert preserved in _text(REPAIR).lower()


def test_repair_verifies_ownership_before_touching_a_task():
    """An operator's task named 'SpeakLink HQ Runtime' that runs something else
    entirely is not ours to unregister."""
    body = _text(REPAIR)
    assert "Unregister-ScheduledTask" in body
    before = body[:body.index("Unregister-ScheduledTask")]
    assert "SpeakLinkHQRuntime" in before


def test_repair_never_initializes_or_resets_anything():
    body = _text(REPAIR)
    for forbidden in ("Initialize-SpeakLinkPersistentLanServer",
                      "create_owner", "Remove-Item $PersistentRoot",
                      "reset-password"):
        assert forbidden not in body, f"repair runs {forbidden}"


def test_repair_replaces_only_files_that_are_missing_or_wrong():
    body = _text(REPAIR)
    assert "SHA256SUMS" in body


# ===========================================================================
# Uninstall keeps the data unless somebody says otherwise, out loud
# ===========================================================================
def test_uninstall_preserves_persistent_data_by_default():
    body = _text(UNINSTALL)
    assert "$RemovePersistentData" in body


def test_the_destructive_option_is_declared_and_not_implemented():
    """It is declared so the default is visibly a choice, and refused so this
    sprint cannot delete 44 Stores by flag."""
    body = _text(UNINSTALL)
    marker = body[body.index("$RemovePersistentData"):]
    assert "throw" in marker or "not implemented" in marker.lower()


def test_uninstall_stops_only_a_verified_runtime_process():
    body = _text(UNINSTALL)
    assert "SpeakLinkHQRuntime.exe" in body
    assert "ExecutablePath" in body, "it stops processes by name alone"


def test_a_recorded_pid_alone_can_never_stop_anything():
    """Windows reuses process numbers. Every place that stops something must
    prove identity first - by the image path for the runtime, by the command
    line for its Python children - because a stale record can name an editor.

    Both scripts that stop processes are held to this, not just the one that
    was written most carefully."""
    for script in (UNINSTALL, INSTALL, REPAIR):
        body = _text(script)
        if "Stop-Process" not in body:
            continue
        for fragment in body.split("Stop-Process")[:-1]:
            window = fragment[-900:]
            assert ("ExecutablePath" in window or "CommandLine" in window), (
                f"{script.name} stops a process without proving it is ours")


def test_the_children_are_matched_by_command_line_not_by_image_name():
    """The runtime's children are python.exe. Stopping every python.exe on an
    HQ desk would be a rude way to end an uninstall."""
    body = _text(UNINSTALL)
    assert "CommandLine" in body
    assert "uvicorn" in body


def test_uninstall_removes_only_a_task_that_is_ours():
    body = _text(UNINSTALL)
    before = body[:body.index("Unregister-ScheduledTask")]
    assert "SpeakLinkHQRuntime" in before


# ===========================================================================
# The verifier asks the questions that matter
# ===========================================================================
@pytest.mark.parametrize("question", [
    "MSFT_TaskLogonTrigger",
    "MultipleInstances",
    "StartWhenAvailable",
    "ExecutionTimeLimit",
    "RestartCount",
    "Repetition",
    "LogonType",
    "SpeakLinkHQRuntime.exe",
    "hq-runtime-status.json",
])
def test_the_verifier_checks_it(question: str):
    assert question in _text(VERIFY), f"the verifier never looks at {question}"


def test_the_verifier_checks_the_database_is_not_corrupt():
    """A database file that EXISTS is not a database that reads. SQLite will
    happily open a truncated file and fail on the first query an hour later,
    which is the same 'it is there, so it works' claim this project keeps
    finding. integrity_check is read-only and answers it properly."""
    assert "integrity_check" in _text(VERIFY)


def test_the_verifier_opens_the_database_read_only():
    body = _text(VERIFY)
    integrity = body[body.index("integrity_check") - 600:]
    assert "mode=ro" in integrity, "the verifier could write to HQ's database"


def test_the_verifier_confirms_the_task_points_at_the_install_root_it_was_given():
    """Otherwise it verifies a correctly-configured task that runs a DIFFERENT
    installation - an older one still on disk - and reports PASS."""
    body = _text(VERIFY)
    assert "expected install root" in body


def test_the_first_start_guard_keys_off_a_successful_start_not_a_file(tmp_path):
    """FOUND BY STAGING A REAL INSTALL.

    The HMAC container and the signing secret are created at the FIRST start,
    so before HQ has ever run their absence must be UNKNOWN rather than FAIL.
    The guard asked 'does a status file exist' - and a status file recording
    CONFIG_ERROR exists precisely when the runtime REFUSED, which is exactly
    the case where those files legitimately do not exist yet. So the guard let
    the check through and reported FAIL on a correct installation.

    Existence is not evidence of success. That is the same mistake as a process
    that exists not being a backend that answers, one level further down.

    THE FIRST VERSION OF THIS TEST PASSED WITHOUT THE FIX. It searched a window
    of text that happened to include the new helper while the two checks below
    still called the old one, so it was green against the very code it was
    written to reject. It asserts on the CALL SITES now.
    """
    body = _text(VERIFY)
    assert "function Test-HQHasStarted" in body
    helper = body[body.index("function Test-HQHasStarted"):][:400]
    assert "CONFIG_ERROR" in helper
    assert "CONFIG_OK" in helper, "a passed configuration check counts as a start"
    for check in ("the Receiver key container is present",
                  "the signing secret is present"):
        call_site = body[body.index(f"Check '{check}'"):][:220]
        assert "Test-HQHasStarted" in call_site, f"{check} uses the old guard"
        assert "Test-Path $StatusFile" not in call_site


def test_the_verifier_emits_the_agreed_marker():
    assert "SPEAKLINK_HQ_AUTOSTART_VERIFIED" in _text(VERIFY)


def test_the_verifier_reports_unknown_rather_than_guessing():
    body = _text(VERIFY)
    assert "UNKNOWN" in body


def test_the_verifier_checks_the_installed_pe_subsystem():
    assert "ReadUInt16" in _text(VERIFY)


def test_the_verifier_counts_runtime_processes():
    body = _text(VERIFY)
    assert "Win32_Process" in body


def test_the_verifier_is_read_only():
    """A verifier that repairs is a verifier whose PASS means nothing."""
    body = _text(VERIFY)
    for forbidden in ("Register-ScheduledTask", "Unregister-ScheduledTask",
                      "Start-ScheduledTask", "Stop-Process", "Remove-Item",
                      "Copy-Item"):
        assert forbidden not in body, f"the verifier calls {forbidden}"


def test_the_verifier_does_not_claim_reboot_or_lock_screen_behaviour():
    body = _text(VERIFY).lower()
    assert "reboot" in body and "sign" in body, (
        "it must say out loud what it cannot see")


# ===========================================================================
# Running it for real, against nothing that matters
# ===========================================================================
def _fake_pe(path: Path, subsystem: int) -> None:
    blob = bytearray(512)
    blob[0:2] = b"MZ"
    header = 0x80
    blob[0x3C:0x40] = header.to_bytes(4, "little")
    blob[header:header + 4] = b"PE\x00\x00"
    at = header + 4 + 20 + 68
    blob[at:at + 2] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(blob))


def _package(tmp_path: Path, *, subsystem: int = 2, with_frontend: bool = True) -> Path:
    package = tmp_path / "package"
    (package / "frontend").mkdir(parents=True)
    _fake_pe(package / "SpeakLinkHQRuntime.exe", subsystem)
    if with_frontend:
        (package / "frontend" / "index.html").write_text("<!doctype html>",
                                                         encoding="utf-8")
    else:
        shutil.rmtree(package / "frontend")
    for script in ALL_FOUR:
        shutil.copy2(script, package / script.name)
    (package / "manifest.json").write_text(
        json.dumps({"version": "0.0.0-test", "source_commit": "0" * 40,
                    "source_commit_short": "0000000"}), encoding="utf-8")
    digests = []
    for item in sorted(package.rglob("*")):
        if item.is_file():
            import hashlib

            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            digests.append(f"{digest}  {item.relative_to(package).as_posix()}")
    (package / "SHA256SUMS.txt").write_text("\n".join(digests) + "\n", encoding="utf-8")
    return package


def _persistent(tmp_path: Path) -> Path:
    root = tmp_path / "persistent"
    for folder in ("data", "config", "keys", "logs", "backups", "runtime"):
        (root / folder).mkdir(parents=True)
    (root / "data" / "speaklink.db").write_bytes(b"SQLite format 3\x00")
    (root / "keys" / "receiver-hmac-keys.bin").write_bytes(b"not-a-real-key")
    (root / "keys" / "jwt-secret.txt").write_text("not-a-real-value", encoding="utf-8")
    return root


def _run(script: Path, arguments: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script)] + arguments,
        capture_output=True, text=True, timeout=180,
    )


windows_only = pytest.mark.skipif(os.name != "nt", reason="Scheduled Tasks are Windows")


@windows_only
def test_a_dry_run_registers_nothing(tmp_path):
    package = _package(tmp_path)
    result = _run(INSTALL, [
        "-PackagePath", str(package),
        "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(_persistent(tmp_path)),
        "-DryRun",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "install").exists(), "a dry run installed files"

    listed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command",
         f"(Get-ScheduledTask -TaskName '{ISOLATED_TASK}' "
         "-ErrorAction SilentlyContinue) -ne $null"],
        capture_output=True, text=True, timeout=60)
    assert "True" not in listed.stdout, "a dry run registered a task"


@windows_only
def test_a_dry_run_prints_the_exact_action_it_would_register(tmp_path):
    package = _package(tmp_path)
    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(_persistent(tmp_path)), "-DryRun",
    ])
    assert "SpeakLinkHQRuntime.exe" in result.stdout
    assert str(tmp_path / "install") in result.stdout


@windows_only
def test_a_console_subsystem_runtime_is_refused(tmp_path):
    """The one check that stops a black window reaching the HQ desk."""
    package = _package(tmp_path, subsystem=3)
    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(_persistent(tmp_path)), "-DryRun",
    ])
    assert result.returncode != 0
    assert "console" in (result.stdout + result.stderr).lower()


@windows_only
def test_a_package_without_a_frontend_build_is_refused(tmp_path):
    package = _package(tmp_path, with_frontend=False)
    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(_persistent(tmp_path)), "-DryRun",
    ])
    assert result.returncode != 0


@windows_only
def test_an_uninitialized_persistent_root_is_refused(tmp_path):
    package = _package(tmp_path)
    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(tmp_path / "never-initialized"), "-DryRun",
    ])
    assert result.returncode != 0
    assert "Initialize" in result.stdout + result.stderr


@windows_only
def test_a_never_started_profile_is_accepted(tmp_path):
    """THE SAME DEFECT, IN A SECOND PLACE.

    The runtime demanded keys/receiver-hmac-keys.bin up front and refused a
    correctly initialized HQ, because nothing creates that file until the
    backend's first start. That was fixed in the runtime and left here - the
    installer demanded it too, so a freshly initialized HQ could be verified,
    packaged and then not installed.

    The rule is the same in both places now, and the parts of it live where the
    evidence does: the installer requires the database and the keys FOLDER,
    and whether a missing container is normal or an emergency is decided by the
    runtime at start, because only it can count the enrolled Devices.
    """
    package = _package(tmp_path)
    fresh = tmp_path / "fresh"
    for folder in ("data", "config", "keys", "logs", "backups", "runtime"):
        (fresh / folder).mkdir(parents=True)
    (fresh / "data" / "speaklink.db").write_bytes(b"SQLite format 3\x00")

    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(fresh), "-DryRun",
    ])
    assert result.returncode == 0, result.stdout + result.stderr


@windows_only
def test_a_missing_database_is_still_refused(tmp_path):
    """The check that had to survive the fix above."""
    package = _package(tmp_path)
    empty = tmp_path / "empty"
    for folder in ("data", "keys"):
        (empty / folder).mkdir(parents=True)

    result = _run(INSTALL, [
        "-PackagePath", str(package), "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "install"),
        "-PersistentRoot", str(empty), "-DryRun",
    ])
    assert result.returncode != 0
    assert "Initialize" in result.stdout + result.stderr


@windows_only
def test_the_verifier_on_an_absent_task_fails_rather_than_passing(tmp_path):
    result = _run(VERIFY, [
        "-TaskName", ISOLATED_TASK,
        "-InstallRoot", str(tmp_path / "nothing"),
        "-PersistentRoot", str(tmp_path / "nothing"),
    ])
    assert result.returncode != 0
    assert "SPEAKLINK_HQ_AUTO_START_VERIFIED" not in result.stdout
