"""Event wake endpoint — turn the heartbeat into heartbeat + events.

A tiny HTTP server (stdlib, daemon thread) exposes ``POST /wake``. devclaw's
notify_url points at it, so a finished task pings goalclaw → an immediate tick,
instead of waiting for the next heartbeat. The body is ignored (devclaw posts a
task row; we just need the signal). The serve loop waits on an asyncio.Event the
handler sets, OR the heartbeat interval — whichever comes first.

Internal-network only (compose), no auth — same posture as devclaw-mcp's MCP
endpoint behind the gateway.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


def start_wake_server(port: int, on_wake: Callable[[], None]) -> ThreadingHTTPServer:
    """Start the wake server in a daemon thread. ``on_wake`` is called on each
    POST /wake (it must be thread-safe — typically loop.call_soon_threadsafe)."""

    class _Handler(BaseHTTPRequestHandler):
        def _ok(self) -> None:
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", 0) or 0)
            if length:
                self.rfile.read(length)  # drain devclaw's task-row body
            self._ok()
            if self.path.startswith("/wake"):
                on_wake()

        def do_GET(self) -> None:  # noqa: N802 — /health
            self._ok()

        def log_message(self, *args) -> None:  # silence default logging
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def wake_setter(loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> Callable[[], None]:
    """A thread-safe callback that sets ``event`` on the asyncio ``loop``."""

    def _set() -> None:
        loop.call_soon_threadsafe(event.set)

    return _set
