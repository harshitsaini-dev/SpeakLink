# PyInstaller spec for SpeakLinkReceiver.exe
#
# A Store desktop must need nothing installed: no Python, no pip, no virtual
# environment, no source checkout, no Node. This produces a one-folder package
# that can be copied to a till and run.
#
# One-folder rather than one-file, deliberately. A one-file build unpacks itself
# into %TEMP% on every launch, which means: a slower start, a directory that
# antivirus and cleanup tools both take an interest in, and - the one that
# matters here - the packaged FFmpeg ending up somewhere different from the
# executable each run. The Agent resolves FFmpeg relative to its own location,
# and a stable location is worth more than a single file.
#
# THE IMPORT PROBLEM THIS SOLVES
#
# tools/audio_receiver_pilot.py reaches the backend's audio modules by putting
# ../backend on sys.path at import time:
#
#     BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
#     sys.path.insert(0, str(BACKEND_DIR))
#     from audio_protocol import ...
#     from audio_streaming import ...
#
# In a frozen bundle there is no ../backend, so that insert finds nothing and
# the imports would fail. Naming them as hidden imports with `backend` on the
# search path bundles them as ordinary top-level modules, so the same import
# statements resolve - and the now-pointless sys.path.insert is harmless.
#
# `tools` is a namespace package with no __init__.py, which is why each module
# under it is named individually rather than collected as a package.

from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs

REPOSITORY_ROOT = Path(SPECPATH).resolve()
BACKEND = REPOSITORY_ROOT / "backend"
TOOLS = REPOSITORY_ROOT / "tools"

# sounddevice is a single module, not a package, so PyInstaller's collectors
# find nothing for it. Its PortAudio DLLs live in a separate _sounddevice_data
# package and have to be named. Without them, hardware sink mode raises at the
# moment an operator tries to use it - null-sink mode never touches them.
portaudio = collect_dynamic_libs("_sounddevice_data")
_data_root = BACKEND / ".venv" / "Lib" / "site-packages" / "_sounddevice_data"
if not portaudio and _data_root.exists():
    portaudio = [
        (str(dll), "_sounddevice_data/portaudio-binaries")
        for dll in (_data_root / "portaudio-binaries").glob("*.dll")
    ]

analysis = Analysis(
    [str(TOOLS / "receiver_agent.py")],
    pathex=[str(REPOSITORY_ROOT), str(BACKEND), str(TOOLS)],
    binaries=portaudio,
    datas=[],
    hiddenimports=[
        # Reached through tools/ which has no __init__.py.
        "tools.receiver_agent",
        "tools.audio_receiver_pilot",
        "tools.receiver_credential_store",
        "tools.windows_audio_devices",
        # Backend modules imported after a sys.path insert that does not exist
        # in a bundle. See the note above.
        "audio_protocol",
        "audio_streaming",
        # Imported lazily inside functions, so the analyser cannot see them.
        "websockets",
        "websockets.client",
        "websockets.asyncio.client",
        "sounddevice",
        "_sounddevice_data",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing that would drag the HQ server, its database or its test suite
    # onto a till. A Store desktop has no business carrying any of it.
    excludes=[
        "server", "db", "models", "schemas", "seed", "auth", "rbac",
        "migrations", "store_lifecycle", "receiver_primary_device",
        "fastapi", "starlette", "sqlalchemy", "uvicorn", "jwt", "bcrypt",
        "pytest", "_pytest", "requests",
        "tkinter", "matplotlib", "numpy", "PIL",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="SpeakLinkReceiver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SpeakLinkReceiver",
)
