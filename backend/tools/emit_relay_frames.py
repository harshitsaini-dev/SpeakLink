"""Emit the frames the REAL relay produces, for the browser to decode.

The browser-side late-join proof must decode what the product actually sends. If
the Playwright test framed the stream itself it would prove only that two
implementations of the same idea agree - and the whole point of the late-join
gate was that the obvious idea is wrong.

So this runs the shipped ``WebmStreamFramer`` and ``WebAudienceRelay`` over a
real MediaRecorder capture and writes exactly what a listener would receive:
the initialization segment, then every whole Cluster in order. Sizes and offsets
only in the index - the payload is written as opaque bytes.

Not part of the server. A build step for tests, run on demand.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from webm_stream import WebmStreamFramer  # noqa: E402


def emit(capture: Path, index_json: Path, out_bin: Path, out_index: Path) -> dict:
    data = capture.read_bytes()
    sizes = json.loads(index_json.read_text())["chunkSizes"]

    framer = WebmStreamFramer()
    init: bytes | None = None
    clusters: list[bytes] = []

    offset = 0
    for size in sizes:
        # Fed exactly as the broadcaster socket delivers it.
        for frame in framer.feed(data[offset:offset + size]):
            if frame.is_init:
                init = frame.data
            else:
                clusters.append(frame.data)
        offset += size

    if init is None:
        raise SystemExit("the capture produced no initialization segment")

    blob = bytearray(init)
    entries = [{"kind": "init", "offset": 0, "length": len(init)}]
    for cluster in clusters:
        entries.append({"kind": "cluster", "offset": len(blob), "length": len(cluster)})
        blob.extend(cluster)

    out_bin.parent.mkdir(parents=True, exist_ok=True)
    out_bin.write_bytes(bytes(blob))
    summary = {
        "mime": "audio/webm;codecs=opus",
        "initBytes": len(init),
        "clusterCount": len(clusters),
        "totalBytes": len(blob),
        "frames": entries,
    }
    out_index.write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path,
                        default=BACKEND_ROOT / "tests/fixtures/mediarecorder-live.webm")
    parser.add_argument("--capture-index", type=Path,
                        default=BACKEND_ROOT / "tests/fixtures/mediarecorder-live.chunks.json")
    parser.add_argument("--out", type=Path,
                        default=BACKEND_ROOT.parent / "frontend/e2e/fixtures/relay-frames.bin")
    parser.add_argument("--out-index", type=Path,
                        default=BACKEND_ROOT.parent / "frontend/e2e/fixtures/relay-frames.json")
    args = parser.parse_args()

    if not args.capture.exists():
        print("no capture; run: SPEAKLINK_CAPTURE_WEBM=1 npx playwright test "
              "e2e/capture-fixture.spec.js", file=sys.stderr)
        return 2

    summary = emit(args.capture, args.capture_index, args.out, args.out_index)
    print(f"init {summary['initBytes']} B, {summary['clusterCount']} clusters, "
          f"{summary['totalBytes']} B total -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
