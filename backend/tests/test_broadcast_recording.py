"""Recording a broadcast, and the rule that outranks recording it.

THE RULE

**A recording must never delay a live announcement.** A shop full of people is
waiting for the audio; a file on a disk is not. Most of this file exists to
prove that a slow, full, broken or absent disk costs a truthful recording
status and never a delivered chunk.

WHAT IS RECORDED

The bytes arriving on the broadcaster's microphone socket - HQ's OUTGOING
audio, after the accepted gain and mute path. Store ambient sound and
LinkGuard monitoring cannot appear in it, not because they are filtered out
but because they never travel on that socket at all. That is a structural
guarantee, and the test below asserts the structure rather than the absence.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
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

import broadcast_recording  # noqa: E402


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy import create_engine, text

    made = create_engine(f"sqlite:///{(tmp_path / 'rec.db').as_posix()}")
    with made.begin() as connection:
        # The two tables the recording metadata refers to. Created directly
        # rather than by importing the whole server: what is under test is the
        # recording lifecycle, not application startup.
        connection.execute(text(
            "CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY, "
            "campaign_name VARCHAR(255))"))
        for session_id in (1, 2, 3):
            connection.execute(
                text("INSERT INTO broadcast_sessions (id, campaign_name) "
                     "VALUES (:id, 'Test campaign')"), {"id": session_id})
    broadcast_recording.ensure_recording_schema(made)
    return made


def write(writer, chunks):
    """Drive a writer through a real event loop, as the server does."""
    async def go():
        writer.start()
        for chunk in chunks:
            writer.offer(chunk)
        return await writer.close()
    return asyncio.run(go())


# A minimal real WebM/Opus file, produced by FFmpeg, so playability is proven
# against the actual toolchain rather than asserted about invented bytes.
def make_webm(path: Path, seconds: float = 1.0) -> bytes:
    import shutil

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is not installed on this machine")
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(path)],
        check=True, capture_output=True, timeout=60)
    return path.read_bytes()


# ===========================================================================
# 1-5  The lifecycle
# ===========================================================================
def test_a_broadcast_gets_exactly_one_recording_row(engine):
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")
    assert len(broadcast_recording.all_recordings(engine)) == 1
    assert broadcast_recording.get_recording(engine, session_id=1).status == "recording"


def test_outgoing_audio_is_written(engine, tmp_path):
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    status = write(writer, [b"chunk-one", b"chunk-two"])

    assert status == broadcast_recording.STATUS_AVAILABLE
    assert writer.chunks_written == 2
    assert writer.final_path.read_bytes() == b"chunk-onechunk-two"


def test_the_in_progress_file_is_never_the_finished_name(engine, tmp_path):
    """A half-written file must not be mistaken for a finished recording."""
    directory = tmp_path / "recordings"
    writer = broadcast_recording.RecordingWriter(session_id=1, directory=directory)

    async def go():
        writer.start()
        writer.offer(b"partial audio")
        await asyncio.sleep(0.05)
        # MID-RECORDING: only the .part exists.
        assert writer.part_path.exists()
        assert not writer.final_path.exists()
        return await writer.close()

    assert asyncio.run(go()) == broadcast_recording.STATUS_AVAILABLE
    # And afterwards the rename is atomic: the finished name appears whole.
    assert writer.final_path.exists()
    assert not writer.part_path.exists()


def test_a_recording_with_no_audio_is_failed_not_available(engine, tmp_path):
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    assert write(writer, []) == broadcast_recording.STATUS_FAILED
    assert "no audio" in writer.error


def test_closing_twice_is_safe(engine, tmp_path):
    """Every path that ends a broadcast calls this, and some call it twice."""
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")

    async def go():
        writer.start()
        writer.offer(b"audio")
        first = await writer.close()
        second = await writer.close()
        return first, second

    first, second = asyncio.run(go())
    assert first == second == broadcast_recording.STATUS_AVAILABLE


# ===========================================================================
# 6-10  Recording must never delay the broadcast
# ===========================================================================
def test_the_queue_is_bounded(engine, tmp_path):
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings", capacity=4)
    assert writer.capacity == 4


def test_a_full_queue_drops_audio_rather_than_blocking(engine, tmp_path):
    """The heart of it. A stalled disk must cost the RECORDING, not the shop.

    The writer task is never allowed to run here, so the queue fills and stays
    full - exactly what a hung disk looks like from the fan-out's point of view.
    """
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings", capacity=3)
    writer.directory.mkdir(parents=True, exist_ok=True)
    writer._handle = open(writer.part_path, "wb")   # started, but nothing drains

    accepted = [writer.offer(b"x") for _ in range(10)]

    assert accepted.count(True) == 3, "only the bounded queue was filled"
    assert accepted.count(False) == 7
    assert writer.chunks_dropped == 7
    writer._handle.close()


def test_offer_never_raises_however_broken_the_writer_is(engine, tmp_path):
    """It is called from the fan-out path; an exception there is an outage."""
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    # Never started: no handle, no queue consumer, no directory.
    assert writer.offer(b"audio") is False

    writer.failed = True
    assert writer.offer(b"audio") is False


def test_dropped_audio_is_reported_as_partial_not_available(engine, tmp_path):
    """PARTIAL and AVAILABLE are different promises about the same file."""
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")

    async def go():
        writer.start()
        writer.offer(b"real audio")
        writer.chunks_dropped = 5          # as a full queue would have left it
        return await writer.close()

    assert asyncio.run(go()) == broadcast_recording.STATUS_PARTIAL


def test_a_disk_that_cannot_be_opened_fails_only_the_recording(engine, tmp_path):
    """A file in the way of the directory: the open cannot succeed."""
    blocked = tmp_path / "recordings"
    blocked.parent.mkdir(parents=True, exist_ok=True)
    blocked.write_text("not a directory", encoding="utf-8")

    writer = broadcast_recording.RecordingWriter(session_id=1, directory=blocked)
    writer.start()
    assert writer.failed is True
    assert writer.error
    # And the fan-out can still call it without an exception escaping.
    assert writer.offer(b"audio") is False


def test_a_write_exception_marks_the_recording_failed(engine, tmp_path):
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")

    class ExplodingHandle:
        def write(self, _chunk):
            raise OSError("no space left on device")
        def flush(self): pass
        def fileno(self): return 0
        def close(self): pass

    async def go():
        writer.directory.mkdir(parents=True, exist_ok=True)
        writer._handle = ExplodingHandle()
        writer._task = asyncio.create_task(writer._drain())
        writer.offer(b"audio")
        await asyncio.sleep(0.05)
        return await writer.close()

    assert asyncio.run(go()) == broadcast_recording.STATUS_FAILED
    assert "no space left" in writer.error


# ===========================================================================
# 11-14  The file is really playable
# ===========================================================================
def test_the_finished_file_is_accepted_by_ffprobe(engine, tmp_path):
    """Not "we appended bytes and assume it plays" - ffprobe is asked."""
    source = make_webm(tmp_path / "source.webm", seconds=1.0)
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    # Written in pieces, exactly as MediaRecorder chunks arrive.
    assert write(writer, [source[i:i + 4096]
                          for i in range(0, len(source), 4096)]) == \
        broadcast_recording.STATUS_AVAILABLE

    probed = broadcast_recording.probe_recording(writer.final_path)
    if not probed:
        pytest.skip("ffprobe is not installed on this machine")
    assert probed.get("error") is None
    assert probed["has_audio"] is True
    assert probed["codec"] == "opus"
    assert "webm" in (probed["container"] or "") or "matroska" in (probed["container"] or "")


def test_the_probed_duration_is_reasonable(engine, tmp_path):
    source = make_webm(tmp_path / "source.webm", seconds=2.0)
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    write(writer, [source])
    probed = broadcast_recording.probe_recording(writer.final_path)
    if not probed:
        pytest.skip("ffprobe is not installed on this machine")
    assert 1.5 <= probed["duration_seconds"] <= 2.5


def test_a_file_of_rubbish_is_rejected(engine, tmp_path):
    """Appending bytes does not make a playable file, and this proves it."""
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    broken = directory / "broadcast-000009.webm"
    broken.write_bytes(b"this is definitely not a WebM container" * 50)

    probed = broadcast_recording.probe_recording(broken)
    if not probed:
        pytest.skip("ffprobe is not installed on this machine")
    assert probed.get("error") or probed.get("has_audio") is False


def test_the_size_recorded_matches_the_file(engine, tmp_path):
    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    write(writer, [b"a" * 1000, b"b" * 500])
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name=writer.file_name)
    broadcast_recording.finish_record(
        engine, session_id=1, status=broadcast_recording.STATUS_AVAILABLE,
        byte_size=writer.final_path.stat().st_size,
        chunks_written=writer.chunks_written)
    assert broadcast_recording.get_recording(engine, session_id=1).byte_size == 1500


# ===========================================================================
# 15-17  Storage, names and secrets
# ===========================================================================
def test_no_audio_is_stored_in_the_database(engine, tmp_path):
    from sqlalchemy import text

    writer = broadcast_recording.RecordingWriter(
        session_id=1, directory=tmp_path / "recordings")
    write(writer, [b"AUDIOAUDIOAUDIO" * 100])
    broadcast_recording.finish_record(
        engine, session_id=1, status=broadcast_recording.STATUS_AVAILABLE,
        byte_size=writer.final_path.stat().st_size)

    with engine.connect() as connection:
        row = connection.execute(text(
            f"SELECT * FROM {broadcast_recording.RECORDING_TABLE}")).fetchone()
    assert b"AUDIO" not in str(row).encode()
    # And the whole row is small - a blob would not be.
    assert len(str(row)) < 500


def test_a_filename_carries_nothing_but_the_session_id(engine):
    """Filenames end up in listings, logs and support screenshots."""
    name = broadcast_recording.recording_filename(123)
    assert name == "broadcast-000123.webm"
    for leak in ("password", "token", "secret", "@", "campaign", "admin"):
        assert leak not in name


def test_the_api_representation_exposes_no_path(engine, tmp_path):
    record = broadcast_recording.start_record(
        engine, session_id=1, file_name="broadcast-000001.webm")
    body = record.as_dict()
    assert "file_name" not in body
    assert not any("path" in key for key in body)
    assert "broadcast-000001" not in str(body)


def test_a_recording_cannot_be_written_outside_its_directory(engine, tmp_path):
    """A recordings folder is where a path bug becomes a catastrophe."""
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    with pytest.raises(broadcast_recording.RecordingError):
        broadcast_recording.remove_recording_file(
            directory, "../../something-important.db")


# ===========================================================================
# 18-22  Crash reconciliation
# ===========================================================================
def test_an_interrupted_part_file_is_never_called_available(engine, tmp_path):
    """The single most important assertion about a restart.

    HQ stopping mid-announcement is exactly when a recording is least
    trustworthy. A file that was never flushed, closed or probed has no
    business being offered as a finished one.
    """
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    source = make_webm(tmp_path / "source.webm", seconds=1.0)
    (directory / "broadcast-000001.webm.part").write_bytes(source)
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")

    outcomes = broadcast_recording.reconcile_recordings(engine, directory)
    assert outcomes == [{"session_id": 1, "status": broadcast_recording.STATUS_PARTIAL}]

    record = broadcast_recording.get_recording(engine, session_id=1)
    assert record.status == broadcast_recording.STATUS_PARTIAL
    assert record.status != broadcast_recording.STATUS_AVAILABLE
    assert "restarted" in record.error


def test_a_missing_file_after_a_restart_is_reported_as_missing(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")

    broadcast_recording.reconcile_recordings(engine, directory)
    record = broadcast_recording.get_recording(engine, session_id=1)
    assert record.status == broadcast_recording.STATUS_MISSING


def test_an_unplayable_part_file_is_failed_not_partial(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    (directory / "broadcast-000001.webm.part").write_bytes(b"rubbish" * 200)
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")

    broadcast_recording.reconcile_recordings(engine, directory)
    record = broadcast_recording.get_recording(engine, session_id=1)
    if broadcast_recording._ffprobe_binary() is None:
        pytest.skip("ffprobe is not installed on this machine")
    assert record.status == broadcast_recording.STATUS_FAILED


def test_reconciliation_leaves_finished_recordings_alone(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")
    broadcast_recording.finish_record(
        engine, session_id=1, status=broadcast_recording.STATUS_AVAILABLE)

    broadcast_recording.reconcile_recordings(engine, directory)
    assert broadcast_recording.get_recording(engine, session_id=1).status == \
        broadcast_recording.STATUS_AVAILABLE


def test_one_broadcasts_reconciliation_does_not_touch_another(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    broadcast_recording.start_record(engine, session_id=1,
                                     file_name="broadcast-000001.webm")
    broadcast_recording.start_record(engine, session_id=2,
                                     file_name="broadcast-000002.webm")
    broadcast_recording.finish_record(
        engine, session_id=2, status=broadcast_recording.STATUS_AVAILABLE)

    broadcast_recording.reconcile_recordings(engine, directory)
    assert broadcast_recording.get_recording(engine, session_id=1).status == \
        broadcast_recording.STATUS_MISSING
    assert broadcast_recording.get_recording(engine, session_id=2).status == \
        broadcast_recording.STATUS_AVAILABLE


# ===========================================================================
# 23-26  Concurrency and deletion
# ===========================================================================
def test_two_concurrent_broadcasts_record_separately(engine, tmp_path):
    directory = tmp_path / "recordings"
    first = broadcast_recording.RecordingWriter(session_id=1, directory=directory)
    second = broadcast_recording.RecordingWriter(session_id=2, directory=directory)

    async def go():
        first.start()
        second.start()
        first.offer(b"FIRST-BROADCAST")
        second.offer(b"SECOND-BROADCAST")
        return await first.close(), await second.close()

    asyncio.run(go())
    assert first.final_path.read_bytes() == b"FIRST-BROADCAST"
    assert second.final_path.read_bytes() == b"SECOND-BROADCAST"
    assert first.final_path != second.final_path


def test_deleting_a_recording_removes_its_file_and_its_part(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    (directory / "broadcast-000001.webm").write_bytes(b"audio")
    (directory / "broadcast-000001.webm.part").write_bytes(b"leftover")

    assert broadcast_recording.remove_recording_file(
        directory, "broadcast-000001.webm") is True
    assert not (directory / "broadcast-000001.webm").exists()
    assert not (directory / "broadcast-000001.webm.part").exists()


def test_deleting_one_recording_leaves_the_others(engine, tmp_path):
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    (directory / "broadcast-000001.webm").write_bytes(b"one")
    (directory / "broadcast-000002.webm").write_bytes(b"two")

    broadcast_recording.remove_recording_file(directory, "broadcast-000001.webm")
    assert not (directory / "broadcast-000001.webm").exists()
    assert (directory / "broadcast-000002.webm").read_bytes() == b"two"
    # And the directory itself is still there. Never removed, ever.
    assert directory.exists()


def test_removing_an_absent_file_is_not_an_error(engine, tmp_path):
    """Delete must not fail because the audio has already gone."""
    directory = tmp_path / "recordings"
    directory.mkdir(parents=True)
    assert broadcast_recording.remove_recording_file(
        directory, "broadcast-000404.webm") is False


def test_deleting_the_metadata_row_leaves_other_rows(engine):
    broadcast_recording.start_record(engine, session_id=1, file_name="a.webm")
    broadcast_recording.start_record(engine, session_id=2, file_name="b.webm")
    broadcast_recording.delete_record(engine, session_id=1)
    assert broadcast_recording.get_recording(engine, session_id=1) is None
    assert broadcast_recording.get_recording(engine, session_id=2) is not None


# ===========================================================================
# 27-28  What can never be recorded
# ===========================================================================
def test_the_recorder_reads_only_the_broadcaster_socket(engine):
    """Store ambient audio is excluded structurally, not by a filter.

    The recording is fed from ONE place in the server - the broadcaster
    uplink, immediately after fan-out. Receiver sockets are never read by the
    recorder, so shop microphones and LinkGuard monitoring cannot reach it
    even in principle. A filter could be got round; an absent wire cannot.
    """
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    offers = [line.strip() for line in source.splitlines()
              if ".offer(" in line and not line.strip().startswith("#")]
    assert len(offers) == 1, f"the recorder is fed from {len(offers)} places: {offers}"
    assert "recorder.offer(data)" in offers[0]

    # And that one call site is inside the broadcaster WebSocket handler.
    index = source.index("recorder.offer(data)")
    preceding = source[:index]
    assert preceding.rindex("/api/ws/broadcaster") > preceding.rindex("/api/ws/receiver")


def test_the_recording_directory_is_git_ignored():
    """A recording of a real announcement must never reach the repository."""
    result = subprocess.run(
        ["git", "check-ignore", "data/recordings/broadcast-000123.webm"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, "recordings are not gitignored"


def test_the_suite_can_never_write_into_the_live_recordings_directory():
    """Recordings resolve outside the repository during tests.

    This is not hypothetical. Backend tests that start a broadcast wrote their
    .part files straight into the repository's data/recordings - which on the
    HQ machine is the folder holding real announcement audio - and left
    zero-byte orphans there with no metadata row pointing at them. The database
    had been scoped since the beginning; the data directory had not, and a
    stray .part file is invisible until somebody lists the folder.
    """
    # conftest is not importable by name from a test module, so the guarantee
    # is asserted directly against the same resolution the product uses.
    import broadcast_recording

    scoped = broadcast_recording.recordings_directory().resolve()
    live = (REPOSITORY_ROOT / "data" / "recordings").resolve()
    assert scoped != live
    assert REPOSITORY_ROOT.resolve() not in scoped.parents
