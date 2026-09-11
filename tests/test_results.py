"""Unit tests for durable experiment output."""

import json
from pathlib import Path

import pytest

from app.telemetry.metrics import ExperimentMetrics, SessionResult, SessionStatus
from app.telemetry.results import ResultWriter, environment_snapshot, write_summary


def make_result(session_id: int, status: SessionStatus = SessionStatus.COMPLETED) -> SessionResult:
    return SessionResult(
        experiment_id="exp-1",
        session_id=session_id,
        status=status,
        proxy_label="1.2.3.4:8080",
        started_at=1_700_000_000.0,
        total_ms=30_012.4,
        setup_ms=34.7,
        navigation_ms=8.6 if status.ok else None,
        dwell_ms=30_000.0 if status.ok else None,
        http_status=200 if status.ok else None,
        error_type=None if status.ok else "Error",
    )


def read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------ writing --


def test_one_line_per_session(tmp_path: Path) -> None:
    path = tmp_path / "e.jsonl"
    with ResultWriter(path) as writer:
        for session_id in range(1, 4):
            writer.write(make_result(session_id))

    rows = read_lines(path)
    assert [row["session_id"] for row in rows] == [1, 2, 3]
    assert rows[0]["status"] == "completed"
    assert rows[0]["total_ms"] == 30_012.4


def test_missing_timings_are_null_not_zero(tmp_path: Path) -> None:
    """Typed output is the reason for JSONL over CSV: null stays null."""
    path = tmp_path / "e.jsonl"
    with ResultWriter(path) as writer:
        writer.write(make_result(1, SessionStatus.FAILED_PROXY))

    row = read_lines(path)[0]
    assert row["navigation_ms"] is None
    assert row["dwell_ms"] is None


def test_rows_are_flushed_as_they_are_written(tmp_path: Path) -> None:
    """A crash at session 48 of 50 must leave 47 valid rows on disk."""
    path = tmp_path / "e.jsonl"
    with ResultWriter(path) as writer:
        writer.write(make_result(1))
        writer.write(make_result(2))
        # Read while the writer is still open, as a crashed run would leave it.
        assert len(read_lines(path)) == 2
        writer.write(make_result(3))

    assert len(read_lines(path)) == 3


def test_missing_directories_are_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deeper" / "e.jsonl"
    with ResultWriter(path) as writer:
        writer.write(make_result(1))
    assert path.exists()


def test_existing_results_are_never_clobbered(tmp_path: Path) -> None:
    """Results record something that happened; overwriting them must be impossible."""
    path = tmp_path / "e.jsonl"
    path.write_text("existing data\n", encoding="utf-8")

    with pytest.raises(FileExistsError), ResultWriter(path):
        pass  # pragma: no cover - the open must fail

    assert path.read_text(encoding="utf-8") == "existing data\n"


def test_writing_outside_the_context_manager_is_an_error(tmp_path: Path) -> None:
    writer = ResultWriter(tmp_path / "e.jsonl")
    with pytest.raises(RuntimeError, match="not open"):
        writer.write(make_result(1))


def test_written_count_is_reported(tmp_path: Path) -> None:
    with ResultWriter(tmp_path / "e.jsonl") as writer:
        writer.write(make_result(1))
        writer.write(make_result(2))
        assert writer.written == 2


def test_no_credentials_can_reach_a_results_file(tmp_path: Path) -> None:
    """Structural, not procedural: SessionResult holds a label, never a Proxy."""
    path = tmp_path / "e.jsonl"
    with ResultWriter(path) as writer:
        writer.write(make_result(1))

    text = path.read_text(encoding="utf-8")
    assert "1.2.3.4:8080" in text
    assert "password" not in text


# ------------------------------------------------------------------ summary --


def test_summary_records_counts_timings_and_config(tmp_path: Path) -> None:
    metrics = ExperimentMetrics(
        experiment_id="exp-1",
        started_at=1_700_000_000.0,
        total_ms=90_000.0,
        results=(make_result(1), make_result(2), make_result(3, SessionStatus.FAILED_PROXY)),
    )
    path = tmp_path / "e.summary.json"
    write_summary(path, metrics, config={"session": {"count": 3}})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["experiment_id"] == "exp-1"
    assert payload["counts"]["sessions_completed"] == 2
    assert payload["counts"]["proxy_connection_failures"] == 1
    assert payload["counts"]["failures_via_proxy"] == 1
    assert payload["status_counts"]["failed_proxy"] == 1
    assert payload["timings_ms"]["average_navigation"] == 8.6
    assert payload["success_rate"] == pytest.approx(2 / 3)
    assert payload["config"] == {"session": {"count": 3}}


def test_summary_records_where_the_numbers_came_from(tmp_path: Path) -> None:
    """Timings are only comparable across machines if you know which machine."""
    metrics = ExperimentMetrics("e", started_at=0.0, total_ms=1.0, results=())
    path = tmp_path / "e.summary.json"
    write_summary(path, metrics)

    environment = json.loads(path.read_text(encoding="utf-8"))["environment"]
    assert environment["python"].startswith("3.")
    assert environment["playwright"] != "unknown"
    assert environment["machine"]


def test_environment_snapshot_is_json_safe() -> None:
    assert json.loads(json.dumps(environment_snapshot()))
