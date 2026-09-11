"""Load and validate experiment configuration.

Two entry points, deliberately separated:

    build_config(mapping) -> Config     pure; no I/O; the unit-test seam
    load_config(path)     -> Config     reads a file, then delegates

Everything here answers "is this a well-formed experiment description?".
It does *not* answer "will it work on this machine?" -- whether proxies.txt
exists or Chromium is installed are environment questions, handled by the
`doctor` command in Phase 18.
"""

import difflib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from app.errors import ConfigurationError

MAX_CONCURRENCY: Final = 64

_ENGINES: Final = ("chromium", "firefox", "webkit")
_PROXY_SCHEMES: Final = ("http", "https", "socks5")
_URL_SCHEMES: Final = ("http", "https")
_WAIT_UNTIL: Final = ("commit", "domcontentloaded", "load", "networkidle")
_DEFAULT_PROXY_FILE: Final = Path("proxies.txt")


@dataclass(frozen=True, slots=True)
class TargetConfig:
    """What we point the browser at. No default: a target is always required."""

    url: str
    # Any other status means the page did not load as intended, so the session
    # is a failure rather than something to sit on for 30 seconds.
    expected_status: int = 200


@dataclass(frozen=True, slots=True)
class SessionConfig:
    count: int = 10
    duration_seconds: float = 30.0
    # What "navigated" means, and therefore what navigation_ms measures.
    # "load" waits for the page and its subresources; "commit" returns as soon
    # as the first bytes of the response arrive. Every latency number in every
    # experiment depends on this, so it belongs in config, not in a default.
    wait_until: str = "load"


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    engine: str = "chromium"
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    locale: str = "en-US"
    # Empty means "leave the browser's own User-Agent alone". Setting an
    # identifiable value is recommended against your own targets: it makes
    # server logs readable and lets you filter lab traffic out of analytics.
    user_agent: str = ""


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """Its own section because launch belongs to the factory and navigation
    belongs to the session -- nesting both under [browser] would mislead."""

    launch_ms: int = 30_000
    navigation_ms: int = 30_000


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    enabled: bool = False
    file: Path = _DEFAULT_PROXY_FILE
    # proxies.txt is bare host:port
    default_scheme: str = "http"


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    concurrency: int = 1


@dataclass(frozen=True, slots=True)
class Config:
    target: TargetConfig
    session: SessionConfig = field(default_factory=SessionConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)


# Validation helper

_MISSING: Final = object()

_TYPE_NAMES: Final = {
    bool: "a boolean",
    int: "an integer",
    float: "a float",
    str: "a string",
    list: "an array",
    dict: "a table",
}


def _kind(value: object) -> str:
    return _TYPE_NAMES.get(type(value), type(value).__name__)


class _Section:
    """Validated access to one ``[section]`` of a parsed TOML document.

    This exists to kill repetition. Every field needs the same four checks --
    present? right type? in range? allowed value? -- and doing that inline for
    twelve fields is ~100 lines of near-identical `if`s, which is where
    inconsistent error messages and copy-paste bugs come from.

    It never raises. Problems are appended to a shared list so that one run
    reports *all* of them; the caller raises once at the end.
    """

    def __init__(self, name: str, data: Any, problems: list[str]) -> None:
        self._name = name
        self._problems = problems
        self._asked: set[str] = set()  # doubles as "the set of valid keys"

        if data is _MISSING or data is None:
            self._data: Mapping[str, Any] = {}
        elif isinstance(data, Mapping):
            self._data = data
        else:
            self._data = {}
            problems.append(f"[{name}] must be a table, got {_kind(data)}")

    # --- internals ---------------------------------------------------------- #

    def _take(self, key: str) -> Any:
        self._asked.add(key)
        return self._data.get(key, _MISSING)

    def _fail(self, key: str, message: str) -> None:
        self._problems.append(f"{self._name}.{key} {message}")

    # --- typed getters ------------------------------------------------------ #

    def boolean(self, key: str, default: bool) -> bool:
        raw = self._take(key)
        if raw is _MISSING:
            return default
        if not isinstance(raw, bool):
            self._fail(key, f"expected a boolean (true/false), got {_kind(raw)}")
            return default
        return raw

    def integer(
        self, key: str, default: int, *, minimum: int | None = None, maximum: int | None = None
    ) -> int:
        raw = self._take(key)
        if raw is _MISSING:
            return default

        if isinstance(raw, bool) or not isinstance(raw, int):
            self._fail(key, f"expected an integer, got {_kind(raw)}")
            return default
        if minimum is not None and raw < minimum:
            self._fail(key, f"must be >= {minimum}, got {raw}")
            return default
        if maximum is not None and raw > maximum:
            self._fail(key, f"must be <= {maximum}, got {raw}")
            return default
        return raw

    def number(self, key: str, default: float, *, minimum: float | None = None) -> float:
        raw = self._take(key)
        if raw is _MISSING:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            self._fail(key, f"expected a number, got {_kind(raw)}")
            return default
        value = float(raw)
        if minimum is not None and value < minimum:
            self._fail(key, f"must be >= {minimum}, got {value}")
            return default
        return value

    def string(self, key: str, default: str, *, choices: Sequence[str] | None = None) -> str:
        raw = self._take(key)
        if raw is _MISSING:
            return default
        if not isinstance(raw, str):
            self._fail(key, f"expected a string, got {_kind(raw)}")
            return default
        if choices is not None and raw not in choices:
            self._fail(key, f"must be one of {', '.join(choices)}; got {raw!r}")
            return default
        return raw

    def path(self, key: str, default: Path) -> Path:
        raw = self._take(key)
        if raw is _MISSING:
            return default
        if not isinstance(raw, str):
            self._fail(key, f"expected a string path, got {_kind(raw)}")
            return default
        if not raw.strip():
            self._fail(key, "must not be empty")
            return default
        return Path(raw)

    def url(self, key: str) -> str:
        """Required, and structurally a fetchable http(s) URL.

        Deliberately does not resolve DNS or connect -- reachability is an
        environment fact, not a configuration fact.
        """
        raw = self._take(key)
        if raw is _MISSING:
            self._fail(key, 'is required (e.g. url = "http://127.0.0.1:8000/")')
            return ""
        if not isinstance(raw, str):
            self._fail(key, f"expected a string, got {_kind(raw)}")
            return ""
        parts = urlsplit(raw)
        if parts.scheme not in _URL_SCHEMES:
            self._fail(key, f"must start with http:// or https://; got {raw!r}")
            return ""
        if not parts.netloc:
            self._fail(key, f"is missing a host; got {raw!r}")
            return ""
        return raw

    # --- closing ------------------------------------------------------------

    def finish(self) -> None:
        """Report keys we were never asked for.

        A silently-ignored typo means the program runs happily and measures
        something other than what the file says -- the worst failure mode for a
        measurement tool.
        """
        for key in self._data:
            if key in self._asked:
                continue
            near = difflib.get_close_matches(key, sorted(self._asked), n=1)
            hint = f"; did you mean {near[0]!r}?" if near else ""
            self._problems.append(f"{self._name}.{key} is not a known setting{hint}")


# Public API

_SECTIONS: Final = ("target", "session", "browser", "timeouts", "proxy", "runner")


def build_config(data: Mapping[str, Any], *, source: str | None = None) -> Config:
    """Validate an already-parsed mapping into a `Config`.

    Pure: no file access, no environment lookups. Raises `ConfigurationError`
    listing every problem found, or returns a fully valid `Config`.
    """
    problems: list[str] = []

    target = _Section("target", data.get("target", _MISSING), problems)
    url = target.url("url")
    expected_status = target.integer("expected_status", 200, minimum=100, maximum=599)
    target.finish()

    session = _Section("session", data.get("session", _MISSING), problems)
    count = session.integer("count", 10, minimum=1)
    duration = session.number("duration_seconds", 30.0, minimum=0.0)
    wait_until = session.string("wait_until", "load", choices=_WAIT_UNTIL)
    session.finish()

    browser = _Section("browser", data.get("browser", _MISSING), problems)
    engine = browser.string("engine", "chromium", choices=_ENGINES)
    headless = browser.boolean("headless", True)
    viewport_width = browser.integer("viewport_width", 1280, minimum=1)
    viewport_height = browser.integer("viewport_height", 800, minimum=1)
    locale = browser.string("locale", "en-US")
    user_agent = browser.string("user_agent", "")
    browser.finish()

    timeouts = _Section("timeouts", data.get("timeouts", _MISSING), problems)
    launch_ms = timeouts.integer("launch_ms", 30_000, minimum=1)
    navigation_ms = timeouts.integer("navigation_ms", 30_000, minimum=1)
    timeouts.finish()

    proxy = _Section("proxy", data.get("proxy", _MISSING), problems)
    proxy_enabled = proxy.boolean("enabled", False)
    proxy_file = proxy.path("file", _DEFAULT_PROXY_FILE)
    proxy_scheme = proxy.string("default_scheme", "http", choices=_PROXY_SCHEMES)
    proxy.finish()

    runner = _Section("runner", data.get("runner", _MISSING), problems)
    concurrency = runner.integer("concurrency", 1, minimum=1, maximum=MAX_CONCURRENCY)
    runner.finish()

    for key in data:
        if key not in _SECTIONS:
            near = difflib.get_close_matches(key, _SECTIONS, n=1)
            hint = f"; did you mean [{near[0]}]?" if near else ""
            problems.append(f"[{key}] is not a known section{hint}")

    if problems:
        raise ConfigurationError(problems, source=source)

    return Config(
        target=TargetConfig(url=url, expected_status=expected_status),
        session=SessionConfig(count=count, duration_seconds=duration, wait_until=wait_until),
        browser=BrowserConfig(
            engine=engine,
            headless=headless,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            locale=locale,
            user_agent=user_agent,
        ),
        timeouts=TimeoutConfig(launch_ms=launch_ms, navigation_ms=navigation_ms),
        proxy=ProxyConfig(enabled=proxy_enabled, file=proxy_file, default_scheme=proxy_scheme),
        runner=RunnerConfig(concurrency=concurrency),
    )


def load_config(path: Path) -> Config:
    """Read a TOML file from disk and validate it."""
    try:
        with path.open("rb") as handle:  # TOML is UTF-8 by spec tomllib decodes
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigurationError([f"config file not found: {path}"]) from exc
    except OSError as exc:
        raise ConfigurationError([f"could not read {path}: {exc}"]) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError([f"not valid TOML: {exc}"], source=str(path)) from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError([f"not valid UTF-8: {exc}"], source=str(path)) from exc

    return build_config(data, source=str(path))
