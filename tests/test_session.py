"""Unit tests for session helpers: classification and error summarising.

The lifecycle itself needs a real browser and lives in tests/integration/.
These two functions are pure, so they are checked here in microseconds --
and classification is exactly the logic most likely to rot silently when
Chromium renames an error code.
"""

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser.session import classify_navigation_error, summarize_error
from app.telemetry.metrics import SessionResult, SessionStatus


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("net::ERR_PROXY_CONNECTION_FAILED at http://x/", SessionStatus.FAILED_PROXY),
        ("net::ERR_TUNNEL_CONNECTION_FAILED at http://x/", SessionStatus.FAILED_PROXY),
        ("net::ERR_SOCKS_CONNECTION_FAILED at http://x/", SessionStatus.FAILED_PROXY),
        ("net::ERR_CONNECTION_REFUSED at http://x/", SessionStatus.FAILED_NAVIGATION),
        ("net::ERR_NAME_NOT_RESOLVED at http://x/", SessionStatus.FAILED_NAVIGATION),
        ("net::ERR_CERT_AUTHORITY_INVALID", SessionStatus.FAILED_NAVIGATION),
        ("something nobody has seen before", SessionStatus.FAILED_NAVIGATION),
    ],
)
def test_navigation_errors_are_classified(message: str, expected: SessionStatus) -> None:
    assert classify_navigation_error(PlaywrightError(message)) is expected


def test_timeouts_win_over_text_matching() -> None:
    """TimeoutError subclasses PlaywrightError, so the isinstance check must come first."""
    exc = PlaywrightTimeoutError("Timeout 500ms exceeded while loading")
    assert classify_navigation_error(exc) is SessionStatus.FAILED_TIMEOUT


def test_unknown_errors_degrade_instead_of_raising() -> None:
    """Classification must never be the thing that breaks a run."""
    assert classify_navigation_error(ValueError("?")) is SessionStatus.FAILED_NAVIGATION


def test_only_the_first_line_of_an_error_is_kept() -> None:
    exc = PlaywrightError("Page.goto: net::ERR_ABORTED\nCall log:\n  - navigating to ...\n")
    assert summarize_error(exc) == "Page.goto: net::ERR_ABORTED"


def test_long_errors_are_truncated() -> None:
    summary = summarize_error(PlaywrightError("x" * 1000))
    assert len(summary) < 250
    assert summary.endswith("...")


def test_status_ok_is_only_true_for_completed() -> None:
    assert SessionStatus.COMPLETED.ok is True
    assert all(not s.ok for s in SessionStatus if s is not SessionStatus.COMPLETED)


def test_result_reports_whether_a_proxy_was_used() -> None:
    base = {
        "experiment_id": "e",
        "session_id": 1,
        "status": SessionStatus.COMPLETED,
        "started_at": 0.0,
        "total_ms": 1.0,
    }
    assert SessionResult(proxy_label=None, **base).used_proxy is False  # type: ignore[arg-type]
    assert SessionResult(proxy_label="1.2.3.4:8080", **base).used_proxy is True  # type: ignore[arg-type]
