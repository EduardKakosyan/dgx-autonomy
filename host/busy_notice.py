#!/usr/bin/python3 -I
"""Answer claude-qwen's clients while the environment holds the DGX inference.

Runs (as a DynamicUser, from dgx-autonomy-busy-notice.service) only while the
reservation record exists, on the address claude-qwen listened on. Every request
gets 503 and a message saying why, in the error shape the client expects: the
Anthropic Messages API for /v1/messages, OpenAI's for everything else. Standard
library only; installed root-owned at /usr/local/lib/dgx-autonomy/busy_notice.py.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MESSAGE = (
    "DGX inference is in use by the autonomous coding environment (dgx-autonomy)."
    " claude-qwen is stopped until the operator runs `dgx-autonomy release` on the DGX."
)


def notice(path: str) -> tuple[int, dict[str, object]]:
    """Status and JSON body for a request to `path`."""
    if path.split("?", 1)[0].rstrip("/").endswith("/messages"):
        return 503, {"type": "error", "error": {"type": "overloaded_error", "message": MESSAGE}}
    return 503, {"error": {"message": MESSAGE, "type": "service_unavailable", "code": 503}}


class Handler(BaseHTTPRequestHandler):
    server_version = "dgx-autonomy-busy-notice"

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= 1024 * 1024:
            self.rfile.read(length)  # drain, so the client sees the reply, not a reset
        status, body = notice(self.path)
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _answer

    def log_message(self, format: str, *args: object) -> None:
        pass  # one line per client request would only fill the journal


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
