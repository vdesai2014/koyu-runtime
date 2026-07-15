"""koyu_runtime — the umbrella package for the runtime's four parts:

  * ``warden``   — supervision glue over supervisord (conf generation, boot, client)
  * ``ipc``      — iceoryx2 wrappers (streams, blackboard, events, the Service base)
  * ``services`` — the first-party services (data_recorder, param_server, ipc_logger, bridge)
  * ``cli``      — the ``koyu`` verbs (plugged into koyu-cli via the koyu.plugins group)

One top-level name so a pip install never collides with anything else's
``services``/``cli``/``ipc`` in site-packages.
"""
