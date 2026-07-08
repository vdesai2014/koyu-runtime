import pytest

from warden.runtime_dir import Runtime, RuntimeResolutionError, resolve


def make_runtime_dir(parent, name="rt"):
    d = parent / name
    d.mkdir()
    (d / "services.yaml").write_text("svc:\n  cmd: [echo, hi]\n")
    return d


def test_explicit_resolves_to_absolute(tmp_path):
    d = make_runtime_dir(tmp_path)
    assert resolve(d, env={}, cwd=tmp_path).dir == d.resolve()


def test_explicit_missing_raises(tmp_path):
    with pytest.raises(RuntimeResolutionError, match="not a directory"):
        resolve(tmp_path / "nope", env={}, cwd=tmp_path)


def test_explicit_file_raises(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(RuntimeResolutionError, match="not a directory"):
        resolve(f, env={}, cwd=tmp_path)


def test_env_var_used(tmp_path):
    d = make_runtime_dir(tmp_path)
    assert resolve(None, env={"KOYU_RUNTIME": str(d)}, cwd=tmp_path).dir == d.resolve()


def test_empty_env_value_falls_through_to_cwd(tmp_path):
    d = make_runtime_dir(tmp_path)
    assert resolve(None, env={"KOYU_RUNTIME": ""}, cwd=d).dir == d.resolve()


def test_cwd_finds_services_here(tmp_path):
    d = make_runtime_dir(tmp_path)
    assert resolve(None, env={}, cwd=d).dir == d.resolve()


def test_cwd_walks_up_to_parent(tmp_path):
    d = make_runtime_dir(tmp_path)
    sub = d / "a" / "b"
    sub.mkdir(parents=True)
    assert resolve(None, env={}, cwd=sub).dir == d.resolve()


def test_no_runtime_raises(tmp_path):
    with pytest.raises(RuntimeResolutionError, match="no runtime found"):
        resolve(None, env={}, cwd=tmp_path)


def test_precedence_explicit_over_env_over_cwd(tmp_path):
    a = make_runtime_dir(tmp_path, "a")
    b = make_runtime_dir(tmp_path, "b")
    c = make_runtime_dir(tmp_path, "c")
    assert resolve(a, env={"KOYU_RUNTIME": str(b)}, cwd=c).dir == a.resolve()
    assert resolve(None, env={"KOYU_RUNTIME": str(b)}, cwd=c).dir == b.resolve()
    assert resolve(None, env={}, cwd=c).dir == c.resolve()


def test_paths_derive_under_dot_koyu(tmp_path):
    base = (tmp_path / "rt").resolve()
    rt = Runtime(base)
    assert rt.services_yaml == base / "services.yaml"
    assert rt.state_dir == base / ".koyu"
    assert rt.conf == base / "supervisord.conf"
    assert rt.run_dir == base / ".koyu" / "run"
    assert rt.socket == base / ".koyu" / "run" / "supervisor.sock"
    assert rt.pidfile == base / ".koyu" / "run" / "supervisord.pid"
    assert rt.daemon_log == base / ".koyu" / "run" / "supervisord.log"
    assert rt.logs_dir == base / ".koyu" / "logs"


def test_ensure_state_dirs_creates(tmp_path):
    rt = Runtime((tmp_path / "rt").resolve())
    rt.dir.mkdir()
    assert not rt.run_dir.exists()
    rt.ensure_state_dirs()
    assert rt.run_dir.is_dir()
    assert rt.logs_dir.is_dir()
