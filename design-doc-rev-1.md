# koyu-runtime — Architecture Summary
### The post-bulldoze design, consolidated

The system that emerged from this redesign: **supervisord for process lifecycle,
iceoryx2 for all on-box IPC, a file contract between runtime and workspace, and
an agent-operable surface over all three.** NATS, the lease, the wrappers, the
heartbeat files, and the runtime→store imports are gone. Your original code
shrinks to roughly 250–300 lines of glue plus the services themselves.

The one-sentence philosophy: every layer is either battle-tested code you don't
own (supervisord, iceoryx2) or a thin seam you fully understand — and every
failure, anywhere, serializes to a string in a file the agent knows to read.

---

## 1. The two packages

**koyu-runtime** — supervision glue, `core/types`, `core/shm` (iceoryx2
bindings), the services (sim, commander, inference harness, recorder,
provenance, param_server, video_bridge), the spool writer, the agent CLI
surface. Imports nothing from workspace. Knows workspace only as opaque
strings (env-var IDs) and opaque paths (commands in services.yaml).

**koyu-workspace** — the store (functional library over a directory tree),
the ingester, cloud sync (push/pull/clone), training/eval orchestration, the
FastAPI app + React frontend. Deploys *into* runtime by writing services.yaml
stanzas and run directories; is never imported by it.

**The contract between them** — versioned schema files (or a zero-dep
`koyu-contract` package): the episode bundle layout, the `episode.json`
sidecar schema, the RecordingContext field set, ID conventions. Both sides
validate against it; neither owns the other. Bundle format is
LeRobot-compatible by design so ingest stays generic.

---

## 2. Supervision: supervisord + four glue pieces

Decision: **use supervisord as-is** (pip install; runs on macOS; 18 years of
process-management scar tissue) rather than extracting its ~450-line core.
The extraction remains a known, scoped option — the annotated core doc exists
if it's ever needed. The mental model gained from reading it is applied as
configuration:

- `stopasgroup=true`, `killasgroup=true` — group-kill, no orphaned children
- `autorestart=unexpected` + `exitcodes=0` — clean exits stay down
- `startsecs` — RUNNING is earned by surviving, kills flap-restart loops
- `startretries` → FATAL — give up loudly, never hammer forever
- two-phase stop: TERM, wait `stopwaitsecs`, KILL — stuck-forever impossible

### The four glue pieces (~250 lines total)

**yaml→INI generator (~80 lines).** services.yaml stays canonical; INI is a
build artifact the agent never edits. Generator stamps house defaults onto
every program and handles the known gotchas:
- `PYTHONUNBUFFERED=1` on every Python service (print() through a pipe block-buffers; logs would arrive minutes late or never)
- `directory=` + absolute `KOYU_HOME` (all commands are relative paths)
- escape `%` → `%%` unconditionally (supervisord %-expansion)
- always double-quote `environment=` values
- `oneshot: true` yaml flag → `startsecs=0, autorestart=false` (eval_runner; otherwise a fast successful exit reads as BACKOFF)
- `priority=` derived from the ipc graph: publishers start before subscribers

**Boot wrapper — `koyu up/down` (~50 lines).** Owns what supervisord can't
know: bump `boot_generation` → sweep `/tmp/iceoryx2` (sweeps happen
always-and-only at generation boundaries) → reconcile strays from the previous
boot (verify pid + start_time + env cookie; group-kill genuine strays; never
trust a bare PID) → start supervisord. Struct changes in `core/types` are by
definition a generation event.

**State listener (~50 lines).** An `[eventlistener:x]` subscribed to
PROCESS_STATE events writes `state.json` atomically on every transition.
Because it's fed by supervisord's change_state choke point, the file cannot
drift from supervisor belief. This is the agent's observed-state source —
readable even when the IPC layer is wedged (supervision must be debuggable
*below* the layer it supervises).

**Agent CLI shim (~50 lines).** JSON-out wrappers over supervisord's XML-RPC
(UDS): status, restart, reread+update. Shipped as `iox2-`-prefixed
subcommands so the agent has one CLI family for transport and supervision.

### Dynamic operations
- Agent adds a service: edit yaml → regenerate → `supervisorctl reread && update` — only the new group starts; running services untouched.
- Agent changes an env var: same flow — `ProcessConfig.__eq__` includes `environment`, so the diff marks exactly that group changed and update restarts only it. (Generator bakes literal values, not `%(ENV_X)s` refs, so every change is diffable.)
- Full fresh boot (struct changes, SHM schema changes): `koyu down && koyu up` — generation bump, sweep, respawn.

---

## 3. IPC: iceoryx2, four planes, NATS deleted

One transport, with each data shape on the pattern built for it. The decision
rule that assigns them: **how many readers, and does anyone need history?**

**pub/sub — streams.** Camera frames (`CameraFrame`), joint state
(`RobStrideState`/`RobStrideCommand`). Mechanics: loan/write/send into
refcounted fixed-size slots; per-subscriber buffers are offset-arrays, not
copies — zero-copy with per-consumer policies off one publisher:
- inference/controller: depth-1, overflow-drop → always-freshest
- recorder: deep buffer → every sample
- camera services: `history=1` so late-joining readers (agent frame-grabs) get the most recent frame instantly
- declare `max_subscribers` headroom (+2) for ephemeral CLI/agent readers
- pool sizing budget: publisher depth + Σ subscriber buffers + in-flight ≤ pool, or `loan()` starves — a slow reader with a deep buffer can stall the camera

**blackboard — current truth.** Latest-value state, many readers, no history:
params (the `global_config` canonical case — param_server shrinks to: own the
blackboard, persist to disk on change, ring `param_changed`), provenance
context (kills the `provenance.get` RPC — late joiners just read), per-service
telemetry (mode, spool depth, frames_buffered). Constraints accepted: keys
fixed at service creation (declared param schema = feature), values are
fixed-size structs (fixed-length char arrays or a max-size serialized buffer —
decided once in `core/types`).

**events — doorbells, not envelopes.** Event-id only, no payload, and events
**coalesce**: "this happened at least once since you last looked." The
universal idiom is *data-then-doorbell*: write the plane, ring the bell;
listeners read the plane. Uses: `recorder/control` verbs (start/stop/discard —
fire-and-forget is now *sufficient* because capture never rejects),
`param_changed`, `context_updated`, `episode_captured` (the data plane is the
filesystem — the bell says "sweep the outbox"), per-service heartbeats,
dead-node notifications.

**request-response — almost nothing.** The near-emptiness of this plane is
the sign the decomposition is right.

**The WaitSet** is each daemon's single reactor: listeners + per-service
heartbeat **deadlines** ("no beat in 3s" arrives as a deadline-miss) + tick
intervals, one loop. Heartbeats split correctly: liveness = bare event +
deadline (supervisor concern); telemetry = blackboard (agent/UI concern).

---

## 4. Data boundary: recorder → spool → ingester

**Recorder's contracted job:** capture buffers; on stop, write a bundle to a
tmp dir — `data.parquet`, mp4s, `episode.json` sidecar — then **atomic-rename
into `spool/outbox/`** and ring `episode_captured`. Zero `local_tool` imports,
zero store knowledge, zero refusal paths. If provenance is unreachable:
record anyway with context=unknown (raw data is sacred; ingester quarantines).
Discard = delete tmp. Crash mid-flush = no torn bundle ever visible.

**The sidecar** sorts metadata by who owns its truth:
- *capture facts* (runtime-owned, unreconstructable — capture everything in this category and nothing else): timestamps, length, achieved vs nominal Hz, start/stop, trigger source, per-source `frame_id` sequences (makes the tick-duplication KNOWN ISSUE downstream-detectable), runtime rev, boot generation, `schema_version`, `completed`
- *provenance tags* (carried opaquely, snapshotted at start): task, policy_name, source_project/run/checkpoint, collection_mode
- *filing intent* (requests, not resolutions): `requested_manifest` as a plain string — manifest_id is never written at capture; resolution is the workspace's verdict at ingest
- *curation signals* (the cheap Tesla-ism): `events: [{t, type: intervention|disagreement|eval_fail}]` — the highest-value mining signal, ten lines in the recorder

Outbox dirnames are `timestamp__manifest-name__shortuuid` — sortable and
human/agent-readable, but **cosmetic**: the sidecar is canonical; the ingester
never parses dirnames.

**The ingester** (a CLI verb or the FastAPI app — the store stays a functional
library; what changed is who may import it): sweep outbox → mint `ep_` ID →
resolve/create manifest (ensure_manifest logic moved in time, not deleted) →
move into store layout → `workspace.episode_ingested`. Idempotent (move-out-of-
outbox as the final act), lazy-able, re-runnable. **The spool is the system of
record; the store is a cache** — re-ingest can rebuild the store, fix filing
rules retroactively, re-tier quality as filters improve.

Costs accepted: eventual consistency (UI shows "captured, pending ingest" via
the recorder's spool-depth telemetry — visible lag is a state, invisible lag is
a mystery); unbounded spool if ingest is dead (spool depth is a monitored
condition). Workspace-side multi-writer discipline: funnel mutations through
one process or atomic-rename + flock on every registry write.

---

## 5. Agent surface

- **Desired state**: services.yaml (canonical, agent-editable; the ipc block doubles as the topology map and feeds priority derivation; services read their own ipc config from it via `SERVICE_NAME` — no JSON-in-env)
- **Observed state**: `state.json` (supervision) + blackboard telemetry + `iox2 service/node list --format json` (transport) — should-vs-is diffing is the foundation of every agent operation
- **Verbs**: `iox2 supervisor restart|status|update`, `iox2 service notify` (fire control events), `iox2-frame <service>` (subscribe, grab first sample via history=1, write PNG, exit — every read is late-open by design), `koyu ingest`, `koyu up/down`
- **Safety asymmetry**: reads free; writes staged through the user; hardware-moving actions user-initiated
- **Design rule**: failures must serialize. A service that can't start exits nonzero with a one-line reason → BACKOFF → spawnerr → state.json → the agent reads it. No silent failure modes anywhere in the loop.

---

## 6. Video & the RT future

**Video (now):** Rust `video_bridge` subscribes UI-resolution camera topics →
local WebSocket/HTTP to the browser. Same-machine, so no Zenoh/WebRTC stack —
but built behind a transport seam (frames in → serve out) so remote viewing
later swaps the out-side (WebRTC, which also brings FEC for teleop) without
touching anything else.

**Real-time islands (later, CAN/determinism):** the deterministic domain lives
inside one Rust process — SCHED_FIFO, mlockall, no allocation in the loop —
publishing/consuming over iceoryx2 at the edge (lock-free over pre-allocated
pools; the transport is allowed in a control loop). Cross-language via shared
`repr(C)` fixed-size types in one source of truth; a type change is a
generation event. Generator grows `chrt`/`taskset`/rtprio knobs. **Python is
quarantined**: never inside any loop with a deadline; holds samples briefly;
depth-1 where it only observes. Hard invariants can later sink further, into
MCU firmware (the comma/panda pattern: the supervisor never has to be perfect
if the invariant-enforcer sits below it).

---

## 7. Testing posture

- **Syscall/IPC seams from line one**: services talk to the OS and transport through injectable interfaces (the supervisord DummyOptions / comma spoofed-msgq trick) — racey conditions become constructed-state-plus-one-tick unit tests; time is data (store deadlines, inject `now`, monotonic only)
- **Errno/edge injection**: ESRCH, EAGAIN, stale SHM, pid-reuse-with-wrong-start-time as fake-OS test cases
- **Issue-driven e2e**: every real incident (orphan, stale segment, desync) becomes a fixture — a services.yaml, a kill -9, an assertion on state.json. Past pain is the highest-value suite owned
- **Process replay (the comma move)**: episodes recorded through the seam can be replayed deterministically through services; every episode is a potential regression test

---

## 8. The index card (invariants)

1. Every piece of state has one writer; every mutation flows through one choke point.
2. Failures become states, never bespoke exception paths — and every failure serializes to a readable string.
3. RUNNING is earned by surviving startsecs, never granted by spawning.
4. Sweeps happen always-and-only at generation boundaries.
5. Never trust a bare PID — verify start_time and the env cookie.
6. Lifecycle math on monotonic time only.
7. Events are doorbells; data lives on a plane. Coalescing is a feature, design for it.
8. Capture everything that cannot be reconstructed; record intent, not resolution.
9. The spool is the record; the store is a cache.
10. Python never stands in a loop with a deadline.
11. Shared types: one source, repr(C), fixed-size; changing them is a reboot.
12. The human owns the invariants; the agent owns the translation. The bedrock layer is hand-read even if agent-typed.

---

## 9. Deleted / Deferred / Verify

**Deleted:** NATS (server, clients, subjects), the lease, service wrappers,
heartbeat JSON files, `provenance.get` RPC, the manifest gate in the recorder,
all `local_tool` imports in runtime, most of the custom CLI.

**Deferred:** Zenoh (enters only when something crosses the machine boundary),
WebRTC/FEC (when remote teleop is real), distributed (discovery/time/partial-
failure is new work when it comes — the transport seam keeps it bolt-on-able),
hung-process auto-restart wiring (deadline-miss → supervisorctl restart).

**Verify before cutover (the load-bearing unknowns):** Python bindings
coverage for listener/WaitSet/deadline/blackboard/request-response; the
pub/sub `history` knob through the Python path; blackboard's read concurrency
model for larger values; `iox2` CLI request-response support.

**Migration order (stay bootable at every step):** (1) supervisord glue under
the existing services unchanged — NATS still running; (2) recorder→spool +
ingester — store imports leave runtime; (3) IPC cutover service-by-service:
params→blackboard, provenance→blackboard, control→events, heartbeats→
events+deadlines; (4) delete NATS; (5) retire the lease/wrappers, which steps
1–4 have made jobless.
