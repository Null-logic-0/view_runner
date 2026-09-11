"""Unit tests for the composition root.

These import the browser layer but never launch a browser: the concurrency
guard runs before anything is started, and proxy loading is pure file I/O.
"""

from pathlib import Path

import pytest

from app.errors import ConfigurationError, ProxyPoolEmptyError
from app.experiment import build_proxy_pool, run_experiment_from_config
from tests.conftest import make_config


async def test_concurrency_above_one_is_refused_not_silently_ignored() -> None:
    """Silently running sequentially would produce a false experimental result."""
    config = make_config(runner={"concurrency": 5})
    with pytest.raises(ConfigurationError, match="only sequential execution is implemented"):
        await run_experiment_from_config(config)


async def test_the_guard_runs_before_anything_is_launched() -> None:
    """Config points at an unreadable proxy file; the guard must fire first."""
    config = make_config(
        runner={"concurrency": 2},
        proxy={"enabled": True, "file": "/nonexistent/proxies.txt"},
    )
    with pytest.raises(ConfigurationError):
        await run_experiment_from_config(config)


def test_no_pool_is_built_when_proxies_are_disabled() -> None:
    pool, report = build_proxy_pool(make_config(proxy={"enabled": False}))
    assert pool is None
    assert report is None


def test_pool_is_built_from_the_configured_file(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("1.2.3.4:8080\n5.6.7.8:3128\n", encoding="utf-8")

    pool, report = build_proxy_pool(make_config(proxy={"enabled": True, "file": str(path)}))

    assert pool is not None and len(pool) == 2
    assert report is not None and report.ok
    assert pool.next_proxy().label == "1.2.3.4:8080"


def test_default_scheme_from_config_reaches_the_parsed_proxies(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("1.2.3.4:1080\n", encoding="utf-8")

    pool, _ = build_proxy_pool(
        make_config(proxy={"enabled": True, "file": str(path), "default_scheme": "socks5"})
    )
    assert pool is not None
    assert pool.next_proxy().server == "socks5://1.2.3.4:1080"


def test_malformed_lines_are_reported_but_do_not_stop_the_run(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("1.2.3.4:8080\nGARBAGE\n5.6.7.8:3128\n", encoding="utf-8")

    pool, report = build_proxy_pool(make_config(proxy={"enabled": True, "file": str(path)}))

    assert pool is not None and len(pool) == 2
    assert report is not None and len(report.errors) == 1


def test_a_file_with_no_usable_proxies_is_fatal(tmp_path: Path) -> None:
    """Proxies enabled plus no proxies is an experiment that cannot run."""
    path = tmp_path / "p.txt"
    path.write_text("# only comments\n\n", encoding="utf-8")

    with pytest.raises(ProxyPoolEmptyError):
        build_proxy_pool(make_config(proxy={"enabled": True, "file": str(path)}))
