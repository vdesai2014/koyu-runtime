"""Event IPC — payload-less doorbells, many-to-many.

A notifier rings an event-id on a channel; every listener connected to that
channel wakes. No payload — the data lives on a plane (blackboard/file), the
event just says "go look". Many notifiers and many listeners coexist
(open_or_create, no single owner), and a rung event survives the notifier's
death as long as a listener was already connected.
"""

from __future__ import annotations

import iceoryx2 as iox2

from .node import name, node


class Notifier:
    """Rings event-ids on a channel."""

    def __init__(self, channel: str):
        service = node().service_builder(name(channel)).event().open_or_create()
        self._notifier = service.notifier_builder().create()

    def ring(self, event_id: int) -> None:
        self._notifier.notify_with_custom_event_id(iox2.EventId.new(event_id))


class Listener:
    """Wakes on a channel; drains the event-ids that rang since last look."""

    def __init__(self, channel: str):
        service = node().service_builder(name(channel)).event().open_or_create()
        self._listener = service.listener_builder().create()

    def drain(self) -> list[int]:
        """Non-blocking: every id that rang since the last drain, in order."""
        return [event.as_value for event in self._listener.try_wait_all()]

    @property
    def raw(self):
        """The iox2 Listener, to attach to a WaitSet (the Service base, chonker 2)."""
        return self._listener
