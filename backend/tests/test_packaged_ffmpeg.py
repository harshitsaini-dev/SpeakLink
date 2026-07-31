"""The Store PC has no FFmpeg on PATH, and must never be asked to get one.

THE REAL FAILURE

RC2 reached "Choose the Store's audio output", listed
"[WIRED] Speakers (Realtek(R) Audio)", and Test Sound returned:

    DEVICE_ERROR: ffmpeg was not found on PATH

while the package it was running from contained ``Receiver\\ffmpeg.exe`` all
along. The lookup was ``shutil.which("ffmpeg")`` - a search of the machine's PATH
- in a program whose whole point is that it carries everything it needs.

The asymmetry is the interesting part. ``receiver_agent`` already had
``prefer_packaged_ffmpeg()`` and calls it at start-up, so the INSTALLED Receiver
was fine. The wizard never called it, and its lookup was relative to the Receiver
executable rather than to StoreSetup's own package. One half of the product knew
the answer and the other half asked the machine.

An absolute path is used rather than prepending to PATH: a process-wide PATH
mutation makes every later ``which`` depend on import order, and cannot stop some
other FFmpeg on the machine from being picked up first.
"""

from __future__ import annotations

import hashlib
import os
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools import resource_paths  # noqa: E402
from tools.resource_paths import (  # noqa: E402
    PACKAGED_FFMPEG,
    RESOURCE_ROOT_ENV,
    PackagedFfmpegMissing,
    packaged_ffmpeg_is_verified,
    resolve_packaged_ffmpeg,
)


def make_package(root: Path, *, ffmpeg=True, body=b"FAKE-FFMPEG", sums=True) -> Path:
    receiver = root / PACKAGED_FFMPEG[0]
    receiver.mkdir(parents=True, exist_ok=True)
    if ffmpeg:
        (receiver / PACKAGED_FFMPEG[1]).write_bytes(body)
    if sums:
        digest = hashlib.sha256(body).hexdigest()
        (root / "SHA256SUMS.txt").write_text(
            f"{digest}  {'/'.join(PACKAGED_FFMPEG)}\n", encoding="utf-8")
    return root


@pytest.fixture()
def empty_path(monkeypatch):
    """PATH completely cleared - the state a real Store desktop is in."""
    monkeypatch.setenv("PATH", "")
    return True


# ===========================================================================
# 1. Packaged mode resolves the bundled binary, absolutely
# ===========================================================================
def test_with_an_empty_path_the_packaged_ffmpeg_is_found(tmp_path, monkeypatch, empty_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path)))

    found = resolve_packaged_ffmpeg()

    assert found == (tmp_path / "Receiver" / "ffmpeg.exe").resolve()
    assert found.is_absolute()


def test_the_resolved_path_is_never_the_bare_word_ffmpeg(tmp_path, monkeypatch, empty_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path)))

    found = str(resolve_packaged_ffmpeg())

    assert found != "ffmpeg"
    assert os.sep in found, "a bare name would be resolved through PATH by the OS"


def test_a_system_ffmpeg_earlier_in_path_is_ignored(tmp_path, monkeypatch):
    """A Store PC that happens to have some other FFmpeg must still run the one
    this package was tested with."""
    other = tmp_path / "system"
    other.mkdir()
    (other / "ffmpeg.exe").write_bytes(b"SOME-OTHER-FFMPEG")
    monkeypatch.setenv("PATH", str(other))
    package = make_package(tmp_path / "package")
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(package))

    found = resolve_packaged_ffmpeg()

    assert found.parent.name == "Receiver"
    assert found.read_bytes() == b"FAKE-FFMPEG"


# ===========================================================================
# 2. Failing closed
# ===========================================================================
def test_a_frozen_package_without_ffmpeg_fails_closed(tmp_path, monkeypatch, empty_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path, ffmpeg=False)))

    with pytest.raises(PackagedFfmpegMissing):
        resolve_packaged_ffmpeg()


def test_the_refusal_never_tells_the_operator_to_install_ffmpeg(tmp_path, monkeypatch,
                                                                empty_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path, ffmpeg=False)))

    with pytest.raises(PackagedFfmpegMissing) as failure:
        resolve_packaged_ffmpeg(allow_path_fallback=False)

    message = str(failure.value).lower()
    assert "incomplete" in message
    assert "verified package" in message
    assert "add ffmpeg to path" not in message
    assert "install ffmpeg on this computer" not in message.replace("do not install ffmpeg", "")


def test_a_missing_bundle_is_refused_before_any_audio_device_is_opened(tmp_path,
                                                                      monkeypatch, empty_path):
    """The resolver raises on its own, so nothing has to open a sound card to
    discover the package is broken."""
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path, ffmpeg=False)))
    with pytest.raises(PackagedFfmpegMissing):
        resolve_packaged_ffmpeg(allow_path_fallback=False)


# ===========================================================================
# 3. Hash verification
# ===========================================================================
def test_a_matching_bundled_ffmpeg_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path)))
    assert packaged_ffmpeg_is_verified() is True


def test_a_swapped_bundled_ffmpeg_does_not_verify(tmp_path, monkeypatch):
    root = make_package(tmp_path)
    (root / "Receiver" / "ffmpeg.exe").write_bytes(b"SOMETHING-ELSE-ENTIRELY")
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(root))

    assert packaged_ffmpeg_is_verified() is False


def test_a_package_with_no_sums_file_does_not_verify(tmp_path, monkeypatch):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path, sums=False)))
    assert packaged_ffmpeg_is_verified() is False


# ===========================================================================
# 4. Development mode
# ===========================================================================
def test_a_checkout_may_fall_back_to_path(tmp_path, monkeypatch):
    other = tmp_path / "devtools"
    other.mkdir()
    (other / "ffmpeg.exe").write_bytes(b"DEV")
    monkeypatch.setenv("PATH", str(other))
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path / "no-package"))
    (tmp_path / "no-package").mkdir()
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    found = resolve_packaged_ffmpeg(allow_path_fallback=True)
    assert found.name.startswith("ffmpeg")


def test_a_checkout_with_nothing_anywhere_still_refuses(tmp_path, monkeypatch, empty_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path / "no-package"))
    (tmp_path / "no-package").mkdir()

    with pytest.raises(PackagedFfmpegMissing):
        resolve_packaged_ffmpeg(allow_path_fallback=True)


# ===========================================================================
# 5. The Test Sound command itself
# ===========================================================================
def test_the_test_sound_command_carries_the_absolute_bundled_path(tmp_path, monkeypatch,
                                                                  empty_path):
    """The end the operator actually meets: the subprocess argv."""
    from tools import store_setup_core as core
    from tools.windows_audio_devices import OutputDevice

    package = make_package(tmp_path)
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(package))

    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = self

        def read(self, _n):
            return b""

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    def fake_popen(command, **kwargs):
        captured["command"] = command
        return FakeProcess()

    class FakeSink:
        failed = False
        frames_written = 0

        def __init__(self, *a, **k):
            pass

        def open(self):
            pass

        def write(self, _chunk):
            return True

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", FakeSink)
    device = OutputDevice(index=8, name="Speakers (Realtek(R) Audio)", host_api="MME",
                          max_output_channels=2, default_samplerate=48000.0, is_default=False)

    core.play_test_tone(device, popen=fake_popen)

    command = captured["command"]
    expected = str((package / "Receiver" / "ffmpeg.exe").resolve())
    assert command[0] == expected, f"Test Sound ran {command[0]!r}"
    assert command[0] != "ffmpeg"


def test_test_sound_reports_the_package_problem_not_a_path_problem(tmp_path, monkeypatch,
                                                                   empty_path):
    from tools import store_setup_core as core
    from tools.windows_audio_devices import OutputDevice

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(make_package(tmp_path, ffmpeg=False)))

    class FakeSink:
        failed = False
        frames_written = 0

        def __init__(self, *a, **k):
            pass

        def open(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", FakeSink)
    device = OutputDevice(index=8, name="Speakers", host_api="MME",
                          max_output_channels=2, default_samplerate=48000.0, is_default=False)

    result = core.play_test_tone(device)

    assert "was not found on PATH" not in result.detail, (
        "the operator is still being told this is a PATH problem"
    )
    assert "incomplete" in result.detail.lower()


def test_the_source_no_longer_looks_ffmpeg_up_on_path():
    """A guard on the guard: the exact call that caused this must not return."""
    import ast

    source = (REPOSITORY_ROOT / "tools" / "store_setup_core.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            if name == "which":
                argument = node.args[0] if node.args else None
                value = getattr(argument, "value", None)
                assert value != "ffmpeg", (
                    f"line {node.lineno}: store_setup_core resolves ffmpeg through PATH again"
                )
