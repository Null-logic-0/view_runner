"""Run exactly one browser session and report what happened.

    CREATE  -> context (isolated cookies/cache/storage, its own proxy)
    START   -> page
    NAVIGATE-> goto(target_url)
    WAIT    -> hold the session open for the configured duration
    COLLECT -> timings, HTTP status, failure classification
    CLOSE   -> guaranteed, by the factory's context manager

What this module owns: one session's lifecycle and its measurements.

What it does not own: how many sessions run, in what order, with what
concurrency, or which proxy this one gets. Those arrive as arguments. Note in
particular that the number 30 does not appear here -- the dwell is whatever
`duration_s` says, and that comes from config by way of the runner.

`run_session` returns a `SessionResult` even when the session fails. A failure
is an expected outcome of an experiment, not a bug in the program, and a
failure that raises produces no row of data -- which is how a failure rate
ends up being computed from the sessions that happened to work.
"""

import asyncio
import time
from typing import Final, Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.factory import BrowserFactory
from app.proxy.parser import Proxy
from app.telemetry.metrics import SessionResult, SessionStatus

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

# Chromium net error prefixes that mean "the proxy is the problem" rather than
# "the target is the problem".
_PROXY_ERROR_MARKERS: Final = ("ERR_PROXY_", "ERR_TUNNEL_", "ERR_SOCKS_")
_MAX_ERROR_CHARS: Final = 200


def _elapsed_ms(since: float) -> float:
    """Milliseconds since a `time.monotonic()` mark.

    Monotonic, not wall clock: an NTP correction mid-session must not be able
    to produce a negative duration.
    """
    return round((time.monotonic() - since) * 1000, 3)


def summarize_error(exc: BaseException) -> str:
    """First line of an exception, truncated.

    Playwright errors carry a multi-line call log that is useful interactively
    and ruinous in a JSONL column. One line, bounded length.
    """
    lines = str(exc).strip().splitlines()
    text = lines[0].strip() if lines else type(exc).__name__
    if len(text) > _MAX_ERROR_CHARS:
        return f"{text[:_MAX_ERROR_CHARS]}..."
    return text


def classify_navigation_error(exc: BaseException) -> SessionStatus:
    """Decide which kind of failure this was.

    Matching on Chromium's error text is brittle by nature -- it is the only
    signal Playwright surfaces, and a browser update could rename a code. It is
    worth the risk because "3 failures, all proxy" is actionable where "3
    failures" is not, and the failure mode is graceful: an unrecognised code
    classifies as FAILED_NAVIGATION rather than raising.
    """
    # TimeoutError subclasses PlaywrightError, so it must be tested first.
    if isinstance(exc, PlaywrightTimeoutError):
        return SessionStatus.FAILED_TIMEOUT
    text = str(exc)
    if any(marker in text for marker in _PROXY_ERROR_MARKERS):
        return SessionStatus.FAILED_PROXY
    return SessionStatus.FAILED_NAVIGATION


async def run_session(
    *,
    factory: BrowserFactory,
    target_url: str,
    duration_s: float,
    experiment_id: str,
    session_id: int,
    proxy: Proxy | None = None,
    expected_status: int = 200,
    wait_until: WaitUntil = "load",
) -> SessionResult:
    """Run one session to completion or failure, and report it either way.

    Keyword-only arguments are deliberate: a positional call site would read
    `run_session(factory, url, 30, "exp", 3, proxy)` and nobody could tell
    whether 30 was the duration or the session id.
    """
    started_at = time.time()  # wall clock: when this happened
    began = time.monotonic()  # monotonic: everything below is a delta from here

    status = SessionStatus.COMPLETED
    setup_ms: float | None = None
    navigation_ms: float | None = None
    dwell_ms: float | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None

    try:
        mark = time.monotonic()
        async with factory.new_context(proxy) as context:
            page = await context.new_page()
            setup_ms = _elapsed_ms(mark)

            mark = time.monotonic()
            # No explicit timeout: the context's default was set by the factory
            # from config, so the policy lives in one place.
            response = await page.goto(target_url, wait_until=wait_until)
            navigation_ms = _elapsed_ms(mark)

            if response is None:
                # goto returns None for same-document navigations and downloads.
                status = SessionStatus.FAILED_NAVIGATION
                error_type = "NoResponse"
                error_message = f"navigation to {target_url} produced no response"
            elif response.status != expected_status:
                # Do not dwell on a page that did not load correctly. The old
                # project printed "Session completed successfully." regardless.
                http_status = response.status
                status = SessionStatus.FAILED_STATUS
                error_type = "UnexpectedStatus"
                error_message = f"expected HTTP {expected_status}, got {response.status}"
            else:
                http_status = response.status
                mark = time.monotonic()
                # asyncio.sleep, never time.sleep: this yields to the event
                # loop so other sessions keep running. time.sleep would freeze
                # every concurrent session and all of the timeout handling.
                await asyncio.sleep(duration_s)
                dwell_ms = _elapsed_ms(mark)

    except PlaywrightError as exc:
        # Deliberately narrow. asyncio.CancelledError inherits from
        # BaseException precisely so it is not swallowed here; a cancelled
        # session must die, not turn into a tidy result row.
        status = SessionStatus.FAILED_SETUP if setup_ms is None else classify_navigation_error(exc)
        error_type = type(exc).__name__
        error_message = summarize_error(exc)

    return SessionResult(
        experiment_id=experiment_id,
        session_id=session_id,
        status=status,
        proxy_label=proxy.label if proxy is not None else None,
        started_at=started_at,
        total_ms=_elapsed_ms(began),
        setup_ms=setup_ms,
        navigation_ms=navigation_ms,
        dwell_ms=dwell_ms,
        http_status=http_status,
        error_type=error_type,
        error_message=error_message,
    )
