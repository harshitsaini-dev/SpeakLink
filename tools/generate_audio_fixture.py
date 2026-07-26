"""Deterministic synthetic WebM/Opus audio fixture generation.

The automated one-Store audio pilot needs audio that is byte-identical on every
run, because browser microphone capture is not deterministic and cannot be
automated honestly. A low-volume sine wave rendered by the installed FFmpeg
gives exactly that.

The generated file is written **outside the repository**, under the existing
external local-pilot root, and is never committed.

Generating and decoding this fixture proves that valid WebM/Opus data can
traverse the software pipeline. It proves nothing about microphone capture,
Windows output devices, amplifiers or audible speakers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess


FIXTURE_NAME = "pilot-tone.webm"

# Deterministic source: a fixed-frequency, low-volume sine wave.
FIXTURE_FREQUENCY_HZ = 440
FIXTURE_SAMPLE_RATE = 48_000
FIXTURE_DURATION_SECONDS = 4
FIXTURE_VOLUME = 0.3
FIXTURE_CHANNELS = 1
FIXTURE_BITRATE = "32k"


class AudioFixtureError(RuntimeError):
    """Raised when FFmpeg is unavailable or the fixture cannot be produced."""


def ffmpeg_executable() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise AudioFixtureError("ffmpeg was not found on PATH")
    return path


def ffprobe_executable() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise AudioFixtureError("ffprobe was not found on PATH")
    return path


def ffmpeg_version() -> str:
    result = subprocess.run(
        [ffmpeg_executable(), "-hide_banner", "-version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise AudioFixtureError("ffmpeg could not report its version")
    return result.stdout.splitlines()[0].strip()


def opus_webm_supported() -> bool:
    """Prove the installed FFmpeg can both decode Opus and demux WebM."""
    ffmpeg = ffmpeg_executable()
    decoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, timeout=30
    )
    formats = subprocess.run(
        [ffmpeg, "-hide_banner", "-formats"], capture_output=True, text=True, timeout=30
    )
    if decoders.returncode != 0 or formats.returncode != 0:
        return False
    has_opus = " opus " in decoders.stdout or "libopus" in decoders.stdout
    has_webm = "matroska,webm" in formats.stdout or "webm" in formats.stdout
    return bool(has_opus and has_webm)


def build_command(destination: Path, *, duration: int = FIXTURE_DURATION_SECONDS) -> list[str]:
    """The exact FFmpeg command used, recorded in the pilot report."""
    return [
        ffmpeg_executable(),
        "-hide_banner",
        "-nostdin",
        "-loglevel", "error",
        "-y",
        "-f", "lavfi",
        "-i",
        f"sine=frequency={FIXTURE_FREQUENCY_HZ}"
        f":sample_rate={FIXTURE_SAMPLE_RATE}:duration={duration}",
        "-af", f"volume={FIXTURE_VOLUME}",
        "-ac", str(FIXTURE_CHANNELS),
        "-c:a", "libopus",
        "-b:a", FIXTURE_BITRATE,
        # Without -bitexact the Matroska muxer writes a random SegmentUID and
        # encoder/date metadata, so the file would differ on every run even
        # though the audio is identical. The fixture must be reproducible.
        "-bitexact",
        "-map_metadata", "-1",
        "-f", "webm",
        str(destination),
    ]


def probe(path: Path) -> dict:
    """Validate the fixture with ffprobe and return its non-secret facts."""
    result = subprocess.run(
        [
            ffprobe_executable(),
            "-v", "error",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AudioFixtureError("ffprobe could not read the generated fixture")
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise AudioFixtureError("the generated fixture has no audio stream")
    container = payload.get("format", {})
    return {
        "codec_name": audio.get("codec_name"),
        "channels": audio.get("channels"),
        "sample_rate": audio.get("sample_rate"),
        "format_name": container.get("format_name"),
        "duration_seconds": float(container.get("duration", 0.0)),
        "bit_rate": int(container.get("bit_rate", 0) or 0),
        "size_bytes": int(container.get("size", 0) or 0),
    }


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate_fixture(
    audio_dir: Path,
    *,
    duration: int = FIXTURE_DURATION_SECONDS,
    force: bool = False,
) -> dict:
    """Create (or reuse) the deterministic fixture and validate it."""
    if not opus_webm_supported():
        raise AudioFixtureError(
            "the installed FFmpeg does not report both Opus decoding and WebM demuxing"
        )

    audio_dir.mkdir(parents=True, exist_ok=True)
    destination = audio_dir / FIXTURE_NAME
    command = build_command(destination, duration=duration)

    regenerated = False
    if force or not destination.exists():
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not destination.exists():
            raise AudioFixtureError("FFmpeg could not generate the synthetic audio fixture")
        regenerated = True

    facts = probe(destination)
    if facts["codec_name"] != "opus":
        raise AudioFixtureError(f"the fixture codec is {facts['codec_name']!r}, expected opus")
    if facts["channels"] != FIXTURE_CHANNELS:
        raise AudioFixtureError("the fixture is not mono")
    if "webm" not in (facts["format_name"] or "") and "matroska" not in (facts["format_name"] or ""):
        raise AudioFixtureError("the fixture container is not WebM/Matroska")
    if facts["size_bytes"] <= 0:
        raise AudioFixtureError("the fixture is empty")

    return {
        "path": str(destination),
        "regenerated": regenerated,
        "sha256": sha256_of(destination),
        "ffmpeg_command": " ".join(command),
        "ffmpeg_version": ffmpeg_version(),
        **facts,
    }
