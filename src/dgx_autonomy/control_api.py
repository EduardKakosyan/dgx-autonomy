"""JSON over a unix socket: the only way into the controller.

One request per connection, one JSON line each way:

    -> {"op": "status", "args": {"run_id": "..."}}
    <- {"ok": true, "result": {...}}   |   {"ok": false, "error": "..."}

There is no TCP listener. The socket is created 0600 and handed to the operator
account (jim), so reaching the controller needs neither root nor the docker group,
and nobody else on the host can talk to it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import socketserver
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

log = logging.getLogger("dgx_autonomy.control_api")

Handler = Callable[[str, Mapping[str, Any]], Any]
MAX_REQUEST_BYTES = 1024 * 1024


class ControlError(RuntimeError):
    """The controller refused the request or could not be reached."""


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    handler: Handler


class _RequestHandler(socketserver.StreamRequestHandler):
    server: _Server

    def handle(self) -> None:
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        try:
            if len(line) > MAX_REQUEST_BYTES:
                raise ValueError("request too large")
            req = json.loads(line)
            if not isinstance(req, dict) or not isinstance(req.get("op"), str):
                raise ValueError("expected {'op': str, 'args': {...}}")
            args = req.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args must be an object")
            reply: dict[str, Any] = {"ok": True, "result": self.server.handler(req["op"], args)}
        except ValueError as exc:  # includes RequestError and JSON errors
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("control request failed")
            reply = {"ok": False, "error": f"internal error: {type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(reply, default=str).encode() + b"\n")


class ControlServer:
    def __init__(
        self,
        path: Path,
        handler: Handler,
        *,
        owner_uid: int | None = None,
        owner_gid: int | None = None,
    ) -> None:
        self.path = path
        self._handler = handler
        self._owner = (owner_uid, owner_gid)
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o755)
        if self.path.is_socket() or self.path.exists():
            self.path.unlink()
        old = os.umask(0o177)  # the socket is born 0600: no window where others can connect
        try:
            server = _Server(str(self.path), _RequestHandler)
        finally:
            os.umask(old)
        server.handler = self._handler
        uid, gid = self._owner
        if uid is not None or gid is not None:
            os.chown(self.path, -1 if uid is None else uid, -1 if gid is None else gid)
        os.chmod(self.path, 0o600)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="control", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()


def call(
    path: Path, op: str, args: Mapping[str, Any] | None = None, *, timeout: float = 60.0
) -> Any:
    """Client side. Raises ControlError with the controller's message on refusal."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(json.dumps({"op": op, "args": dict(args or {})}).encode() + b"\n")
            with sock.makefile("rb") as f:
                line = f.readline()
    except FileNotFoundError:
        raise ControlError(f"no controller socket at {path}; is the controller running?") from None
    except PermissionError:
        raise ControlError(f"permission denied on {path}; it belongs to the operator") from None
    except OSError as exc:
        raise ControlError(f"cannot reach the controller at {path}: {exc}") from None
    if not line:
        raise ControlError("the controller closed the connection without replying")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise ControlError(str(reply.get("error", "unknown error")))
    return reply.get("result")
