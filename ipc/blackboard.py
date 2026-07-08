"""Blackboard IPC — latest-value, single-writer, lock-free read.

A blackboard is one shared cell per topic: the writer overwrites it, readers copy
out the current value (no queue, no history). Ported from the old core/shm.py,
keeping its two hard-won behaviors:

  * stale-segment recovery — a crashed writer can leave a segment that readers
    still hold alive; ``_create_or_reopen`` sweeps dead nodes and, failing that,
    reopens the segment to take the writer slot.
  * fail-fast writes — after WRITE_FAIL_LIMIT consecutive failures the writer
    raises, so the supervisor restarts it rather than letting it zombie-publish.
"""

from __future__ import annotations

import ctypes
from typing import Optional, Type

import iceoryx2 as iox2

from .node import name, node, sweep_dead

# Single entry per blackboard, keyed by u64 — must match the Rust side's key type.
KEY_TYPE = ctypes.c_uint64
KEY = KEY_TYPE(0)
WRITE_FAIL_LIMIT = 3


def _already_exists(exc: Exception) -> bool:
    msg = str(exc)
    return "AlreadyExists" in msg or "already exists" in msg.lower()


def _create_or_reopen(topic: str, initial):
    """Create the blackboard, recovering from a stale segment left by a crash."""
    def build():
        return (
            node().service_builder(name(topic))
            .blackboard_creator(KEY_TYPE)
            .add(KEY, initial)
        )

    try:
        return build().create()
    except iox2.BlackboardCreateError as exc:
        if not _already_exists(exc):
            raise

    sweep_dead()                                    # crashed node? reap and retry
    try:
        return build().create()
    except iox2.BlackboardCreateError as exc:
        if not _already_exists(exc):
            raise

    # Readers hold the stale segment alive — open it and take the writer slot.
    return node().service_builder(name(topic)).blackboard_opener(KEY_TYPE).open()


class Writer:
    """Owns a blackboard topic; overwrites its single current value."""

    def __init__(self, topic: str, struct_type: Type, initial=None):
        self._topic = topic
        initial_value = initial if initial is not None else struct_type()
        service = _create_or_reopen(topic, initial_value)
        self._entry = service.writer_builder().create().entry(KEY, struct_type)
        # Reopening a stale segment keeps its pre-crash value; write the initial
        # explicitly so readers never see stale state (e.g. a live motor command)
        # between recovery and this writer's first control-loop write().
        self._entry.update_with_copy(initial_value)
        self._fails = 0

    def write(self, struct) -> bool:
        """Publish the latest value. Returns False on a transient failure; raises
        after WRITE_FAIL_LIMIT consecutive failures so the supervisor restarts us."""
        try:
            self._entry.update_with_copy(struct)
            self._fails = 0
            return True
        except Exception as exc:
            self._fails += 1
            if self._fails >= WRITE_FAIL_LIMIT:
                raise RuntimeError(
                    f"blackboard {self._topic!r}: {self._fails} consecutive write "
                    f"failures — segment is dead, crashing for supervisor restart"
                ) from exc
            return False


class Reader:
    """Reads the current value of a blackboard topic; None until the writer is up.

    ``__init__`` raises if no writer has created the topic yet — open-with-retry
    is the caller's (the Service base's) concern, not this wrapper's.
    """

    def __init__(self, topic: str, struct_type: Type):
        self._struct_type = struct_type
        service = node().service_builder(name(topic)).blackboard_opener(KEY_TYPE).open()
        self._entry = service.reader_builder().create().entry(KEY, struct_type)

    def read(self) -> Optional[object]:
        # get() returns the current BlackboardValue (None only if absent); a
        # decode failure is a real schema bug and is allowed to bubble rather
        # than being silently swallowed into None.
        value = self._entry.get()
        return value.decode_as(self._struct_type) if value is not None else None
