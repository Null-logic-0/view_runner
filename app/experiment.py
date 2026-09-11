"""Composition root: assemble config, proxies and a browser into a running experiment.

Somewhere, something has to know about all the concrete pieces at once. That
job is real, but it is a different job from orchestration, so it lives here
rather than in `runner.py` -- which stays ignorant of browsers and therefore
stays fast to test.

The pattern is worth naming: push everything that "knows about everything" to
one edge of the system, and keep the middle unaware.
"""

import logging
from typing import cast

from app.browser.factory import BrowserFactory
from app.browser.session import WaitUntil, run_session
from app.config import Config
from app.errors import ConfigurationError
from app.proxy.parser import ParseReport, Proxy, load_proxies
from app.proxy.pool import ProxyPool
from app.runner import ResultCallback, run_experiment
from app.telemetry.metrics import ExperimentMetrics, SessionResult

# Module-level logger, unconfigured on purpose. A library logs unconditionally
# and lets the application decide where it goes; until Phase 12 attaches
# handlers this is a no-op rather than stray output on someone's stdout.
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

    # Raises ProxyPoolEmptyError if nothing usable survived -- fatal, because
    # "proxies enabled" plus "no proxies" is an experiment that cannot run.
    return ProxyPool(report.proxies), report


async def run_experiment_from_config(
    config: Config,
    *,
    experiment_id: str | None = None,
    on_result: ResultCallback | None = None,
) -> ExperimentMetrics:
    """Run the experiment a `Config` describes."""
    # Checked before anything is launched. Silently running sequentially when
    # the file says concurrency = 5 would produce a clean run, identical
    # throughput, and the false conclusion that concurrency does not help.
    # For a measurement tool, ignoring a parameter is worse than refusing.
    if config.runner.concurrency != 1:
        raise ConfigurationError(
            [
                f"runner.concurrency is {config.runner.concurrency}, but only "
                "sequential execution is implemented; set it to 1"
            ]
        )

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
            pool=pool,
            experiment_id=experiment_id,
            on_result=on_result,
        )


__all__ = ["build_proxy_pool", "run_experiment_from_config"]
