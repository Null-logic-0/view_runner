"""Unit tests for the failure policy. No browser, no network, no config file."""

import pytest

from app.failure import FailurePolicy, FailureTracker, is_retryable
from app.telemetry.metrics import SessionResult, SessionStatus


def result(status: SessionStatus, http_status: int | None = None) -> SessionResult:
    return SessionResult(
        experiment_id="e",
        session_id=1,
        status=status,
        proxy_label=None,
        started_at=0.0,
        total_ms=1.0,
        http_status=http_status,
    )


# ------------------------------------------------------------- retryability --


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.FAILED_PROXY,
        SessionStatus.FAILED_TIMEOUT,
        SessionStatus.FAILED_NAVIGATION,
    ],
)
def test_transient_failures_are_retryable(status: SessionStatus) -> None:
    assert is_retryable(result(status)) is True


def test_a_completed_session_is_never_retried() -> None:
    assert is_retryable(result(SessionStatus.COMPLETED, 200)) is False


def test_setup_failures_are_not_retryable() -> None:
    """Retry the request, never the infrastructure.

    FAILED_SETUP means the browser could not create a context. Retrying a
    broken component turns a loud two-second failure into a slow one.
    """
    assert is_retryable(result(SessionStatus.FAILED_SETUP)) is False


@pytest.mark.parametrize(
    ("http_status", "expected"),
    [(500, True), (502, True), (503, True), (599, True), (404, False), (400, False), (403, False)],
)
def test_server_errors_are_retryable_but_client_errors_are_not(
    http_status: int, expected: bool
) -> None:
    """A 503 is the server having a bad moment; a 404 will say the same next time."""
    assert is_retryable(result(SessionStatus.FAILED_STATUS, http_status)) is expected


def test_a_status_failure_with_no_recorded_status_is_not_retried() -> None:
    assert is_retryable(result(SessionStatus.FAILED_STATUS, None)) is False


# ------------------------------------------------------------------ policy --


def test_the_default_policy_never_retries() -> None:
    """Retries change what is measured, so they are opt-in."""
    policy = FailurePolicy()
    assert policy.max_attempts == 1
    assert policy.should_retry(result(SessionStatus.FAILED_PROXY), attempt=1) is False


def test_retries_stop_at_the_attempt_budget() -> None:
    policy = FailurePolicy(max_attempts=3)
    failure = result(SessionStatus.FAILED_PROXY)

    assert policy.should_retry(failure, attempt=1) is True
    assert policy.should_retry(failure, attempt=2) is True
    assert policy.should_retry(failure, attempt=3) is False


def test_a_non_retryable_failure_is_not_retried_however_large_the_budget() -> None:
    policy = FailurePolicy(max_attempts=10)
    assert policy.should_retry(result(SessionStatus.FAILED_SETUP), attempt=1) is False


@pytest.mark.parametrize(
    ("attempt", "expected"), [(1, 0.0), (2, 0.5), (3, 1.0), (4, 2.0), (5, 4.0)]
)
def test_backoff_doubles_per_attempt(attempt: int, expected: float) -> None:
    assert FailurePolicy(backoff_seconds=0.5).delay_before(attempt) == expected


def test_zero_backoff_means_retry_immediately() -> None:
    assert FailurePolicy(backoff_seconds=0.0).delay_before(3) == 0.0


# ----------------------------------------------------------------- tracker --


def test_the_tracker_is_inert_when_aborting_is_disabled() -> None:
    tracker = FailureTracker(FailurePolicy(abort_after_consecutive_failures=0))
    for _ in range(100):
        tracker.record(result(SessionStatus.FAILED_PROXY))
    assert tracker.abort_reason is None


def test_abort_fires_at_the_configured_run_of_failures() -> None:
    tracker = FailureTracker(FailurePolicy(abort_after_consecutive_failures=3))

    # Captured as values: mypy narrows a property after `assert x is None` and
    # then calls the later `is not None` unreachable. Same trap as Phase 7.
    tracker.record(result(SessionStatus.FAILED_PROXY))
    tracker.record(result(SessionStatus.FAILED_PROXY))
    after_two = tracker.abort_reason

    tracker.record(result(SessionStatus.FAILED_PROXY))
    after_three = tracker.abort_reason

    assert after_two is None
    assert after_three is not None
    assert "3 consecutive failures" in after_three
    assert "failed_proxy" in after_three


def test_one_success_resets_the_run() -> None:
    """Scattered failures are normal; a run of them means something is broken."""
    tracker = FailureTracker(FailurePolicy(abort_after_consecutive_failures=3))

    tracker.record(result(SessionStatus.FAILED_PROXY))
    tracker.record(result(SessionStatus.FAILED_PROXY))
    tracker.record(result(SessionStatus.COMPLETED, 200))
    assert tracker.consecutive_failures == 0

    tracker.record(result(SessionStatus.FAILED_PROXY))
    tracker.record(result(SessionStatus.FAILED_PROXY))
    assert tracker.abort_reason is None


def test_the_abort_reason_is_captured_once_and_not_overwritten() -> None:
    tracker = FailureTracker(FailurePolicy(abort_after_consecutive_failures=2))
    tracker.record(result(SessionStatus.FAILED_PROXY))
    tracker.record(result(SessionStatus.FAILED_PROXY))
    first = tracker.abort_reason

    tracker.record(result(SessionStatus.FAILED_TIMEOUT))
    assert tracker.abort_reason == first
