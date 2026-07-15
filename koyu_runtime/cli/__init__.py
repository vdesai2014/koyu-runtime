"""cli — the composition root and the user/agent hand.

This is the one place that imports across package boundaries (warden + ipc +
journal). It wires the first-party IPC checks into warden's boot and exposes the
operator + introspection commands. warden and ipc stay decoupled; cli connects.
"""
