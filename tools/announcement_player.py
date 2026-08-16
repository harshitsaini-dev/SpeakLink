"""Playing a recorded announcement on a Store PC.

WHY THIS REUSES THE BROADCAST SINK RATHER THAN PLAYING THE FILE ITSELF

The obvious implementation is to hand the file to ffplay and let it open the
speaker. That would give the Store a second, independent path to the sound
card - one that does not know which device the operator selected, does not
respect the SpeakLink level, and can be playing at the same moment as a
broadcast without either knowing about the other.

So a recording is decoded to the same PCM shape the broadcast uses and written
into the SAME sink. One device selection, one level, one thing making sound.

THE CACHE IS KEYED BY CONTENT, NOT BY NAME OR ID

A Store keeps every recording it has been asked to play, under its SHA-256.
Two consequences, and the second is the reason:

  * a recording it already holds costs no download, so a shop on a slow link
    starts instantly and a template restarted twice a day costs nothing;
  * a recording that was REPLACED at HQ has a different hash, so no cache
    anywhere can serve the old bytes. Keying by filename or by id is how half
    an estate ends up playing last year's Diwali offer for a fortnight after
    somebody "just re-uploaded it".

The hash is also verified after the download. A file that arrives corrupted -
a truncated response, a proxy that rewrote it - is discarded rather than
cached and played, because a cached bad file is a Store that stays broken
until somebody notices.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess

try:  # the Receiver runtime
    from tools.audio_receiver_pilot import hidden_child_process_options
except ImportError:  # pragma: no cover - a checkout laid out differently
    try:
        from audio_receiver_pilot import hidden_child_process_options
    except ImportError:
        def hidden_child_process_options() -> dict:
            """No Receiver runtime here - a test, or a non-Windows machine."""
            return {}
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("speaklink.receiver.announcement")

#: The PCM shape the broadcast path already produces, so both feed one sink.
#:
#: The comment was right and the number was wrong. The broadcast decoder asks
#: ffmpeg for OUTPUT_CHANNELS - one - because the Store's output stream is
#: opened mono. This said two, so every announcement wrote interleaved stereo
#: into a mono stream: the write failed, the sink marked itself failed, and
#: the Receiver reported the shop as playing while it sat in silence. That is
#: the whole difference between "broadcast works and announcements do not".
SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_FORMAT = "s16le"

#: How much decoded audio to hand the sink at a time. Small enough that a
#: pause takes effect immediately - a chunk already written cannot be
#: unwritten, so a large one is a Pause button with a delay on it.
CHUNK_BYTES = 8192

#: Refused rather than played. A Store must not be talked into fetching an
#: arbitrary URL by a malformed command.
ALLOWED_PATH_PREFIX = "/api/receiver/announcements/"


class AnnouncementError(RuntimeError):
    """Something an operator or a log reader can act on."""


def cache_directory(state_root: Path) -> Path:
    directory = Path(state_root) / "announcement-cache"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def cached_path(state_root: Path, sha256: str) -> Path:
    """Where a recording with this content lives.

    The hash is checked to be a hash before it becomes part of a path. It
    arrives over the network, and a value like "../../config.json" would
    otherwise be a way to make this program read or write outside its own
    directory.
    """
    if len(sha256) != 64 or any(character not in "0123456789abcdef"
                                for character in sha256.lower()):
        raise AnnouncementError(
            "That is not a SHA-256 digest, so it is not being used as a "
            "filename.")
    return cache_directory(state_root) / f"{sha256.lower()}.audio"


def verify_download_path(path: str) -> str:
    """Refuse anything that is not HQ's announcement download route."""
    if not isinstance(path, str) or not path.startswith(ALLOWED_PATH_PREFIX):
        raise AnnouncementError(
            f"An announcement may only be fetched from {ALLOWED_PATH_PREFIX}; "
            f"{path!r} was given.")
    return path


def fetch_if_absent(*, state_root: Path, sha256: str, download_path: str,
                    backend_url: str, credential: str, opener=None) -> Path:
    """Return the cached file, downloading it once if this Store lacks it."""
    destination = cached_path(state_root, sha256)
    if destination.is_file():
        return destination
    verify_download_path(download_path)

    import urllib.request

    request = urllib.request.Request(
        backend_url.rstrip("/") + download_path,
        headers={"Authorization": f"Bearer {credential}"})
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=60) as response:
        raw = response.read()

    actual = hashlib.sha256(raw).hexdigest()
    if actual != sha256.lower():
        # NOT cached. A bad file written to the cache is a Store that stays
        # broken until a person notices, because every later play finds it
        # already there and never downloads again.
        raise AnnouncementError(
            "The recording that arrived is not the one that was asked for "
            f"(expected {sha256[:12]}…, received {actual[:12]}…). It has been "
            "discarded rather than cached.")

    # Written beside and renamed, so an interrupted download can never be
    # found by the next play as a complete file.
    partial = destination.with_suffix(".part")
    partial.write_bytes(raw)
    os.replace(partial, destination)
    return destination


def ffmpeg_executable() -> str:
    """The FFmpeg this Receiver runs, resolved the way the Receiver resolves it.

    MY OWN CORRECTION, TWICE OVER.

    It began as the bare word "ffmpeg", which fails on a Store desktop with
    nothing on PATH. I then pointed it at `resolve_packaged_ffmpeg`, which
    resolves against the SETUP PACKAGE's layout (Receiver/ffmpeg.exe) - and
    the installed Receiver has a different layout, so on a real shop computer
    it could not find the file and reported "the audio decoder is missing from
    this installation". The report was honest; the resolver was wrong.

    The broadcast decoder has always used `shutil.which("ffmpeg")` and always
    worked, because `receiver_agent.prefer_packaged_ffmpeg()` puts the
    packaged binary at the front of this process's PATH at startup. Using the
    same lookup means the announcement decoder and the broadcast decoder can
    never disagree about which binary is being run.
    """
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found

    # Not on PATH: a checkout, or a runtime where prefer_packaged_ffmpeg has
    # not run. Ask the package layout as a second opinion rather than falling
    # back to a bare name that cannot work.
    try:
        from tools.resource_paths import resolve_packaged_ffmpeg
    except ImportError:  # pragma: no cover - a checkout laid out differently
        try:
            from resource_paths import resolve_packaged_ffmpeg
        except ImportError:
            return "ffmpeg"
    try:
        return str(resolve_packaged_ffmpeg(allow_path_fallback=True))
    except Exception:  # noqa: BLE001
        return "ffmpeg"


def decode_command(path: Path, *, channels: int = CHANNELS,
                   sample_rate: int = SAMPLE_RATE) -> list[str]:
    """ffmpeg arguments to turn any accepted format into the sink's PCM.

    ``-nostdin`` matters: without it ffmpeg competes for the console's input
    with whatever started the Receiver, and a background service ends up with
    a decoder that blocks on a terminal nobody is attached to.
    """
    return [
        ffmpeg_executable(), "-nostdin", "-loglevel", "error",
        "-i", str(path),
        "-f", SAMPLE_FORMAT, "-ar", str(sample_rate), "-ac", str(channels),
        "-",
    ]


class AnnouncementPlayback:
    """One recording, playing into the shared sink until told otherwise.

    Pause does NOT stop the decoder and does not close the sink. It stops
    writing. Tearing the decoder down and rebuilding it would restart the
    recording from its first word every time a broadcast interrupted it, which
    for a thirty-second jingle interrupted twice an hour means the second half
    is never heard.
    """

    def __init__(self, *, path: Path, sink, loop: bool = True,
                 volume_percent: int = 80, spawn=subprocess.Popen) -> None:
        self._path = path
        self._sink = sink
        self._loop = loop
        self._spawn = spawn
        self._volume_percent = max(0, min(100, int(volume_percent)))
        self._process = None
        self._thread = None
        self._playing = threading.Event()
        self._finished = threading.Event()
        #: Set when the SPEAKER has taken a frame. Not when the thread starts,
        #: and not when ffmpeg starts - both of those are claims about intent,
        #: and a shop reported as playing on the strength of them was silent
        #: for a whole day.
        self._audible = threading.Event()
        self._failed = False
        self._failure_reason = ""
        self._stopping = False

    @property
    def is_playing(self) -> bool:
        return self._playing.is_set() and not self._finished.is_set()

    def start(self) -> None:
        if self._thread is not None:
            self.resume()
            return
        self._failed = False
        self._failure_reason = ""
        self._audible.clear()
        self._playing.set()
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="speaklink-announcement")
        self._thread.start()

    def pause(self) -> None:
        self._playing.clear()

    def resume(self) -> None:
        if not self._finished.is_set():
            self._playing.set()

    def stop(self) -> None:
        self._stopping = True
        self._playing.set()  # release the pump so it can notice and exit
        process = self._process
        if process is not None:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        self._finished.set()

    def wait_until_audible(self, timeout: float) -> bool:
        """Wait for the first frame the speaker accepted.

        Returns False if that never happens within the timeout - which is the
        honest answer to "is this shop playing", and the one the Receiver
        sends to HQ instead of a hopeful yes.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self._audible.is_set():
                return True
            if self._failed or self._finished.is_set():
                return False
            time.sleep(0.02)
        return self._audible.is_set()

    def failure_reason(self) -> str:
        """Why it did not start, in a sentence, or empty if it did."""
        return self._failure_reason

    def set_volume(self, percent: int) -> None:
        """Change the level of THIS announcement, from the next chunk on.

        Scaled here, on the announcement's own samples, and deliberately NOT
        on the sink. The sink is shared with the broadcast: turning a jingle
        down there would turn the person speaking down with it, and turning
        the jingle back up after a broadcast would raise the broadcast too.

        Nor is it the Windows endpoint master, which is the whole computer -
        an announcement must be able to be quiet without silencing everything
        else that computer plays.
        """
        self._volume_percent = max(0, min(100, int(percent)))

    def _at_volume(self, pcm: bytes) -> bytes:
        """Scale 16-bit little-endian samples.

        Unity is returned unchanged rather than multiplied by 1.0: the common
        case does no arithmetic at all, and identical bytes cannot be
        distorted by rounding.
        """
        percent = self._volume_percent
        if percent >= 100:
            return pcm
        if percent <= 0:
            return bytes(len(pcm))
        import array

        samples = array.array("h")
        # An odd trailing byte cannot be half a sample. Dropping it is better
        # than raising, which would end the announcement over one byte.
        samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
        scale = percent / 100.0
        for index, value in enumerate(samples):
            samples[index] = int(value * scale)
        if sys.byteorder != "little":
            samples.byteswap()
        return samples.tobytes()

    def _pump(self) -> None:
        while not self._stopping:
            try:
                # NO CONSOLE WINDOW.
                #
                # SpeakLinkReceiverBackground.exe is GUI-subsystem, so it has
                # no console. Starting a console child - and ffmpeg is one -
                # without CREATE_NO_WINDOW makes Windows hand that child a
                # BRAND-NEW console, which is a black window appearing on the
                # shop counter the moment an announcement starts.
                #
                # The broadcast decoder already does this; the announcement
                # decoder was the one place that did not, and the window
                # flashing on the Store PC was exactly that.
                # ASKED, NOT ASSUMED. A sink carries its own rate and
                # channel count; a second opinion hard-coded here is what
                # produced a stereo stream for a mono device.
                configuration = getattr(self._sink, "configuration", None)
                command = decode_command(
                    self._path,
                    channels=getattr(configuration, "channels", CHANNELS),
                    sample_rate=getattr(configuration, "sample_rate", SAMPLE_RATE))
                self._process = self._spawn(command,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL,
                                            **hidden_child_process_options())
            except FileNotFoundError:
                logger.error("ffmpeg is not installed, so the announcement "
                             "cannot be decoded.")
                self._failed = True
                self._failure_reason = (
                    "the audio decoder is missing from this installation - "
                    "reinstall the Store Kit on this computer")
                self._finished.set()
                return
            try:
                while True:
                    if self._stopping:
                        break
                    # Blocks while paused. The decoder's pipe fills and ffmpeg
                    # blocks with it, which is what keeps a paused
                    # announcement from silently running to its end.
                    self._playing.wait()
                    if self._stopping:
                        break
                    chunk = self._process.stdout.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    accepted = self._sink.write(self._at_volume(chunk))
                    if accepted is not False:
                        # One frame the speaker took. This is the only
                        # evidence anywhere in this program that a recording
                        # is actually audible-bound rather than merely
                        # started.
                        self._audible.set()
                    if accepted is False:
                        # The sink REFUSED the audio - a closed device, or a
                        # format it cannot take. Looping on that is how a shop
                        # stays silent while every layer reports success.
                        logger.error(
                            "The Store's speaker refused the announcement "
                            "audio; stopping rather than pretending to play.")
                        self._failed = True
                        self._failure_reason = (
                            "the Store's speaker refused the audio - it may be "
                            "in use by something else, or set to a format this "
                            "recording cannot be played in")
                        self._stopping = True
                        break
            finally:
                try:
                    self._process.kill()
                except Exception:  # noqa: BLE001
                    pass
                self._process = None
            if not self._loop or self._stopping:
                break
        self._finished.set()
