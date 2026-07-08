"""WebSocket + static bridge for the browser — ported to koyu-runtime IPC.

The browser can't read iceoryx2 SHM or fire events; this bridges both. The old
OS bridge sat on one plane (core.shm blackboard) + NATS. This one speaks the new
runtime's three planes and the param inbox; NATS is gone.

WS /ws  (client -> bridge):
  subscribe-topic   {topic, rate_hz?}              stream a blackboard OR pub/sub topic
  unsubscribe-topic {topic}
  ring-event        {channel, event_id}            fire a payload-less event (a verb)
  listen-event      {channel}                      forward event-ids fired after connect
  unlisten-event    {channel}
  set-param         {topic, key, value, persist?}  drop a request in param_server's inbox

bridge -> client:
  topic-data {topic, timestamp, frame_id, values}
  event      {channel, event_id}
  error      {message}

A topic's plane (blackboard vs pub/sub stream) and struct type are resolved from
services.yaml, so the client never has to know them. Generic — it carries no
app-specific knowledge.

HTTP:
  GET /            -> the static test page ($BRIDGE_STATIC_DIR/index.html), if set
  GET /static/...  -> static assets from $BRIDGE_STATIC_DIR
(MJPEG/camera streaming is deferred with video_bridge — cameras moved to pub/sub.)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import aiohttp
import yaml
from aiohttp import web

from ipc import types, events
from ipc.service import _LazyReader, _LazySubscriber
from services.param_server import Inbox

PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
STATIC_DIR = os.environ.get("BRIDGE_STATIC_DIR", "")
RUNTIME_DIR = Path(os.environ.get("KOYU_RUNTIME_DIR", "."))

# Drop heavy fields before the JSON encode (e.g. a CameraFrame buffer) so a
# careless subscribe-topic can't blow up the browser. Camera frames go over the
# (future) video path, not /ws.
LARGE_FIELD_SKIP = {"data", "_pad"}
LARGE_ARRAY_THRESHOLD = 1000


def _static_dir() -> Path | None:
    if not STATIC_DIR:
        return None
    d = Path(STATIC_DIR)
    return d if d.is_absolute() else RUNTIME_DIR / d


def topic_planes() -> dict[str, tuple[str, str]]:
    """{topic: (type_name, plane)} across every service's ipc block in services.yaml.

    plane is 'blackboard' or 'stream', taken from the ipc family the topic is
    declared under (blackboard.* vs streams.*)."""
    raw = yaml.safe_load((RUNTIME_DIR / "services.yaml").read_text()) or {}
    out: dict[str, tuple[str, str]] = {}
    for stanza in raw.values():
        ipc = (stanza or {}).get("ipc") or {}
        for direction in ("writes", "reads"):
            for topic, spec in ((ipc.get("blackboard") or {}).get(direction) or {}).items():
                out[topic] = (spec if isinstance(spec, str) else spec["type"], "blackboard")
        for direction in ("publishes", "subscribes"):
            for topic, spec in ((ipc.get("streams") or {}).get(direction) or {}).items():
                out[topic] = (spec if isinstance(spec, str) else spec["type"], "stream")
    return out


def serialize_struct(data) -> dict | None:
    if data is None:
        return None
    out = {}
    for field_name, _ in data._fields_:
        if field_name in LARGE_FIELD_SKIP:
            continue
        val = getattr(data, field_name)
        if hasattr(val, "__len__") and not isinstance(val, (str, bytes)):
            if len(val) > LARGE_ARRAY_THRESHOLD:
                continue
            val = list(val)
        out[field_name] = val
    return out


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    planes = topic_planes()
    sub_tasks: dict[str, asyncio.Task] = {}
    listen_tasks: dict[str, asyncio.Task] = {}
    notifiers: dict[str, events.Notifier] = {}
    print("[bridge] WS client connected", flush=True)

    async def poll_and_send(topic, type_name, plane, rate_hz):
        T = types.resolve(type_name)
        # Read-side ports open lazily (None until their writer/publisher is up).
        port = _LazySubscriber(topic, T, buffer=1) if plane == "stream" else _LazyReader(topic, T)
        read = port.latest if plane == "stream" else port.read
        last = None
        dt = 1.0 / max(rate_hz, 1)
        try:
            while True:
                v = read()
                if v is not None and getattr(v, "frame_id", None) != last:
                    last = v.frame_id
                    try:
                        await ws.send_json({
                            "type": "topic-data", "topic": topic,
                            "timestamp": v.timestamp, "frame_id": v.frame_id,
                            "values": serialize_struct(v),
                        })
                    except Exception:
                        break
                await asyncio.sleep(dt)
        except asyncio.CancelledError:
            pass

    async def listen_and_forward(channel):
        lis = events.Listener(channel)
        try:
            while True:
                for eid in lis.drain():
                    try:
                        await ws.send_json({"type": "event", "channel": channel, "event_id": eid})
                    except Exception:
                        return
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            pass

    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                req = json.loads(msg.data)
            except Exception:
                continue
            mt = req.get("type")

            if mt == "subscribe-topic":
                topic = req.get("topic", "")
                rate = req.get("rate_hz", 30)
                info = planes.get(topic)
                if not info:
                    await ws.send_json({"type": "error", "message": f"unknown topic {topic!r}"})
                    continue
                type_name, plane = info
                old = sub_tasks.pop(topic, None)
                if old:
                    old.cancel()
                sub_tasks[topic] = asyncio.create_task(poll_and_send(topic, type_name, plane, rate))
                print(f"[bridge] subscribe {topic} ({type_name}/{plane}) @ {rate}Hz", flush=True)

            elif mt == "unsubscribe-topic":
                old = sub_tasks.pop(req.get("topic", ""), None)
                if old:
                    old.cancel()

            elif mt == "ring-event":
                ch = req.get("channel", "")
                eid = int(req.get("event_id", 0))
                if ch:
                    notifiers.setdefault(ch, events.Notifier(ch)).ring(eid)
                    print(f"[bridge] ring {ch} id={eid}", flush=True)

            elif mt == "listen-event":
                ch = req.get("channel", "")
                if ch and ch not in listen_tasks:
                    listen_tasks[ch] = asyncio.create_task(listen_and_forward(ch))

            elif mt == "unlisten-event":
                old = listen_tasks.pop(req.get("channel", ""), None)
                if old:
                    old.cancel()

            elif mt == "set-param":
                topic = req.get("topic", "")
                key = req.get("key")
                if topic and key is not None:
                    r = {"key": key, "value": req.get("value")}
                    if req.get("persist"):
                        r["persist"] = True
                    Inbox(RUNTIME_DIR / "services" / "param_server" / "inbox" / topic.replace("/", "~")).submit(r)
                    print(f"[bridge] set-param {topic} {key}={r['value']}", flush=True)
    except Exception:
        pass
    finally:
        for t in list(sub_tasks.values()) + list(listen_tasks.values()):
            t.cancel()
        print("[bridge] WS client disconnected", flush=True)
    return ws


async def index_handler(request):
    d = _static_dir()
    if d is not None:
        idx = d / "index.html"
        if idx.exists():
            return web.Response(text=idx.read_text(), content_type="text/html")
    return web.Response(text="bridge up — set BRIDGE_STATIC_DIR for a page\n", content_type="text/plain")


async def run_bridge():
    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/", index_handler)
    d = _static_dir()
    if d is not None and d.is_dir():
        app.router.add_static("/static/", d)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    print(f"[bridge] listening on http://0.0.0.0:{PORT}", flush=True)
    await site.start()
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()


def main():
    asyncio.run(run_bridge())


if __name__ == "__main__":
    main()
