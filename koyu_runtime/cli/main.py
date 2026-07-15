"""The ``koyu`` command line: operate a runtime via supervisord.

This is the composition root's user surface. ``up``/``apply`` route through
``bring_up``/``reconfigure`` so the IPC type-check runs before anything starts;
everything else drives warden's supervisord client directly.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from koyu_runtime.warden import boot, runtime_dir
from koyu_runtime.warden.runtime_dir import Runtime, RuntimeResolutionError
from koyu_runtime.warden.services import ServicesError
from koyu_runtime.warden.supervisord_client import SupervisordClient, SupervisordError

from .bring_up import bring_up, reconfigure

import yaml

from koyu_runtime.ipc import blackboard, checks
from koyu_runtime.ipc import types as ipc_types
from koyu_runtime.services.inbox import Inbox, inbox_path


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        rt = runtime_dir.resolve(args.runtime)
    except RuntimeResolutionError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    try:
        return args.handler(rt, args)
    except (boot.BootError, SupervisordError, ServicesError,
            checks.TypeCheckError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="koyu", description="Operate a koyu runtime.")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-r",
        "--runtime",
        help="runtime directory (default: $KOYU_RUNTIME, else search up from cwd)",
    )

    p = sub.add_parser("up", parents=[common], help="generate the conf and start supervisord")
    p.set_defaults(handler=_cmd_up)

    p = sub.add_parser("down", parents=[common], help="stop the runtime's supervisord")
    p.set_defaults(handler=_cmd_down)

    p = sub.add_parser("status", parents=[common], help="show each program's state")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(handler=_cmd_status)

    p = sub.add_parser("restart", parents=[common], help="restart the whole runtime, or one service")
    p.add_argument("service", nargs="?", help="service to restart (default: the whole runtime)")
    p.set_defaults(handler=_cmd_restart)

    p = sub.add_parser("apply", parents=[common], help="regenerate the conf and reload changed services")
    p.set_defaults(handler=_cmd_apply)

    p = sub.add_parser("logs", parents=[common], help="tail a service's stdout/stderr")
    p.add_argument("service")
    p.add_argument("-f", "--follow", action="store_true", help="follow new output")
    p.set_defaults(handler=_cmd_logs)

    p = sub.add_parser("set", parents=[common], help="set a param via the param_server inbox")
    p.add_argument("topic")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("--persist", action="store_true", help="save to disk (survives reboot)")
    p.set_defaults(handler=_cmd_set)

    p = sub.add_parser("get", parents=[common], help="read a blackboard topic's live value")
    p.add_argument("topic")
    p.set_defaults(handler=_cmd_get)

    p = sub.add_parser("tail", parents=[common], help="print recent logger records for a topic")
    p.add_argument("topic")
    p.add_argument("-n", type=int, default=20, help="number of records (default 20)")
    p.set_defaults(handler=_cmd_tail)

    p = sub.add_parser("frame", parents=[common], help="path to the newest logged frame for a topic")
    p.add_argument("topic")
    p.set_defaults(handler=_cmd_frame)

    p = sub.add_parser(
        "context", parents=[common],
        help="show or set the recording context (task, manifest filing, provenance)")
    p.add_argument("action", nargs="?", default="show", choices=("show", "set", "clear"))
    p.add_argument("pairs", nargs="*", metavar="key=value",
                   help="fields for set, e.g. source_run_id=run_… policy_name=koyu-ACT "
                        "(empty value clears one field)")
    p.set_defaults(handler=_cmd_context)

    return parser


def _cmd_context(rt: Runtime, args) -> int:
    """Read/write recording-context.json — the file the recorder snapshots at
    every START (see AGENTS.md, the Register provenance route). The file is the contract; this verb is
    sugar over it. Changes apply to recordings started from now on."""
    from pydantic import ValidationError

    from koyu_runtime.services.episode_schema import RecordingContext

    path = rt.dir / "recording-context.json"
    if args.action == "clear":
        path.unlink(missing_ok=True)
        print("recording context cleared — next recordings are unfiled")
        return 0

    try:
        context = RecordingContext.model_validate_json(path.read_text())
    except (FileNotFoundError, ValidationError):
        context = RecordingContext()

    if args.action == "set":
        if not args.pairs:
            print("error: set needs key=value pairs, e.g. "
                  "koyu context set source_run_id=run_… policy_name=koyu-ACT",
                  file=sys.stderr)
            return 2
        valid = RecordingContext.model_fields
        updates = {}
        for pair in args.pairs:
            key, sep, value = pair.partition("=")
            if not sep or key not in valid:
                print(f"error: expected key=value with key one of: "
                      f"{', '.join(valid)} (got {pair!r})", file=sys.stderr)
                return 2
            updates[key] = value or None           # `key=` clears that field
        context = context.model_copy(update=updates)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(context.model_dump_json(indent=2))
        tmp.replace(path)                          # atomic, like everything else

    print(context.model_dump_json(indent=2))
    if all(v is None for v in context.model_dump().values()):
        print("(empty context — recordings will land unfiled; "
              "set at least requested_manifest)", file=sys.stderr)
    elif args.action == "set":
        print("(snapshotted by the recorder at each START — "
              "applies to recordings from now on)", file=sys.stderr)
    return 0


def _cmd_up(rt: Runtime, args) -> int:
    bring_up(rt)
    print(f"runtime up: {rt.dir}")
    _print_status(SupervisordClient(rt.socket))
    return 0


def _cmd_down(rt: Runtime, args) -> int:
    client = SupervisordClient(rt.socket)
    if not client.is_running():
        print(f"runtime already down: {rt.dir}")
        return 0
    boot.down(rt)
    print(f"runtime down: {rt.dir}")
    return 0


def _cmd_status(rt: Runtime, args) -> int:
    client = SupervisordClient(rt.socket)
    if not client.is_running():
        print("[]" if args.json else f"runtime is down: {rt.dir}")
        return 0
    info = client.process_info()
    if args.json:
        print(json.dumps(info, indent=2))
    else:
        _print_status(client, info)
    return 0


def _cmd_restart(rt: Runtime, args) -> int:
    client = SupervisordClient(rt.socket)
    if not client.is_running():
        raise SupervisordError(f"runtime is down: {rt.dir} (run `koyu up`)")
    if args.service:
        client.restart_process(args.service)
        print(f"restarted: {args.service}")
    else:
        client.restart_all()
        print("restarted the whole runtime")
    return 0


def _cmd_apply(rt: Runtime, args) -> int:
    result = reconfigure(rt)
    changes = [f"{verb} {names}" for verb, names in result.items() if names]
    print("applied: " + (", ".join(changes) if changes else "no changes"))
    return 0


def _cmd_logs(rt: Runtime, args) -> int:
    files = [rt.logs_dir / f"{args.service}.log", rt.logs_dir / f"{args.service}.err"]
    present = [str(f) for f in files if f.exists()]
    if not present:
        raise SupervisordError(f"no logs for '{args.service}' in {rt.logs_dir}")
    cmd = ["tail"] + (["-f"] if args.follow else ["-n", "100"]) + present
    return subprocess.run(cmd).returncode


def _slug(topic: str) -> str:
    return topic.replace("/", "~")


def _topic_types(rt: Runtime) -> dict:
    """{topic: type_name} across every service's ipc block in services.yaml."""
    raw = yaml.safe_load(rt.services_yaml.read_text()) or {}
    out: dict = {}
    for stanza in raw.values():
        out.update(checks._typed_topics((stanza or {}).get("ipc") or {}))
    return out


def _cmd_set(rt: Runtime, args) -> int:
    try:
        value = json.loads(args.value)            # 3.5 -> float, true -> bool, ...
    except json.JSONDecodeError:
        value = args.value                        # bare word -> string
    req = {"key": args.key, "value": value}
    if args.persist:
        req["persist"] = True
    Inbox(inbox_path(rt.dir, "param_server", _slug(args.topic))).submit(req)
    print(f"set {args.topic} {args.key}={value!r}" + (" (persist)" if args.persist else ""))
    return 0


def _cmd_get(rt: Runtime, args) -> int:
    tyname = _topic_types(rt).get(args.topic)
    if tyname is None:
        print(f"error: unknown topic {args.topic!r}", file=sys.stderr)
        return 1
    try:
        value = blackboard.Reader(args.topic, ipc_types.resolve(tyname)).read()
    except Exception as exc:
        print(f"error: {args.topic} isn't a readable blackboard ({type(exc).__name__}); "
              f"for streams use `koyu tail`", file=sys.stderr)
        return 1
    if value is None:
        print("null")
    else:
        out = {}
        for f, _ in value._fields_:
            v = getattr(value, f)
            if isinstance(v, bytes):                  # c_char fields (capture_id, ...)
                out[f] = v.decode("utf-8", errors="replace")
            elif not hasattr(v, "__len__"):           # scalars; arrays are skipped
                out[f] = v
        print(json.dumps(out))
    return 0


def _cmd_tail(rt: Runtime, args) -> int:
    base = rt.dir / "services" / "ipc_logger" / _slug(args.topic)
    log = base / "state.jsonl"
    if not log.exists():
        log = base / "events.jsonl"
    if not log.exists():
        print(f"error: no log for {args.topic!r} (is the logger watching it?)", file=sys.stderr)
        return 1
    for line in log.read_text().splitlines()[-args.n:]:
        print(line)
    return 0


def _cmd_frame(rt: Runtime, args) -> int:
    frames = sorted((rt.dir / "services" / "ipc_logger" / _slug(args.topic) / "frames").glob("*.jpg"))
    if not frames:
        print(f"error: no frames for {args.topic!r}", file=sys.stderr)
        return 1
    print(frames[-1])            # newest; the agent then Reads this path to see it
    return 0


def _print_status(client: SupervisordClient, info=None) -> None:
    if info is None:
        info = client.process_info()
    for p in sorted(info, key=lambda x: x["name"]):
        print(f"  {p['name']:<24} {p['statename']:<10} {p.get('description', '')}")
        if p.get("spawnerr"):
            print(f"    └─ {p['spawnerr']}")


if __name__ == "__main__":
    sys.exit(main())
