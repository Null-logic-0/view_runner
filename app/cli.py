"""Command line interface.

    automation-lab run      run an experiment
    automation-lab doctor   check that this machine can run one
    automation-lab config   print the configuration that would be used

argparse rather than click or typer: three commands and ten flags are well
within what the standard library handles, and this project has exactly one
runtime dependency. Adding a second to save boilerplate is a poor trade for a
tool whose reproducibility is the point. Past roughly six commands, or once
shell completion matters, click earns its place.

This module is deliberately thin. It parses arguments, builds a Config, calls
into `app.experiment`, and turns outcomes into exit codes. No experiment logic
lives here.
"""

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from app import __version__
from app.config import Config, build_config, load_config
from app.errors import ConfigurationError, LabError
from app.experiment import build_proxy_pool, run_and_record
from app.telemetry.logger import configure_logging

EXIT_OK: Final = 0
EXIT_CANNOT_RUN: Final = 1

EXIT_ABORTED: Final = 3
EXIT_INTERRUPTED: Final = 130  # 128 + SIGINT, by shell convention

_MIN_PYTHON: Final = (3, 12)


# Argument parsing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="automation-lab",
        description="Browser automation lab: measure sessions, proxies and concurrency.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def with_config(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("-c", "--config", type=Path, help="path to a TOML config file")
        return sub

    def with_overrides(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sub.add_argument("--url", help="target URL (overrides [target] url)")
        sub.add_argument("-n", "--count", type=int, help="number of sessions")
        sub.add_argument("-d", "--duration", type=float, help="seconds to hold each session open")
        sub.add_argument("-j", "--concurrency", type=int, help="sessions in flight at once")
        sub.add_argument("--proxy-file", type=Path, help="proxy file to use (implies --proxy)")
        proxy = sub.add_mutually_exclusive_group()
        proxy.add_argument("--proxy", dest="proxy", action="store_true", default=None)
        proxy.add_argument("--no-proxy", dest="proxy", action="store_false", default=None)
        headless = sub.add_mutually_exclusive_group()
        headless.add_argument("--headless", dest="headless", action="store_true", default=None)
        headless.add_argument(
            "--headed",
            dest="headless",
            action="store_false",
            default=None,
            help="show the browser window",
        )
        sub.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
        sub.add_argument("--log-format", choices=["text", "json"])
        sub.add_argument("--results-dir", type=Path, help="empty string disables writing results")
        return sub

    run = with_overrides(with_config(subcommands.add_parser("run", help="run an experiment")))
    run.add_argument(
        "--fail-under",
        type=float,
        metavar="RATE",
        help="exit non-zero if the success rate is below RATE (0.0-1.0)",
    )

    with_overrides(
        with_config(subcommands.add_parser("doctor", help="check this machine can run experiments"))
    )
    with_overrides(
        with_config(subcommands.add_parser("config", help="print the effective configuration"))
    )
    return parser


def overrides_from(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    """Turn parsed flags into the same shape as a parsed TOML document.

    Every value may be None, meaning "not given"; `merge_overrides` skips those
    so an absent flag never erases a file setting.
    """
    proxy_enabled = getattr(args, "proxy", None)
    proxy_file = getattr(args, "proxy_file", None)
    if proxy_file is not None and proxy_enabled is None:
        proxy_enabled = True  # naming a file implies wanting to use it

    results_dir = getattr(args, "results_dir", None)
    return {
        "target": {"url": getattr(args, "url", None)},
        "session": {
            "count": getattr(args, "count", None),
            "duration_seconds": getattr(args, "duration", None),
        },
        "browser": {"headless": getattr(args, "headless", None)},
        "runner": {"concurrency": getattr(args, "concurrency", None)},
        "proxy": {
            "enabled": proxy_enabled,
            "file": str(proxy_file) if proxy_file is not None else None,
        },
        "telemetry": {
            "level": getattr(args, "log_level", None),
            "format": getattr(args, "log_format", None),
            "results_dir": str(results_dir) if results_dir is not None else None,
        },
    }


def resolve_config(args: argparse.Namespace) -> Config:
    """Defaults, then the file if given, then CLI flags -- validated once."""
    overrides = overrides_from(args)
    if args.config is not None:
        return load_config(args.config, overrides=overrides)
    return build_config(
        {
            section: {k: v for k, v in values.items() if v is not None}
            for section, values in overrides.items()
        }
    )


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #


def command_run(args: argparse.Namespace) -> int:
    config = resolve_config(args)
    configure_logging(level=config.telemetry.level, fmt=config.telemetry.format)

    metrics, path = asyncio.run(run_and_record(config))

    print()
    print(metrics.summary())
    if path is not None:
        print(f"\nResults: {path}")

    if metrics.aborted:
        return EXIT_ABORTED
    if args.fail_under is not None and metrics.success_rate < args.fail_under:
        print(
            f"\nsuccess rate {metrics.success_rate:.1%} is below "
            f"--fail-under {args.fail_under:.1%}",
            file=sys.stderr,
        )
        return EXIT_ABORTED
    # Sessions failing is a measurement, not a program error. The old project
    # exited 0 unconditionally; this exits 0 only when the run itself was sound.
    return EXIT_OK


def command_config(args: argparse.Namespace) -> int:
    print(json.dumps(asdict(resolve_config(args)), indent=2, default=str))
    return EXIT_OK


def command_doctor(args: argparse.Namespace) -> int:
    # Doctor IS the report. Quiet the app's own logging so a warning from, say,
    # proxy parsing does not print above the header and confuse the output.
    configure_logging(level="ERROR")

    checks = list(run_checks(args))
    width = max(len(name) for name, _, _ in checks)
    failed = warned = 0

    print("browser-automation-lab doctor\n")
    for name, status, detail in checks:
        mark = {"ok": "✓", "warn": "!", "fail": "✗"}[status]
        print(f"  {mark}  {name.ljust(width)}   {detail}")
        if status == "fail":
            failed += 1
        elif status == "warn":
            warned += 1

    print()
    if failed:
        print(f"{failed} check(s) failed.", file=sys.stderr)
        return EXIT_CANNOT_RUN
    if warned:
        print(f"All checks passed, with {warned} warning(s).")
        return EXIT_OK
    print("All checks passed.")
    return EXIT_OK


CheckResult = tuple[str, str, str]


def run_checks(args: argparse.Namespace) -> Iterator[CheckResult]:
    """Yield checks in dependency order, so the first failure is the root cause."""
    yield _check_python()

    playwright_check = _check_playwright()
    yield playwright_check
    if playwright_check[1] == "ok":
        yield _check_browser()

    config: Config | None = None
    try:
        config = resolve_config(args)
        source = str(args.config) if args.config else "defaults + flags"
        yield ("Configuration", "ok", f"valid ({source})")
    except ConfigurationError as exc:
        # Report the problems, not the "N configuration problems:" header --
        # the header alone tells the reader nothing they can act on.
        yield ("Configuration", "fail", "; ".join(exc.problems))
    except LabError as exc:
        yield ("Configuration", "fail", str(exc).splitlines()[0])

    if config is None:
        return

    yield _check_proxies(config)
    yield _check_results_dir(config)
    yield _check_target(config)


def _check_python() -> CheckResult:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] < _MIN_PYTHON:
        needed = ".".join(str(part) for part in _MIN_PYTHON)
        return ("Python", "fail", f"{version} (need >= {needed})")
    return ("Python", "ok", version)


def _check_playwright() -> CheckResult:
    try:
        from importlib.metadata import version

        return ("Playwright", "ok", version("playwright"))
    except Exception as exc:  # doctor reports problems, it never raises them
        return ("Playwright", "fail", f"not installed ({exc})")


def _check_browser() -> CheckResult:
    """Launch and close a browser. The only check that proves it really works.

    `playwright install chromium` is a separate step from installing the Python
    package, and forgetting it is the single most common setup failure.
    """

    async def probe() -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                return str(browser.version)
            finally:
                await browser.close()

    try:
        return ("Browser", "ok", f"chromium {asyncio.run(probe())}")
    except Exception as exc:  # doctor reports problems, it never raises them
        first = str(exc).splitlines()[0][:80]
        return ("Browser", "fail", f"{first}  (try: playwright install chromium)")


def _check_proxies(config: Config) -> CheckResult:
    if not config.proxy.enabled:
        return ("Proxy file", "ok", "proxies disabled")
    try:
        _pool, report = build_proxy_pool(config)
    except LabError as exc:
        return ("Proxy file", "fail", str(exc))
    if report is None:  # pragma: no cover - proxies enabled always yields a report
        return ("Proxy file", "fail", "no proxy report was produced")
    status = "warn" if report.errors else "ok"
    return ("Proxy file", status, f"{config.proxy.file}: {report.summary()}")


def _check_results_dir(config: Config) -> CheckResult:
    if not config.telemetry.writes_results:
        return ("Results dir", "warn", "disabled; results will not be written")
    directory = config.telemetry.results_dir
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".doctor-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return ("Results dir", "fail", f"{directory} is not writable ({exc})")
    return ("Results dir", "ok", f"{directory} is writable")


def _check_target(config: Config) -> CheckResult:
    """A direct request, ignoring proxy settings.

    A warning rather than a failure: the target being down right now does not
    mean the lab is misconfigured, and this check does not go through a proxy
    so a pass here does not prove the real path works.
    """
    url = config.target.url
    request = urllib.request.Request(url, headers={"User-Agent": "automation-lab/doctor"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code = int(response.status)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return ("Target URL", "warn", f"{url} unreachable ({exc})")

    expected = config.target.expected_status
    if code != expected:
        return ("Target URL", "warn", f"{url} returned {code}, expected {expected}")
    return ("Target URL", "ok", f"{url} -> {code} (direct, no proxy)")


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

_COMMANDS: Final = {
    "run": command_run,
    "doctor": command_doctor,
    "config": command_config,
}


def _leaves(error: BaseException) -> Iterator[BaseException]:
    """Flatten a (possibly nested) ExceptionGroup into its actual exceptions."""
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            yield from _leaves(child)
    else:
        yield error


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _COMMANDS[args.command](args)
    except LabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    except ExceptionGroup as group:
        # TaskGroup wraps everything. If every leaf is one of ours, report it
        # cleanly; if any leaf is a genuine bug, re-raise so the traceback
        # survives. Hiding a TypeError behind a tidy message helps nobody.
        leaves = list(_leaves(group))
        if not all(isinstance(leaf, LabError) for leaf in leaves):
            raise
        for leaf in leaves:
            print(f"error: {leaf}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
