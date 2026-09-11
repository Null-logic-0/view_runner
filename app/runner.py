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
    pool: ProxyPool | None = None,
    experiment_id: str | None = None,
    on_result: ResultCallback | None = None,
) -> ExperimentMetrics:
    """Run `count` sessions one after another and report what happened.

    Strictly sequential. Session N+1 does not begin until session N has
    finished and its result has been recorded -- which is the property that
    makes this version easy to reason about, and the one Phase 11 gives up on
    purpose.

    `on_result` is called as each result arrives rather than at the end. A
    50-session experiment at 30 s each runs for 25 minutes; if results only
    existed in memory until the final return, a crash at session 48 would
    throw away 24 minutes of data. Exceptions from the callback are
    deliberately not caught: a results writer that is silently failing is
    worse than one that stops the run.
    """
    experiment_id = experiment_id or new_experiment_id()
    started_at = time.time()
    began = time.monotonic()

    results: list[SessionResult] = []
    for session_id in range(1, count + 1):
        # Pulled per session rather than zipped up front, so the pool's
        # rotation is what decides -- and Phase 11 can hand out proxies in
        # completion order without changing this line.
        proxy = pool.next_proxy() if pool is not None else None

        result = await run_session(experiment_id=experiment_id, session_id=session_id, proxy=proxy)
        results.append(result)
        if on_result is not None:
            on_result(result)

    return ExperimentMetrics(
        experiment_id=experiment_id,
        started_at=started_at,
        total_ms=round((time.monotonic() - began) * 1000, 3),
        results=tuple(results),
    )
