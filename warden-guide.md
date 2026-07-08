# warden — usage guide

`warden` runs a fleet of processes for one *runtime* and gives you (`koyu …`) a thin
CLI over [supervisord](http://supervisord.org/). You edit one file — `services.yaml` —
and operate everything through the verbs below.

- **Desired state** = `services.yaml` (you edit this).
- **Observed state** = `koyu status` (read live from supervisord).
- `supervisord.conf` is **generated** from `services.yaml` — never hand-edit it.

---

## The runtime folder

A runtime is just a directory containing a `services.yaml`:

```
my-runtime/
  services.yaml        # what you edit  ← the only file you touch
  supervisord.conf     # generated build artifact (gitignored) — do not edit
  .koyu/
    run/               # supervisor.sock + supervisord.pid (written by supervisord)
    logs/<svc>.log     # each service's stdout
    logs/<svc>.err     # each service's stderr
```

**Which runtime does `koyu` act on?** First match wins:
1. `--runtime <dir>` / `-r <dir>`
2. `$KOYU_RUNTIME`
3. the nearest `services.yaml` at or above the current directory
4. none found → it refuses (it never guesses).

**Multiple runtimes** = multiple folders. They're fully isolated (each has its own
socket), so the same service names can run side by side. `cd` into one, or pass `-r`.

---

## services.yaml

Each top-level key is a service. Only `cmd` is required; the rest have sane defaults.

```yaml
sim:
  cmd: ["python3", "sim.py"]      # required (list form). cmd runs with the runtime dir as cwd.
  env:                            # optional plain string map
    LOOP_RATE_HZ: "50"

recorder:
  cmd: ["python3", "-m", "recorder"]
  autorestart: unexpected         # default: restart only on a crash (clean exit stays down)
  startsecs: 1                    # must survive this long to count as RUNNING (default 1)
  startretries: 3                 # give up (→ FATAL) after this many fast crashes (default 3)
  stopwaitsecs: 10                # on stop: SIGTERM, wait, then SIGKILL (default 10)
  exitcodes: [0]                  # which exit codes count as a clean exit (default [0])

migrate:
  cmd: ["python3", "migrate.py"]
  oneshot: true                   # a run-once job: runs, exits 0, is NOT restarted
```

- `cmd` starting with `python`/`python3` runs under warden's own interpreter (venv-safe).
- Auto-injected env vars: `SERVICE_NAME`, `KOYU_RUNTIME_DIR`, `PYTHONUNBUFFERED=1`.
  `SERVICE_NAME` and `KOYU_RUNTIME_DIR` are reserved — you can't set them in `env`.
- Unknown keys (e.g. an `ipc:` block) are **ignored** by warden — they're for the
  services themselves to read.

---

## Commands

| Command | What it does |
|---|---|
| `koyu up` | generate the conf and start the whole runtime |
| `koyu down` | stop the whole runtime |
| `koyu status` | show each service's state (`--json` for machine output) |
| `koyu restart` | bounce the **whole** runtime (services restart; the daemon stays up) |
| `koyu restart <svc>` | restart just one service |
| `koyu apply` | after editing `services.yaml`, reload **only what changed** |
| `koyu logs <svc>` | tail a service's stdout+stderr (`-f` to follow) |

All accept `-r <dir>` to target a specific runtime.

---

## Common tasks

**Add a service** — add a stanza to `services.yaml`, then:
```bash
koyu apply        # only the new service starts; everything already running is untouched
```

**Change an env var (or any field)** — edit the service's `env` in `services.yaml`, then:
```bash
koyu apply        # only that service restarts with the new value; others keep running
```

**Restart**
```bash
koyu restart sim  # one service
koyu restart      # the whole runtime
```

**Check status**
```bash
koyu status
#   sim          RUNNING    pid 20935, uptime 0:03:11
#   recorder     FATAL      Exited too quickly (process log may have details)
#     └─ <spawnerr line: why it failed>
koyu status --json   # same data as JSON
```

**Debug a crash** — `koyu status` shows `FATAL` plus a one-line `spawnerr` (the *why*),
then read the output:
```bash
koyu logs recorder
```

---

## Behaviors worth knowing

- **`apply` vs `up`/`down`.** `apply` is a *hot, surgical* reload — it diffs the conf and
  restarts only the services whose definition changed. `down` then `up` is a full restart
  of everything. Reach for `apply` for edits; `down`/`up` for a clean slate.
- **Never edit `supervisord.conf`.** It's regenerated from `services.yaml` on every
  `up`/`apply`. Your edits there will be overwritten.
- **RUNNING is earned.** A service must survive `startsecs` to be RUNNING. Crash before
  that and it backs off and retries up to `startretries`, then goes `FATAL` (gives up
  loudly — it won't hammer forever).
- **No orphans.** Services are stopped as a group (children die too), with a two-phase
  stop: SIGTERM, wait `stopwaitsecs`, then SIGKILL.
- **`oneshot` for jobs.** Without it, a task that exits quickly-and-cleanly looks like a
  crash-loop to supervisord. `oneshot: true` tells it "this is supposed to exit once."
- **Down is a valid state.** If the runtime isn't up, `koyu status` says so and most verbs
  tell you to `koyu up` first. The control socket *is* the liveness signal.
