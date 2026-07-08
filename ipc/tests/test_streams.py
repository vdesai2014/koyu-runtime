import ctypes

import pytest

from ipc import streams


class F(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("v", ctypes.c_uint64),
    ]


def test_latest_returns_newest():
    pub = streams.Publisher("test/ps_latest", F)
    sub = streams.Subscriber("test/ps_latest", F, buffer=4)
    assert pub.send(F(v=1)) == 1            # one subscriber connected
    pub.send(F(v=2))
    assert sub.latest().v == 2


def test_latest_none_when_empty():
    pub = streams.Publisher("test/ps_empty", F)   # keep ref: the topic lives with it
    sub = streams.Subscriber("test/ps_empty", F)
    assert sub.latest() is None
    assert pub is not None


def test_drain_returns_all_in_order():
    pub = streams.Publisher("test/ps_drain", F, max_buffer=8)
    sub = streams.Subscriber("test/ps_drain", F, buffer=8)
    for i in range(1, 4):
        pub.send(F(v=i))
    assert [s.v for s in sub.drain()] == [1, 2, 3]


def test_deep_reader_overflows_oldest():
    pub = streams.Publisher("test/ps_overflow", F, max_buffer=4)
    sub = streams.Subscriber("test/ps_overflow", F, buffer=4)
    for i in range(1, 11):                  # 10 sends into a depth-4 buffer, never drained
        pub.send(F(v=i))
    assert [s.v for s in sub.drain()] == [7, 8, 9, 10]   # newest 4 kept, oldest dropped


def test_subscriber_before_publisher_raises():
    with pytest.raises(Exception):
        streams.Subscriber("test/ps_missing_xyz", F)


def test_latest_returns_a_stable_copy():
    # latest() returns owned memory — unaffected by later sends, safe to keep
    pub = streams.Publisher("test/ps_copy", F, max_buffer=4)
    sub = streams.Subscriber("test/ps_copy", F, buffer=4)
    pub.send(F(v=111))
    held = sub.latest()
    pub.send(F(v=222))
    pub.send(F(v=333))
    assert held.v == 111            # an independent copy, not a recycled view


def test_latest_newest_no_borrow_overflow():
    # with several samples pending, latest() returns the newest and never blows
    # the borrow limit (the bug when the Sample was anchored)
    pub = streams.Publisher("test/ps_noborrow", F, max_buffer=4)
    sub = streams.Subscriber("test/ps_noborrow", F, buffer=4)
    pub.send(F(v=1))
    assert sub.latest().v == 1
    for i in (2, 3, 4, 5):
        pub.send(F(v=i))
    assert sub.latest().v == 5      # newest, no ExceedsMaxBorrows


def test_publisher_recovers_when_subscriber_holds_segment():
    import gc
    pub = streams.Publisher("test/ps_recover", F)
    sub = streams.Subscriber("test/ps_recover", F)   # keeps the segment alive
    del pub
    gc.collect()
    streams.Publisher("test/ps_recover", F)          # restart must not AlreadyExists
    assert sub is not None
