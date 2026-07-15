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

from koyu_runtime.ipc import types, events
from koyu_runtime.ipc.service import _LazyReader, _LazySubscriber
from koyu_runtime.services.inbox import Inbox, inbox_path

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
        if isinstance(val, bytes):
            # c_char arrays (capture_id, task, ...) read back as bytes; JSON
            # can't carry bytes, and one bad field would kill the whole stream
            val = val.decode("utf-8", errors="replace")
        elif hasattr(val, "__len__") and not isinstance(val, str):
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
                    Inbox(inbox_path(RUNTIME_DIR, "param_server", topic.replace("/", "~"))).submit(r)
                    print(f"[bridge] set-param {topic} {key}={r['value']}", flush=True)
    except Exception:
        pass
    finally:
        for t in list(sub_tasks.values()) + list(listen_tasks.values()):
            t.cancel()
        print("[bridge] WS client disconnected", flush=True)
    return ws


# Camera serving — the video_bridge seam (design doc §6): frames in from a
# camera stream, served out over local HTTP. Two shapes on one encode path:
# /frame/<topic> (single JPEG, for snapshots/agents) and /mjpeg/<topic>
# (multipart/x-mixed-replace push stream — browser-native in a plain <img>,
# no polling, no sampling jitter). Each viewer gets its own depth-1
# subscriber (independent per-subscriber buffers; the recorder's subscription
# is untouched) and drains to newest, so a slow client only ever drops frames.
#
# PERFORMANCE NOTE: JPEG encodes run in the default thread executor so the
# event loop (telemetry + ring-event, i.e. the VERB path) never blocks on
# pixels. At sim scale (128px, ~1 ms/encode) this is noise. At real-robot
# scale (640x480 x N cams x 30 fps ≈ 8 ms/encode each) the bridge process
# will saturate a core and viewer fps will sag first. If that happens, break
# this block out into the dedicated Rust video_bridge the design doc
# originally sketched (os/services/video_bridge is the reference; port it to
# pub/sub streams + turbojpeg). The URL contract (/frame, /mjpeg?fps=) is the
# seam — promotion means a new port and one frontend proxy line, nothing else.
_frame_subs: dict = {}
_frame_cache: dict[str, bytes] = {}
_BOUNDARY = "koyuframe"


def _encode_jpeg(topic: str, v) -> bytes | None:
    """ImageCell-shaped struct -> JPEG bytes; caches per topic. Falls back to
    the cached last frame when nothing new (or nothing image-shaped) arrived."""
    if v is not None and all(hasattr(v, f) for f in ("width", "height", "data")):
        from io import BytesIO

        import numpy as np
        from PIL import Image

        n = v.height * v.width * 3
        img = np.frombuffer(bytes(v.data[:n]), dtype=np.uint8).reshape(v.height, v.width, 3)
        buf = BytesIO()
        Image.fromarray(img).save(buf, format="JPEG", quality=85)
        _frame_cache[topic] = buf.getvalue()
    return _frame_cache.get(topic)


def _stream_info(topic: str):
    info = topic_planes().get(topic)
    return info if info is not None and info[1] == "stream" else None


async def frame_handler(request):
    topic = request.match_info["topic"]
    info = _stream_info(topic)
    if info is None:
        return web.Response(status=404, text=f"unknown stream topic {topic!r}\n")
    sub = _frame_subs.get(topic)
    if sub is None:
        sub = _frame_subs[topic] = _LazySubscriber(topic, types.resolve(info[0]), buffer=1)
    loop = asyncio.get_event_loop()
    jpeg = await loop.run_in_executor(None, _encode_jpeg, topic, sub.latest())
    if jpeg is None:
        return web.Response(status=204)                    # no frame published yet
    return web.Response(body=jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


async def mjpeg_handler(request):
    """One long-lived response per <img>; each newly published frame is pushed
    as a multipart part, capped by ?fps= (default 30). Opens with the cached
    last frame so an idle runtime still shows its most recent view."""
    topic = request.match_info["topic"]
    info = _stream_info(topic)
    if info is None:
        return web.Response(status=404, text=f"unknown stream topic {topic!r}\n")
    try:
        fps = min(max(float(request.query.get("fps", 30)), 1.0), 120.0)
    except ValueError:
        fps = 30.0
    min_dt = 1.0 / fps

    resp = web.StreamResponse(headers={
        "Content-Type": f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        "Cache-Control": "no-store",
    })
    await resp.prepare(request)

    async def push(jpeg: bytes) -> None:
        await resp.write(
            f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n".encode() + jpeg + b"\r\n")

    sub = _LazySubscriber(topic, types.resolve(info[0]), buffer=1)
    loop = asyncio.get_event_loop()
    last_fid = None
    last_push = 0.0
    KEEPALIVE_S = 3.0        # re-push the last frame while idle: keeps refreshes
                             # showing something AND probes the socket so a dead
                             # viewer exits and frees its subscriber slot — an
                             # idle stream must never hold slots forever (each
                             # viewer takes one of the publisher's max_subscribers)
    probe_at = loop.time() + 2.0
    print(f"[bridge] mjpeg viewer on {topic} (fps<={fps:g})", flush=True)
    try:
        cached = _frame_cache.get(topic)
        if cached is not None:
            await push(cached)
        while True:
            if request.transport is None or request.transport.is_closing():
                break                              # viewer went away between frames
            now = loop.time()
            if probe_at is not None and now >= probe_at:
                probe_at = None                    # silent-failure guard, once
                if sub._ensure() is None:
                    print(f"[bridge] mjpeg {topic}: subscriber not connected — "
                          "publisher down, or its max_subscribers slots are full",
                          flush=True)
            if now - last_push >= min_dt:
                v = sub.latest()
                if v is not None and getattr(v, "frame_id", None) != last_fid:
                    jpeg = await loop.run_in_executor(None, _encode_jpeg, topic, v)
                    if jpeg is not None:
                        last_fid, last_push = v.frame_id, now
                        await push(jpeg)
                elif now - last_push >= KEEPALIVE_S:
                    last_push = now
                    if (jpeg := _frame_cache.get(topic)) is not None:
                        await push(jpeg)
            await asyncio.sleep(0.005)
    except (asyncio.CancelledError, ConnectionError):
        pass
    finally:
        print(f"[bridge] mjpeg viewer left {topic}", flush=True)
    return resp


async def context_handler(request):
    """The recording context, read-only: whatever provenance is armed right
    now (koyu context / an orchestrator's flags). Browser surfaces show it so
    the operator can see what the next recording will be stamped with."""
    path = Path(os.environ["KOYU_RUNTIME_DIR"]) / "recording-context.json"
    try:
        return web.json_response(json.loads(path.read_text()))
    except (FileNotFoundError, ValueError):
        return web.json_response({})


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
    app.router.add_get("/frame/{topic:.+}", frame_handler)
    app.router.add_get("/mjpeg/{topic:.+}", mjpeg_handler)
    app.router.add_get("/context", context_handler)
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
