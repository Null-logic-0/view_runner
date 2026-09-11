"""Observability: measurements, structured logging, and durable results.

A leaf package. Nothing here imports from the rest of `app`, which is what
lets every other module import it without creating a cycle.
"""

from app.telemetry.logger import configure_logging, scrub
from app.telemetry.metrics import ExperimentMetrics, SessionResult, SessionStatus
from app.telemetry.results import ResultWriter, environment_snapshot, write_summary

__all__ = [
    "ExperimentMetrics",
    "ResultWriter",
    "SessionResult",
    "SessionStatus",
    "configure_logging",
    "environment_snapshot",
    "scrub",
    "write_summary",
]
