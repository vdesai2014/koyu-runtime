"""Bring a runtime up and down: validate, generate the conf, run supervisord."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from . import conf_generator
from .runtime_dir import Runtime
from .services import ServiceSpec, load_services
from .supervisord_client import SupervisordClient

# A validator is handed the services.yaml PATH (not parsed specs) so it can read
# whatever blocks it owns — e.g. the ipc: block warden ignores — without warden
# knowing anything about them. It raises to abort the boot.
Validator = Callable[[Path], None]
Cleanup = Callable[[Runtime], None]

_READY_TIMEOUT_S = 10.0


class BootError(Exception):
    """Raised when a runtime can't be brought up or reconfigured."""


def prepare(
    runtime: Runtime,
    *,
    validators: Iterable[Validator] = (),
    cleanups: Iterable[Cleanup] = (),
    python_executable: str = sys.executable,
) -> dict[str, ServiceSpec]:
    """Validate services.yaml, run hooks, and write the supervisord.conf.

    Validators run first and abort by raising; cleanups run next; then the state
    dirs are created and the conf is written. Returns the parsed specs.
    """
    specs = load_services(runtime.services_yaml)
    for validate in validators:
        validate(runtime.services_yaml)
    for cleanup in cleanups:
        cleanup(runtime)
    _write_conf(runtime, specs, python_executable)
    return specs


def up(
    runtime: Runtime,
    *,
    validators: Iterable[Validator] = (),
    cleanups: Iterable[Cleanup] = (),
    python_executable: str = sys.executable,
    ready_timeout_s: float = _READY_TIMEOUT_S,
) -> None:
    """Generate the conf and start supervisord, waiting until it accepts calls."""
    if SupervisordClient(runtime.socket).is_running():
        raise BootError(f"runtime is already up: {runtime.dir}")
    prepare(runtime, validators=validators, cleanups=cleanups, python_executable=python_executable)
    _start_supervisord(runtime)
    _wait_until_running(runtime, ready_timeout_s)


def apply(
    runtime: Runtime,
    *,
    validators: Iterable[Validator] = (),
    python_executable: str = sys.executable,
) -> dict[str, list[str]]:
    """Regenerate the conf and reload, restarting only changed programs.

    Validators run so a bad edit aborts before anything reloads. Cleanups do not —
    this is a hot reload of a live runtime, not a fresh boot.
    """
    client = SupervisordClient(runtime.socket)
    if not client.is_running():
        raise BootError(f"runtime is not up: {runtime.dir}")
    specs = load_services(runtime.services_yaml)
    for validate in validators:
        validate(runtime.services_yaml)
    _write_conf(runtime, specs, python_executable)
    return client.apply()


def down(runtime: Runtime, *, ready_timeout_s: float = _READY_TIMEOUT_S) -> None:
    """Shut the runtime's supervisord down and wait until it's fully gone, so a
    following up() doesn't race the dying daemon for the socket."""
    client = SupervisordClient(runtime.socket)
    if not client.is_running():
        return
    client.shutdown()
    deadline = time.monotonic() + ready_timeout_s
    while time.monotonic() < deadline:
        if not client.is_running():
            return
        time.sleep(0.1)
    raise BootError(f"supervisord did not shut down within {ready_timeout_s:.0f}s")


def _write_conf(runtime: Runtime, specs: dict[str, ServiceSpec], python_executable: str) -> None:
    runtime.ensure_state_dirs()
    runtime.conf.write_text(
        conf_generator.generate_conf(specs, python_executable=python_executable),
        encoding="utf-8",
    )


def _start_supervisord(runtime: Runtime) -> None:
    # An absolute conf path is required: directory=%(here)s changes the daemon's
    # cwd, so a relative path would break a later reread.
    result = subprocess.run(
        [sys.executable, "-m", "supervisor.supervisord", "-c", str(runtime.conf)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BootError(f"supervisord failed to start:\n{detail}")


def _wait_until_running(runtime: Runtime, timeout_s: float) -> None:
    client = SupervisordClient(runtime.socket)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if client.is_running():
            return
        time.sleep(0.1)
    raise BootError(f"supervisord did not become reachable within {timeout_s:.0f}s")
