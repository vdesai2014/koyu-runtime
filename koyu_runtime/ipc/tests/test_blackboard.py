import ctypes

import pytest

from koyu_runtime.ipc import blackboard as bb


class S(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("v", ctypes.c_uint64),
    ]


def test_write_then_read():
    w = bb.Writer("test/bb_rw", S)
    assert w.write(S(v=42)) is True
    assert bb.Reader("test/bb_rw", S).read().v == 42


def test_latest_value_wins():
    w = bb.Writer("test/bb_latest", S)
    w.write(S(v=1))
    w.write(S(v=2))
    assert bb.Reader("test/bb_latest", S).read().v == 2


def test_reader_before_writer_raises():
    with pytest.raises(Exception):
        bb.Reader("test/bb_missing_xyz", S)


def test_second_writer_conflicts():
    w1 = bb.Writer("test/bb_single", S)       # keep the ref so the slot stays held
    with pytest.raises(Exception):
        bb.Writer("test/bb_single", S)        # single-writer invariant -> raises
    assert w1 is not None                     # (keeps w1 alive past the with-block)


def test_writer_resets_initial_on_reopen():
    import gc
    w1 = bb.Writer("test/bb_reinit", S, initial=S(v=999))   # "dangerous" stale value
    r = bb.Reader("test/bb_reinit", S)        # holds the segment alive across the crash
    del w1
    gc.collect()
    bb.Writer("test/bb_reinit", S, initial=S(v=0))          # reopens the stale segment
    assert r.read().v == 0                    # reset to the safe initial, not stale 999
