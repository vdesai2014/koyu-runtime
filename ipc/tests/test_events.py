from ipc import events


def test_ring_then_drain():
    listener = events.Listener("test/ev_basic")
    notifier = events.Notifier("test/ev_basic")
    notifier.ring(5)
    assert listener.drain() == [5]


def test_fan_out_to_many_listeners():
    a = events.Listener("test/ev_fanout")
    b = events.Listener("test/ev_fanout")
    notifier = events.Notifier("test/ev_fanout")
    notifier.ring(7)                          # one ring...
    assert a.drain() == [7]                   # ...both wake
    assert b.drain() == [7]


def test_order_and_count_preserved():
    listener = events.Listener("test/ev_order")
    notifier = events.Notifier("test/ev_order")
    for eid in (1, 2, 1):
        notifier.ring(eid)
    assert listener.drain() == [1, 2, 1]      # ordered FIFO, not coalesced


def test_drain_empty_is_empty():
    listener = events.Listener("test/ev_quiet")
    assert listener.drain() == []


def test_raw_exposes_the_listener():
    listener = events.Listener("test/ev_raw")
    assert listener.raw is not None
