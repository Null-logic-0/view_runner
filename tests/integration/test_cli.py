"""End-to-end CLI tests: real argv, real browser, real exit codes.

These are plain synchronous tests. `main()` calls asyncio.run() itself, which
is exactly how a shell invokes it, and running them as async tests would mean
testing a code path no user takes.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app import cli
from lab.target_server import TargetServer

pytestmark = pytest.mark.integration


def test_doctor_passes_on_a_healthy_setup(local_server: TargetServer, capsys: Any) -> None:
    code = cli.main(["doctor", "--url", local_server.base_url, "--results-dir", ""])
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "Python" in out
    assert "chromium" in out
    assert "(direct, no proxy)" in out


def test_doctor_fails_when_the_configuration_is_invalid(capsys: Any) -> None:
    code = cli.main(["doctor", "--url", "not-a-url"])
    assert code == cli.EXIT_CANNOT_RUN
    assert "must start with http" in capsys.readouterr().out


def test_run_executes_an_experiment_and_writes_results(
    local_server: TargetServer, tmp_path: Path, capsys: Any
) -> None:
    code = cli.main(
        [
            "run",
            "--url",
            local_server.base_url,
            "-n",
            "3",
            "-d",
            "0",
            "--results-dir",
            str(tmp_path / "out"),
            "--log-level",
            "ERROR",
        ]
    )
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert "Completed:" in out
    assert "Success rate:" in out
    assert len(local_server.hits) == 3

    written = list((tmp_path / "out").glob("*.jsonl"))
    assert len(written) == 1
    rows = [json.loads(line) for line in written[0].read_text(encoding="utf-8").splitlines()]
    assert [row["session_id"] for row in rows] == [1, 2, 3]


def test_failing_sessions_alone_do_not_make_the_command_fail(
    local_server: TargetServer, closed_port: int, capsys: Any
) -> None:
    """Sessions failing is a measurement. The old project exited 0 always; this
    exits 0 only because the run itself was sound."""
    code = cli.main(
        [
            "run",
            "--url",
            f"http://127.0.0.1:{closed_port}/",
            "-n",
            "2",
            "-d",
            "0",
            "--results-dir",
            "",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == cli.EXIT_OK
    assert "0.0%" in capsys.readouterr().out


def test_fail_under_turns_a_bad_success_rate_into_a_non_zero_exit(
    closed_port: int, capsys: Any
) -> None:
    code = cli.main(
        [
            "run",
            "--url",
            f"http://127.0.0.1:{closed_port}/",
            "-n",
            "2",
            "-d",
            "0",
            "--results-dir",
            "",
            "--fail-under",
            "0.9",
            "--log-level",
            "ERROR",
        ]
    )
    assert code == cli.EXIT_ABORTED
    assert "below --fail-under" in capsys.readouterr().err


def test_an_unreadable_proxy_file_is_a_clean_error_not_a_traceback(
    local_server: TargetServer, tmp_path: Path, capsys: Any
) -> None:
    code = cli.main(
        [
            "run",
            "--url",
            local_server.base_url,
            "--proxy-file",
            str(tmp_path / "absent.txt"),
            "--log-level",
            "ERROR",
        ]
    )
    assert code == cli.EXIT_CANNOT_RUN
    assert "proxy file not found" in capsys.readouterr().err
