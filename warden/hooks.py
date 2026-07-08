"""Registry for boot-time validators and cleanups contributed by other packages.

A validator receives the services.yaml path and raises to abort the boot. A
cleanup receives the runtime and runs side effects (e.g. clearing stale state)
before launch. This package registers none of its own.

This is the dynamic, self-registration path (for optional/third-party hooks).
Known first-party checks are wired explicitly by the composition root instead.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .runtime_dir import Runtime

Validator = Callable[[Path], None]
Cleanup = Callable[[Runtime], None]

_validators: list[Validator] = []
_cleanups: list[Cleanup] = []


def register_validator(fn: Validator) -> None:
    _validators.append(fn)


def register_cleanup(fn: Cleanup) -> None:
    _cleanups.append(fn)


def validators() -> tuple[Validator, ...]:
    return tuple(_validators)


def cleanups() -> tuple[Cleanup, ...]:
    return tuple(_cleanups)


def clear() -> None:
    """Drop all registered hooks."""
    _validators.clear()
    _cleanups.clear()
