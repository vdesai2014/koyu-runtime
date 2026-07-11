"""Verdict interface: capture_id flows into the sidecar; verdicts merge at
finalize; mismatches quarantine loudly (see interfaces.md)."""

import json

from services.data_recorder import Source, finalize
from services.episode_schema import EpisodeSidecar, RecordingContext
from services.inbox import Inbox, inbox_path

from .test_data_recorder_bundle import _rows, _sources


def _finalize(tmp_path, **kw):
    rec = tmp_path / "data-recordings"
    rec.mkdir(exist_ok=True)
    out = finalize(rec, RecordingContext(task="pick"), _rows(3), _sources(), **kw)
    assert out.path is not None
    return EpisodeSidecar.model_validate_json((out.path / "episode.json").read_text())


def test_capture_id_passthrough(tmp_path):
    side = _finalize(tmp_path, capture_id="a" * 32)
    assert side.capture_id == "a" * 32          # minted at start, not at write


def test_verdict_merges_into_sidecar(tmp_path):
    side = _finalize(tmp_path, capture_id="b" * 32,
                     verdict={"capture_id": "b" * 32, "reward": 1.0,
                              "events": [{"t": 123, "type": "eval_success"}]})
    assert side.reward == 1.0
    assert side.events == [{"t": 123, "type": "eval_success"}]


def test_no_verdict_means_none(tmp_path):
    side = _finalize(tmp_path, capture_id="c" * 32)
    assert side.reward is None and side.events == []


def test_inbox_path_law(tmp_path):
    p = inbox_path(tmp_path, "data_recorder", "verdicts")
    assert p == tmp_path / "services" / "data_recorder" / "inbox" / "verdicts"


def test_inbox_quarantine_keeps_and_explains(tmp_path):
    box = Inbox(inbox_path(tmp_path, "data_recorder", "verdicts"))
    box.quarantine({"capture_id": "x" * 32, "reward": 0.0}, "capture_id mismatch")
    parked = list((box.dir.parent / "quarantine").glob("*.json"))
    assert len(parked) == 1
    body = json.loads(parked[0].read_text())
    assert body["reason"].startswith("capture_id mismatch")
    assert body["request"]["capture_id"] == "x" * 32


def test_inbox_submit_drain_roundtrip(tmp_path):
    box = Inbox(inbox_path(tmp_path, "data_recorder", "verdicts"))
    box.submit({"capture_id": "y" * 32, "reward": 0.5})
    box.submit({"capture_id": "y" * 32, "reward": 0.7})
    got = box.drain()
    assert [g["reward"] for g in got] == [0.5, 0.7]   # ordered
    assert box.drain() == []                          # drained
