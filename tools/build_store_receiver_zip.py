"""Package the Store Kit into one versioned ZIP to carry to a Store PC.

WHAT THIS IS, AND WHAT IT IS NOT

It is an ORCHESTRATOR. Every build step here is an existing, supported script
that already knows how to do its job:

    scripts/Build-SpeakLinkReceiver.ps1          the Receiver, with FFmpeg
    scripts/Test-SpeakLinkReceiverPackage.ps1    verifies that package
    scripts/Build-SpeakLinkStoreSetupPackage.ps1 wraps it with the wizard,
                                                installer scripts, manifest
                                                and SHA256SUMS

Nothing about how a Store package is built is reimplemented here. Doing that
would create a second definition of "a Store Kit" that drifts from the one the
packaging tests already cover.

WHY PYTHON RATHER THAN A LONGER .BAT

Same reason as the HQ launcher: a .bat file is Windows-only, and logic buried
in one cannot be tested, read or reused. build-store-receiver.bat is a thin
wrapper around this.

The Store Kit itself remains Windows-specific and that is not a limitation
being worked around - a Store PC is a Windows till, the Receiver is a Windows
executable, and its auto-start is a Windows Scheduled Task.

WHAT THE ZIP MUST NEVER CONTAIN

A real Device credential, an enrolment code, a Settings Password verifier, an
HQ signing secret, the Receiver key container, an HQ database, logs or
recordings. Those either belong to one particular Store or belong to HQ alone,
and a ZIP that carries them turns a convenience into an incident. The audit
below refuses the build rather than warning about it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
ARTIFACTS = REPO_ROOT / "artifacts"

#: Names that must never appear in the ZIP, whatever directory they turn up in.
FORBIDDEN_NAMES = {
    ".env",
    "jwt-secret.txt",
    "receiver-hmac-keys.bin",
    "settings-password.json",
    "credential.json",
    "device-credential.json",
    "speaklink.db",
    "speaklink_live.db",
    "hq.db",
}

#: Suffixes that must never appear.
FORBIDDEN_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key", ".wav", ".mp3"}

#: Directory names that must never appear.
FORBIDDEN_DIRS = {".venv", "node_modules", "__pycache__", "recordings", ".git"}

#: Content patterns. A real Device credential and a real enrolment code have
#: recognisable shapes, and a developer's home directory is a path that will
#: not exist on a till.
#:
#: The credential pattern requires the prefix FOLLOWED BY MATERIAL, matching
#: tools/receiver_agent.py's _CREDENTIAL_PATTERN. Searching for the bare
#: prefix flagged the installer and verifier scripts, which contain it inside
#: their OWN leak-detection patterns - a script that checks for a leaked
#: credential is the opposite of a leaked credential, and a scanner that
#: cannot tell those apart is one an operator learns to override.
FORBIDDEN_CONTENT = (
    rb"speaklink_rcv_v1\.[A-Za-z0-9._\-]+",
    rb"ECHO(-[A-Z0-9]{4}){2,}",
    rb"C:\\Users\\admin",
)

#: The executables a Store actually needs, and their required icon.
EXPECTED_EXECUTABLES = (
    "SpeakLinkStoreSetup.exe",
    "SpeakLinkReceiver.exe",
    "SpeakLinkReceiverBackground.exe",
)

CANONICAL_ICON = REPO_ROOT / "assets" / "speaklink.ico"


class BuildError(RuntimeError):
    """Reported without a traceback - it is an operator problem."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def run_powershell(script: Path, arguments: list[str]) -> str:
    if platform.system() != "Windows":
        raise BuildError(
            "The Store Kit is Windows software - its executables are built by "
            "PyInstaller on Windows and its auto-start is a Windows Scheduled "
            "Task. This builder must run on Windows.")
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(script), *arguments]
    print(f"  -> {script.name} {' '.join(arguments)}")
    finished = subprocess.run(command, capture_output=True, text=True)
    if finished.returncode != 0:
        raise BuildError(
            f"{script.name} failed (exit {finished.returncode}).\n"
            f"{(finished.stdout or '')[-2000:]}\n{(finished.stderr or '')[-2000:]}")
    return finished.stdout or ""


def newest_package(prefix: str) -> Path:
    candidates = sorted((p for p in ARTIFACTS.glob(f"{prefix}-*") if p.is_dir()),
                        key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise BuildError(f"No {prefix}-* package was produced in {ARTIFACTS}.")
    return candidates[-1]


def git_commit() -> str:
    finished = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(REPO_ROOT), capture_output=True, text=True)
    return (finished.stdout or "").strip() or "unknown"


def icon_payloads() -> list[bytes]:
    """Every image inside the canonical icon, so an executable can be checked
    for the real artwork rather than for "has some icon"."""
    import struct
    raw = CANONICAL_ICON.read_bytes()
    _reserved, _kind, count = struct.unpack("<HHH", raw[:6])
    payloads = []
    for index in range(count):
        offset = 6 + index * 16
        (_w, _h, _c, _r, _p, _bpp, size, data) = struct.unpack(
            "<BBBBHHII", raw[offset:offset + 16])
        payloads.append(raw[data:data + size])
    return payloads


def pe_subsystem(path: Path) -> int:
    with path.open("rb") as handle:
        handle.seek(0x3C)
        pe_offset = int.from_bytes(handle.read(4), "little")
        handle.seek(pe_offset + 4 + 20 + 68)
        return int.from_bytes(handle.read(2), "little")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
def audit_tree(root: Path) -> list[str]:
    """Refuse rather than warn. Every finding is returned, not just the first,
    so one build reports everything an operator has to fix."""
    findings: list[str] = []
    biggest_icon = max(icon_payloads(), key=len)

    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in FORBIDDEN_DIRS for part in relative.parts):
            findings.append(f"forbidden directory: {relative}")
            continue
        if not path.is_file():
            continue
        if path.name.lower() in FORBIDDEN_NAMES:
            findings.append(f"forbidden file: {relative}")
            continue
        # ffmpeg.exe is third-party and deliberately unbranded; everything
        # else with these suffixes is ours and must not be here.
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden file type: {relative}")
            continue

        # Content scan, bounded to text-ish and small files so a 250 MB binary
        # is not read into memory. The executables are checked separately.
        if path.suffix.lower() in (".txt", ".json", ".ps1", ".bat", ".md", ".cfg", ".ini"):
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            for pattern in FORBIDDEN_CONTENT:
                match = re.search(pattern, blob)
                if match:
                    # The MATCH is never printed - it would be the very
                    # credential this refuses to ship. The pattern and the file
                    # are enough to act on.
                    findings.append(
                        f"forbidden content matching {pattern.decode()} in {relative}")

    # Every SpeakLink executable must carry the SpeakLink icon, and the
    # background one must be windowless.
    for executable in sorted(root.rglob("*.exe")):
        relative = executable.relative_to(root)
        if executable.name.lower() == "ffmpeg.exe":
            continue          # third-party, correctly unbranded
        raw = executable.read_bytes()
        if biggest_icon not in raw:
            findings.append(f"missing the SpeakLink icon: {relative}")
        if executable.name == "SpeakLinkReceiverBackground.exe":
            if pe_subsystem(executable) != 2:
                findings.append(
                    f"{relative} is a console application - it would put a "
                    "black window on a Store counter")
        if executable.name == "SpeakLinkStoreSetup.exe":
            if pe_subsystem(executable) != 2:
                findings.append(f"{relative} is not windowed")

    return findings


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build(receiver_version: str, setup_version: str, ffmpeg: str | None,
          skip_build: bool) -> Path:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    if skip_build:
        print("Reusing the most recent verified packages (--skip-build)")
        receiver_package = newest_package("SpeakLinkReceiver")
        setup_package = newest_package("SpeakLinkStoreSetup")
    else:
        print("=== 1/3  building the Receiver ===")
        arguments = ["-Version", receiver_version]
        if ffmpeg:
            arguments += ["-FfmpegPath", ffmpeg]
        run_powershell(SCRIPTS / "Build-SpeakLinkReceiver.ps1", arguments)
        receiver_package = newest_package("SpeakLinkReceiver")

        print("=== 2/3  verifying the Receiver package ===")
        run_powershell(SCRIPTS / "Test-SpeakLinkReceiverPackage.ps1",
                       ["-PackagePath", str(receiver_package),
                        "-Version", receiver_version])

        print("=== 3/3  building the Store Setup package ===")
        run_powershell(SCRIPTS / "Build-SpeakLinkStoreSetupPackage.ps1",
                       ["-ReceiverPackagePath", str(receiver_package),
                        "-Version", setup_version])
        setup_package = newest_package("SpeakLinkStoreSetup")

    print(f"\nStore Setup package: {setup_package.name}")

    print("=== auditing the package before it is zipped ===")
    findings = audit_tree(setup_package)
    if findings:
        raise BuildError(
            "Refusing to build the ZIP - the package contains things a Store "
            "must never receive:\n  " + "\n  ".join(findings))
    print("  no credential, key, database, log or developer path        PASS")
    print("  every SpeakLink executable carries the SpeakLink icon        PASS")
    print("  the background Receiver is windowless                      PASS")

    for name in EXPECTED_EXECUTABLES:
        found = list(setup_package.rglob(name))
        if not found:
            raise BuildError(f"The package is missing {name}.")
    print("  Setup, Receiver and background Receiver all present        PASS")

    # ---- the ZIP -----------------------------------------------------------
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    zip_name = f"SpeakLink-Store-Kit-{setup_version}-{git_commit()}-{stamp}.zip"
    zip_path = ARTIFACTS / zip_name

    files = sorted(p for p in setup_package.rglob("*") if p.is_file())
    print(f"\n=== writing {zip_name} ({len(files)} files) ===")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for path in files:
            archive.write(path, path.relative_to(setup_package.parent))

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    checksum_path = zip_path.with_suffix(".zip.sha256")
    checksum_path.write_text(f"{digest}  {zip_name}\n", encoding="utf-8")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  {zip_name}")
    print(f"  {size_mb:.1f} MB")
    print(f"  SHA256 {digest}")
    print(f"  checksum written to {checksum_path.name}")
    print("\nSPEAKLINK_STORE_KIT_ZIP_READY")
    print(f"Take this one file to the Store PC: {zip_path}")
    return zip_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_store_receiver_zip",
        description="Build one versioned Store Kit ZIP using the existing "
                    "supported build scripts.")
    parser.add_argument("--receiver-version", default="1.0.0")
    parser.add_argument("--setup-version", default="1.1.0")
    parser.add_argument("--ffmpeg", default=None,
                        help="Path to the ffmpeg.exe to ship. Defaults to the "
                             "one on PATH; never downloaded.")
    parser.add_argument("--skip-build", action="store_true",
                        help="Zip and audit the most recent existing packages "
                             "instead of rebuilding them.")
    arguments = parser.parse_args(argv)

    try:
        build(arguments.receiver_version, arguments.setup_version,
              arguments.ffmpeg, arguments.skip_build)
        return 0
    except BuildError as failure:
        print(f"\n{failure}\n", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
