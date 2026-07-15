"""Resolve which runtime directory a command targets, and derive its paths.

A runtime is a directory containing a ``services.yaml``. All generated state — the
supervisord.conf, the control socket, the pidfile, and logs — lives under
``<dir>/.koyu``. Resolution order: an explicit path, then the ``KOYU_RUNTIME``
environment variable, then the nearest ``services.yaml`` at or above the current
directory. If none of those yield a directory, resolution fails rather than guessing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SERVICES_FILENAME = "services.yaml"
_STATE_DIRNAME = ".koyu"
_RUNTIME_ENV = "KOYU_RUNTIME"


class RuntimeResolutionError(Exception):
    """Raised when no runtime directory can be determined."""


@dataclass(frozen=True)
class Runtime:
    """A resolved runtime directory and the paths derived from it."""

    dir: Path

    @property
    def services_yaml(self) -> Path:
        return self.dir / SERVICES_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.dir / _STATE_DIRNAME

    @property
    def conf(self) -> Path:
        # At the runtime root so supervisord's %(here)s resolves to the runtime dir.
        return self.dir / "supervisord.conf"

    @property
    def run_dir(self) -> Path:
        return self.state_dir / "run"

    @property
    def socket(self) -> Path:
        return self.run_dir / "supervisor.sock"

    @property
    def pidfile(self) -> Path:
        return self.run_dir / "supervisord.pid"

    @property
    def daemon_log(self) -> Path:
        return self.run_dir / "supervisord.log"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    def ensure_state_dirs(self) -> None:
        """Create the run/ and logs/ directories supervisord writes into."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def resolve(
    explicit: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> Runtime:
    """Determine the target runtime (see module docstring for the order)."""
    if explicit is not None and str(explicit):
        return Runtime(_as_dir(explicit, source="--runtime"))

    env = os.environ if env is None else env
    env_value = env.get(_RUNTIME_ENV)
    if env_value:
        return Runtime(_as_dir(env_value, source=_RUNTIME_ENV))

    start = (Path.cwd() if cwd is None else Path(cwd)).expanduser().resolve()
    for directory in (start, *start.parents):
        if (directory / SERVICES_FILENAME).is_file():
            return Runtime(directory)

    raise RuntimeResolutionError(
        f"no runtime found: pass --runtime <dir>, set {_RUNTIME_ENV}, or run from a "
        f"directory at or below one containing {SERVICES_FILENAME} "
        f"(searched upward from {start})"
    )


def _as_dir(path: str | os.PathLike[str], *, source: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_dir():
        raise RuntimeResolutionError(f"{source} is not a directory: {resolved}")
    return resolved
