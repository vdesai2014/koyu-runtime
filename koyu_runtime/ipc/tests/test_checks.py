import os
import stat
import textwrap

import pytest

from koyu_runtime.ipc import checks, types


def write_services(tmp_path, text):
    p = tmp_path / "services.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def layout_lines(name, struct):
    """Render a struct's layout exactly as Rust print_type_layout() would."""
    lay = types.layout(struct)
    lines = [f"TYPE {name} SIZE {lay['size']}"]
    lines += [f"FIELD {n} OFFSET {o} SIZE {s}" for n, o, s in lay["fields"]]
    return "\n".join(lines)


def fake_binary(tmp_path, output, name="fakebin"):
    """A non-python executable that echoes `output` (ignores --type-check)."""
    p = tmp_path / name
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{output}\nEOF\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


# --- parse_layout -----------------------------------------------------------

def test_parse_layout_roundtrips():
    text = layout_lines("CameraFrame", types.CameraFrame)
    parsed = checks.parse_layout(text)
    assert parsed["CameraFrame"] == types.layout(types.CameraFrame)


# --- _typed_topics (schema-agnostic) ----------------------------------------

def test_typed_topics_flat_and_grouped():
    flat = {"publishes": {"a": "CameraFrame"}, "subscribes": {"b": "RobStrideCommand"}}
    assert checks._typed_topics(flat) == {"a": "CameraFrame", "b": "RobStrideCommand"}
    grouped = {
        "blackboard": {"writes": {"s": "RobStrideState"}},
        "streams": {"publishes": {"c": {"type": "CameraFrame", "max_buffer": 90}}},
    }
    assert checks._typed_topics(grouped) == {"s": "RobStrideState", "c": "CameraFrame"}


def test_typed_topics_topic_named_like_direction():
    # a topic literally named 'writes' must not crash, and must not hallucinate
    assert checks._typed_topics({"writes": {"writes": "CameraFrame"}}) == {"writes": "CameraFrame"}


def test_typed_topics_no_phantom_type_topic():
    # {writes: {writes: {type: CameraFrame}}} must yield ONE real topic, no phantom 'type'
    assert checks._typed_topics({"writes": {"writes": {"type": "CameraFrame"}}}) == {"writes": "CameraFrame"}


def test_is_native_sees_python_through_wrappers():
    assert checks._is_native(["./video-bridge"]) is True
    assert checks._is_native(["python3", "-m", "services.commander"]) is False
    assert checks._is_native(["/usr/bin/env", "python", "svc.py"]) is False
    assert checks._is_native(["poetry", "run", "python", "svc.py"]) is False


# --- typecheck: name resolution (python services) ---------------------------

def test_typecheck_passes_for_known_types(tmp_path):
    sy = write_services(tmp_path, """
        commander:
          cmd: [python3, commander.py]
          ipc:
            streams:
              publishes: { sim_robot/desired: RobStrideCommand }
              subscribes: { inference/desired: { type: RobStrideCommand, buffer: 1 } }
    """)
    checks.typecheck(sy)            # no raise


def test_typecheck_rejects_unknown_type(tmp_path):
    sy = write_services(tmp_path, """
        commander:
          cmd: [python3, commander.py]
          ipc:
            streams:
              publishes: { sim_robot/desired: WobblyCommand }
    """)
    with pytest.raises(checks.TypeCheckError, match="unknown type 'WobblyCommand'"):
        checks.typecheck(sy)


def test_typecheck_ignores_services_without_ipc(tmp_path):
    sy = write_services(tmp_path, "logger:\n  cmd: [python3, logger.py]\n")
    checks.typecheck(sy)            # no raise


def test_typecheck_skips_native_with_no_typed_topics(tmp_path):
    # a native, event-only service must NOT have --type-check run on it
    failing = tmp_path / "failbin"
    failing.write_text("#!/bin/sh\nexit 1\n")    # would abort the boot if executed
    failing.chmod(0o755)
    sy = write_services(tmp_path, f"""
        evttool:
          cmd: [{failing}]
          ipc:
            events:
              listens: [ some/channel ]
    """)
    checks.typecheck(sy)            # no raise — no typed topics, binary never run


# --- typecheck: native layout comparison ------------------------------------

def test_typecheck_native_layout_matches(tmp_path):
    binary = fake_binary(tmp_path, layout_lines("CameraFrame", types.CameraFrame))
    sy = write_services(tmp_path, f"""
        camera:
          cmd: [{binary}]
          ipc:
            streams:
              publishes: {{ camera/rgb: {{ type: CameraFrame }} }}
    """)
    checks.typecheck(sy)            # binary layout == python layout -> ok


def test_typecheck_native_matches_crate_qualified_name(tmp_path):
    # binary prints the fully-qualified name; services.yaml uses the short one
    binary = fake_binary(tmp_path, layout_lines("camera_service::CameraFrame", types.CameraFrame))
    sy = write_services(tmp_path, f"""
        camera:
          cmd: [{binary}]
          ipc:
            streams:
              publishes: {{ camera/rgb: {{ type: CameraFrame }} }}
    """)
    checks.typecheck(sy)            # robust name match -> ok


def test_typecheck_tolerates_string_cmd(tmp_path):
    binary = fake_binary(tmp_path, layout_lines("CameraFrame", types.CameraFrame))
    sy = write_services(tmp_path, f"""
        camera:
          cmd: {binary}
          ipc:
            streams:
              publishes: {{ camera/rgb: {{ type: CameraFrame }} }}
    """)
    checks.typecheck(sy)            # unquoted string command shlex-split, not shattered


def test_typecheck_native_layout_mismatch_raises(tmp_path):
    real_size = types.layout(types.CameraFrame)["size"]
    bad = layout_lines("CameraFrame", types.CameraFrame).replace(
        f"SIZE {real_size}", "SIZE 999", 1
    )
    binary = fake_binary(tmp_path, bad)
    sy = write_services(tmp_path, f"""
        camera:
          cmd: [{binary}]
          ipc:
            streams:
              publishes: {{ camera/rgb: {{ type: CameraFrame }} }}
    """)
    with pytest.raises(checks.TypeCheckError, match="layout mismatch"):
        checks.typecheck(sy)
