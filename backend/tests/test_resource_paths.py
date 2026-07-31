"""A packaged application must find its own files.

THE FAILURE THIS EXISTS TO PREVENT

``store_setup_core`` computed its root as ``Path(__file__).resolve().parents[1]``.
Frozen, that is ``_internal`` - PyInstaller's private area - so the packaged
wizard looked for ``_internal\\artifacts`` and ``_internal\\scripts\\...``, found
neither, and an operator was told to hand-create those folders. That "fix" works
by making the wrong path true and has to be redone after every rebuild.

``hq_runtime`` had already hit this and solved it. The rule was in one module and
not the other, which is the shape this repository keeps rediscovering.
"""

from __future__ import annotations

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
    RECEIVER_DIRECTORY,
    REQUIRED_RECEIVER_FILES,
    REQUIRED_SCRIPTS,
    RESOURCE_ROOT_ENV,
    SCRIPTS_DIRECTORY,
    ResourceNotFound,
)


@pytest.fixture(autouse=True)
def no_override(monkeypatch):
    monkeypatch.delenv(RESOURCE_ROOT_ENV, raising=False)


def complete_package(root: Path) -> Path:
    """A package laid out the way the build script produces it."""
    receiver = root / RECEIVER_DIRECTORY
    (receiver / "_internal").mkdir(parents=True)
    for name in REQUIRED_RECEIVER_FILES:
        (receiver / name).write_text("x", encoding="utf-8")
    (receiver / "_internal" / "ffmpeg.exe").write_text("x", encoding="utf-8")
    scripts = root / SCRIPTS_DIRECTORY
    scripts.mkdir(parents=True)
    for name in REQUIRED_SCRIPTS:
        (scripts / name).write_text("x", encoding="utf-8")
    (root / "EchoCastStoreSetup.exe").write_text("x", encoding="utf-8")
    return root


# ===========================================================================
# 1. Source, frozen one-folder, frozen one-file, installed
# ===========================================================================
def test_in_a_checkout_the_root_is_the_repository():
    assert resource_paths.is_frozen() is False
    assert resource_paths.resource_root() == REPOSITORY_ROOT


def test_frozen_one_folder_uses_the_folder_holding_the_executable(monkeypatch, tmp_path):
    installed = tmp_path / "EchoCastStoreSetup"
    (installed / "_internal").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "EchoCastStoreSetup.exe"))

    assert resource_paths.resource_root() == installed


def test_frozen_never_resolves_into_the_internal_directory(monkeypatch, tmp_path):
    """The exact defect. _internal is PyInstaller's private area; its layout is an
    implementation detail and a build that reorganises it breaks anything reaching
    inside."""
    installed = tmp_path / "EchoCastStoreSetup"
    (installed / "_internal").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "EchoCastStoreSetup.exe"))

    for path in (resource_paths.resource_root(),
                 resource_paths.receiver_root(),
                 resource_paths.scripts_root()):
        assert "_internal" not in path.parts, f"{path} reaches into _internal"


def test_one_file_mode_uses_the_executable_not_the_temporary_extraction(monkeypatch, tmp_path):
    """sys._MEIPASS is deleted when the process exits. A Scheduled Task registered
    against a path inside it would work once and then point at nothing."""
    installed = tmp_path / "installed"
    installed.mkdir()
    meipass = tmp_path / "_MEI123456"
    meipass.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setattr(sys, "executable", str(installed / "EchoCastStoreSetup.exe"))

    assert resource_paths.resource_root() == installed
    assert resource_paths.resource_root() != meipass


def test_an_explicit_override_wins_everywhere(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path))
    assert resource_paths.resource_root() == tmp_path.resolve()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert resource_paths.resource_root() == tmp_path.resolve()


def test_a_blank_override_is_ignored(monkeypatch):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, "   ")
    assert resource_paths.resource_root() == REPOSITORY_ROOT


# ===========================================================================
# 2. Completeness - the package says so itself
# ===========================================================================
def test_a_complete_package_reports_nothing_missing(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(complete_package(tmp_path)))
    assert resource_paths.missing_resources() == []


def test_the_shipped_layout_is_the_one_the_wizard_looks_for(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(complete_package(tmp_path)))
    assert resource_paths.receiver_root() == (tmp_path / RECEIVER_DIRECTORY).resolve()
    assert resource_paths.scripts_root() == (tmp_path / SCRIPTS_DIRECTORY).resolve()


@pytest.mark.parametrize("name", REQUIRED_SCRIPTS)
def test_every_required_script_is_reported_when_absent(monkeypatch, tmp_path, name):
    root = complete_package(tmp_path)
    (root / SCRIPTS_DIRECTORY / name).unlink()
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(root))

    assert f"{SCRIPTS_DIRECTORY}/{name}" in resource_paths.missing_resources()


@pytest.mark.parametrize("name", REQUIRED_RECEIVER_FILES)
def test_every_required_receiver_file_is_reported_when_absent(monkeypatch, tmp_path, name):
    root = complete_package(tmp_path)
    (root / RECEIVER_DIRECTORY / name).unlink()
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(root))

    assert f"{RECEIVER_DIRECTORY}/{name}" in resource_paths.missing_resources()


def test_missing_ffmpeg_is_reported(monkeypatch, tmp_path):
    root = complete_package(tmp_path)
    (root / RECEIVER_DIRECTORY / "_internal" / "ffmpeg.exe").unlink()
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(root))

    assert any("ffmpeg" in entry for entry in resource_paths.missing_resources())


def test_an_entirely_absent_receiver_payload_is_one_clear_line(monkeypatch, tmp_path):
    """This is the state the shipped package was actually in: the wizard alone,
    with nothing to install."""
    scripts = tmp_path / SCRIPTS_DIRECTORY
    scripts.mkdir()
    for name in REQUIRED_SCRIPTS:
        (scripts / name).write_text("x", encoding="utf-8")
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path))

    missing = resource_paths.missing_resources()
    assert f"{RECEIVER_DIRECTORY}/" in missing


# ===========================================================================
# 3. Failing closed, with an error that names the directory
# ===========================================================================
def test_a_missing_file_raises_and_names_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path))
    with pytest.raises(ResourceNotFound) as failure:
        resource_paths.script("Manage-EchoCastStoreReceiverTask.ps1")

    message = str(failure.value)
    assert str(tmp_path) in message, "the error does not say where it looked"
    assert "Build-EchoCastStoreSetupPackage" in message, (
        "the error does not tell the operator to rebuild, which is how the last "
        "one ended up hand-creating folders"
    )


def test_required_false_returns_a_path_without_raising(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(tmp_path))
    candidate = resource_paths.script("Nope.ps1", required=False)
    assert candidate.name == "Nope.ps1"
    assert not candidate.exists()


def test_a_present_script_resolves(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(complete_package(tmp_path)))
    found = resource_paths.script("Manage-EchoCastStoreReceiverTask.ps1")
    assert found.exists()


# ===========================================================================
# 4. In a checkout, the same expressions still work
# ===========================================================================
def test_the_repository_really_holds_every_required_script():
    """The package layout mirrors the checkout, so one expression serves both. If
    a script is renamed in the repository this fails here rather than in a
    package an operator has already copied to a Store."""
    for name in REQUIRED_SCRIPTS:
        assert (REPOSITORY_ROOT / SCRIPTS_DIRECTORY / name).exists(), (
            f"scripts/{name} is named in REQUIRED_SCRIPTS but not in the repository"
        )


def test_describe_is_safe_to_put_in_a_diagnostic(monkeypatch, tmp_path):
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(complete_package(tmp_path)))
    detail = resource_paths.describe()

    assert set(detail) == {"frozen", "resource_root", "override_set",
                           "receiver_root", "scripts_root", "missing"}
    assert detail["missing"] == []
    # Paths only - there is nothing here that could carry a credential.
    assert all(not isinstance(v, bytes) for v in detail.values())
