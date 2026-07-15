"""Paired-mode pairing: exact rows or a loud abort — never silent misalignment.

Constructed-state tests through the spoofed-seams pattern: a recorder built
with object.__new__, fake subscriptions, injected timestamps. No IPC, no sleeps.
"""

from types import SimpleNamespace

from koyu_runtime.services.data_recorder import CLOCK_BUFFER, DataRecorder, Source


class FakeSub:
    def __init__(self):
        self.queue = []
        self.value = None

    def drain(self):
        out, self.queue = self.queue, []
        return out

    def latest(self):
        v, self.value = self.value, None
        return v


class FakeNotifier:
    def __init__(self):
        self.rung = []

    def ring(self, eid):
        self.rung.append(eid)


def sample(ts_s, fid, v=0.0):
    return SimpleNamespace(timestamp=ts_s, frame_id=fid, value=v)


def make_recorder(paired_action=True):
    rec = object.__new__(DataRecorder)
    clock = Source("cam", "obs.img", lambda x: x.value, {}, "video", 20.0)
    action = Source("act", "action", lambda x: x.value, {"dtype": "float32"},
                    "column", 20.0, paired=paired_action)
    rec.clock, rec.others, rec.sources = clock, [action], [clock, action]
    rec.subs = {"cam": FakeSub(), "act": FakeSub()}
    rec.episode = FakeNotifier()
    rec.state = "recording"
    rec.capture_id = "f" * 32
    rec.deferred, rec.rows, rec.cache = [], [], {}
    rec.pair_cache = {}
    rec.rec_hz, rec.period_ns, rec.next_due = 0.0, 0, 0
    rec.last_ts, rec.t0_ns, rec.clock_seen = 0, 0, 0.0
    rec.pending = []
    rec._telemetry = lambda: None                 # no IPC in tests
    rec.verdicts = SimpleNamespace(drain=lambda: [], quarantine=lambda *a: None)
    return rec


def step(rec):
    """One real _capture pass; paired answers arrive through the sub queue."""
    rec._capture()


def test_paired_waits_then_pairs_exactly():
    rec = make_recorder()
    rec.subs["cam"].queue = [sample(1.00, fid=5, v=50)]
    rec.subs["act"].queue = [sample(0.95, fid=4, v=44)]   # previous answer, in-window!
    step(rec)
    assert rec.rows == [] and len(rec.deferred) == 1  # waited — no off-by-one row

    rec.subs["act"].queue = [sample(1.08, fid=5, v=55)]   # the answer for frame 5 lands
    step(rec)
    assert len(rec.rows) == 1 and rec.deferred == []
    assert rec.rows[0][1] == {"obs.img": 50, "action": 55}   # exact pairing


def test_window_mode_records_the_stale_pair():
    # the counterfactual: without paired, the in-window previous action is taken
    rec = make_recorder(paired_action=False)
    rec.subs["cam"].queue = [sample(1.00, fid=5, v=50)]
    rec.cache["act"] = sample(0.95, fid=4, v=44)
    step(rec)
    assert rec.rows[0][1] == {"obs.img": 50, "action": 44}   # the off-by-one


def test_deferred_preserves_order_across_frames():
    rec = make_recorder()
    rec.subs["cam"].queue = [sample(1.00, fid=5, v=50)]
    step(rec)                                          # frame 5 deferred
    rec.subs["cam"].queue = [sample(1.05, fid=6, v=60)]
    rec.subs["act"].queue = [sample(1.02, fid=5, v=55)]
    step(rec)                                          # 5 lands; 6 now waits
    assert [r[1]["obs.img"] for r in rec.rows] == [50]
    assert [f.frame_id for f in rec.deferred] == [6]
    rec.subs["act"].queue = [sample(1.07, fid=6, v=66)]
    step(rec)
    assert [r[1]["action"] for r in rec.rows] == [55, 66]


def test_fast_lockstep_answers_between_ticks_all_land():
    # regression: a lockstep faster than the tick delivers two answers inside
    # one capture window. Depth-1 latest-value reads used to overwrite the
    # first answer, leaving its clock frame unmatchable forever — one hole
    # aborted the whole episode. Held-by-frame_id pairing lands both rows.
    rec = make_recorder()
    rec.subs["cam"].queue = [sample(1.00, fid=5, v=50), sample(1.05, fid=6, v=60)]
    rec.subs["act"].queue = [sample(1.02, fid=5, v=55), sample(1.07, fid=6, v=66)]
    step(rec)
    assert [r[1] for r in rec.rows] == [
        {"obs.img": 50, "action": 55}, {"obs.img": 60, "action": 66}]
    assert rec.deferred == [] and rec.state == "recording"


def test_unmatched_forever_aborts_loudly():
    rec = make_recorder()
    for i in range(CLOCK_BUFFER + 1):
        rec.subs["cam"].queue = [sample(1.0 + i * 0.05, fid=i, v=i)]
        step(rec)                                      # action never arrives
    assert rec.state == "idle" and rec.rows == []      # aborted, not misaligned
    assert rec.episode.rung                            # EP_FAILED rang


def test_abort_clears_the_pocket():
    rec = make_recorder()
    rec.subs["cam"].queue = [sample(1.00, fid=5, v=50)]
    step(rec)
    assert rec.deferred
    rec._abort("test")
    assert rec.deferred == [] and rec.state == "idle"
    assert rec.pair_cache == {}                        # held answers die with the episode
