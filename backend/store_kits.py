"""The Store Kits this HQ has, and which one is the latest.

WHY HQ SERVES THE KIT AT ALL

Getting a kit onto a Store PC used to mean a USB stick or a shared folder, and
both have the same failure: nobody can tell which build a shop actually
received. A kit fetched FROM HQ is the kit HQ has - the same bytes, with the
same checksum, and HQ can say when it was downloaded.

WHAT COUNTS AS A KIT

A .exe or .zip in the kits directory whose name starts with SpeakLink. The
.exe is the one-file installer a Store actually wants; the .zip is kept
because an operator sometimes needs the unpacked payload to look inside.

Nothing is generated here and nothing is unpacked: this module lists, orders
and reads files that a build produced. A packaging step that produced a broken
kit produces a broken download, and that is correct - HQ is not in a position
to validate somebody else's build, and pretending to would be worse than not
trying.

ORDERING IS BY MODIFICATION TIME, NOT BY NAME. Version strings sort wrongly
(1.10.0 before 1.9.0) and a name is only a claim; the file's own timestamp is
what the machine can actually observe. The name is still shown, because that
is what a person recognises.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: The one prefix a downloadable kit may have. A directory that happens to
#: contain other zips does not turn them into Store Kits.
KIT_PREFIX = "SpeakLink"

#: What may be stored and served. Deliberately short, and checked on the way
#: IN as well as on the way out - a directory that accepted anything would be
#: a file share with an HQ login, and the first thing somebody would put in it
#: is whatever they were emailed.
KIT_SUFFIXES = (".exe", ".zip")

#: A ceiling on an upload. The real installer is around 130MB; this leaves
#: room for it to grow without leaving room for somebody to fill the HQ disk.
MAX_KIT_BYTES = 400 * 1024 * 1024


class KitRefused(RuntimeError):
    """An upload that will not be stored, with a reason fit to show a person."""


@dataclass(frozen=True)
class StoreKit:
    name: str
    size_bytes: int
    modified_at: str
    sha256: str

    def public_dict(self) -> dict:
        return {
            "name": self.name,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            # Published so a Store can check what it received against what HQ
            # holds. It is a checksum, not a signature: it proves the transfer,
            # not the origin.
            "sha256": self.sha256,
        }


def kits_directory(repository_root: Path | None = None) -> Path:
    """Where built kits are put for HQ to serve.

    Under the data directory rather than the repository, for the same reason
    recordings are: it is already gitignored and already excluded from the kit
    itself, so a build artifact cannot be committed by accident.
    """
    configured = os.environ.get("SPEAKLINK_DATA_DIR", "").strip()
    if configured:
        base = Path(configured).expanduser().resolve()
    else:
        root = repository_root or Path(__file__).resolve().parents[1]
        base = root / "data"
    return base / "store-kits"


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def list_kits() -> list[StoreKit]:
    """Every kit, newest first. An empty list is a truthful answer."""
    directory = kits_directory()
    if not directory.exists():
        return []
    found = []
    for path in sorted(directory.iterdir()):
        if (path.suffix.lower() not in KIT_SUFFIXES
                or not path.name.startswith(KIT_PREFIX) or not path.is_file()):
            continue
        stat = path.stat()
        found.append(StoreKit(
            name=path.name,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            sha256=_digest(path),
        ))
    return sorted(found, key=lambda kit: kit.modified_at, reverse=True)


def latest_kit() -> StoreKit | None:
    kits = list_kits()
    return kits[0] if kits else None


def safe_kit_name(raw: str) -> str:
    """The name an uploaded kit will be stored under.

    Built from the SUFFIX and a sanitised stem, never from the caller's string
    as given: a filename that arrives over HTTP is input, and input joined onto
    a directory is how a path traversal happens. Anything that is not a letter,
    digit, dash, underscore or dot is replaced, and the result is forced to
    start with the prefix so an upload cannot hide from the listing that serves
    it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise KitRefused("That upload had no filename.")
    suffix = Path(raw).suffix.lower()
    if suffix not in KIT_SUFFIXES:
        raise KitRefused("A Store Kit must be an .exe installer or a .zip.")
    stem = "".join(character if (character.isalnum() or character in "-_.")
                   else "-" for character in Path(raw).stem)[:80].strip("-.")
    if not stem:
        stem = "StoreKit"
    if not stem.startswith(KIT_PREFIX):
        stem = f"{KIT_PREFIX}-{stem}"
    return f"{stem}{suffix}"


def store_uploaded_kit(raw: bytes, *, filename: str) -> tuple[StoreKit, list[str]]:
    """Write an uploaded kit and remove every earlier one.

    HQ HOLDS EXACTLY ONE KIT, whatever it is called. That is a decision, not a
    limitation: a list of builds means somebody eventually installs the wrong
    one, and "which build is that Store on?" stops having a single answer. The
    newest upload is what every Store gets, and the older files go.

    Returns the stored kit and the names it superseded, so the caller can log
    what was removed and tell the operator rather than doing it silently.

    The write is atomic - the bytes go to a temporary file beside the target
    and are then renamed over it. os.replace is atomic on Windows and POSIX
    alike, so a Store mid-download gets all of the old file or all of the new
    one, never half of each. The old files are only removed AFTER that rename
    succeeds, so a failed upload leaves the estate exactly as it was.
    """
    if not raw:
        raise KitRefused("That file was empty.")
    if len(raw) > MAX_KIT_BYTES:
        raise KitRefused(
            f"A Store Kit must be smaller than {MAX_KIT_BYTES // (1024 * 1024)} MB.")

    name = safe_kit_name(filename)
    if name.endswith(".exe") and not raw.startswith(b"MZ"):
        # The cheapest possible check that this is a Windows executable at all.
        # It does not prove the file is the installer - nothing here can - but
        # it catches the ordinary mistake of uploading the wrong file.
        raise KitRefused("That .exe is not a Windows executable.")
    if name.endswith(".zip") and not raw.startswith(b"PK"):
        raise KitRefused("That .zip is not a zip archive.")

    directory = kits_directory()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    superseded = [kit.name for kit in list_kits() if kit.name != name]

    staging = directory / f".{name}.incoming"
    try:
        staging.write_bytes(raw)
        os.replace(staging, target)
    finally:
        # A failed write must not leave a half-file in the directory the
        # listing reads from - which is also why the staging name starts with a
        # dot and could never be served: it does not begin with the prefix.
        if staging.exists():
            staging.unlink(missing_ok=True)

    # Only now, with the new file safely in place. Removing the old ones first
    # would leave an HQ with nothing to hand out if the write then failed.
    for old in superseded:
        try:
            (directory / old).unlink()
        except OSError:
            # Reported rather than raised: the new kit IS installed, and
            # failing the upload over a leftover file would be a lie about
            # what happened.
            superseded = [n for n in superseded if n != old]

    stat = target.stat()
    return StoreKit(
        name=name,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        sha256=hashlib.sha256(raw).hexdigest(),
    ), superseded


def delete_kit(name: str) -> bool:
    """Remove one kit. Matched against the listing, never joined from input."""
    path = resolve_kit_path(name)
    if path is None:
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def resolve_kit_path(name: str) -> Path | None:
    """The file for a named kit, or None.

    The name is matched against the DIRECTORY LISTING rather than joined onto
    the path. A caller-supplied name joined to a directory is a path traversal
    waiting to happen, and no amount of checking for ".." afterwards is as
    convincing as never building the path from the input at all.
    """
    for kit in list_kits():
        if kit.name == name:
            return kits_directory() / kit.name
    return None
