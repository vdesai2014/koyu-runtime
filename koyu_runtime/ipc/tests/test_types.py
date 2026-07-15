import ctypes

import pytest

from koyu_runtime.ipc import types


def test_resolve_known_struct():
    assert types.resolve("CameraFrame") is types.CameraFrame
    assert types.resolve("RobStrideCommand") is types.RobStrideCommand


def test_resolve_via_crate_qualified_name():
    # CameraFrame overrides type_name() to the Rust crate-qualified form.
    assert types.resolve("camera_service::CameraFrame") is types.CameraFrame


def test_resolve_unknown_raises():
    with pytest.raises(types.UnknownType):
        types.resolve("NopeFrame")


def test_layout_shape_matches_fields():
    lay = types.layout(types.RobStrideCommand)
    assert lay["size"] == ctypes.sizeof(types.RobStrideCommand)
    names = [name for name, _, _ in lay["fields"]]
    assert names == [n for n, _ in types.RobStrideCommand._fields_]
    # first field is timestamp at offset 0
    assert lay["fields"][0] == ("timestamp", 0, 8)


def test_layout_offsets_are_real():
    lay = types.layout(types.CameraFrame)
    by_name = {n: (o, s) for n, o, s in lay["fields"]}
    assert by_name["frame_id"] == (8, 8)            # right after the c_double timestamp
    assert by_name["data"][1] == 921600             # the fixed RGB capacity
