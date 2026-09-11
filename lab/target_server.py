"""A target website you control, with failures you can cause on demand.

    python -m lab.target_server --port 8000

Endpoints::

    /                  200, a small HTML page
    /slow?ms=N         200 after N milliseconds
    /status?code=N     responds with HTTP N
    /heavy?n=N         a page referencing N subresources
    /pixel             a 1x1 GIF (what /heavy references)
    /flaky?fail=N      the first N requests fail with 503, then it recovers
    /__headers         echoes the headers that arrived (see what a proxy did)
    /__stats           JSON request counts (not itself recorded)

Why this exists at all:

    Third-party sites are poor instruments. Their latency, caching, rate limits
    and A/B behaviour drift independently of your experiment, so a change in
    your numbers cannot be attributed. Here you set the latency and you read
    the access log, which means you can do two things you otherwise cannot:

      calibrate   /slow?ms=800 has a known answer. If navigation_ms comes back
                  at 40 ms, your instrument is measuring the wrong thing.

      verify      The lab says 47 sessions completed. Does the access log show
                  47 requests? If not, your telemetry is lying, and against a
                  site you cannot see inside you would never find out.
"""

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import parse_qs, urlsplit

_PIXEL: Final = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)
_MAX_SLOW_MS: Final = 60_000
_MAX_HEAVY: Final = 200


@dataclass(frozen=True, slots=True)
class Hit:
    """One request as the server saw it. This is the ground truth."""

    ts: float
    method: str
    path: str
    status: int
    duration_ms: float
    user_agent: str
    #: True when the request arrived through a proxy. Detected from the `Via`
    #: header a well-behaved proxy adds, or from an absolute-form request line
    #: (`GET http://host/path`), which only ever comes from a proxy.
    via_proxy: bool


@dataclass
class _State:
    hits: list[Hit] = field(default_factory=list)
    flaky_seen: dict[str, int] = field(default_factory=dict)
    access_log: Path | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, hit: Hit) -> None:
        # ThreadingHTTPServer really does use threads, so unlike everything in
        # app/ this genuinely needs a lock.
        with self.lock:
            self.hits.append(hit)
            if self.access_log is not None:
                with self.access_log.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(asdict(hit)) + "\n")


def _page(title: str, body: str = "ok") -> bytes:
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # so keep-alive works and Content-Length matters
    state: _State

    def do_GET(self) -> None:
        started = time.monotonic()
        route, _, raw_query = self.path.partition("?")
        # A proxy may send absolute-form: GET http://host/path HTTP/1.1
        if route.startswith("http://") or route.startswith("https://"):
            route = urlsplit(route).path or "/"
        query = parse_qs(raw_query)

        if route == "/__headers":
            # Echoes what actually arrived. Lets you see exactly what a proxy
            # forwarded, rewrote, added, or stripped.
            received = {name.lower(): value for name, value in self.headers.items()}
            self._send(200, json.dumps(received).encode(), "application/json")
            return

        if route == "/__stats":
            self._send(200, json.dumps(self._stats()).encode(), "application/json")
            return  # deliberately not recorded: observing must not change the count

        status = self._route(route, query)
        self.state.record(
            Hit(
                ts=time.time(),
                method="GET",
                path=self.path,
                status=status,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                user_agent=self.headers.get("User-Agent", ""),
                via_proxy=bool(self.headers.get("Via")) or self.path.startswith("http"),
            )
        )

    def _route(self, route: str, query: dict[str, list[str]]) -> int:
        if route == "/slow":
            time.sleep(min(_int(query, "ms", 0), _MAX_SLOW_MS) / 1000)
            return self._send(200, _page("bench"))
        if route == "/status":
            return self._send(_int(query, "code", 200), _page("bench"))
        if route == "/pixel":
            return self._send(200, _PIXEL, "image/gif")
        if route == "/heavy":
            count = min(_int(query, "n", 20), _MAX_HEAVY)
            images = "".join(f'<img src="/pixel?i={i}">' for i in range(count))
            return self._send(200, _page("heavy", images))
        if route == "/flaky":
            # Deterministic, not random: an experiment you cannot repeat is not
            # an experiment. The Nth request for a given key always behaves the
            # same way.
            budget = _int(query, "fail", 1)
            key = query.get("key", ["default"])[0]
            with self.state.lock:
                seen = self.state.flaky_seen.get(key, 0)
                self.state.flaky_seen[key] = seen + 1
            return self._send(503 if seen < budget else 200, _page("bench"))
        return self._send(200, _page("bench"))

    def _send(self, status: int, body: bytes, content_type: str = "text/html") -> int:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return status

    def _stats(self) -> dict[str, Any]:
        with self.state.lock:
            hits = list(self.state.hits)
        return {
            "requests": len(hits),
            "via_proxy": sum(1 for hit in hits if hit.via_proxy),
            "by_status": _counts(hit.status for hit in hits),
            "by_path": _counts(hit.path.partition("?")[0] for hit in hits),
        }

    def log_message(self, *args: Any) -> None:
        """Silence stderr logging; we keep our own structured record."""


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(query.get(key, [str(default)])[0])
    except (ValueError, IndexError):
        return default


def _counts(values: Iterator[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for value in values:
        out[str(value)] = out.get(str(value), 0) + 1
    return out


class TargetServer:
    """A controllable target, usable as a context manager or standalone.

    Port 0 asks the OS for a free port, so parallel test runs cannot collide.
    """

    def __init__(
        self, host: str = "127.0.0.1", port: int = 0, access_log: Path | None = None
    ) -> None:
        self._host = host
        self._state = _State(access_log=access_log)
        handler = type("_BoundHandler", (_Handler,), {"state": self._state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self.port}"

    @property
    def hits(self) -> list[Hit]:
        with self._state.lock:
            return list(self._state.hits)

    @property
    def paths(self) -> list[str]:
        """Just the request paths, for concise assertions."""
        return [hit.path for hit in self.hits]

    def start(self) -> None:
        # poll_interval, not the 0.5 s default: shutdown() waits for the serve
        # loop to notice the stop flag on its next poll, so the default makes
        # every stop() take half a second. That is invisible when you run the
        # bench by hand and costs ~8 s across a test suite that starts it once
        # per test.
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab.target_server", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--access-log", type=Path, help="append one JSON object per request")
    args = parser.parse_args(argv)

    server = TargetServer(args.host, args.port, access_log=args.access_log)
    with server:
        print(f"target  {server.base_url}", file=sys.stderr)
        print(
            "        /  /slow?ms=N  /status?code=N  /heavy?n=N  /flaky?fail=N"
            "  /__headers  /__stats",
            file=sys.stderr,
        )
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
