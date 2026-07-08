"""Parameter server — the single source of truth for tunable config.

For each blackboard topic it writes (services.yaml `blackboard.writes`) it owns a
JSON file and a live blackboard. The JSON holds, per param, a value and — for
numeric params only — an optional range:
    {"speed": {"value": 1.0, "min": 0.0, "max": 10.0}}
On boot every struct param must have a `value` or it raises (→ crash-loop →
FATAL); the range is optional. A set {key, value, persist?} is range-checked only
if the param declares min/max, updates the live blackboard always, and disk only
when persist is set.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from ipc import types
from ipc.service import Service, read_service_ipc

META = ("timestamp", "frame_id")    # struct header fields, not params


class Inbox:
    """Durable multi-writer command queue: any process atomically drops a JSON
    request file; the owner drains them in order. Survives the writer's death —
    the file persists, unlike an in-flight iceoryx2 sample."""

    def __init__(self, path):
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)

    def submit(self, req: dict) -> None:
        stem = f"{time.time_ns()}_{os.getpid()}"
        tmp = self.dir / f".{stem}.tmp"
        tmp.write_text(json.dumps(req))
        tmp.rename(self.dir / f"{stem}.json")          # atomic publish

    def drain(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.json")):
            # files are atomic + complete, so a parse error is genuine garbage:
            # drop it loudly (don't crash-loop the server) — any OTHER error bubbles.
            try:
                out.append(json.loads(f.read_text()))
            except json.JSONDecodeError as exc:
                print(f"[inbox] dropping malformed request {f.name}: {exc}", flush=True)
            f.unlink()
        return out


def _typename(spec):
    return spec if isinstance(spec, str) else spec["type"]


def _param_fields(struct_type):
    return [f for f, _ in struct_type._fields_ if f not in META]


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
                "inbox": Inbox(base / "inbox" / slug), "frame": 0,
            }
            self._publish(self.topics[topic])
        self.tick(20)

    def on_tick(self):
        for t in self.topics.values():
            changed = persist = False
            for req in t["inbox"].drain():
                if self._apply(t, req):
                    changed = True
                    persist = persist or bool(req.get("persist"))
            if changed:
                self._publish(t)
                if persist:
                    self._save(t)

    def _apply(self, t, req) -> bool:
        key, val = req.get("key"), req.get("value")
        entry = t["params"].get(key)
        if entry is None:
            print(f"[param_server] rejected unknown param {key!r}", flush=True)
            return False
        if "min" in entry and "max" in entry and not (
            isinstance(val, (int, float)) and entry["min"] <= val <= entry["max"]
        ):
            print(f"[param_server] rejected {key}={val} (out of range)", flush=True)
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
