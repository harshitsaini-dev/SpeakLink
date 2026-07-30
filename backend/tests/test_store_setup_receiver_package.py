"""Which Receiver package StoreSetup installs, and why it is never a guess.

The original InstallScreen pointed at a hardcoded ``artifacts/receiver-package``
that has never existed on this branch. That is exactly the shape of "looks
finished" this project keeps finding: a path that reads correctly and points at
nothing. This locates the newest EchoCastReceiver-* package built by
Build-EchoCastReceiver.ps1 and re-verifies it independently before StoreSetup
is allowed to install from it - never trusting "newest" or "correctly named"
by itself.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools import store_setup_core as core  # noqa: E402


def _fake_pe(path: Path, subsystem: int) -> None:
    blob = bytearray(512)
    blob[0:2] = b"MZ"
    header = 0x80
    blob[0x3C:0x40] = header.to_bytes(4, "little")
    blob[header:header + 4] = b"PE\x00\x00"
    at = header + 4 + 20 + 68
    blob[at:at + 2] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(blob))


def _build_package(root: Path, name: str, *, console_subsystem=3, background_subsystem=2,
                   with_ffmpeg=True, tamper_hash=False, extra_files=()) -> Path:
    package = root / name
    package.mkdir(parents=True)
    _fake_pe(package / "EchoCastReceiver.exe", console_subsystem)
    _fake_pe(package / "EchoCastReceiverBackground.exe", background_subsystem)
    if with_ffmpeg:
        (package / "ffmpeg.exe").write_bytes(b"not a real ffmpeg")
    for relative, content in extra_files:
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    (package / "manifest.json").write_text(json.dumps({"product": "EchoCastReceiver"}),
                                           encoding="utf-8")

    digests = []
    for item in sorted(package.rglob("*")):
        if item.is_file() and item.name not in ("SHA256SUMS.txt", "manifest.json"):
            digest = hashlib.sha256(item.read_bytes()).hexdigest()
            if tamper_hash and item.name == "ffmpeg.exe":
                digest = "0" * 64
            digests.append(f"{digest}  {item.relative_to(package).as_posix()}")
    (package / "SHA256SUMS.txt").write_text("\n".join(digests) + "\n", encoding="utf-8")
    return package


# ===========================================================================
# read_pe_subsystem
# ===========================================================================
def test_read_pe_subsystem_reads_windows_gui(tmp_path):
    exe = tmp_path / "a.exe"
    _fake_pe(exe, 2)
    assert core.read_pe_subsystem(exe) == 2


def test_read_pe_subsystem_reads_windows_cui(tmp_path):
    exe = tmp_path / "a.exe"
    _fake_pe(exe, 3)
    assert core.read_pe_subsystem(exe) == 3


# ===========================================================================
# verify_receiver_package
# ===========================================================================
def test_a_correct_package_verifies(tmp_path):
    package = _build_package(tmp_path, "EchoCastReceiver-1.0.0-abc1234-20260101-000000")
    verdict = core.verify_receiver_package(package)
    assert verdict.ok, verdict.reasons


def test_a_missing_background_exe_is_refused(tmp_path):
    package = _build_package(tmp_path, "pkg")
    (package / "EchoCastReceiverBackground.exe").unlink()
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("EchoCastReceiverBackground.exe" in r for r in verdict.reasons)


def test_a_console_subsystem_background_exe_is_refused(tmp_path):
    package = _build_package(tmp_path, "pkg", background_subsystem=3)
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("WINDOWS_GUI" in r for r in verdict.reasons)


def test_a_windowed_console_exe_is_refused(tmp_path):
    """The operator EXE must keep its console - a windowed one has nowhere to
    print 'enrol', 'status' or 'list-audio-devices' output."""
    package = _build_package(tmp_path, "pkg", console_subsystem=2)
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("WINDOWS_CUI" in r for r in verdict.reasons)


def test_missing_ffmpeg_is_refused(tmp_path):
    package = _build_package(tmp_path, "pkg", with_ffmpeg=False)
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("ffmpeg" in r for r in verdict.reasons)


def test_a_wrong_hash_is_refused(tmp_path):
    package = _build_package(tmp_path, "pkg", tamper_hash=True)
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("hash" in r for r in verdict.reasons)


@pytest.mark.parametrize("bad_file", [".env", "server.py", "leftover.db"])
def test_a_forbidden_file_is_refused(tmp_path, bad_file):
    package = _build_package(tmp_path, "pkg", extra_files=[(bad_file, b"x")])
    verdict = core.verify_receiver_package(package)
    assert not verdict.ok
    assert any("forbidden" in r for r in verdict.reasons)


# ===========================================================================
# locate_verified_receiver_package - never a hardcoded placeholder
# ===========================================================================
def test_the_gui_never_hardcodes_receiver_package():
    """The defect this file exists to close: InstallScreen used to point at
    artifacts/receiver-package, a path that has never existed on this branch."""
    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(encoding="utf-8")
    assert "receiver-package" not in source
    assert "locate_verified_receiver_package" in source


def test_locating_with_no_artifacts_directory_raises_a_clear_error(tmp_path):
    with pytest.raises(core.NoVerifiedReceiverPackage):
        core.locate_verified_receiver_package(artifacts_root=tmp_path / "does-not-exist")


def test_locating_with_no_candidates_raises_a_clear_error(tmp_path):
    (tmp_path / "unrelated-folder").mkdir()
    with pytest.raises(core.NoVerifiedReceiverPackage):
        core.locate_verified_receiver_package(artifacts_root=tmp_path)


def test_the_newest_valid_package_is_chosen(tmp_path):
    import os
    import time

    older = _build_package(tmp_path, "EchoCastReceiver-1.0.0-aaa1111-20260101-000000")
    time.sleep(0.05)
    newer = _build_package(tmp_path, "EchoCastReceiver-1.0.1-bbb2222-20260102-000000")
    chosen = core.locate_verified_receiver_package(artifacts_root=tmp_path)
    assert chosen == newer


def test_a_newer_but_broken_package_is_skipped_for_an_older_good_one(tmp_path):
    """Newest is a suggestion, not a trust decision. A broken newest package
    must not be installed just because it sorts first."""
    import time

    good = _build_package(tmp_path, "EchoCastReceiver-1.0.0-aaa1111-20260101-000000")
    time.sleep(0.05)
    _build_package(tmp_path, "EchoCastReceiver-1.0.1-bbb2222-20260102-000000",
                   background_subsystem=3)  # broken: console subsystem
    chosen = core.locate_verified_receiver_package(artifacts_root=tmp_path)
    assert chosen == good


def test_when_every_candidate_is_broken_the_error_names_why(tmp_path):
    _build_package(tmp_path, "EchoCastReceiver-1.0.0-aaa1111-20260101-000000",
                   with_ffmpeg=False)
    with pytest.raises(core.NoVerifiedReceiverPackage) as failure:
        core.locate_verified_receiver_package(artifacts_root=tmp_path)
    assert "ffmpeg" in str(failure.value)


def test_non_receiver_directories_under_artifacts_are_ignored(tmp_path):
    (tmp_path / "EchoCastHQ-0.1.0-abc-20260101-000000").mkdir()
    good = _build_package(tmp_path, "EchoCastReceiver-1.0.0-aaa1111-20260101-000000")
    chosen = core.locate_verified_receiver_package(artifacts_root=tmp_path)
    assert chosen == good
