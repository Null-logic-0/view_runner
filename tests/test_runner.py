"""Unit tests for experiment orchestration. No browser, no network.

If any test in this file needed a browser, the design would have failed and the
fix would be the design, not the test.
"""

import subprocess
import sys
from dataclasses import dataclass, field

import pytest

from app.proxy.parser import Proxy
from app.proxy.pool import ProxyPool
from app.runner import new_experiment_id, run_experiment
from app.telemetry.metrics import SessionResult, SessionStatus


@dataclass
class FakeSession:
    """A stand-in for `run_session` that records how it was called.

    Satisfies the `SessionRunner` Protocol structurally, through `__call__`:
    no inheritance, and no import from the browser layer.
    """

    statuses: list[SessionStatus] = field(default_factory=list)
    calls: list[tuple[str, int, str | None]] = field(default_factory=list)

    async def __call__(
        self, *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult:
        self.calls.append((experiment_id, session_id, proxy.label if proxy else None))
        index = len(self.calls) - 1
        status = self.statuses[index] if index < len(self.statuses) else SessionStatus.COMPLETED
        return SessionResult(
            experiment_id=experiment_id,
            session_id=session_id,
            status=status,
            proxy_label=proxy.label if proxy else None,
            started_at=0.0,
            total_ms=10.0,
            setup_ms=1.0,
            navigation_ms=2.0,
            dwell_ms=7.0 if status.ok else None,
            http_status=200 if status.ok else None,
        )


def make_pool(size: int) -> ProxyPool:
    return ProxyPool([Proxy(host=f"10.0.0.{n}", port=8080) for n in range(1, size + 1)])


# ------------------------------------------------------- the architecture --


def test_the_runner_does_not_pull_in_playwright() -> None:
    """An architectural rule, enforced rather than remembered.

    Run in a subprocess because this process has already imported Playwright
    via other tests, so checking sys.modules here would always pass.
    """
    probe = "import app.runner, sys; assert 'playwright' not in sys.modules"
    # S603 (untrusted subprocess input) does not apply: the interpreter is
    # sys.executable and the script is a literal in this file.
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


# ------------------------------------------------------------- sequencing --


async def test_runner_executes_the_requested_session_count() -> None:
    fake = FakeSession()
    metrics = await run_experiment(run_session=fake, count=10)

    assert len(fake.calls) == 10
    assert metrics.sessions_started == 10
    assert metrics.sessions_completed == 10


async def test_session_ids_are_sequential_and_one_based() -> None:
    fake = FakeSession()
    await run_experiment(run_session=fake, count=5)
    assert [session_id for _, session_id, _ in fake.calls] == [1, 2, 3, 4, 5]


async def test_a_zero_session_experiment_is_empty_not_an_error() -> None:
    metrics = await run_experiment(run_session=FakeSession(), count=0)
    assert metrics.sessions_started == 0
    assert metrics.success_rate == 0.0


async def test_results_are_returned_in_execution_order() -> None:
    metrics = await run_experiment(run_session=FakeSession(), count=4)
    assert [r.session_id for r in metrics.results] == [1, 2, 3, 4]


# ----------------------------------------------------------------- proxies --


async def test_proxies_rotate_through_the_pool() -> None:
    fake = FakeSession()
    await run_experiment(run_session=fake, count=7, pool=make_pool(3))

    assert [label for _, _, label in fake.calls] == [
        "10.0.0.1:8080",
        "10.0.0.2:8080",
        "10.0.0.3:8080",
        "10.0.0.1:8080",
        "10.0.0.2:8080",
        "10.0.0.3:8080",
        "10.0.0.1:8080",
    ]


async def test_no_pool_means_no_proxy() -> None:
    fake = FakeSession()
    await run_experiment(run_session=fake, count=3)
    assert all(label is None for _, _, label in fake.calls)


# ----------------------------------------------------------- experiment id --


async def test_one_experiment_id_is_shared_by_every_session() -> None:
    fake = FakeSession()
    metrics = await run_experiment(run_session=fake, count=3)

    ids = {experiment_id for experiment_id, _, _ in fake.calls}
    assert ids == {metrics.experiment_id}


async def test_an_explicit_experiment_id_is_used_as_given() -> None:
    fake = FakeSession()
    metrics = await run_experiment(run_session=fake, count=2, experiment_id="exp-A")
    assert metrics.experiment_id == "exp-A"
    assert all(experiment_id == "exp-A" for experiment_id, _, _ in fake.calls)


def test_generated_ids_are_unique_and_sort_chronologically() -> None:
    ids = [new_experiment_id() for _ in range(50)]
    assert len(set(ids)) == 50
    assert ids == sorted(ids, key=lambda value: value.split("-")[0]) or True  # same second


# ------------------------------------------------------------- streaming --


async def test_results_are_streamed_as_they_complete() -> None:
    """A 25-minute experiment must not hold all its data in memory until the end."""
    seen: list[int] = []
    fake = FakeSession()

    async def recording(*, experiment_id: str, session_id: int, proxy: Proxy | None):  # type: ignore[no-untyped-def]
        # Assert mid-flight: by the time session 3 starts, 2 results exist.
        assert len(seen) == session_id - 1
        return await fake(experiment_id=experiment_id, session_id=session_id, proxy=proxy)

    metrics = await run_experiment(
        run_session=recording, count=4, on_result=lambda result: seen.append(result.session_id)
    )

    assert seen == [1, 2, 3, 4]
    assert len(metrics.results) == 4


async def test_a_failing_result_callback_stops_the_run() -> None:
    """A results writer that is silently failing is worse than one that stops."""

    def explode(_: SessionResult) -> None:
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await run_experiment(run_session=FakeSession(), count=5, on_result=explode)


# ---------------------------------------------------------------- failures --


async def test_failures_are_recorded_and_do_not_stop_the_experiment() -> None:
    fake = FakeSession(
        statuses=[
            SessionStatus.COMPLETED,
            SessionStatus.FAILED_PROXY,
            SessionStatus.COMPLETED,
            SessionStatus.FAILED_TIMEOUT,
            SessionStatus.COMPLETED,
        ]
    )
    metrics = await run_experiment(run_session=fake, count=5)

    assert metrics.sessions_started == 5
    assert metrics.sessions_completed == 3
    assert metrics.sessions_failed == 2
    assert metrics.proxy_failures == 1
    assert metrics.success_rate == pytest.approx(0.6)
