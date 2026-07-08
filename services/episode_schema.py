"""Episode metadata contract — shared by the recorder (writes) and the ingester (reads).

``RecordingContext`` is the user/agent-set context, persisted as recording-context.json
in the runtime dir. ``EpisodeSidecar`` extends it with the facts the recorder captures
and is written as each episode's episode.json. The ingester resolves the manifest, mints
the episode id, hashes the files, and turns this into the store's episode — none of which
the recorder does, so none of that lives here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RecordingContext(BaseModel):
    """The settable recording context (recording-context.json), written by the CLI/browser."""

    model_config = ConfigDict(extra="ignore")

    task: str | None = None
    task_description: str | None = None
    requested_manifest: str | None = None    # manifest NAME — the filing intent
    manifest_id: str | None = None           # carried only when already known
    collection_mode: str | None = None       # = manifest type (teleop | eval | ...)
    source_project_id: str | None = None
    source_run_id: str | None = None
    source_checkpoint: str | None = None
    policy_name: str | None = None


class EpisodeSidecar(RecordingContext):
    """One episode's episode.json: the context above plus what the recorder captured."""

    schema_version: int = SCHEMA_VERSION
    capture_id: str = Field(default_factory=lambda: uuid4().hex)  # unique per capture; the ingester's idempotency key
    recorded_at: datetime = Field(default_factory=_utc_now)
    length: int                              # number of frames / rows
    record_hz: float | None = None           # nominal requested rate; None = native clock rate
    fps: float                               # measured landed rate (not the requested param)
    features: dict = Field(default_factory=dict)
    encoding: dict = Field(default_factory=dict)
