"""Compose the Windows EXE icon from the website's own favicon assets.

WHY THIS EXISTS RATHER THAN A CHECKED-IN BINARY NOBODY CAN REPRODUCE

Every shipped SpeakLink executable must carry the same icon the website uses.
The website already owns that artwork in ``frontend/public``; this script
assembles those exact files into one multi-resolution Windows .ico so the
desktop identity is DERIVED from the web identity rather than maintained
beside it. Re-run it if the favicon ever changes.

NO RESAMPLING, AND THEREFORE NO REDRAW

There is no image library in this project's environment, and adding one to
resize a logo would be a dependency on every developer machine for a build
step that runs occasionally. So this copies existing images at their existing
sizes:

  16, 32, 48   the three entries already inside frontend/public/favicon.ico
  192          android-chrome-192x192.png, embedded as PNG (Vista and later
               read PNG-compressed ICO entries directly)

That covers Explorer's small, medium and large views. It does NOT contain a
literal 256x256 entry, because producing one would mean resampling the 512
PNG and this script will not silently invent pixels. Windows scales the 192
entry for 256-pixel views; the artwork is identical either way.

An ICO entry's width and height are single bytes, so 512 cannot be
represented at all - 256 is the format's maximum, with 0 meaning 256.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PUBLIC = REPOSITORY_ROOT / "frontend" / "public"
WEBSITE_FAVICON = PUBLIC / "favicon.ico"
LARGE_PNG = PUBLIC / "android-chrome-192x192.png"
OUTPUT = REPOSITORY_ROOT / "assets" / "speaklink.ico"


def _read_ico_entries(path: Path):
    """Every image inside an existing .ico, as (width, height, bpp, payload)."""
    raw = path.read_bytes()
    _reserved, kind, count = struct.unpack("<HHH", raw[:6])
    if kind != 1:
        raise SystemExit(f"{path} is not an icon file")
    entries = []
    for index in range(count):
        offset = 6 + index * 16
        width, height, _colours, _r, _planes, bpp, size, data_offset = struct.unpack(
            "<BBBBHHII", raw[offset:offset + 16])
        entries.append((width or 256, height or 256, bpp,
                        raw[data_offset:data_offset + size]))
    return entries


def _png_size(path: Path):
    """Width and height from a PNG header, without decoding the image."""
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"{path} is not a PNG")
    width, height = struct.unpack(">II", raw[16:24])
    return width, height, raw


def build(output: Path = OUTPUT) -> Path:
    images = []
    for width, height, bpp, payload in _read_ico_entries(WEBSITE_FAVICON):
        images.append((width, height, bpp, payload))

    width, height, payload = _png_size(LARGE_PNG)
    if width > 256 or height > 256:
        raise SystemExit(
            f"{LARGE_PNG} is {width}x{height}; an ICO entry cannot exceed 256")
    images.append((width, height, 32, payload))

    images.sort(key=lambda item: item[0])

    header = struct.pack("<HHH", 0, 1, len(images))
    directory = b""
    body = b""
    offset = len(header) + 16 * len(images)
    for width, height, bpp, payload in images:
        directory += struct.pack(
            "<BBBBHHII",
            0 if width == 256 else width,
            0 if height == 256 else height,
            0, 0, 1, bpp, len(payload), offset,
        )
        body += payload
        offset += len(payload)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + directory + body)
    return output


def main(argv=None) -> int:
    written = build()
    entries = _read_ico_entries(written)
    print(f"wrote {written} ({written.stat().st_size} bytes)")
    for width, height, bpp, payload in entries:
        print(f"  {width}x{height}  {bpp}bpp  {len(payload)} bytes")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main(sys.argv[1:]))
