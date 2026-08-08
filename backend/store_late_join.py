"""Enough of the outgoing stream for a Store to START mid-Broadcast.

WHY THIS EXISTS

Stores that are targeted when a Broadcast starts receive the broadcaster's
chunks untouched, from byte zero. That works only because they were there for
the header: a WebM stream is an EBML header and Segment metadata followed by
Clusters, and a decoder handed the middle of one has nothing to open.

A Store added while a Broadcast is already running is exactly that case. It
needs the initialization segment first, and then to begin on a real Cluster
boundary - never at an arbitrary MediaRecorder timeslice.

WHAT THIS IS NOT

It is not a history buffer. The Store joins the live edge, not the beginning:
nobody wants four seconds of an announcement that has already been made, and a
Store playing yesterday's audio behind today's is worse than a Store that
joined a moment late.

It is also deliberately NOT the web audience relay. The parsing implementation
is shared - one tested WebM framer, not two - but the state is separate, so a
Store cannot fail to join because the web audience is disabled, degraded, or
has no listeners. Those are unrelated facts about unrelated audiences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from webm_stream import Frame, WebmFramingError, WebmStreamFramer

logger = logging.getLogger("echocast.audio")

__all__ = [
    "DELIVERY_INITIAL_RAW",
    "DELIVERY_LATE_JOIN_FRAMED",
    "StoreBootstrap",
    "StoreLateJoinSource",
]

#: A Store that was targeted before the first byte. It receives the
#: broadcaster's chunks exactly as it always has - this path is physically
#: accepted and is deliberately left alone.
DELIVERY_INITIAL_RAW = "INITIAL_RAW"

#: A Store that joined mid-Broadcast. It receives the initialization segment,
#: then whole Clusters, for the WHOLE of its participation. It is never handed
#: back to raw chunks, because the point at which that happened would be the
#: middle of a Cluster.
DELIVERY_LATE_JOIN_FRAMED = "LATE_JOIN_FRAMED"

#: How many already-complete Clusters a late joiner is given before the live
#: edge.
#:
#: Zero. The initialization segment is what a decoder needs to open the stream;
#: it does not need a Cluster that has already been broadcast. The joiner waits
#: for the NEXT complete Cluster, which is at most one chunk away.
#:
#: This is a deliberate departure from the web audience relay, which keeps
#: several seconds so a browser's MediaSource has something to buffer before it
#: can start. A Store Receiver is decoding to a speaker in a shop, and a shop
#: playing four seconds of stale announcement is a worse failure than a Store
#: starting a quarter of a second later.
DEFAULT_STORE_BOOTSTRAP_CLUSTERS = 0


@dataclass(frozen=True, slots=True)
class StoreBootstrap:
    """The exact bytes a joining Store must be sent, in this order."""

    init_segment: bytes
    clusters: tuple[bytes, ...]
    #: The index of the last Cluster included. Everything after it is live.
    next_cluster_index: int

    @property
    def payloads(self) -> tuple[bytes, ...]:
        return (self.init_segment, *self.clusters)

    @property
    def total_bytes(self) -> int:
        return len(self.init_segment) + sum(len(c) for c in self.clusters)


class StoreLateJoinSource:
    """One Broadcast's framed view of its own outgoing audio.

    Fed every broadcaster chunk. Holds the initialization segment and, by
    default, nothing else - so its memory cost is one header for the life of
    the Broadcast however long it runs.
    """

    def __init__(self, *, session_id: int,
                 bootstrap_clusters: int = DEFAULT_STORE_BOOTSTRAP_CLUSTERS
                 ) -> None:
        if bootstrap_clusters < 0:
            raise ValueError("bootstrap_clusters cannot be negative")
        self.session_id = session_id
        self._framer = WebmStreamFramer()
        self._init_segment: bytes | None = None
        self._bootstrap_clusters = bootstrap_clusters
        self._recent: list[bytes] = []
        self._cluster_index = 0
        self._framing_error: str | None = None

    # ---------- ingest ----------
    def offer(self, chunk: bytes) -> list[Frame]:
        """Feed one broadcaster chunk. Returns the whole Clusters it completed.

        A stream this cannot frame costs late joiners only. The Stores that
        were present from the start are on the raw path and are unaffected, and
        so are the recording and the web audience - the same rule those two
        already follow.
        """
        if self._framing_error is not None:
            return []
        try:
            frames = self._framer.feed(chunk)
        except WebmFramingError as failure:
            self._framing_error = str(failure)
            logger.warning("store late-join framing stopped for session=%s: %s",
                           self.session_id, type(failure).__name__)
            return []

        clusters: list[Frame] = []
        for frame in frames:
            if frame.is_init:
                self._init_segment = frame.data
                continue
            self._cluster_index = frame.index
            clusters.append(frame)
            if self._bootstrap_clusters:
                self._recent.append(frame.data)
                del self._recent[:-self._bootstrap_clusters]
        return clusters

    # ---------- joining ----------
    @property
    def ready(self) -> bool:
        """Whether a Store could be started right now.

        The initialization segment is the whole requirement. Waiting for a
        Cluster as well would delay a join for no benefit: the next one is
        already on its way and will be delivered whole.
        """
        return self._init_segment is not None and self._framing_error is None

    @property
    def framing_error(self) -> str | None:
        return self._framing_error

    def bootstrap(self) -> StoreBootstrap | None:
        """What to send a Store that is joining now, or None if not ready."""
        if not self.ready:
            return None
        return StoreBootstrap(
            init_segment=self._init_segment,
            clusters=tuple(self._recent),
            next_cluster_index=self._cluster_index,
        )

    def metrics(self) -> dict:
        """Counters only. Never audio, and never anything a log should not hold."""
        return {
            "session_id": self.session_id,
            "init_ready": self._init_segment is not None,
            "init_bytes": len(self._init_segment) if self._init_segment else 0,
            "clusters_seen": self._cluster_index,
            "bootstrap_clusters": len(self._recent),
            "framing_error": self._framing_error,
        }
