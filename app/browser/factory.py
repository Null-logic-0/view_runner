"""Turn configuration into a running browser and configured contexts.

    BrowserFactory  ->  Browser (one process)  ->  BrowserContext (per session)

What the factory owns:
    the Playwright driver process, the browser process, launch arguments,
    and every per-context setting -- viewport, locale, user agent, proxy,
    default timeouts.

What it does not own:
    navigation, dwell time, metrics, retries, how many sessions to run. It
    hands back a configured context and stops caring. If experiment logic
    appears in this file, the design has leaked.

Layering, for orientation::

    BrowserFactory
        -> playwright driver (a bundled Node process, spoken to over stdio)
        -> Chrome DevTools Protocol over a persistent WebSocket
        -> Chromium process
        -> BrowserContext   isolated cookies/cache/storage, own proxy
        -> Page             a tab
        -> HTTP/TLS -> target
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

from playwright.async_api import Browser, BrowserContext, BrowserType, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from app.config import Config
from app.errors import BrowserLaunchError
from app.proxy.parser import Proxy

# --------------------------------------------------------------------------- #
# Pure option builders                                                         #
# --------------------------------------------------------------------------- #
# Kept as free functions rather than methods so the config-to-arguments mapping
# can be unit tested in microseconds without launching anything. This is the
# testability seam of the whole module: the risky part (process management) is
# small, and the fiddly part (option assembly) needs no browser to verify.


def launch_options(config: Config) -> dict[str, Any]:
    """Arguments for `browser_type.launch()`.

    Note what is absent: `--no-sandbox`. The old project passed it, which turns
    off Chromium's OS-level sandbox -- a real security control, usually
    cargo-culted from a Docker guide. Containers need a proper seccomp profile,
    not a disabled sandbox, and locally it should simply stay on.
    """
    return {
        "headless": config.browser.headless,
        "timeout": float(config.timeouts.launch_ms),
    }


def context_options(config: Config, proxy: Proxy | None = None) -> dict[str, Any]:
    """Arguments for `browser.new_context()`.

    Credentials are passed as separate `username`/`password` keys rather than
    embedded in the server URL. That is why `Proxy.server` deliberately omits
    them: a URL with credentials in it ends up in logs and error messages.
    """
    options: dict[str, Any] = {
        "viewport": {
            "width": config.browser.viewport_width,
            "height": config.browser.viewport_height,
        },
        "locale": config.browser.locale,
    }

    if config.browser.user_agent:
        options["user_agent"] = config.browser.user_agent

    if proxy is not None:
        settings: dict[str, str] = {"server": proxy.server}
        if proxy.username is not None:
            settings["username"] = proxy.username
        if proxy.password is not None:
            settings["password"] = proxy.password
        options["proxy"] = settings

    return options


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #


class BrowserFactory:
    """Owns the Playwright driver and one browser process.

    One browser, many contexts -- the model Playwright was chosen for. A
    context costs roughly 5-15 MB and tens of milliseconds; a browser process
    costs ~150 MB and 1-3 seconds. At concurrency 10 that is the difference
    between one process and ten.

    The trade-off, stated plainly: all sessions share one process, so if it
    dies every in-flight session dies with it, and they contend for its CPU
    and network stack. Per-session browsers would isolate that at roughly ten
    times the cost. Shared is the default here; the split between this class
    and the session makes measuring the alternative a change in one place.
    """

    __slots__ = ("_browser", "_config", "_playwright")

    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    # -- context manager ---------------------------------------------------- #

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

    # -- lifecycle ---------------------------------------------------------- #

    async def start(self) -> None:
        """Start the driver and launch the browser. Idempotent."""
        if self._browser is not None:
            return

        engine = self._config.browser.engine
        self._playwright = await async_playwright().start()
        try:
            browser_type = self._select_engine(self._playwright, engine)
            self._browser = await browser_type.launch(**launch_options(self._config))
        except PlaywrightError as exc:
            # Without this, a failed launch leaves the driver's Node process
            # running for the lifetime of the program -- the same shape of leak
            # as the old project's `driver.quit()` inside a `try`.
            await self._stop_driver()
            raise BrowserLaunchError(f"could not launch {engine}: {exc}") from exc

    async def stop(self) -> None:
        """Close the browser and stop the driver. Safe to call twice."""
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            # Runs even if close() raised, so a wedged browser cannot strand
            # the driver process behind it.
            self._browser = None
            await self._stop_driver()

    async def _stop_driver(self) -> None:
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    @staticmethod
    def _select_engine(playwright: Playwright, engine: str) -> BrowserType:
        match engine:
            case "chromium":
                return playwright.chromium
            case "firefox":
                return playwright.firefox
            case "webkit":
                return playwright.webkit
            case _:  # pragma: no cover - config validation rejects this first
                raise BrowserLaunchError(f"unknown browser engine {engine!r}")

    # -- contexts ----------------------------------------------------------- #

    @property
    def browser(self) -> Browser:
        if self._browser is None:
            raise BrowserLaunchError("factory is not started; use `async with BrowserFactory(...)`")
        return self._browser

    @property
    def started(self) -> bool:
        return self._browser is not None

    @asynccontextmanager
    async def new_context(self, proxy: Proxy | None = None) -> AsyncIterator[BrowserContext]:
        """Yield an isolated context, closing it no matter how the body exits.

        `try/finally` around the `yield` is the whole point: an exception in
        the caller's body still runs the close. Without it, every failed
        session would leak a context -- and at 50 sessions that is a browser
        holding 50 abandoned cookie jars and render processes.
        """
        context = await self.browser.new_context(**context_options(self._config, proxy))
        context.set_default_navigation_timeout(float(self._config.timeouts.navigation_ms))
        try:
            yield context
        finally:
            await context.close()

    def __repr__(self) -> str:
        state = "started" if self.started else "stopped"
        return f"<BrowserFactory engine={self._config.browser.engine} {state}>"
