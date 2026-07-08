"""Load and validate a runtime's ``services.yaml`` into typed ``ServiceSpec``s.

Understands process fields (cmd, env, restart policy) and ignores any other
block (e.g. ``ipc:``) so a service can carry its own config there. Unknown keys
are skipped rather than rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ServicesError(ValueError):
    """Raised when services.yaml is malformed or a service spec is invalid."""


# supervisord accepts exactly these literal values for `autorestart`.
_AUTORESTART_VALUES = {"true", "false", "unexpected"}

# Env keys the generator injects itself; users may not set them.
_RESERVED_ENV = {"SERVICE_NAME", "KOYU_RUNTIME_DIR"}


@dataclass(frozen=True)
class ServiceSpec:
    """A single validated, fully-defaulted service definition."""

    name: str
    cmd: list[str]
    env: dict[str, str] = field(default_factory=dict)
    autostart: bool = True
    autorestart: str = "unexpected"  # one of _AUTORESTART_VALUES
    startsecs: int = 1
    startretries: int = 3
    stopwaitsecs: int = 10
    exitcodes: list[int] = field(default_factory=lambda: [0])
    oneshot: bool = False


def load_services(path: str | Path) -> dict[str, ServiceSpec]:
    """Read + validate a services.yaml file into ``{name: ServiceSpec}``."""
    path = Path(path)
    if not path.is_file():
        raise ServicesError(f"services.yaml not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ServicesError(f"services.yaml is not valid YAML: {exc}") from exc
    return parse_services(raw)


def parse_services(raw: Any) -> dict[str, ServiceSpec]:
    """Validate an already-parsed services.yaml mapping (insertion order kept)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ServicesError(
            "services.yaml must be a mapping of service-name -> spec, "
            f"got {type(raw).__name__}"
        )
    return {str(name): _parse_one(str(name), stanza) for name, stanza in raw.items()}


def _parse_one(name: str, stanza: Any) -> ServiceSpec:
    if not isinstance(stanza, dict):
        raise ServicesError(
            f"service '{name}': spec must be a mapping, got {type(stanza).__name__}"
        )
    return ServiceSpec(
        name=name,
        cmd=_parse_cmd(name, stanza.get("cmd")),
        env=_parse_env(name, stanza.get("env")),
        autostart=_parse_bool(name, "autostart", stanza.get("autostart", True)),
        autorestart=_parse_autorestart(name, stanza.get("autorestart", "unexpected")),
        startsecs=_parse_nonneg_int(name, "startsecs", stanza.get("startsecs", 1)),
        startretries=_parse_nonneg_int(name, "startretries", stanza.get("startretries", 3)),
        stopwaitsecs=_parse_nonneg_int(name, "stopwaitsecs", stanza.get("stopwaitsecs", 10)),
        exitcodes=_parse_exitcodes(name, stanza.get("exitcodes", [0])),
        oneshot=_parse_bool(name, "oneshot", stanza.get("oneshot", False)),
    )


def _parse_cmd(name: str, cmd: Any) -> list[str]:
    if cmd is None:
        raise ServicesError(f"service '{name}': missing required field 'cmd'")
    if not isinstance(cmd, list) or not cmd:
        raise ServicesError(f"service '{name}': 'cmd' must be a non-empty list, got {cmd!r}")
    out: list[str] = []
    for item in cmd:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ServicesError(f"service '{name}': 'cmd' entries must be scalars, got {item!r}")
        out.append(str(item))
    return out


def _parse_env(name: str, env: Any) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise ServicesError(f"service '{name}': 'env' must be a mapping, got {type(env).__name__}")
    out: dict[str, str] = {}
    for key, value in env.items():
        key = str(key)
        if key in _RESERVED_ENV:
            raise ServicesError(
                f"service '{name}': env key '{key}' is reserved (injected by the supervisor)"
            )
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            out[key] = str(value)
        else:
            raise ServicesError(
                f"service '{name}': env value for '{key}' must be a scalar, got {value!r}"
            )
    return out


def _parse_bool(name: str, field_name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    raise ServicesError(f"service '{name}': '{field_name}' must be a boolean, got {value!r}")


def _parse_autorestart(name: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.lower() in _AUTORESTART_VALUES:
        return value.lower()
    raise ServicesError(
        f"service '{name}': 'autorestart' must be true, false, or 'unexpected', got {value!r}"
    )


def _parse_nonneg_int(name: str, field_name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServicesError(f"service '{name}': '{field_name}' must be an integer, got {value!r}")
    if value < 0:
        raise ServicesError(f"service '{name}': '{field_name}' must be >= 0, got {value}")
    return value


def _parse_exitcodes(name: str, value: Any) -> list[int]:
    if isinstance(value, bool):
        raise ServicesError(f"service '{name}': 'exitcodes' must be an int or list of ints, got {value!r}")
    if isinstance(value, int):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ServicesError(
            f"service '{name}': 'exitcodes' must be a non-empty int or list of ints, got {value!r}"
        )
    out: list[int] = []
    for code in value:
        if isinstance(code, bool) or not isinstance(code, int):
            raise ServicesError(f"service '{name}': 'exitcodes' entries must be ints, got {code!r}")
        out.append(code)
    return out
