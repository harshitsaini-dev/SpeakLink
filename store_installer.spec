# PyInstaller spec for SpeakLinkStoreInstaller.exe
#
# ONE FILE, and that is the whole requirement. A Store PC gets a single
# executable: no zip to unpack into the right folder, no script Windows opens
# in Notepad, no runbook. Everything the Receiver needs travels inside it.
#
# ONEFILE HERE, ONEDIR FOR THE RECEIVER - and the difference is deliberate.
# The Receiver runs for a whole trading day and resolves FFmpeg relative to its
# own location, so it must live in a stable directory; a onefile build would
# unpack it into %TEMP% afresh on every launch. The installer runs for ninety
# seconds and then exits, so unpacking to %TEMP% costs nothing and buys the
# thing that matters: one file to hand somebody.
#
# THE PAYLOAD
#
# store-payload.zip is the built Store Kit - the Receiver, FFmpeg and the
# enrolment wizard. It is produced by scripts/Build-SpeakLinkStoreInstaller.ps1
# immediately before this spec runs, and its absence is a build error rather
# than a silent installer that installs nothing.

import os
from pathlib import Path

REPOSITORY_ROOT = Path(os.getcwd())
PAYLOAD = REPOSITORY_ROOT / "build" / "store-payload.zip"
ICON = REPOSITORY_ROOT / "assets" / "speaklink.ico"

if not PAYLOAD.exists():
    raise SystemExit(
        f"The installer payload is missing: {PAYLOAD}\n"
        "Build it with scripts/Build-SpeakLinkStoreInstaller.ps1, which packs "
        "the Receiver and the enrolment wizard before calling PyInstaller.")

block_cipher = None

analysis = Analysis(
    [str(REPOSITORY_ROOT / "tools" / "store_installer.py")],
    pathex=[str(REPOSITORY_ROOT / "tools")],
    binaries=[],
    # Carried as data rather than compiled in: it is 120MB of executables and
    # a codec, and PyInstaller handles it as an opaque blob either way.
    # The payload, and the icon - the icon TWICE over, in effect: icon= below
    # sets what Explorer draws on the file, and this copy is what the window
    # itself loads at runtime. They are different mechanisms and a build that
    # sets only the first ships an installer whose window wears a stranger's
    # face.
    datas=[(str(PAYLOAD), "."), (str(ICON), ".")],
    hiddenimports=["tkinter", "tkinter.messagebox", "tkinter.scrolledtext"],
    hookspath=[],
    runtime_hooks=[],
    # The installer is tkinter and the standard library. Excluding the science
    # stack keeps it from silently absorbing whatever else is in the build
    # environment's site-packages.
    excludes=["numpy", "matplotlib", "pandas", "scipy", "PIL", "pytest",
              "fastapi", "uvicorn", "sqlalchemy"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(analysis.pure, analysis.zipped_data, cipher=block_cipher)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    [],
    name="SpeakLinkStoreInstaller",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # WINDOWED. A console flashing up on a shop counter, or worse staying
    # there, is how a till ends up with a black window nobody dares close.
    # Command-line use still works: passing an argument runs the CLI path.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # The SpeakLink mark. A build that fell back to PyInstaller's default icon
    # would put a Python logo on a Store desktop, which says the machine is
    # running something other than what it is.
    icon=str(ICON),
)
