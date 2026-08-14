"""The Store Receiver answering HQ's announcement and speaker commands.

The two properties worth the most here:

  * switching speaker opens the NEW device before letting go of the old one,
    so a device that cannot be opened leaves the shop playing rather than
    silent;
  * the new selection is SAVED. Without that the next restart quietly returns
    the shop to its old speaker, hours later, with nobody connecting the
    silence to a change made from HQ that morning.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.audio_receiver_pilot import AudioReceiverPilot  # noqa: E402
from tools.windows_audio_devices import (  # noqa: E402
    AudioDeviceError, OutputDevice)


class FakeConnection:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        import json
        self.sent.append(json.loads(payload))

    send_text = send


def make_pilot(tmp_path, **overrides):
    pilot = AudioReceiverPilot(ws_url="ws://hq.example:8000/ws/receiver")
    pilot.state_root = tmp_path
    pilot._credential = "the-device-credential"
    for name, value in overrides.items():
        setattr(pilot, name, value)
    return pilot


def sent_of(pilot, connection):
    return connection.sent


def run(coroutine):
    return asyncio.run(coroutine)


def device(index=3, name="Speakers (Realtek(R) Audio)"):
    return OutputDevice(index=index, name=name, host_api="MME",
                        max_output_channels=2, default_samplerate=48000,
                        is_default=True)


# ===========================================================================
# Where a Store fetches from
# ===========================================================================

def test_recordings_are_fetched_from_the_hq_this_store_is_connected_to(tmp_path):
    """Derived from the socket URL rather than configured separately, so a
    Store cannot fetch recordings from one HQ while taking commands from
    another."""
    pilot = make_pilot(tmp_path)
    assert pilot._announcement_backend_url() == "http://hq.example:8000"

    secure = make_pilot(tmp_path)
    secure.ws_url = "wss://hq.example.com/ws/receiver"
    assert secure._announcement_backend_url() == "https://hq.example.com"


def test_a_recording_that_cannot_be_fetched_is_reported_not_swallowed(tmp_path):
    """Without this message HQ shows PLAYING for a shop playing nothing, and
    silence is the one failure HQ cannot see for itself."""
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()

    run(pilot._on_announcement_play(connection, {
        "audio_id": 4, "sha256": "not-a-hash",
        "download_path": "/api/receiver/announcements/4/download",
        "volume_percent": 80}))

    assert connection.sent, "the Store said nothing at all"
    message = connection.sent[-1]
    assert message["type"] == "announcement_failed"
    assert message["audio_id"] == 4
    assert message["error"]


def test_pausing_answers_with_the_reason_it_was_given(tmp_path):
    """A Store log that cannot tell a person pausing from a broadcast arriving
    cannot answer "why did it go quiet at 4pm"."""
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()
    run(pilot._on_announcement_pause(connection, {"reason": "broadcast"}))
    assert connection.sent[-1] == {"type": "announcement_paused",
                                   "reason": "broadcast"}


def test_setting_the_volume_does_not_answer_with_a_play(tmp_path):
    """Restating what is playing would restart the recording."""
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()
    run(pilot._on_announcement_set_volume(connection, {"volume_percent": 25}))
    assert pilot._announcement_volume_percent == 25
    assert connection.sent == []


def test_a_volume_that_is_not_a_number_changes_nothing(tmp_path):
    pilot = make_pilot(tmp_path)
    pilot._announcement_volume_percent = 60
    run(pilot._on_announcement_set_volume(FakeConnection(),
                                          {"volume_percent": "loud"}))
    assert pilot._announcement_volume_percent == 60


# ===========================================================================
# Speakers
# ===========================================================================

def test_the_store_reports_the_speakers_it_actually_has(tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()
    monkeypatch.setattr("tools.windows_audio_devices.list_output_devices",
                        lambda: (device(), device(5, "Bluetooth Headset")))

    run(pilot._on_list_output_devices(connection, {}))
    message = connection.sent[-1]
    assert message["type"] == "output_devices"
    assert [entry["name"] for entry in message["devices"]] == [
        "Speakers (Realtek(R) Audio)", "Bluetooth Headset"]
    assert message["devices"][0]["verified_selector"].startswith("index:3@")


def test_a_computer_that_cannot_enumerate_says_so_rather_than_claiming_none(
        tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()

    def refuse():
        raise AudioDeviceError("this build has no audio backend")

    monkeypatch.setattr("tools.windows_audio_devices.list_output_devices", refuse)
    run(pilot._on_list_output_devices(connection, {}))
    message = connection.sent[-1]
    assert message["devices"] == []
    assert "no audio backend" in message["error"]


def test_an_unresolvable_selector_is_refused_before_anything_changes(
        tmp_path, monkeypatch):
    pilot = make_pilot(tmp_path)
    original = object()
    pilot.pcm_sink = original
    connection = FakeConnection()

    def refuse(selector):
        raise AudioDeviceError(f"{selector!r} matches no device")

    monkeypatch.setattr("tools.windows_audio_devices.resolve_output_device", refuse)
    run(pilot._on_set_output_device(connection, {"selector": "index:99"}))

    assert connection.sent[-1]["result"] == "refused"
    assert pilot.pcm_sink is original, "the Store gave up its speaker anyway"


def test_a_speaker_that_cannot_be_opened_leaves_the_shop_playing(
        tmp_path, monkeypatch):
    """The order is the whole point: open the new device first, so a failure
    means the Store keeps the one it has instead of being left with nothing
    while HQ believes the change worked."""
    pilot = make_pilot(tmp_path)
    original = object()
    pilot.pcm_sink = original
    connection = FakeConnection()
    monkeypatch.setattr("tools.windows_audio_devices.resolve_output_device",
                        lambda selector: device())

    def cannot_open(configuration, **kwargs):
        raise RuntimeError("the endpoint is in exclusive use")

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", cannot_open)
    run(pilot._on_set_output_device(connection, {"selector": "index:3"}))

    message = connection.sent[-1]
    assert message["result"] == "refused"
    assert "could not be opened" in message["error"]
    assert pilot.pcm_sink is original


def test_a_successful_switch_names_the_speaker_it_ended_up_on(
        tmp_path, monkeypatch):
    """"applied" on its own is not an answer to "which speaker is the shop
    on", and nobody at HQ can hear it."""
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()
    chosen = device(5, "Bluetooth Headset")
    monkeypatch.setattr("tools.windows_audio_devices.resolve_output_device",
                        lambda selector: chosen)

    class OpenedSink:
        def __init__(self, configuration, **kwargs):
            self.configuration = configuration

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", OpenedSink)
    run(pilot._on_set_output_device(connection, {"selector": "index:5"}))

    message = connection.sent[-1]
    assert message["result"] == "applied"
    assert message["applied_device_name"] == "Bluetooth Headset"
    assert message["applied_selector"] == chosen.verified_selector
    assert pilot.sink.device is chosen


def test_the_new_speaker_is_saved_so_a_restart_does_not_undo_it(
        tmp_path, monkeypatch):
    """Without this the shop returns to its old speaker at the next restart -
    hours later, with nobody connecting the silence to a change made from HQ
    that morning."""
    import json
    from tools.receiver_agent import ReceiverConfig, save_config

    config_path = tmp_path / "config.json"
    save_config(config_path, ReceiverConfig(
        backend_url="http://hq.example:8000", audio_sink="windows",
        audio_output_device="index:3@Speakers (Realtek(R) Audio)"))

    pilot = make_pilot(tmp_path, config_path=config_path)
    chosen = device(5, "Bluetooth Headset")
    monkeypatch.setattr("tools.windows_audio_devices.resolve_output_device",
                        lambda selector: chosen)

    class OpenedSink:
        def __init__(self, configuration, **kwargs):
            self.configuration = configuration

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", OpenedSink)
    run(pilot._on_set_output_device(FakeConnection(), {"selector": "index:5"}))

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["audio_output_device"] == chosen.verified_selector


def test_a_store_with_no_config_still_switches_rather_than_failing(
        tmp_path, monkeypatch):
    """Not being able to REMEMBER the choice is not a reason to refuse to MAKE
    it - the shop would stay on the wrong speaker for no benefit."""
    pilot = make_pilot(tmp_path)
    connection = FakeConnection()
    monkeypatch.setattr("tools.windows_audio_devices.resolve_output_device",
                        lambda selector: device())

    class OpenedSink:
        def __init__(self, configuration, **kwargs):
            self.configuration = configuration

        def start(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", OpenedSink)
    run(pilot._on_set_output_device(connection, {"selector": "index:3"}))
    assert connection.sent[-1]["result"] == "applied"
