import ctypes

from koyu_runtime.ipc import blackboard, events
from koyu_runtime.ipc.service import Service


class S(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("v", ctypes.c_uint64),
    ]


def test_service_ticks_writes_and_stops():
    class Counter(Service):
        def setup(self):
            self.w = self.writer("test/svc_state", S)
            self.tick(500)
            self.n = 0

        def on_tick(self):
            self.n += 1
            self.w.write(S(v=self.n))
            if self.n >= 5:
                self.stop()

    svc = Counter("counter")
    svc.run()
    assert svc.n == 5
    assert blackboard.Reader("test/svc_state", S).read().v == 5


def test_service_dispatches_events():
    class Echo(Service):
        def setup(self):
            self.on("test/svc_ctl")
            self.tick(500)
            self.seen = []
            self.n = 0

        def on_event(self, channel, event_id):
            self.seen.append((channel, event_id))

        def on_tick(self):
            self.n += 1
            if self.n >= 10:
                self.stop()

    svc = Echo("echo")
    events.Notifier("test/svc_ctl").ring(7)     # pending before the loop starts
    svc.run()
    assert ("test/svc_ctl", 7) in svc.seen


def test_lazy_subscriber_inert_until_publisher_exists():
    # a service whose subscriber's publisher never comes up must not crash
    class Consumer(Service):
        def setup(self):
            self.sub = self.subscriber("test/svc_never", S, buffer=1)
            self.tick(500)
            self.n = 0
            self.last = "unset"

        def on_tick(self):
            self.n += 1
            self.last = self.sub.latest()       # None while disconnected, no raise
            if self.n >= 3:
                self.stop()

    svc = Consumer("consumer")
    svc.run()
    assert svc.last is None
