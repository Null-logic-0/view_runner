"""Experiment orchestration: how many sessions, in what order, with which proxy.

This module must not import Playwright, directly or transitively. That is not
a style preference -- it is what makes concurrency testable. Phase 11 has to
answer questions like "does concurrency 5 really run five at once, and never
six?", and answering those by launching Chromium costs seconds per assertion.
With orchestration kept pure, a fake session makes the same assertions exact
and instant.

`tests/test_runner.py` enforces this with a subprocess check, so the rule
fails a test rather than relying on anyone remembering it.

What the runner owns:
    session count, ordering, proxy assignment, result collection, aggregation.

What it does not own:
    what a browser is, what a page is, how a session works, where results are
    written. It calls something matching `SessionRunner` and hands each result
    to an optional callback.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from app.proxy.parser import Proxy
from app.proxy.pool import ProxyPool
from app.telemetry.metrics import ExperimentMetrics, SessionResult


class SessionRunner(Protocol):
    """Anything that can run one session.

    A Protocol rather than a base class: structural typing means a test's
    five-line `async def` satisfies this with no inheritance and no import
    from the browser layer. That is what keeps the rule above enforceable.
    """

    async def __call__(
        self, *, experiment_id: str, session_id: int, proxy: Proxy | None
    ) -> SessionResult: ...


ResultCallback = Callable[[SessionResult], None]


def new_experiment_id() -> str:
    """A sortable, unique identifier for one run.

    Timestamp first so that listing a results directory sorts chronologically;
    a short random suffix so two runs started in the same second cannot collide.
    """
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


async def run_experiment(
    *,
    run_session: SessionRunner,
    count: int,
    concurrency: int = 1,
    pool: ProxyPool | None = None,
    experiment_id: str | None = None,
    on_result: ResultCallback | None = None,
) -> ExperimentMetrics:
    """Run `count` sessions with at most `concurrency` in flight at once.

    concurrency = 1 is sequential. There is no separate sequential code path:
    a semaphore of one admits exactly one task at a time, so the two cannot
    drift apart as the code changes.

    Structured concurrency, via TaskGroup rather than gather. `gather`
    propagates the first exception but leaves its siblings running, detached --
    which here means orphaned browser contexts accumulating in a Chromium
    process after the experiment has already "failed". A TaskGroup cancels and
    awaits its children before the block exits, so nothing escapes.

    The cost of TaskGroup is that failures arrive as an ExceptionGroup. That is
    the accurate shape: several sessions really can fail at the same instant.

    Memory scales with `count`, because every task is created up front. At a
    few thousand sessions that is a few MB. For a much larger run the right
    shape is a fixed set of workers pulling from an asyncio.Queue, which bounds
    memory to the worker count instead.
    """
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")

    run_id = experiment_id or new_experiment_id()
    started_at = time.time()
    began = time.monotonic()

    # Proxies are assigned BEFORE anything runs. Pulling from the pool inside
    # each task would make assignment depend on scheduling order, so session 3
    # would get a different proxy on every run and experiments would stop being
    # reproducible. Execution order is nondeterministic; assignment is not.
    assignments = [
        (session_id, pool.next_proxy() if pool is not None else None)
        for session_id in range(1, count + 1)
    ]

    # Pre-sized, written by index: results stay ordered by session_id even
    # though sessions finish in whatever order they finish.
    slots: list[SessionResult | None] = [None] * count
    limiter = asyncio.Semaphore(concurrency)

    async def run_one(index: int, session_id: int, proxy: Proxy | None) -> None:
        async with limiter:
            result = await run_session(experiment_id=run_id, session_id=session_id, proxy=proxy)

        # Deliberately outside the semaphore: a slow results writer must not
        # hold a concurrency slot. `on_result` is sync and contains no await,
        # so it cannot interleave with another task even when several finish
        # at once -- the same reasoning that lets ProxyPool run without a lock.
        slots[index] = result
        if on_result is not None:
            on_result(result)

    async with asyncio.TaskGroup() as group:
        for index, (session_id, proxy) in enumerate(assignments):
            group.create_task(run_one(index, session_id, proxy))

    return ExperimentMetrics(
        experiment_id=run_id,
        started_at=started_at,
        total_ms=round((time.monotonic() - began) * 1000, 3),
        results=tuple(result for result in slots if result is not None),
    )
