# The koyu-runtime manual

Operational manual for coding agents working on a koyu runtime. Laws apply
to every task. Routes are recipes for common tasks, and they cite laws where
the laws bite. The reasoning behind all of it lives in
[docs/philosophy.md](docs/philosophy.md).

A runtime is a directory: a `services.yaml` describing supervised processes,
plus the state the runtime generates beneath it. Code lives in this repo. A
robot's identity lives in its runtime directory.

## Laws

1. **Every runtime path derives from `$KOYU_RUNTIME_DIR`** through the
   `koyu_runtime.services.inbox` helpers. Hand-composed inbox paths
   fragmented the inbox mechanism once already; the helpers exist so that
   stays impossible.
2. **Target runtimes explicitly.** Pass `-r <dir>` or export
   `KOYU_RUNTIME`. Shells reset their cwd and machines host multiple
   runtime directories, so ambient resolution eventually finds the wrong
   one.
3. **One writer per topic.** Each blackboard cell and each stream has
   exactly one owning process.
4. **Frozen seams evolve additively.** New cells, new fields, and new
   inboxes are welcome; changed meanings never are. A struct layout change
   is a generation event: bring the runtime down and up.
5. **Declare every topic and every event channel** in the owning service's
   `ipc:` block. Declaration buys the boot typecheck, bridge access, and
   flight recording. An undeclared doorbell is invisible to `koyu tail`.
6. **Verdicts land before STOP.** Submit to the recorder's verdict inbox,
   then ring stop. Anything later goes through the workspace `PATCH`,
   never the inbox.
7. **You own the recording context.** Before any recording starts, ensure
   `recording-context.json` says what the data is and where it came from,
   and confirm with `koyu context show`. Ingest carries whatever the
   sidecar holds, so context set at record time flows to the workspace and
   cloud automatically, while unfiled data means hand repair and lost
   provenance means the lineage is gone for good.
8. **One live runtime per machine.** iceoryx2 shared memory is
   machine-global, so a second live runtime collides with the first.
9. **Bring the runtime down before running the IPC test suite.** The test
   sweep destroys live shared-memory segments.
10. **Verify by artifact.** A bundle in the outbox, a row from `koyu tail`,
    a frame from `koyu frame`. Process listings lie.

## Routes

### Add a process

Append a stanza to `services.yaml`: a `cmd` in list form, optional `env`,
and an `ipc:` block declaring what it writes, reads, publishes, subscribes,
notifies, and listens to (law 5). Run `koyu apply` to hot-add it without
touching running services, or `koyu up` from cold. The boot typecheck names
any type it cannot resolve; fix exactly what it names.

### Add a sensor or peripheral

Define one `ctypes.Structure` in `koyu_runtime/ipc/types.py`: `timestamp`
then `frame_id` first, fixed sizes, and a valid-length field for variable
data. Publish it from the owning service, using a stream for high rates or
a blackboard cell for latest-value state. Declare it (law 5) and
observability arrives free: `koyu tail <topic>` for values, `koyu frame
<topic>` for images. A low-rate input like a foot pedal fits an event
channel instead: payload-free, `Notifier.ring(id)`, declared under
`ipc.events`.

### Choose where new state lives

Choose by the rate it changes. Changes rarely: an env var in the service
stanza, where a change means a restart. Changes below roughly one hertz:
the param server or a file inbox, which covers task names, tunables, and
provenance. Changes faster than that: shared memory, with streams for
camera-rate data and events for doorbells.

### Record and judge episodes

Write the recording context first (law 7), then ring `recorder/control`
START (1).
The recorder mints a `capture_id` and publishes it on `recorder/telemetry`;
that id is the episode's identity forever, and the workspace episode
becomes `ep_<capture_id>`. To judge an episode, submit
`{"capture_id": …, "reward": …, "events": […]}` to the `data_recorder`
verdicts inbox and then ring STOP (2), in that order (law 6). DISCARD (3)
kills a recording mid-flight; rows live in memory until finalize, so
nothing touches disk. Finished bundles land in
`$KOYU_RUNTIME_DIR/data-recordings/`, and `koyu ingest` files them into the
workspace.

### Register provenance

Run `koyu context set source_run_id=… source_checkpoint=… policy_name=…`
before collecting, and confirm with `koyu context show`. The file
`recording-context.json` is the contract; the recorder snapshots it at
every START. Orchestrators that change the task per episode write the file
directly.

### Wire in a policy checkpoint

Serve the policy as a process speaking the eval contract streams (see any
template's `eval_contract.py`): observation cells in, one action cell out,
with the action echoing the observation's `frame_id` and timestamp. Add it
to `services.yaml` with its checkpoint path as an argument, register
provenance for it (route above), and point the recorder's SOURCES at the
contract topics with `paired=True`, so rows pair by exact frame identity
instead of timestamps.

### Build a browser surface

The bridge serves four primitives: latest values over WS `subscribe-topic`,
history you accumulate client-side, video at `/mjpeg/<topic>` and
`/frame/<topic>`, and verbs over `ring-event`. Topic types resolve from
`services.yaml`, so declare first (law 5). The reference client and the
full primer both live in the workspace's `src/lib/useBridge.ts` header. An
agent that wants its own eyes on the robot runs `koyu frame <topic>` and
reads the file it prints.

### Observe and debug

`koyu status` for liveness, `koyu logs <service> -f` for stdout,
`koyu tail <topic>` for recent values, `koyu frame <topic>` for the newest
image. A service that cannot start exits nonzero with a one-line reason,
visible in `koyu status`. Errors here are written to name the next move;
read them fully before acting (law 10 for confirming the fix).
