from koyu_runtime.warden.services import parse_services
from koyu_runtime.warden.conf_generator import generate_conf

PY = "/usr/bin/python3"


def gen(stanzas, **kw):
    kw.setdefault("python_executable", PY)
    return generate_conf(parse_services(stanzas), **kw)


def test_global_sections_present():
    out = gen({"a": {"cmd": ["x"]}})
    assert "[unix_http_server]" in out
    assert "[supervisord]" in out
    assert "[rpcinterface:supervisor]" in out
    assert "[supervisorctl]" in out
    assert "file=%(here)s/.koyu/run/supervisor.sock" in out
    assert "serverurl=unix://%(here)s/.koyu/run/supervisor.sock" in out
    assert "directory=%(here)s" in out


def test_program_section_basic():
    out = gen({"sim": {"cmd": ["./sim"]}})
    assert "[program:sim]" in out
    assert "command=./sim" in out
    assert "stdout_logfile=%(here)s/.koyu/logs/sim.log" in out
    assert "stderr_logfile=%(here)s/.koyu/logs/sim.err" in out
    assert "stopasgroup=true" in out
    assert "killasgroup=true" in out
    assert "stopwaitsecs=10" in out


def test_house_env_injected():
    out = gen({"sim": {"cmd": ["x"]}})
    assert 'PYTHONUNBUFFERED="1"' in out
    assert 'SERVICE_NAME="sim"' in out
    assert 'KOYU_RUNTIME_DIR="%(here)s"' in out


def test_user_env_appended():
    out = gen({"sim": {"cmd": ["x"], "env": {"RATE": "50"}}})
    assert 'RATE="50"' in out


def test_percent_escaped_in_user_env_but_not_here():
    out = gen({"sim": {"cmd": ["x"], "env": {"FMT": "100%done"}}})
    assert 'FMT="100%%done"' in out
    assert 'KOYU_RUNTIME_DIR="%(here)s"' in out  # our token stays literal


def test_python_substitution():
    out = gen({"sim": {"cmd": ["python3", "-m", "sim"]}})
    assert f"command={PY} -m sim" in out


def test_python_not_substituted_for_other_cmds():
    out = gen({"sim": {"cmd": ["./bin/video-bridge"]}})
    assert "command=./bin/video-bridge" in out


def test_oneshot_sugar_overrides():
    out = gen({"job": {"cmd": ["x"], "oneshot": True, "startsecs": 5}})
    assert "startsecs=0" in out
    assert "autorestart=false" in out


def test_non_oneshot_keeps_values():
    out = gen({"svc": {"cmd": ["x"], "startsecs": 3}})
    assert "startsecs=3" in out
    assert "autorestart=unexpected" in out


def test_exitcodes_joined():
    assert "exitcodes=0,2" in gen({"svc": {"cmd": ["x"], "exitcodes": [0, 2]}})


def test_multiple_programs_in_order():
    out = gen({"a": {"cmd": ["x"]}, "b": {"cmd": ["y"]}})
    assert out.index("[program:a]") < out.index("[program:b]")


def test_ipc_block_absent_from_output():
    out = gen({"sim": {"cmd": ["x"], "ipc": {"publishes": {"t": "RobStrideCommand"}}}})
    assert "RobStrideCommand" not in out
    assert "publishes" not in out
