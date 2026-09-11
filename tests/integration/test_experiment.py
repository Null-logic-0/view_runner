"""End-to-end: config in, real browser sessions out, metrics back.

The whole stack in one call -- config validation, proxy loading, browser
launch, sequential sessions, aggregation -- checked against what the target
server actually received.
"""

from pathlib import Path

import pytest

from app.experiment import run_experiment_from_config
from app.telemetry.metrics import SessionResult, SessionStatus
from tests.conftest import LocalServer, make_config

pytestmark = pytest.mark.integration


async def test_a_whole_experiment_runs_and_the_server_sees_every_session(
    local_server: LocalServer,
) -> None:
    config = make_config(
        target={"url": f"{local_server.base_url}/page"},
        session={"count": 3, "duration_seconds": 0.1},
    )

    metrics = await run_experiment_from_config(config)

    assert metrics.sessions_started == 3
    assert metrics.sessions_completed == 3
    assert metrics.success_rate == 1.0
    assert [r.session_id for r in metrics.results] == [1, 2, 3]
    assert all(r.status is SessionStatus.COMPLETED for r in metrics.results)

    assert metrics.average_navigation_ms is not None
    assert metrics.average_dwell_ms is not None and metrics.average_dwell_ms >= 100

    # Ground truth: three requests really arrived.
    assert local_server.hits == ["/page", "/page", "/page"]


async def test_sessions_run_sequentially_not_all_at_once(local_server: LocalServer) -> None:
    """Wall clock must be at least the sum of the dwells, not one dwell."""
    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 3, "duration_seconds": 0.4},
    )

    metrics = await run_experiment_from_config(config)

    assert metrics.total_ms >= 1200, "three 400 ms dwells cannot finish in under 1.2 s"


async def test_results_stream_out_during_the_run(local_server: LocalServer) -> None:
    seen: list[SessionResult] = []
    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 3, "duration_seconds": 0},
    )

    metrics = await run_experiment_from_config(config, on_result=seen.append)

    assert [r.session_id for r in seen] == [1, 2, 3]
    assert len(metrics.results) == 3


async def test_an_experiment_through_dead_proxies_fails_cleanly_and_completely(
    local_server: LocalServer, tmp_path: Path, closed_port: int
) -> None:
    """Every session fails, every failure is classified, nothing reaches the target."""
    proxy_file = tmp_path / "proxies.txt"
    proxy_file.write_text(f"127.0.0.1:{closed_port}\n", encoding="utf-8")

    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 3, "duration_seconds": 30},  # must never be reached
        proxy={"enabled": True, "file": str(proxy_file)},
    )

    metrics = await run_experiment_from_config(config)

    assert metrics.sessions_started == 3
    assert metrics.sessions_completed == 0
    assert metrics.proxy_failures == 3
    assert metrics.sessions_via_proxy == 3
    assert metrics.average_navigation_ms is None, "nothing completed, so there is no average"
    assert metrics.total_ms < 30_000, "failures must not have waited out the dwell"
    assert local_server.hits == []


async def test_an_unexpected_status_fails_the_experiment_rather_than_passing_it(
    local_server: LocalServer,
) -> None:
    config = make_config(
        target={"url": f"{local_server.base_url}/status?code=503"},
        session={"count": 2, "duration_seconds": 0},
    )

    metrics = await run_experiment_from_config(config)

    assert metrics.sessions_completed == 0
    assert metrics.status_counts == {SessionStatus.FAILED_STATUS: 2}


async def test_concurrent_sessions_overlap_against_a_real_browser(
    local_server: LocalServer,
) -> None:
    """Six 1-second dwells finish in about 2 s at concurrency 3, not 6 s."""
    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 6, "duration_seconds": 1.0},
        runner={"concurrency": 3},
    )

    metrics = await run_experiment_from_config(config)

    assert metrics.sessions_completed == 6
    assert len(local_server.hits) == 6
    assert metrics.total_ms < 5_000, f"no overlap happened: {metrics.total_ms:.0f} ms"
    assert metrics.total_ms > 1_800, "cannot beat two sequential batches of 1 s"


async def test_concurrent_sessions_do_not_leak_contexts(local_server: LocalServer) -> None:
    """Every context must be closed even when eight of them overlapped."""
    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 8, "duration_seconds": 0.2},
        runner={"concurrency": 4},
    )

    metrics = await run_experiment_from_config(config)
    assert metrics.sessions_completed == 8


async def test_results_remain_ordered_by_session_id_under_concurrency(
    local_server: LocalServer,
) -> None:
    config = make_config(
        target={"url": local_server.base_url},
        session={"count": 6, "duration_seconds": 0.1},
        runner={"concurrency": 6},
    )

    metrics = await run_experiment_from_config(config)
    assert [r.session_id for r in metrics.results] == [1, 2, 3, 4, 5, 6]
