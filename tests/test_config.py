"""Unit tests for configuration loading and validation.

Almost every test here passes a plain dict to `build_config`. That is the
payoff of splitting parsing from I/O: no temp files, no fixtures, no disk.
Only the three `load_config` tests at the bottom touch the filesystem.
"""

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from app.config import MAX_CONCURRENCY, build_config, load_config
from app.errors import ConfigurationError

MINIMAL: dict[str, Any] = {"target": {"url": "http://127.0.0.1:8000/"}}


def problems_from(data: dict[str, Any]) -> tuple[str, ...]:
    """Run build_config expecting failure, and return the collected problems."""
    with pytest.raises(ConfigurationError) as exc_info:
        build_config(data)
    return exc_info.value.problems


# happy path


def test_minimal_config_applies_every_default() -> None:
    config = build_config(MINIMAL)

    assert config.target.url == "http://127.0.0.1:8000/"
    assert config.session.count == 10
    assert config.session.duration_seconds == 30.0
    assert config.browser.engine == "chromium"
    assert config.browser.headless is True
    assert config.timeouts.navigation_ms == 30_000
    assert config.proxy.enabled is False
    assert config.proxy.file == Path("proxies.txt")
    assert config.runner.concurrency == 1


def test_values_override_defaults() -> None:
    config = build_config(
        {
            "target": {"url": "https://example.test/page"},
            "session": {"count": 3, "duration_seconds": 1.5},
            "browser": {"engine": "firefox", "headless": False, "locale": "de-DE"},
            "timeouts": {"launch_ms": 5_000, "navigation_ms": 10_000},
            "proxy": {"enabled": True, "file": "custom.txt", "default_scheme": "socks5"},
            "runner": {"concurrency": 4},
        }
    )

    assert config.session.count == 3
    assert config.session.duration_seconds == 1.5
    assert config.browser.headless is False
    assert config.browser.locale == "de-DE"
    assert config.proxy.file == Path("custom.txt")
    assert config.proxy.default_scheme == "socks5"
    assert config.runner.concurrency == 4


def test_toml_integer_is_accepted_for_a_float_field() -> None:
    """TOML distinguishes 30 from 30.0; both must mean 30 seconds."""
    config = build_config({**MINIMAL, "session": {"duration_seconds": 30}})
    assert config.session.duration_seconds == 30.0
    assert isinstance(config.session.duration_seconds, float)


def test_zero_duration_is_allowed() -> None:
    """Navigate-then-close is a valid experiment: it measures startup cost."""
    config = build_config({**MINIMAL, "session": {"duration_seconds": 0}})
    assert config.session.duration_seconds == 0.0


def test_config_is_immutable() -> None:
    config = build_config(MINIMAL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.session.count = 99  # type: ignore[misc]


# target


def test_missing_target_section_is_reported() -> None:
    assert any("target.url is required" in p for p in problems_from({}))


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test",  # wrong scheme
        "example.test",  # no scheme at all
        "http://",  # no host
        "",  # empty
    ],
)
def test_invalid_urls_are_rejected(url: str) -> None:
    assert problems_from({"target": {"url": url}})


def test_url_must_be_a_string() -> None:
    assert any("expected a string" in p for p in problems_from({"target": {"url": 8000}}))


# numbers


@pytest.mark.parametrize("count", [0, -1])
def test_session_count_must_be_positive(count: int) -> None:
    problems = problems_from({**MINIMAL, "session": {"count": count}})
    assert any("session.count must be >= 1" in p for p in problems)


def test_negative_duration_is_rejected() -> None:
    problems = problems_from({**MINIMAL, "session": {"duration_seconds": -5}})
    assert any("session.duration_seconds must be >= 0" in p for p in problems)


def test_booleans_are_not_accepted_as_integers() -> None:
    """bool subclasses int in Python, so `count = true` would otherwise be 1."""
    problems = problems_from({**MINIMAL, "session": {"count": True}})
    assert any("expected an integer, got a boolean" in p for p in problems)


@pytest.mark.parametrize("concurrency", [0, -3, MAX_CONCURRENCY + 1])
def test_concurrency_is_bounded_at_both_ends(concurrency: int) -> None:
    problems = problems_from({**MINIMAL, "runner": {"concurrency": concurrency}})
    assert any("runner.concurrency" in p for p in problems)


def test_concurrency_ceiling_is_inclusive() -> None:
    config = build_config({**MINIMAL, "runner": {"concurrency": MAX_CONCURRENCY}})
    assert config.runner.concurrency == MAX_CONCURRENCY


# choices


def test_unknown_browser_engine_lists_the_valid_options() -> None:
    problems = problems_from({**MINIMAL, "browser": {"engine": "safari"}})
    assert any("chromium, firefox, webkit" in p for p in problems)


def test_unknown_proxy_scheme_is_rejected() -> None:
    assert problems_from({**MINIMAL, "proxy": {"default_scheme": "gopher"}})


# typos and unknowns


def test_typo_in_a_key_is_an_error_with_a_suggestion() -> None:
    """The dangerous case: silently ignoring this would measure 30s, not 5s."""
    problems = problems_from({**MINIMAL, "session": {"durations_seconds": 5}})
    assert len(problems) == 1
    assert "session.durations_seconds is not a known setting" in problems[0]
    assert "did you mean 'duration_seconds'?" in problems[0]


def test_typo_in_a_section_name_is_an_error_with_a_suggestion() -> None:
    problems = problems_from({**MINIMAL, "sessions": {"count": 5}})
    assert any("[sessions] is not a known section" in p for p in problems)
    assert any("did you mean [session]?" in p for p in problems)


def test_section_of_the_wrong_type_is_reported() -> None:
    problems = problems_from({**MINIMAL, "runner": 5})
    assert any("[runner] must be a table" in p for p in problems)


# error collection


def test_every_problem_is_reported_in_one_pass() -> None:
    """Fail fast in time, but exhaustively in content: one run, all problems."""
    problems = problems_from(
        {
            "target": {"url": "ftp://nope"},
            "session": {"count": 0, "duration_seconds": -1},
            "browser": {"engine": "safari"},
            "runner": {"concurrency": 0},
        }
    )
    assert len(problems) == 5


def test_error_message_lists_each_problem_on_its_own_line() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        build_config({"target": {"url": "nope"}, "runner": {"concurrency": 0}})

    rendered = str(exc_info.value)
    assert rendered.startswith("2 configuration problems")
    assert rendered.count("\n  - ") == 2


def test_source_is_named_in_the_message_when_known() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        build_config({}, source="configs/broken.toml")
    assert "in configs/broken.toml" in str(exc_info.value)


# file loading


def test_load_config_reads_a_real_file(tmp_path: Path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        '[target]\nurl = "http://127.0.0.1:8000/"\n\n[session]\ncount = 2\n',
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.session.count == 2


def test_load_config_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="config file not found"):
        load_config(tmp_path / "absent.toml")


def test_load_config_reports_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[target\nurl = ", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_config(path)


def test_shipped_example_config_is_valid() -> None:
    """Guards against the example drifting out of sync with the validator."""
    config = load_config(Path("configs/example.toml"))
    assert config.target.url.startswith("http")
