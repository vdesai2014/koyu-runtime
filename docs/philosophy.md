# Why the runtime is built this way

koyu-runtime makes a small number of structural bets and then gets out of
the way. This page records the reasoning behind those bets, so that future
changes can argue with the reasons instead of the code. The compressed
output of all this reasoning lives in the laws at the top of
[AGENTS.md](../AGENTS.md).

## Process isolation

Robot programs in Python drift toward a single multithreaded process, and
that shape ages badly. AI inference holds the GIL hostage, a crashed camera
thread takes the controller down with it, and swapping one policy for
another means restarting the world. koyu runs every concern as its own
supervised process instead. When a camera cable comes loose, you plug it
back in and restart one process. When you trade one policy for another, the
rest of the robot keeps running.

Supervisord manages those processes, adopted as is rather than wrapped or
reinvented. It is Python, it runs on Ubuntu and macOS, and it has done this
one job well for eighteen years. Process supervision is a solved problem,
and a robot runtime should spend its novelty budget elsewhere.

## Shared memory as the backbone

The price of processes is interprocess communication, so the IPC choice
matters most. iceoryx2 is a young library with old-school rigor: written in
Rust, memory safe, and built on shared memory that bypasses the kernel on
the data path, which keeps latency low and throughput high enough for
video. Its primitives are the exact vocabulary robot learning needs.
Streams carry camera frames and action chunks, blackboard cells hold
latest-value state, and events ring doorbells between processes.

Together, supervisord and iceoryx2 form the backbone: lifecycle and
plumbing, the two structural problems every robot runtime must solve.
Both are proven, and both are dependencies we accept happily.

## A backbone and nothing else

The repo ships with zero robot-specific code, and installation is a git
clone rather than a pip install. Both choices come from the same
observation: coding agents have made process-level work fast and personal.
The CAN adapter for your motor controllers, the browser page for your
control surface, the foot pedal that actuates your gripper, each of these
is an afternoon for an agent working against stable conventions. Shipping
our version of them would only give you something to delete. The runtime
provides the part that must hold still, and your agent builds the part
that must fit your robot.

## Built for agents

The larger bet is that software operated by coding agents behaves like
living software. Much of robot learning time has historically gone to
chores around the edges: checking whether a sensor is actually publishing,
wiring a new policy into the loop, keeping evaluation metadata honest by
hand. Done manually, these chores are slow and error prone. The runtime is
designed so an agent can do every one of them. Every topic is declared and
therefore observable, every path derives from one root, everything the
runtime knows is one `koyu` verb away, and every error is written to name
the next move. When introspection and modification become this cheap, most
of the historical friction in robot learning simply disappears.
