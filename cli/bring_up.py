"""The composition root for a full koyu bring-up.

warden boots any services.yaml on its own; these are the *assembled* system —
warden's boot/apply plus the ipc package's first-party hooks (struct type-check
before launch, segment sweep at the generation boundary) and any dynamically
registered third-party hooks. These are the functions the CLI and full-system
tests call so the checks are never accidentally skipped. ``warden.boot.up``
remains available directly as the bare-supervisor escape hatch.
"""

from __future__ import annotations

from ipc import checks
from warden import boot, hooks
from warden.runtime_dir import Runtime


def _validators():
    # first-party type-check, then anything a plugin self-registered
    return [checks.typecheck, *hooks.validators()]


def bring_up(runtime: Runtime) -> None:
    """Bring a runtime up with the standard IPC checks wired in."""
    boot.up(
        runtime,
        validators=_validators(),
        cleanups=[checks.sweep_segments, *hooks.cleanups()],
    )


def reconfigure(runtime: Runtime) -> dict[str, list[str]]:
    """Hot-reload a live runtime, type-checking the edit before it applies."""
    return boot.apply(runtime, validators=_validators())
