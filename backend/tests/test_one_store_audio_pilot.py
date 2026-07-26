"""End-to-end tests for the one-Store live-audio software pilot.

These tests use the external local-pilot root under a pytest temporary
directory, a deterministic synthetic WebM/Opus fixture and real FFmpeg. They
never open, copy or modify ``backend/speaklink_live.db``.

A passing run proves the software path only: one Receiver connects, becomes
READY after real FFmpeg/codec checks, valid WebM/Opus bytes traverse the
pipeline, the Receiver acknowledges AUDIO_RECEIVING, FFmpeg actually decodes
the audio, the Receiver acknowledges PLAYBACK_CONFIRMED, and stop/cleanup
work. It proves nothing about output devices, amplifiers or audible speakers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.audio_receiver_pilot import (  # noqa: E402
    RECEIVER_TOKEN_ENV,
    AudioReceiverError,
    FfmpegDecoder,
    _require_token,
    ffmpeg_available,
    opus_webm_decode_supported,
)
from tools.generate_audio_fixture import (  # noqa: E402
    FIXTURE_CHANNELS,
    FIXTURE_NAME,
    AudioFixtureError,
    generate_fixture,
    probe,
)
from tools.local_audio_pilot import (  # noqa: E402
    READINESS_SCOPE,
    AudioPilotError,
    prepare,
    smoke,
)
from tools.local_pilot import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    ADMIN_USERNAME_ENV,
    resolve_pilot_paths,
)

PROTECTED_DATABASE = REPOSITORY_ROOT / "backend" / "speaklink_live.db"

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg/ffprobe are required for the audio pilot",
)


def _protected_metadata():
    if not PROTECTED_DATABASE.exists():
        return None
    stat = PROTECTED_DATABASE.stat()
    return stat.st_size, stat.st_mtime_ns


def _protected_sidecars_present() -> bool:
    return any(
        Path(str(PROTECTED_DATABASE) + suffix).exists() for suffix in ("-wal", "-shm")
    )


# ---------------------------------------------------------------------------
# Capability checks
# ---------------------------------------------------------------------------
@requires_ffmpeg
def test_ffmpeg_and_opus_webm_support_are_detected_not_assumed():
    assert ffmpeg_available() is True
    assert opus_webm_decode_supported() is True


def test_receiver_refuses_to_start_without_a_credential_in_the_environment(monkeypatch):
    monkeypatch.delenv(RECEIVER_TOKEN_ENV, raising=False)
    with pytest.raises(AudioReceiverError) as error:
        _require_token()
    # The error names the variable but can never contain a credential value.
    assert RECEIVER_TOKEN_ENV in str(error.value)


def test_receiver_rejects_a_blank_credential(monkeypatch):
    monkeypatch.setenv(RECEIVER_TOKEN_ENV, "   ")
    with pytest.raises(AudioReceiverError):
        _require_token()


# ---------------------------------------------------------------------------
# Synthetic fixture
# ---------------------------------------------------------------------------
@requires_ffmpeg
def test_synthetic_fixture_is_deterministic_mono_webm_opus(tmp_path):
    audio_dir = tmp_path / "audio"
    first = generate_fixture(audio_dir)
    second = generate_fixture(audio_dir, force=True)

    assert first["codec_name"] == "opus"
    assert first["channels"] == FIXTURE_CHANNELS == 1
    assert "webm" in first["format_name"] or "matroska" in first["format_name"]
    assert first["size_bytes"] > 0
    assert 3.5 <= first["duration_seconds"] <= 4.5
    # Deterministic: regenerating produces byte-identical audio.
    assert first["sha256"] == second["sha256"]
    assert Path(first["path"]).name == FIXTURE_NAME


@requires_ffmpeg
def test_fixture_lives_outside_the_repository(tmp_path):
    facts = generate_fixture(tmp_path / "audio")
    assert REPOSITORY_ROOT not in Path(facts["path"]).parents


@requires_ffmpeg
def test_fixture_contains_real_non_silent_audio(tmp_path):
    facts = generate_fixture(tmp_path / "audio")
    # A 4 second 32 kbps mono Opus tone is comfortably over a few kilobytes;
    # a silent or empty render would be far smaller.
    assert facts["size_bytes"] > 2000
    assert probe(Path(facts["path"]))["duration_seconds"] > 3.5


def test_probe_rejects_a_file_that_is_not_audio(tmp_path):
    broken = tmp_path / "not-audio.webm"
    broken.write_bytes(b"this is definitely not a media file")
    with pytest.raises(AudioFixtureError):
        probe(broken)


# ---------------------------------------------------------------------------
# FFmpeg decoder behaviour
# ---------------------------------------------------------------------------
@requires_ffmpeg
def test_decoder_processes_valid_webm_opus_and_reports_decoded_time(tmp_path):
    facts = generate_fixture(tmp_path / "audio")
    data = Path(facts["path"]).read_bytes()

    decoder = FfmpegDecoder()
    decoder.start()
    try:
        step = max(1, len(data) // 16)
        for offset in range(0, len(data), step):
            assert decoder.feed(data[offset:offset + step]) is True
        returncode = decoder.close()
    finally:
        if decoder.running:
            decoder.close()

    assert returncode == 0
    assert decoder.decoded_microseconds > 0
    assert decoder.sink_mode == "null"
    assert decoder.running is False


@requires_ffmpeg
def test_decoder_reports_failure_for_corrupt_audio():
    decoder = FfmpegDecoder()
    decoder.start()
    try:
        decoder.feed(b"this is not a valid WebM stream at all" * 64)
        returncode = decoder.close()
    finally:
        if decoder.running:
            decoder.close()

    # Corrupt input must not be reported as a successful decode.
    assert returncode != 0 or decoder.decoded_microseconds == 0


@requires_ffmpeg
def test_decoder_command_uses_a_null_sink_and_never_an_output_device():
    command = " ".join(FfmpegDecoder().command())
    assert "-f null" in command
    for forbidden in ("dshow", "waveaudio", "directsound", "-f wav", "autoaudiosink"):
        assert forbidden not in command


@requires_ffmpeg
def test_decoder_close_is_safe_to_call_twice():
    decoder = FfmpegDecoder()
    decoder.start()
    decoder.close()
    assert decoder.close() is not None
    assert decoder.running is False


# ---------------------------------------------------------------------------
# End-to-end one-Store audio pilot
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def audio_pilot_result(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("FFmpeg is required for the audio pilot")

    root = tmp_path_factory.mktemp("speaklink-audio") / "local-pilot"
    previous = {
        key: os.environ.get(key)
        for key in (ADMIN_USERNAME_ENV, ADMIN_PASSWORD_ENV, "SPEAKLINK_DB_PATH", "JWT_SECRET")
    }
    os.environ[ADMIN_USERNAME_ENV] = "pilot-operator"
    os.environ[ADMIN_PASSWORD_ENV] = "audio-pilot-only-temporary-passphrase"
    os.environ["JWT_SECRET"] = "audio-pilot-only-temporary-jwt-secret"
    os.environ.pop("SPEAKLINK_DB_PATH", None)

    protected_before = _protected_metadata()
    try:
        paths = resolve_pilot_paths(root)
        prepared = prepare(paths)
        result = smoke(paths)
        yield {
            "prepared": prepared,
            "result": result,
            "paths": paths,
            "protected_before": protected_before,
        }
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_pilot_database_is_the_canonical_catalog(audio_pilot_result):
    database = audio_pilot_result["prepared"]["database"]
    assert database["store_count"] == 44
    assert database["zone_count"] == 9
    assert database["reconciliation"] == "EXACT_CANONICAL_MATCH"
    assert database["demo_codes_present"] == []


def test_backend_runs_loopback_only_with_one_worker(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["backend_host"] == "127.0.0.1"
    assert result["uvicorn_workers"] == 1
    assert result["backend_url"].startswith("http://127.0.0.1:")


def test_one_store_reaches_connected_then_ready(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["liveness"] == "ok"
    assert result["login"] == "ok"
    assert result["observed_connected"] is True
    # READY is only recorded after the Receiver's real FFmpeg/codec checks.
    assert result["observed_ready"] is True
    assert result["selected_store_code"] == "UN"


def test_audio_receiving_and_playback_confirmed_are_real_acknowledgements(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["observed_audio_receiving"] is True
    assert result["observed_playback_confirmed"] is True
    assert result["sent_chunks"] > 1
    assert result["sent_bytes"] > 0
    assert result["receiver_total_chunks"] == result["sent_chunks"]
    assert result["receiver_total_bytes"] == result["sent_bytes"]


def test_ffmpeg_actually_decoded_the_audio(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["ffmpeg_returncode"] == 0
    assert result["ffmpeg_decoded_microseconds"] > 0
    assert result["sink_mode"] == "null"


def test_speaker_verified_is_never_claimed(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["speaker_verified"] is False
    assert result["readiness_scope"] == READINESS_SCOPE
    serialised = json.dumps(result)
    for forbidden in (
        "SPEAKER_VERIFIED",
        "AMPLIFIER_VERIFIED",
        "READY_FOR_SPEAKER_TEST",
        "READY_FOR_PRODUCTION",
        "ALL_STORES_READY",
    ):
        assert forbidden not in serialised


def test_queue_stayed_bounded_and_nothing_was_dropped(audio_pilot_result):
    result = audio_pilot_result["result"]
    # A local loopback Receiver keeping up must not drop anything, and the
    # queue is bounded regardless.
    assert result["receiver_dropped_chunks"] == 0


def test_stop_and_cleanup_complete(audio_pilot_result):
    result = audio_pilot_result["result"]
    assert result["observed_stopped"] is True
    assert result["shutdown"] == "ok"
    assert result["backend_process_running"] is False
    assert result["receiver_process_running"] is False
    assert result["overall_result"] == "ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED"


def test_protected_database_and_sidecars_are_untouched(audio_pilot_result):
    assert _protected_metadata() == audio_pilot_result["protected_before"]
    assert _protected_sidecars_present() is False


def test_reports_and_logs_contain_no_secret(audio_pilot_result):
    paths = audio_pilot_result["paths"]
    contents: list[str] = []
    for candidate in sorted(paths.logs_dir.glob("*")):
        if candidate.is_file():
            contents.append(candidate.read_text(encoding="utf-8", errors="replace"))
    assert contents, "the audio pilot should have written logs and a report"

    for content in contents:
        lowered = content.lower()
        assert "audio-pilot-only-temporary-passphrase" not in content
        assert "audio-pilot-only-temporary-jwt-secret" not in content
        for marker in ("receiver_token", "authorization:", "bearer "):
            assert marker not in lowered


def test_generated_audio_and_reports_stay_outside_git(audio_pilot_result):
    paths = audio_pilot_result["paths"]
    fixture_path = Path(audio_pilot_result["result"]["fixture"]["path"])
    assert REPOSITORY_ROOT not in fixture_path.parents
    assert REPOSITORY_ROOT not in paths.logs_dir.parents
    assert REPOSITORY_ROOT not in paths.database_path.parents


def test_repository_contains_no_committed_audio_artifact():
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert tracked.returncode == 0
    for line in tracked.stdout.splitlines():
        assert not line.lower().endswith((".webm", ".opus", ".wav", ".pcm", ".raw"))
