"""Shared fixtures.

`local_server` is a minimal stand-in for the lab bench: a real HTTP server on
loopback that records what actually arrived. Having ground truth on the server
side is what lets an integration test assert "the browser really fetched this"
rather than "no exception was raised".
"""

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs

import pytest

from app.config import Config, build_config

_BODY = b"<!doctype html><html><head><title>bench</title></head><body>ok</body></html>"


@dataclass
class LocalServer:
    base_url: str
    hits: list[str] = field(default_factory=list)


@pytest.fixture
def closed_port() -> int:
    """A port on loopback with nothing listening.

    Binding to port 0 lets the OS pick a free one; closing it immediately
    leaves an address that will refuse connections. Do not simply hardcode a
    low port: Chromium refuses to navigate to ~80 "unsafe" ports (1, 7, 22,
    25, 6000 ...) with ERR_UNSAFE_PORT, which is a different failure entirely.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def local_server() -> Iterator[LocalServer]:
    record: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        """Deterministic endpoints, so failures can be caused rather than waited for.

        /                 200, a small HTML page
        /slow?ms=N        200 after N milliseconds  -- for calibrating timings
                                                       and forcing timeouts
        /status?code=N    responds with HTTP N      -- for status handling
        """

        def do_GET(self) -> None:
            record.append(self.path)
            route, _, raw_query = self.path.partition("?")
            query = parse_qs(raw_query)

            if route == "/slow":
                time.sleep(int(query.get("ms", ["0"])[0]) / 1000)
                self._respond(200)
            elif route == "/status":
                self._respond(int(query.get("code", ["200"])[0]))
            else:
                self._respond(200)

        def _respond(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(_BODY)))
            self.end_headers()
            self.wfile.write(_BODY)

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalServer(base_url=f"http://127.0.0.1:{port}", hits=record)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_config(**sections: dict[str, Any]) -> Config:
    """Build a valid Config with targeted overrides.

    Goes through build_config rather than constructing dataclasses directly, so
    tests exercise the same validation the real program does.
    """
    data: dict[str, Any] = {"target": {"url": "http://127.0.0.1:8000/"}}
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return build_config(data)
