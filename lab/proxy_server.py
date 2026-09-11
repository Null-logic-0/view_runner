"""A minimal HTTP proxy, written from scratch so you can see what one does.

    python -m lab.proxy_server --port 3128
    python -m lab.proxy_server --port 3129 --auth bob:secret --delay-ms 200
    python -m lab.proxy_server --port 3130 --fail-every 3

A proxy handles plain HTTP and HTTPS by two completely different mechanisms,
and that difference explains most of what proxies can and cannot do.

PLAIN HTTP -- the client sends an *absolute* URI::

    GET http://target.example/page HTTP/1.1      <- to the proxy
    Host: target.example

    The proxy reads the whole request, rewrites the line to origin form
    (`GET /page HTTP/1.1`), opens its own connection to the target, forwards
    the request, and relays the response back. It sees and could modify
    everything.

HTTPS -- the client sends CONNECT and then speaks TLS through a blind tunnel::

    CONNECT target.example:443 HTTP/1.1          <- to the proxy
                                                 -> HTTP/1.1 200 Connection Established
    <TLS handshake and everything after is opaque bytes>

    The proxy opens a TCP connection and shovels bytes in both directions. It
    knows the hostname and nothing else. This is why a proxy can log your HTTP
    URLs but not your HTTPS ones, and why a "MITM proxy" needs you to trust a
    certificate it generates -- there is no other way in.

Deliberate limitations, because this is an instrument and not a product:

  * One request per connection (`Connection: close` upstream). Correct, but
    not persistent, so it is slower than tinyproxy under load.
  * Request bodies only with Content-Length; no chunked uploads.
  * No caching, no DNS cache, no upstream proxy chaining.

For measuring *our own client* those cost nothing. For production traffic,
use tinyproxy or Squid.
"""

import argparse
import asyncio
import base64
import contextlib
import sys
from dataclasses import dataclass, field
from types import TracebackType
from typing import Final, Self
from urllib.parse import urlsplit

# Headers that describe a single hop and must not be forwarded to the next one.
# Forwarding `Connection: keep-alive` to the target, for instance, would make
# the target's idea of the connection disagree with the client's.
HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

_BUFFER: Final = 64 * 1024
_HEADER_TIMEOUT: Final = 15.0
_MAX_HEADERS: Final = 100


@dataclass
class ProxyStats:
    requests: int = 0  # absolute-URI HTTP requests
    connects: int = 0  # CONNECT tunnels
    injected_failures: int = 0
    auth_failures: int = 0
    upstream_errors: int = 0
    log: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.log.append(line)


class ProxyServer:
    """An HTTP proxy you can point at, break on purpose, and read the log of."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        username: str | None = None,
        password: str | None = None,
        delay_ms: int = 0,
        fail_every: int = 0,
        verbose: bool = False,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._delay_ms = delay_ms
        self._fail_every = fail_every
        self._verbose = verbose
        self._server: asyncio.Server | None = None
        self.stats = ProxyStats()

        self._expected_auth: str | None = None
        if username is not None:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self._expected_auth = f"Basic {token}"

    # lifecycle

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._requested_port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("proxy is not started")
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def address(self) -> str:
        """host:port, the form proxies.txt uses."""
        return f"{self._host}:{self.port}"

    # connection handling

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await self._read_request(reader)
            if request is None:
                return
            method, target, version, headers = request

            if not self._authorised(headers):
                self.stats.auth_failures += 1
                self._note(f"407 {method} {target}")
                await _respond(
                    writer,
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="lab-proxy"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n",
                )
                return

            if self._delay_ms:
                await asyncio.sleep(self._delay_ms / 1000)

            if self._should_inject_failure(method):
                self.stats.injected_failures += 1
                self._note(f"502 (injected) {method} {target}")
                await _respond(
                    writer,
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                return

            if method == "CONNECT":
                await self._tunnel(target, reader, writer)
            else:
                await self._forward(method, target, version, headers, reader, writer)
        except (TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            await _close(writer)

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, str, list[bytes]] | None:
        line = await asyncio.wait_for(reader.readline(), timeout=_HEADER_TIMEOUT)
        if not line.strip():
            return None
        parts = line.decode("latin-1").split()
        if len(parts) != 3:
            return None
        method, target, version = parts

        headers: list[bytes] = []
        for _ in range(_MAX_HEADERS):
            raw = await asyncio.wait_for(reader.readline(), timeout=_HEADER_TIMEOUT)
            if raw in (b"\r\n", b"\n", b""):
                break
            headers.append(raw)
        return method, target, version, headers

    def _authorised(self, headers: list[bytes]) -> bool:
        if self._expected_auth is None:
            return True
        return _header(headers, "proxy-authorization") == self._expected_auth

    def _should_inject_failure(self, method: str) -> bool:
        """Deterministic, not random: the Nth request always fails.

        A random failure rate would make an experiment unrepeatable, which is
        the one thing this bench exists to avoid.
        """
        if not self._fail_every:
            return False
        seen = self.stats.requests + self.stats.connects + self.stats.injected_failures + 1
        return seen % self._fail_every == 0

    # HTTPS: a blind tunnel

    async def _tunnel(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, raw_port = target.rpartition(":")
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, int(raw_port))
        except (OSError, ValueError):
            self.stats.upstream_errors += 1
            self._note(f"502 CONNECT {target}")
            await _respond(writer, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return

        self.stats.connects += 1
        self._note(f"CONNECT {target}")
        await _respond(writer, b"HTTP/1.1 200 Connection Established\r\n\r\n")
        # From here the proxy understands nothing. It moves bytes.
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
        )
        await _close(upstream_writer)

    # HTTP: parse, rewrite, forward

    async def _forward(
        self,
        method: str,
        target: str,
        version: str,
        headers: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parts = urlsplit(target)
        if not parts.hostname:
            await _respond(writer, b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            return

        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                parts.hostname, parts.port or 80
            )
        except OSError:
            self.stats.upstream_errors += 1
            self._note(f"502 {method} {target}")
            await _respond(writer, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return

        self.stats.requests += 1
        self._note(f"{method} {target}")

        # Absolute form -> origin form. This rewrite is the heart of an HTTP
        # proxy: the client addressed us, and we address the target.
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"

        out = [f"{method} {path} {version}\r\n".encode("latin-1")]
        for raw in headers:
            name = raw.split(b":", 1)[0].decode("latin-1").strip().lower()
            if name not in HOP_BY_HOP:
                out.append(raw)
        out.append(b"Via: 1.1 lab-proxy\r\n")
        out.append(b"Connection: close\r\n\r\n")
        upstream_writer.write(b"".join(out))

        length = _header(headers, "content-length")
        if length and length.isdigit() and int(length) > 0:
            upstream_writer.write(await reader.readexactly(int(length)))
        await upstream_writer.drain()

        await _pipe(upstream_reader, writer)
        await _close(upstream_writer)

    def _note(self, line: str) -> None:
        self.stats.note(line)
        if self._verbose:
            print(f"[proxy {self._requested_port or self.port}] {line}", file=sys.stderr)


# Byte plumbing


def _header(headers: list[bytes], name: str) -> str | None:
    prefix = name.encode("latin-1") + b":"
    for raw in headers:
        if raw.lower().startswith(prefix):
            return raw.split(b":", 1)[1].decode("latin-1").strip()
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Move bytes until the source is exhausted."""
    try:
        while chunk := await reader.read(_BUFFER):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, TimeoutError):
        pass
    with contextlib.suppress(OSError):
        if writer.can_write_eof():
            writer.write_eof()


async def _respond(writer: asyncio.StreamWriter, payload: bytes) -> None:
    writer.write(payload)
    with contextlib.suppress(ConnectionResetError, BrokenPipeError):
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, ConnectionResetError, BrokenPipeError):
        writer.close()
        await writer.wait_closed()


# Entry point


async def _serve_forever(server: ProxyServer) -> None:
    async with server:
        print(f"proxy   {server.address}", file=sys.stderr)
        await asyncio.Event().wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab.proxy_server", description="A minimal HTTP proxy.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--auth", metavar="USER:PASS", help="require Basic proxy authentication")
    parser.add_argument("--delay-ms", type=int, default=0, help="add latency to every request")
    parser.add_argument(
        "--fail-every", type=int, default=0, metavar="N", help="fail every Nth request with 502"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    username = password = None
    if args.auth:
        username, _, password = args.auth.partition(":")

    server = ProxyServer(
        args.host,
        args.port,
        username=username,
        password=password,
        delay_ms=args.delay_ms,
        fail_every=args.fail_every,
        verbose=True,
    )
    try:
        asyncio.run(_serve_forever(server))
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
