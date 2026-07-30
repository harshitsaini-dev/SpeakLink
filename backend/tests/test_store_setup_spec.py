"""What store_setup.spec must build. Read, not run - PyInstaller itself is
exercised manually/CI-side, the same split test_hq_runtime.py already uses for
hq_runtime.spec."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPOSITORY_ROOT / "store_setup.spec"


def _text() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_the_spec_exists():
    assert SPEC.exists()


def test_the_spec_builds_a_windowed_setup_wizard():
    spec = _text()
    assert "EchoCastStoreSetup" in spec
    assert "console=False" in spec
    assert "disable_windowed_traceback=True" in spec, (
        "a Store counter is exactly where a modal error box sits unclosed")


def test_the_spec_excludes_the_server_and_test_tooling():
    spec = _text()
    assert "excludes" in spec
    assert "fastapi" in spec
    assert "pytest" in spec


def test_the_spec_entry_point_is_the_gui_module():
    spec = _text()
    assert "store_setup_gui.py" in spec


def test_the_spec_includes_audio_and_receiver_modules():
    spec = _text()
    for module in ("tools.store_setup_core", "tools.receiver_agent",
                  "tools.windows_audio_devices", "sounddevice"):
        assert module in spec
