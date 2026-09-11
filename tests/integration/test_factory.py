"""Integration tests for BrowserFactory. These launch a real browser.

Run only these:      uv run pytest -m integration
Skip them entirely:  uv run pytest -m "not integration"

They are separated from the unit suite because they cost roughly a second each
rather than a microsecond. A test suite you stop running because it is slow
protects nothing.
"""

import pytest
from playwright.async_api import Error as PlaywrightError

from app.browser.factory import BrowserFactory
from app.errors import BrowserLaunchError
from app.proxy.parser import Proxy
from lab.target_server import TargetServer
from tests.conftest import make_config

pytestmark = pytest.mark.integration

# Nothing listens on port 1. If the proxy setting is honoured, every request
# through this proxy must fail; if it is silently ignored, they would succeed.
DEAD_PROXY = Proxy(host="127.0.0.1", port=1)


async def test_factory_starts_and_stops() -> None:
    # Captured as values rather than asserted in place: mypy narrows a property
    # after `assert x is False` and then calls the later `is True` unreachable.
    factory = BrowserFactory(make_config())
    before = factory.started
    async with factory:
        during = factory.started
        connected = factory.browser.is_connected()
    after = factory.started

    assert (before, during, after) == (False, True, False)
    assert connected is True


async def test_browser_property_fails_clearly_before_start() -> None:
    with pytest.raises(BrowserLaunchError, match="not started"):
        _ = BrowserFactory(make_config()).browser


async def test_stop_is_safe_to_call_twice() -> None:
    factory = BrowserFactory(make_config())
    await factory.start()
    await factory.stop()
    await factory.stop()
    assert factory.started is False


async def test_start_is_idempotent() -> None:
    async with BrowserFactory(make_config()) as factory:
        first = factory.browser
        await factory.start()
        assert factory.browser is first


async def test_an_impossible_launch_timeout_raises_browser_launch_error() -> None:
    """A 1 ms budget cannot start Chromium; the Playwright error must be wrapped."""
    with pytest.raises(BrowserLaunchError, match="could not launch chromium"):
        async with BrowserFactory(make_config(timeouts={"launch_ms": 1})):
            pass  # pragma: no cover - launch must fail before reaching here


# ------------------------------------------------------------ real traffic --


async def test_a_page_really_loads_and_the_server_really_sees_it(
    local_server: TargetServer,
) -> None:
    """Ground truth on both ends: the client says 200, the server logged the hit."""
    async with BrowserFactory(make_config()) as factory, factory.new_context() as context:
        page = await context.new_page()
        response = await page.goto(f"{local_server.base_url}/hello")

        assert response is not None
        assert response.status == 200
        assert await page.title() == "bench"

    assert local_server.paths == ["/hello"]


async def test_viewport_is_applied_to_the_page(local_server: TargetServer) -> None:
    config = make_config(browser={"viewport_width": 640, "viewport_height": 480})
    async with BrowserFactory(config) as factory, factory.new_context() as context:
        page = await context.new_page()
        await page.goto(local_server.base_url)
        assert await page.evaluate("window.innerWidth") == 640
        assert await page.evaluate("window.innerHeight") == 480


async def test_user_agent_is_applied_to_the_page(local_server: TargetServer) -> None:
    config = make_config(browser={"user_agent": "browser-automation-lab/0.1.0"})
    async with BrowserFactory(config) as factory, factory.new_context() as context:
        page = await context.new_page()
        await page.goto(local_server.base_url)
        assert await page.evaluate("navigator.userAgent") == "browser-automation-lab/0.1.0"


async def test_locale_is_applied_to_the_page(local_server: TargetServer) -> None:
    config = make_config(browser={"locale": "de-DE"})
    async with BrowserFactory(config) as factory, factory.new_context() as context:
        page = await context.new_page()
        await page.goto(local_server.base_url)
        assert await page.evaluate("navigator.language") == "de-DE"


async def test_contexts_do_not_share_cookies(local_server: TargetServer) -> None:
    """Isolation is the reason a context is the right unit for a session."""
    async with BrowserFactory(make_config()) as factory, factory.new_context() as first:
        await first.add_cookies([{"name": "session", "value": "abc", "url": local_server.base_url}])
        assert len(await first.cookies()) == 1

        async with factory.new_context() as second:
            assert await second.cookies() == []


# ---------------------------------------------------------------- cleanup --


async def test_context_is_closed_even_when_the_body_raises() -> None:
    """The failure the old project had: cleanup skipped on the error path."""
    async with BrowserFactory(make_config()) as factory:
        with pytest.raises(RuntimeError, match="boom"):
            async with factory.new_context():
                assert len(factory.browser.contexts) == 1
                raise RuntimeError("boom")

        assert factory.browser.contexts == []


async def test_contexts_do_not_accumulate_across_sessions() -> None:
    async with BrowserFactory(make_config()) as factory:
        for _ in range(3):
            async with factory.new_context():
                pass
        assert factory.browser.contexts == []


# ------------------------------------------------------------------ proxy --


async def test_a_dead_proxy_makes_navigation_fail(local_server: TargetServer) -> None:
    """Proves the per-context proxy is actually wired up.

    If Playwright ignored the setting, this navigation would succeed -- the
    target is on loopback and reachable. It must not succeed.
    """
    factory = BrowserFactory(make_config())
    async with factory, factory.new_context(DEAD_PROXY) as context:
        page = await context.new_page()
        with pytest.raises(PlaywrightError, match="ERR_PROXY_CONNECTION_FAILED"):
            await page.goto(local_server.base_url, timeout=8000)

    assert local_server.paths == [], "request reached the server, so the proxy was bypassed"


async def test_proxy_applies_per_context_not_per_browser(local_server: TargetServer) -> None:
    """One browser process, two contexts, different proxy settings.

    This is the capability Playwright was chosen for: with Selenium the proxy
    is a browser launch argument, so two proxies means two browser processes.
    """
    async with BrowserFactory(make_config()) as factory:
        async with factory.new_context(DEAD_PROXY) as proxied:
            page = await proxied.new_page()
            with pytest.raises(PlaywrightError, match="ERR_PROXY_CONNECTION_FAILED"):
                await page.goto(local_server.base_url, timeout=8000)

        async with factory.new_context() as direct:
            page = await direct.new_page()
            response = await page.goto(local_server.base_url)
            assert response is not None
            assert response.status == 200

    assert local_server.paths == ["/"]
