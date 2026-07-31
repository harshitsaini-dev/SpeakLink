"""Where a packaged SpeakLink application finds the files it ships with.

THE DEFECT THIS REPLACES

``tools/store_setup_core.py`` computed its root as::

    REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

which is the repository when Python imports the module and ``_internal`` when
PyInstaller has frozen it - because the frozen module lives inside the bundle.
So the packaged wizard looked for ``_internal\\artifacts`` and
``_internal\\scripts\\Manage-SpeakLinkStoreReceiverTask.ps1``, found neither, and
an operator was told to hand-create those folders. That "fix" works by making the
wrong path true, and it has to be redone after every rebuild.

``tools/hq_runtime.py`` already solved this with ``_packaged_root()`` and a
docstring describing the same trap. This module is that idea, generalised and
tested, so the two cannot drift apart again.

THE RULE

Resources ship BESIDE the executable, never inside ``_internal``. ``_internal`` is
PyInstaller's private area: its layout is an implementation detail, and a build
that reorganises it silently breaks anything reaching into it.

Four situations, one answer each:

* **source / development** - the repository root, so a developer runs the same
  code paths without building anything;
* **one-folder frozen** - the folder holding the ``.exe``;
* **one-file frozen** - also the folder holding the ``.exe``, NOT ``sys._MEIPASS``.
  ``_MEIPASS`` is a temporary extraction directory that disappears when the
  process exits, so a Scheduled Task pointed at a file inside it would work once
  and then fail;
* **installed** - the same as one-folder, because installation copies the folder.

``SPEAKLINK_RESOURCE_ROOT`` overrides all of it, which is what lets the tests drive
every branch without freezing anything.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Test and deployment override. Absolute path, checked first.
RESOURCE_ROOT_ENV = "SPEAKLINK_RESOURCE_ROOT"

#: Directory names the packaged application expects beside its executable.
RECEIVER_DIRECTORY = "Receiver"
SCRIPTS_DIRECTORY = "scripts"


class ResourceNotFound(FileNotFoundError):
    """A required packaged resource is absent.

    Carries the root that was searched, because "file not found" without the
    directory it looked in is the least useful error message there is - and this
    exact absence is what sent an operator hand-building folders.
    """


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_directory() -> Path:
    """The folder containing the running executable.

    ``sys.executable`` is the interpreter in a checkout and the packaged .exe when
    frozen, which is precisely why it is only consulted when frozen.
    """
    return Path(sys.executable).resolve().parent


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resource_root() -> Path:
    """The one directory every packaged resource is resolved against."""
    override = os.environ.get(RESOURCE_ROOT_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        # Deliberately NOT sys._MEIPASS. In one-file mode _MEIPASS is a temporary
        # extraction directory that is deleted when the process exits; a Scheduled
        # Task registered against a path inside it would run once and then point
        # at nothing.
        return executable_directory()
    return repository_root()


def receiver_root() -> Path:
    """Where the Receiver payload lives: EXEs, FFmpeg, its manifest and hashes."""
    return resource_root() / RECEIVER_DIRECTORY


def scripts_root() -> Path:
    return resource_root() / SCRIPTS_DIRECTORY


def resolve(*parts: str, required: bool = True) -> Path:
    """One packaged file, resolved and optionally required to exist."""
    candidate = resource_root().joinpath(*parts)
    if required and not candidate.exists():
        raise ResourceNotFound(
            f"the packaged file {'/'.join(parts)} is missing from {resource_root()}. "
            "This package is incomplete - do not hand-create the folder; rebuild it "
            "with scripts/Build-SpeakLinkStoreSetupPackage.ps1, which is what puts "
            "these files where the application looks for them."
        )
    return candidate


def script(name: str, *, required: bool = True) -> Path:
    """A packaged PowerShell script.

    In a checkout these live in ``scripts/`` at the repository root, which is the
    same relative location the package uses - so one expression serves both.
    """
    return resolve(SCRIPTS_DIRECTORY, name, required=required)


#: Everything the wizard needs in order to install a Store Receiver. Named here so
#: the build script, the package verifier and the runtime agree by construction
#: rather than by three people remembering the same list.
REQUIRED_SCRIPTS = (
    "Install-SpeakLinkStoreReceiver.ps1",
    "Repair-SpeakLinkStoreReceiver.ps1",
    "Test-SpeakLinkStoreReceiver.ps1",
    "Uninstall-SpeakLinkStoreReceiver.ps1",
    "Manage-SpeakLinkStoreReceiverTask.ps1",
    "SpeakLinkProcessTree.ps1",
)

REQUIRED_RECEIVER_FILES = (
    "SpeakLinkReceiver.exe",
    "SpeakLinkReceiverBackground.exe",
    "manifest.json",
    "SHA256SUMS.txt",
)


def missing_resources() -> "list[str]":
    """Everything required and absent, as relative paths. Empty means complete.

    Used by the wizard at start-up so an incomplete package is reported once, in
    words, instead of failing halfway through an installation.
    """
    missing = []
    receiver = receiver_root()
    if not receiver.is_dir():
        missing.append(f"{RECEIVER_DIRECTORY}/")
    else:
        for name in REQUIRED_RECEIVER_FILES:
            if not (receiver / name).exists():
                missing.append(f"{RECEIVER_DIRECTORY}/{name}")
        if not any(receiver.rglob("ffmpeg.exe")):
            missing.append(f"{RECEIVER_DIRECTORY}/**/ffmpeg.exe")
    for name in REQUIRED_SCRIPTS:
        if not (scripts_root() / name).exists():
            missing.append(f"{SCRIPTS_DIRECTORY}/{name}")
    return missing


def describe() -> dict:
    """Safe diagnostic detail. No secret can appear here - these are paths."""
    return {
        "frozen": is_frozen(),
        "resource_root": str(resource_root()),
        "override_set": bool(os.environ.get(RESOURCE_ROOT_ENV, "").strip()),
        "receiver_root": str(receiver_root()),
        "scripts_root": str(scripts_root()),
        "missing": missing_resources(),
    }
