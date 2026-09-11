"""Shared fixtures.

`local_server` is a minimal stand-in for the lab bench: a real HTTP server on
loopback that records what actually arrived. Having ground truth on the server
side is what lets an integration test assert "the browser really fetched this"
rather than "no exception was raised".
"""

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app.config import Config, build_config

_BODY = b"<!doctype html><html><head><title>bench</title></head><body>ok</body></html>"


@dataclass
class LocalServer:
    base_url: str
    hits: list[str] = field(default_factory=list)


@pytest.fixture
def local_server() -> Iterator[LocalServer]:
    record: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            record.append(self.path)
            self.send_response(200)
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
