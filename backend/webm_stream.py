"""Incremental WebM framing for the live web audience, and nothing more.

WHY THIS EXISTS

A browser listener joining a Broadcast already in progress needs two things
before it can decode: the initialization segment, and a resume point the decoder
can actually start from. The measured behaviour of Chromium's MediaRecorder is
that neither comes for free.

The late-join gate (``frontend/e2e/late-join-gate.spec.js``) recorded a real
34-second stream at the same 250 ms timeslice the HQ broadcaster uses and found
that **0 of 113** non-initial chunks began with a Cluster identifier. A
``dataavailable`` boundary is not a container boundary. Forwarding whole
timeslice chunks from an arbitrary point therefore hands a SourceBuffer a
partial cluster, and Chromium tolerates that only INTERMITTENTLY - across
repeated runs over identical bytes the 30-second join decoded on some runs and
failed with an append error on others. Intermittent is not support.

Resuming from a genuine Cluster boundary, with the initialization segment sent
first, decoded and advanced at every offset and on every repeat. That is the
architecture this module implements.

WHY A CHILD-WALK RATHER THAN A BYTE SCAN

Scanning the stream for the four-byte Cluster identifier is the obvious
implementation and it is wrong: those four bytes can occur inside Opus payload,
so a scan can split a cluster in the middle of a block and produce exactly the
partial-cluster corruption this module exists to prevent.

Measured against real MediaRecorder output, Clusters are written with UNKNOWN
size (an all-ones 8-byte length) while every child - ``Timecode`` and
``SimpleBlock`` - carries a known size. So a cluster's end can be found
deterministically by walking its children until an identifier appears that is
only legal at the top level. No guessing, and no false positive from payload
bytes that happen to look like an identifier.

WHAT THIS MODULE DELIBERATELY IS NOT

It is not a Matroska parser. It reads element identifiers and lengths, and it
recognises which identifiers are top-level and which are legal inside a Cluster.
It never interprets a block's contents, never decodes Opus and never rewrites
timestamps - the listener's ``SourceBuffer`` in ``sequence`` mode does that.

It imports no FastAPI, no WebSocket and no SQLAlchemy: framing is separable from
delivery, and this half is testable without a server.

NOTHING HERE LOGS AUDIO. Sizes and counts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

__all__ = [
    "WebmFramingError",
    "InitSegmentTooLargeError",
    "ClusterTooLargeError",
    "MalformedWebmStreamError",
    "WebmStreamFramer",
    "DEFAULT_MAX_INIT_BYTES",
    "DEFAULT_MAX_CLUSTER_BYTES",
]


# ---------------------------------------------------------------------------
# Element identifiers, as written by Chromium's MediaRecorder.
# ---------------------------------------------------------------------------

ID_EBML = 0x1A45DFA3
ID_SEGMENT = 0x18538067
ID_CLUSTER = 0x1F43B675

#: Identifiers that may appear directly inside a Segment. Meeting any of these
#: while walking a Cluster's children means the Cluster has ended - which is the
#: only way to find the end of an unknown-size Cluster without guessing.
TOP_LEVEL_IDS = frozenset({
    ID_EBML,
    ID_SEGMENT,
    ID_CLUSTER,
    0x114D9B74,   # SeekHead
    0x1549A966,   # Info
    0x1654AE6B,   # Tracks
    0x1C53BB6B,   # Cues
    0x1254C367,   # Tags
    0x1043A770,   # Chapters
    0x1941A469,   # Attachments
})

#: Identifiers legal INSIDE a Cluster. An allowlist rather than "anything that
#: is not top level": an identifier this module does not recognise means the
#: stream is not what was measured, and guessing at that point is how a framer
#: silently emits corrupt clusters. Fail closed instead.
CLUSTER_CHILD_IDS = frozenset({
    0xE7,         # Timecode
    0xA3,         # SimpleBlock
    0xA0,         # BlockGroup
    0xA7,         # Position
    0xAB,         # PrevSize
    0xAF,         # EncryptedBlock
    0xEC,         # Void
    0xBF,         # CRC-32
})

# A 250 ms mono Opus cluster measured at roughly 1.2 KB. These ceilings sit far
# above anything legitimate while still refusing to buffer without limit: a
# stream that never produces a Cluster boundary must fail, not consume memory.
DEFAULT_MAX_INIT_BYTES = 262_144
DEFAULT_MAX_CLUSTER_BYTES = 1_048_576


class WebmFramingError(ValueError):
    """Base class for controlled, payload-free framing failures."""


class InitSegmentTooLargeError(WebmFramingError):
    """The bytes before the first Cluster exceeded the permitted size."""


class ClusterTooLargeError(WebmFramingError):
    """One Cluster exceeded the permitted size before it was completed."""


class MalformedWebmStreamError(WebmFramingError):
    """The stream is not the WebM structure this framer was measured against."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One distributable unit of the stream.

    ``kind`` is ``"init"`` for the initialization segment - emitted exactly once
    per stream - and ``"cluster"`` for every complete Cluster after it.
    """

    kind: str
    data: bytes
    #: 0 for the init segment, then 1, 2, 3 ... for clusters. A listener's
    #: bootstrap records which cluster it started from, so a reconnect is
    #: distinguishable from a first join without consulting a clock.
    index: int

    @property
    def is_init(self) -> bool:
        return self.kind == "init"


def _read_element_id(buf: bytes, pos: int) -> tuple[int | None, int]:
    """Read an EBML element identifier, marker bits included.

    Returns ``(None, pos)`` when more bytes are needed - a partial identifier
    split across two socket reads is ordinary, not an error.
    """
    if pos >= len(buf):
        return None, pos
    first = buf[pos]
    if first == 0x00:
        raise MalformedWebmStreamError("element identifier has no length marker")
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 4:
            raise MalformedWebmStreamError("element identifier is longer than four bytes")
    if pos + length > len(buf):
        return None, pos
    value = 0
    for offset in range(length):
        value = (value << 8) | buf[pos + offset]
    return value, pos + length


def _read_element_size(buf: bytes, pos: int) -> tuple[int | None, int, bool]:
    """Read an EBML element length.

    Returns ``(size, next_pos, unknown)``. ``unknown`` is what a live Cluster
    uses: an all-ones length meaning "ends when the next element begins".
    """
    if pos >= len(buf):
        return None, pos, False
    first = buf[pos]
    if first == 0x00:
        raise MalformedWebmStreamError("element length has no length marker")
    length = 1
    mask = 0x80
    while not first & mask:
        mask >>= 1
        length += 1
        if length > 8:
            raise MalformedWebmStreamError("element length is longer than eight bytes")
    if pos + length > len(buf):
        return None, pos, False
    value = first & (mask - 1)
    for offset in range(1, length):
        value = (value << 8) | buf[pos + offset]
    unknown = value == (1 << (7 * length)) - 1
    return value, pos + length, unknown


class WebmStreamFramer:
    """Turns a live WebM byte stream into an init segment and whole Clusters.

    Feed it whatever arrives from the broadcaster socket, in whatever sizes.
    Identifiers and lengths split across feeds are held until complete, so the
    caller never has to align its reads to anything.

    A Cluster is emitted only once its end is *known* - that is, once the first
    byte of the following element has arrived. That costs one cluster of
    latency and is the entire reason a late joiner can decode at all.
    """

    __slots__ = (
        "_buf", "_pos", "_in_segment", "_init_emitted", "_cluster_start",
        "_child_pos", "_max_init", "_max_cluster", "_index",
        "_clusters_emitted", "_bytes_in", "_failed",
    )

    def __init__(self, *, max_init_bytes: int = DEFAULT_MAX_INIT_BYTES,
                 max_cluster_bytes: int = DEFAULT_MAX_CLUSTER_BYTES) -> None:
        if max_init_bytes <= 0 or max_cluster_bytes <= 0:
            raise ValueError("framer limits must be positive")
        self._buf = bytearray()
        self._pos = 0                    # parse cursor while still in the header
        self._in_segment = False
        self._init_emitted = False
        self._cluster_start: int | None = None
        self._child_pos = 0
        self._max_init = max_init_bytes
        self._max_cluster = max_cluster_bytes
        self._index = 0
        self._clusters_emitted = 0
        self._bytes_in = 0
        self._failed = False

    # -- introspection ----------------------------------------------------
    @property
    def init_emitted(self) -> bool:
        return self._init_emitted

    @property
    def clusters_emitted(self) -> int:
        return self._clusters_emitted

    @property
    def buffered_bytes(self) -> int:
        """Bytes held pending a Cluster boundary. Must stay small and bounded."""
        return len(self._buf)

    def metrics(self) -> dict[str, int | bool]:
        """Non-secret counters. Never any audio payload."""
        return {
            "bytes_in": self._bytes_in,
            "buffered_bytes": len(self._buf),
            "clusters_emitted": self._clusters_emitted,
            "init_emitted": self._init_emitted,
            "failed": self._failed,
        }

    # -- framing ----------------------------------------------------------
    def feed(self, data: bytes) -> list[Frame]:
        """Offer bytes from the broadcaster. Returns whatever became complete.

        Raises on a malformed or unbounded stream. A caller on the audio path
        must treat that as "this stream cannot serve web listeners" and keep
        Stores and recording running regardless.
        """
        if self._failed:
            raise MalformedWebmStreamError("this framer already failed and cannot continue")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise MalformedWebmStreamError("audio input must be binary data")
        if not data:
            return []

        self._bytes_in += len(data)
        self._buf.extend(bytes(data))
        try:
            return list(self._drain())
        except WebmFramingError:
            self._failed = True
            raise

    def _drain(self) -> Iterator[Frame]:
        if not self._init_emitted:
            frame = self._try_complete_init()
            if frame is None:
                # Still in the header. Refuse to accumulate for ever.
                if len(self._buf) > self._max_init:
                    raise InitSegmentTooLargeError(
                        f"no Cluster within {self._max_init} bytes of stream start")
                return
            yield frame

        yield from self._drain_clusters()

    def _try_complete_init(self) -> Frame | None:
        """Consume header elements until the first Cluster identifier appears."""
        buf = bytes(self._buf)
        pos = self._pos

        while True:
            element_id, after_id = _read_element_id(buf, pos)
            if element_id is None:
                self._pos = pos
                return None

            if element_id == ID_CLUSTER:
                # Everything before this identifier is the bootstrap a late
                # joiner needs, and it is now complete.
                init = bytes(buf[:pos])
                if not init:
                    raise MalformedWebmStreamError("stream begins with a Cluster and no header")
                if len(init) > self._max_init:
                    raise InitSegmentTooLargeError("initialization segment is too large")
                self._init_emitted = True
                self._cluster_start = pos
                self._child_pos = pos
                self._index = 1
                return Frame(kind="init", data=init, index=0)

            size, after_size, unknown = _read_element_size(buf, after_id)
            if size is None:
                self._pos = pos
                return None

            if element_id == ID_SEGMENT:
                # A live Segment has unknown size and is never "consumed" - its
                # children are the stream. Descend rather than skip.
                self._in_segment = True
                pos = after_size
                continue

            if unknown:
                raise MalformedWebmStreamError(
                    "unexpected unknown-size element before the first Cluster")
            if after_size + size > len(buf):
                self._pos = pos
                return None
            pos = after_size + size

    def _drain_clusters(self) -> Iterator[Frame]:
        """Walk Cluster children, emitting each Cluster once its end is known."""
        while True:
            buf = bytes(self._buf)
            start = self._cluster_start
            assert start is not None
            pos = self._child_pos

            # A Cluster's own identifier and unknown length sit at its start.
            if pos == start:
                element_id, after_id = _read_element_id(buf, pos)
                if element_id is None:
                    return
                if element_id != ID_CLUSTER:
                    raise MalformedWebmStreamError("expected a Cluster identifier")
                size, after_size, unknown = _read_element_size(buf, after_id)
                if size is None:
                    return
                if not unknown:
                    # A known-size Cluster is legal WebM and simply easier: its
                    # end is arithmetic rather than a search.
                    end = after_size + size
                    if end > len(buf):
                        return
                    yield self._emit_cluster(end)
                    continue
                pos = after_size

            completed_at = None
            while True:
                element_id, after_id = _read_element_id(buf, pos)
                if element_id is None:
                    break
                if element_id in TOP_LEVEL_IDS:
                    # The previous Cluster ends exactly where this begins.
                    completed_at = pos
                    break
                if element_id not in CLUSTER_CHILD_IDS:
                    raise MalformedWebmStreamError(
                        f"identifier {element_id:#x} is not legal inside a Cluster")
                size, after_size, unknown = _read_element_size(buf, after_id)
                if size is None:
                    break
                if unknown:
                    raise MalformedWebmStreamError(
                        "unknown-size element inside a Cluster")
                if after_size + size > len(buf):
                    break
                pos = after_size + size

            self._child_pos = pos
            if completed_at is None:
                if len(buf) - start > self._max_cluster:
                    raise ClusterTooLargeError(
                        f"a Cluster exceeded {self._max_cluster} bytes without ending")
                return
            yield self._emit_cluster(completed_at)

    def _emit_cluster(self, end: int) -> Frame:
        start = self._cluster_start
        assert start is not None
        payload = bytes(self._buf[start:end])
        if len(payload) > self._max_cluster:
            raise ClusterTooLargeError("a Cluster exceeded the permitted size")
        # Drop everything already emitted. The buffer never holds more than the
        # Cluster currently being assembled.
        del self._buf[:end]
        self._cluster_start = 0
        self._child_pos = 0
        frame = Frame(kind="cluster", data=payload, index=self._index)
        self._index += 1
        self._clusters_emitted += 1
        return frame
