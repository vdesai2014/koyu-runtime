"""One iceoryx2 node per process — the handle every reader/writer opens through.

A node registers in /tmp/iceoryx2/nodes/; one node per topic would pollute that
namespace and confuse iceoryx2's dead-node GC, so the whole process shares this
singleton.
"""

from __future__ import annotations

import threading

import iceoryx2 as iox2

_NODE = None
_LOCK = threading.Lock()


def node():
    """The process-wide singleton node (created on first use).

    Double-checked locking: the iceoryx2 FFI drops the GIL inside ``create()``,
    so concurrent first-callers (e.g. threads opening ports at boot) could
    otherwise race past the None check and register two nodes for one process.
    """
    global _NODE
    if _NODE is None:
        with _LOCK:
            if _NODE is None:
                iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)
                _NODE = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
                # iceoryx2 reaps dead nodes on creation by default, so this is
                # belt-and-suspenders — but it makes crash-recovery (a restarted
                # publisher reclaiming its old port) not depend on that default.
                _NODE.cleanup_dead_nodes(iox2.ServiceType.Ipc, _NODE.config)
    return _NODE


def name(topic: str):
    """A ``ServiceName`` from a topic string."""
    return iox2.ServiceName.new(topic)


def sweep_dead() -> None:
    """Reap iceoryx2 resources left behind by crashed processes."""
    n = node()
    n.cleanup_dead_nodes(iox2.ServiceType.Ipc, n.config)
