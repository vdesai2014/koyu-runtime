"""IPC logger — passive, always-on recorder for agent introspection.

By default it logs EVERY topic and event channel any service declares in
services.yaml (auto-discovery: declare a topic, get observability). Give the
logger its own ipc block to override with an explicit list, or an
``exclude:`` glob list in its stanza to silence noisy topics. Logs on
change, so an agent can `koyu tail` a topic and `koyu frame` recent images.
Read-only — it never writes a topic, so it can't perturb what it observes.
All output lives under <runtime>/services/ipc_logger/<topic>/ :
  - structured topics -> state.jsonl   (size-capped, rotating)
  - image topics       -> frames/*.jpg (size-capped ring)
  - event channels     -> events.jsonl (size-capped, rotating)

Note: events are typeless, so nothing else forces their declaration — but
only DECLARED channels (ipc.events.notifies/listens) are discovered here.
Declaring your doorbells is what makes their history tailable.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time
from fnmatch import fnmatch
from pathlib import Path

import yaml
from PIL import Image

from koyu_runtime.ipc import types
from koyu_runtime.ipc.service import Service, read_service_ipc

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
    """A struct's scalar fields as a dict; skips arrays (data/_pad) we can't log.
    c_char fields (capture_id, ...) read back as bytes and are decoded to str."""
    out = {}
    for field, _ in struct._fields_:
        value = getattr(struct, field)
        if isinstance(value, bytes):
            out[field] = value.decode("utf-8", errors="replace")
        elif not hasattr(value, "__len__"):
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


def discover_ipc(home, exclude=()):
    """Every topic and channel any service declares, folded into the logger's
    own config shape. blackboard writes|reads -> reads; stream
    publishes|subscribes -> subscribes; event notifies|listens -> listens.
    Dict-merge dedupes topics across services (boot typecheck already keeps
    their type declarations consistent)."""
    raw = yaml.safe_load((Path(home) / "services.yaml").read_text()) or {}
    bb, st, ev = {}, {}, set()
    for stanza in raw.values():
        ipc = (stanza or {}).get("ipc") or {}
        for direction in ("writes", "reads"):
            bb.update((ipc.get("blackboard") or {}).get(direction) or {})
        for direction in ("publishes", "subscribes"):
            st.update((ipc.get("streams") or {}).get(direction) or {})
        for direction in ("notifies", "listens"):
            ev.update((ipc.get("events") or {}).get(direction) or [])

    def keep(topic):
        return not any(fnmatch(topic, pattern) for pattern in exclude)

    return {
        "blackboard": {"reads": {t: s for t, s in bb.items() if keep(t)}},
        "streams": {"subscribes": {t: s for t, s in st.items() if keep(t)}},
        "events": {"listens": sorted(t for t in ev if keep(t))},
    }


def main():
    home = os.environ["KOYU_RUNTIME_DIR"]
    own = read_service_ipc(home, "ipc_logger")
    if any(own.values()):
        ipc = own                                  # explicit block wins
    else:
        raw = yaml.safe_load((Path(home) / "services.yaml").read_text()) or {}
        exclude = (raw.get("ipc_logger") or {}).get("exclude") or []
        ipc = discover_ipc(home, exclude)
    IpcLogger(home, ipc).run()


if __name__ == "__main__":
    main()
