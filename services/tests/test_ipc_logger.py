import json

from PIL import Image

from ipc import blackboard
from ipc.types import CameraFrame, GlobalConfig
from services.ipc_logger import FrameRing, IpcLogger, _snapshot


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


def test_frame_ring_caps_disk(tmp_path):
    ring = FrameRing(tmp_path / "frames", cap_bytes=20_000)
    img = Image.new("RGB", (32, 32))
    for _ in range(200):
        ring.save(img)
    files = list((tmp_path / "frames").glob("*.jpg"))
    assert sum(f.stat().st_size for f in files) <= 20_000      # disk bounded
    assert 0 < len(files) < 200                                 # oldest evicted, newest kept


def test_snapshot_skips_arrays():
    snap = _snapshot(CameraFrame(frame_id=7, width=4, height=4))
    assert snap["frame_id"] == 7
    assert snap["width"] == 4
    assert "data" not in snap                                   # the big array is skipped


def test_logs_blackboard_state_per_topic(tmp_path):
    w = blackboard.Writer("log/state", GlobalConfig)
    w.write(GlobalConfig(frame_id=1, speed=2.0))
    lg = IpcLogger(tmp_path, {"blackboard": {"reads": {"log/state": "GlobalConfig"}}})
    lg.on_tick()
    recs = _read(tmp_path / "services" / "ipc_logger" / "log~state" / "state.jsonl")
    assert any(r["kind"] == "state" and r["speed"] == 2.0 for r in recs)
    assert w is not None


def test_logs_events_per_channel(tmp_path):
    lg = IpcLogger(tmp_path, {"events": {"listens": ["log/ctl"]}})
    lg.on_event("log/ctl", 9)
    recs = _read(tmp_path / "services" / "ipc_logger" / "log~ctl" / "events.jsonl")
    assert any(r["kind"] == "event" and r["id"] == 9 for r in recs)


def test_logs_image_to_frames_and_state(tmp_path):
    w = blackboard.Writer("log/cam", CameraFrame)
    fr = CameraFrame(frame_id=1, width=8, height=8)
    for i in range(8 * 8 * 3):
        fr.data[i] = (i * 5) % 256
    w.write(fr)
    lg = IpcLogger(tmp_path, {"blackboard": {"reads": {"log/cam": "CameraFrame"}}})
    lg.on_tick()
    cam = tmp_path / "services" / "ipc_logger" / "log~cam"
    assert len(list((cam / "frames").glob("*.jpg"))) == 1       # jpg saved
    assert any(r["kind"] == "frame" for r in _read(cam / "state.jsonl"))   # + frame record
    assert w is not None
