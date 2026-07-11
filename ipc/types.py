"""Shared ctypes structs for iceoryx2 IPC — the one source of truth for the wire
format. Ported from the old ``os/core/types.py``; the mechanism is unchanged.

Define one ``ctypes.Structure`` per SHM topic type. The Rust side carries a
``#[repr(C)]`` companion per crate plus a ``print_type_layout()`` that the
``checks.typecheck`` validator compares against ``layout()`` below.

Invariants every struct must respect
------------------------------------
1. Fixed size — segments are pre-sized at create; resizing invalidates them.
2. C layout — ``_fields_`` here matches ``#[repr(C)]`` on the Rust side exactly.
3. ``timestamp: c_double`` then ``frame_id: c_uint64`` first (staleness/dedup).
4. Big arrays are fixed-capacity; a width/height/length field marks the valid slice.

A Rust companion registered under a crate-qualified name overrides ``type_name``.
"""

from __future__ import annotations

import ctypes


NUM_JOINTS = 7


class RobStrideState(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("position", ctypes.c_double * NUM_JOINTS),
        ("velocity", ctypes.c_double * NUM_JOINTS),
        ("torque", ctypes.c_double * NUM_JOINTS),
        ("temperature", ctypes.c_double * NUM_JOINTS),
        ("enabled", ctypes.c_uint8 * NUM_JOINTS),
    ]


class RobStrideCommand(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("position", ctypes.c_double * NUM_JOINTS),
        ("velocity", ctypes.c_double * NUM_JOINTS),
        ("torque", ctypes.c_double * NUM_JOINTS),
    ]


class CameraFrame(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("channels", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("data", ctypes.c_uint8 * 921600),
    ]

    @classmethod
    def type_name(cls) -> str:
        return "camera_service::CameraFrame"


class GlobalConfig(ctypes.Structure):
    """Tunable params, published latest-value by param_server. Fields after the
    timestamp/frame_id header are the params (here: cursor speed)."""
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("speed", ctypes.c_double),
    ]


class RecorderTelemetry(ctypes.Structure):
    """Recorder's live state, published latest-value on ``recorder/telemetry``.
    ``capture_id`` is the 32-hex identity of the recording in progress — the
    SAME id the episode carries forever (ingest derives ep_<capture_id>). Any
    process (eval driver, rater UI) reads it here to address feedback. Empty
    while idle."""
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("state", ctypes.c_uint8),                     # 0 idle | 1 recording
        ("_pad", ctypes.c_uint8 * 7),
        ("frames", ctypes.c_uint64),                   # rows captured so far
        ("capture_id", ctypes.c_char * 33),            # 32-hex + NUL
    ]


class RecorderConfig(ctypes.Structure):
    """Data-recorder params, published latest-value by param_server.
    record_hz: requested recording rate; <= 0 records at the clock's native rate."""
    _fields_ = [
        ("timestamp", ctypes.c_double),
        ("frame_id", ctypes.c_uint64),
        ("record_hz", ctypes.c_double),
    ]


# ---------------------------------------------------------------------------
# Registry + introspection — what the validator and services resolve through.
# ---------------------------------------------------------------------------

class UnknownType(KeyError):
    """Raised when a services.yaml type name has no struct in this registry."""


def _structs() -> dict[str, type]:
    """All structs defined in this module, keyed by class name."""
    return {
        name: obj
        for name, obj in globals().items()
        if isinstance(obj, type)
        and issubclass(obj, ctypes.Structure)
        and obj.__module__ == __name__
    }


def type_name(struct: type) -> str:
    """The name the Rust side registers under (crate-qualified if overridden)."""
    fn = getattr(struct, "type_name", None)
    return fn() if callable(fn) else struct.__name__


def resolve(name: str) -> type:
    """Look up a struct by class name (or its crate-qualified ``type_name``)."""
    structs = _structs()
    if name in structs:
        return structs[name]
    for struct in structs.values():
        if type_name(struct) == name:
            return struct
    raise UnknownType(name)


def layout(struct: type) -> dict:
    """The struct's memory layout, in the same shape as the Rust ``--type-check``
    output once parsed: ``{"size": int, "fields": [(name, offset, size), ...]}``."""
    fields = []
    for fname, _ftype in struct._fields_:
        field = getattr(struct, fname)
        fields.append((fname, field.offset, field.size))
    return {"size": ctypes.sizeof(struct), "fields": fields}
