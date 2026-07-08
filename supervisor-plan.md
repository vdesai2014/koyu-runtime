# koyu-runtime — Supervisor package plan (Phase 1)

Living doc. Iterate here before scaffolding. Scope is **the supervisor package only** —
the domain-agnostic process layer. IPC (iceoryx2, events/pub-sub, type-check, video
bridge) is **Phase 2** and explicitly out of scope below.

---

## 1. What we're building

A thin, **domain-agnostic** supervisor: `supervisord` does process lifecycle; we add a
generator (`services.yaml` → `supervisord.conf`), a boot wrapper, and an agent-facing
CLI. It can boot *any* fleet of processes — robot or not. It knows nothing about
iceoryx2, types, cameras, or topics.

**Litmus test:** the supervisor boots a fleet of plain web services — no `core/types.py`,
no iceoryx2, no `ipc:` block — and works perfectly.

---

## 2. Decisions locked in (from the design chats)

- **Don't rebuild what supervisord solves.** Lifecycle, restart/backoff, group-kill,
  two-phase stop (TERM→wait→KILL) are supervisord's job. We write glue + CLI.
  *(Validated: supervisor 4.3.0 runs on Python 3.12.8.)*
- **One-way dependency.** `ipc` → depends on → `supervisor`. The supervisor **never**
  imports `ipc`. Lint-enforced. Keeps the supervisor liftable to its own repo later (a
  `git mv`), but for now: **separate package, same repo.**
- **Single source of truth.** `services.yaml` is canonical and the only thing edited.
  `supervisord.conf` is a **derived build artifact, never hand-edited** (like a `.o`).
- **A runtime is a folder.** Multi-runtime = different folders, isolated by a per-folder
  control socket (`<dir>/.koyu/run/supervisor.sock`). *Code location ≠ runtime location*:
  the package is installed once; a runtime is any folder with a `services.yaml`.
- **Runtime resolution precedence:** `--runtime <path>` → `KOYU_RUNTIME` env → walk up
  from cwd for `services.yaml` → **else refuse and list candidates** (never guess; these
  move hardware).
- **Always absolute `-c` conf path.** `directory=%(here)s` changes the daemon cwd, so a
  relative conf path breaks `reread` (`CANT_REREAD`). The wrapper resolves to absolute.
- **The supervisor ignores `ipc:`.** Services read their own `ipc:` block from
  `services.yaml` via the injected `SERVICE_NAME` env var. The type-check is an **injected
  hook** (Phase 2), not supervisor code. Stray-SHM cleanup is *not* a global sweep — Phase 2
  isolates iceoryx2 per-runtime so there are no stray segments (see Dropped).
- **Observed state = live `getAllProcessInfo` over the socket.** No `state.json`, no
  eventlistener. The socket *is* the liveness signal; `spawnerr` tells us *why* a program
  is FATAL.

### Dropped / deferred
- **Priority / topology ordering** — *cut.* Control loops are cyclic, so "producers before
  consumers" is unsatisfiable; it cosplays as a safety mechanism but isn't.
  Retry-until-connected (Phase 2) is the real robustness.
- **Global `/tmp/iceoryx2` sweep on boot** — *cut.* A blanket `rm` is a global path and would
  nuke *other* runtimes' segments. It was a band-aid for a buggy old supervisor/IPC; the real
  fix is per-runtime SHM isolation in Phase 2 so stray segments never accumulate.
- **`koyu ls` + `~/.koyu/runtimes` breadcrumb** — deferred. Pure folder-binding ships first.
- **Extending the iox2 CLI** — no. Separate `koyu` CLI; simpler.
- **Phase-2 composition wiring** (how `ipc` injects its hooks) — deferred; Phase 1 ships
  empty hook registries proven with fakes.

---

## 3. File layout

```
koyu-runtime/                  ← repo root (in dev, also a runtime instance)
  services.yaml                ← THIS runtime's desired state (at cwd)
  supervisord.conf             ← generated build artifact (gitignored); at ROOT so %(here)s = runtime dir
  .koyu/                       ← generated runtime state (gitignored)
    run/{supervisor.sock, supervisord.pid}   ← written by supervisord (boot just mkdir's run/)
    logs/<service>.log                       ← written by supervisord (boot just mkdir's logs/)
  pyproject.toml               ← installs the `koyu` console script
  warden/                      ← the agnostic process layer — imported as `warden`
    cli.py                     ← argparse: up/down/status/restart/apply/logs    ✅ built, tested
    boot.py                    ← validate → hooks → generate → launch           ✅ built, tested
    conf_generator.py          ← ServiceSpecs → supervisord.conf (pure)         ✅ built, tested
    services.py                ← parse + validate services.yaml → ServiceSpec   ✅ built, tested
    runtime_dir.py             ← locate the runtime dir + derive .koyu/ paths   ✅ built, tested
    supervisord_client.py      ← XML-RPC client over the socket                 ✅ built, tested
    hooks.py                   ← validator/cleanup registry                     ✅ built
    tests/                     ← 65 passing (unit + cli + real-supervisord integration)
      test_boot_integration.py                                                  ✅ passing
  ipc/                         ← (Phase 2) imported as `ipc`; depends on `warden`
```

> **Naming:** the package is top-level **`warden`** — our thin wrapper that *keeps* the
> processes; `supervisord` is the engine it drives. We avoid the name `supervisor` on purpose:
> the supervisord pip package owns that import namespace (the conf references
> `supervisor.rpcinterface`). **Convention for this doc: `warden` = our package, `supervisord`
> = the engine.** Phase 2's robot layer is `ipc`, depends on `warden`, never the reverse.
> (Three names, on purpose: repo = `koyu-runtime`, CLI you type = `koyu`, package = `warden`.)

`services.py` vs `hooks.py`: **services = "what this runtime declares" (data, my own schema);
hooks = "what other packages inject into boot" (behavior, IPC fills it).**

### 3.1 The runtime dir is resolved ONCE (single source of truth)

The absolute runtime dir touches many things (conf path, socket, pidfile, logs, `directory=`,
`KOYU_RUNTIME_DIR`, the `-c` flag). To keep that from scattering, **two mechanisms converge:**

- **`runtime_dir.py` resolves it exactly once** (the flag→env→cwd→absolute dance) into a single
  `Runtime` value. Every other module *receives* that `Runtime` and reads paths off it
  (`rt.conf`, `rt.socket`, `rt.pidfile`, `rt.logs`, `rt.services_yaml`) — nothing re-resolves
  or re-derives the dir. Dependency-injected, never recomputed.
- **`%(here)s` inside the generated conf** — supervisord computes every conf-internal path
  (socket, pidfile, logs, `directory`) relative to the conf's *own* location at daemon runtime.
  So the artifact hardcodes no absolute path; it's relocatable.

These agree because the conf lives *inside* the runtime dir. The only absolute string passed
around is `-c <abs conf>`, straight off the one `Runtime`. So: **one resolver, one `Runtime`
object, `%(here)s` on the supervisord side.** That's the whole consolidation.

---

## 4. `services.yaml` schema (what the supervisor understands)

```yaml
<service-name>:
  cmd: ["python3", "-m", "services.foo"]   # REQUIRED (list form; relative to runtime dir)
  env:                                      # optional, plain string map
    LOOP_RATE_HZ: "50"
  # restart policy — all optional, house defaults in (§5):
  autostart: true            # default true
  autorestart: unexpected    # default "unexpected" (clean exits stay down)
  startsecs: 1               # default 1 (RUNNING earned by surviving)
  startretries: 3            # default 3 → FATAL (give up loudly)
  stopwaitsecs: 10           # default 10
  exitcodes: [0]             # default [0]
  oneshot: false             # run-once JOB (not a long-running svc) → startsecs=0, autorestart=false

  ipc: {...}                 # IGNORED by supervisor; read by the service + IPC validator
```

- **Required:** `cmd`. Everything else optional.
- **Unknown keys (e.g. `ipc:`) are ignored**, not errors — that's the seam.
- Validation rejects: missing/empty `cmd`, non-map `env`, bad `autorestart` value.

---

## 5. Generated `supervisord.conf` (house defaults + gotchas handled)

Global sections (once per runtime, `%(here)s` = the runtime dir so it's relocatable):

```ini
[unix_http_server]
file=%(here)s/.koyu/run/supervisor.sock
[supervisord]
pidfile=%(here)s/.koyu/run/supervisord.pid
logfile=%(here)s/.koyu/run/supervisord.log
directory=%(here)s
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://%(here)s/.koyu/run/supervisor.sock
```

Per program, generated from each service stanza:

```ini
[program:commander]
command=<cmd>                       ; python/python3 → sys.executable
directory=%(here)s
environment=PYTHONUNBUFFERED="1",SERVICE_NAME="commander",KOYU_RUNTIME_DIR="%(here)s",<env...>
autostart=true
autorestart=unexpected
exitcodes=0
startsecs=1
startretries=3
stopasgroup=true                    ; group-kill, no orphans
killasgroup=true
stopwaitsecs=10                     ; TERM → wait → KILL
stdout_logfile=%(here)s/.koyu/logs/commander.log
stderr_logfile=%(here)s/.koyu/logs/commander.err
```

Gotchas the generator handles unconditionally:
- `%` → `%%` in all values (supervisord %-expansion).
- `PYTHONUNBUFFERED=1` on every program (pipe block-buffering swallows logs).
- Inject `SERVICE_NAME` + `KOYU_RUNTIME_DIR` so the service can find its own `services.yaml`
  / `ipc:` block and write under `.koyu/`.
- `oneshot: true` → `startsecs=0, autorestart=false` (else a fast clean exit reads as BACKOFF).
- Double-quote all `environment=` values.

---

## 6. Boot sequence (`koyu up`)

```
1. resolve runtime folder         (runtime_dir.py: flag → env → cwd walk-up → else refuse)
2. load + validate services.yaml  (services.py)
3. run registered validators      (hooks.py)  ← Phase 2 IPC type-check plugs in HERE
4. run registered cleanups        (hooks.py)  ← Phase 2 per-runtime cleanups (NEVER a global rm)
5. generate .koyu/supervisord.conf (conf_generator.py)
6. launch supervisord -c <ABS conf> (boot.py / subprocess)
```

Any validator failure → abort *before* launch, exit nonzero with the reason. In Phase 1
steps 3–4 run empty lists; a **fake** validator/cleanup in tests proves the seam.

`koyu down` = `supervisor.shutdown()` over the socket. A full boot (`down` then `up`) is the
"generation boundary" where cleanups run; `apply` (below) is a surgical reload that doesn't.

---

## 7. CLI verbs (all folder-scoped)

| Verb | Does | Talks to |
|---|---|---|
| `up` | boot sequence (§6) | generator + subprocess |
| `down` | `supervisor.shutdown()` | socket |
| `status [--json]` | `getAllProcessInfo()` → statename, pid, uptime, **spawnerr** | socket |
| `restart` | bounce the **whole runtime** — all programs, daemon stays up (you do this a lot) | socket |
| `restart <svc>` | stop+start one program | socket |
| `apply` | regenerate conf → `reloadConfig` (reread) → add/remove changed groups (update) | generator + socket |
| `logs <svc> [-f]` | tail `.koyu/logs/<svc>.{log,err}` | filesystem |

`apply` is the change-config verb: edit `services.yaml` → `koyu apply` → only changed
programs restart (proven in the coupon test: `commander` restarted, others kept pid/uptime).

---

## 8. Module line estimates (excl. tests)

| Module | ~lines |
|---|---|
| services.py | 50 |
| conf_generator.py | 100 |
| boot.py | 80 |
| runtime_dir.py | 40 |
| supervisord_client.py | 60 |
| cli.py | 120 |
| hooks.py | 25 |
| **total** | **~475** (band 400–600) |

For reference, the old custom supervisor being deleted was ~815 lines and a worse process
manager.

---

## 9. Tests (`warden/tests/`, travel with the package)

- **`test_conf_generator.py`** — pure units: yaml dict → conf text. Asserts defaults stamped,
  `%%` escaping, `%(here)s` paths, `SERVICE_NAME`/`KOYU_RUNTIME_DIR` injected, `oneshot`
  expansion. *(Highest value, fastest.)*
- **`test_services.py`** — validation: rejects missing `cmd`, ignores `ipc:`, accepts/normalizes
  restart fields.
- **`test_boot_integration.py`** — boots a tiny **fake** runtime (coupon-test style: services
  print env on a loop). Asserts: whole fleet starts; edit env → `apply` restarts only the
  changed program; `restart` changes pid. Plus a **fake validator** that aborts boot →
  proves the hooks seam. No iceoryx2 anywhere.

---

## 10. Phase 2 preview (out of scope, for continuity)

The `ipc` package will *register into* this supervisor's hooks (never modify it):
- **validator:** the cross-language struct type-check (lift `_check_ipc_types` +
  `_parse_type_check_output` from old OS; reads `ipc:` blocks + `core/types.py` + shells the
  Rust binaries).
- **cleanup:** per-runtime SHM isolation so segments are scoped to a runtime — no global
  sweep needed (the old global `rm` is cut).
- the four planes (pub/sub streams, blackboard current-truth, events, request-response),
  `get()`-raises-on-undeclared enforcement, and the binding coupon tests
  (Listener/WaitSet/deadline/pub-sub history — the genuine unknowns).

---

## 11. Decisions (resolved)

1. **CLI name:** `koyu`. ✓
2. **Generated-state dir:** `.koyu/` (hidden, gitignored: conf + run + logs). ✓
3. **`cmd` path base:** relative to the runtime dir (`directory=%(here)s`, runtime dir on
   PYTHONPATH) — runtime folder is self-contained. ✓ All path derivation funnels through the
   single resolved `Runtime` (see §3.1).

### Known edge, deferred
- **Orphan reaping after a hard `kill -9` of supervisord.** Normal shutdown is covered by
  `stopasgroup`/`killasgroup` (group-kill, no orphans). If the daemon is SIGKILL'd, its
  children can outlive it. The old OS had `reap_runtime_processes()` for this; we can add a
  boot-time stray check later if it bites. Not Phase 1.
```
