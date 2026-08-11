"""Images sent in a Broadcast's chat.

An upload endpoint is the widest door in an application, so this module is
deliberately narrow about what it lets through.

WHAT AN UPLOAD IS ALLOWED TO BE

Nothing here trusts the filename or the declared content type - both are
supplied by the caller and neither is evidence. Every upload is DECODED with
Pillow and then RE-ENCODED from the decoded pixels. That is the whole defence,
and it is stronger than any check on bytes:

  * a file that is not really an image fails to decode, so it is refused;
  * a polyglot - a valid PNG whose tail is a script, or an SVG with JavaScript
    in it - loses everything that is not pixels, because what gets written is
    a fresh file encoded from the decoded image;
  * EXIF goes with it, which matters because a phone photograph carries the
    GPS coordinates of the shop that took it;
  * a decompression bomb is caught by a pixel-count ceiling before it is ever
    fully decoded.

WHERE THEY LIVE

Under the launcher's data directory, one folder per Broadcast, filename a
random UUID. Never the name the caller supplied: a caller-chosen filename is
how a path traversal happens, and there is nothing an operator gains from
"IMG_2247.jpg" appearing on the disk.

The folder is removed when the Broadcast is deleted from history, so an image
never outlives the conversation it was part of - the same rule the messages
themselves follow.
"""

from __future__ import annotations

import io
import os
import shutil
import uuid
from pathlib import Path

#: What an operator or listener may send. Deliberately three formats, all of
#: them ones Pillow can decode and re-encode losslessly enough for a photo of
#: a shop display. No SVG - it is a document that can contain script, not an
#: image - and no GIF, whose only distinctive feature here would be animation
#: nobody asked for.
ALLOWED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}

#: The upload ceiling. Generous for a phone photograph of a speaker or a
#: screenshot of an error, useless for anything else.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: Refused before decoding finishes. A 20,000 x 20,000 PNG is a few hundred
#: kilobytes on disk and 1.2GB of RAM decoded - the classic decompression bomb.
MAX_PIXELS = 40_000_000

#: What is actually stored. Anything larger is scaled down: nobody reads chat
#: at 4000px, and the image is going into a small panel beside a broadcast.
MAX_DIMENSION = 1600


class AttachmentRefused(RuntimeError):
    """An upload that will not be stored, with a reason fit to show a person."""


def attachments_directory(repository_root: Path | None = None) -> Path:
    """Where chat images live.

    Beside recordings, under the same data directory - already gitignored and
    already excluded from the Store Kit, so a photograph from a real shop
    cannot be committed or shipped by accident.
    """
    configured = os.environ.get("SPEAKLINK_DATA_DIR", "").strip()
    if configured:
        base = Path(configured).expanduser().resolve()
    else:
        root = repository_root or Path(__file__).resolve().parents[1]
        base = root / "data"
    return base / "chat-attachments"


def session_directory(session_id: int) -> Path:
    return attachments_directory() / f"session-{int(session_id)}"


def store_image(raw: bytes, *, session_id: int) -> dict:
    """Validate, normalise and write one image. Returns what to record.

    Raises AttachmentRefused with a sentence a person can read. Every refusal
    here is also one the browser should have prevented; it is repeated because
    a control that only exists in a browser is a suggestion.
    """
    if not raw:
        raise AttachmentRefused("That file was empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        megabytes = MAX_UPLOAD_BYTES // (1024 * 1024)
        raise AttachmentRefused(f"Images must be smaller than {megabytes} MB.")

    # Imported here rather than at module import: the rest of SpeakLink runs
    # without Pillow, and a missing image library must not stop HQ booting.
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as missing:  # pragma: no cover - environment guard
        raise AttachmentRefused(
            "This HQ cannot process images. Ask an administrator to install "
            "the imaging library.") from missing

    try:
        probe = Image.open(io.BytesIO(raw))
        image_format = (probe.format or "").upper()
        width, height = probe.size
    except UnidentifiedImageError as bad:
        raise AttachmentRefused("That file is not an image.") from bad
    except Exception as bad:  # pragma: no cover - Pillow's odd corruption paths
        raise AttachmentRefused("That image could not be read.") from bad

    if image_format not in ALLOWED_FORMATS:
        raise AttachmentRefused("Only PNG, JPEG and WebP images can be sent.")
    if width * height > MAX_PIXELS:
        # Refused before a full decode. This is the decompression bomb case:
        # small on disk, enormous in memory.
        raise AttachmentRefused("That image is too large to process.")

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as bad:
        raise AttachmentRefused("That image could not be read.") from bad

    # Re-encoded from decoded pixels, which is what drops EXIF, trailing
    # payloads and anything else that was riding along in the container.
    if image.mode in ("P", "LA", "RGBA") and image_format != "JPEG":
        image = image.convert("RGBA")
    elif image.mode != "RGB":
        image = image.convert("RGB")
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))

    buffer = io.BytesIO()
    save_format = image_format
    if save_format == "JPEG":
        image.save(buffer, format="JPEG", quality=85, optimize=True)
    elif save_format == "PNG":
        image.save(buffer, format="PNG", optimize=True)
    else:
        image.save(buffer, format="WEBP", quality=85)
    payload = buffer.getvalue()

    directory = session_directory(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    # A random name, never the caller's. A caller-chosen filename is how path
    # traversal happens, and the original name is of no use to anyone here.
    name = f"{uuid.uuid4().hex}{EXTENSIONS[save_format]}"
    (directory / name).write_bytes(payload)

    return {
        "attachment_name": name,
        "attachment_mime": ALLOWED_FORMATS[save_format],
        "attachment_bytes": len(payload),
        "attachment_width": image.width,
        "attachment_height": image.height,
    }


def read_image(session_id: int, name: str) -> bytes | None:
    """The stored bytes, or None. Never reaches outside the session folder."""
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = session_directory(session_id) / name
    directory = session_directory(session_id).resolve()
    try:
        resolved = path.resolve()
        # Belt and braces: even with the name checked above, the resolved path
        # must still be inside the folder it is supposed to be in.
        if directory not in resolved.parents:
            return None
        return resolved.read_bytes()
    except (OSError, ValueError):
        return None


def delete_image(session_id: int, name: str) -> bool:
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    path = session_directory(session_id) / name
    try:
        path.unlink()
        return True
    except OSError:
        return False


def delete_session_images(session_id: int) -> int:
    """Remove everything a Broadcast's chat stored. Called when its history is.

    Returns how many files went. Missing is success: the point is that nothing
    is left, not that something was there.
    """
    directory = session_directory(session_id)
    if not directory.exists():
        return 0
    count = sum(1 for entry in directory.iterdir() if entry.is_file())
    shutil.rmtree(directory, ignore_errors=True)
    return count
