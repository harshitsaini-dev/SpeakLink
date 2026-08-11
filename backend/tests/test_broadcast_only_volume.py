"""Store output control exists only inside a live broadcast.

THE SCOPE CORRECTION THIS GUARDS

SpeakLink briefly grew a dedicated always-on Master Volume console: a separate
page, a persistent Target per Store, offline pending commands and a background
task that put shops back when their staff changed them. That is gone.

The rule is now simple, and these tests exist to keep it simple: **when no
broadcast is running, SpeakLink does not touch a Store's Windows mixer at all.**
Store staff use Windows normally. The moment a broadcast owns a Store, HQ can
steer its real endpoint - and the moment that broadcast stops, the shop is put
back exactly as it was and left alone.

The tests below are mostly ABSENCE tests, which are easy to write badly. Each
one names the thing that must not exist rather than asserting a vague
negative, so a re-introduction fails loudly instead of quietly passing.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
FRONTEND_SOURCE = REPOSITORY_ROOT / "frontend" / "src"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)


# ===========================================================================
# The always-on feature is really gone
# ===========================================================================
def test_no_master_volume_module_remains():
    for name in ("master_volume_api.py", "store_audio_target.py",
                 "store_audio_pending.py", "target_enforcement.py",
                 "store_master_audio.py"):
        assert not (BACKEND_ROOT / name).exists(), name


def test_no_master_volume_page_or_route_remains():
    assert not (FRONTEND_SOURCE / "pages" / "MasterVolume.jsx").exists()
    assert not (FRONTEND_SOURCE / "components" / "StoreAudioSummary.jsx").exists()

    app = (FRONTEND_SOURCE / "App.js").read_text(encoding="utf-8")
    assert "master-volume" not in app
    assert "MasterVolume" not in app


def test_the_navigation_no_longer_offers_master_volume():
    layout = (FRONTEND_SOURCE / "components" / "Layout.jsx").read_text(encoding="utf-8")
    assert "master-volume" not in layout
    assert "Master Volume" not in layout

    permissions = (FRONTEND_SOURCE / "lib" / "menuPermissions.js").read_text(
        encoding="utf-8")
    # A dead entry here would block a URL nobody can reach, which is harmless
    # but is exactly the unreachable code this refactor is meant to remove.
    assert "master-volume" not in permissions


def test_no_server_route_serves_always_on_volume():
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for gone in ("/store-audio/master", "read_master_volume", "set_master_volume",
                 "_enforce_targets_once", "_target_enforcement_loop",
                 "_apply_target_master_volume", "store_audio_target",
                 "target_enforcement", "store_master_audio"):
        assert gone not in source, gone


def test_no_target_or_pending_concept_survives_in_the_backend():
    """The vocabulary itself must go, or it grows back."""
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for word in ("desired_volume_percent", "target_volume_percent",
                 "WAITING_FOR_SYNC", "pending_volume_percent",
                 "ENFORCEMENT_SUSPENDED"):
        assert word not in source, word


def test_no_schema_describes_a_target_state():
    schemas = (BACKEND_ROOT / "schemas.py").read_text(encoding="utf-8")
    for word in ("MasterVolume", "target_volume_percent", "sync_state"):
        assert word not in schemas, word


def test_the_receiver_only_observes_inside_a_broadcast():
    """Observation starts at PREPARE and is detached at restoration."""
    pilot = (REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py").read_text(
        encoding="utf-8")
    assert "_start_endpoint_observer" in pilot
    # The always-on entry points are gone, so nothing can start observation
    # from a mere connection.
    for gone in ("ensure_endpoint_observer", "stop_endpoint_observer",
                 "read_endpoint_state_now", "_report_endpoint_state_now"):
        assert gone not in pilot, gone

    agent = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(
        encoding="utf-8")
    for gone in ("ensure_endpoint_observer", "_report_endpoint_state_now"):
        assert gone not in agent, gone


def test_a_control_command_must_name_a_broadcast():
    """A sessionless command is refused by construction, not by a check."""
    from audio_protocol import AudioProtocolError, build_set_audio_control_message

    with pytest.raises(AudioProtocolError):
        build_set_audio_control_message(
            session_id=None, command_id=1, volume_percent=50, muted=False)


def test_telemetry_must_name_a_broadcast():
    from pydantic import ValidationError

    from receiver_contract import EndpointStateAcknowledgement

    with pytest.raises(ValidationError):
        EndpointStateAcknowledgement(
            protocol_version="1.0",
            message_id="0f0d3b3a-1b2c-4d5e-8f90-a1b2c3d4e5f6",
            occurred_at="2026-08-06T10:00:00+00:00",
            sequence=4, type="endpoint_state",
            state_sequence=1, volume_percent=25, muted=False,
        )


def test_a_receiver_ignores_a_command_when_no_broadcast_is_running():
    """The Store-side half of the same rule."""
    import asyncio

    from tools.audio_receiver_pilot import AudioReceiverPilot

    pilot = AudioReceiverPilot(ws_url="ws://test/ws")
    pilot.session_id = None                      # nothing on air
    pilot.windows_endpoint_id = "{0.0.0.00000000}.{aaaa-bbbb}"

    sent = []

    async def capture(_connection, message):
        sent.append(message)

    pilot._send = capture
    asyncio.run(pilot._on_set_audio_control(object(), {
        "type": "set_audio_control", "session_id": 7, "command_id": 1,
        "volume_percent": 50, "muted": False,
    }))
    assert sent == [], "no broadcast, no mixer command"


# ===========================================================================
# What the live database is left holding
# ===========================================================================
def test_startup_no_longer_creates_the_removed_tables():
    """Dormant is fine; recreating them would not be.

    The two tables reached the live database before this scope correction and
    hold real operator rows, so they are deliberately left alone rather than
    dropped. What must not happen is the application creating or touching them
    again - that is what makes them dormant rather than merely unused.
    """
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for gone in ("ensure_target_audio_schema", "ensure_pending_audio_schema",
                 "store_audio_target_state", "store_audio_pending_commands"):
        assert gone not in source, gone
