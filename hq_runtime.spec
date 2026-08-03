# PyInstaller spec for EchoCastHQRuntime.exe
#
# One windowed supervisor for an HQ machine. Windowed is the whole point: an HQ
# desk should show no console, and Windows decides that from the PE Subsystem
# field, not from how the process was launched.
#
# `disable_windowed_traceback=True` because an unattended HQ desk is exactly
# where a modal error box sits unclosed for a week with the runtime dead behind
# it. Failures belong in the rotating log.

from pathlib import Path

REPOSITORY_ROOT = Path(SPECPATH)

#: The EchoCast Windows icon, derived from the website favicon by
#: tools/build_windows_icon.py. One asset for every shipped executable: a
#: build that quietly fell back to PyInstaller's default icon would put a
#: Python logo on a Store PC desktop.
ICON = str(REPOSITORY_ROOT / "assets" / "echocast.ico")
BACKEND = REPOSITORY_ROOT / "backend"
TOOLS = REPOSITORY_ROOT / "tools"

analysis = Analysis(
    [str(TOOLS / "hq_runtime.py")],
    pathex=[str(REPOSITORY_ROOT), str(BACKEND), str(TOOLS)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "tools.hq_runtime",
        "tools.persistent_lan_server",
        "tools.receiver_agent",
        "tools.audio_receiver_pilot",
    ],
    hookspath=[],
    runtime_hooks=[],
    # The supervisor starts the backend as a CHILD process using the machine's
    # own Python, so it does not need FastAPI, SQLAlchemy or uvicorn inside
    # itself. Excluding them keeps the executable small and keeps server code
    # off a machine that only supervises.
    excludes=[
        "fastapi", "starlette", "sqlalchemy", "uvicorn", "jwt", "bcrypt",
        "pytest", "_pytest", "requests",
        "tkinter", "matplotlib", "numpy", "PIL",
        "sounddevice",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

runtime_executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="EchoCastHQRuntime",
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
    runtime_executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EchoCastHQRuntime",
)
