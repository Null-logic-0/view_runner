"""Integration tests for the session lifecycle. Real browser, real HTTP.

These are the tests that can catch a measurement being *wrong* rather than
merely absent -- most importantly by checking a timing against a delay the
test itself caused.
"""

import pytest

from app.browser.factory import BrowserFactory
from app.browser.session import run_session
from app.proxy.parser import Proxy
from app.telemetry.metrics import SessionStatus
from tests.conftest import LocalServer, make_config

pytestmark = pytest.mark.integration


async def test_a_successful_session_reports_every_measurement(
    local_server: LocalServer,
) -> None:
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"{local_server.base_url}/page",
            duration_s=0.2,
            experiment_id="exp-1",
            session_id=7,
        )

    assert result.status is SessionStatus.COMPLETED
    assert result.ok is True
    assert result.experiment_id == "exp-1"
    assert result.session_id == 7
    assert result.http_status == 200
    assert result.error_type is None
    assert result.proxy_label is None

    # Every phase was measured, not merely "no exception was raised".
    assert result.setup_ms is not None and result.setup_ms > 0
    assert result.navigation_ms is not None and result.navigation_ms > 0
    assert result.dwell_ms is not None

    # Ground truth: the server actually received the request.
    assert local_server.hits == ["/page"]


async def test_total_time_covers_the_phases_it_contains(local_server: LocalServer) -> None:
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=local_server.base_url,
            duration_s=0.2,
            experiment_id="e",
            session_id=1,
        )

    phases = (result.setup_ms or 0) + (result.navigation_ms or 0) + (result.dwell_ms or 0)
    assert result.total_ms >= phases


# calibration


async def test_dwell_honours_the_configured_duration(local_server: LocalServer) -> None:
    """The duration is an argument. The number 30 appears nowhere in session.py."""
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=local_server.base_url,
            duration_s=0.5,
            experiment_id="e",
            session_id=1,
        )

    assert result.dwell_ms is not None
    assert 500 <= result.dwell_ms < 1500


async def test_a_zero_duration_session_still_completes(local_server: LocalServer) -> None:
    """Navigate-then-close: a legitimate experiment measuring pure startup cost."""
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=local_server.base_url,
            duration_s=0,
            experiment_id="e",
            session_id=1,
        )

    assert result.status is SessionStatus.COMPLETED
    assert result.dwell_ms is not None
    assert result.dwell_ms < 100


async def test_navigation_timing_tracks_a_delay_we_caused(local_server: LocalServer) -> None:
    """Instrument calibration: measure a known value before trusting unknown ones.

    The server sleeps 800 ms before responding. If navigation_ms came back at
    40 ms, the timer would be measuring the wrong thing -- for instance goto()
    returning at `commit` rather than `load`.
    """
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"{local_server.base_url}/slow?ms=800",
            duration_s=0,
            experiment_id="e",
            session_id=1,
        )

    assert result.status is SessionStatus.COMPLETED
    assert result.navigation_ms is not None
    assert 800 <= result.navigation_ms < 3000


# failure --


async def test_a_dead_proxy_is_reported_as_a_proxy_failure(
    local_server: LocalServer, closed_port: int
) -> None:
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=local_server.base_url,
            duration_s=30,  # must never be reached
            experiment_id="e",
            session_id=1,
            proxy=Proxy(host="127.0.0.1", port=closed_port),
        )

    assert result.status is SessionStatus.FAILED_PROXY
    assert result.ok is False
    assert result.proxy_label == f"127.0.0.1:{closed_port}"
    assert result.dwell_ms is None, "a failed session must not report a dwell"
    assert result.navigation_ms is None
    assert result.setup_ms is not None
    assert result.total_ms > result.setup_ms
    assert result.error_type == "Error"
    assert "ERR_PROXY_CONNECTION_FAILED" in (result.error_message or "")
    assert local_server.hits == []


async def test_an_unreachable_target_is_a_navigation_failure(closed_port: int) -> None:
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"http://127.0.0.1:{closed_port}/",
            duration_s=0,
            experiment_id="e",
            session_id=1,
        )

    assert result.status is SessionStatus.FAILED_NAVIGATION
    assert "ERR_CONNECTION_REFUSED" in (result.error_message or "")


async def test_a_slow_target_times_out_within_its_budget(local_server: LocalServer) -> None:
    config = make_config(timeouts={"navigation_ms": 500})
    async with BrowserFactory(config) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"{local_server.base_url}/slow?ms=3000",
            duration_s=0,
            experiment_id="e",
            session_id=1,
        )

    assert result.status is SessionStatus.FAILED_TIMEOUT
    assert result.error_type == "TimeoutError"
    assert result.total_ms < 3000, "the timeout must fire before the server replies"


async def test_an_unexpected_http_status_is_a_failure_not_a_success(
    local_server: LocalServer,
) -> None:
    """The old project printed 'Session completed successfully.' regardless."""
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"{local_server.base_url}/status?code=503",
            duration_s=30,  # must never be reached
            experiment_id="e",
            session_id=1,
        )

    assert result.status is SessionStatus.FAILED_STATUS
    assert result.http_status == 503
    assert result.dwell_ms is None
    assert "expected HTTP 200, got 503" in (result.error_message or "")


async def test_expected_status_is_configurable(local_server: LocalServer) -> None:
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=f"{local_server.base_url}/status?code=404",
            duration_s=0,
            experiment_id="e",
            session_id=1,
            expected_status=404,
        )

    assert result.status is SessionStatus.COMPLETED


# hygiene


async def test_a_failed_session_leaks_no_context(closed_port: int) -> None:
    """The old project's defect: cleanup skipped on the error path."""
    async with BrowserFactory(make_config()) as factory:
        for session_id in range(3):
            await run_session(
                factory=factory,
                target_url=f"http://127.0.0.1:{closed_port}/",
                duration_s=0,
                experiment_id="e",
                session_id=session_id,
            )
        assert factory.browser.contexts == []


async def test_a_proxy_password_never_reaches_the_result(
    local_server: LocalServer, closed_port: int
) -> None:
    proxy = Proxy(host="127.0.0.1", port=closed_port, username="bob", password="hunter2")
    async with BrowserFactory(make_config()) as factory:
        result = await run_session(
            factory=factory,
            target_url=local_server.base_url,
            duration_s=0,
            experiment_id="e",
            session_id=1,
            proxy=proxy,
        )

    assert "hunter2" not in repr(result)
    assert "hunter2" not in str(result.error_message)
    assert result.proxy_label == f"127.0.0.1:{closed_port}"
