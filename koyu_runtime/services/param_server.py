"""Parameter server — the single source of truth for tunable config.

For each blackboard topic it writes (services.yaml `blackboard.writes`) it owns a
JSON file and a live blackboard. The JSON holds, per param, a value and — for
numeric params only — an optional range:
    {"speed": {"value": 1.0, "min": 0.0, "max": 10.0}}
On boot every struct param must have a `value` or it raises (→ crash-loop →
FATAL); the range is optional. A set {key, value, persist?} must fit the struct
field's type and is range-checked only if the param declares min/max; a valid set
updates the live blackboard always, and disk only when persist is set. Invalid
sets (unknown key, wrong type, out of range) are quarantined, never applied.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from koyu_runtime.ipc import types
from koyu_runtime.ipc.service import Service, read_service_ipc
from koyu_runtime.services.inbox import Inbox, inbox_path

META = ("timestamp", "frame_id")    # struct header fields, not params


def _typename(spec):
    return spec if isinstance(spec, str) else spec["type"]


def _param_fields(struct_type):
    return [f for f, _ in struct_type._fields_ if f not in META]


def _fits(struct_type, key, val) -> bool:
    """Probe whether the value lands in the struct field, by trying it on a
    scratch instance. Bools are rejected explicitly: ctypes would silently
    coerce True into a c_double as 1.0."""
    if isinstance(val, bool):
        return False
    try:
        setattr(struct_type(), key, val)
        return True
    except TypeError:
        return False


class ParamServer(Service):
    def __init__(self, home, ipc):
        self.home, self.ipc = Path(home), ipc
        super().__init__("param_server")

    def setup(self):
        self.topics = {}
        base = self.home / "services" / self.name            # <runtime>/services/param_server/
        for topic, spec in (self.ipc.get("blackboard") or {}).get("writes", {}).items():
            T = types.resolve(_typename(spec))
            slug = topic.replace("/", "~")
            file = base / f"{slug}.json"
            params = self._load(file)
            for f in _param_fields(T):                        # bootstrap-or-die
                if not isinstance(params.get(f), dict) or "value" not in params[f]:
                    raise RuntimeError(f"param_server: {topic} param '{f}' has no value in {file.name}")
            self.topics[topic] = {
                "w": self.writer(topic, T), "T": T, "params": params, "file": file,
                "inbox": Inbox(inbox_path(self.home, self.name, slug)), "frame": 0,
            }
            self._publish(self.topics[topic])
        self.tick(20)

    def on_tick(self):
        for topic, t in self.topics.items():
            changed = persist = False
            for req in t["inbox"].drain():
                if self._apply(topic, t, req):
                    changed = True
                    persist = persist or bool(req.get("persist"))
            if changed:
                self._publish(t)
                if persist:
                    self._save(t)

    def _apply(self, topic, t, req) -> bool:
        """Validate-or-quarantine: a rejected set never reaches the struct (a bad
        value would TypeError inside _publish and crash-loop the server) and is
        parked loudly next to the inbox rather than silently dropped."""
        key, val = req.get("key"), req.get("value")
        entry = t["params"].get(key)
        if entry is None:
            t["inbox"].quarantine(req, f"{topic}: unknown param {key!r}")
            return False
        if not _fits(t["T"], key, val):
            t["inbox"].quarantine(req, f"{topic}: value {val!r} does not fit param {key!r}")
            return False
        if "min" in entry and "max" in entry and not (entry["min"] <= val <= entry["max"]):
            t["inbox"].quarantine(
                req, f"{topic}: {key}={val} out of range [{entry['min']}, {entry['max']}]")
            return False
        entry["value"] = val
        print(f"[param_server] {key}={val} persist={bool(req.get('persist'))}", flush=True)
        return True

    def _publish(self, t):
        t["frame"] += 1
        cfg = t["T"](timestamp=time.time(), frame_id=t["frame"])
        for f in _param_fields(t["T"]):
            setattr(cfg, f, t["params"][f]["value"])
        t["w"].write(cfg)

    def _load(self, path) -> dict:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, t):
        tmp = t["file"].with_suffix(".tmp")
        tmp.write_text(json.dumps(t["params"], indent=2))
        os.replace(tmp, t["file"])


def main():
    home = os.environ["KOYU_RUNTIME_DIR"]
    ParamServer(home, read_service_ipc(home, "param_server")).run()


if __name__ == "__main__":
    main()
