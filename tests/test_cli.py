"""Unit tests for the CLI. No browser is launched.

Argument parsing, the precedence chain and the exit-code contract are all
testable without starting anything, which is the point of keeping cli.py thin.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app import cli
from app.errors import ConfigurationError, LabError, ProxyParseError

# ------------------------------------------------------------------ parsing --


def test_a_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args([])
    assert exc_info.value.code == 2  # argparse's usage-error code


def test_exit_codes_do_not_collide_with_argparse() -> None:
    """argparse exits 2 for usage errors, and that is the Unix convention.

    Reusing 2 would make "you typed it wrong" indistinguishable from "the
    experiment aborted" to a calling script.
    """
    assert 2 not in {cli.EXIT_OK, cli.EXIT_CANNOT_RUN, cli.EXIT_ABORTED}


def test_proxy_and_no_proxy_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--proxy", "--no-proxy"])


def test_doctor_accepts_the_same_overrides_as_run() -> None:
    """Checking a target should not require writing a config file first."""
    args = cli.build_parser().parse_args(["doctor", "--url", "http://x.test/"])
    assert args.url == "http://x.test/"


# ---------------------------------------------------------------- overrides --


def test_flags_map_onto_config_sections() -> None:
    args = cli.build_parser().parse_args(
        ["run", "--url", "http://x.test/", "-n", "5", "-d", "2.5", "-j", "3"]
    )
    overrides = cli.overrides_from(args)

    assert overrides["target"]["url"] == "http://x.test/"
    assert overrides["session"]["count"] == 5
    assert overrides["session"]["duration_seconds"] == 2.5
    assert overrides["runner"]["concurrency"] == 3


def test_absent_flags_are_none_so_they_cannot_erase_file_settings() -> None:
    overrides = cli.overrides_from(cli.build_parser().parse_args(["run"]))
    assert overrides["session"]["count"] is None
    assert overrides["browser"]["headless"] is None
    assert overrides["proxy"]["enabled"] is None


def test_naming_a_proxy_file_implies_enabling_proxies() -> None:
    args = cli.build_parser().parse_args(["run", "--proxy-file", "p.txt"])
    overrides = cli.overrides_from(args)
    assert overrides["proxy"]["enabled"] is True
    assert overrides["proxy"]["file"] == "p.txt"


def test_no_proxy_wins_even_with_a_file_named() -> None:
    args = cli.build_parser().parse_args(["run", "--proxy-file", "p.txt", "--no-proxy"])
    assert cli.overrides_from(args)["proxy"]["enabled"] is False


def test_headed_flag_turns_headless_off() -> None:
    args = cli.build_parser().parse_args(["run", "--headed"])
    assert cli.overrides_from(args)["browser"]["headless"] is False


# --------------------------------------------------------------- precedence --


def test_flags_override_the_file_but_leave_other_settings_alone(tmp_path: Path) -> None:
    """Phase 5's promise: defaults < file < flags, validated in one place."""
    path = tmp_path / "c.toml"
    path.write_text(
        '[target]\nurl = "http://file.test/"\n\n[session]\ncount = 10\nduration_seconds = 7\n',
        encoding="utf-8",
    )

    args = cli.build_parser().parse_args(["run", "-c", str(path), "-n", "2"])
    config = cli.resolve_config(args)

    assert config.session.count == 2  # flag won
    assert config.session.duration_seconds == 7.0  # file survived
    assert config.target.url == "http://file.test/"  # file survived


def test_a_config_file_is_optional() -> None:
    args = cli.build_parser().parse_args(["run", "--url", "http://x.test/", "-n", "3"])
    config = cli.resolve_config(args)
    assert config.session.count == 3
    assert config.session.duration_seconds == 30.0  # dataclass default


def test_overrides_are_validated_by_the_same_code_as_the_file() -> None:
    args = cli.build_parser().parse_args(["run", "--url", "not-a-url"])
    with pytest.raises(ConfigurationError, match="must start with http"):
        cli.resolve_config(args)


# ----------------------------------------------------------------- commands --


def test_config_command_prints_the_effective_configuration(capsys: Any) -> None:
    code = cli.main(["config", "--url", "http://x.test/", "-n", "4"])
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_OK
    assert payload["target"]["url"] == "http://x.test/"
    assert payload["session"]["count"] == 4
    assert payload["runner"]["concurrency"] == 1


# --------------------------------------------------------------- exit codes --


def test_a_lab_error_is_reported_cleanly_without_a_traceback(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: Any) -> int:
        raise ProxyParseError("proxy file not found: /nope.txt")

    monkeypatch.setitem(cli._COMMANDS, "config", boom)

    assert cli.main(["config"]) == cli.EXIT_CANNOT_RUN
    assert "proxy file not found" in capsys.readouterr().err


def test_an_exception_group_of_lab_errors_is_reported_cleanly(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TaskGroup wraps everything, so the CLI must unwrap before reporting."""

    def boom(_: Any) -> int:
        raise ExceptionGroup("failed", [LabError("disk full"), LabError("also bad")])

    monkeypatch.setitem(cli._COMMANDS, "config", boom)

    assert cli.main(["config"]) == cli.EXIT_CANNOT_RUN
    err = capsys.readouterr().err
    assert "disk full" in err
    assert "also bad" in err


def test_a_genuine_bug_inside_an_exception_group_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hiding a TypeError behind a tidy message helps nobody."""

    def boom(_: Any) -> int:
        raise ExceptionGroup("failed", [LabError("fine"), TypeError("a real bug")])

    monkeypatch.setitem(cli._COMMANDS, "config", boom)

    with pytest.raises(ExceptionGroup):
        cli.main(["config"])


def test_interrupting_returns_the_shell_convention(
    capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setitem(cli._COMMANDS, "config", boom)

    assert cli.main(["config"]) == cli.EXIT_INTERRUPTED
    assert "interrupted" in capsys.readouterr().err
