import pytest

from warden.boot import prepare
from warden.services import ServicesError
from warden.runtime_dir import Runtime

PY = "/usr/bin/python3"


def make_runtime(tmp_path, services_text="svc:\n  cmd: [echo, hi]\n"):
    d = tmp_path / "rt"
    d.mkdir()
    (d / "services.yaml").write_text(services_text)
    return Runtime(d.resolve())


def test_prepare_writes_conf_and_dirs(tmp_path):
    rt = make_runtime(tmp_path)
    specs = prepare(rt, python_executable=PY)
    assert rt.conf.is_file()
    assert rt.run_dir.is_dir()
    assert rt.logs_dir.is_dir()
    assert "[program:svc]" in rt.conf.read_text()
    assert "svc" in specs


def test_prepare_runs_validators_with_path(tmp_path):
    rt = make_runtime(tmp_path)
    seen = []
    prepare(rt, validators=[seen.append], python_executable=PY)
    assert seen == [rt.services_yaml]


def test_validator_failure_aborts_before_conf(tmp_path):
    rt = make_runtime(tmp_path)

    def bad(_path):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        prepare(rt, validators=[bad], python_executable=PY)
    assert not rt.conf.exists()
    assert not rt.run_dir.exists()


def test_prepare_runs_cleanups_with_runtime(tmp_path):
    rt = make_runtime(tmp_path)
    seen = []
    prepare(rt, cleanups=[seen.append], python_executable=PY)
    assert seen == [rt]


def test_prepare_missing_services_yaml_raises(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    rt = Runtime(d.resolve())
    with pytest.raises(ServicesError, match="not found"):
        prepare(rt, python_executable=PY)
