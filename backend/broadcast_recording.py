"""Recording the announcement HQ actually sent.

WHAT IS RECORDED, AND WHY IT CANNOT BE ANYTHING ELSE

Exactly the bytes arriving on the broadcaster's microphone WebSocket - the
same buffer handed to ``fanout_audio``. That stream is produced by the HQ
browser AFTER the accepted microphone path (``getUserMedia`` -> ``GainNode``
-> ``MediaRecorder``), so the operator's gain and mute are already baked into
it. Muting the HQ microphone therefore records silence, which is the truth.

It is structurally impossible for this to capture Store ambient sound,
LinkGuard monitoring or shop conversation: none of those ever travel on this
socket. They arrive, if at all, on Receiver connections that this module never
reads. That is a much stronger guarantee than a filter would be.

THE RULE THAT OUTRANKS EVERYTHING ELSE HERE

**A recording must never delay a live announcement.** A shop full of people is
waiting for the audio; a file on a disk is not. So the writer owns a bounded
queue and a background task, the fan-out only ever ``put_nowait``s into it, and
when the queue is full the chunk is DROPPED and the recording is marked
PARTIAL. A slow or failing disk costs a truthful recording status, never a
delivered announcement.

STATES, AND WHY THERE ARE FIVE

    RECORDING - in progress, on disk as .part
    AVAILABLE - finished, validated, atomically renamed
    PARTIAL   - audio was dropped; what exists is real but incomplete
    FAILED    - could not be written or could not be validated
    MISSING   - the row exists, the file does not

PARTIAL and FAILED are deliberately different. "Some of it is there" and "none
of it is usable" send an operator to different places, and collapsing them
would make a recoverable recording look worthless.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("speaklink.recording")

RECORDING_TABLE = "broadcast_recordings"

STATUS_RECORDING = "recording"
STATUS_AVAILABLE = "available"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_MISSING = "missing"

#: Roughly a minute of the 32 kbps Opus the Console produces, at the browser's
#: 250 ms chunk cadence. Big enough that an ordinary disk hiccup is absorbed
#: invisibly, small enough that a genuinely stuck writer cannot consume memory
#: while forty Stores are mid-announcement.
DEFAULT_QUEUE_CAPACITY = 240

PART_SUFFIX = ".part"


class RecordingError(RuntimeError):
    """Base class, so no caller handles one failure and misses another."""


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def recordings_directory(repository_root: Path | None = None) -> Path:
    """Where recordings live.

    Under the launcher's data directory, which is already gitignored and
    already excluded from the Store Kit - so a recording of a real
    announcement cannot be committed or shipped to a shop by accident.
    """
    configured = os.environ.get("SPEAKLINK_DATA_DIR", "").strip()
    if configured:
        base = Path(configured).expanduser().resolve()
    else:
        root = repository_root or Path(__file__).resolve().parents[1]
        base = root / "data"
    return base / "recordings"


def recording_filename(session_id: int) -> str:
    """A name derived from the session id and nothing else.

    Deliberately carries no campaign name, username, credential or token. This
    string appears in directory listings, log lines and support screenshots,
    and a filename is the easiest place in a system to leak something by
    accident.
    """
    return f"broadcast-{int(session_id):06d}.webm"


def ensure_recording_schema(engine: Engine) -> None:
    """Create the metadata table if absent. Safe on every boot, purely additive."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"""
            CREATE TABLE IF NOT EXISTS {RECORDING_TABLE} (
                -- One recording per broadcast, enforced by the schema rather
                -- than remembered by the application.
                session_id INTEGER PRIMARY KEY,
                -- The FILE NAME only, never a full path. What is stored must
                -- not be something a browser could be persuaded to fetch, and
                -- the directory is a deployment detail rather than data.
                file_name VARCHAR(120) NOT NULL,
                status VARCHAR(16) NOT NULL,
                container VARCHAR(16),
                codec VARCHAR(16),
                byte_size INTEGER,
                duration_seconds REAL,
                chunks_written INTEGER NOT NULL DEFAULT 0,
                chunks_dropped INTEGER NOT NULL DEFAULT 0,
                started_at VARCHAR(40) NOT NULL,
                finalized_at VARCHAR(40),
                error VARCHAR(300),
                CONSTRAINT fk_recording_session
                    FOREIGN KEY (session_id) REFERENCES broadcast_sessions(id)
                    ON DELETE CASCADE,
                CONSTRAINT ck_recording_status CHECK (
                    status IN ('recording', 'available', 'partial',
                               'failed', 'missing')
                )
            )
            """
        )
        connection.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS ix_{RECORDING_TABLE}_status "
            f"ON {RECORDING_TABLE}(status)"
        )


@dataclass
class RecordingRecord:
    session_id: int
    file_name: str
    status: str
    container: str | None = None
    codec: str | None = None
    byte_size: int | None = None
    duration_seconds: float | None = None
    chunks_written: int = 0
    chunks_dropped: int = 0
    started_at: str = ""
    finalized_at: str | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "container": self.container,
            "codec": self.codec,
            "byte_size": self.byte_size,
            "duration_seconds": self.duration_seconds,
            "chunks_written": self.chunks_written,
            "chunks_dropped": self.chunks_dropped,
            "started_at": self.started_at,
            "finalized_at": self.finalized_at,
            "error": self.error,
            # file_name is deliberately ABSENT. The browser addresses a
            # recording by its session id; where it lives on this machine is
            # nobody else's business and is not something a client should be
            # able to influence.
        }


_COLUMNS = ("session_id, file_name, status, container, codec, byte_size, "
            "duration_seconds, chunks_written, chunks_dropped, started_at, "
            "finalized_at, error")


def _row_to_record(row) -> RecordingRecord:
    return RecordingRecord(
        session_id=row[0], file_name=row[1], status=row[2], container=row[3],
        codec=row[4], byte_size=row[5], duration_seconds=row[6],
        chunks_written=row[7], chunks_dropped=row[8], started_at=row[9],
        finalized_at=row[10], error=row[11])


def get_recording(engine: Engine, *, session_id: int) -> RecordingRecord | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT {_COLUMNS} FROM {RECORDING_TABLE} "
                 "WHERE session_id = :session_id"),
            {"session_id": session_id}).fetchone()
    return _row_to_record(row) if row else None


def all_recordings(engine: Engine) -> dict[int, RecordingRecord]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT {_COLUMNS} FROM {RECORDING_TABLE}")).fetchall()
    return {row[0]: _row_to_record(row) for row in rows}


def start_record(engine: Engine, *, session_id: int, file_name: str) -> RecordingRecord:
    started_at = _utc_now_text()
    with engine.begin() as connection:
        connection.execute(
            text(f"""
                INSERT INTO {RECORDING_TABLE}
                    (session_id, file_name, status, started_at)
                VALUES (:session_id, :file_name, :status, :started_at)
                ON CONFLICT(session_id) DO UPDATE SET
                    file_name = excluded.file_name,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    finalized_at = NULL,
                    error = NULL
            """),
            {"session_id": session_id, "file_name": file_name,
             "status": STATUS_RECORDING, "started_at": started_at})
    return RecordingRecord(session_id=session_id, file_name=file_name,
                           status=STATUS_RECORDING, started_at=started_at)


def finish_record(engine: Engine, *, session_id: int, status: str,
                  container: str | None = None, codec: str | None = None,
                  byte_size: int | None = None,
                  duration_seconds: float | None = None,
                  chunks_written: int = 0, chunks_dropped: int = 0,
                  error: str | None = None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"""
                UPDATE {RECORDING_TABLE} SET
                    status = :status, container = :container, codec = :codec,
                    byte_size = :byte_size, duration_seconds = :duration_seconds,
                    chunks_written = :chunks_written,
                    chunks_dropped = :chunks_dropped,
                    finalized_at = :finalized_at, error = :error
                WHERE session_id = :session_id
            """),
            {"session_id": session_id, "status": status, "container": container,
             "codec": codec, "byte_size": byte_size,
             "duration_seconds": duration_seconds,
             "chunks_written": chunks_written, "chunks_dropped": chunks_dropped,
             "finalized_at": _utc_now_text(),
             "error": None if error is None else str(error)[:300]})


def delete_record(engine: Engine, *, session_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"DELETE FROM {RECORDING_TABLE} WHERE session_id = :session_id"),
            {"session_id": session_id})


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _ffprobe_binary() -> str | None:
    return shutil.which("ffprobe")


def probe_recording(path: Path) -> dict:
    """Ask ffprobe whether this file is actually playable.

    A recording is a stream of MediaRecorder chunks appended to a file. That
    is USUALLY a valid WebM, because the first chunk carries the header - but
    "usually" is not a thing to tell an operator who needs the audio. So the
    file is probed, and only a file ffprobe can read and report a duration for
    is called AVAILABLE.

    Returns ``{}`` when ffprobe is absent; the caller then keeps the recording
    rather than condemning it, because a missing tool on the HQ machine says
    nothing at all about the file.
    """
    binary = _ffprobe_binary()
    if binary is None:
        return {}
    try:
        completed = subprocess.run(
            [binary, "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
    except Exception as failure:                       # pragma: no cover
        return {"error": str(failure)[:200]}
    if completed.returncode != 0:
        return {"error": (completed.stderr or "ffprobe rejected the file")[:200]}
    try:
        parsed = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return {"error": "ffprobe returned output that could not be read"}

    streams = parsed.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = parsed.get("format") or {}
    duration = fmt.get("duration") or (audio or {}).get("duration")
    return {
        "container": (fmt.get("format_name") or "").split(",")[0] or None,
        "codec": (audio or {}).get("codec_name"),
        "duration_seconds": float(duration) if duration else None,
        "has_audio": audio is not None,
    }


# ---------------------------------------------------------------------------
# The writer
# ---------------------------------------------------------------------------
class RecordingWriter:
    """Writes one broadcast to disk without ever making a Store wait.

    ``offer`` is called from the fan-out path and NEVER blocks or awaits I/O:
    it puts a chunk into a bounded queue and returns. A background task does
    the file work. If the queue is full the chunk is dropped and counted, and
    the recording becomes PARTIAL - a truthful, degraded artefact rather than a
    delayed announcement.
    """

    def __init__(self, *, session_id: int, directory: Path,
                 capacity: int = DEFAULT_QUEUE_CAPACITY) -> None:
        self.session_id = session_id
        self.directory = Path(directory)
        self.file_name = recording_filename(session_id)
        self.final_path = self.directory / self.file_name
        #: Written to a .part first, so a half-written file can never be
        #: mistaken for a finished recording - not by the API, not by an
        #: operator browsing the folder, not by a restart.
        self.part_path = self.directory / (self.file_name + PART_SUFFIX)

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=capacity)
        self._task: asyncio.Task | None = None
        self._handle = None
        self.chunks_written = 0
        self.chunks_dropped = 0
        self.bytes_written = 0
        self.failed = False
        self.error: str | None = None
        self._closed = False

    @property
    def capacity(self) -> int:
        return self._queue.maxsize

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def start(self) -> None:
        try:
            # Creating the directory is inside the guard too. It can fail for
            # the same reasons opening the file can - permissions, a file in
            # the way, a disconnected drive - and an exception escaping here
            # would take out the broadcast that was about to start.
            self.directory.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.part_path, "wb")
        except Exception as failure:
            # The broadcast is not affected in any way. Only the recording is.
            self.failed = True
            self.error = f"the recording file could not be opened: {failure}"
            logger.warning("Recording for session %s could not start: %s",
                           self.session_id, failure)
            return
        self._task = asyncio.create_task(self._drain())

    def offer(self, chunk: bytes) -> bool:
        """Called from the fan-out. Never blocks, never awaits, never raises.

        Returns False when the chunk was dropped, which the caller does not
        need to act on - a dropped chunk is a recording problem and the
        announcement has already gone out.
        """
        if self._closed or self.failed or self._handle is None:
            return False
        try:
            self._queue.put_nowait(chunk)
            return True
        except asyncio.QueueFull:
            # The disk is not keeping up. The shop still gets its audio.
            self.chunks_dropped += 1
            return False

    async def _drain(self) -> None:
        while True:
            chunk = await self._queue.get()
            if chunk is None:                      # the close sentinel
                self._queue.task_done()
                return
            try:
                # Blocking file I/O moved off the event loop: writing inline
                # would make a slow disk stall the very loop that is feeding
                # every Store.
                await asyncio.to_thread(self._handle.write, chunk)
                self.chunks_written += 1
                self.bytes_written += len(chunk)
            except Exception as failure:
                self.failed = True
                self.error = f"the recording could not be written: {failure}"
                logger.warning("Recording write failed for session %s: %s",
                               self.session_id, failure)
                self._queue.task_done()
                return
            self._queue.task_done()

    async def close(self) -> str:
        """Flush, close, validate, and atomically publish. Returns the status.

        Never raises. Every caller is a broadcast ending - normal stop,
        emergency stop, a dropped microphone, server cleanup - and an exception
        here would turn the end of an announcement into a crash.
        """
        if self._closed:
            return self._status()
        self._closed = True

        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._queue.put(None)
                await asyncio.wait_for(self._task, timeout=30)
        if self._handle is not None:
            with contextlib.suppress(Exception):
                self._handle.flush()
                os.fsync(self._handle.fileno())
                self._handle.close()

        if self.failed:
            return STATUS_FAILED
        if self.chunks_written == 0:
            self.error = "no audio was recorded"
            return STATUS_FAILED

        try:
            # Atomic on the same filesystem: the finished name appears whole or
            # not at all. Nothing ever sees a partially written recording under
            # the real name.
            os.replace(self.part_path, self.final_path)
        except Exception as failure:
            self.failed = True
            self.error = f"the recording could not be finalized: {failure}"
            return STATUS_FAILED

        return self._status()

    def _status(self) -> str:
        if self.failed:
            return STATUS_FAILED
        # Dropped chunks mean there is a real gap in the audio. Saying
        # AVAILABLE would promise something the file does not contain.
        return STATUS_PARTIAL if self.chunks_dropped else STATUS_AVAILABLE


# ---------------------------------------------------------------------------
# Crash reconciliation
# ---------------------------------------------------------------------------
def reconcile_recordings(engine: Engine, directory: Path) -> list[dict]:
    """Resolve recordings left mid-flight by a crash or restart.

    An unfinished ``.part`` is NEVER promoted to AVAILABLE. HQ stopping in the
    middle of an announcement is exactly when a recording is least trustworthy,
    and a file that has not been flushed, closed or probed has no business
    being offered to an operator as a finished one.
    """
    outcomes: list[dict] = []
    directory = Path(directory)
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT session_id, file_name FROM {RECORDING_TABLE} "
                 "WHERE status = :status"),
            {"status": STATUS_RECORDING}).fetchall()

    for session_id, file_name in rows:
        final_path = directory / file_name
        part_path = directory / (file_name + PART_SUFFIX)

        if part_path.exists() and part_path.stat().st_size > 0:
            # There IS audio, and it may well be most of the announcement - but
            # it was never finalized, so it is PARTIAL rather than AVAILABLE.
            probed = probe_recording(part_path)
            if probed.get("error") or probed.get("has_audio") is False:
                finish_record(engine, session_id=session_id,
                              status=STATUS_FAILED,
                              error="interrupted and not playable")
                outcomes.append({"session_id": session_id, "status": STATUS_FAILED})
                continue
            try:
                os.replace(part_path, final_path)
            except Exception as failure:
                finish_record(engine, session_id=session_id, status=STATUS_FAILED,
                              error=f"could not recover: {failure}")
                outcomes.append({"session_id": session_id, "status": STATUS_FAILED})
                continue
            finish_record(
                engine, session_id=session_id, status=STATUS_PARTIAL,
                container=probed.get("container"), codec=probed.get("codec"),
                duration_seconds=probed.get("duration_seconds"),
                byte_size=final_path.stat().st_size,
                error="HQ restarted while this broadcast was recording")
            outcomes.append({"session_id": session_id, "status": STATUS_PARTIAL})
            continue

        if final_path.exists():
            finish_record(engine, session_id=session_id, status=STATUS_PARTIAL,
                          byte_size=final_path.stat().st_size,
                          error="HQ restarted while this broadcast was recording")
            outcomes.append({"session_id": session_id, "status": STATUS_PARTIAL})
            continue

        # The row says there should be audio and there is none.
        finish_record(engine, session_id=session_id, status=STATUS_MISSING,
                      error="no recording file was found after a restart")
        outcomes.append({"session_id": session_id, "status": STATUS_MISSING})

    return outcomes


def remove_recording_file(directory: Path, file_name: str) -> bool:
    """Delete one recording's audio. Never touches anything else.

    Takes a FILE NAME and joins it here, and refuses anything that escapes the
    directory. A recordings folder is exactly the kind of place a path bug
    turns into a catastrophe, so the guard is explicit rather than assumed.
    """
    directory = Path(directory).resolve()
    candidate = (directory / file_name).resolve()
    if candidate.parent != directory:
        raise RecordingError("a recording file must live in the recordings directory")
    removed = False
    for path in (candidate, Path(str(candidate) + PART_SUFFIX)):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed = True
    return removed
