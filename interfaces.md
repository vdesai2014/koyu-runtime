# koyu-runtime interfaces

*The seams external processes are allowed to touch. Everything else is
implementation. Additive evolution only: new cells, new inboxes, new fields —
never changed meanings.*

## The path law

Every runtime path derives from `$KOYU_RUNTIME_DIR` through the helpers in
`services/inbox.py`. **No process — service, CLI, or external — ever composes
`services/<name>/inbox` by hand, and the current working directory is never
consulted.**

```python
from services.inbox import Inbox, inbox_path
inbox_path(os.environ["KOYU_RUNTIME_DIR"], "data_recorder", "verdicts")
#  -> $KOYU_RUNTIME_DIR/services/data_recorder/inbox/verdicts/
```

## Episode identity (capture_id)

One identity per episode, minted **once, at recording start**, by the recorder:
`capture_id = uuid4().hex` (32 hex chars). It never changes:

```
recorder mints it ─→ sidecar episode.json (capture_id)
                  ─→ workspace ingest:  episode_id = "ep_" + capture_id
                  ─→ cloud:             same ep_… id (client-provided)
```

Anything that wants to reference "the episode being recorded right now" —
an eval driver, a rating UI, a foot pedal — reads it live from the telemetry
cell below. There is no other way to learn it, by design (single writer).

## `recorder/telemetry` — blackboard cell (read-only for everyone else)

Struct `RecorderTelemetry` (`ipc/types.py`): `timestamp`, `frame_id`,
`state` (0 idle · 1 recording), `frames` (rows captured so far),
`capture_id` (32-hex, empty while idle). Latest-value semantics; open it
lazily and read whenever — e.g.:

```python
from ipc import blackboard
from ipc.types import RecorderTelemetry
cell = blackboard.Reader("recorder/telemetry", RecorderTelemetry).read()
capture_id = cell.capture_id.decode()          # "" -> nothing recording
```

## Verdict inbox — how judgments reach an episode

A *verdict* is a judgment about an episode (success, score, notable moments).
It is not a capture fact — the recorder records what happened; a judge decides
what it was worth. Judges submit files:

```python
from services.inbox import Inbox, inbox_path
Inbox(inbox_path(runtime_dir, "data_recorder", "verdicts")).submit({
    "capture_id": capture_id,                  # from recorder/telemetry
    "reward": 1.0,                             # 0..1 episode outcome
    "events": [{"t": 1712345678901234567, "type": "intervention"}],  # optional
})
```

Rules:

1. **Submit before ringing `stop`.** The recorder drains the inbox when the
   stop lands; a matching verdict merges into the sidecar (`reward`,
   `events`), and flows to the workspace/cloud episode untouched.
2. **Match-or-quarantine.** A verdict whose `capture_id` doesn't name the
   capture being finalized is parked in
   `…/data_recorder/quarantine/` with a reason — kept, loud, never silently
   dropped. Duplicates: first wins, rest quarantined.
3. **Late is a different path.** Rating an episode after it landed is a
   workspace/cloud `PATCH` on the episode's `reward` — not this inbox.

## `recorder/control` — event channel (verbs)

Payload-less doorbells: `1 START · 2 STOP · 3 DISCARD` in; the recorder rings
`recorder/episode` back (`1 CAPTURED · 2 DISCARDED · 3 FAILED`). Context
(task, provenance tags, requested manifest) is snapshotted at START from
`$KOYU_RUNTIME_DIR/recording-context.json` — write it before ringing start.

## The outbox — the data seam

Finalized bundles atomic-rename into `$KOYU_RUNTIME_DIR/data-recordings/`.
Format: see koyu.dev/docs/format. The sidecar is canonical; dirnames are
cosmetic. Consumers (the workspace ingester) sweep and move; re-ingest is
idempotent.

## Building robot UIs on the bridge — the four data primitives

The bridge (`services/bridge.py`, WS on `:8765/ws` + HTTP) is the browser's
window into the runtime. It is deliberately a *plumbing* layer, not a widget
framework: every robot's control surface is different, so pages are written
(usually by your agent) against four primitives that never change. Topic
struct types resolve server-side from services.yaml ipc blocks — declare a
topic there or the bridge won't know it. No schemas live client-side.

**1. Live telemetry** — latest value of any blackboard cell or stream:

```json
→ {"type": "subscribe-topic", "topic": "arm/state", "rate_hz": 10}
← {"type": "topic-data", "topic": "arm/state", "timestamp": …,
   "frame_id": …, "values": { …struct fields as JSON… }}
```

Sent only when `frame_id` changes; big byte fields (camera data) are dropped
from the JSON automatically.

**2. Time series** — the same subscription. The bridge is latest-value by
design; history belongs to the consumer. Accumulate samples into a ring
buffer client-side and plot from that.

**3. Video** — HTTP, not WS:

```
GET /mjpeg/<topic>?fps=30    multipart/x-mixed-replace push stream; renders
                             in a plain <img>; one connection per viewer
GET /frame/<topic>           single JPEG snapshot (UI thumbnails, poll fallbacks)
```

Feeds stream while the publisher publishes; late joiners get the cached
last frame. These endpoints are for browser UIs. A coding agent that wants
eyes on the robot uses the CLI convention instead: `koyu frame <topic>`
prints the path of the newest logged frame (from the ipc_logger flight
recorder), ready to open with a file Read.

**4. Verbs** — payload-less doorbells and params:

```json
→ {"type": "ring-event",   "channel": "arm/control", "event_id": 2}
→ {"type": "listen-event", "channel": "arm/events"}
← {"type": "event",        "channel": "arm/events", "event_id": 1}
→ {"type": "set-param", "topic": "arm/config", "key": "speed",
   "value": 0.5, "persist": true}
```

**Liveness rules** (each one paid for): connection state is not data state —
a connected page may have nothing publishing, so every widget needs an empty
state. There is no history for late joiners — a fresh subscription sees the
next sample, not the last (cameras excepted, via the bridge frame cache).
Publishers pause and feeds pause with them — widgets idle, they don't error.
The reference client for all four primitives is the workspace frontend's
`src/lib/useBridge.ts`.
