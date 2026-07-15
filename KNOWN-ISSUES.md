# Known Issues

This runtime is currently in alpha and has several known issues, documented below:

- **Data recorder needs polish.** The current logic can abort due to `_frame` aborts. In general, robustness to sources emitting at different clocks, especially when used with streams instead of blackboard, is not great. Paired mode under the current logic is also prone to spurious aborts on stale clock frames.

- **Only ONE runtime is supported concurrently.** This becomes apparent when you realize the boot sweep is machine-global and nukes all iceoryx2 shm.

- **The bridge currently binds on 0.0.0.0**, exposing the runtime to anyone on the LAN. Future improvements will address this security issue.

- **`koyu restart` of a single process can crash iceoryx2 readers.** When the restarted process is publishing iceoryx2 shm data, its readers can crash. Recommend using `koyu restart` only for all processes for now.


- **Provenance is enforced by convention only.** Nothing stops a recording that starts with an empty `recording-context.json`; the episode simply lands unfiled and without lineage. A more rigid mechanism deserves consideration (for example, the recorder warning loudly or refusing START when the context is empty, or eval drivers requiring a source run before rolling), so that evals cannot silently run without proper provenance.
