import pytest

from koyu_runtime.services.data_recorder import Source, _validate_sources, gate, tol_ns


# --- gate (anchored-grid decimation) -----------------------------------------

def test_gate_native_rate_keeps_everything():
    assert gate(123, 0, 0) == (True, 0)


def test_gate_first_frame_anchors_grid():
    keep, nxt = gate(1000, 0, 100)
    assert keep and nxt == 1100


def test_gate_skips_frames_before_due():
    assert gate(1050, 1100, 100) == (False, 1100)


def test_gate_advances_from_due_time_not_frame_time():
    # the frame is 30 late but the next due stays on the grid (1200, not 1230),
    # so per-frame lateness doesn't compound into rate undershoot
    keep, nxt = gate(1130, 1100, 100)
    assert keep and nxt == 1200


def test_gate_reanchors_after_stall():
    # frame already past the next slot -> re-anchor; no burst through the backlog
    keep, nxt = gate(1500, 1100, 100)
    assert keep and nxt == 1600


def test_gate_60hz_clock_to_23hz_request_lands_23():
    period = int(1e9 / 23)
    kept, nxt = 0, 0
    for i in range(600):                     # 10s of 60Hz frames
        keep, nxt = gate(int(i * 1e9 / 60), nxt, period)
        kept += keep
    assert abs(kept - 230) <= 1              # ~23Hz landed although 60/23 doesn't divide


# --- tol_ns -------------------------------------------------------------------

def test_tol_is_one_full_period_plus_jitter():
    # latest-value sampling is one-sided: a healthy source can be a whole period old
    s = Source("t", "f", lambda x: x, {}, "column", 100.0)
    assert tol_ns(s) == 10_000_000 + 5_000_000


# --- source validation ----------------------------------------------------------

def _video(topic, feature):
    return Source(topic, feature, lambda x: x, {"dtype": "video", "shape": [32, 32, 3]},
                  "video", 30.0, type_name="CameraFrame")


def test_validate_requires_a_video_clock():
    col = Source("s", "observation.state", lambda x: x, {"dtype": "float32"},
                 "column", 100.0, type_name="RobStrideState")
    with pytest.raises(ValueError, match="video"):
        _validate_sources([col])


def test_validate_requires_positive_rate():
    bad = Source("cam", "observation.images.top", lambda x: x, {"dtype": "video"},
                 "video", 0.0, type_name="CameraFrame")
    with pytest.raises(ValueError, match="rate_hz"):
        _validate_sources([bad])


def test_validate_rejects_video_filename_collision():
    # Two topics cannot author the same canonical feature-key filename.
    with pytest.raises(ValueError, match="collision"):
        _validate_sources([
            _video("a", "observation.images.top"),
            _video("b", "observation.images.top"),
        ])


def test_validate_rejects_nested_video_filename():
    with pytest.raises(ValueError, match="flat filename"):
        _validate_sources([_video("a", "observation/images/top")])
