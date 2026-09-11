"""Unit tests for experiment aggregation.

The rule under test throughout: averages cover COMPLETED sessions only, and
skip None rather than treating it as zero.
"""

from app.telemetry.metrics import ExperimentMetrics, SessionResult, SessionStatus


def result(
    session_id: int,
    status: SessionStatus = SessionStatus.COMPLETED,
    *,
    navigation_ms: float | None = 10.0,
    total_ms: float = 100.0,
    proxy_label: str | None = None,
) -> SessionResult:
    return SessionResult(
        experiment_id="e",
        session_id=session_id,
        status=status,
        proxy_label=proxy_label,
        started_at=0.0,
        total_ms=total_ms,
        setup_ms=5.0,
        navigation_ms=navigation_ms,
        dwell_ms=80.0 if status.ok else None,
        http_status=200 if status.ok else None,
    )


def metrics(*results: SessionResult) -> ExperimentMetrics:
    return ExperimentMetrics("exp-1", started_at=0.0, total_ms=1234.0, results=results)


def test_counts() -> None:
    m = metrics(
        result(1),
        result(2, SessionStatus.FAILED_PROXY),
        result(3),
        result(4, SessionStatus.FAILED_TIMEOUT),
    )
    assert m.sessions_started == 4
    assert m.sessions_completed == 2
    assert m.sessions_failed == 2
    assert m.success_rate == 0.5
    assert m.proxy_failures == 1


def test_status_counts_group_the_failure_modes() -> None:
    m = metrics(
        result(1),
        result(2, SessionStatus.FAILED_PROXY),
        result(3, SessionStatus.FAILED_PROXY),
    )
    assert m.status_counts == {
        SessionStatus.COMPLETED: 1,
        SessionStatus.FAILED_PROXY: 2,
    }


def test_an_empty_experiment_reports_zero_rather_than_dividing_by_zero() -> None:
    m = metrics()
    assert m.sessions_started == 0
    assert m.success_rate == 0.0
    assert m.average_navigation_ms is None
    assert m.summary()  # must not raise


def test_averages_ignore_failed_sessions() -> None:
    """A fast failure must not make navigation look quicker than it is."""
    m = metrics(
        result(1, navigation_ms=100.0),
        result(2, navigation_ms=200.0),
        result(3, SessionStatus.FAILED_PROXY, navigation_ms=1.0),
    )
    assert m.average_navigation_ms == 150.0


def test_averages_skip_missing_values_rather_than_counting_them_as_zero() -> None:
    m = metrics(result(1, navigation_ms=100.0), result(2, navigation_ms=None))
    assert m.average_navigation_ms == 100.0


def test_average_is_none_when_nothing_completed() -> None:
    m = metrics(result(1, SessionStatus.FAILED_TIMEOUT), result(2, SessionStatus.FAILED_PROXY))
    assert m.average_navigation_ms is None
    assert m.average_duration_ms is None


def test_proxy_usage_is_counted_separately_from_proxy_failures() -> None:
    m = metrics(
        result(1, proxy_label="1.1.1.1:80"),
        result(2, SessionStatus.FAILED_PROXY, proxy_label="2.2.2.2:80"),
        result(3),
    )
    assert m.sessions_via_proxy == 2
    assert m.proxy_failures == 1


def test_summary_reports_the_headline_numbers() -> None:
    m = metrics(result(1), result(2), result(3, SessionStatus.FAILED_PROXY))
    text = m.summary()

    assert "exp-1" in text
    assert "Sessions:" in text
    assert "failed_proxy" in text
    assert "66.7%" in text


def test_summary_does_not_print_none_for_missing_timings() -> None:
    assert "None" not in metrics(result(1, SessionStatus.FAILED_PROXY)).summary()
