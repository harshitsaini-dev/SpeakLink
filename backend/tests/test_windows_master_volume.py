"""HQ's per-Store volume now moves the Store's WINDOWS MASTER volume.

WHAT CHANGED, AND WHAT IT COSTS

Until now the Receiver scaled its own decoded PCM and never touched the system
mixer. A Store user who muted Windows therefore silenced every announcement
while HQ cheerfully reported "Applied 100%". That limitation is why this
reversal was accepted, and the price is stated plainly rather than discovered:

  * other applications on the same endpoint are affected;
  * the change is persistent Windows state, so it must be captured before the
    first mutation, restored afterwards, and recoverable after a crash;
  * it is still not the amplifier, and no test here is acoustic evidence.

WHY A FAKE ENDPOINT AND NOT REAL CORE AUDIO

These tests must never move the volume of the machine running them. The fake
below records what was asked of it and answers like Windows does - including
clamping - so ordering, restoration and refusal are all exercised without a
single real endpoint being touched.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from tools import windows_endpoint_restore as restore  # noqa: E402
from tools import windows_endpoint_volume as endpoint  # noqa: E402


BP_ID = "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}"
RG_ID = "{0.0.0.00000000}.{bbbbbbbb-5555-6666-7777-888888888888}"


class FakeEndpoint:
    """One Windows output, as Core Audio behaves."""

    def __init__(self, volume_percent=50, muted=False):
        self.volume_percent = volume_percent
        self.muted = muted
        self.writes = []

    def GetMasterVolumeLevelScalar(self):
        return self.volume_percent / 100.0

    def GetMute(self):
        return 1 if self.muted else 0

    def SetMasterVolumeLevelScalar(self, scalar, _context):
        # Windows clamps; so does this, so a test cannot pass on a value the
        # real API would have refused.
        self.volume_percent = max(0, min(100, round(scalar * 100)))
        self.writes.append(("volume", self.volume_percent))

    def SetMute(self, muted, _context):
        self.muted = bool(muted)
        self.writes.append(("mute", self.muted))


class FakeBackend:
    """Stands in for Core Audio. Knows only the endpoints it was given."""

    def __init__(self, endpoints: dict, names: dict | None = None):
        self.endpoints = endpoints
        self.names = names or {}

    def controller(self, endpoint_id):
        if endpoint_id not in self.endpoints:
            raise endpoint.EndpointNotFound(endpoint_id)
        return self.endpoints[endpoint_id]

    def list_endpoints(self):
        return [{"endpoint_id": eid, "name": self.names.get(eid, eid)}
                for eid in self.endpoints]


@pytest.fixture()
def bp():
    return FakeEndpoint(volume_percent=10, muted=False)


@pytest.fixture()
def backend(bp):
    return FakeBackend({BP_ID: bp}, {BP_ID: "Speakers (Realtek(R) Audio)"})


# ===========================================================================
# Reading and writing the master
# ===========================================================================
def test_master_volume_and_mute_are_readable(backend):
    state = endpoint.read_state(BP_ID, backend=backend)
    assert state.volume_percent == 10
    assert state.muted is False
    assert state.endpoint_id == BP_ID


@pytest.mark.parametrize("percent", [0, 30, 60, 90, 100])
def test_hq_volume_becomes_the_windows_master(backend, bp, percent):
    """The whole point: HQ 30 means the Store's Windows master is 30."""
    applied = endpoint.apply_state(BP_ID, volume_percent=percent, backend=backend)
    assert bp.volume_percent == percent
    assert applied.volume_percent == percent


def test_mute_and_unmute_move_the_windows_master_mute(backend, bp):
    endpoint.apply_state(BP_ID, muted=True, backend=backend)
    assert bp.muted is True
    endpoint.apply_state(BP_ID, muted=False, backend=backend)
    assert bp.muted is False


def test_unmuting_keeps_the_level_hq_last_chose(backend, bp):
    endpoint.apply_state(BP_ID, volume_percent=65, backend=backend)
    endpoint.apply_state(BP_ID, volume_percent=65, muted=True, backend=backend)
    assert bp.muted is True and bp.volume_percent == 65
    unmuted = endpoint.apply_state(BP_ID, volume_percent=65, muted=False,
                                   backend=backend)
    assert unmuted.muted is False
    assert unmuted.volume_percent == 65, "unmute must not jump to full volume"


def test_the_reported_value_is_read_back_not_echoed(backend, bp):
    """Windows can clamp or a policy can override; 'applied' must mean the
    endpoint reported it afterwards, not that a call returned."""
    class Stubborn(FakeEndpoint):
        def SetMasterVolumeLevelScalar(self, scalar, _context):
            self.volume_percent = 42          # refuses to go where asked

    stubborn = Stubborn(volume_percent=42)
    applied = endpoint.apply_state(
        BP_ID, volume_percent=90,
        backend=FakeBackend({BP_ID: stubborn}))
    assert applied.volume_percent == 42, "the reported value must come from Windows"


@pytest.mark.parametrize("bad", [-1, 101, 1000, True, "80", 3.5])
def test_a_value_outside_the_contract_is_refused(backend, bp, bad):
    with pytest.raises(endpoint.EndpointControlError):
        endpoint.apply_state(BP_ID, volume_percent=bad, backend=backend)
    assert bp.volume_percent == 10, "a refusal must change nothing"


# ===========================================================================
# Identity - the dangerous part
# ===========================================================================
def test_an_unknown_endpoint_is_refused_and_nothing_else_is_touched(backend, bp):
    """Never fall back to the default device.

    Falling back is the single most dangerous thing this code could do: it
    would move the master volume of whatever output happened to be default,
    on a computer nobody is watching.
    """
    with pytest.raises(endpoint.EndpointNotFound):
        endpoint.apply_state("{0.0.0.00000000}.{gone}", volume_percent=80,
                             backend=backend)
    assert bp.volume_percent == 10
    assert bp.writes == []


def test_one_stores_endpoint_is_independent_of_another(backend):
    rg = FakeEndpoint(volume_percent=55)
    both = FakeBackend({BP_ID: backend.endpoints[BP_ID], RG_ID: rg})
    endpoint.apply_state(BP_ID, volume_percent=30, backend=both)
    assert both.endpoints[BP_ID].volume_percent == 30
    assert rg.volume_percent == 55, "another Store must not move"


def test_a_playback_device_resolves_to_exactly_one_endpoint(backend):
    assert endpoint.resolve_endpoint_for_playback_device(
        "Speakers (Realtek(R) Audio)", backend=backend) == BP_ID


def test_two_endpoints_with_the_same_name_are_refused_not_guessed():
    """A wrong answer here is permanent, silent and points at real hardware."""
    ambiguous = FakeBackend(
        {BP_ID: FakeEndpoint(), RG_ID: FakeEndpoint()},
        {BP_ID: "Speakers (Realtek(R) Audio)", RG_ID: "Speakers (Realtek(R) Audio)"})
    with pytest.raises(endpoint.EndpointAmbiguous):
        endpoint.resolve_endpoint_for_playback_device(
            "Speakers (Realtek(R) Audio)", backend=ambiguous)


def test_a_device_that_matches_nothing_is_refused(backend):
    with pytest.raises(endpoint.EndpointAmbiguous):
        endpoint.resolve_endpoint_for_playback_device("Nothing Like This",
                                                      backend=backend)


def test_a_portaudio_index_is_never_accepted_as_an_endpoint(backend, bp):
    """index:N renumbers when hardware changes. It must never reach control."""
    with pytest.raises(endpoint.EndpointNotFound):
        endpoint.apply_state("index:3", volume_percent=80, backend=backend)
    assert bp.writes == []


# ===========================================================================
# The restoration record
# ===========================================================================
def test_the_record_round_trips(tmp_path):
    path = tmp_path / restore.RECORD_NAME
    restore.write_record(path, session_id=77, endpoint_id=BP_ID,
                         original_volume_percent=13, original_muted=True)
    record = restore.read_record(path)
    assert record.session_id == 77
    assert record.endpoint_id == BP_ID
    assert record.original_volume_percent == 13
    assert record.original_muted is True


def test_the_record_holds_no_secret(tmp_path):
    """It is read by support and pasted into diagnostics bundles."""
    path = tmp_path / restore.RECORD_NAME
    restore.write_record(path, session_id=77, endpoint_id=BP_ID,
                         original_volume_percent=13, original_muted=True)
    raw = path.read_text(encoding="utf-8").lower()
    for leak in ("credential", "token", "password", "secret", "bearer",
                 "verifier", "speaklink_rcv_v1"):
        assert leak not in raw, leak
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "version", "session_id", "endpoint_id", "original_volume_percent",
        "original_muted", "written_at_utc"}


def test_a_corrupt_record_is_an_error_not_a_shrug(tmp_path):
    """Treating it as 'nothing to restore' leaves a shop loud and says nothing."""
    path = tmp_path / restore.RECORD_NAME
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(restore.RestoreRecordError):
        restore.read_record(path)


def test_no_record_is_simply_nothing_to_do(tmp_path):
    assert restore.read_record(tmp_path / restore.RECORD_NAME) is None


# ===========================================================================
# Crash recovery
# ===========================================================================
def test_startup_restores_a_state_a_previous_run_never_put_back(tmp_path, backend, bp):
    path = tmp_path / restore.RECORD_NAME
    restore.write_record(path, session_id=77, endpoint_id=BP_ID,
                         original_volume_percent=13, original_muted=True)
    bp.volume_percent, bp.muted = 82, False       # left at announcement level

    summary = restore.recover_on_startup(
        path, apply_state=lambda eid, **kw: endpoint.apply_state(
            eid, backend=backend, **kw))

    assert summary["recovered"] is True
    assert bp.volume_percent == 13 and bp.muted is True
    assert not path.exists(), "the record is cleared only after success"


def test_a_failed_recovery_keeps_the_record_for_next_time(tmp_path, backend, bp):
    """The endpoint may be unplugged now and back in ten minutes."""
    path = tmp_path / restore.RECORD_NAME
    restore.write_record(path, session_id=77, endpoint_id="{0.0.0.00000000}.{gone}",
                         original_volume_percent=13, original_muted=True)

    summary = restore.recover_on_startup(
        path, apply_state=lambda eid, **kw: endpoint.apply_state(
            eid, backend=backend, **kw))

    assert summary["recovered"] is False
    assert path.exists(), "forgetting here would strand the Store loud for good"
    assert bp.writes == [], "another output must not be touched"


def test_recovery_with_no_record_is_not_an_error(tmp_path, backend):
    summary = restore.recover_on_startup(
        tmp_path / restore.RECORD_NAME,
        apply_state=lambda eid, **kw: endpoint.apply_state(eid, backend=backend, **kw))
    assert summary["recovered"] is False


# ===========================================================================
# No double attenuation
# ===========================================================================
def test_the_pcm_gain_no_longer_follows_the_hq_slider():
    """HQ 50 must mean 50, not 50% of 50%.

    The PCM path is kept - it is the only way to silence SpeakLink without
    touching a shared endpoint - but nothing moves it off unity today.
    """
    import inspect

    from tools import audio_receiver_pilot

    source = inspect.getsource(audio_receiver_pilot.AudioReceiverPilot._on_set_audio_control)
    assert "windows_endpoint_id" in source
    assert "pcm_sink.set_audio_control" not in source, (
        "the HQ slider must not also scale the PCM, or 50% becomes 25%")


def test_the_sink_still_starts_at_unity():
    from tools.audio_receiver_pilot import SinkConfiguration, WindowsPcmSink
    from tools.windows_audio_devices import OutputDevice

    device = OutputDevice(index=1, name="Test", host_api="MME",
                          max_output_channels=1, default_samplerate=48000,
                          is_default=False)
    sink = WindowsPcmSink(SinkConfiguration(sink_mode="windows", device=device),
                          backend=object())
    assert sink.volume_percent == 100
    assert sink.muted is False
    assert sink.effective_percent == 100
