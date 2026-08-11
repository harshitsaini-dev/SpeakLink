"""Why a black window appeared on the Store counter when a broadcast started.

MEASURED, NOT ASSUMED

``SpeakLinkReceiverBackground.exe`` is a GUI-subsystem process, so it has no
console. ``ffmpeg.exe`` is a console-subsystem application. When a parent with
no console starts a console child and does not ask for ``CREATE_NO_WINDOW``,
Windows gives that child a **brand-new console** - and a new console is a new
visible window.

Measured on this machine with ``pythonw.exe`` as the parent, which is GUI
subsystem exactly like the background Receiver::

    parent_has_console            : False
    child, today's flags (none)   : has_console=True,  console_hwnd=721134
    child, with CREATE_NO_WINDOW  : has_console=False, console_hwnd=0

That is the whole bug. It appears when a broadcast starts because that is when
``FfmpegDecoder.start`` runs.

WHAT THESE TESTS DO AND DO NOT PROVE

They prove every FFmpeg spawn path asks for a hidden child, on Windows only,
without ``shell=True`` and without a PowerShell or cmd wrapper. They do **not**
prove a Store desktop shows no window - that needs a real desktop session and a
person watching it, and the manual acceptance test exists for exactly that.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from tools.audio_receiver_pilot import (  # noqa: E402
    FfmpegDecoder,
    hidden_child_process_options,
    opus_webm_decode_supported,
)


ON_WINDOWS = os.name == "nt"


# ===========================================================================
# The helper
# ===========================================================================
@pytest.mark.skipif(not ON_WINDOWS, reason="Windows console behaviour")
def test_the_helper_asks_for_no_window_on_windows():
    options = hidden_child_process_options()
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW


@pytest.mark.skipif(not ON_WINDOWS, reason="Windows console behaviour")
def test_the_helper_also_hides_the_window_through_startupinfo():
    """Belt and braces, and cheap.

    CREATE_NO_WINDOW alone is enough for a console child. STARTUPINFO with
    SW_HIDE covers a child that creates a window of its own for another reason,
    and costs nothing when it does not.
    """
    startup = hidden_child_process_options()["startupinfo"]
    assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == subprocess.SW_HIDE


@pytest.mark.skipif(ON_WINDOWS, reason="the non-Windows branch")
def test_the_helper_adds_nothing_anywhere_else():
    """No Windows-only constant may reach a Popen call on another platform -
    they do not exist there and the call would raise."""
    assert hidden_child_process_options() == {}


def test_the_helper_never_asks_for_a_shell():
    assert "shell" not in hidden_child_process_options()


def test_the_helper_never_asks_for_a_new_console():
    options = hidden_child_process_options()
    flags = options.get("creationflags", 0)
    if ON_WINDOWS:
        assert not flags & subprocess.CREATE_NEW_CONSOLE


# ===========================================================================
# Every spawn site uses it
# ===========================================================================
PILOT_SOURCE = (REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py").read_text(
    encoding="utf-8")


def test_every_subprocess_call_in_the_pilot_is_hidden():
    """The regression guard.

    One unhidden spawn is one black window, and the one that was missed was the
    only one that runs during a broadcast.
    """
    import re

    offenders = []
    for match in re.finditer(r"subprocess\.(Popen|run)\((.*?)\n\s*\)", PILOT_SOURCE, re.DOTALL):
        call = match.group(0)
        if "hidden_child_process_options" not in call:
            offenders.append(call.splitlines()[0].strip())
    assert offenders == [], f"these spawn without the hidden options: {offenders}"


#: Scanned through the AST rather than as text.
#:
#: The first version of these two tests searched the raw source, and failed on
#: the docstring that explains *why* the code does not use `shell=True` or a
#: cmd.exe wrapper. A scan that cannot tell code from the comment describing it
#: will always be loudest about the file that documents itself best.
RECEIVER_MODULES = ("audio_receiver_pilot.py", "receiver_agent.py", "windows_audio_devices.py")


def _code_of(name: str):
    import ast

    return ast.parse((REPOSITORY_ROOT / "tools" / name).read_text(encoding="utf-8"))


def test_nothing_in_the_pilot_uses_a_shell():
    import ast

    offenders = []
    for node in ast.walk(_code_of("audio_receiver_pilot.py")):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "shell" and getattr(keyword.value, "value", False) is True:
                offenders.append(node.lineno)
    assert offenders == [], f"shell=True at lines {offenders}"


def test_nothing_in_the_receiver_wraps_a_child_in_powershell_or_cmd():
    """Only string *literals in code* count. A wrapper named in prose is prose."""
    import ast

    wrappers = ("powershell.exe", "pwsh.exe", "cmd.exe", "cmd /c")
    offenders = []
    for name in RECEIVER_MODULES:
        tree = _code_of(name)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docstrings:
                    continue
                lowered = node.value.lower()
                for wrapper in wrappers:
                    if wrapper in lowered:
                        offenders.append(f"{name}:{node.lineno} {wrapper}")
    assert offenders == [], f"a child is wrapped in a shell: {offenders}"


# ===========================================================================
# The decoder still behaves
# ===========================================================================
class RecordingPopen:
    """Captures how the decoder asked for its child."""

    calls: list = []

    def __init__(self, command, **kwargs):
        RecordingPopen.calls.append({"command": command, "kwargs": kwargs})
        self.pid = 4242
        self.stdin = None
        self.stdout = None
        self.returncode = None

    def poll(self):
        return None


@pytest.fixture()
def recorded(monkeypatch):
    RecordingPopen.calls = []
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    monkeypatch.setattr("tools.audio_receiver_pilot.subprocess.Popen", RecordingPopen)
    return RecordingPopen


def test_the_decoder_starts_ffmpeg_hidden(recorded, monkeypatch):
    monkeypatch.setattr("tools.audio_receiver_pilot.threading.Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    decoder = FfmpegDecoder()
    decoder.start()
    call = recorded.calls[-1]
    if ON_WINDOWS:
        assert call["kwargs"]["creationflags"] & subprocess.CREATE_NO_WINDOW
    assert call["kwargs"].get("shell") in (None, False)


def test_the_decoder_keeps_its_pipes(recorded, monkeypatch):
    """Hiding the window must not close the pipes the decode path needs: stdin
    is how audio gets in, stdout is how progress comes back."""
    monkeypatch.setattr("tools.audio_receiver_pilot.threading.Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    FfmpegDecoder().start()
    kwargs = recorded.calls[-1]["kwargs"]
    assert kwargs["stdin"] is subprocess.PIPE
    assert kwargs["stdout"] is subprocess.PIPE


def test_the_decoder_still_runs_a_real_ffmpeg_binary(recorded, monkeypatch):
    monkeypatch.setattr("tools.audio_receiver_pilot.threading.Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())
    FfmpegDecoder().start()
    command = recorded.calls[-1]["command"]
    assert isinstance(command, (list, tuple)), "a string command would need a shell"
    assert "ffmpeg" in str(command[0]).lower()


def test_the_capability_probe_is_hidden_too(monkeypatch):
    """It runs before a broadcast, and a flash at start-up is still a flash."""
    recorded = []

    def fake_run(command, **kwargs):
        recorded.append(kwargs)
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("tools.audio_receiver_pilot.shutil.which", lambda _n: "ffmpeg.exe")
    monkeypatch.setattr("tools.audio_receiver_pilot.subprocess.run", fake_run)
    opus_webm_decode_supported()
    assert recorded, "the probe did not run"
    for kwargs in recorded:
        if ON_WINDOWS:
            assert kwargs.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
        assert kwargs.get("shell") in (None, False)


# ===========================================================================
# Failure is still reported
# ===========================================================================
def test_a_missing_ffmpeg_is_a_controlled_error_not_a_dialog(monkeypatch):
    """A windowed process that raises unhandled shows a modal box on an
    unattended counter. The decoder must fail as an exception the Agent already
    classifies, not as a popup."""
    def explode(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg.exe not found")

    monkeypatch.setattr("tools.audio_receiver_pilot.subprocess.Popen", explode)
    with pytest.raises(OSError):
        FfmpegDecoder().start()


def test_hiding_the_window_did_not_remove_stderr_handling():
    """stderr stays DEVNULL by design - FFmpeg writes progress there in torrents
    and it is not the diagnostic channel here - but the choice must be explicit
    in the source rather than an accident of the rewrite."""
    assert "stderr=subprocess.DEVNULL" in PILOT_SOURCE
