import json

from cli import main
from ipc import blackboard
from ipc.types import GlobalConfig


def runtime_dir(tmp_path, services_yaml="svc:\n  cmd: [echo, hi]\n"):
    d = tmp_path / "rt"
    d.mkdir()
    (d / "services.yaml").write_text(services_yaml)
    return d


def test_status_when_down(tmp_path, capsys):
    rc = main.main(["status", "-r", str(runtime_dir(tmp_path))])
    assert rc == 0
    assert "down" in capsys.readouterr().out


def test_status_json_when_down(tmp_path, capsys):
    rc = main.main(["status", "-r", str(runtime_dir(tmp_path)), "--json"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "[]"


def test_restart_when_down_errors(tmp_path, capsys):
    rc = main.main(["restart", "-r", str(runtime_dir(tmp_path))])
    assert rc == 1
    assert "down" in capsys.readouterr().err


def test_resolution_failure_returns_2(tmp_path, capsys):
    missing = tmp_path / "empty" / "nope"
    rc = main.main(["status", "-r", str(missing)])
    assert rc == 2
    assert "error" in capsys.readouterr().err


def test_logs_missing_errors(tmp_path, capsys):
    rc = main.main(["logs", "svc", "-r", str(runtime_dir(tmp_path))])
    assert rc == 1
    assert "no logs" in capsys.readouterr().err


def test_set_writes_inbox_request(tmp_path):
    d = runtime_dir(tmp_path)
    assert main.main(["set", "param/config", "speed", "3.5", "-r", str(d)]) == 0
    files = list((d / "services" / "param_server" / "inbox" / "param~config").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == {"key": "speed", "value": 3.5}


def test_set_persist_and_string_value(tmp_path):
    d = runtime_dir(tmp_path)
    main.main(["set", "task/ctx", "name", "demo-v1", "--persist", "-r", str(d)])
    files = list((d / "services" / "param_server" / "inbox" / "task~ctx").glob("*.json"))
    assert json.loads(files[0].read_text()) == {"key": "name", "value": "demo-v1", "persist": True}


def test_get_reads_live_blackboard(tmp_path, capsys):
    sy = ("param_server:\n  cmd: [echo, hi]\n  ipc:\n    blackboard:\n"
          "      writes:\n        param/config: GlobalConfig\n")
    d = runtime_dir(tmp_path, sy)
    w = blackboard.Writer("param/config", GlobalConfig)
    w.write(GlobalConfig(frame_id=1, speed=2.0))
    assert main.main(["get", "param/config", "-r", str(d)]) == 0
    assert json.loads(capsys.readouterr().out)["speed"] == 2.0
    assert w is not None


def test_tail_prints_recent(tmp_path, capsys):
    d = runtime_dir(tmp_path)
    log = d / "services" / "ipc_logger" / "log~x" / "state.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"v":1}\n{"v":2}\n{"v":3}\n')
    assert main.main(["tail", "log/x", "-n", "2", "-r", str(d)]) == 0
    assert capsys.readouterr().out.splitlines() == ['{"v":2}', '{"v":3}']


def test_tail_missing_errors(tmp_path, capsys):
    assert main.main(["tail", "nope/x", "-r", str(runtime_dir(tmp_path))]) == 1
    assert "no log" in capsys.readouterr().err


def test_frame_prints_newest_path(tmp_path, capsys):
    d = runtime_dir(tmp_path)
    frames = d / "services" / "ipc_logger" / "cam~x" / "frames"
    frames.mkdir(parents=True)
    (frames / "000000001.jpg").write_bytes(b"a")
    (frames / "000000002.jpg").write_bytes(b"b")
    assert main.main(["frame", "cam/x", "-r", str(d)]) == 0
    assert capsys.readouterr().out.strip().endswith("000000002.jpg")


def test_frame_missing_errors(tmp_path, capsys):
    assert main.main(["frame", "nope/x", "-r", str(runtime_dir(tmp_path))]) == 1
    assert "no frames" in capsys.readouterr().err
