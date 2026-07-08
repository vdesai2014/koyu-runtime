"""Talk to a running supervisord over its XML-RPC interface on the unix socket.

A ``SupervisordClient`` connects to one runtime's ``supervisor.sock`` and exposes
the operations the CLI needs: read process state, start/stop/restart, apply a
config change, and shut the daemon down. Each call opens a fresh connection and
closes it again (``_server``), so no connection state is shared between calls and
no socket is leaked.
"""

from __future__ import annotations

import http.client
import xmlrpc.client
from contextlib import contextmanager
from pathlib import Path

from supervisor.xmlrpc import Faults, SupervisorTransport

# Connection-level failures (vs. a Fault, which is supervisord rejecting a valid call).
_UNREACHABLE = (OSError, http.client.HTTPException, xmlrpc.client.ProtocolError)


class SupervisordError(Exception):
    """Raised when supervisord can't be reached or rejects a call."""


class SupervisordClient:
    """A thin client over a runtime's supervisord XML-RPC interface."""

    def __init__(self, socket_path: str | Path):
        self._socket_path = str(socket_path)
        self._serverurl = f"unix://{self._socket_path}"

    @contextmanager
    def _server(self):
        """Yield a fresh supervisor proxy, closing its connection afterward."""
        transport = SupervisorTransport(None, None, self._serverurl)
        try:
            # The host in the URL is ignored for a unix transport, but ServerProxy needs one.
            yield xmlrpc.client.ServerProxy("http://localhost", transport=transport).supervisor
        finally:
            transport.close()

    def is_running(self) -> bool:
        """True if a supervisord daemon is listening on the socket."""
        try:
            with self._server() as s:
                s.getState()
        except (*_UNREACHABLE, xmlrpc.client.Fault):
            return False
        return True

    def process_info(self) -> list[dict]:
        """Per-program state dicts: name, statename, pid, description, spawnerr, ..."""
        return self._call("status", "getAllProcessInfo")

    def start_process(self, name: str) -> None:
        self._call(f"start {name}", "startProcess", name)

    def stop_process(self, name: str) -> None:
        self._call(f"stop {name}", "stopProcess", name)

    def restart_process(self, name: str) -> None:
        self._tolerate(Faults.NOT_RUNNING, f"stop {name}", "stopProcess", name)
        self._call(f"start {name}", "startProcess", name)

    def restart_all(self) -> None:
        self._call("stop all", "stopAllProcesses")
        self._call("start all", "startAllProcesses")

    def apply(self) -> dict[str, list[str]]:
        """Reread the conf and reconcile groups, restarting only changed ones."""
        added, changed, removed = self._call("reload", "reloadConfig")[0]
        for name in removed:
            self._remove_group(name)
        for name in changed:
            self._remove_group(name)
            self._add_group(name)
        for name in added:
            self._add_group(name)
        return {"added": added, "changed": changed, "removed": removed}

    def shutdown(self) -> None:
        self._call("shutdown", "shutdown")

    def _add_group(self, name: str) -> None:
        self._call(f"add {name}", "addProcessGroup", name)

    def _remove_group(self, name: str) -> None:
        self._call(f"stop {name}", "stopProcessGroup", name)
        self._call(f"remove {name}", "removeProcessGroup", name)

    def _call(self, what: str, method: str, *args):
        try:
            with self._server() as s:
                return getattr(s, method)(*args)
        except xmlrpc.client.Fault as fault:
            raise SupervisordError(f"{what}: {fault.faultString}") from fault
        except _UNREACHABLE as err:
            raise SupervisordError(
                f"{what}: cannot reach supervisord at {self._socket_path}: {err}"
            ) from err

    def _tolerate(self, code: int, what: str, method: str, *args):
        try:
            with self._server() as s:
                return getattr(s, method)(*args)
        except xmlrpc.client.Fault as fault:
            if fault.faultCode == code:
                return None
            raise SupervisordError(f"{what}: {fault.faultString}") from fault
        except _UNREACHABLE as err:
            raise SupervisordError(
                f"{what}: cannot reach supervisord at {self._socket_path}: {err}"
            ) from err
