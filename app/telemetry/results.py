"""Write experiment output to disk.

Two files per experiment::

    results/<id>.jsonl          one JSON object per session, appended as it finishes
    results/<id>.summary.json   aggregate metrics + the config that produced them

Why JSONL for the rows:
    Appendable, so a 25-minute run does not have to hold 50 results in memory
    and hope. Streamable, so `tail -f` works while it runs and a file larger
    than RAM can still be processed. Typed, unlike CSV, so a missing timing
    stays null instead of becoming "" and quietly dragging an average down.
    A single JSON array would need its closing bracket to be valid, so a run
    that crashed would leave an unparseable file.

Why the summary is separate:
    Mixing two record shapes in one JSONL file means every consumer has to
    branch on a type tag before it can do anything.

Why the config is stored alongside the numbers:
    "What settings produced this?" must be answerable in three weeks without
    trusting anyone's memory or shell history.

This module takes the config as a plain mapping rather than importing Config.
`telemetry` is a leaf package -- nothing in it imports from the rest of `app`
except its own sibling -- which is what lets every other layer import it
without any risk of a cycle.
"""

import json
import platform
import sys
from collections.abc import Mapping
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import TracebackType
from typing import Any, Self, TextIO

from app.telemetry.metrics import ExperimentMetrics, SessionResult


def environment_snapshot() -> dict[str, Any]:
    """Where these numbers came from.

    Timings are only comparable across machines if you know which machine.
    """
    try:
        playwright_version = version("playwright")
    except PackageNotFoundError:  # pragma: no cover - only if uninstalled
        playwright_version = "unknown"

    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "playwright": playwright_version,
    }


def result_to_dict(result: SessionResult) -> dict[str, Any]:
    """A SessionResult as plain JSON-safe data.

    No redaction step is needed: `proxy_label` is already a credential-free
    string, because SessionResult was designed in Phase 2 to hold the label
    rather than the Proxy. The safety is structural, not procedural.
    """
    return asdict(result)


class ResultWriter:
    """Append-only JSONL sink, one line per session.

    Opened with mode "x": if a file for this experiment id already exists the
    open fails rather than truncating or interleaving. Results are an immutable
    record of something that happened; clobbering them silently is the one
    behaviour that must not be possible.
    """

    __slots__ = ("_handle", "_written", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None
        self._written = 0

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("x", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def write(self, result: SessionResult) -> None:
        if self._handle is None:
            raise RuntimeError("ResultWriter is not open; use it as a context manager")
        self._handle.write(json.dumps(result_to_dict(result), default=str) + "\n")

        self._handle.flush()
        self._written += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    @property
    def written(self) -> int:
        return self._written

    def __repr__(self) -> str:
        return f"<ResultWriter {self.path} written={self._written}>"


def write_summary(
    path: Path,
    metrics: ExperimentMetrics,
    *,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Write the aggregate view plus the settings that produced it."""
    payload: dict[str, Any] = {
        "experiment_id": metrics.experiment_id,
        "started_at": metrics.started_at,
        "total_ms": metrics.total_ms,
        "environment": environment_snapshot(),
        "counts": {
            "sessions_started": metrics.sessions_started,
            "sessions_completed": metrics.sessions_completed,
            "sessions_failed": metrics.sessions_failed,
            "proxy_failures": metrics.proxy_failures,
            "sessions_via_proxy": metrics.sessions_via_proxy,
        },
        "status_counts": {status.value: count for status, count in metrics.status_counts.items()},
        "timings_ms": {
            "average_setup": metrics.average_setup_ms,
            "average_navigation": metrics.average_navigation_ms,
            "average_dwell": metrics.average_dwell_ms,
            "average_duration": metrics.average_duration_ms,
        },
        "success_rate": metrics.success_rate,
    }
    if config is not None:
        payload["config"] = config

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
