"""The versioned HQ package: what ships, and - mostly - what must not.

A package is the one artifact that leaves this machine. Everything dangerous
about this system is a file that could be copied into it by accident: the
persistent database, the Receiver HMAC key container, the signing secret, a
.env, a log with a Store name and a token in it. None of them are needed to run
HQ on another computer, and every one of them is a full compromise if it turns
up on a USB stick.

So the verifier's job is mostly negative. It checks that what should be there is
there and hashes correctly, and then it goes looking for the things that must
not be.

These tests read the two scripts and build a real package into a temporary
directory. They never write into artifacts/ and never touch the persistent root.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPOSITORY_ROOT / "scripts"
BUILD = SCRIPTS / "Build-EchoCastHQPackage.ps1"
VERIFY = SCRIPTS / "Test-EchoCastHQPackage.ps1"


def _text(script: Path) -> str:
    return script.read_text(encoding="utf-8")


windows_only = pytest.mark.skipif(os.name != "nt", reason="packaging is Windows")


# ===========================================================================
# The scripts exist and behave like the rest of the repository's scripts
# ===========================================================================
@pytest.mark.parametrize("script", [BUILD, VERIFY], ids=lambda p: p.name)
def test_the_script_exists(script: Path):
    assert script.exists(), f"{script.name} has not been written"


@pytest.mark.parametrize("script", [BUILD, VERIFY], ids=lambda p: p.name)
def test_the_script_has_no_byte_order_mark(script: Path):
    assert not script.read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("script", [BUILD, VERIFY], ids=lambda p: p.name)
def test_the_script_stops_on_the_first_error(script: Path):
    assert "$ErrorActionPreference = 'Stop'" in _text(script)


# ===========================================================================
# The build records what it built from
# ===========================================================================
def test_the_package_name_carries_version_commit_and_time():
    body = _text(BUILD)
    assert "EchoCastHQ-" in body
    assert "rev-parse" in body, "the package does not record a source commit"


def test_a_dirty_tree_is_recorded_or_refused():
    """A package built from uncommitted work that says nothing about it is a
    package nobody can reproduce."""
    body = _text(BUILD)
    assert "status --porcelain" in body


def test_the_manifest_carries_no_username_or_build_path():
    """A manifest that shipped C:\\Users\\<someone>\\... names the person who
    built it to everyone who receives it."""
    body = _text(BUILD)
    assert "$env:USERNAME" not in body


def test_the_build_refuses_a_console_subsystem_runtime():
    body = _text(BUILD)
    assert "ReadUInt16" in body and "0x3C" in body


def test_the_build_requires_a_production_frontend():
    assert "yarn build" in _text(BUILD)


# ===========================================================================
# What must never be copied in
# ===========================================================================
@pytest.mark.parametrize("forbidden", [
    "*.db", ".env", "node_modules", "*.log", "jwt-secret", "hmac-keys",
])
def test_the_builder_excludes_it(forbidden: str):
    assert forbidden in _text(BUILD), f"nothing excludes {forbidden}"


@pytest.mark.parametrize("forbidden", [
    "*.db", ".env", "node_modules", "*.log", "jwt-secret", "hmac-keys",
    "*.sqlite", "*.pem", "*.key",
])
def test_the_verifier_looks_for_it(forbidden: str):
    assert forbidden in _text(VERIFY), f"the verifier never looks for {forbidden}"


def test_the_verifier_rejects_an_unexpected_executable():
    body = _text(VERIFY)
    assert "*.exe" in body


def test_the_verifier_checks_every_manifest_hash():
    body = _text(VERIFY)
    assert "Get-FileHash" in body and "SHA256SUMS" in body


def test_the_verifier_accounts_for_files_the_manifest_never_mentions():
    """Hashing only the listed files means an extra file - a database somebody
    dropped in - passes every check."""
    body = _text(VERIFY)
    assert "unlisted" in body.lower() or "not listed" in body.lower()


def test_the_verifier_refuses_a_development_server_command():
    body = _text(VERIFY)
    assert "react-scripts" in body


def test_the_verifier_checks_the_runtime_pe_subsystem():
    assert "ReadUInt16" in _text(VERIFY)


def test_the_verifier_is_read_only():
    body = _text(VERIFY)
    for forbidden in ("Remove-Item", "Copy-Item", "Set-Content", "New-Item"):
        assert forbidden not in body, f"the verifier calls {forbidden}"


# ===========================================================================
# Building one for real
# ===========================================================================
def _run(script: Path, arguments: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script)] + arguments,
        capture_output=True, text=True, timeout=900, cwd=str(REPOSITORY_ROOT),
    )


def _built_runtime() -> "Path | None":
    """The most recent locally built runtime, if there is one."""
    candidates = sorted((REPOSITORY_ROOT / "build").glob(
        "hq-dist-*/EchoCastHQRuntime/EchoCastHQRuntime.exe"))
    return candidates[-1] if candidates else None


needs_runtime = pytest.mark.skipif(
    _built_runtime() is None,
    reason="no locally built EchoCastHQRuntime.exe; run hq_runtime.spec first")
needs_frontend = pytest.mark.skipif(
    not (REPOSITORY_ROOT / "frontend" / "build" / "index.html").exists(),
    reason="no production frontend build; run 'yarn build' in frontend")


@windows_only
@needs_runtime
@needs_frontend
def test_a_package_builds_and_verifies(tmp_path):
    runtime = _built_runtime()
    built = _run(BUILD, [
        "-RuntimePath", str(runtime.parent),
        "-OutputRoot", str(tmp_path),
        "-Version", "0.0.0-test", "-AllowDirtyTree",
    ])
    assert built.returncode == 0, built.stdout + built.stderr
    assert "ECHOCAST_HQ_PACKAGE_BUILT" in built.stdout

    packages = [item for item in tmp_path.iterdir() if item.is_dir()]
    assert len(packages) == 1, f"expected one package, found {packages}"
    package = packages[0]

    verified = _run(VERIFY, ["-PackagePath", str(package)])
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "ECHOCAST_HQ_PACKAGE_VERIFIED" in verified.stdout


@windows_only
@needs_runtime
@needs_frontend
def test_the_built_package_contains_no_data_of_any_kind(tmp_path):
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())

    offenders = []
    for item in package.rglob("*"):
        if not item.is_file():
            continue
        name = item.name.lower()
        if (name.endswith((".db", ".sqlite", ".sqlite3", ".log", ".pem", ".key"))
                or name == ".env" or "jwt-secret" in name or "hmac-keys" in name):
            offenders.append(str(item.relative_to(package)))
    assert offenders == [], f"the package ships data: {offenders}"


@windows_only
@needs_runtime
@needs_frontend
def test_the_built_package_carries_the_four_task_scripts(tmp_path):
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())
    for name in ("Install-EchoCastHQAutoStart.ps1", "Test-EchoCastHQAutoStart.ps1",
                 "Repair-EchoCastHQAutoStart.ps1", "Uninstall-EchoCastHQAutoStart.ps1"):
        assert (package / name).exists(), f"{name} is not in the package"


@windows_only
@needs_runtime
@needs_frontend
def test_every_manifest_hash_matches_what_was_written(tmp_path):
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())

    listed = {}
    for line in (package / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, relative = line.split("  ", 1)
        listed[relative] = digest

    assert listed, "the package has an empty hash manifest"
    for relative, digest in listed.items():
        target = package / relative.replace("/", os.sep)
        assert target.exists(), f"{relative} is listed and missing"
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual == digest, f"{relative} does not match its recorded hash"


@windows_only
@needs_runtime
@needs_frontend
def test_a_tampered_package_is_refused(tmp_path):
    """The check that makes the other checks worth running."""
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())
    (package / "frontend" / "index.html").write_text("<!-- changed -->",
                                                     encoding="utf-8")
    verified = _run(VERIFY, ["-PackagePath", str(package)])
    assert verified.returncode != 0
    assert "ECHOCAST_HQ_PACKAGE_VERIFIED" not in verified.stdout


@windows_only
@needs_runtime
@needs_frontend
def test_a_database_dropped_into_a_package_is_caught(tmp_path):
    """A file the manifest never mentions is the one nobody hashes."""
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())
    (package / "echocast.db").write_bytes(b"SQLite format 3\x00")
    verified = _run(VERIFY, ["-PackagePath", str(package)])
    assert verified.returncode != 0
    assert "ECHOCAST_HQ_PACKAGE_VERIFIED" not in verified.stdout


@windows_only
@needs_runtime
@needs_frontend
def test_the_manifest_records_version_and_commit(tmp_path):
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.0.0-test"
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source_commit"])
    assert manifest["runtime"]["pe_subsystem"] == 2


@windows_only
@needs_runtime
@needs_frontend
def test_the_manifest_names_nobody(tmp_path):
    runtime = _built_runtime()
    _run(BUILD, ["-RuntimePath", str(runtime.parent), "-OutputRoot", str(tmp_path),
                 "-Version", "0.0.0-test", "-AllowDirtyTree"])
    package = next(item for item in tmp_path.iterdir() if item.is_dir())
    text = (package / "manifest.json").read_text(encoding="utf-8")
    assert os.environ.get("USERNAME", "\x00nobody\x00") not in text
    assert "C:\\\\Users\\\\" not in text
