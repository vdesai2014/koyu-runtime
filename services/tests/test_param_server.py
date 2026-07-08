import json

import pytest

from ipc import blackboard
from ipc.types import GlobalConfig
from services.param_server import Inbox, ParamServer

SPEED = {"speed": {"value": 1.0, "min": 0.0, "max": 10.0}}


def _ipc(topic):
    return {"blackboard": {"writes": {topic: "GlobalConfig"}}}


def _seed(home, slug, params):
    d = home / "services" / "param_server"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps(params))


def test_inbox_roundtrip(tmp_path):
    ib = Inbox(tmp_path / "ib")
    ib.submit({"key": "speed", "value": 2.0})
    ib.submit({"key": "deadzone", "value": 0.1, "persist": True})
    assert ib.drain() == [
        {"key": "speed", "value": 2.0},
        {"key": "deadzone", "value": 0.1, "persist": True},
    ]
    assert ib.drain() == []                 # drained; files removed


def test_inbox_drops_malformed_and_keeps_going(tmp_path):
    ib = Inbox(tmp_path / "ib")
    ib.submit({"key": "speed", "value": 1.0})
    (ib.dir / "999_0.json").write_text("{not json")     # poison
    assert ib.drain() == [{"key": "speed", "value": 1.0}]   # good one through, poison dropped
    assert list(ib.dir.glob("*.json")) == []                # poison removed, no recurrence


def test_bootstrap_missing_value_raises(tmp_path):
    with pytest.raises(RuntimeError, match="has no value"):
        ParamServer(tmp_path, _ipc("param/boot"))            # no params file at all


def test_set_updates_blackboard_not_disk_by_default(tmp_path):
    _seed(tmp_path, "param~live", SPEED)
    ps = ParamServer(tmp_path, _ipc("param/live"))
    ps.topics["param/live"]["inbox"].submit({"key": "speed", "value": 3.5})
    ps.on_tick()
    assert blackboard.Reader("param/live", GlobalConfig).read().speed == 3.5        # live
    on_disk = json.loads((tmp_path / "services" / "param_server" / "param~live.json").read_text())
    assert on_disk["speed"]["value"] == 1.0                                         # disk untouched


def test_persist_writes_disk_and_keeps_range(tmp_path):
    _seed(tmp_path, "param~persist", SPEED)
    ps = ParamServer(tmp_path, _ipc("param/persist"))
    ps.topics["param/persist"]["inbox"].submit({"key": "speed", "value": 4.0, "persist": True})
    ps.on_tick()
    saved = json.loads((tmp_path / "services" / "param_server" / "param~persist.json").read_text())
    assert saved["speed"]["value"] == 4.0
    assert saved["speed"]["min"] == 0.0          # range preserved


def test_out_of_range_rejected(tmp_path):
    _seed(tmp_path, "param~range", SPEED)
    ps = ParamServer(tmp_path, _ipc("param/range"))
    ps.topics["param/range"]["inbox"].submit({"key": "speed", "value": 999})
    ps.on_tick()
    assert blackboard.Reader("param/range", GlobalConfig).read().speed == 1.0       # unchanged
