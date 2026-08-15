"""The Store side: fetching a recording, caching it, and playing it.

Two properties are worth more than the rest of this file put together:

  * a recording that arrives corrupted is DISCARDED, not cached. A bad file in
    the cache is a Store that stays broken until a person notices, because
    every later play finds it already there and never downloads again.
  * the cache is keyed by content. Re-uploading a recording under the same
    name at HQ must not leave half an estate playing the old bytes.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.announcement_player import (  # noqa: E402
    AnnouncementError,
    AnnouncementPlayback,
    cached_path,
    decode_command,
    fetch_if_absent,
    verify_download_path,
)

AUDIO = b"ID3-pretend-this-is-an-mp3"
DIGEST = hashlib.sha256(AUDIO).hexdigest()


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False


# ===========================================================================
# Fetching and caching
# ===========================================================================

def test_a_recording_is_downloaded_once_and_reused(tmp_path):
    calls = []

    def opener(request, timeout=None):
        calls.append(request.full_url)
        return FakeResponse(AUDIO)

    for _ in range(3):
        path = fetch_if_absent(
            state_root=tmp_path, sha256=DIGEST,
            download_path="/api/receiver/announcements/4/download",
            backend_url="http://hq.example:8000", credential="token",
            opener=opener)
        assert path.read_bytes() == AUDIO
    assert len(calls) == 1, "the Store downloaded a recording it already had"


def test_the_download_carries_the_receivers_credential(tmp_path):
    seen = {}

    def opener(request, timeout=None):
        seen["authorization"] = request.get_header("Authorization")
        return FakeResponse(AUDIO)

    fetch_if_absent(state_root=tmp_path, sha256=DIGEST,
                    download_path="/api/receiver/announcements/4/download",
                    backend_url="http://hq.example:8000",
                    credential="the-device-credential", opener=opener)
    assert seen["authorization"] == "Bearer the-device-credential"


def test_a_corrupted_download_is_discarded_rather_than_cached(tmp_path):
    """A cached bad file is a Store that stays broken until somebody notices."""
    def opener(request, timeout=None):
        return FakeResponse(b"truncated")

    with pytest.raises(AnnouncementError) as refusal:
        fetch_if_absent(state_root=tmp_path, sha256=DIGEST,
                        download_path="/api/receiver/announcements/4/download",
                        backend_url="http://hq.example:8000",
                        credential="token", opener=opener)
    assert "discarded" in str(refusal.value)
    assert not cached_path(tmp_path, DIGEST).exists()
    assert list(cached_path(tmp_path, DIGEST).parent.glob("*.part")) == [], (
        "a partial download was left where the next play could find it")


def test_replacing_a_recording_at_hq_is_not_served_from_the_cache(tmp_path):
    """Keying by name or id is how an estate plays last year's offer for a
    fortnight after somebody 'just re-uploaded it'."""
    replacement = b"ID3-this-years-offer"
    new_digest = hashlib.sha256(replacement).hexdigest()

    fetch_if_absent(state_root=tmp_path, sha256=DIGEST,
                    download_path="/api/receiver/announcements/4/download",
                    backend_url="http://hq", credential="t",
                    opener=lambda request, timeout=None: FakeResponse(AUDIO))
    path = fetch_if_absent(
        state_root=tmp_path, sha256=new_digest,
        download_path="/api/receiver/announcements/4/download",
        backend_url="http://hq", credential="t",
        opener=lambda request, timeout=None: FakeResponse(replacement))
    assert path.read_bytes() == replacement


# ===========================================================================
# What a Store refuses to be talked into
# ===========================================================================

def test_a_hash_that_is_not_a_hash_never_becomes_a_filename(tmp_path):
    for hostile in ("../../config.json", "a" * 63, "z" * 64, ""):
        with pytest.raises(AnnouncementError):
            cached_path(tmp_path, hostile)


def test_a_download_path_outside_the_announcement_route_is_refused():
    verify_download_path("/api/receiver/announcements/9/download")
    for hostile in ("/api/stores", "http://elsewhere.example/x",
                    "/api/receiver-devices/enroll", "", None):
        with pytest.raises(AnnouncementError):
            verify_download_path(hostile)


# ===========================================================================
# Playing
# ===========================================================================

def test_the_decoder_produces_the_shape_the_broadcast_sink_expects():
    """One device selection, one level, one thing making sound - which only
    holds if both paths hand the sink the same PCM."""
    command = decode_command(Path("x.mp3"))
    assert "48000" in command
    assert "s16le" in command
    assert command[command.index("-ac") + 1] == "2"
    assert "-nostdin" in command, (
        "without this a background Receiver blocks on a console nobody is "
        "attached to")


class FakeSink:
    def __init__(self):
        self.written = bytearray()

    def write(self, pcm):
        self.written.extend(pcm)
        return True


class FakeProcess:
    def __init__(self, payload: bytes):
        import io
        self.stdout = io.BytesIO(payload)
        self.killed = False

    def kill(self):
        self.killed = True


def test_playing_writes_the_decoded_audio_into_the_shared_sink():
    sink = FakeSink()
    # At unity, so this test is about the audio reaching the sink and not
    # about the scaling - which has its own tests below.
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False, volume_percent=100,
        spawn=lambda *args, **kwargs: FakeProcess(b"PCMDATA" * 100))
    playback.start()
    playback._thread.join(timeout=5)
    assert bytes(sink.written).startswith(b"PCMDATA")


class EndlessProcess:
    """A decoder that keeps producing until the test stops it.

    A finite payload made this test a race: the pump could drain it and finish
    before pause() arrived, and a finished recording correctly refuses to
    resume - so the test failed for a reason that had nothing to do with what
    it was checking.
    """

    def __init__(self):
        self.killed = False
        self.stdout = self

    def read(self, size):
        return b"" if self.killed else b"A" * size

    def kill(self):
        self.killed = True


def test_pausing_does_not_tear_the_decoder_down():
    """Rebuilding it would restart the recording from its first word every
    time a broadcast interrupted - for a jingle interrupted twice an hour, the
    second half is never heard."""
    sink = FakeSink()
    decoder = EndlessProcess()
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False,
        spawn=lambda *args, **kwargs: decoder)
    playback.start()

    playback.pause()
    assert not playback.is_playing
    assert not decoder.killed, "pausing killed the decoder"

    playback.resume()
    assert playback.is_playing

    playback.stop()
    playback._thread.join(timeout=5)
    assert decoder.killed, "stopping left the decoder running"


def test_a_missing_ffmpeg_is_reported_rather_than_hung():
    sink = FakeSink()

    def missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    playback = AnnouncementPlayback(path=Path("x.mp3"), sink=sink, loop=False,
                                    spawn=missing)
    playback.start()
    playback._thread.join(timeout=5)
    assert not playback.is_playing
    assert sink.written == b""


# ===========================================================================
# Volume
#
# Scaled on the announcement's own samples, and deliberately NOT on the sink.
# The sink is shared with the broadcast: turning a jingle down there would
# turn the person speaking down with it.
# ===========================================================================

def test_the_volume_scales_the_announcement_and_not_the_sink():
    sink = FakeSink()
    loud = b"\x00\x40" * 512          # +16384 in every sample
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False, volume_percent=50,
        spawn=lambda *args, **kwargs: FakeProcess(loud))
    playback.start()
    playback._thread.join(timeout=5)

    import array
    written = array.array("h")
    written.frombytes(bytes(sink.written))
    assert written, "nothing was written"
    assert all(value == 8192 for value in written), (
        "the announcement was not scaled to half")
    assert not hasattr(sink, "volume_percent"), (
        "the level was pushed onto the shared sink, which would take the "
        "broadcast down with it")


def test_unity_passes_the_samples_through_untouched():
    """The common case does no arithmetic at all, so identical bytes cannot be
    distorted by rounding."""
    sink = FakeSink()
    original = bytes(range(256)) * 4
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False, volume_percent=100,
        spawn=lambda *args, **kwargs: FakeProcess(original))
    playback.start()
    playback._thread.join(timeout=5)
    assert bytes(sink.written) == original


def test_zero_percent_is_silence_not_a_stop():
    """Silent and stopped are different: a jingle turned to zero must still be
    running, so turning it back up resumes where it is rather than restarting."""
    sink = FakeSink()
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False, volume_percent=0,
        spawn=lambda *args, **kwargs: FakeProcess(b"\x00\x40" * 512))
    playback.start()
    playback._thread.join(timeout=5)
    assert bytes(sink.written) == bytes(1024)
    assert len(sink.written) == 1024, "silence must still be written"


def test_an_odd_trailing_byte_does_not_end_the_announcement():
    """Dropping half a sample is better than raising over one byte."""
    sink = FakeSink()
    playback = AnnouncementPlayback(
        path=Path("x.mp3"), sink=sink, loop=False, volume_percent=50,
        spawn=lambda *args, **kwargs: FakeProcess(b"\x00\x40" * 4 + b"\x01"))
    playback.start()
    playback._thread.join(timeout=5)
    assert len(sink.written) == 8


# ===========================================================================
# What the shop computer actually runs
# ===========================================================================

def test_the_decoder_names_the_packaged_ffmpeg_not_a_bare_command():
    """A Store desktop has no FFmpeg on PATH and no reason to acquire one.

    The Receiver package carries its own, and every other decoder in this
    product already resolves it by absolute path. This one asked PATH - so on
    a real shop computer the decode failed instantly, the announcement was
    silent, and HQ went on showing it as playing.
    """
    from tools.announcement_player import decode_command
    from pathlib import Path

    first = decode_command(Path("promo.mp3"))[0]
    assert first != "ffmpeg", "a bare command is resolved against PATH"
    assert first.lower().endswith("ffmpeg.exe") or first.endswith("ffmpeg")
    assert Path(first).is_absolute()


def test_the_decoder_starts_with_no_console_window():
    """SpeakLinkReceiverBackground.exe is GUI-subsystem, so it has no console.

    A console child started without CREATE_NO_WINDOW is given a brand-new
    console by Windows - a black window on the shop counter, appearing exactly
    when an announcement starts. The broadcast decoder already guards this;
    the announcement decoder did not, and that window was reported from a real
    Store.
    """
    import sys
    import time
    from pathlib import Path
    from tools.announcement_player import AnnouncementPlayback

    seen = {}

    class FakeStdout:
        def read(self, _size):
            return b""          # end of stream: one pass and done

        def close(self):
            return None

    class FakeProcess:
        stdout = FakeStdout()

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

        def terminate(self):
            return None

    def spy(command, **options):
        seen.update(options)
        return FakeProcess()

    playback = AnnouncementPlayback(path=Path("promo.mp3"), sink=None,
                                    spawn=spy)
    playback.start()
    # The decoder starts on its own thread; this waits for the spawn rather
    # than for the playback, which loops until it is told to stop.
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)
    playback.stop()

    if sys.platform == "win32":
        assert seen.get("creationflags"), "no CREATE_NO_WINDOW on Windows"
    else:
        # Nothing to hide on a platform without consoles, and the option set
        # is empty rather than wrong.
        assert "creationflags" not in seen
