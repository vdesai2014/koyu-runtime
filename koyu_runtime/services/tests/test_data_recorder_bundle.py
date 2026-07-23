import av
import numpy as np
import pyarrow.parquet as pq
import pytest
from datetime import datetime, timezone

from koyu_runtime.services.data_recorder import Source, finalize
from koyu_runtime.services.episode_schema import EpisodeSidecar, RecordingContext


def _frame(v):
    return np.full((32, 32, 3), v % 256, dtype=np.uint8)


def _sources():
    return [
        Source("cam", "observation.images.top", lambda x: x,
               {"dtype": "video", "shape": [32, 32, 3]}, "video", 30.0),
        Source("state", "observation.state", lambda x: x,
               {"dtype": "float32", "shape": [2]}, "column", 100.0),
    ]


def _rows(n, t0=10_000_000, dt=10_000_000):
    return [(t0 + i * dt, {"observation.images.top": _frame(i),
                           "observation.state": [float(i), float(i)]})
            for i in range(n)]


def _decode_count(path):
    with av.open(str(path)) as c:
        return sum(1 for _ in c.decode(video=0))


def test_finalize_writes_bundle(tmp_path):
    rec = tmp_path / "data-recordings"
    rec.mkdir()
    ctx = RecordingContext(requested_manifest="teleop", collection_mode="teleop", task="pick")
    out = finalize(rec, ctx, _rows(3), _sources())

    assert out.path is not None and out.path.is_dir()
    assert "teleop" in out.path.name and not out.path.name.startswith(".tmp")

    tbl = pq.read_table(out.path / "data.parquet")
    assert tbl.num_rows == 3
    assert set(tbl.column_names) >= {"step", "timestamp", "observation.state"}
    assert _decode_count(out.path / "videos" / "top.mp4") == 3      # row <-> frame, 1:1

    sc = EpisodeSidecar.model_validate_json((out.path / "episode.json").read_text())
    assert sc.length == 3
    assert sc.capture_id and out.path.name.endswith(sc.capture_id[:8])
    assert sc.requested_manifest == "teleop" and sc.task == "pick"
    assert sc.fps == pytest.approx(100.0)       # (3-1)/(20ms), measured from real stamps
    assert sc.record_hz is None                 # native-rate capture
    assert sc.recorded_at == datetime.fromtimestamp(0.01, tz=timezone.utc)
    assert sc.features["observation.images.top"]["shape"] == [32, 32, 3]


def test_finalize_records_nominal_next_to_measured(tmp_path):
    rec = tmp_path / "data-recordings"
    rec.mkdir()
    out = finalize(rec, RecordingContext(), _rows(3), _sources(), record_hz=23.0)
    sc = EpisodeSidecar.model_validate_json((out.path / "episode.json").read_text())
    assert sc.record_hz == 23.0                 # nominal (the requested param)
    assert sc.fps == pytest.approx(100.0)       # achieved (measured)
    assert "unfiled" in out.path.name           # no requested_manifest set


def test_finalize_discards_no_frames(tmp_path):
    rec = tmp_path / "data-recordings"
    rec.mkdir()
    out = finalize(rec, RecordingContext(), [], _sources())
    assert out.path is None and out.reason == "no frames"
    assert list(rec.iterdir()) == []            # nothing written, no .tmp
