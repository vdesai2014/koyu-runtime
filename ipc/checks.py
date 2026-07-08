"""Boot-time IPC checks, contributed to warden as hooks.

``typecheck`` is a validator: given the path to a ``services.yaml``, it resolves
every declared topic type and — for native (Rust) services — compares the
struct layout the binary reports (``--type-check``) against the Python struct.
A mismatch raises, aborting the boot before any process starts.

``sweep_segments`` is a cleanup: it wipes the iceoryx2 segments at a generation
boundary (both ``/tmp/iceoryx2`` management state AND the ``/dev/shm`` payload).

Neither imports warden. The composition root passes them to ``boot.up``.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import yaml

from . import types


class TypeCheckError(Exception):
    """Raised when a service's declared IPC types are unknown or mismatched."""


# Directions in a services.yaml ipc block that carry ``topic: type`` pairs.
# Works for the flat schema (publishes/subscribes at top level) and the grouped
# one (blackboard/streams families one level down).
_TYPED_DIRECTIONS = ("writes", "reads", "publishes", "subscribes")


def _typed_topics(ipc_block: dict) -> dict[str, str]:
    """Extract ``{topic: type_name}`` from an ipc block.

    Two layouts: *flat* (directions at the top: ``ipc: {publishes: {...}}``) or
    *grouped* (families at the top: ``ipc: {streams: {publishes: {...}}}``). We
    pick one by whether any top-level key is a direction — never both, and never
    a recursive re-scan, so a topic legitimately named ``writes`` can't be
    mistaken for a direction or hallucinate a phantom ``type`` topic.
    """
    found: dict[str, str] = {}

    def collect(directions_owner: dict) -> None:
        for direction in _TYPED_DIRECTIONS:
            block = directions_owner.get(direction)
            if not isinstance(block, dict):
                continue
            for topic, spec in block.items():
                if isinstance(spec, str):
                    found[topic] = spec                 # {topic: TypeName}
                elif isinstance(spec, dict) and "type" in spec:
                    found[topic] = spec["type"]         # {topic: {type: TypeName, ...}}

    if any(key in _TYPED_DIRECTIONS for key in ipc_block):
        collect(ipc_block)                              # flat
    else:
        for family in ipc_block.values():               # grouped: one level of families
            if isinstance(family, dict):
                collect(family)
    return found


def _is_native(cmd: list) -> bool:
    """A native binary exposes ``--type-check``; a python service doesn't.

    Scan every token, not just cmd[0], so a wrapped invocation
    (``env python …``, ``poetry run python …``) is still recognized as python and
    never has ``--type-check`` run against it. Erring toward "not native" is the
    safe failure: at worst a real Rust mismatch goes uncaught at boot, rather than
    a python service getting spawned (with side effects) mid-check.
    """
    return bool(cmd) and not any("python" in Path(str(arg)).name.lower() for arg in cmd)


def _reported_layout(reported: dict, declared_name: str, struct: type) -> dict | None:
    """Find the binary-reported layout under any name the struct goes by — the
    services.yaml name, the python class name, or the crate-qualified one — since
    different Rust crates may print short or fully-qualified type names."""
    for candidate in (declared_name, struct.__name__, types.type_name(struct)):
        if candidate in reported:
            return reported[candidate]
    return None


def parse_layout(text: str) -> dict[str, dict]:
    """Parse ``print_type_layout()`` output into ``{type_name: layout}``.

    Lines: ``TYPE <name> SIZE <n>`` and ``FIELD <name> OFFSET <o> SIZE <s>``.
    """
    layouts: dict[str, dict] = {}
    current: str | None = None
    for line in text.splitlines():
        p = line.split()
        if len(p) >= 4 and p[0] == "TYPE" and p[2] == "SIZE":
            current = p[1]
            layouts[current] = {"size": int(p[3]), "fields": []}
        elif len(p) >= 6 and p[0] == "FIELD" and p[2] == "OFFSET" and p[4] == "SIZE" and current:
            layouts[current]["fields"].append((p[1], int(p[3]), int(p[5])))
    return layouts


def _binary_layouts(cmd: list) -> dict[str, dict]:
    try:
        proc = subprocess.run(
            [*map(str, cmd), "--type-check"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TypeCheckError(f"could not run --type-check on {cmd[0]!r}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise TypeCheckError(f"--type-check failed for {cmd[0]!r}: {detail}")
    return parse_layout(proc.stdout)


def typecheck(services_yaml: str | Path) -> None:
    """Validator: resolve every declared type; verify native struct layouts."""
    raw = yaml.safe_load(Path(services_yaml).read_text(encoding="utf-8")) or {}
    for name, stanza in raw.items():
        stanza = stanza or {}
        ipc_block = stanza.get("ipc")
        if not isinstance(ipc_block, dict):
            continue
        declared = _typed_topics(ipc_block)

        # 1. Every declared type must resolve — catches typos in any service.
        structs: dict[str, type] = {}
        for topic, tyname in declared.items():
            try:
                structs[topic] = types.resolve(tyname)
            except types.UnknownType as exc:
                raise TypeCheckError(
                    f"service '{name}': topic '{topic}' uses unknown type '{tyname}'"
                ) from exc

        # 2. Native services: the binary's reported layout must match Python.
        #    Skip when nothing is typed (e.g. an event-only native tool) — there's
        #    nothing to compare, and running --type-check on a binary that doesn't
        #    implement it would abort the boot.
        cmd = stanza.get("cmd") or []
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)              # tolerate an unquoted string command
        if not declared or not _is_native(cmd):
            continue
        reported = _binary_layouts(cmd)
        for topic, tyname in declared.items():
            want = types.layout(structs[topic])
            got = _reported_layout(reported, tyname, structs[topic])
            if got is None:
                raise TypeCheckError(
                    f"service '{name}': binary does not expose type '{tyname}' "
                    f"(topic '{topic}'); --type-check reported {sorted(reported)}"
                )
            if got != want:
                raise TypeCheckError(
                    f"service '{name}': layout mismatch for '{tyname}' (topic '{topic}')\n"
                    f"  binary: {got}\n  python: {want}"
                )


def sweep_segments(runtime) -> None:
    """Cleanup: wipe iceoryx2 management state AND payload segments."""
    subprocess.run("rm -rf /tmp/iceoryx2 /dev/shm/iox2*", shell=True, check=False)
