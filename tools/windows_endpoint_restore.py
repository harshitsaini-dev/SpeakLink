"""Remember a Store's Windows audio state so a crash cannot keep it.

WHY THIS FILE EXISTS AT ALL

Endpoint master volume and mute are persistent Windows settings. When EchoCast
only scaled its own PCM there was nothing to put back; now that it moves the
real master, a Receiver that dies mid-announcement would leave a shop at the
announcement level - or unmuted at midnight - with nothing on the machine
knowing why.

So the original state is written to disk BEFORE the first mutation, and the
record is removed only after it has been put back. Startup checks for a
leftover record and restores it. That ordering is the whole design: a record
that exists when no broadcast is running means the last one did not finish.

WHAT IS IN IT, AND WHAT IS NOT

Session id, endpoint id, the two original values, a timestamp. No credential,
no token, no Settings Password, nothing derived from any of them. This file is
read by support and copied into diagnostics bundles; it has to be safe to
paste into an email.

Written atomically - temporary file, flush, fsync, replace - because the case
it exists for is the machine losing power, and a half-written record is worse
than none: it would either fail to parse or, worse, parse into a plausible
wrong volume.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

RECORD_NAME = "windows-endpoint-restore.json"

#: Bumped only if the shape changes incompatibly. A record from a future
#: version is ignored rather than guessed at.
RECORD_VERSION = 1


class RestoreRecordError(RuntimeError):
    """The record exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class RestoreRecord:
    session_id: int
    endpoint_id: str
    original_volume_percent: int
    original_muted: bool
    written_at_utc: str

    def as_dict(self) -> dict:
        return {
            "version": RECORD_VERSION,
            "session_id": self.session_id,
            "endpoint_id": self.endpoint_id,
            "original_volume_percent": self.original_volume_percent,
            "original_muted": self.original_muted,
            "written_at_utc": self.written_at_utc,
        }


def default_record_path(state_root: Path | str | None = None) -> Path:
    root = Path(state_root) if state_root is not None else (
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "EchoCast-AI" / "receiver")
    return root / RECORD_NAME


def write_record(path: Path | str, *, session_id: int, endpoint_id: str,
                 original_volume_percent: int, original_muted: bool) -> RestoreRecord:
    """Record the pre-broadcast state. Atomic, and never partial."""
    record = RestoreRecord(
        session_id=int(session_id),
        endpoint_id=str(endpoint_id),
        original_volume_percent=int(original_volume_percent),
        original_muted=bool(original_muted),
        written_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record.as_dict(), stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return record


def read_record(path: Path | str) -> RestoreRecord | None:
    """The leftover record, or None. A corrupt one is an error, not a None.

    Silently treating unreadable JSON as "nothing to restore" would leave a
    shop at the announcement volume and say nothing, which is the exact
    failure this file exists to prevent.
    """
    target = Path(path)
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise RestoreRecordError(
            "The Windows audio restoration record is unreadable, so the "
            "output's previous volume is not known. Check it in Store Setup."
        ) from failure
    if payload.get("version") != RECORD_VERSION:
        raise RestoreRecordError(
            "The Windows audio restoration record was written by a different "
            "version of EchoCast and will not be applied.")
    try:
        return RestoreRecord(
            session_id=int(payload["session_id"]),
            endpoint_id=str(payload["endpoint_id"]),
            original_volume_percent=int(payload["original_volume_percent"]),
            original_muted=bool(payload["original_muted"]),
            written_at_utc=str(payload.get("written_at_utc", "")),
        )
    except (KeyError, TypeError, ValueError) as failure:
        raise RestoreRecordError(
            "The Windows audio restoration record is incomplete.") from failure


def clear_record(path: Path | str) -> bool:
    """Remove the record. Only ever called AFTER a successful restore."""
    target = Path(path)
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return False


def recover_on_startup(path: Path | str, *, apply_state, read_state=None,
                       logger=None) -> dict:
    """Put back a state a previous run never restored.

    Returns a short, secret-free summary for the log and for Status. The
    record is cleared ONLY when the endpoint really went back, so a Receiver
    that starts while the device is unplugged tries again next time instead of
    forgetting.
    """
    try:
        record = read_record(path)
    except RestoreRecordError as failure:
        return {"recovered": False, "reason": str(failure)}
    if record is None:
        return {"recovered": False, "reason": "no unfinished override"}

    try:
        applied = apply_state(record.endpoint_id,
                              volume_percent=record.original_volume_percent,
                              muted=record.original_muted)
    except Exception as failure:
        # Deliberately NOT cleared. The endpoint may be unplugged right now
        # and present again in ten minutes; forgetting here would strand the
        # Store at the announcement volume for good.
        return {"recovered": False, "endpoint_id": record.endpoint_id,
                "reason": str(failure)}

    clear_record(path)
    summary = {
        "recovered": True,
        "endpoint_id": record.endpoint_id,
        "restored_volume_percent": getattr(applied, "volume_percent",
                                           record.original_volume_percent),
        "restored_muted": getattr(applied, "muted", record.original_muted),
        "from_session_id": record.session_id,
    }
    if logger is not None:
        logger.info(
            "Restored the Windows audio output left changed by session %s",
            record.session_id)
    return summary
