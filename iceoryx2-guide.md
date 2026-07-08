# iceoryx2 for the koyu runtime — a working guide

A from-scratch mental model of iceoryx2, oriented around what koyu actually is:
an on-box robot OS with high-rate IPC, an event-driven control plane, and an AI
agent that needs to read and steer the whole thing. Everything here is grounded
in the old `os/core/shm.py` helpers and in live experiments (the runnable
versions are in `examples/`, validated against iceoryx2 **0.8.1**).

---

## 0. The one-paragraph model

iceoryx2 is **shared-memory IPC with no broker**. Processes (each a *node*) talk
through *named services*. A service is exactly one of four *messaging patterns*.
Data is written **once** into a shared-memory segment and read by **pointer** —
zero-copy, so fanning a 2.76 MB camera frame out to four readers costs one write,
not four copies. There is no daemon in the data path: the kernel + `/dev/shm`
*is* the transport. Discovery and liveness live in two directories on disk
(`/tmp/iceoryx2` for management, `/dev/shm` for payload). That "it's just files
and shared memory" property is exactly what makes it agent-introspectable.

Contrast with what the old OS actually did: it used iceoryx2 for **one** of those
four patterns (blackboard) and bolted **NATS** on for everything else. The
rewrite uses all four patterns and deletes NATS. This guide teaches the whole
surface so that move makes sense.

> **What Richard Feynman would say:** Strip the fancy words off and what've you
> got? Two programs staring at the same patch of memory, with some rules about who
> writes and who reads. That's the whole thing. "Broker-less zero-copy transport"
> is a very dignified way of saying *a wall with a window in it, and nobody's dumb
> enough to photocopy the photo when they can just point at it through the glass.*

---

## 1. The three primitives: Node, Service, Port

**Node** — one per process. It's your handle to the iceoryx2 "system" and it
registers in `/tmp/iceoryx2/nodes/`. Create exactly one and share it; the old
code's `_get_node()` singleton (`os/core/shm.py:46`) exists precisely because one
node per topic pollutes the namespace and confuses dead-node GC.

```python
import iceoryx2 as iox2
node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)   # ServiceType.Ipc = cross-process
```

**Service** — a named rendezvous point (`"camera/rgb"`, `"commander/state"`).
You *create* it (you're the owner who fixes its config) or *open* it (you attach
to someone else's). Its **static config — sizes, buffer ceilings, history — is
frozen at create time by whoever creates it first.** This single fact drives a
lot of design (see §4, §10).

**Port** — the read/write endpoint you actually use. Each pattern names its ports
differently, and that naming *is* the pattern's personality:

| Pattern | Write side | Read side |
|---|---|---|
| pub/sub | `Publisher` | `Subscriber` |
| blackboard | `Writer` | `Reader` |
| event | `Notifier` | `Listener` |
| request-response | `Client` | `Server` |

So "readers and writers" isn't one concept — it's four pairs, each shaped for a
different data flow. Learning iceoryx2 *is* learning which pair fits which data.

> **What Richard Feynman would say:** Don't let "Node, Service, Port" fool you
> into thinking you learned something — those are just names. A node is a program
> that showed up. A service is a spot with a name chalked on it. A port is the
> actual hole you reach through. The thing worth knowing isn't the words, it's
> that one fellow puts stuff in, another takes it out, and they agreed where. Four
> kinds of holes, fine — but they're four answers to one plain question: how does
> the stuff get from *in* to *out*?

---

## 2. The four patterns and the rule that picks them

The decision rule is two questions: **how many readers, and does anyone need
history?**

| Pattern | Shape | Semantics | koyu use |
|---|---|---|---|
| **blackboard** | a shared variable | latest-value, many readers, **always readable**, no history | params, provenance context, per-service telemetry, loop state |
| **pub/sub** | a conveyor belt w/ per-reader queues | streamed samples, zero-copy, per-consumer buffering | camera frames, joint state/command |
| **event** | a doorbell heard building-wide | event-id only, no payload, wakes **all** listeners (many-to-many) | control verbs, heartbeats, "go look at the plane" |
| **request-response** | a function call | one request → one reply | almost nothing (its emptiness is the tell) |

Rule of thumb: changes every frame → pub/sub. "What is true right now," read by
many → blackboard. "Something happened, go look" → event. "I need an answer
back" → request-response (and you should be suspicious you do).

> **What Richard Feynman would say:** You don't pick the pattern off a table — you
> picture the data and the right one is just *obvious*. A camera frame is a river:
> it keeps coming whether you're ready or not, and you grab what flows past. A
> parameter is a number chalked on a wall: it just sits there being the current
> value. A doorbell is a doorbell. If you catch yourself squinting at the table to
> choose, that means you haven't actually pictured what the data *does* yet. Go do
> that first; the table's just a crutch.

---

## 3. Pattern 1 — blackboard (what the old OS used for *everything*)

**Mental model: a shared variable behind a name.** One slot per key. The writer
overwrites it; readers copy out the latest value. No queue, no history — miss
frame N, you just see N+1 next time. That's the whole semantics of a control
loop, which is why the old OS could get away with using it for camera frames,
joint state, *and* commands.

This is the pattern you already have, in `os/core/shm.py`. The write side:

```python
KEY_T = ctypes.c_uint64           # key type — MUST match Rust readers (they use u64)
KEY   = KEY_T(0)                  # single-entry convention: one slot, key 0

svc    = (node.service_builder(iox2.ServiceName.new("commander/state"))
              .blackboard_creator(KEY_T).add(KEY, MyStruct()).create())
writer = svc.writer_builder().create()
entry  = writer.entry(KEY, MyStruct)
entry.update_with_copy(state)     # atomically swap in the latest value
```

The read side — note it can `open()` only if a creator already exists, which is
why `ReaderManager` (`os/core/shm.py:296`) wraps open in retry/backoff:

```python
svc   = node.service_builder(iox2.ServiceName.new("commander/state")).blackboard_opener(KEY_T).open()
entry = svc.reader_builder().create().entry(KEY, MyStruct)
val   = entry.get().decode_as(MyStruct)   # copy out the current value
```

**The defining property: it's always readable.** The value sits in shared memory;
*any* reader, opening at *any* time, immediately gets the current value with no
replay and no request. A reader created an hour after the writer reads the latest
value on its first `get()`. (Proven in the late-join test — a fresh reader saw
`loop=15` instantly, no RPC.) This is the single most agent-friendly property in
the system: state is a thing you can walk up to and read.

**What it costs:** the struct is fixed-size and pre-allocated at create. Resize
it and the segment is invalid — readers segfault until you nuke the segments and
restart. Readers don't get woken; they **poll** and dedup on `frame_id`. And it's
copy-out per read, which is fine for a small params struct but wasteful for a
2.76 MB frame fanned to four readers (the reason streams move to pub/sub).

> **What Richard Feynman would say:** It's a whiteboard. One guy wipes it and
> writes the new number; anybody wandering past reads whatever's up there *now*.
> It doesn't remember what you wiped — and that's not a flaw, it's *exactly* what
> you want for "where's the arm this instant." The one way to get hurt: change how
> big the box is without telling the reader, and now he's reading half his number
> and half the next field, and he keels over. "Fixed-size" isn't red tape — it's
> that the memory is just a row of bytes and *somebody* has to agree which byte is
> which.

---

## 4. Pattern 2 — pub/sub (where the streams are going)

**Mental model: a conveyor belt with a separate basket per consumer.** The
publisher loans a slot from a shared pool, writes into it, and `send()`s; the
sample lands in **each subscriber's own queue**. Subscribers are independent —
one falling behind doesn't touch the others.

```python
svc = (node.service_builder(iox2.ServiceName.new("camera/rgb")).publish_subscribe(Frame)
           .subscriber_max_buffer_size(64)   # CEILING any subscriber may request
           .history_size(1)                  # retained for late joiners
           .enable_safe_overflow(True)       # full reader -> drop oldest, never block
           .create())
pub = svc.publisher_builder().create()
pub.send_copy(frame)                          # one memmove into shm; readers read by pointer

sub    = svc.subscriber_builder().buffer_size(1).create()   # this reader's own depth
sample = sub.receive()                        # None, or a Sample
if sample:
    fid = sample.payload().contents.frame_id  # read header through the pointer — no full copy
```

Five things the experiments nailed down, each load-bearing for koyu:

1. **Connected readers are never late.** Send then `receive()` returns that frame
   the same iteration — median **82 µs** for a 2.76 MB frame, ~400× faster than a
   30 fps frame period. You read the instant it's written. (The only thing that
   "waits for the next send" is a *late joiner's backlog* via `history` — see §4a.)

2. **Variable buffers off one publisher — with a ceiling.** Each subscriber picks
   its own `buffer_size`, but `subscriber_max_buffer_size` (fixed by the service
   *creator* — the publisher) is a hard ceiling. A late reader asking for more is
   **rejected**, not clamped (`BufferSizeExceedsMaxSupportedBufferSizeOfService`).
   → Declare the ceiling ≥ your deepest reader (the recorder) in the publisher's
   config. Because publishers boot before subscribers, the publisher owns it.

3. **Deep + shallow coexist cleanly.** A recorder with `buffer_size=60` that never
   drains and an inference reader with `buffer_size=1` ran off the same publisher:
   inference always got the freshest frame, the recorder dropped *its own* oldest
   when full, the publisher's delivered-count stayed constant — **never stalled.**

4. **The Block footgun.** `enable_safe_overflow(True)` (or
   `UnableToDeliverStrategy.DiscardSample`) means a full slow reader drops samples
   for itself and the publisher sails on. The iceoryx2 **default is `Block`**,
   which makes a full reader *stall the publisher* — and therefore stall
   inference too. Camera/stream publishers must opt into overflow explicitly.

5. **The pool is pre-allocated, and it's `max_subscribers × ceiling × frame`.**
   On 2.76 MB frames, ceiling 90 × 4 subs ≈ **1 GB** of `/dev/shm`, reserved up
   front. The mitigation is already in your `services.yaml`: split camera into
   `*_policy` (128×128 = 49 KB, the topic the recorder/inference buffer deeply)
   and `*_ui` (512×512, depth-1 for the video bridge). **Rule: deep buffers only
   on small frames; full-res topics use depth-1.**

> **What Richard Feynman would say:** A conveyor belt with one basket per person.
> The pretty part — and don't take my word for it, the experiment's sitting right
> there, *run it* — is that the slowpoke's overflowing basket doesn't slow the
> quick fellow down one bit. 82 millionths of a second to hand over a
> 2.76-megabyte picture, because nobody hauls the picture around; they pass a
> claim-check to the one copy in the shared room. And here's me nearly fooling
> myself: I said readers might be "a frame late," reasoned it out all clever-like —
> dead wrong. The cure was to quit arguing and *run it and look.* First principle:
> you must not fool yourself, and you are the easiest person to fool.

### 4a. The one real subtlety: `history` and late joiners

`history_size(N)` lets the publisher retain its last N samples and hand them to a
**newly connected** subscriber. But that handoff happens during the publisher's
**next `send()`**, not at the instant of connect:

```
reader joins after frame 5, no new publish -> receive(): None      (backlog not pushed yet)
publisher then sends frame 6               -> receive(): [5, 6]     (history + new)
```

For a live camera this is invisible (next frame is ~33 ms away). It only matters
for a **one-shot read against an idle/stopped stream** — exactly the agent
"grab me a frame" case on a paused camera. There, a blackboard (always-readable)
beats pub/sub-history (nothing until the next send). Keep this in mind when
deciding which agent reads go on which pattern.

> **What Richard Feynman would say:** This little wrinkle is the whole game in
> miniature. I had a tidy theory about when a latecomer sees the backlog — and the
> theory was beside the point. I sent the frames, looked at what came out, and the
> machine told me the truth in two lines. Trust the experiment over the story in
> your head. Every single time.

---

## 5. Pattern 3 — events (the doorbell; the new control plane)

**Mental model: a doorbell with a number on it — that the whole building hears,
and anyone can press.** A `Notifier` rings an `EventId`; **every** `Listener`
connected to that event service wakes and reads which id(s) rang. **No payload.**
It's a many-to-many channel, not one-to-one: the service name is the "subject"
(à la NATS), `max_listeners`/`max_notifiers` (default 16 each) are your fan-out
headroom, and the id space is an enum (`event_id_max_value`, default 255). One
ring → N processes woke is proven in `examples/` — the frontend *and* the AI can
both be notifiers on the same channel.

```python
ev       = node.service_builder(iox2.ServiceName.new("commander/control")).event().open_or_create()
notifier = ev.notifier_builder().create()
notifier.notify_with_custom_event_id(iox2.EventId.new(1))   # "1" = START_TELEOP, a verb

listener = ev.listener_builder().create()
verbs    = [e.as_value for e in listener.try_wait_all()]    # as_value is a PROPERTY, no parens
```

The universal idiom is **data-then-doorbell**: put the real payload on a plane
(a blackboard, or the filesystem), then ring the bell; the listener reads the
plane. The bell carries no data — it only says "go look." This is how the control
plane that used to be NATS messages becomes events: `param_changed`,
`context_updated`, `episode_captured` ("sweep the outbox"), recorder
`start/stop/discard`, heartbeats.

Two findings that matter:

- **Events are an ordered bounded FIFO in 0.8.1, not coalescing.** Ringing
  `START, STOP, START` and draining yields `[1, 2, 1]` — order and count
  preserved (≥50 same-id bursts all survived). So ordered control verbs are safe
  without ceremony. The design doc's "events coalesce, design for it" is the
  conservative model; reality is more forgiving, but the queue *is* bounded, so
  don't use events for high-rate data — that's what pub/sub is for.
- **Liveness primitives are built in.** The event service exposes
  `notifier_dead_event` / `notifier_dropped_event` / `notifier_created_event` —
  so peer-death detection is first-class, not something you hand-roll.

> **What Richard Feynman would say:** A doorbell's got no letter taped to it. It
> only hollers *go look!* The classic blunder is trying to mail your letter
> *through* the doorbell — don't; lay the letter on the table, ring the bell, and
> let the other fellow read the table. And that word "coalescing" the manual
> scares you with? We rang the thing fifty times and counted fifty. The
> frightening name didn't match what the bird was doing. So look at the bird — not
> the name of the bird.

---

## 6. Pattern 4 — request-response (the one you should rarely reach for)

A real call-and-reply: `Client` sends a request, `Server` sends one response. The
binding exposes it (`ServiceBuilderRequestResponse`), but the design philosophy
is that **a near-empty request-response plane is the sign your decomposition is
right.** The RPCs the old OS had over NATS — `commander.status`,
`provenance.get`, `param.get_all` — all become **blackboard reads**: instead of
asking and waiting, the answer is current-truth you just read. (This is the one
pattern not exercised in the spikes; treat its details as unverified.)

> **What Richard Feynman would say:** This is just asking a question and standing
> there waiting for the answer. The interesting thing is how rarely you should
> need it. If you're forever asking "what's your status?", chances are you forgot
> to write the answer up on the wall where anybody could've just read it. A
> machine that has to keep asking questions is a machine that didn't write enough
> down.

---

## 7. The WaitSet — your event loop

**Mental model: `epoll`/`select`, but for iceoryx2 ports.** One reactor per
daemon, blocking on many sources at once, woken only when something happens.
Three kinds of attachment:

- `attach_interval(Duration)` — a periodic tick (your control loop, a telemetry flush)
- `attach_notification(listener)` — wake when this listener's event rings
- `attach_deadline(listener, Duration)` — wake on the event **or** when the timeout elapses with no event (a *missed deadline*)

The Python contract returns a list of fired attachments plus a result:

```python
ws    = iox2.WaitSetBuilder.new().create(iox2.ServiceType.Ipc)
tick  = ws.attach_interval(iox2.Duration.from_millis(100))
ctl_g = ws.attach_notification(control_listener)
hb_g  = ws.attach_deadline(heartbeat_listener, iox2.Duration.from_millis(300))

while running:
    aids, result = ws.wait_and_process_with_timeout(iox2.Duration.from_millis(500))
    for aid in aids:
        if aid.has_event_from(tick):          do_control_tick()
        if aid.has_event_from(ctl_g):         apply_verbs(control_listener.try_wait_all())
        if aid.has_missed_deadline(hb_g):     mark_peer_dead()
```

This single construct is what makes "one transport, no NATS" ergonomic. It
replaces, in one loop, what used to be **three** mechanisms: the asyncio loop
dispatching NATS callbacks, the hand-rolled timed control loop, and the
filesystem-lease liveness poll. **Liveness becomes a deadline-miss** — no
heartbeat JSON, no lease. The control-plane spike (`examples/control_plane.py`)
runs a whole commander + a browser-facing bridge this way, with `kill -9`
detected as a missed deadline in 300 ms.

> **What Richard Feynman would say:** It's one sleepy fellow you've handed a short
> list of things allowed to wake him: a kitchen timer, a doorbell, and "if the
> baby hasn't cried in three seconds, go make sure it's still breathing." That's
> the whole "reactor." The clever one is that third alarm — waking up because
> something *didn't* happen. Any fool can jump at a noise; noticing the *silence*
> is how you catch the thing that quietly died.

---

## 8. The koyu data-flow map

```
                 ┌── pub/sub (depth-1, overflow-drop) ──▶ inference   ─┐
   camera ──pub──┼── pub/sub (deep buffer) ─────────────▶ recorder    │  each daemon's
   (publisher)   └── pub/sub (depth-1) ─────────────────▶ video_bridge│  WaitSet is its
                                                                       │  single reactor:
   param_server ──blackboard(params)──┐                                │   • interval ticks
   provenance   ──blackboard(context)─┼──▶ readers (incl. AI agent)    │   • event listeners
   each service ──blackboard(telemetry)┘    read current truth anytime │   • hb deadlines
                                                                       │
   frontend/agent ──event(verbs)──▶ commander/recorder  ◀──event(hb)──┘
                                                              │
   bridge ── reads blackboards + listens to hb events ──▶ browser (WS/HTTP unchanged)
```

The old→new translation in one table:

| Old OS | New (iceoryx2 only) |
|---|---|
| iceoryx2 blackboard for *everything* (incl. camera) | blackboard for **current truth only**; streams move to **pub/sub** |
| NATS `param.updated` + `param.get_all` | blackboard + `param_changed` event |
| NATS `provenance.context` + `provenance.get` | blackboard + `context_updated` event |
| NATS `commander.*` / `recorder.*` verbs | events (doorbells) |
| NATS `commander.status` RPC | blackboard read |
| NATS `service.*.heartbeat` JSON | event (liveness) + blackboard (telemetry) |
| filesystem lease + wrappers (process-alive) | WaitSet **deadline-miss** |
| busy-poll loops + asyncio dispatch | one **WaitSet** per daemon |

> **What Richard Feynman would say:** Don't trust the diagram — trust that you
> could draw it yourself from scratch with the lights off. *What I cannot create,
> I do not understand.* If you can lay out who-writes-what and who-reads-what
> without peeking, you've got the machine in your head; the boxes and arrows are
> just a string tied round your finger to remember it by.

---

## 9. The throughline: AI-agent introspectability

This is the reason the pattern choices matter beyond performance. Each pattern
has an agent-facing personality:

- **Current truth is always readable (blackboard).** The agent opens a reader,
  reads params / provenance context / a service's telemetry / loop state, and
  exits. **Late-open is the norm** — no subscription, no RPC, no race. "What is
  the robot doing right now" is a file-like read, available any time. This is the
  single biggest reason to prefer blackboard for state the agent inspects.
- **Failures become readable states, not exceptions.** The discipline is *every
  failure serializes to a string in a file the agent knows to read*: `state.json`
  (supervision), the episode sidecar (capture), spool depth (ingest lag). The
  agent debugs **below** the IPC layer — even when the transport is wedged, the
  files still tell the story.
- **Liveness is observable, not inferred.** Heartbeat events + WaitSet deadlines
  mean "is X alive" is a deadline-miss the supervisor records — and the agent
  reads the record. Telemetry (mode, frames_buffered, spool depth) is a blackboard
  the agent reads directly.
- **Transport topology is queryable.** `iox2 service list` / `node list` (the CLI
  over the same `/tmp/iceoryx2` + `/dev/shm` state) gives the agent the *observed*
  IPC graph; `services.yaml` is the *desired* graph. Should-vs-is diffing is the
  foundation of every agent operation.
- **Frame grabs are late-open reads.** `history=1` + a fresh subscriber lets the
  agent grab the latest camera frame and exit (with the idle-stream caveat of §4a
  — which is itself an argument for keeping some agent reads on blackboard).
- **Reads are free; writes are staged.** The safety asymmetry: the agent reads
  anything, but writes (firing a verb, restarting a service) go through the user,
  and hardware-moving actions are user-initiated. The transport makes reads cheap
  and ubiquitous *by design*, which is what makes that asymmetry practical.

The pattern, restated: **state you want the agent to see → blackboard (always
readable). Things the agent should react to → events. Everything serializes to a
file so the agent can read it even when IPC is down.**

> **What Richard Feynman would say:** The whole game is this: can a nosy stranger
> walk up and *see* what's going on, without interrupting anybody or knocking the
> thing over? A wall you can read any time beats a busy man you have to tap on the
> shoulder. And the deepest trick is writing every failure down as a plain
> sentence on that wall — so when the gears jam, the *reason* isn't jammed too. A
> system that can't tell you why it's stuck is fooling you, and you shouldn't
> stand for it.

---

## 10. Operational gotchas (paid for in the experiments)

- **Two directories, not one.** Management state is in `/tmp/iceoryx2`
  (`nodes/`, `services/`); **payload segments are in `/dev/shm/iox2*`**. The old
  doc's "nuke `/tmp/iceoryx2`" reset is *incomplete* — stale `/dev/shm` segments
  cause `ServiceInCorruptedState` on the next create. A full reset clears both;
  normal operation relies on `node.cleanup_dead_nodes(...)`.
- **Properties masquerading as methods.** `EventId.as_value` and
  `Subscriber.buffer_size` are properties — no parens. `listener.try_wait_all()`
  returns a *list* (despite a docstring that says it takes a callback).
- **Fixed-size, `repr(C)`, mirrored.** Every shared struct is a `ctypes.Structure`
  on the Python side and a `#[repr(C)]` companion on the Rust side, field-for-field
  (`os/core/types.py` ↔ `os/services/camera/src/lib.rs`). First two fields are
  always `timestamp: f64`, `frame_id: u64`. A struct change is a **generation
  event**: bump the boot generation, sweep segments, restart everyone. The
  supervisor's `--type-check` compares Rust's `print_type_layout()` against the
  Python struct and refuses to boot on a mismatch.
- **Stale-segment recovery is real work.** The old `BlackboardWriter` does a
  create → `cleanup_dead_nodes` → reopen-and-take-over dance
  (`os/core/shm.py:106`) because a dead writer can leave a segment that readers
  are still holding alive. Budget for this in the rewrite's writer path.
- **Cross-language key/type names must match.** Blackboard matches on key **and**
  type name; the Python `CameraFrame` overrides `type_name()` to return the Rust
  crate-qualified `camera_service::CameraFrame` (`os/core/types.py:120`).

> **What Richard Feynman would say:** These aren't trivia — they're the spots
> where reality reached out and bit you. The `/dev/shm` one is the whole lesson in
> a nutshell: the manual said "clean this drawer," we believed it, then we
> actually *looked* and the real mess was in a different room entirely. So the
> takeaway isn't "memorize two directories." It's: when something's screwy, go
> look at where the bytes really live. Never trust the label over the territory.

---

## 11. Cheat sheet

```python
import ctypes, iceoryx2 as iox2
node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
 name = lambda s: iox2.ServiceName.new(s)
KEY_T, KEY = ctypes.c_uint64, ctypes.c_uint64(0)

# --- blackboard (current truth) ---
w = node.service_builder(name("t/state")).blackboard_creator(KEY_T).add(KEY, S()).create() \
        .writer_builder().create().entry(KEY, S)
w.update_with_copy(s)
r = node.service_builder(name("t/state")).blackboard_opener(KEY_T).open() \
        .reader_builder().create().entry(KEY, S)
val = r.get().decode_as(S)

# --- pub/sub (streams) ---
svc = node.service_builder(name("t/stream")).publish_subscribe(Frame) \
          .subscriber_max_buffer_size(64).history_size(1).enable_safe_overflow(True).create()
pub = svc.publisher_builder().create();             pub.send_copy(frame)
sub = svc.subscriber_builder().buffer_size(1).create()
sample = sub.receive()                              # None or Sample; .payload().contents.<field>

# --- event (doorbell) ---
ev = node.service_builder(name("t/ev")).event().open_or_create()
ev.notifier_builder().create().notify_with_custom_event_id(iox2.EventId.new(1))
ids = [e.as_value for e in ev.listener_builder().create().try_wait_all()]

# --- waitset (reactor) ---
ws = iox2.WaitSetBuilder.new().create(iox2.ServiceType.Ipc)
g_tick = ws.attach_interval(iox2.Duration.from_millis(100))
g_evt  = ws.attach_notification(listener)
g_dead = ws.attach_deadline(hb_listener, iox2.Duration.from_millis(300))
aids, result = ws.wait_and_process_with_timeout(iox2.Duration.from_millis(500))
for aid in aids:
    aid.has_event_from(g_tick); aid.has_event_from(g_evt); aid.has_missed_deadline(g_dead)
```

Reset between runs / on generation change:
```bash
rm -rf /tmp/iceoryx2 /dev/shm/iox2*
```

> **What Richard Feynman would say:** A crib sheet's fine for going fast. But if
> all you can do is *copy* it — if you couldn't sit down with a blank page and
> write it out yourself — then you've got the name of the bird and not the bird.
> Use it to save time, never to skip the understanding.

---

## 12. The index card

1. Node per process; service per name; port per pattern.
2. Four patterns, picked by *how many readers* and *does anyone need history*.
3. Blackboard = a shared variable: latest-value, **always readable**, the agent's window into state.
4. Pub/sub = per-consumer conveyor belts: zero-copy, independent buffers, connected readers are never late.
5. The service *creator* fixes the static config (the buffer ceiling). Publishers boot first so they own it.
6. Overflow ON for streams — `Block` is the default and it's a footgun.
7. Deep buffers only on small frames; the pool is `max_subscribers × ceiling × frame`, pre-allocated.
8. Events are doorbells: data on a plane, then ring. Ordered FIFO, bounded — not for high-rate.
9. One WaitSet per daemon is the whole event loop. Liveness = deadline-miss.
10. Two directories: `/tmp/iceoryx2` (mgmt) + `/dev/shm` (payload). Sweep both.
11. Shared types: one source, `repr(C)`, fixed-size. Changing one is a reboot.
12. Every failure serializes to a file the agent can read — even when IPC is wedged.

> **What Richard Feynman would say:** Notice it fits on a card. That's not because
> we left things out — it's because once you genuinely understand a thing, it gets
> *small*. The card isn't what you memorize up front; it's what's left in your
> hand after the real understanding has burned off all the smoke.

---

*Runnable companions in `examples/`: `control_plane.py` (events + blackboard +
WaitSet deadlines, NATS-free), `streams.py` (pub/sub buffers, overflow, RAM),
`latency.py` (send→receive timing). Each prints what it proves.*
