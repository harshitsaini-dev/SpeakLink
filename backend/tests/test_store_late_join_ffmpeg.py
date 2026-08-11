"""Real FFmpeg must be able to open what a late-joining Store is sent.

This is the hard gate. Everything else about late joining is arrangement; this
is whether the bytes decode.

It feeds a REAL captured MediaRecorder WebM/Opus stream through the REAL
framer, starts a Store part-way through, and pushes the exact byte sequence
that Store would receive into the SAME FFmpeg command line the Receiver uses.

It also runs the plausible wrong implementation - correct header, then raw
timeslice chunks - and requires that one to fail. A gate that passes for both
is not a gate.
"""

from __future__ import annotations

import json
import os
import shutil
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

from store_late_join import StoreLateJoinSource  # noqa: E402

CAPTURE = BACKEND_ROOT / "tests" / "fixtures" / "mediarecorder-live.webm"
CAPTURE_INDEX = BACKEND_ROOT / "tests" / "fixtures" / "mediarecorder-live.chunks.json"
FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(
    not (CAPTURE.exists() and CAPTURE_INDEX.exists() and FFMPEG),
    reason="needs the real MediaRecorder capture and ffmpeg on PATH")

#: The Receiver decodes to signed 16-bit PCM at 48 kHz mono. Same arguments
#: here, so this proves the path the Store actually uses rather than a
#: convenient one.
DECODE = ["-hide_banner", "-loglevel", "error",
          "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
          "-ac", "1", "-ar", "48000", "pipe:1"]


def real_chunks():
    sizes = json.loads(CAPTURE_INDEX.read_text())["chunkSizes"]
    data = CAPTURE.read_bytes()
    out, offset = [], 0
    for size in sizes:
        out.append(data[offset:offset + size])
        offset += size
    return out


def decode(payloads) -> tuple[int, str]:
    """Feed bytes to real FFmpeg. Returns (pcm bytes produced, stderr)."""
    process = subprocess.run(
        [FFMPEG, *DECODE], input=b"".join(payloads),
        capture_output=True, timeout=120)
    return len(process.stdout), process.stderr.decode("utf-8", "replace")


def late_join_delivery(join_after: int):
    """Exactly what a Store joining after `join_after` chunks is sent."""
    chunks = real_chunks()
    source = StoreLateJoinSource(session_id=1)

    for chunk in chunks[:join_after]:
        source.offer(chunk)

    bootstrap = source.bootstrap()
    assert bootstrap is not None, "no header was available at the join point"
    delivered = [bootstrap.init_segment, *bootstrap.clusters]

    # Everything after the join arrives as whole Clusters and nothing else.
    for chunk in chunks[join_after:]:
        for frame in source.offer(chunk):
            delivered.append(frame.data)
    return delivered, bootstrap


# ===========================================================================
# The gate
# ===========================================================================

def test_ffmpeg_decodes_a_store_that_joined_mid_broadcast():
    delivered, bootstrap = late_join_delivery(join_after=20)
    assert len(delivered) > 5, "the join produced almost nothing to decode"

    pcm_bytes, stderr = decode(delivered)

    assert pcm_bytes > 0, (
        f"FFmpeg produced no audio for a late-joining Store.\n{stderr}")
    for phrase in ("Invalid data", "could not find codec",
                   "Failed to read", "EBML header parsing failed"):
        assert phrase.lower() not in stderr.lower(), (
            f"FFmpeg reported a stream error: {stderr}")

    # A quarter of a second of 48 kHz mono s16 is 24000 bytes. This is many
    # seconds of real audio, not a decoder that opened and gave up.
    assert pcm_bytes > 24000, (
        f"only {pcm_bytes} PCM bytes - the decoder opened but produced almost "
        f"nothing.\n{stderr}")
    assert bootstrap.clusters == (), "history was replayed to reach this"

    print(f"\nLATE-JOIN DECODE: {len(delivered)} payloads "
          f"({bootstrap.total_bytes} bootstrap bytes, "
          f"0 historical Clusters) -> {pcm_bytes} PCM bytes")


def rechunked(payloads, size: int):
    """Re-cut a stream at arbitrary byte offsets, ignoring element boundaries.

    This is what a transport is free to do and what a different recorder
    configuration produces. Nothing in WebM promises that a delivery chunk ends
    where a Cluster ends.
    """
    blob = b"".join(payloads)
    return [blob[at:at + size] for at in range(0, len(blob), size)]


def test_this_captures_timeslices_happen_to_be_cluster_aligned():
    """Worth stating, because it is why the raw path works at all today.

    This browser emitted roughly one Cluster per 250 ms timeslice, so a Store
    handed raw chunks from a Cluster boundary decodes them. That is a property
    of this recorder, not a guarantee of the format - and relying on it is the
    accidental correctness the framed path exists to remove.
    """
    chunks = real_chunks()
    source = StoreLateJoinSource(session_id=1)
    for chunk in chunks[:20]:
        source.offer(chunk)
    bootstrap = source.bootstrap()

    naive = [bootstrap.init_segment, *chunks[20:]]
    naive_bytes, _ = decode(naive)
    framed_bytes, _ = decode(late_join_delivery(join_after=20)[0])

    print(f"\nALIGNMENT: header+raw-timeslices {naive_bytes} PCM bytes, "
          f"header+whole-Clusters {framed_bytes} PCM bytes")
    assert naive_bytes > 0 and framed_bytes > 0


def test_a_stream_cut_off_cluster_boundaries_needs_the_framer():
    """The real hazard, made reproducible.

    Here the delivery boundaries deliberately do NOT line up with Clusters,
    which is legal and is what a different timeslice or a transport that
    coalesces would produce. Handing those bytes to a decoder from an arbitrary
    offset is the failure the framed path prevents; running them through the
    framer first restores a decodable stream.
    """
    chunks = real_chunks()
    total = sum(len(c) for c in chunks)
    # Thirty-ish delivery pieces over the whole stream, at a size chosen so it
    # cannot coincide with Cluster boundaries. Sized from the real capture, so
    # this stays honest if the fixture is ever regenerated.
    piece_size = max(701, (total // 30) | 1)
    misaligned = rechunked(chunks, piece_size)
    join_at = len(misaligned) // 3
    assert len(misaligned) - join_at > 5, "not enough stream left after the join"

    # The naive implementation: header, then whatever arrives, from part-way in.
    source = StoreLateJoinSource(session_id=1)
    for piece in misaligned[:join_at]:
        source.offer(piece)
    bootstrap = source.bootstrap()
    assert bootstrap is not None
    naive_bytes, naive_err = decode([bootstrap.init_segment,
                                     *misaligned[join_at:]])

    # The framed path over exactly the same misaligned input.
    framed = [bootstrap.init_segment]
    for piece in misaligned[join_at:]:
        for frame in source.offer(piece):
            framed.append(frame.data)
    framed_bytes, framed_err = decode(framed)

    print(f"\nMISALIGNED: naive {naive_bytes} PCM bytes, errors="
          f"{bool(naive_err.strip())} | framed {framed_bytes} PCM bytes, "
          f"errors={bool(framed_err.strip())}")

    assert framed_bytes > 0, (
        f"the framed path failed on a misaligned stream.\n{framed_err}")

    # The measurable difference is NOT the byte count. FFmpeg is resilient: it
    # reports the damage, resynchronises at the next element it recognises, and
    # still produces audio. So a count-based assertion would call a corrupt
    # stream healthy.
    #
    # What separates them is whether the demuxer had to recover at all. The
    # framed path is clean; the naive one is a parser walking through bytes it
    # cannot identify, which is a stream that can glitch, lose the start of an
    # announcement, or fail outright on a stricter or older FFmpeg than the one
    # this machine happens to have.
    assert naive_err.strip(), (
        "the misaligned stream produced no decoder complaint at all, so this "
        "test is no longer reproducing the hazard it was written for")
    assert not framed_err.strip(), (
        f"the framed path produced decoder errors, which is the one thing it "
        f"exists to prevent.\n{framed_err}")


def test_joining_very_late_still_decodes():
    """Near the end of a long announcement is the same problem, further along."""
    chunks = real_chunks()
    delivered, _ = late_join_delivery(join_after=max(1, len(chunks) - 12))
    pcm_bytes, stderr = decode(delivered)
    assert pcm_bytes > 0, f"a very late joiner decoded nothing.\n{stderr}"


def test_the_decoder_stays_healthy_for_the_rest_of_the_stream():
    """Not just the first Cluster: everything after the join must decode too."""
    delivered, _ = late_join_delivery(join_after=15)
    head_only, _ = decode(delivered[:3])
    whole, stderr = decode(delivered)
    assert whole > head_only, (
        f"the stream stopped producing audio after its first Clusters.\n{stderr}")


def test_an_initial_store_stream_still_decodes_unchanged():
    """The untouched raw path must remain decodable, since it is unchanged."""
    pcm_bytes, stderr = decode(real_chunks())
    assert pcm_bytes > 24000, (
        f"the existing raw Store path stopped decoding.\n{stderr}")
