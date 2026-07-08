"""IPC logger — passive, always-on recorder for agent introspection.

Logs the topics assigned to it in services.yaml (its own ipc reads/subscribes/
listens), on change, so an agent can `tail` a topic and pull recent frames.
Read-only — it never writes a topic, so it can't perturb what it observes. All
output lives under <runtime>/ipc_logger/<topic>/ , one folder per topic/channel:
  - structured topics -> state.jsonl   (size-capped, rotating)
  - image topics       -> frames/*.jpg (size-capped ring)
  - event channels     -> events.jsonl (size-capped, rotating)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from pathlib import Path

from PIL import Image

from ipc import types
from ipc.service import Service, read_service_ipc

JSONL_CAP, JSONL_BACKUPS, FRAME_CAP = 1_000_000, 4, 5_000_000   # per-topic caps


class FrameRing:
    """A byte-capped ring of JPEGs on disk; oldest evicted once over the cap."""

    def __init__(self, path, cap_bytes: int):
        self.dir = Path(path)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cap = cap_bytes
        self.n = 0

    def save(self, img) -> None:
        self.n += 1
        img.save(self.dir / f"{self.n:09d}.jpg", "JPEG", quality=70)
        files = sorted(self.dir.glob("*.jpg"))
        total = sum(f.stat().st_size for f in files)
        i = 0
        while total > self.cap and i < len(files):
            total -= files[i].stat().st_size
            files[i].unlink()
            i += 1


def _snapshot(struct) -> dict:
    """A struct's scalar fields as a dict; skips arrays (data/_pad) we can't log."""
    out = {}
    for field, _ in struct._fields_:
        value = getattr(struct, field)
        if hasattr(value, "__len__"):
            continue
        out[field] = value
    return out


def _typename(spec):
    return spec if isinstance(spec, str) else spec["type"]


def _is_image(struct_type):
    return all(hasattr(struct_type, a) for a in ("data", "width", "height"))


class IpcLogger(Service):
    def __init__(self, home, ipc):
        self.home, self.ipc = Path(home), ipc
        super().__init__("ipc_logger")

    def setup(self):
        self.base = self.home / "services" / self.name     # <runtime>/services/ipc_logger/
        self._readers, self._rings, self._logs, self._last = {}, {}, {}, {}
        bb = (self.ipc.get("blackboard") or {}).get("reads", {})
        st = (self.ipc.get("streams") or {}).get("subscribes", {})
        for topic, spec in {**bb, **st}.items():
            T = types.resolve(_typename(spec))
            slug = topic.replace("/", "~")
            self._readers[topic] = (self.reader if topic in bb else self.subscriber)(topic, T)
            self._logs[topic] = self._open_log(slug, "state.jsonl")
            if _is_image(T):
                self._rings[topic] = FrameRing(self.base / slug / "frames", FRAME_CAP)
        for channel in (self.ipc.get("events") or {}).get("listens", []):
            self._logs[channel] = self._open_log(channel.replace("/", "~"), "events.jsonl")
            self.on(channel)
        self.tick(100)

    def _open_log(self, slug, filename):
        path = self.base / slug / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=JSONL_CAP, backupCount=JSONL_BACKUPS)
        log = logging.getLogger(f"blackbox-{self.base}-{slug}")
        log.handlers = [handler]
        log.propagate = False
        log.setLevel(logging.INFO)
        return log

    def on_event(self, channel, event_id):
        self._emit(self._logs[channel], {"kind": "event", "id": event_id})

    def on_tick(self):
        for topic, reader in self._readers.items():
            value = reader.latest() if hasattr(reader, "latest") else reader.read()
            if value is None:
                continue
            fid = getattr(value, "frame_id", None)
            if fid == self._last.get(topic):               # log only on change
                continue
            self._last[topic] = fid
            if topic in self._rings:
                self._save_frame(topic, value, fid)
            else:
                self._emit(self._logs[topic], {"kind": "state", **_snapshot(value)})

    def _save_frame(self, topic, frame, fid):
        w, h = int(frame.width), int(frame.height)
        if w and h and w * h * 3 <= len(frame.data):
            img = Image.frombytes("RGB", (w, h), bytes(frame.data[: w * h * 3]))
            self._rings[topic].save(img)
            self._emit(self._logs[topic], {"kind": "frame", "frame_id": fid, "w": w, "h": h})

    def _emit(self, log, rec):
        rec["t"] = time.time()
        log.info(json.dumps(rec))


def main():
    home = os.environ["KOYU_RUNTIME_DIR"]
    IpcLogger(home, read_service_ipc(home, "ipc_logger")).run()


if __name__ == "__main__":
    main()
