"""Tests for the lab bench itself: the target and the proxy.

Real sockets, no browser, so these live in the fast suite. An instrument that
is not itself verified cannot be trusted to verify anything else -- if the
proxy silently failed to proxy, every proxy measurement in every experiment
would be a measurement of nothing.

Blocking HTTP clients are driven through asyncio.to_thread. Calling urllib
directly from a test would block the event loop the proxy runs on, and the
proxy would never get a chance to answer -- a deadlock, not a slow test.
"""

import asyncio
import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from lab.proxy_server import HOP_BY_HOP, ProxyServer
from lab.target_server import TargetServer


def _get(url: str, proxy: str | None = None, auth: str | None = None) -> tuple[int, str]:
    handler = urllib.request.ProxyHandler({"http": f"http://{proxy}"} if proxy else {})
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url)
    if auth:
        token = base64.b64encode(auth.encode()).decode()
        request.add_header("Proxy-Authorization", f"Basic {token}")
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


async def get(url: str, proxy: str | None = None, auth: str | None = None) -> tuple[int, str]:
    return await asyncio.to_thread(_get, url, proxy, auth)


# --------------------------------------------------------------- the target --


async def test_the_target_serves_a_page(local_server: TargetServer) -> None:
    status, body = await get(local_server.base_url + "/")
    assert status == 200
    assert "<title>bench</title>" in body


async def test_slow_really_is_slow(local_server: TargetServer) -> None:
    """This endpoint is what calibrates navigation_ms: a delay with a known answer."""
    began = time.monotonic()
    status, _ = await get(local_server.base_url + "/slow?ms=300")
    elapsed_ms = (time.monotonic() - began) * 1000

    assert status == 200
    assert 300 <= elapsed_ms < 2000


@pytest.mark.parametrize("code", [200, 404, 500, 503])
async def test_status_returns_what_it_is_asked_for(local_server: TargetServer, code: int) -> None:
    status, _ = await get(f"{local_server.base_url}/status?code={code}")
    assert status == code


async def test_heavy_references_the_requested_number_of_subresources(
    local_server: TargetServer,
) -> None:
    """Gives wait_until something to actually wait for."""
    _, body = await get(local_server.base_url + "/heavy?n=7")
    assert body.count("<img") == 7


async def test_flaky_is_deterministic_not_random(local_server: TargetServer) -> None:
    """An experiment you cannot repeat is not an experiment."""
    first = [(await get(f"{local_server.base_url}/flaky?fail=2&key=a"))[0] for _ in range(4)]
    second = [(await get(f"{local_server.base_url}/flaky?fail=2&key=b"))[0] for _ in range(4)]
    assert first == [503, 503, 200, 200]
    assert second == first


async def test_the_target_records_what_it_saw(local_server: TargetServer) -> None:
    await get(local_server.base_url + "/one")
    await get(local_server.base_url + "/two")

    assert local_server.paths == ["/one", "/two"]
    assert all(hit.status == 200 for hit in local_server.hits)
    assert all(not hit.via_proxy for hit in local_server.hits)


async def test_observing_the_stats_does_not_change_them(local_server: TargetServer) -> None:
    await get(local_server.base_url + "/")
    status, body = await get(local_server.base_url + "/__stats")

    assert status == 200
    assert json.loads(body)["requests"] == 1
    assert len(local_server.hits) == 1  # the /__stats call itself is not counted


async def test_the_access_log_is_written_as_it_happens(tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with TargetServer(access_log=log) as server:
        await get(server.base_url + "/a")
        await get(server.base_url + "/b")

    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [row["path"] for row in rows] == ["/a", "/b"]
    assert rows[0]["status"] == 200


# ---------------------------------------------------------------- the proxy --


async def test_the_proxy_forwards_http_and_both_ends_agree(local_server: TargetServer) -> None:
    """Ground truth at both ends: the proxy logged it, the target received it."""
    async with ProxyServer() as proxy:
        status, _ = await get(local_server.base_url + "/page", proxy=proxy.address)

    assert status == 200
    assert proxy.stats.requests == 1
    # The client addressed the proxy with an absolute URI...
    assert proxy.stats.log == [f"GET {local_server.base_url}/page"]
    # ...and the target received an ordinary request marked as proxied.
    assert local_server.paths == ["/page"]
    assert local_server.hits[0].via_proxy is True


async def test_the_proxy_adds_a_via_header(local_server: TargetServer) -> None:
    async with ProxyServer() as proxy:
        _, body = await get(local_server.base_url + "/__headers", proxy=proxy.address)
    assert "lab-proxy" in json.loads(body)["via"]


async def test_hop_by_hop_headers_are_not_forwarded(local_server: TargetServer) -> None:
    """`Proxy-Authorization` describes the client-to-proxy hop only. Forwarding
    it would hand the target credentials that were never meant for it."""
    async with ProxyServer(username="bob", password="hunter2") as proxy:
        _, body = await get(
            local_server.base_url + "/__headers", proxy=proxy.address, auth="bob:hunter2"
        )

    received = json.loads(body)
    assert "proxy-authorization" not in received
    assert not (set(received) & HOP_BY_HOP - {"connection"})


async def test_authentication_is_enforced(local_server: TargetServer) -> None:
    async with ProxyServer(username="bob", password="hunter2") as proxy:
        missing, _ = await get(local_server.base_url, proxy=proxy.address)
        wrong, _ = await get(local_server.base_url, proxy=proxy.address, auth="bob:nope")
        right, _ = await get(local_server.base_url, proxy=proxy.address, auth="bob:hunter2")

        assert (missing, wrong, right) == (407, 407, 200)
        assert proxy.stats.auth_failures == 2

    assert len(local_server.hits) == 1, "rejected requests must never reach the target"


async def test_failure_injection_is_deterministic(local_server: TargetServer) -> None:
    async with ProxyServer(fail_every=3) as proxy:
        codes = [(await get(local_server.base_url, proxy=proxy.address))[0] for _ in range(6)]

    assert codes == [200, 200, 502, 200, 200, 502]
    assert proxy.stats.injected_failures == 2


async def test_latency_injection_adds_the_configured_delay(local_server: TargetServer) -> None:
    async with ProxyServer(delay_ms=250) as proxy:
        began = time.monotonic()
        await get(local_server.base_url, proxy=proxy.address)
        elapsed_ms = (time.monotonic() - began) * 1000

    assert 250 <= elapsed_ms < 2000


async def test_an_unreachable_upstream_is_a_502(closed_port: int) -> None:
    async with ProxyServer() as proxy:
        status, _ = await get(f"http://127.0.0.1:{closed_port}/", proxy=proxy.address)

    assert status == 502
    assert proxy.stats.upstream_errors == 1


async def test_connect_opens_a_blind_tunnel(local_server: TargetServer) -> None:
    """CONNECT is protocol-agnostic: TLS is merely what usually goes through it.

    The proxy records the host and nothing else -- which is exactly why a proxy
    can log your HTTP URLs but not your HTTPS ones.
    """
    async with ProxyServer() as proxy:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(f"CONNECT 127.0.0.1:{local_server.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()

        assert b"200 Connection Established" in await reader.readline()
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass

        writer.write(b"GET /inside HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await writer.drain()
        assert b"200 OK" in await reader.readline()
        writer.close()

        assert proxy.stats.connects == 1
        assert proxy.stats.requests == 0
        assert proxy.stats.log == [f"CONNECT 127.0.0.1:{local_server.port}"]

    assert local_server.paths == ["/inside"], "the target saw a path the proxy never did"
