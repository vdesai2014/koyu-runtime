"""End-to-end: a real recorder over iceoryx2, fed fake camera + robot publishers.

Boots DataRecorder on its WaitSet thread and drives it over the real planes:
ring start -> publish frames + states -> ring stop, asserting a bundle lands
(row<->frame 1:1) and the right recorder/episode id rings. Covers native-rate
capture, the mid-episode stale-source abort, and record_hz decimation via the
recorder/config blackboard.

Each test uses its own topic prefix; the decimation test runs last because the
recorder/config blackboard it creates outlives it (ports linger until GC).
"""

import ctypes
import threading
import time

import av
import numpy as np
import pyarrow.parquet as pq
import pytest

from ipc import blackboard, events, streams, types
from services import data_recorder as dr
from services.data_recorder import DataRecorder, Source
from services.episode_schema import EpisodeSidecar, RecordingContext


def _rgb(frame):
    h, w, c = int(frame.height), int(frame.width), int(frame.channels)
    return np.frombuffer(bytes(frame.data[: h * w * c]), np.uint8).reshape(h, w, c).copy()


def _positions(state):
    return [float(state.position[i]) for i in range(7)]


def _sources(prefix):
    # the state's declared rate matches the test's ~30Hz publish cadence, so a
    # co-published sample is always within tolerance of its clock frame
    return [
        Source(f"{prefix}/cam", "observation.images.top", _rgb,
               {"dtype": "video", "shape": [32, 32, 3]}, "video", 30.0, type_name="CameraFrame"),
        Source(f"{prefix}/state", "observation.state", _positions,
               {"dtype": "float32", "shape": [7]}, "column", 30.0, type_name="RobStrideState"),
    ]


def _mk_frame(i, ts):
    f = types.CameraFrame(timestamp=ts, frame_id=i + 1, width=32, height=32, channels=3)
    ctypes.memmove(f.data, np.full((32, 32, 3), (i * 40) % 256, np.uint8).tobytes(), 32 * 32 * 3)
    return f


def _mk_state(i, ts):
    s = types.RobStrideState(timestamp=ts, frame_id=i + 1)
    for j in range(7):
        s.position[j] = float(i)
    return s


def _wait(pred, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _bundles(home):
    recordings = home / "data-recordings"
    return [p for p in recordings.glob("*") if p.is_dir() and "__" in p.name]


def _boot(home, prefix):
    rec = DataRecorder(str(home), _sources(prefix))
    runner = threading.Thread(target=rec.run, daemon=True)
    runner.start()
    time.sleep(0.3)                              # recorder up: setup() ran, WaitSet looping
    return rec, runner


def _shutdown(rec, runner):
    rec.stop()
    runner.join(timeout=3)
    rec.pool.shutdown(wait=False)


def test_e2e_records_one_episode(tmp_path):
    home = tmp_path
    (home / "recording-context.json").write_text(
        RecordingContext(requested_manifest="e2e-test", collection_mode="teleop", task="t").model_dump_json()
    )
    # publishers + control/episode channels exist before the recorder rings/reads them
    state_pub = streams.Publisher("e2e1/state", types.RobStrideState)
    cam_pub = streams.Publisher("e2e1/cam", types.CameraFrame)
    control = events.Notifier("recorder/control")
    episode = events.Listener("recorder/episode")

    rec, runner = _boot(home, "e2e1")
    try:
        control.ring(dr.CTL_START)
        time.sleep(0.15)                         # subscribers connect on the first recording tick

        for i in range(6):
            ts = time.time()
            state_pub.send(_mk_state(i, ts))     # state first: in cache before its clock frame
            cam_pub.send(_mk_frame(i, ts))
            time.sleep(0.03)                     # ~30 fps

        control.ring(dr.CTL_STOP)

        assert _wait(lambda: _bundles(home)), "no bundle"
        bundle = _bundles(home)[0]

        rows = pq.read_table(bundle / "data.parquet").num_rows
        with av.open(str(bundle / "videos" / "top.mp4")) as c:
            frames = sum(1 for _ in c.decode(video=0))
        assert rows == frames                    # the 1:1 invariant, captured end to end
        assert rows >= 3

        sc = EpisodeSidecar.model_validate_json((bundle / "episode.json").read_text())
        assert sc.length == rows
        assert sc.requested_manifest == "e2e-test"
        assert sc.record_hz is None              # no param server -> native rate
        assert "e2e-test" in bundle.name

        assert _wait(lambda: dr.EP_CAPTURED in episode.drain()), "EP_CAPTURED not rung"
    finally:
        _shutdown(rec, runner)


def test_e2e_aborts_when_a_source_goes_stale(tmp_path):
    home = tmp_path
    state_pub = streams.Publisher("e2e2/state", types.RobStrideState)
    cam_pub = streams.Publisher("e2e2/cam", types.CameraFrame)
    control = events.Notifier("recorder/control")
    episode = events.Listener("recorder/episode")

    rec, runner = _boot(home, "e2e2")
    try:
        control.ring(dr.CTL_START)
        time.sleep(0.15)

        for i in range(5):                       # healthy: rows accumulate
            ts = time.time()
            state_pub.send(_mk_state(i, ts))
            cam_pub.send(_mk_frame(i, ts))
            time.sleep(0.03)
        for i in range(5, 12):                   # state dies; the clock keeps going
            cam_pub.send(_mk_frame(i, time.time()))
            time.sleep(0.03)

        got = []
        assert _wait(lambda: (got.extend(episode.drain()), dr.EP_FAILED in got)[1]), "EP_FAILED not rung"
        assert not _bundles(home)                # the episode was dropped, not written
    finally:
        _shutdown(rec, runner)


def test_e2e_decimates_to_requested_record_hz(tmp_path):
    home = tmp_path
    state_pub = streams.Publisher("e2e3/state", types.RobStrideState)
    cam_pub = streams.Publisher("e2e3/cam", types.CameraFrame)
    control = events.Notifier("recorder/control")
    episode = events.Listener("recorder/episode")

    # the param server's job, played by the test: record_hz=10 on the blackboard
    cfg = blackboard.Writer("recorder/config", types.RecorderConfig)
    cfg.write(types.RecorderConfig(timestamp=time.time(), frame_id=1, record_hz=10.0))

    rec, runner = _boot(home, "e2e3")
    try:
        control.ring(dr.CTL_START)               # snapshots record_hz at start
        time.sleep(0.15)

        for i in range(36):                      # ~1.2s+ of ~30Hz frames (send cost adds jitter)
            ts = time.time()
            state_pub.send(_mk_state(i, ts))
            cam_pub.send(_mk_frame(i, ts))
            time.sleep(0.03)

        control.ring(dr.CTL_STOP)

        assert _wait(lambda: _bundles(home)), "no bundle"
        bundle = _bundles(home)[0]

        ts_col = pq.read_table(bundle / "data.parquet")["timestamp"].to_pylist()
        assert len(ts_col) >= 8                  # decimated rows, not an empty episode
        gaps = sorted(b - a for a, b in zip(ts_col, ts_col[1:]))
        # the anchored grid holds: rows land ~100ms apart, not at the 30Hz native ~33ms
        assert gaps[len(gaps) // 2] == pytest.approx(100_000_000, rel=0.15)

        sc = EpisodeSidecar.model_validate_json((bundle / "episode.json").read_text())
        assert sc.record_hz == 10.0              # nominal (the requested param)
        assert sc.fps == pytest.approx(10.0, rel=0.25)   # achieved (measured)

        assert _wait(lambda: dr.EP_CAPTURED in episode.drain()), "EP_CAPTURED not rung"
    finally:
        _shutdown(rec, runner)
