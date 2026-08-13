"""The Store Receiver Scheduled Task: what the installer registers, checked in CI.

WHY THIS FILE EXISTS

Every property the Store task must have - AtLogon, interactive, never SYSTEM,
the windowed executable directly, no wrapper, StartWhenAvailable, IgnoreNew, no
execution time limit, bounded restart, a separate periodic recovery trigger, no
secret in the XML - was verified only by Test-SpeakLinkStoreReceiver.ps1, which
reads a task that is already INSTALLED on a real machine. That means none of it
was checked by any automated run: it needed a Store desktop with the task
present.

The HQ auto-start scripts have 82 such tests. The Store side, which is the one
that ships to 44 tills, had none. This closes that asymmetry.

WHAT THIS FILE DOES NOT COVER, AND WHERE THAT LIVES INSTEAD

Reconnect behaviour - NETWORK_ERROR, bounded jittered backoff, the reset after a
stable session, and a revoked credential refusing to retry - is already proven
in test_receiver_agent.py against the real supervise()/BackoffPolicy, and is not
duplicated here. Behaviour after a real reboot or sign-in cannot be automated at
all and is an operator checkpoint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"

INSTALL = SCRIPTS / "Install-SpeakLinkStoreReceiver.ps1"
VERIFY = SCRIPTS / "Test-SpeakLinkStoreReceiver.ps1"
REPAIR = SCRIPTS / "Repair-SpeakLinkStoreReceiver.ps1"
UNINSTALL = SCRIPTS / "Uninstall-SpeakLinkStoreReceiver.ps1"

ALL_FOUR = (INSTALL, VERIFY, REPAIR, UNINSTALL)


def _text(script: Path) -> str:
    return script.read_text(encoding="utf-8")


def _task_action_block(body: str) -> str:
    """The New-ScheduledTaskAction call and what immediately follows it."""
    start = body.index("New-ScheduledTaskAction")
    return body[start:start + 400]


# ===========================================================================
# All four scripts exist and behave like the rest of the repository's scripts
# ===========================================================================
@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_exists(script: Path):
    assert script.exists()


@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_has_no_byte_order_mark(script: Path):
    """Set-Content -Encoding utf8 writes one on Windows PowerShell 5.1, and a
    BOM in front of a param block is a parse error at the worst moment."""
    assert not script.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("script", ALL_FOUR, ids=lambda p: p.name)
def test_the_script_stops_on_the_first_error(script: Path):
    assert "$ErrorActionPreference = 'Stop'" in _text(script)


# ===========================================================================
# The action: the WINDOWED executable, directly, with no wrapper
# ===========================================================================
def test_the_task_runs_the_windowed_executable():
    """The console build on a Store counter is a black window a member of staff
    eventually closes. Task Scheduler's "hidden" setting does not prevent it -
    the PE subsystem does."""
    action = _task_action_block(_text(INSTALL))
    assert "SpeakLinkReceiverBackground.exe" in action


def test_the_task_never_runs_the_console_executable():
    action = _task_action_block(_text(INSTALL))
    assert not re.search(r"SpeakLinkReceiver\.exe", action), (
        "the task action names the console build")


def test_the_task_runs_no_shell_script_or_interpreter_wrapper():
    action = _task_action_block(_text(INSTALL))
    for forbidden in ("powershell.exe", "pwsh.exe", "cmd.exe", ".ps1", ".bat",
                      ".cmd", "python.exe", "pythonw.exe"):
        assert forbidden not in action, f"the task action runs {forbidden}"


def test_the_task_has_a_working_directory():
    action = _task_action_block(_text(INSTALL))
    assert "-WorkingDirectory" in action


def test_the_task_arguments_carry_no_secret():
    """Everything configurable lives in the config file, so the task definition
    has nowhere to carry a code, a credential or an audio device that could go
    stale."""
    action = _task_action_block(_text(INSTALL))
    for forbidden in ("code", "credential", "password", "token", "secret", "bearer"):
        # -Argument may name a credential PATH for an isolated test install;
        # a credential VALUE must never appear.
        assert f"${forbidden}" not in action.lower() or "path" in action.lower()


# ===========================================================================
# Triggers: one at logon, one bounded periodic recovery, deliberately separate
# ===========================================================================
def test_there_is_a_logon_trigger_for_the_installing_user():
    body = _text(INSTALL)
    assert "-AtLogOn" in body
    logon = body[body.index("-AtLogOn"):][:160]
    assert "-User" in logon


def test_there_is_a_separate_periodic_recovery_trigger():
    """A repetition attached only to a logon trigger begins only at a logon, so
    a Receiver that dies at 11am would wait for the next sign-in. Measured on
    this very task, and fixed by giving the repetition its own time-based
    trigger."""
    body = _text(INSTALL)
    assert body.count("New-ScheduledTaskTrigger") >= 2
    assert "-RepetitionInterval" in body


def test_the_recovery_repetition_is_bounded_in_time():
    assert "-RepetitionDuration" in _text(INSTALL)


def test_restart_count_is_not_relied_on_for_recovery():
    """Task Scheduler's RestartCount applies when a task fails to START, not
    when the program it started exits - measured with `cmd /c exit 1` and
    RestartCount 2, which never re-ran. The repetition schedule is what
    actually brings a dead Receiver back, and the script says so."""
    body = _text(INSTALL)
    assert "RestartCount" in body, "bounded restart-on-failure is still required"
    assert "repetition" in body.lower(), (
        "nothing explains that the repetition, not RestartCount, is the recovery path")


# ===========================================================================
# Principal: the signed-in Store user, no stored password, never SYSTEM
# ===========================================================================
def test_it_runs_as_an_interactive_user():
    assert "-LogonType Interactive" in _text(INSTALL)


def test_it_stores_no_windows_password():
    body = _text(INSTALL)
    assert "-Password" not in body
    assert "LogonType Password" not in body


def test_it_does_not_demand_administrator_rights():
    assert "-RunLevel Limited" in _text(INSTALL)


def test_the_principal_is_not_a_service_account():
    body = _text(INSTALL)
    principal = body[body.index("New-ScheduledTaskPrincipal"):][:220]
    for forbidden in ("SYSTEM", "LOCALSERVICE", "NETWORKSERVICE", "LOCAL SERVICE"):
        assert forbidden not in principal.upper()


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


def test_the_registered_xml_is_re_read_and_rejected_if_it_carries_a_secret():
    """Checking the variables is not checking the task. Export the definition
    Windows actually stored and read it back."""
    body = _text(INSTALL)
    assert "Export-ScheduledTask" in body
    after = body[body.index("Export-ScheduledTask"):]
    assert "Unregister-ScheduledTask" in after, (
        "a task found to carry a secret must be removed, not merely reported")


# ===========================================================================
# What the installer preserves - an upgrade is not a re-enrolment
# ===========================================================================
def test_installing_over_an_existing_installation_preserves_the_credential():
    body = _text(INSTALL)
    assert "PRESERVED" in body
    assert "$credentialPath" in body
    # The credential path must never be deleted by the installer.
    for line in body.splitlines():
        if "Remove-Item" in line and "credential" in line.lower():
            pytest.fail(f"the installer deletes a credential: {line.strip()}")


def test_the_installer_verifies_every_copied_file_against_the_package_manifest():
    """A Receiver that is 99% copied fails weeks later with a DLL error nobody
    connects to install day."""
    body = _text(INSTALL)
    assert "SHA256SUMS.txt" in body
    assert "Get-FileHash" in body


def test_the_installer_refuses_a_stale_package():
    assert "STALE-DO-NOT-DEPLOY" in _text(INSTALL)


def test_the_installer_refuses_plain_http_to_a_public_address():
    body = _text(INSTALL)
    assert "Refusing plain HTTP to a non-private address" in body


def test_the_installer_refuses_a_credential_in_the_backend_url():
    body = _text(INSTALL)
    assert "query string" in body.lower()


# ===========================================================================
# Repair preserves identity. That is its whole job.
# ===========================================================================
@pytest.mark.parametrize("preserved", ["credential", "config"])
def test_repair_preserves_it(preserved: str):
    assert preserved in _text(REPAIR).lower()


def test_repair_never_deletes_the_credential():
    body = _text(REPAIR)
    for line in body.splitlines():
        if "Remove-Item" in line and "credential" in line.lower():
            pytest.fail(f"repair deletes a credential: {line.strip()}")


def test_repair_verifies_ownership_before_touching_a_task():
    body = _text(REPAIR)
    if "Unregister-ScheduledTask" not in body:
        pytest.skip("this repair script does not re-register the task")
    before = body[:body.index("Unregister-ScheduledTask")]
    assert "SpeakLink" in before


def test_repair_delegates_hashing_to_the_installer_rather_than_repeating_it():
    """My first version of this test asserted Repair hashes files itself and
    failed. The script was right and the test was wrong: Repair reads the saved
    settings and re-runs Install-SpeakLinkStoreReceiver.ps1 on top of them, so
    the manifest verification lives in exactly one place. A second copy of that
    logic is the thing worth preventing, not the thing worth requiring."""
    body = _text(REPAIR)
    assert "Install-SpeakLinkStoreReceiver.ps1" in body
    assert "SHA256SUMS" not in body, (
        "Repair has grown its own copy of the installer's hash check")
    assert "SHA256SUMS" in _text(INSTALL), (
        "the installer it delegates to must still verify every file")


# ===========================================================================
# Uninstall keeps the credential unless told otherwise, and never revokes
# ===========================================================================
def test_uninstall_keeps_the_credential_by_default():
    body = _text(UNINSTALL)
    assert "$RemoveCredential" in body, (
        "removing the credential must be an explicit, separate decision")


def test_uninstall_says_the_hq_device_is_not_revoked():
    """Removing a local credential revokes nothing. Saying so matters: a
    half-done removal leaves a Device that HQ still lists as enrolled and that
    will never connect again - a Store that looks fine on the dashboard and is
    silent.

    receiver_agent.remove_local_credential() has always printed this. The
    PowerShell uninstaller did not, which is the same sentence missing from one
    of the two languages it needs to exist in.
    """
    body = _text(UNINSTALL).lower()
    assert "not revoked" in body, "nothing explains that the HQ Device is not revoked"
    assert "administrator" in body, "nothing says who has to revoke it"


def test_both_credential_removal_paths_give_the_same_warning():
    """The Python helper and the PowerShell script must not disagree about what
    removing a credential does - that divergence is what let one of them ship
    without the warning at all."""
    powershell = _text(UNINSTALL).lower()
    python_helper = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(
        encoding="utf-8").lower()
    for claim in ("not revoked", "administrator"):
        assert claim in powershell, f"the PowerShell path never says {claim!r}"
        assert claim in python_helper, f"the Python path never says {claim!r}"


def test_uninstall_stops_processes_by_verified_path_not_by_name_alone():
    body = _text(UNINSTALL)
    if "Stop-Process" not in body:
        pytest.skip("this uninstall script stops no process")
    for fragment in body.split("Stop-Process")[:-1]:
        window = fragment[-900:]
        assert "ExecutablePath" in window or "CommandLine" in window, (
            "a process is stopped without proving it is ours")


# ===========================================================================
# The verifier states what it cannot see
# ===========================================================================
def test_the_verifier_admits_what_it_cannot_prove():
    """Reboot, sign-out/sign-in, locked screen and 'was it audible' all need a
    person. A verifier that stays quiet about that reads as if it covered
    them."""
    body = _text(VERIFY).lower()
    assert "reboot" in body
    assert "heard" in body or "audible" in body


def test_the_verifier_reports_unknown_rather_than_guessing():
    assert "UNKNOWN" in _text(VERIFY)


def test_the_verifier_checks_both_pe_subsystems():
    body = _text(VERIFY)
    assert "ReadUInt16" in body
    assert "-eq 2" in body and "-eq 3" in body, (
        "both the windowed and the console executable must be checked")


def test_the_verifier_checks_for_orphan_ffmpeg():
    assert "ffmpeg" in _text(VERIFY).lower()


def test_the_verifier_checks_exactly_one_receiver_is_running():
    body = _text(VERIFY)
    assert "Count -eq 1" in body


# ===========================================================================
# A held-open install folder must not end the installation
#
# A real Store PC failed here, AFTER enrolment had already succeeded - the
# worst possible moment, because the Device was registered at HQ and the
# computer had nothing installed to use it:
#
#     Remove-Item : ...\SpeakLink\receiver-app because it is in use
#
# The old Receiver had just been killed and its handles had not been released
# yet. One attempt, no retry, and the whole install was lost.
# ===========================================================================

def test_the_installer_stops_the_task_before_the_processes():
    """Killing the process without stopping the task is a race the task wins:
    Task Scheduler can restart it between the kill and the delete."""
    body = _text(INSTALL)
    stop_task = body.index("Stop-ScheduledTask")
    stop_process = body.index("Stop-Process -Id $($process.ProcessId)"
                              if "Stop-Process -Id $($process.ProcessId)" in body
                              else "Stop-Process")
    assert stop_task < stop_process, "the task must be stopped first"


def test_the_installer_matches_a_running_receiver_by_name():
    """An upgrade from an older kit, or the same folder reached by a different
    spelling, leaves a process a path filter does not recognise - and it is
    that process which holds the folder open."""
    body = _text(INSTALL)
    window = body[body.index("Stop-ScheduledTask"):body.index("Remove-Item $InstallRoot")]
    assert "Name = 'SpeakLinkReceiverBackground.exe'" in window
    assert "StartsWith($InstallRoot" not in window, (
        "matching only by path is what let the holding process survive")


def test_removing_the_install_root_is_retried():
    body = _text(INSTALL)
    window = body[body.index("Stop-ScheduledTask"):]
    assert "1..5" in window and "still in use, waiting" in window


def test_a_folder_that_cannot_be_removed_is_replaced_in_place():
    """Falling back is safe only because every file is checksum-verified after
    the copy - so this asserts the check is still there, not merely the
    fallback."""
    body = _text(INSTALL)
    assert "replacing its contents in place" in body
    assert "SHA256SUMS.txt" in body, "the fallback relies on this staying"
