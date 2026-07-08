"""warden — a domain-agnostic process layer over supervisord.

Loads a runtime's services.yaml, generates a supervisord.conf, and exposes
boot/apply/down plus a supervisord client to operate and inspect the fleet. It
knows nothing about IPC. The CLI that drives it lives in the top-level `cli`
package, which is also where IPC checks are wired in.
"""
