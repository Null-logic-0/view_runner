"""Composition root: assemble config, proxies and a browser into a running experiment.

Somewhere, something has to know about all the concrete pieces at once. That
job is real, but it is a different job from orchestration, so it lives here
rather than in `runner.py` -- which stays ignorant of browsers and therefore
stays fast to test.

The pattern is worth naming: push everything that "knows about everything" to
one edge of the system, and keep the middle unaware.
"""

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from app.browser.factory import BrowserFactory
from app.browser.session import WaitUntil, run_session
from app.config import Config
from app.proxy.parser import ParseReport, Proxy, load_proxies
from app.proxy.pool import ProxyPool
from app.runner import ResultCallback, new_experiment_id, run_experiment
from app.telemetry.metrics import ExperimentMetrics, SessionResult
from app.telemetry.results import ResultWriter, write_summary

logger = logging.getLogger(__name__)


def build_proxy_pool(config: Config) -> tuple[ProxyPool | None, ParseReport | None]:
    """Load and validate the proxy file, if proxies are enabled.

    Returns the report alongside the pool so the caller can surface malformed
    entries. Skipping bad lines silently would mean an experiment quietly ran
    against a smaller pool than the file describes.
    """
    if not config.proxy.enabled:
        return None, None

    report = load_proxies(config.proxy.file, default_scheme=config.proxy.default_scheme)
    logger.info("proxy file %s: %s", config.proxy.file, report.summary())
    for error in report.errors:
        logger.warning("proxy file %s: %s", config.proxy.file, error)

    return ProxyPool(report.proxies), report


async def run_experiment_from_config(
    config: Config,
    *,
    experiment_id: str | None = None,
    on_result: ResultCallback | None = None,
) -> ExperimentMetrics:
    """Run the experiment a `Config` describes."""
    pool, _report = build_proxy_pool(config)

    async with BrowserFactory(config) as factory:

        async def session(
            *, experiment_id: str, session_id: int, proxy: Proxy | None
        ) -> SessionResult:
            return await run_session(
                factory=factory,
                target_url=config.target.url,
                duration_s=config.session.duration_seconds,
                experiment_id=experiment_id,
                session_id=session_id,
                proxy=proxy,
                expected_status=config.target.expected_status,
                # Safe: config validated this against the same tuple of
                # literals that WaitUntil is built from.
                wait_until=cast(WaitUntil, config.session.wait_until),
            )

        return await run_experiment(
            run_session=session,
            count=config.session.count,
            concurrency=config.runner.concurrency,
            pool=pool,
            experiment_id=experiment_id,
            on_result=on_result,
        )


__all__ = ["build_proxy_pool", "config_snapshot", "run_and_record", "run_experiment_from_config"]


def config_snapshot(config: Config) -> dict[str, Any]:
    """The config as plain data, to be stored beside the numbers it produced.

    Converted here rather than in telemetry so that `telemetry` stays a leaf
    package and never has to import Config.
    """
    return asdict(config)


async def run_and_record(
    config: Config, *, experiment_id: str | None = None
) -> tuple[ExperimentMetrics, Path | None]:
    """Run an experiment and write its results to disk.

    Returns the metrics and the path of the JSONL file, or None if
    `telemetry.results_dir` is empty (results disabled).

    The JSONL file is opened for the whole run and each row is flushed as it
    arrives, so the partial results of a run that crashes at session 48 are
    still on disk and still valid JSONL.
    """
    run_id = experiment_id or new_experiment_id()

    if not config.telemetry.writes_results:
        logger.warning("telemetry.results_dir is empty; results will not be written to disk")
        return await run_experiment_from_config(config, experiment_id=run_id), None

    results_dir = config.telemetry.results_dir
    jsonl_path = results_dir / f"{run_id}.jsonl"

    with ResultWriter(jsonl_path) as writer:
        metrics = await run_experiment_from_config(
            config, experiment_id=run_id, on_result=writer.write
        )

    write_summary(results_dir / f"{run_id}.summary.json", metrics, config=config_snapshot(config))
    logger.info("results written to %s", jsonl_path, extra={"path": str(jsonl_path)})
    return metrics, jsonl_path
