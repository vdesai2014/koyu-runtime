"""Service base — the reactor every koyu service is built on.

A service subclasses ``Service``, declares its ports in ``setup()``, and fills in
``on_tick()`` / ``on_event()``. The base does the three things every service
otherwise repeats:

  * holds the ports — an iceoryx2 port dies with its Python object, so the base
    keeps a reference to every one for the service's lifetime;
  * opens read-side ports with retry — readers/subscribers raise until their
    writer/publisher exists, so they're wrapped to stay inert until they connect;
  * runs one WaitSet loop — the control tick plus the event listeners, dispatched
    to the two hooks.

Write-side ports (writer/publisher/notifier) open eagerly: boot priority starts
producers before consumers, so creating them in setup() is safe.
"""

from __future__ import annotations

from pathlib import Path
from typing import Type

import iceoryx2 as iox2
import yaml

from . import blackboard, events, streams


def read_service_ipc(home: str, name: str) -> dict:
    """A service's own ``ipc:`` block from services.yaml, looked up by name."""
    data = yaml.safe_load((Path(home) / "services.yaml").read_text()) or {}
    return (data.get(name) or {}).get("ipc", {})


class _LazyReader:
    """A blackboard Reader that opens on first success; reads None until then."""

    def __init__(self, topic: str, struct_type: Type):
        self._topic, self._type, self._port = topic, struct_type, None

    def read(self):
        if self._port is None:
            try:
                self._port = blackboard.Reader(self._topic, self._type)
            except Exception:
                return None
        return self._port.read()


class _LazySubscriber:
    """A pub/sub Subscriber that opens on first success; inert until then."""

    def __init__(self, topic: str, struct_type: Type, buffer: int):
        self._topic, self._type, self._buffer, self._port = topic, struct_type, buffer, None

    def _ensure(self):
        if self._port is None:
            try:
                self._port = streams.Subscriber(self._topic, self._type, buffer=self._buffer)
            except Exception:
                self._port = None
        return self._port

    def latest(self):
        port = self._ensure()
        return port.latest() if port is not None else None

    def drain(self):
        port = self._ensure()
        return port.drain() if port is not None else []


class Service:
    """Subclass, override setup()/on_tick()/on_event(), then call run()."""

    def __init__(self, name: str):
        self.name = name
        self._ports: list = []          # hold every port so iceoryx2 doesn't reap it
        self._listeners: dict = {}       # channel -> events.Listener (attached to the WaitSet)
        self._tick_period = None
        self._running = True
        self.setup()

    # -- declaration helpers (call these in setup) --------------------------

    def writer(self, topic: str, struct_type: Type) -> blackboard.Writer:
        return self._hold(blackboard.Writer(topic, struct_type))

    def reader(self, topic: str, struct_type: Type) -> _LazyReader:
        return self._hold(_LazyReader(topic, struct_type))

    def publisher(self, topic: str, struct_type: Type, **policy) -> streams.Publisher:
        return self._hold(streams.Publisher(topic, struct_type, **policy))

    def subscriber(self, topic: str, struct_type: Type, buffer: int = 1) -> _LazySubscriber:
        return self._hold(_LazySubscriber(topic, struct_type, buffer))

    def notifier(self, channel: str) -> events.Notifier:
        return self._hold(events.Notifier(channel))

    def on(self, channel: str) -> None:
        """Wake on this event channel; ids dispatch to on_event(channel, id)."""
        self._listeners[channel] = self._hold(events.Listener(channel))

    def tick(self, hz: float) -> None:
        """Run on_tick() at this rate."""
        self._tick_period = iox2.Duration.from_nanos(int(1e9 / hz))

    # -- hooks to override --------------------------------------------------

    def setup(self) -> None: ...
    def on_tick(self) -> None: ...
    def on_event(self, channel: str, event_id: int) -> None: ...

    # -- the reactor --------------------------------------------------------

    def run(self) -> None:
        ws = iox2.WaitSetBuilder.new().create(iox2.ServiceType.Ipc)
        tick = ws.attach_interval(self._tick_period) if self._tick_period else None
        guards = {
            ws.attach_notification(listener.raw): (channel, listener)
            for channel, listener in self._listeners.items()
        }
        if tick is None and not guards:
            raise RuntimeError(f"{self.name}: a service needs a tick or a listener to run")

        while self._running:
            fired, result = ws.wait_and_process_with_timeout(iox2.Duration.from_millis(500))
            if result in (iox2.WaitSetRunResult.TerminationRequest, iox2.WaitSetRunResult.Interrupt):
                break                                  # SIGTERM/SIGINT -> clean exit (supervisord stop)
            for aid in fired:
                if tick is not None and aid.has_event_from(tick):
                    self.on_tick()
                for guard, (channel, listener) in guards.items():
                    if aid.has_event_from(guard):
                        for event_id in listener.drain():
                            self.on_event(channel, event_id)

    def stop(self) -> None:
        self._running = False

    def _hold(self, port):
        self._ports.append(port)
        return port
