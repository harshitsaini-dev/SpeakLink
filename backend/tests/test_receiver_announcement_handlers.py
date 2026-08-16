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

        def open(self):
            """`open()`, which is what WindowsPcmSink actually has.

            These doubles said `start()`, so they agreed with a call that
            fails on the real class - and the AttributeError reached a Store
            instead of this file."""

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

        def open(self):
            """`open()`, which is what WindowsPcmSink actually has.

            These doubles said `start()`, so they agreed with a call that
            fails on the real class - and the AttributeError reached a Store
            instead of this file."""

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

        def open(self):
            """`open()`, which is what WindowsPcmSink actually has.

            These doubles said `start()`, so they agreed with a call that
            fails on the real class - and the AttributeError reached a Store
            instead of this file."""

        def close(self):
            pass

    monkeypatch.setattr("tools.audio_receiver_pilot.WindowsPcmSink", OpenedSink)
    run(pilot._on_set_output_device(connection, {"selector": "index:3"}))
    assert connection.sent[-1]["result"] == "applied"


# ===========================================================================
# The speaker an announcement writes to
# ===========================================================================

def test_an_announcement_opens_the_speaker_when_no_broadcast_is_running():
    """THE BUG THIS ENDS.

    `pcm_sink` was opened when a BROADCAST was prepared and closed when it
    stood down. An announcement arriving in between found it None, wrote its
    decoded audio into nothing, and the Receiver then told HQ
    "announcement_playing" - so the console showed a shop playing a promotion
    that could not have made a sound. Every layer was honest about what it had
    been told, and nobody was holding the speaker.
    """
    import asyncio
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = None
    receiver.decoder = None
    receiver.session_id = None
    receiver._announcement = None
    receiver._audio_backend = None
    receiver.sink = type("Config", (), {"is_hardware": True})()

    opened = create_autospec(pilot.WindowsPcmSink, instance=True)
    original = pilot.WindowsPcmSink
    pilot.WindowsPcmSink = lambda *a, **k: opened
    try:
        sink = receiver._ensure_pcm_sink()
    finally:
        pilot.WindowsPcmSink = original

    assert sink is opened
    opened.open.assert_called_once_with()
    assert receiver.pcm_sink is opened


def test_ending_a_broadcast_does_not_close_a_speaker_an_announcement_is_using():
    """A broadcast ending is not a reason for the shop's promotion to stop -
    that is the whole point of ducking, and closing the device here would have
    made the announcement resume into a closed speaker."""
    import asyncio
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    sink = create_autospec(pilot.WindowsPcmSink, instance=True)
    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = sink
    receiver.decoder = None
    receiver.queue = None
    receiver.session_id = 7
    class StillPlaying:
        def paused(self):
            return False

        def wait_until_audible(self, _timeout):
            return True

    receiver._announcement = StillPlaying()    # ducked, still assigned
    # The register is the mechanism: both are using this speaker.
    receiver._pcm_sink_users = {"broadcast", "announcement"}
    receiver.report = {}
    receiver.stood_down = False
    receiver.restore_windows_endpoint = lambda: None
    # The acknowledgement envelope needs these; the test is about the speaker,
    # not about the message, so they are the smallest true values.
    receiver._sequence = 0
    receiver._states = []
    receiver.device_public_id = "dev-test"
    receiver.store_id = 1

    class Silent:
        async def send(self, _message):
            return None

    asyncio.run(receiver._on_stand_down(Silent(), {}))

    sink.close.assert_not_called()
    assert receiver.pcm_sink is sink


def test_stopping_the_announcement_releases_a_speaker_it_opened():
    """A Store with nothing to play should not hold an output device open -
    that is what stops somebody else's application using it."""
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    sink = create_autospec(pilot.WindowsPcmSink, instance=True)
    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = sink
    receiver.decoder = None
    receiver.session_id = None
    receiver._announcement = None
    receiver._pcm_sink_users = {"announcement"}

    receiver._release_announcement_sink()

    sink.close.assert_called_once_with()
    assert receiver.pcm_sink is None


def test_a_speaker_a_broadcast_owns_is_not_closed_by_an_announcement():
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    sink = create_autospec(pilot.WindowsPcmSink, instance=True)
    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = sink
    receiver.decoder = object()                # a broadcast is decoding
    receiver.session_id = 7
    receiver._announcement = None
    receiver._pcm_sink_users = {"broadcast", "announcement"}

    receiver._release_announcement_sink()

    sink.close.assert_not_called()
    assert receiver.pcm_sink is sink


def test_a_broadcast_and_an_announcement_share_one_speaker():
    """The audit found the interaction, not the units.

    Each half was tested on its own and each half was right. Together, a
    broadcast prepared while an announcement was playing opened a SECOND sink
    and left the first orphaned with a pump still writing into it - and the
    flag that was meant to remember who owned what then named the wrong
    object, so stopping the announcement closed the broadcast's device.
    """
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    opened = []
    # Captured BEFORE the patch: once pilot.WindowsPcmSink is the stand-in,
    # autospec would be describing the stand-in rather than the real class.
    real_class = pilot.WindowsPcmSink

    def one_sink(*_args, **_kwargs):
        sink = create_autospec(real_class, instance=True)
        opened.append(sink)
        return sink

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = None
    receiver._audio_backend = None
    receiver.sink = type("Config", (), {"is_hardware": True})()

    original = pilot.WindowsPcmSink
    pilot.WindowsPcmSink = one_sink
    try:
        first = receiver._ensure_pcm_sink("announcement")
        second = receiver._ensure_pcm_sink("broadcast")
    finally:
        pilot.WindowsPcmSink = original

    assert first is second, "a second speaker was opened underneath the first"
    assert len(opened) == 1

    # The broadcast ends. The announcement is still playing, so the speaker
    # stays open - and stays the SAME object.
    receiver._release_pcm_sink("broadcast")
    assert receiver.pcm_sink is first
    first.close.assert_not_called()

    # The announcement ends. Now nobody is using it, and it closes once.
    receiver._release_pcm_sink("announcement")
    first.close.assert_called_once_with()
    assert receiver.pcm_sink is None


def test_a_receiver_with_no_audio_output_says_so_rather_than_claiming_to_play():
    """`_ensure_pcm_sink` returns None where there is no hardware sink. The
    first version built a playback around that None, started it, and sent
    announcement_playing - which is the exact lie this whole feature has been
    chasing."""
    import asyncio
    from tools import audio_receiver_pilot as pilot

    sent = []

    class Connection:
        async def send(self, message):
            sent.append(message)

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = None
    receiver._announcement = None
    receiver._audio_backend = None
    receiver.sink = type("Config", (), {"is_hardware": False})()
    receiver.state_root = None
    receiver._announcement_volume_percent = 80
    receiver._send = lambda connection, payload: connection.send(payload)
    receiver._announcement_state_root = lambda: __import__("pathlib").Path(".")
    receiver._announcement_backend_url = lambda: "http://hq"
    receiver._announcement_credential = lambda: "cred"

    import tools.announcement_player as player
    original = player.fetch_if_absent
    player.fetch_if_absent = lambda **_kwargs: __import__("pathlib").Path("promo.mp3")
    try:
        asyncio.run(receiver._on_announcement_play(
            Connection(), {"audio_id": 3, "sha256": "a" * 64,
                           "download_path": "/api/receiver/announcements/3/download"}))
    finally:
        player.fetch_if_absent = original

    assert sent, "nothing was reported at all"
    assert sent[0]["type"] == "announcement_failed"
    assert "no audio output" in sent[0]["error"]


def test_the_console_volume_moves_the_shop_master_not_only_our_samples():
    """Asked for directly, twice.

    The slider on the Announcements console is what an operator reaches for
    when a shop is too loud. It scaled only the announcement's own samples, so
    the Windows master stayed exactly where it was - the shop's own music
    stayed loud and the change looked like it had done nothing.

    It is NOT restored afterwards, unlike the broadcast's ducking of this same
    control: a broadcast borrows the volume for a minute; this is somebody
    deciding how loud this shop should be.
    """
    import asyncio
    from tools import audio_receiver_pilot as pilot

    applied = {}

    class Playback:
        def set_volume(self, percent):
            applied["software"] = percent

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver._announcement = Playback()
    receiver._announcement_volume_percent = 80
    receiver.windows_endpoint_id = "{endpoint-1}"
    receiver._endpoint_backend = None
    receiver._apply_master_volume = lambda percent: applied.update(master=percent)

    asyncio.run(receiver._on_announcement_set_volume(None, {"volume_percent": 20}))

    assert applied["software"] == 20, "the recording's own level"
    assert applied["master"] == 20, "the shop's master volume"


def test_a_store_with_no_endpoint_still_plays_at_the_software_level():
    """A control that is not the audio must never stop the audio."""
    from tools import audio_receiver_pilot as pilot

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.windows_endpoint_id = None

    receiver._apply_master_volume(30)      # must not raise


def test_a_store_without_a_saved_endpoint_still_gets_its_master_controlled():
    """Reported from a live shop, twice, as two different faults.

    `windows_endpoint_id` is written into config when somebody picks an output
    in Store Setup. A Store set up before that existed has a perfectly good
    speaker and no endpoint id - and then the volume set from HQ never reaches
    the shop, and a change made at the till is never reported back. Both look
    like features that do not work rather than a field nobody filled in.
    """
    from tools import audio_receiver_pilot as pilot

    class Device:
        name = "Speakers (Realtek(R) Audio)"

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.windows_endpoint_id = None
    receiver._endpoint_backend = None
    receiver.sink = type("Config", (), {"device": Device()})()

    import tools.windows_endpoint_volume as volume
    original = volume.resolve_endpoint_for_playback_device
    volume.resolve_endpoint_for_playback_device = (
        lambda name, **_kwargs: type("Endpoint", (), {"endpoint_id": "{ep-42}"})())
    try:
        resolved = receiver._ensure_windows_endpoint()
    finally:
        volume.resolve_endpoint_for_playback_device = original

    assert resolved == "{ep-42}"
    assert receiver.windows_endpoint_id == "{ep-42}", "it should be remembered"


def test_a_store_whose_endpoint_cannot_be_resolved_is_not_broken_by_it():
    """No endpoint means no master control, which is a degradation - not a
    reason to stop playing anything."""
    from tools import audio_receiver_pilot as pilot

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.windows_endpoint_id = None
    receiver._endpoint_backend = None
    receiver.sink = type("Config", (), {"device": None})()

    assert receiver._ensure_windows_endpoint() is None
    receiver._apply_master_volume(50)      # must not raise


def test_every_name_this_module_uses_when_things_go_wrong_exists():
    """`logger` did not exist in audio_receiver_pilot.

    Six calls to it were added with the announcement work, all of them inside
    except blocks - so on a Store, the first time anything went wrong, the
    handler for that failure would itself raise NameError. Tests never saw it
    because tests take the happy path through those lines.

    Compiling the module is not enough; a name is only resolved when the line
    runs. This asserts the module actually has the names its error paths use.
    """
    from tools import audio_receiver_pilot as pilot

    for name in ("logger", "logging"):
        assert hasattr(pilot, name), f"audio_receiver_pilot has no {name}"

    # And it is a real logger, not something that happens to be truthy.
    pilot.logger.debug("exercised by the test suite")


def test_the_level_is_read_even_when_no_observer_is_running():
    """A Store where Core Audio's change notification never arrives.

    The loop used to sleep and continue when there was no observer, which
    skipped the read entirely - so HQ received exactly one level, the one
    taken at connect, and never another. A shop moving its dial from 83 to 100
    changed nothing on the console, and the console was showing a real
    reading, which is the most convincing way to be wrong.
    """
    import inspect
    from tools import audio_receiver_pilot as pilot

    source = inspect.getsource(pilot.AudioReceiverPilot._endpoint_state_loop)
    no_observer = source.index("if observer is None or not observer.started")
    take = source.index("observer.take()")
    poll_in_that_branch = source.index("_report_store_volume_if_changed",
                                       no_observer)
    assert poll_in_that_branch < take, (
        "the no-observer branch must still read the level, or a Store without "
        "a working notification never reports again")


def test_every_connect_path_says_the_same_first_words():
    """A real Store runs DeviceReceiverSession, which OVERRIDES run().

    Everything added to the pilot's run() - the version report, the endpoint
    resolution, the first volume reading - therefore never executed on a shop
    computer. The console then said "Receiver older than 1.7.6" about a Store
    running exactly 1.7.6, which is worse than saying nothing: it accuses the
    person who has just done the install.

    Both paths now call one method. This asserts they still do, because the
    next override will be written by somebody who never read this.
    """
    import inspect
    from tools import audio_receiver_pilot, receiver_agent

    pilot_run = inspect.getsource(audio_receiver_pilot.AudioReceiverPilot.run)
    agent_run = inspect.getsource(receiver_agent.DeviceReceiverSession.run)

    assert "_announce_self" in pilot_run
    assert "_announce_self" in agent_run, (
        "the class a real Store runs must say what the pilot says")

    said = inspect.getsource(audio_receiver_pilot.AudioReceiverPilot._announce_self)
    assert "receiver_version" in said
    assert "_report_store_volume_if_changed" in said


def test_stopping_a_broadcast_leaves_the_announcement_its_speaker():
    """Reported from the estate: after Stop Broadcast the announcement never
    came back.

    Ducking is what makes an announcement stand aside for a live broadcast,
    and HQ dutifully sent the resume when the broadcast ended. The Store
    accepted it and stayed silent - because stopping the broadcast had closed
    the output device the announcement was ducked on. The resume then wrote
    into a closed speaker.

    Stop releases the BROADCAST's claim on the speaker. The announcement's
    claim keeps it open.
    """
    import asyncio
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    sink = create_autospec(pilot.WindowsPcmSink, instance=True)
    sink.frames_written = 1234

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = sink
    receiver.decoder = None
    receiver.queue = None
    receiver.session_id = 7
    receiver.report = {}
    class StillPlaying:
        """The surface the teardown asks about - it now checks whether the
        shop is genuinely audible once the broadcast is gone."""

        def paused(self):
            return False

        def wait_until_audible(self, _timeout):
            return True

    receiver._announcement = StillPlaying()    # ducked, still assigned
    receiver._pcm_sink_users = {"broadcast", "announcement"}
    receiver._sequence = 0
    receiver._states = []
    receiver.device_public_id = "dev-test"
    receiver.store_id = 1
    receiver.restore_windows_endpoint = lambda: None

    class Silent:
        async def send(self, _message):
            return None

    asyncio.run(receiver._on_stop(Silent(), {"session_id": 7}))

    sink.close.assert_not_called()
    assert receiver.pcm_sink is sink
    # And the broadcast's own reporting still happened.
    assert receiver.report["output_frames_written"] == 1234


def test_stopping_a_broadcast_does_close_the_speaker_when_nothing_else_wants_it():
    """A Store with nothing to play must not hold an output device open."""
    import asyncio
    from unittest.mock import create_autospec
    from tools import audio_receiver_pilot as pilot

    sink = create_autospec(pilot.WindowsPcmSink, instance=True)
    sink.frames_written = 10

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver.pcm_sink = sink
    receiver.decoder = None
    receiver.queue = None
    receiver.session_id = 7
    receiver.report = {}
    receiver._announcement = None
    receiver._pcm_sink_users = {"broadcast"}
    receiver._sequence = 0
    receiver._states = []
    receiver.device_public_id = "dev-test"
    receiver.store_id = 1
    receiver.restore_windows_endpoint = lambda: None

    class Silent:
        async def send(self, _message):
            return None

    asyncio.run(receiver._on_stop(Silent(), {"session_id": 7}))

    sink.close.assert_called_once_with()
    assert receiver.pcm_sink is None


def test_after_a_broadcast_the_shop_checks_it_is_really_audible():
    """Reported: stop the broadcast, HQ shows the announcement playing, and
    the shop is silent until somebody presses Pause and then Play - which does
    nothing the resume had not already done, and that is the clue.

    The resume lands while the Receiver is still tearing the broadcast down.
    Rather than reason about the order of two teardowns and a resume, the
    Receiver checks the one thing that matters afterwards: is a frame actually
    reaching the speaker?
    """
    import asyncio
    from tools import audio_receiver_pilot as pilot

    sent = []

    class Connection:
        async def send(self, message):
            sent.append(message)

    class Silent:
        """Started, never audible - the state that used to go unnoticed."""
        _path = __import__("pathlib").Path("promo.mp3")
        _volume_percent = 70
        stopped = False

        def paused(self):
            return False

        def wait_until_audible(self, _timeout):
            return False

        def failure_reason(self):
            return ""

        def stop(self):
            self.__class__.stopped = True

        def start(self):
            return None

    restarted = {}

    class Restarted(Silent):
        def wait_until_audible(self, _timeout):
            return True

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver._announcement = Silent()
    receiver._announcement_volume_percent = 70
    receiver.pcm_sink = object()
    receiver._pcm_sink_users = {"announcement"}
    receiver._send = lambda connection, payload: connection.send(payload)
    receiver._ensure_pcm_sink = lambda user="announcement": receiver.pcm_sink

    import tools.announcement_player as player
    original = player.AnnouncementPlayback

    def rebuild(**kwargs):
        restarted.update(kwargs)
        return Restarted()

    player.AnnouncementPlayback = rebuild
    try:
        asyncio.run(receiver._ensure_announcement_audible(Connection()))
    finally:
        player.AnnouncementPlayback = original

    assert Silent.stopped, "the silent playback should have been torn down"
    assert restarted["volume_percent"] == 70, "it restarts at the shop's level"
    assert sent and sent[-1]["type"] == "announcement_playing"


def test_a_person_who_paused_a_shop_is_not_overruled_by_the_check():
    """The same rule as every other edge of this feature."""
    import asyncio
    from tools import audio_receiver_pilot as pilot

    class Paused:
        def paused(self):
            return True

        def wait_until_audible(self, _timeout):  # pragma: no cover
            raise AssertionError("a paused shop must not be examined further")

    receiver = pilot.AudioReceiverPilot.__new__(pilot.AudioReceiverPilot)
    receiver._announcement = Paused()

    class Connection:
        async def send(self, _message):  # pragma: no cover
            raise AssertionError("nothing should be reported")

    asyncio.run(receiver._ensure_announcement_audible(Connection()))
