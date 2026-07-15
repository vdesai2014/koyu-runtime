"""Pub/sub IPC — zero-copy streams with independent per-subscriber buffers.

The publisher owns the topic and fixes the static config every subscriber shares:
the buffer *ceiling*, history, and overflow behavior. Each subscriber then picks
its own depth under that ceiling. Proven in the spikes: connected reads are ~µs,
a slow deep reader never stalls the publisher (with overflow on), and the ceiling
is set once by the owner at create.

Overflow defaults to on — the iceoryx2 default (Block) would let a full slow
reader stall the publisher and everyone downstream of it.
"""

from __future__ import annotations

from typing import Optional, Type

from .node import name, node


class Publisher:
    """Owns a stream topic; sets the ceiling/overflow all subscribers share."""

    def __init__(
        self,
        topic: str,
        struct_type: Type,
        *,
        max_subscribers: int = 8,
        max_buffer: int = 16,
        history: int = 1,
        overflow: bool = True,
    ):
        # open_or_create, not create: if this publisher crashed, surviving
        # subscribers keep the segment alive, so a bare create() on restart would
        # AlreadyExists and crash-loop. The dead publisher's port is reaped by
        # cleanup_dead_nodes at node() creation, so we just reopen and re-claim.
        service = (
            node().service_builder(name(topic))
            .publish_subscribe(struct_type)
            .subscriber_max_buffer_size(max_buffer)
            .history_size(history)
            .enable_safe_overflow(overflow)
            .max_subscribers(max_subscribers)
            .open_or_create()
        )
        self._pub = service.publisher_builder().create()

    def send(self, struct) -> int:
        """One memmove into shm; returns the number of subscribers delivered to."""
        return self._pub.send_copy(struct)


class Subscriber:
    """Reads a stream at its own buffer depth, independent of other readers.

    ``__init__`` raises until the publisher has created the topic — open-with-retry
    is the caller's (the Service base's) concern.
    """

    def __init__(self, topic: str, struct_type: Type, *, buffer: int = 1):
        self._struct_type = struct_type
        service = node().service_builder(name(topic)).publish_subscribe(struct_type).open()
        self._sub = service.subscriber_builder().buffer_size(buffer).create()

    def latest(self) -> Optional[object]:
        """Drain to the newest sample (control-loop read); None if nothing new.

        Returns a fresh copy and holds nothing across calls. Anchoring the newest
        Sample instead would consume a borrow slot — the next drain would then
        blow the borrow limit (ReceiveError: ExceedsMaxBorrows) — and would still
        dangle if the caller cached the view. The copy is owned Python memory:
        safe to keep, and one small memcpy on the read path.
        """
        newest = None
        while (sample := self._sub.receive()) is not None:
            newest = sample
        if newest is None:
            return None
        return self._struct_type.from_buffer_copy(newest.payload().contents)

    def drain(self) -> list:
        """Every buffered sample, oldest first (deep/recorder read).

        Each is copied out of shm so it outlives the next receive().
        """
        out = []
        while (sample := self._sub.receive()) is not None:
            out.append(self._struct_type.from_buffer_copy(sample.payload().contents))
        return out
