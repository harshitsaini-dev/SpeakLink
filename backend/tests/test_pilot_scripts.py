"""Static safety checks for the operator-facing PowerShell pilot scripts.

These scripts are the only way an operator starts or stops a pilot, but nothing
tested them until a real defect made a Receiver silently fail to launch. They
are checked here as text, so the suite stays safe on any machine: nothing is
executed, no process is started and no device is opened.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIRECTORY = REPOSITORY_ROOT / "scripts"


def _scripts() -> list[Path]:
    found = sorted(SCRIPTS_DIRECTORY.glob("*.ps1"))
    assert found, "no PowerShell pilot scripts were found to check"
    return found


def _argument_list_blocks(text: str) -> list[tuple[int, str]]:
    """Return (line_number, inner_text) for every -ArgumentList @( ... )."""
    blocks = []
    for match in re.finditer(r"-ArgumentList\s*@\((.*?)\)\s*(?:`|\r?\n)", text, re.DOTALL):
        line_number = text.count("\n", 0, match.start()) + 1
        blocks.append((line_number, match.group(1)))
    return blocks


def _elements(inner: str) -> list[str]:
    return [part.strip() for part in inner.split(",") if part.strip()]


# ---------------------------------------------------------------------------
# Start-Process argument quoting
#
# Found while starting the Bluetooth amplifier Receiver. This repository lives
# at "...\HQ-Broadcast-Full (1)", and PowerShell joins -ArgumentList elements
# with spaces WITHOUT quoting them. The unquoted script path was therefore split
# at the space and python reported:
#     can't open file '...\HQ-Broadcast-Full': [Errno 2] No such file
# The Receiver process died instantly, so it never connected and never reported
# DEVICE_ERROR either - it simply was not there. Any Windows path containing a
# space breaks this, which includes "C:\Program Files" and most user folders.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("script", _scripts(), ids=lambda p: p.name)
def test_start_process_arguments_that_interpolate_a_variable_are_quoted(script: Path):
    text = script.read_text(encoding="utf-8")
    offenders = []
    for line_number, inner in _argument_list_blocks(text):
        for element in _elements(inner):
            if "$" not in element:
                continue  # a plain literal such as 'run' or '--url'
            # Safe form: "`"$path`"" - embedded quotes survive the join.
            if element.startswith('"`"') and element.endswith('`""'):
                continue
            offenders.append(f"{script.name}:{line_number} -> {element}")
    assert not offenders, (
        "PowerShell joins -ArgumentList elements with spaces and does NOT quote "
        "them, so any interpolated value containing a space silently truncates "
        "the argument. Wrap each one as \"`\"$value`\"\". Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_the_two_receiver_launchers_pass_a_quoted_tool_path():
    """Regression guard naming the exact defect that broke the amplifier run."""
    for name in (
        "Start-SpeakLinkAudioReceiverPilot.ps1",
        "Start-SpeakLinkWindowsAudioReceiverPilot.ps1",
    ):
        script = SCRIPTS_DIRECTORY / name
        assert script.exists(), f"{name} is missing"
        text = script.read_text(encoding="utf-8")
        assert '"`"$tool`""' in text, (
            f"{name} must pass the tool path quoted, or a repository path "
            "containing a space truncates it and python cannot open the file"
        )


def test_scripts_never_interpolate_a_credential_into_an_argument_list():
    """Credentials travel through the child environment, never the command line,
    where any other user could read them with Get-CimInstance Win32_Process."""
    forbidden = ("RECEIVER_TOKEN", "ADMIN_PASSWORD", "JWT_SECRET", "Authorization")
    offenders = []
    for script in _scripts():
        text = script.read_text(encoding="utf-8")
        for line_number, inner in _argument_list_blocks(text):
            for secret in forbidden:
                if secret.lower() in inner.lower():
                    offenders.append(f"{script.name}:{line_number} mentions {secret}")
    assert not offenders, "credentials must never reach a command line: " + "; ".join(offenders)
