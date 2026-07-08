"""End-to-end: boot a real (nodaemon) supervisord against a fake runtime.

supervisord runs in nodaemon mode here (no double-fork) so it survives as a normal
child process. Each test gets a fresh runtime + daemon via the `runtime` fixture.
"""

import subprocess
import sys
import time

import pytest

from warden import boot
from warden.runtime_dir import Runtime
from warden.supervisord_client import SupervisordClient

# A trivial service: prints its injected env on a loop so logs are assertable.
SVC = """\
import os, time
name = os.environ.get("SERVICE_NAME", "?")
rt = os.environ.get("KOYU_RUNTIME_DIR", "?")
greeting = os.environ.get("GREETING", "<unset>")
i = 0
while True:
    print(f"{name} i={i} GREETING={greeting} KOYU_RUNTIME_DIR={rt}", flush=True)
    i += 1
    time.sleep(0.3)
"""


def _services_yaml(greeting: str = "hello") -> str:
    return (
        "alpha:\n"
        "  cmd: [python3, svc.py]\n"
        "  env:\n"
        f"    GREETING: {greeting}\n"
        "beta:\n"
        "  cmd: [python3, svc.py]\n"
    )


def _wait(pred, timeout_s: float = 12.0, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")


def _pids(client) -> dict[str, int]:
    return {p["name"]: p["pid"] for p in client.process_info()}


def _all_running(client) -> bool:
    info = client.process_info()
    return len(info) == 2 and all(p["statename"] == "RUNNING" for p in info)


def _log(rt, name) -> str:
    path = rt.logs_dir / f"{name}.log"
    return path.read_text() if path.exists() else ""


@pytest.fixture
def runtime(tmp_path):
    d = tmp_path / "rt"
    d.mkdir()
    (d / "svc.py").write_text(SVC)
    (d / "services.yaml").write_text(_services_yaml())
    rt = Runtime(d.resolve())
    boot.prepare(rt)
    proc = subprocess.Popen(
        [sys.executable, "-m", "supervisor.supervisord", "-n", "-c", str(rt.conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = SupervisordClient(rt.socket)
    try:
        try:
            _wait(client.is_running)
        except AssertionError:
            proc.kill()
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise AssertionError(f"supervisord did not come up:\n{err}")
        yield rt, client
    finally:
        try:
            if client.is_running():
                client.shutdown()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        finally:
            if proc.stderr:
                proc.stderr.close()


def test_up_starts_fleet_and_expands_here(runtime):
    rt, client = runtime
    _wait(lambda: _all_running(client))
    assert {p["name"] for p in client.process_info()} == {"alpha", "beta"}

    # The load-bearing question: %(here)s must expand inside environment=, so the
    # service sees the real runtime dir, not the literal token.
    _wait(lambda: "KOYU_RUNTIME_DIR=" in _log(rt, "alpha"))
    log = _log(rt, "alpha")
    assert f"KOYU_RUNTIME_DIR={rt.dir}" in log
    assert "%(here)s" not in log
    assert "GREETING=hello" in log


def test_down_waits_so_immediate_up_succeeds(tmp_path):
    # down() must block until the daemon is fully gone, or a following up() races
    # it for the socket ("Another program is already listening on a port").
    d = tmp_path / "rt"
    d.mkdir()
    (d / "svc.py").write_text(SVC)
    (d / "services.yaml").write_text(_services_yaml())
    rt = Runtime(d.resolve())
    client = SupervisordClient(rt.socket)
    try:
        boot.up(rt)
        _wait(lambda: _all_running(client))
        boot.down(rt)
        assert not client.is_running()          # truly down by the time down() returns
        boot.up(rt)                              # the formerly-racing call
        _wait(lambda: _all_running(client))
    finally:
        boot.down(rt)


def test_restart_then_apply_only_touches_changed(runtime):
    rt, client = runtime
    _wait(lambda: _all_running(client))
    before = _pids(client)

    # restart one service: its pid changes, the other is untouched
    client.restart_process("alpha")
    _wait(lambda: _all_running(client))
    after_restart = _pids(client)
    assert after_restart["alpha"] != before["alpha"]
    assert after_restart["beta"] == before["beta"]

    # edit alpha's env and apply: only alpha reloads, beta keeps its pid
    (rt.dir / "services.yaml").write_text(_services_yaml(greeting="changed"))
    result = boot.apply(rt)
    assert "alpha" in result["changed"]
    assert "beta" not in result["changed"]
    _wait(lambda: "GREETING=changed" in _log(rt, "alpha"))
    after_apply = _pids(client)
    assert after_apply["alpha"] != after_restart["alpha"]
    assert after_apply["beta"] == after_restart["beta"]
