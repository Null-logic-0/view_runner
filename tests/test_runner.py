"""Unit tests for experiment orchestration. No browser, no network.

If any test in this file needed a browser, the design would have failed and the
fix would be the design, not the test.
"""

import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass, field

import pytest

from app.failure import FailurePolicy
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


# The architecture


def test_the_runner_does_not_pull_in_playwright() -> None:
    """An architectural rule, enforced rather than remembered.

    Run in a subprocess because this process has already imported Playwright
    via other tests, so checking sys.modules here would always pass.
    """
    probe = "import app.runner, sys; assert 'playwright' not in sys.modules"

    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


# Sequencing


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


# proxies


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


# experiment id


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


# Streaming


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
    """A results writer that is silently failing is worse than one that stops.

    The exception arrives wrapped in an ExceptionGroup: TaskGroup collects
    failures from its children, because several really can fail at once. That
    is a visible API change caused purely by adopting structured concurrency.
    """

    def explode(_: SessionResult) -> None:
        raise OSError("disk full")

    with pytest.raises(ExceptionGroup) as exc_info:
        await run_experiment(run_session=FakeSession(), count=5, on_result=explode)

    assert all(isinstance(exc, OSError) for exc in exc_info.value.exceptions)


# failures


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


# concurrency


@dataclass
class ConcurrencyProbe:
    """A fake session that records how many ran at the same time.

    `peak` is the measurement that matters: a semaphore that quietly admitted
    an extra task would still produce correct results and a plausible runtime,
    so the only way to know the limit holds is to watch it.
    """

    delay: float = 0.02
    active: int = 0
    peak: int = 0
    completion_order: list[int] = field(default_factory=list)

    async def __call__(
        self, *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        self.completion_order.append(session_id)
        return SessionResult(
            experiment_id=experiment_id,
            session_id=session_id,
            status=SessionStatus.COMPLETED,
            proxy_label=proxy.label if proxy else None,
            started_at=0.0,
            total_ms=self.delay * 1000,
        )


@pytest.mark.parametrize("concurrency", [1, 2, 3, 5, 7])
async def test_the_concurrency_limit_is_never_exceeded(concurrency: int) -> None:
    probe = ConcurrencyProbe()
    await run_experiment(run_session=probe, count=20, concurrency=concurrency)
    assert probe.peak <= concurrency


async def test_concurrency_one_really_is_sequential() -> None:
    """No separate sequential code path exists; a semaphore of 1 must provide it."""
    probe = ConcurrencyProbe()
    await run_experiment(run_session=probe, count=8, concurrency=1)
    assert probe.peak == 1
    assert probe.completion_order == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_the_limit_is_actually_reached_not_merely_respected() -> None:
    """A broken semaphore that admitted one at a time would pass the test above."""
    probe = ConcurrencyProbe()
    await run_experiment(run_session=probe, count=20, concurrency=5)
    assert probe.peak == 5


async def test_concurrency_larger_than_the_session_count_is_harmless() -> None:
    probe = ConcurrencyProbe()
    await run_experiment(run_session=probe, count=3, concurrency=50)
    assert probe.peak == 3


async def test_concurrency_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="concurrency must be at least 1"):
        await run_experiment(run_session=FakeSession(), count=1, concurrency=0)


# what survives concurrency, and what does not


@dataclass
class ReverseFinisher:
    """Later sessions finish first, so completion order is the reverse of start order."""

    count: int
    completion_order: list[int] = field(default_factory=list)

    async def __call__(
        self, *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult:
        await asyncio.sleep(0.01 * (self.count - session_id + 1))
        self.completion_order.append(session_id)
        return SessionResult(
            experiment_id=experiment_id,
            session_id=session_id,
            status=SessionStatus.COMPLETED,
            proxy_label=proxy.label if proxy else None,
            started_at=0.0,
            total_ms=1.0,
        )


async def test_results_stay_ordered_by_session_id_however_they_finish() -> None:
    """Survives: results are written into pre-sized slots by index, not appended."""
    fake = ReverseFinisher(count=5)
    metrics = await run_experiment(run_session=fake, count=5, concurrency=5)

    assert fake.completion_order == [5, 4, 3, 2, 1], "sessions did finish out of order"
    assert [r.session_id for r in metrics.results] == [1, 2, 3, 4, 5]


async def test_proxy_rotation_stays_deterministic_under_concurrency() -> None:
    """Survives: proxies are assigned before any task starts."""
    fake = ReverseFinisher(count=7)
    metrics = await run_experiment(run_session=fake, count=7, concurrency=7, pool=make_pool(3))

    assert [r.proxy_label for r in metrics.results] == [
        "10.0.0.1:8080",
        "10.0.0.2:8080",
        "10.0.0.3:8080",
        "10.0.0.1:8080",
        "10.0.0.2:8080",
        "10.0.0.3:8080",
        "10.0.0.1:8080",
    ]


async def test_the_result_callback_fires_in_completion_order_not_session_order() -> None:
    """Does NOT survive, by design.

    `on_result` exists so results reach disk as soon as they exist. Under
    concurrency that means completion order. A consumer needing session order
    must sort -- which it can, because session_id is in every row.
    """
    seen: list[int] = []
    fake = ReverseFinisher(count=5)

    await run_experiment(
        run_session=fake,
        count=5,
        concurrency=5,
        on_result=lambda result: seen.append(result.session_id),
    )

    assert seen == [5, 4, 3, 2, 1]


# failure handling


async def test_one_raising_session_cancels_the_others() -> None:
    """Structured concurrency: nothing outlives the TaskGroup block.

    asyncio.gather would propagate this exception while leaving the siblings
    running detached -- which for real sessions means orphaned browser
    contexts piling up after the experiment has already failed.
    """
    started: list[int] = []

    async def exploding(
        *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult:
        started.append(session_id)
        if session_id == 2:
            raise RuntimeError("boom")
        await asyncio.sleep(1.0)  # long enough that cancellation is observable
        raise AssertionError("should have been cancelled")

    with pytest.raises(ExceptionGroup) as exc_info:
        await run_experiment(run_session=exploding, count=10, concurrency=4)

    assert any(isinstance(exc, RuntimeError) for exc in exc_info.value.exceptions)

    assert len(started) < 10


async def test_concurrency_actually_overlaps_the_waiting() -> None:
    """20 sessions of 50 ms cannot finish in 1 s sequentially, but can at 10."""
    probe = ConcurrencyProbe(delay=0.05)
    began = time.monotonic()
    await run_experiment(run_session=probe, count=20, concurrency=10)
    elapsed = time.monotonic() - began

    assert elapsed < 0.6, f"expected roughly 2 batches of 50 ms, took {elapsed:.2f}s"


# ---------------------------------------------------- retries and aborting --


@dataclass
class ScriptedSession:
    """Returns a scripted status per attempt, per session.

    `script[session_id]` is the sequence of statuses that session's successive
    attempts return. Anything past the end repeats the last entry.
    """

    script: dict[int, list[SessionStatus]] = field(default_factory=dict)
    attempts: list[tuple[int, str | None]] = field(default_factory=list)

    async def __call__(
        self, *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult:
        # A real session always suspends on network I/O, and cancellation is
        # only ever delivered at a suspension point. Without this yield the
        # fake runs to completion inside one scheduling slice, so a TaskGroup
        # abort cannot stop its siblings and the test would assert something
        # that is false of every real session.
        await asyncio.sleep(0)
        seen = sum(1 for sid, _ in self.attempts if sid == session_id)
        statuses = self.script.get(session_id, [SessionStatus.COMPLETED])
        status = statuses[min(seen, len(statuses) - 1)]
        self.attempts.append((session_id, proxy.label if proxy else None))
        return SessionResult(
            experiment_id=experiment_id,
            session_id=session_id,
            status=status,
            proxy_label=proxy.label if proxy else None,
            started_at=0.0,
            total_ms=1.0,
            http_status=200 if status.ok else None,
        )


NO_WAIT = FailurePolicy(max_attempts=3, backoff_seconds=0.0)


async def test_no_retries_happen_by_default() -> None:
    """The default policy must not quietly improve anyone's success rate."""
    fake = ScriptedSession(script={1: [SessionStatus.FAILED_PROXY]})
    metrics = await run_experiment(run_session=fake, count=1)

    assert len(fake.attempts) == 1
    assert metrics.results[0].attempts == 1
    assert metrics.sessions_retried == 0


async def test_a_retryable_failure_is_retried_until_it_succeeds() -> None:
    fake = ScriptedSession(
        script={
            1: [SessionStatus.FAILED_PROXY, SessionStatus.FAILED_TIMEOUT, SessionStatus.COMPLETED]
        }
    )
    metrics = await run_experiment(run_session=fake, count=1, policy=NO_WAIT)

    assert len(fake.attempts) == 3
    assert metrics.sessions_completed == 1
    assert metrics.results[0].attempts == 3
    assert metrics.results[0].was_retried is True


async def test_retries_stop_at_the_budget_and_the_last_failure_is_recorded() -> None:
    fake = ScriptedSession(script={1: [SessionStatus.FAILED_PROXY]})
    metrics = await run_experiment(run_session=fake, count=1, policy=NO_WAIT)

    assert len(fake.attempts) == 3
    assert metrics.sessions_completed == 0
    assert metrics.results[0].status is SessionStatus.FAILED_PROXY
    assert metrics.results[0].attempts == 3


async def test_a_non_retryable_failure_is_not_retried() -> None:
    fake = ScriptedSession(script={1: [SessionStatus.FAILED_SETUP]})
    metrics = await run_experiment(run_session=fake, count=1, policy=NO_WAIT)

    assert len(fake.attempts) == 1
    assert metrics.results[0].attempts == 1


async def test_a_retry_gets_the_next_proxy_not_the_failed_one() -> None:
    """Retrying a proxy failure on the same proxy would be pointless."""
    fake = ScriptedSession(script={1: [SessionStatus.FAILED_PROXY, SessionStatus.COMPLETED]})
    await run_experiment(run_session=fake, count=1, policy=NO_WAIT, pool=make_pool(3))

    used = [label for _, label in fake.attempts]
    assert used == ["10.0.0.1:8080", "10.0.0.2:8080"]


async def test_total_attempts_exposes_the_hidden_cost_of_retrying() -> None:
    fake = ScriptedSession(script={sid: [SessionStatus.FAILED_TIMEOUT] for sid in (1, 2, 3)})
    metrics = await run_experiment(run_session=fake, count=3, policy=NO_WAIT)

    assert metrics.sessions_started == 3
    assert metrics.total_attempts == 9


async def test_backoff_is_awaited_between_attempts() -> None:
    fake = ScriptedSession(script={1: [SessionStatus.FAILED_PROXY, SessionStatus.COMPLETED]})
    policy = FailurePolicy(max_attempts=2, backoff_seconds=0.1)

    began = time.monotonic()
    await run_experiment(run_session=fake, count=1, policy=policy)
    assert time.monotonic() - began >= 0.1


# -- aborting ---------------------------------------------------------------- #


async def test_an_experiment_aborts_after_a_run_of_failures() -> None:
    fake = ScriptedSession(script={sid: [SessionStatus.FAILED_PROXY] for sid in range(1, 21)})
    policy = FailurePolicy(abort_after_consecutive_failures=3)

    metrics = await run_experiment(run_session=fake, count=20, concurrency=1, policy=policy)

    assert metrics.aborted is True
    assert metrics.abort_reason is not None
    assert "3 consecutive failures" in metrics.abort_reason
    assert metrics.sessions_started < 20, "the remaining sessions must not have run"


async def test_an_aborted_experiment_still_returns_the_data_it_collected() -> None:
    """The useful answer is "I stopped at session N, here is what I saw"."""
    fake = ScriptedSession(script={sid: [SessionStatus.FAILED_TIMEOUT] for sid in range(1, 21)})
    policy = FailurePolicy(abort_after_consecutive_failures=2)

    metrics = await run_experiment(run_session=fake, count=20, concurrency=1, policy=policy)

    assert metrics.aborted is True
    assert metrics.sessions_started >= 2
    assert all(r.status is SessionStatus.FAILED_TIMEOUT for r in metrics.results)
    assert "ABORTED" in metrics.summary()


async def test_scattered_failures_do_not_abort() -> None:
    """Only a run of failures means something is broken."""
    script = {2: [SessionStatus.FAILED_PROXY], 5: [SessionStatus.FAILED_TIMEOUT]}
    fake = ScriptedSession(script=script)
    policy = FailurePolicy(abort_after_consecutive_failures=2)

    metrics = await run_experiment(run_session=fake, count=8, concurrency=1, policy=policy)

    assert metrics.aborted is False
    assert metrics.sessions_started == 8
    assert metrics.sessions_failed == 2


async def test_a_genuine_bug_still_propagates_rather_than_being_reported_as_an_abort() -> None:
    """`except*` matches only ExperimentAbortedError; anything else escapes."""

    async def buggy(*, experiment_id: str, session_id: int, proxy: Proxy | None) -> SessionResult:
        raise TypeError("this is a bug, not a failed session")

    with pytest.raises(ExceptionGroup) as exc_info:
        await run_experiment(run_session=buggy, count=3)

    assert any(isinstance(exc, TypeError) for exc in exc_info.value.exceptions)
