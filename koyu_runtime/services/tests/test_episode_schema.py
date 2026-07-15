import pytest
from pydantic import ValidationError

from koyu_runtime.services.episode_schema import SCHEMA_VERSION, EpisodeSidecar, RecordingContext


def test_context_roundtrip():
    ctx = RecordingContext(task="pick", requested_manifest="teleop-cubes", collection_mode="teleop")
    back = RecordingContext.model_validate_json(ctx.model_dump_json())
    assert back == ctx
    assert back.manifest_id is None


def test_empty_context_is_valid():
    # a missing/empty recording-context.json -> all-None (the "unfiled" case)
    ctx = RecordingContext.model_validate_json("{}")
    assert ctx.task is None and ctx.requested_manifest is None


def test_context_ignores_unknown_fields():
    ctx = RecordingContext.model_validate_json('{"task": "x", "bogus": 123}')
    assert ctx.task == "x"
    assert not hasattr(ctx, "bogus")


def test_sidecar_is_context_plus_capture_facts():
    ctx = RecordingContext(task="pick", requested_manifest="m", policy_name="p")
    sc = EpisodeSidecar(**ctx.model_dump(), length=300, fps=29.7,
                        features={"action": {"dtype": "float32", "shape": [7]}})
    # context rides along
    assert sc.task == "pick" and sc.requested_manifest == "m" and sc.policy_name == "p"
    # capture facts added
    assert sc.length == 300
    assert sc.fps == 29.7 and isinstance(sc.fps, float)
    assert sc.record_hz is None                 # nominal rate is optional (native capture)
    assert sc.schema_version == SCHEMA_VERSION
    assert sc.encoding == {}


def test_sidecar_mints_unique_capture_id():
    a = EpisodeSidecar(length=1, fps=30.0)
    b = EpisodeSidecar(length=1, fps=30.0)
    assert a.capture_id and b.capture_id and a.capture_id != b.capture_id


def test_sidecar_requires_length_and_fps():
    with pytest.raises(ValidationError):
        EpisodeSidecar(task="x")            # no length, no fps


def test_sidecar_excludes_store_owned_fields():
    # reward/events moved INTO the sidecar with the verdict inbox (AGENTS.md, law 6):
    # they are capture-time judgments now, not store-resolved metadata.
    dumped = EpisodeSidecar(length=1, fps=30.0).model_dump()
    for store_field in ("id", "manifest_ids", "files", "size_bytes"):
        assert store_field not in dumped
    assert "reward" in dumped and dumped["reward"] is None
    assert dumped["events"] == []


def test_sidecar_roundtrips_through_json():
    sc = EpisodeSidecar(requested_manifest="m", collection_mode="teleop",
                        length=2, fps=30.0, record_hz=23.0,
                        features={"a": {}}, encoding={"codec": "h264"})
    back = EpisodeSidecar.model_validate_json(sc.model_dump_json())
    assert back == sc
    assert back.recorded_at == sc.recorded_at
