"""data recorder — clock-gated capture into the outbox.

The slowest camera is the clock. Each new clock frame becomes at most one row,
so row i <-> video frame i by construction: no duplicated frames, no offline
alignment. Every other source is latest-value sampled (depth-1 subscriber,
blackboard semantics) at each kept frame and must stamp within one period (+
jitter) of it — a stale source skips frames at the leading edge (sources start
asymmetrically) and aborts the episode once rows exist, so a bad recording
fails at capture time, not at finalize. All sources stamp from one wall clock

(``time.time()``); a backwards clock step aborts loudly.

A ``record_hz`` below the clock rate keeps frames on an anchored time grid
(see ``gate``). It is a param-server blackboard value snapshotted at start;
the sidecar records it (nominal) next to the measured landed ``fps``.

Layers: the pure capture helpers (gate / tol_ns), the bundle writer
(parquet / mp4 / episode.json), and the IPC service shell on top.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from shutil import rmtree
from typing import Any, Callable
from uuid import uuid4

from pydantic import ValidationError

from ipc import types
from ipc.service import Service

from .episode_schema import EpisodeSidecar, RecordingContext
from .inbox import Inbox, inbox_path

JITTER_NS = 5_000_000     # staleness slack on top of one source period
TICK_HZ = 200             # clock poll cadence (Hz), far above any camera rate
CLOCK_BUFFER = 8          # clock frames queued between ticks (absorbs tick jitter)
CLOCK_TIMEOUT_S = 1.0     # no clock frame for this long while recording -> abort
MAX_FRAMES = 1_800        # whole-episode row cap (~60s @ 30fps); auto-stop, never discard
ENCODING = {"video_codec": "libx264", "pix_fmt": "yuv420p"}

# recorder/control — the frontend rings these
CTL_START, CTL_STOP, CTL_DISCARD = 1, 2, 3
# recorder/episode — the recorder rings these so the frontend can react
EP_CAPTURED, EP_DISCARDED, EP_FAILED = 1, 2, 3


@dataclass(frozen=True)
class Source:
    topic: str
    feature: str               # LeRobot-style key; becomes a parquet column or mp4 name
    extract: Callable[[Any], Any]
    schema: dict
    kind: str                  # "video" | "column"
    rate_hz: float             # declared rate -> staleness tolerance + clock choice
    type_name: str = ""        # struct class name for the subscription (e.g. "CameraFrame")
    paired: bool = False       # pair by frame_id == clock frame_id (lockstep answers,
                               # e.g. eval actions) instead of timestamp window; a
                               # missing match means "still cooking" -> the clock
                               # frame is DEFERRED, never dropped and never an abort


def tol_ns(s: Source) -> int:
    """Staleness bound vs the clock frame: latest-value sampling only ever sees
    samples from *before* the poll, so a healthy source can be a full period old
    (not half, as with nearest-match, which looks on both sides)."""
    return int(1e9 / s.rate_hz) + JITTER_NS


# --- pure capture helpers ----------------------------------------------------

def gate(ts: int, next_due: int, period: int) -> tuple[bool, int]:
    """Anchored-grid decimation: keep a clock frame iff it has reached the grid.

    The grid advances by one period from the DUE time, not the frame time, so
    per-frame lateness doesn't compound into rate undershoot. A frame already
    past the next slot means the clock stalled: re-anchor on it rather than
    bursting through the backlog at native rate. period <= 0 keeps everything.
    """
    if period <= 0:
        return True, 0
    if ts < next_due:
        return False, next_due
    nxt = next_due + period
    if ts >= nxt:
        nxt = ts + period
    return True, nxt


# --- bundle writing ----------------------------------------------------------

@dataclass
class Outcome:
    """Result of a finalize: a written bundle, or a discard with a reason."""
    path: Path | None = None
    reason: str | None = None      # set when discarded (path is None)


def finalize(recordings_dir: Path, ctx: RecordingContext, rows: list,
             sources: list[Source], record_hz: float = 0.0,
             capture_id: str | None = None, verdict: dict | None = None) -> Outcome:
    """Write one episode bundle from captured rows of ``(ts_ns, {feature: value})``.

    Rows arrive already aligned (capture is clock-gated), so this is just the
    disk format: parquet for columns, one mp4 per video, episode.json sidecar,
    committed by a single atomic rename.
    """
    if not rows:
        return Outcome(reason="no frames")
    ts = [t for t, _ in rows]
    n = len(ts)
    # measured landed fps from real timestamps; nominal is the lone-frame fallback
    fps = (n - 1) / ((ts[-1] - ts[0]) / 1e9) if n > 1 else (record_hz or 30.0)

    tmp = recordings_dir / f".tmp-{uuid4().hex}"
    try:
        (tmp / "videos").mkdir(parents=True)
        _write_parquet(tmp / "data.parquet", ts,
                       {s.feature: [r[s.feature] for _, r in rows]
                        for s in sources if s.kind == "column"})
        for s in sources:
            if s.kind == "video":
                _encode_mp4(tmp / "videos" / _vid(s.feature),
                            [r[s.feature] for _, r in rows], fps)

        sidecar = EpisodeSidecar(**ctx.model_dump(), length=n, fps=fps,
                                 record_hz=record_hz or None,
                                 features=_features(sources, rows[0][1]), encoding=ENCODING,
                                 **({"capture_id": capture_id} if capture_id else {}),
                                 **({k: verdict[k] for k in ("reward", "events") if k in verdict}
                                    if verdict else {}))
        (tmp / "episode.json").write_text(sidecar.model_dump_json(indent=2))

        final = recordings_dir / f"{_dirname(ts[0])}__{ctx.requested_manifest or 'unfiled'}__{sidecar.capture_id[:8]}"
        tmp.rename(final)                                      # atomic commit, same filesystem
        return Outcome(path=final)
    except BaseException:
        rmtree(tmp, ignore_errors=True)                       # no orphan .tmp on failure
        raise


def _vid(feature: str) -> str:
    return f"{feature.split('.')[-1]}.mp4"


def _dirname(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y%m%dT%H%M%S")


def _features(sources: list[Source], first_row: dict) -> dict:
    feats: dict = {}
    for s in sources:
        if s.kind == "video":
            im = first_row[s.feature]                          # refine shape from a real frame
            feats[s.feature] = {"dtype": "video", "shape": [im.shape[0], im.shape[1], 3]}
        else:
            feats[s.feature] = dict(s.schema)
    return feats


def _write_parquet(path: Path, ts: list[int], columns: dict[str, list]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = {
        "step": pa.array(range(len(ts)), type=pa.int64()),
        "timestamp": pa.array(ts, type=pa.int64()),
    }
    for feature, values in columns.items():
        table[feature] = pa.array(values, type=pa.list_(pa.float32()))   # column = float32 vector
    pq.write_table(pa.table(table), str(path))


def _encode_mp4(path: Path, frames: list, fps: float) -> None:
    import av

    with av.open(str(path), mode="w") as container:          # closed/flushed even if encode raises
        stream = container.add_stream("libx264", rate=Fraction(fps).limit_denominator(1000))
        stream.height, stream.width = frames[0].shape[0], frames[0].shape[1]
        stream.pix_fmt = "yuv420p"
        for img in frames:
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():                       # flush
            container.mux(packet)


def _validate_sources(sources: list[Source]) -> None:
    """Config checks at startup, so a misconfigured recorder fails loud, not mid-episode."""
    videos = [s for s in sources if s.kind == "video"]
    if not videos:
        raise ValueError("data_recorder: needs at least one video source as the clock")
    for s in sources:
        if not s.type_name:
            raise ValueError(f"data_recorder: source {s.topic!r} needs a type_name")
        if s.rate_hz <= 0:
            raise ValueError(f"data_recorder: source {s.topic!r} needs a positive rate_hz")
        if s.kind == "column" and s.schema.get("dtype") != "float32":
            raise ValueError(
                f"data_recorder: column {s.feature!r} must declare dtype float32 "
                "(stored as list<float32>)"
            )
    names = [_vid(s.feature) for s in videos]
    if len(names) != len(set(names)):
        raise ValueError(f"data_recorder: video filename collision: {names}")


# --- the service shell -------------------------------------------------------

class DataRecorder(Service):
    """Clock-gated recorder. ``on_event()`` and ``on_tick()`` run on the WaitSet's
    single thread (never concurrently), so capture state needs no lock. Finalize
    runs on the worker and the rows are handed off on stop — a new episode may
    start while the previous one is still encoding."""

    def __init__(self, home: str, sources: list[Source]):
        self.home = Path(home)
        self.recordings = self.home / "data-recordings"
        self.context_file = self.home / "recording-context.json"
        self.sources = sources
        videos = [s for s in sources if s.kind == "video"]
        self.clock = min(videos, key=lambda s: s.rate_hz) if videos else None
        self.others = [s for s in sources if s is not self.clock]
        self.state = "idle"                            # idle | recording
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending: list[Future] = []                # finalize jobs still encoding
        self._reset()
        super().__init__("data_recorder")

    def _reset(self) -> None:
        self.capture_id = ""                          # minted at START; "" = idle
        self.deferred: list = []                      # clock frames awaiting a paired match
        self.rows: list[tuple[int, dict]] = []         # (clock ts_ns, {feature: value})
        self.cache: dict[str, Any] = {}                # topic -> latest sample (non-clock)
        self.snap: RecordingContext | None = None
        self.rec_hz = 0.0                              # snapshotted record_hz; 0 = native
        self.period_ns = 0
        self.next_due = 0                              # decimation grid cursor (gate())
        self.last_ts = 0                               # last seen clock stamp (backwards check)
        self.t0_ns = 0                                 # start fence
        self.clock_seen = 0.0                          # monotonic time of the last clock frame

    def setup(self) -> None:
        _validate_sources(self.sources)
        self.recordings.mkdir(exist_ok=True)
        _sweep_tmp(self.recordings)                    # crashed runs leave .tmp-* behind
        # the clock gets a small queue so a late tick can't drop a frame; the
        # others are depth-1 latest-value reads (blackboard semantics over pub/sub).
        #
        # TODO(multi-buffer capture): non-clock sources at buffer=1 are sampled
        # onto the clock spine — samples faster than the clock are DECIMATED by
        # design. Lossless capture of faster-than-clock sources (e.g. 500 Hz
        # torque under a 30 Hz camera clock) needs (a) drained multi-buffer
        # subscriptions here AND (b) a format decision for sub-clock timelines
        # (nested lists per row vs. separate timestamped columns). Also budget
        # publisher buffer ceilings for deep-history subscribers before raising
        # any buffer here — see the pool-sizing rule in design-doc §3.
        #
        # TODO(adversarial sync suite): constructed-state tests for the pairing
        # logic under hostile timing — desynced cameras (constant offset, slow
        # drift), mismatched frame rates (30 vs 29.97 vs 60), a source that
        # stalls mid-episode then bursts, clock jitter at the tolerance edge,
        # and lockstep actions arriving 0/1/2 ticks late (paired-mode deferral).
        # Each case asserts either exact rows or a loud abort — never silent
        # misalignment. Time is data: inject timestamps, no sleeps.
        self.subs = {
            s.topic: self.subscriber(s.topic, types.resolve(s.type_name),
                                     buffer=CLOCK_BUFFER if s is self.clock else 1)
            for s in self.sources
        }
        self.config = self.reader("recorder/config", types.RecorderConfig)
        # identity + liveness for external processes (eval drivers, rater UIs):
        # this capture_id IS the episode identity (ingest derives ep_<capture_id>)
        self.telemetry = self.writer("recorder/telemetry", types.RecorderTelemetry)
        self.verdicts = Inbox(inbox_path(self.home, "data_recorder", "verdicts"))
        self.on("recorder/control")
        self.episode = self.notifier("recorder/episode")
        self.tick(TICK_HZ)

    def on_event(self, channel: str, event_id: int) -> None:
        if channel != "recorder/control":
            return
        if event_id == CTL_START:
            if self.state != "idle":
                print(f"[data_recorder] start ignored: state={self.state}", flush=True)
                return
            self._reset()
            self.snap = _read_context(self.context_file)
            self.rec_hz = self._read_record_hz()
            self.period_ns = int(1e9 / self.rec_hz) if self.rec_hz else 0
            self.t0_ns = time.time_ns()                # fence: ignore frames queued while idle
            self.clock_seen = time.monotonic()
            self.capture_id = uuid4().hex              # THE episode identity, minted now
            self.state = "recording"
            self._telemetry()
        elif event_id == CTL_DISCARD and self.state == "recording":
            self._drain_verdicts(None)                 # discard -> park any verdicts, loudly
            self._reset()
            self.state = "idle"
            self._telemetry()
        elif event_id == CTL_STOP and self.state == "recording":
            self._capture()                            # catch the clock queue's tail
            if self.state == "recording":              # the tail can still abort
                self._submit_finalize()

    def on_tick(self) -> None:
        self._collect()                                # ring results of finished jobs
        if self.state != "recording":
            return
        self._capture()
        if self.state != "recording":                  # capture may have aborted
            return
        self._telemetry()
        if time.monotonic() - self.clock_seen > CLOCK_TIMEOUT_S:
            self._abort(f"clock {self.clock.topic!r} silent for {CLOCK_TIMEOUT_S}s")
        elif len(self.rows) >= MAX_FRAMES:
            print("[data_recorder] auto-stop: MAX_FRAMES", flush=True)
            self._submit_finalize()

    def _read_record_hz(self) -> float:
        """Snapshot the requested rate from the param blackboard; 0 = native rate.
        At or above the clock's rate it can't be honored (frames can't be
        invented), so fall back to native, loudly. No param server -> native."""
        cfg = self.config.read()
        hz = float(cfg.record_hz) if cfg is not None else 0.0
        if hz >= self.clock.rate_hz:
            print(f"[data_recorder] record_hz={hz:g} >= clock rate "
                  f"{self.clock.rate_hz:g}; recording at native rate", flush=True)
            return 0.0
        return max(hz, 0.0)

    def _capture(self) -> None:
        for s in self.others:                          # refresh the latest-value cache
            if (sample := self.subs[s.topic].latest()) is not None:
                self.cache[s.topic] = sample
        frames = self.subs[self.clock.topic].drain()
        if frames:
            self.clock_seen = time.monotonic()         # liveness, even for unkept frames
        self.deferred.extend(frames)
        while self.deferred and self.state == "recording":
            if self._frame(self.deferred[0]):          # consumed (recorded/gated/abort)
                self.deferred.pop(0)
            else:                                       # paired wait: keep, retry next tick
                if len(self.deferred) > CLOCK_BUFFER:
                    self._abort("paired source never matched "
                                f"frame {self.deferred[0].frame_id}")
                break

    def _frame(self, frame) -> bool:
        """Process one clock frame. Returns True if the frame was consumed
        (recorded, gated out, fenced, or aborted) and False for a paired-mode
        wait — the caller keeps the frame and retries next tick."""
        # paired sources first: an unmatched answer means "still cooking", and
        # the frame must not pass the fence/gate until it can actually land
        for s in self.others:
            if s.paired:
                c = self.cache.get(s.topic)
                if c is None or getattr(c, "frame_id", None) != getattr(frame, "frame_id", None):
                    return False
        ts = int(frame.timestamp * 1e9)                # wall-clock seconds -> ns
        if ts < self.t0_ns:
            return True                                # queued while idle
        if ts < self.last_ts:
            self._abort("clock went backwards (NTP step?)")
            return True
        self.last_ts = ts
        keep, self.next_due = gate(ts, self.next_due, self.period_ns)
        if not keep:
            return True
        for s in self.others:                          # window sources fresh at this frame?
            if s.paired:
                continue                               # already matched above, exactly
            c = self.cache.get(s.topic)
            if c is None or abs(int(c.timestamp * 1e9) - ts) > tol_ns(s):
                if self.rows:
                    self._abort(f"{s.topic!r} stale at t={ts}")   # mid-episode drift
                return True                            # leading edge: wait for the source
        self.rows.append((ts, {
            self.clock.feature: self.clock.extract(frame),
            **{s.feature: s.extract(self.cache[s.topic]) for s in self.others},
        }))
        return True

    def _abort(self, reason: str) -> None:
        print(f"[data_recorder] recording aborted: {reason}", flush=True)
        self._drain_verdicts(None)
        self._reset()
        self.state = "idle"
        self._telemetry()
        self.episode.ring(EP_FAILED)

    def _submit_finalize(self) -> None:
        verdict = self._drain_verdicts(self.capture_id)
        self.pending.append(self.pool.submit(
            finalize, self.recordings, self.snap, self.rows, self.sources, self.rec_hz,
            self.capture_id, verdict))
        self._reset()                                  # hand off; ready for the next start now
        self.state = "idle"
        self._telemetry()

    def _drain_verdicts(self, capture_id: str | None) -> dict | None:
        """Match-or-quarantine: a verdict either names the capture being
        finalized, or it is parked loudly. Submit verdicts BEFORE ringing stop;
        anything later belongs to the post-hoc path (workspace/cloud PATCH)."""
        matched = None
        for req in self.verdicts.drain():
            if capture_id and req.get("capture_id") == capture_id and matched is None:
                matched = req
            elif capture_id and req.get("capture_id") == capture_id:
                self.verdicts.quarantine(req, f"duplicate verdict for {capture_id}")
            else:
                self.verdicts.quarantine(
                    req, f"capture_id mismatch (current: {capture_id or 'none'})")
        return matched

    def _telemetry(self) -> None:
        cell = types.RecorderTelemetry(
            timestamp=time.time(), frame_id=0,
            state=1 if self.state == "recording" else 0,
            frames=len(self.rows), capture_id=self.capture_id.encode())
        self.telemetry.write(cell)

    def _collect(self) -> None:
        still: list[Future] = []
        for fut in self.pending:
            if not fut.done():
                still.append(fut)
                continue
            try:
                out = fut.result()
                if out.path is not None:
                    self.episode.ring(EP_CAPTURED)
                else:
                    print(f"[data_recorder] discarded: {out.reason}", flush=True)
                    self.episode.ring(EP_DISCARDED)
            except Exception as exc:
                print(f"[data_recorder] finalize failed: {exc!r}", flush=True)
                self.episode.ring(EP_FAILED)
        self.pending = still


def _read_context(path: Path) -> RecordingContext:
    try:
        return RecordingContext.model_validate_json(path.read_text())
    except (FileNotFoundError, ValidationError):
        return RecordingContext()                      # unfiled — record anyway; ingester quarantines


def _sweep_tmp(recordings: Path) -> None:
    for d in recordings.glob(".tmp-*"):
        rmtree(d, ignore_errors=True)


# Populate for your robot: one Source per feature you want in the episode.
# The slowest kind="video" entry is the clock.
SOURCES: list[Source] = []


def main() -> None:
    home = os.environ["KOYU_RUNTIME_DIR"]
    DataRecorder(home, SOURCES).run()


if __name__ == "__main__":
    main()
