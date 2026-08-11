"""Manage-SpeakLinkStoreReceiverTask.ps1: start/stop/status for one named task,
never anything else.

WHY A SEPARATE SCRIPT INSTEAD OF AN INLINE POWERSHELL -COMMAND STRING

StoreSetup's Rerun screen needs to start, stop and query the Store Receiver
task from Python. Building a `-Command "Start-ScheduledTask -TaskName $x"`
string in Python and shelling it out is exactly the injection shape this
project refuses everywhere else - the task name would have to be interpolated
into a string a shell parses. A dedicated, tested script with a real parameter
takes the name as data, never as text glued into a command.

WHY IT VERIFIES OWNERSHIP BEFORE ACTING

A task name is not proof of identity - an operator could have another
scheduled task by the same name, or `-TaskName` could be mistyped and match
something else on a shared machine. Start and Stop both refuse unless the
task's own action names SpeakLinkReceiverBackground.exe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "scripts" / "Manage-SpeakLinkStoreReceiverTask.ps1"

windows_only = pytest.mark.skipif(os.name != "nt", reason="Scheduled Tasks are Windows")


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_exists():
    assert SCRIPT.exists()


def test_the_script_has_no_byte_order_mark():
    assert not SCRIPT.read_bytes().startswith(b"\xef\xbb\xbf")


def test_the_script_stops_on_the_first_error():
    assert "$ErrorActionPreference = 'Stop'" in _text()


def test_it_verifies_ownership_before_starting_or_stopping():
    body = _text()
    assert "SpeakLinkReceiverBackground.exe" in body
    for cmdlet in ("Start-ScheduledTask", "Stop-ScheduledTask"):
        assert cmdlet in body


def test_it_never_builds_a_shell_command_string():
    body = _text()
    assert "Invoke-Expression" not in body
    assert "iex " not in body.lower()


def test_it_supports_status_start_and_stop_actions():
    body = _text()
    for action in ("Status", "Start", "Stop"):
        assert f"'{action}'" in body or f'"{action}"' in body


ISOLATED_TASK = "SpeakLink Store Receiver (automated test - do not keep)"


def _fake_pe(path: Path, subsystem: int) -> None:
    blob = bytearray(512)
    blob[0:2] = b"MZ"
    header = 0x80
    blob[0x3C:0x40] = header.to_bytes(4, "little")
    blob[header:header + 4] = b"PE\x00\x00"
    at = header + 4 + 20 + 68
    blob[at:at + 2] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(blob))


def _run(arguments: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT)] + arguments,
        capture_output=True, text=True, timeout=60,
    )


@windows_only
def test_status_on_an_absent_task_reports_absent_not_a_crash(tmp_path):
    result = _run(["-TaskName", ISOLATED_TASK, "-Action", "Status"])
    assert result.returncode == 0
    assert "NOT_REGISTERED" in result.stdout


@windows_only
def test_start_refuses_a_task_that_is_not_ours(tmp_path):
    exe = tmp_path / "notepad-lookalike.exe"
    exe.write_bytes(b"not really an exe")
    task_name = ISOLATED_TASK
    try:
        subprocess.run([
            "powershell.exe", "-NoProfile", "-Command",
            f"$a = New-ScheduledTaskAction -Execute '{exe}'; "
            f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(5); "
            f"Register-ScheduledTask -TaskName '{task_name}' -Action $a -Trigger $t -Force | Out-Null",
        ], capture_output=True, text=True, timeout=30)

        result = _run(["-TaskName", task_name, "-Action", "Start"])
        assert result.returncode != 0
        assert "not ours" in (result.stdout + result.stderr).lower() or \
               "SpeakLinkReceiverBackground" in (result.stdout + result.stderr)
    finally:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false "
                        "-ErrorAction SilentlyContinue"],
                       capture_output=True, text=True, timeout=30)


@windows_only
def test_stop_refuses_a_task_that_is_not_ours(tmp_path):
    exe = tmp_path / "notepad-lookalike.exe"
    exe.write_bytes(b"not really an exe")
    task_name = ISOLATED_TASK
    try:
        subprocess.run([
            "powershell.exe", "-NoProfile", "-Command",
            f"$a = New-ScheduledTaskAction -Execute '{exe}'; "
            f"$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(5); "
            f"Register-ScheduledTask -TaskName '{task_name}' -Action $a -Trigger $t -Force | Out-Null",
        ], capture_output=True, text=True, timeout=30)

        result = _run(["-TaskName", task_name, "-Action", "Stop"])
        assert result.returncode != 0
    finally:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{task_name}' -Confirm:$false "
                        "-ErrorAction SilentlyContinue"],
                       capture_output=True, text=True, timeout=30)
