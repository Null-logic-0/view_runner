"""What a session measured.

One `SessionResult` per session, always -- success or failure. A failed
session is a data point, not an absence of one: a failure rate computed from
rows that never got written is a failure rate of zero.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class SessionStatus(StrEnum):
    """How a session ended.

    Deliberately finer-grained than success/failure. "3 of 50 failed" prompts
    a shrug; "3 of 50 failed, all FAILED_PROXY" tells you where to look. The
    old project had one `except Exception` and so could only ever say the
    former.
    """

    COMPLETED = "completed"
    FAILED_SETUP = "failed_setup"  # context or page could not be created
    FAILED_PROXY = "failed_proxy"  # the proxy refused, or could not be reached
    FAILED_NAVIGATION = "failed_navigation"  # DNS, refused, reset, TLS
    FAILED_TIMEOUT = "failed_timeout"  # navigation exceeded its budget
    FAILED_STATUS = "failed_status"  # loaded, but not the expected HTTP status

    @property
    def ok(self) -> bool:
        return self is SessionStatus.COMPLETED


@dataclass(frozen=True, slots=True)
class SessionResult:
    """One row of experiment data.

    `proxy_label` is a redacted string, never a `Proxy`. Results are written to
    disk, so holding the object would mean a credentialed proxy could serialise
    its password into a results file. Storing only the pre-redacted label makes
    that leak structurally impossible instead of a rule someone has to remember.

    Timing fields are milliseconds, and each records a phase that *completed*.
    A phase that failed reports None, not the time it spent failing: a column
    that means "time to succeed" in some rows and "time to fail" in others
    cannot be averaged. How long a failure took is `total_ms` minus the phases
    that did finish.

    None is likewise honest where 0.0 would quietly drag an average down.
    """

    experiment_id: str
    session_id: int
    status: SessionStatus
    proxy_label: str | None
    started_at: float
    total_ms: float
    setup_ms: float | None = None
    navigation_ms: float | None = None
    dwell_ms: float | None = None
    http_status: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    #: Attempts made, including the first. > 1 means the session was retried.
    #: Set by the runner, which owns the retry loop; the session itself runs
    #: exactly once and does not know it may be called again.
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status.ok

    @property
    def used_proxy(self) -> bool:
        return self.proxy_label is not None

    @property
    def was_retried(self) -> bool:
        return self.attempts > 1


def _mean(values: Iterable[float | None]) -> float | None:
    """Average of the values that exist.

    None entries are skipped rather than treated as zero. A session that failed
    before navigating has no navigation time; counting it as 0 ms would make
    the fleet look faster the more of it was broken.
    """
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / len(present)


@dataclass(frozen=True, slots=True)
class ExperimentMetrics:
    """Everything one experiment measured, plus the derived view of it.

    Holds the raw rows and computes summaries on demand rather than storing
    pre-aggregated counters. Two reasons: a stored average cannot be
    recomputed a different way once you think of a better question, and the
    raw rows are what get written to disk for later analysis.

    Averages are taken over COMPLETED sessions only. Mixing successes and
    failures into one mean produces a number that moves for two unrelated
    reasons and therefore means nothing.
    """

    experiment_id: str
    started_at: float
    total_ms: float
    results: tuple[SessionResult, ...]
    #: True if the failure policy stopped the run early. The results below are
    #: still valid -- they are just fewer than were requested.
    aborted: bool = False
    abort_reason: str | None = None

    # -- counts ------------------------------------------------------------ #

    @property
    def sessions_started(self) -> int:
        return len(self.results)

    @property
    def completed(self) -> tuple[SessionResult, ...]:
        return tuple(result for result in self.results if result.ok)

    @property
    def sessions_completed(self) -> int:
        return len(self.completed)

    @property
    def sessions_failed(self) -> int:
        return self.sessions_started - self.sessions_completed

    @property
    def success_rate(self) -> float:
        """Fraction in [0, 1]. An experiment with no sessions is 0.0, not a crash."""
        if not self.results:
            return 0.0
        return self.sessions_completed / self.sessions_started

    @property
    def status_counts(self) -> dict[SessionStatus, int]:
        return dict(Counter(result.status for result in self.results))

    @property
    def proxy_failures(self) -> int:
        return sum(1 for r in self.results if r.status is SessionStatus.FAILED_PROXY)

    @property
    def sessions_via_proxy(self) -> int:
        return sum(1 for r in self.results if r.used_proxy)

    @property
    def sessions_retried(self) -> int:
        return sum(1 for r in self.results if r.was_retried)

    @property
    def total_attempts(self) -> int:
        """Sessions plus retries. Divergence from sessions_started is the
        hidden cost of a retry policy."""
        return sum(r.attempts for r in self.results)

    # -- timings (completed sessions only) --------------------------------- #

    @property
    def average_setup_ms(self) -> float | None:
        return _mean(r.setup_ms for r in self.completed)

    @property
    def average_navigation_ms(self) -> float | None:
        return _mean(r.navigation_ms for r in self.completed)

    @property
    def average_dwell_ms(self) -> float | None:
        return _mean(r.dwell_ms for r in self.completed)

    @property
    def average_duration_ms(self) -> float | None:
        return _mean(r.total_ms for r in self.completed)

    # -- reporting ---------------------------------------------------------- #

    def summary(self) -> str:
        def ms(value: float | None) -> str:
            return "-" if value is None else f"{value:,.1f} ms"

        lines = [
            f"Experiment {self.experiment_id}",
            "-" * 44,
            f"{'Sessions:':<24}{self.sessions_started:>18,}",
            f"{'Completed:':<24}{self.sessions_completed:>18,}",
            f"{'Failed:':<24}{self.sessions_failed:>18,}",
        ]
        for status, count in sorted(self.status_counts.items()):
            if not status.ok:
                lines.append(f"{'  ' + status.value:<24}{count:>18,}")

        if self.sessions_retried:
            lines.append(f"{'  (retried):':<24}{self.sessions_retried:>18,}")

        lines += [
            "",
            f"{'Average setup:':<24}{ms(self.average_setup_ms):>18}",
            f"{'Average navigation:':<24}{ms(self.average_navigation_ms):>18}",
            f"{'Average dwell:':<24}{ms(self.average_dwell_ms):>18}",
            f"{'Average duration:':<24}{ms(self.average_duration_ms):>18}",
            "",
            f"{'Success rate:':<24}{self.success_rate:>17.1%}",
            f"{'Sessions via proxy:':<24}{self.sessions_via_proxy:>18,}",
            f"{'Wall clock:':<24}{self.total_ms / 1000:>15,.1f} s",
        ]
        if self.aborted:
            lines += ["", f"ABORTED: {self.abort_reason}"]
        return "\n".join(lines)
