"""Render a ``supervisord.conf`` (INI) from validated ``ServiceSpec``s.

A pure function: specs in, conf text out, no filesystem access. Every path is
emitted as ``%(here)s/...`` so supervisord resolves it relative to the conf's own
location, keeping the file relocatable. The only external input is the Python
interpreter used to run python services (overridable for tests).

House defaults applied to every program:
  - ``%`` in user values is doubled to ``%%``; our own ``%(here)s`` tokens stay literal.
  - ``PYTHONUNBUFFERED=1`` + ``SERVICE_NAME`` + ``KOYU_RUNTIME_DIR`` are injected.
  - ``stopasgroup``/``killasgroup`` and a two-phase stop via ``stopwaitsecs``.
  - ``oneshot`` -> ``startsecs=0, autorestart=false``.
"""

from __future__ import annotations

import sys

from .services import ServiceSpec

# All daemon state lives under the runtime dir (= %(here)s).
_SOCK = "%(here)s/.koyu/run/supervisor.sock"
_PIDFILE = "%(here)s/.koyu/run/supervisord.pid"
_DAEMON_LOG = "%(here)s/.koyu/run/supervisord.log"


def generate_conf(
    specs: dict[str, ServiceSpec],
    *,
    python_executable: str = sys.executable,
) -> str:
    """Render the full supervisord.conf for a runtime's services."""
    blocks = [_global_sections()]
    blocks.extend(_program_section(spec, python_executable) for spec in specs.values())
    return "\n".join(blocks) + "\n"


def _global_sections() -> str:
    return "\n".join(
        [
            "[unix_http_server]",
            f"file={_SOCK}",
            "",
            "[supervisord]",
            f"pidfile={_PIDFILE}",
            f"logfile={_DAEMON_LOG}",
            "directory=%(here)s",
            "loglevel=info",
            "nodaemon=false",
            "",
            "[rpcinterface:supervisor]",
            "supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface",
            "",
            "[supervisorctl]",
            f"serverurl=unix://{_SOCK}",
            "",
        ]
    )


def _program_section(spec: ServiceSpec, python_executable: str) -> str:
    # oneshot sugar: a run-once job, not a long-running service. Forces a fast
    # clean exit to read as success, and never relaunches.
    if spec.oneshot:
        startsecs, autorestart = 0, "false"
    else:
        startsecs, autorestart = spec.startsecs, spec.autorestart

    log = f"%(here)s/.koyu/logs/{spec.name}"
    return "\n".join(
        [
            f"[program:{spec.name}]",
            f"command={_command(spec.cmd, python_executable)}",
            "directory=%(here)s",
            f"environment={_environment(spec)}",
            f"autostart={'true' if spec.autostart else 'false'}",
            f"autorestart={autorestart}",
            f"exitcodes={','.join(str(c) for c in spec.exitcodes)}",
            f"startsecs={startsecs}",
            f"startretries={spec.startretries}",
            "stopasgroup=true",
            "killasgroup=true",
            f"stopwaitsecs={spec.stopwaitsecs}",
            f"stdout_logfile={log}.log",
            f"stderr_logfile={log}.err",
            "",
        ]
    )


def _command(cmd: list[str], python_executable: str) -> str:
    parts = list(cmd)
    # Run services under the same interpreter as the supervisor (venv correctness).
    if parts and parts[0] in ("python", "python3"):
        parts[0] = python_executable
    return " ".join(_escape(p) for p in parts)


def _environment(spec: ServiceSpec) -> str:
    # House-injected vars first, then the user's. KOYU_RUNTIME_DIR carries the
    # literal %(here)s token (a supervisord expansion) and must NOT be escaped.
    parts = [
        f'PYTHONUNBUFFERED="{_escape("1")}"',
        f'SERVICE_NAME="{_escape(spec.name)}"',
        'KOYU_RUNTIME_DIR="%(here)s"',
    ]
    parts.extend(f'{key}="{_escape(value)}"' for key, value in spec.env.items())
    return ",".join(parts)


def _escape(value: str) -> str:
    """Double literal % so supervisord doesn't treat it as an expansion."""
    return value.replace("%", "%%")
