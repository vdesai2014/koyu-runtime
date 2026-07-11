"""File inboxes — the durable, multi-writer command seam into a service.

THE PATH LAW: every runtime path derives from $KOYU_RUNTIME_DIR through these
helpers. No service, client, or external process ever composes
``services/<name>/inbox`` by hand, and the current working directory is never
consulted. (The param-server folder crash was this law being violated.)

    inbox_path(home, "param_server", "camera~exposure")   # a topic's param inbox
    inbox_path(home, "data_recorder", "verdicts")         # the verdict inbox

``Inbox`` is the mechanism: any process atomically drops a JSON file; the
owning service drains them in order. Survives the writer's death — the file
persists, unlike an in-flight iceoryx2 sample.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def slug(topic: str) -> str:
    """Topic name -> filesystem-safe inbox name."""
    return topic.replace("/", "~")


def service_dir(home: str | Path, service: str) -> Path:
    return Path(home) / "services" / service


def inbox_path(home: str | Path, service: str, box: str) -> Path:
    return service_dir(home, service) / "inbox" / box


class Inbox:
    """Durable multi-writer command queue: any process atomically drops a JSON
    request file; the owner drains them in order."""

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

    def quarantine(self, req: dict, reason: str) -> None:
        """Park a rejected request next to the inbox, loudly. Failures become
        states: the file is kept for a human/agent to inspect, never silently
        dropped."""
        qdir = self.dir.parent / "quarantine"
        qdir.mkdir(parents=True, exist_ok=True)
        name = f"{time.time_ns()}.json"
        (qdir / name).write_text(json.dumps({"reason": reason, "request": req}, indent=2))
        print(f"[inbox] quarantined ({reason}): {qdir / name}", flush=True)
