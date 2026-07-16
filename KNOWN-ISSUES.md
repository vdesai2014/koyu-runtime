# Known Issues

This runtime is currently in alpha and has several known issues, documented below:

- **Data recorder needs polish.** Paired lockstep capture was hardened (answers are now queued and held by frame_id, so a lockstep faster than the recorder tick can no longer lose samples and abort the episode), but robustness to window sources emitting on different clocks remains lightly tested. The adversarial timing suite sketched in the recorder's TODO is the planned fix.

- **Only ONE runtime is supported concurrently.** This becomes apparent when you realize the boot sweep is machine-global and nukes all iceoryx2 shm.

- **The bridge currently binds on 0.0.0.0**, exposing the runtime to anyone on the LAN. Future improvements will address this security issue.

- **`koyu restart` of a single process is racy.** Restarting a process that publishes iceoryx2 shm data can crash its readers. Restarting a blackboard writer can also kill the replacement process itself: the respawn can beat the old handle's teardown in shared memory, the new writer hits `HandleAlreadyExists`, exits immediately, and supervisord marks it FATAL after its retry budget. Restart the whole runtime instead (`koyu down`, wait a few seconds, `koyu up`), which recreates the segments cleanly. The eventual fix is retry with backoff in writer setup.


- **Provenance is enforced by convention only.** Nothing stops a recording that starts with an empty `recording-context.json`; the episode simply lands unfiled and without lineage. A more rigid mechanism deserves consideration (for example, the recorder warning loudly or refusing START when the context is empty, or eval drivers requiring a source run before rolling), so that evals cannot silently run without proper provenance.
