"""Static safety checks for the operator-facing PowerShell pilot scripts.

These scripts are the only way an operator starts or stops a pilot, but nothing
tested them until a real defect made a Receiver silently fail to launch. They
are checked here as text, so the suite stays safe on any machine: nothing is
executed, no process is started and no device is opened.
"""

from __future__ import annotations

from pathlib import Path
import os
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
# at "a path containing a space", and PowerShell joins -ArgumentList elements
# with spaces WITHOUT quoting them. The unquoted script path was therefore split
# at the space and python reported:
#     can't open file 'a path containing a space': [Errno 2] No such file
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


# ---------------------------------------------------------------------------
# Stopping a pilot
#
# Found while shutting down the Bluetooth amplifier live test.
# Stop-SpeakLinkLocalPilot.ps1 stopped only the PID it recorded at launch - the
# "cmd.exe /c yarn start" wrapper - then printed "Frontend : stopped." The dev
# server that actually held port 3000 was five levels below it and survived.
# The behavioural half of this is backend/tests/test_process_tree.py; these are
# the text guards that keep the scripts wired to it.
# ---------------------------------------------------------------------------
STOP_SCRIPTS = (
    "Stop-SpeakLinkLocalPilot.ps1",
    "Stop-SpeakLinkAudioReceiverPilot.ps1",
    "Stop-SpeakLinkWindowsAudioReceiverPilot.ps1",
)


@pytest.mark.parametrize("name", STOP_SCRIPTS)
def test_stop_scripts_use_the_tested_process_tree_planner(name: str):
    script = SCRIPTS_DIRECTORY / name
    assert script.exists(), f"{name} is missing"
    text = script.read_text(encoding="utf-8")
    assert "Stop-SpeakLinkProcessTree" in text, (
        f"{name} must stop the whole owned tree through the planner that "
        "backend/tests/test_process_tree.py covers, not a single recorded PID"
    )
    assert "SpeakLinkProcessTree.ps1" in text, f"{name} does not dot-source the shared helper"


@pytest.mark.parametrize("name", STOP_SCRIPTS)
def test_stop_scripts_never_stop_a_process_by_name(name: str):
    """Stop-Process -Name node would take out every Node process on the machine."""
    text = (SCRIPTS_DIRECTORY / name).read_text(encoding="utf-8")
    assert not re.search(r"Stop-Process\s+(-\w+\s+)*-Name\b", text), (
        f"{name} stops processes by name; that cannot distinguish this pilot "
        "from the operator's unrelated work"
    )
    assert not re.search(r"Get-Process\s+-Name\s+['\"]?(node|python|ffmpeg)", text, re.IGNORECASE), (
        f"{name} selects processes by name rather than by verified ownership"
    )


@pytest.mark.parametrize("name", STOP_SCRIPTS)
def test_stop_scripts_do_not_claim_success_unconditionally(name: str):
    """The old script printed "stopped." on the line after force-terminating,
    with no check that anything had actually gone."""
    text = (SCRIPTS_DIRECTORY / name).read_text(encoding="utf-8")
    assert re.search(r"if\s*\(\s*\$\w*[Ss]topped", text) or "$allClean" in text, (
        f"{name} must gate its success message on the verified stop result"
    )
    assert "exit 1" in text, (
        f"{name} must exit non-zero when survivors remain, so an operator or a "
        "caller cannot mistake a partial stop for a clean one"
    )


def test_the_local_pilot_stop_script_verifies_both_ports_are_released():
    """A released port is the independent check that nothing survived."""
    text = (SCRIPTS_DIRECTORY / "Stop-SpeakLinkLocalPilot.ps1").read_text(encoding="utf-8")
    assert "Test-SpeakLinkPortReleased -Port 8000" in text, "port 8000 is never verified"
    assert "Test-SpeakLinkPortReleased -Port 3000" in text, "port 3000 is never verified"


def test_the_shared_helper_removes_a_pid_file_only_after_the_tree_is_gone():
    text = (SCRIPTS_DIRECTORY / "SpeakLinkProcessTree.ps1").read_text(encoding="utf-8")
    survivors = text.index("$survivors = @($ordered")
    removal = text.index("Remove-Item $PidFile -Force", survivors)
    claim = text.index("stopped (whole tree verified gone", survivors)
    assert survivors < removal < claim, (
        "the PID file must be removed only after the survivor check passes, or "
        "surviving processes stop being traceable"
    )


def _code_lines(text: str) -> list[tuple[int, str]]:
    """(line_number, code) with <# block #> and # line comments removed.

    Comments matter here: the helper's own documentation talks about
    Write-Output, and a scan that cannot tell prose from code would flag it.
    """
    without_blocks = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL)
    lines = []
    for number, line in enumerate(without_blocks.splitlines(), start=1):
        code = re.sub(r"#.*$", "", line)
        if code.strip():
            lines.append((number, code))
    return lines


def _function_body(text: str, name: str) -> str:
    """The source of one PowerShell function, by brace balance."""
    start = text.index(f"function {name}")
    depth, index = 0, text.index("{", start)
    opening = index
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[opening : index + 1]
        index += 1
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_shared_helper_never_uses_write_output_inside_its_functions():
    """PowerShell folds every uncaptured value a function emits into its return
    value. A single Write-Output inside Stop-SpeakLinkProcessTree turns
    "return $false" into @("message", $false) - a non-empty array, and therefore
    always truthy - which silently defeats the survivor check the helper exists
    to enforce. Caught by running the real stop path: Test-SpeakLinkPortReleased
    reported success for a port that was still bound."""
    text = (SCRIPTS_DIRECTORY / "SpeakLinkProcessTree.ps1").read_text(encoding="utf-8")
    offenders = [
        f"line {number}: {code.strip()}"
        for number, code in _code_lines(text)
        if "Write-Output" in code
    ]
    assert not offenders, (
        "these functions return a value, so their progress messages must use "
        "Write-Host or they corrupt that value:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("function", ["Stop-SpeakLinkProcessTree", "Test-SpeakLinkPortReleased"])
def test_the_boolean_helpers_return_a_bare_boolean_from_every_exit(function: str):
    """A caller writes `if ($stopped)`, so anything else returned is a trap."""
    text = (SCRIPTS_DIRECTORY / "SpeakLinkProcessTree.ps1").read_text(encoding="utf-8")
    body = _function_body(text, function)
    returns = re.findall(r"^\s*return\s+(.+)$", body, re.MULTILINE)
    assert returns, f"{function} has no return statements; its shape changed"
    for value in returns:
        assert value.strip() in ("$true", "$false"), (
            f"{function} returns {value.strip()!r}, which is not a bare boolean"
        )


def test_start_scripts_record_the_creation_time_for_pid_reuse_detection():
    """Windows recycles PIDs. A bare number cannot prove it is still our process."""
    for name in (
        "Start-SpeakLinkLocalPilot.ps1",
        "Start-SpeakLinkAudioReceiverPilot.ps1",
        "Start-SpeakLinkWindowsAudioReceiverPilot.ps1",
    ):
        text = (SCRIPTS_DIRECTORY / name).read_text(encoding="utf-8")
        assert "Write-SpeakLinkPidFile" in text, (
            f"{name} still writes a bare PID; the stop script then cannot tell a "
            "recycled PID from the real one"
        )


# ===========================================================================
# A native command's own INFO/warning logging must not abort the script
# ===========================================================================
def test_the_pyinstaller_build_call_does_not_treat_its_own_stderr_as_fatal():
    """FOUND BY ACTUALLY RUNNING Build-SpeakLinkReceiver.ps1.

    PyInstaller writes its own progress ("114 INFO: PyInstaller: 6.18.0...")
    to STDERR. Under $ErrorActionPreference = 'Stop' - which every script in
    this repository sets - PowerShell 5.1 treats ANY stderr text from a native
    command as a terminating NativeCommandError, even when the process exits 0
    a moment later. The build failed on its very first line of PyInstaller
    output, before PyInstaller had done anything wrong at all.

    Every other script in this repository that calls a chatty native command
    already works around this the same way: flip ErrorActionPreference to
    Continue for exactly that one call, then check $LASTEXITCODE by hand. This
    is that same guard, now required of the PyInstaller call too.
    """
    script = (Path(__file__).resolve().parents[2] / "scripts" /
              "Build-SpeakLinkReceiver.ps1").read_text(encoding="utf-8")
    call_line = next(line for line in script.splitlines() if "PyInstaller $spec" in line)
    index = script.index(call_line)
    window = script[max(0, index - 400):index]
    assert "'Continue'" in window, (
        "the PyInstaller call runs under ErrorActionPreference=Stop and will "
        "abort on PyInstaller's own INFO/warning output")


# ===========================================================================
# A parameter must never become a command of its own
# ===========================================================================
def test_no_script_has_a_command_whose_name_is_a_parameter():
    """FOUND BY THE SECURITY AUDIT, CONFIRMED WITH THE POWERSHELL PARSER.

    Start-SpeakLinkPersistentLanServer.ps1 had a backtick line-continuation on
    one line and a '#' comment on the next. The escaped newline joins them into
    ONE logical line, so the comment swallowed everything after it - and the
    parser reported `Start-Process` with three elements (`-FilePath $python`)
    plus a SEPARATE command literally named `-ArgumentList`.

    Zero parse errors, which is exactly why it survived: the file was valid
    PowerShell that did something completely different from what it reads like.
    At runtime it launched a bare interactive Python REPL in a visible window -
    no uvicorn, no --host, no --workers 1, no log redirection - on the documented
    operator path for starting the persistent HQ server.

    The comment that broke it was explaining a PREVIOUS launcher bug about
    quoting -ArgumentList values.

    This guard is structural rather than textual: any CommandAst whose command
    name begins with '-' is an orphaned parameter, whatever caused it.
    """
    import json
    import subprocess

    probe = r'''
$results = @()
foreach ($file in Get-ChildItem -LiteralPath $env:SPEAKLINK_SCRIPTS_DIR -Filter *.ps1) {
  $errors = $null; $tokens = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
  if ($null -eq $ast) { continue }
  $cmds = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)
  foreach ($c in $cmds) {
    $name = $c.GetCommandName()
    if ($name -and $name.StartsWith('-')) {
      $results += [pscustomobject]@{ file = $file.Name; line = $c.Extent.StartLineNumber; name = $name }
    }
  }
}
ConvertTo-Json -Compress -InputObject @($results)
'''
    # The directory travels in the ENVIRONMENT, not as a trailing argument to
    # -Command: PowerShell tries to execute a trailing positional as a command,
    # and a path with spaces in it then becomes a CommandNotFoundException. It is
    # also the injection-free way to hand a path to a shell.
    environment = dict(os.environ, SPEAKLINK_SCRIPTS_DIR=str(SCRIPTS_DIRECTORY))
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", probe],
        capture_output=True, text=True, timeout=240, env=environment,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    orphans = json.loads(completed.stdout.strip() or "[]")
    assert orphans == [], (
        "a parameter was parsed as its own command - almost certainly a '#' comment "
        f"immediately after a backtick continuation: {orphans}")
