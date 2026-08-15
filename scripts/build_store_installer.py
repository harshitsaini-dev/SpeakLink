"""Build SpeakLinkStoreInstaller.exe, and prove it contains what was just written.

THE FAILURE THIS SCRIPT EXISTS FOR

An installer went out as 1.6.3 carrying the enrolment wizard from 1.6.2. Every
step had "succeeded": PyInstaller printed "Build complete", the package script
printed a package path, the file was 132MB and ran. What it did not do was
contain the fix that had been written an hour earlier - PyInstaller reused
cached modules from a work directory it believed was still valid, and nothing
in the chain ever read the artifact back.

So this script does three things the hand-run sequence did not:

  1. ONE VERSION. Everything reads tools/speaklink_version.py. Three
     hand-typed version strings is how a 1.6.3 installer ships a 1.6.2 wizard.

  2. CLEAN WORK DIRECTORIES. Both builds start from nothing. Slower by a
     minute; the alternative is trusting a cache that was already wrong once.

  3. VERIFICATION BY READING THE ARTIFACT. The packaged bytecode is opened and
     the build marker is read back out of it. "It compiled" and "it contains
     what I just wrote" are different claims, and only the second one matters.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from speaklink_version import BUILD_MARKER, STORE_KIT_VERSION  # noqa: E402

PYTHON = ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
KITS = ROOT / "data" / "store-kits"


def say(message: str) -> None:
    print(f"==> {message}", flush=True)


def run(*command: str) -> None:
    result = subprocess.run(command, cwd=str(ROOT))
    if result.returncode != 0:
        raise SystemExit(f"failed: {' '.join(command)}")


def clean(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def pyz_of(executable: Path) -> Path:
    """Extract the PYZ archive out of a frozen executable."""
    sys.path.insert(0, str(ROOT / "backend" / ".venv" / "Lib" / "site-packages"))
    from PyInstaller.archive.readers import CArchiveReader

    reader = CArchiveReader(str(executable))
    for name in reader.toc:
        if str(name).lower().endswith(".pyz"):
            data = reader.extract(name)
            payload = data if isinstance(data, bytes) else data[1]
            target = BUILD / f"{executable.stem}.pyz"
            target.write_bytes(payload)
            return target
    raise SystemExit(f"{executable.name} has no PYZ archive to inspect")


def _digest(code) -> str:
    """A stable fingerprint of a code object and everything nested in it.

    Comparing str(co_consts) does NOT work, and the reason is worth writing
    down because it cost an hour: a nested code object's repr contains its
    memory address, so two compilations of identical source never match. The
    fingerprint walks into nested functions instead, and uses only the parts
    that describe behaviour - the bytecode, the names, and the constants that
    are not themselves code.
    """
    import hashlib
    import types

    sink = hashlib.sha256()

    def absorb(node):
        sink.update(node.co_code)
        sink.update(repr(node.co_names).encode())
        sink.update(repr(node.co_varnames).encode())
        for const in node.co_consts:
            if isinstance(const, types.CodeType):
                absorb(const)
            else:
                sink.update(repr(const).encode())

    absorb(code)
    return sink.hexdigest()


def assert_matches_source(executable: Path, module_key: str, source: Path) -> None:
    """Prove the packaged module IS the current source, not a cached copy.

    Compiling the file on disk and fingerprinting it against what came out of
    the executable is the only check that separates "the build ran" from "the
    build shipped what I wrote". Reading a marker string cannot do it - a stale
    module that imports the marker from elsewhere still mentions it - and
    searching for a phrase cannot either, because a module's own constants do
    not include the ones inside its functions. Both of those were tried here
    and both gave the wrong answer.
    """
    sys.path.insert(0, str(ROOT / "backend" / ".venv" / "Lib" / "site-packages"))
    from PyInstaller.archive.readers import ZlibArchiveReader

    expected = _digest(compile(source.read_text(encoding="utf-8"),
                               "<speaklink>", "exec"))

    archive = ZlibArchiveReader(str(pyz_of(executable)))
    for key in (k for k in archive.toc if k.endswith(module_key)):
        if _digest(archive.extract(key)) == expected:
            say(f"verified {executable.name}: {key} matches {source.name}")
            return

    # The ENTRY script is not in the PYZ. PyInstaller keeps it in the outer
    # archive as marshalled bytecode, so an installer verified only against
    # the PYZ would report "no such module" for its own main file - which is
    # the one most likely to have been edited.
    import marshal
    from PyInstaller.archive.readers import CArchiveReader

    outer = CArchiveReader(str(executable))
    for name in outer.toc:
        if str(name) != module_key:
            continue
        blob = outer.extract(name)
        raw = blob if isinstance(blob, bytes) else blob[1]
        try:
            code = marshal.loads(raw)
        except (ValueError, EOFError):
            continue
        if _digest(code) == expected:
            say(f"verified {executable.name}: entry script {name} matches "
                f"{source.name}")
            return
        raise SystemExit(
            f"{executable.name} was built from STALE sources: its entry "
            f"script does not match the current {source.name}.")

    raise SystemExit(
        f"{executable.name} was built from STALE sources: {candidates} does "
        f"not match the current {source.name}. Delete build/pyi-* and build "
        "again.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receiver-package",
                        help="a built SpeakLinkReceiver-* package to wrap")
    parser.add_argument("--publish", action="store_true",
                        help="replace the kit HQ hands out with this build")
    arguments = parser.parse_args()

    if not PYTHON.exists():
        raise SystemExit(f"no build environment at {PYTHON}")

    say(f"building the Store Kit as {STORE_KIT_VERSION}")

    # ---- 1. the enrolment wizard, from clean
    clean(BUILD / "pyi-storesetup")
    clean(DIST / "SpeakLinkStoreSetup")
    run(str(PYTHON), "-m", "PyInstaller", "--noconfirm", "store_setup.spec",
        "--distpath", str(DIST), "--workpath", str(BUILD / "pyi-storesetup"))
    wizard = DIST / "SpeakLinkStoreSetup" / "SpeakLinkStoreSetup.exe"
    # Both halves of the wizard: the window and the module that talks to HQ.
    # The enrolment fix lived in the second one, and a check on only the first
    # would have passed while shipping the old messages.
    assert_matches_source(wizard, "store_setup_gui",
                          ROOT / "tools" / "store_setup_gui.py")
    assert_matches_source(wizard, "store_setup_core",
                          ROOT / "tools" / "store_setup_core.py")

    # ---- 2. the kit package: wizard + Receiver + scripts
    receiver = arguments.receiver_package
    if not receiver:
        # By the timestamp in the name, for the same reason the Store Setup
        # package is: the git hash sits before the stamp, so alphabetical
        # order is not chronological, and "the newest Receiver" was one sort
        # away from being last month's.
        packages = sorted((ROOT / "artifacts").glob("SpeakLinkReceiver-*"),
                          key=lambda path: path.name.rsplit("-", 2)[-2:])
        if not packages:
            raise SystemExit(
                "no Receiver package in artifacts/. Build one first with "
                "scripts/Build-SpeakLinkReceiver.ps1.")
        receiver = str(packages[-1])
    started_at = time.time()
    say(f"wrapping Receiver package {Path(receiver).name}")
    run("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts" / "Build-SpeakLinkStoreSetupPackage.ps1"),
        "-ReceiverPackagePath", receiver, "-Version", STORE_KIT_VERSION,
        "-AllowDirtyTree")

    # THE ONE THAT WAS JUST BUILT, not the one that sorts last.
    #
    # Package names are SpeakLinkStoreSetup-<version>-<git hash>-<stamp>, and
    # the hash sits BEFORE the timestamp - so alphabetical order is not
    # chronological order. `sorted(...)[-1]` picked a package built half an
    # hour earlier because its hash happened to start with a later letter, and
    # the installer shipped without the fix that had just been written. That
    # is the exact failure this script exists to prevent, arriving through a
    # door it had left open.
    #
    # Sorted on the timestamp the name carries, and then checked against the
    # clock: a package that is not newer than this run started is not this
    # run's package.
    candidates = sorted(
        (ROOT / "artifacts").glob(f"SpeakLinkStoreSetup-{STORE_KIT_VERSION}-*"),
        key=lambda path: path.name.rsplit("-", 2)[-2:])
    if not candidates:
        raise SystemExit("the Store Setup package step produced nothing.")
    package = candidates[-1]
    if package.stat().st_mtime < started_at:
        raise SystemExit(
            f"the newest Store Setup package is {package.name}, which predates "
            "this build. Something failed quietly - refusing to ship a kit "
            "that does not contain what was just written.")

    # ---- 3. the payload the installer carries
    payload = BUILD / "store-payload.zip"
    if payload.exists():
        payload.unlink()
    count = 0
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(package).as_posix())
                count += 1
    say(f"payload: {count} files, {payload.stat().st_size / (1024 * 1024):.1f} MB")

    # THE SCRIPTS INSIDE THE PAYLOAD ARE THE ONES IN THIS REPOSITORY.
    #
    # "It built" and "it contains what I just wrote" are different claims, and
    # this script already checks the second one for its own entry module. It
    # was not checking it for the PowerShell the wizard actually runs - which
    # is where the last two Store-side fixes lived, and where a stale copy
    # went out unnoticed.
    with zipfile.ZipFile(payload) as archive:
        packaged = {name for name in archive.namelist()
                    if name.startswith("scripts/") and name.endswith(".ps1")}
        stale = []
        for name in sorted(packaged):
            source = ROOT / "scripts" / Path(name).name
            if not source.exists():
                continue
            shipped = archive.read(name).decode("utf-8", "replace")
            written = source.read_text(encoding="utf-8")
            if shipped.replace("\r\n", "\n") != written.replace("\r\n", "\n"):
                stale.append(Path(name).name)
        if stale:
            raise SystemExit(
                "the payload carries a different version of: "
                + ", ".join(stale)
                + ". The kit would ship without the change that was just made.")
    say(f"verified {len(packaged)} packaged scripts match the repository")

    # ---- 4. the installer, from clean, and verified
    clean(BUILD / "pyi-installer")
    run(str(PYTHON), "-m", "PyInstaller", "--noconfirm", "store_installer.spec",
        "--distpath", str(DIST), "--workpath", str(BUILD / "pyi-installer"))
    installer = DIST / "SpeakLinkStoreInstaller.exe"
    assert_matches_source(installer, "store_installer",
                          ROOT / "tools" / "store_installer.py")

    say(f"built {installer.name}: "
        f"{installer.stat().st_size / (1024 * 1024):.1f} MB")

    if arguments.publish:
        # HQ holds exactly one kit, so the old one goes.
        KITS.mkdir(parents=True, exist_ok=True)
        for stale in KITS.iterdir():
            if stale.is_file():
                stale.unlink()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        target = KITS / f"SpeakLinkStoreInstaller-{STORE_KIT_VERSION}-{stamp}.exe"
        shutil.copy2(installer, target)
        say(f"published {target.name} - HQ will hand this one out")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
