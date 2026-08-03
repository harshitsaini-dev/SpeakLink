# PyInstaller spec for SpeakLinkStoreSetup.exe
#
# One windowed first-run wizard for a Store till. Windowed is the whole point:
# a Store counter should show no console, and Windows decides that from the PE
# Subsystem field, not from how the process was launched.
#
# `disable_windowed_traceback=True` because a modal error box on a Store
# counter is a black window nobody in the Store can close either - the same
# defect this whole line of work keeps finding and fixing.

from pathlib import Path

REPOSITORY_ROOT = Path(SPECPATH)

#: The SpeakLink Windows icon, derived from the website favicon by
#: tools/build_windows_icon.py. One asset for every shipped executable: a
#: build that quietly fell back to PyInstaller's default icon would put a
#: Python logo on a Store PC desktop.
ICON = str(REPOSITORY_ROOT / "assets" / "speaklink.ico")
BACKEND = REPOSITORY_ROOT / "backend"
TOOLS = REPOSITORY_ROOT / "tools"

analysis = Analysis(
    [str(TOOLS / "store_setup_gui.py")],
    pathex=[str(REPOSITORY_ROOT), str(BACKEND), str(TOOLS)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tools.store_setup_gui",
        "tools.store_setup_core",
        # store_setup_core imports it at module level, so the analysis would
        # find it anyway. Named explicitly because the whole Settings Password
        # gate lives in it: if it ever failed to bundle, the frozen Store Kit
        # would raise on import rather than quietly running unprotected - but
        # this list is where a reader looks to confirm it travels.
        "tools.store_kit_settings_password",
        "tools.receiver_agent",
        "tools.receiver_credential_store",
        "tools.audio_receiver_pilot",
        "tools.windows_audio_devices",
        "sounddevice",
    ],
    hookspath=[],
    runtime_hooks=[],
    # StoreSetup calls Install-SpeakLinkStoreReceiver.ps1 as a CHILD process and
    # never imports FastAPI/uvicorn/pytest - it configures and enrols a
    # Receiver, it does not run one.
    excludes=[
        "fastapi", "starlette", "sqlalchemy", "uvicorn", "jwt", "bcrypt",
        "pytest", "_pytest", "matplotlib",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

setup_executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SpeakLinkStoreSetup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    icon=ICON,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    setup_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SpeakLinkStoreSetup",
)
