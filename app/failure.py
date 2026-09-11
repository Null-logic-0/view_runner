"""When to retry, when to give up, and when to stop the whole experiment.

Three questions, three answers:

    retryable?   would doing the same thing again plausibly differ?
    end session? not retryable, or out of attempts
    end run?     structural errors (already exceptions), or failures piling up

Imports only `errors` and `telemetry`, so the policy can be unit tested without
a browser, a network, or a config file.
"""

from dataclasses import dataclass
from typing import Final

from app.telemetry.metrics import SessionResult, SessionStatus

#: Failures where a second attempt could plausibly succeed.
RETRYABLE_STATUSES: Final = frozenset(
    {
        SessionStatus.FAILED_PROXY,  # a different proxy may work
        SessionStatus.FAILED_TIMEOUT,  # transient congestion is real
        # Connection reset, yes; DNS failure, no. We cannot tell them apart
        # without parsing error strings even more finely than Phase 8 does,
        # and a bounded retry makes guessing wrong cheap.
        SessionStatus.FAILED_NAVIGATION,
    }
)


def is_retryable(result: SessionResult) -> bool:
    """Whether this failure is worth another attempt.

    FAILED_SETUP is deliberately absent. It means the browser could not create
    a context -- the browser itself is unhealthy -- and retrying a broken
    component turns a loud two-second failure into a confusing twenty-minute
    one. Retry the request, never the infrastructure.
    """
    if result.ok:
        return False

    if result.status is SessionStatus.FAILED_STATUS:
        # 5xx is the server having a bad moment; 4xx will say the same thing
        # next time. Retrying a 404 fifty times is just a slower 404.
        return result.http_status is not None and 500 <= result.http_status < 600

    return result.status in RETRYABLE_STATUSES


@dataclass(frozen=True, slots=True)
class FailurePolicy:
    """How hard to try, and when to stop trying entirely.

    Defaults are deliberately inert: one attempt, no abort. Retries change what
    an experiment measures -- "success rate" becomes "success rate given up to
    N attempts" -- so they are opt-in rather than something that quietly
    improves your numbers.
    """

    #: Total attempts per session, including the first. 1 disables retries.
    max_attempts: int = 1
    #: Base delay before a retry; doubles per attempt.
    backoff_seconds: float = 0.5
    #: Abort after this many consecutive failed sessions. 0 disables.
    abort_after_consecutive_failures: int = 0

    def should_retry(self, result: SessionResult, attempt: int) -> bool:
        return attempt < self.max_attempts and is_retryable(result)

    def delay_before(self, attempt: int) -> float:
        """Exponential backoff before `attempt` (which is 2 or more).

        No jitter, on purpose. Jitter stops N clients that failed together from
        retrying together -- essential at scale and against services you do not
        own. Here it would trade reproducibility for protection against a herd
        of ten requests hitting a server we control. Add it before pointing
        this at anything shared.
        """
        if attempt <= 1:
            return 0.0
        return self.backoff_seconds * 2.0 ** (attempt - 2)


class FailureTracker:
    """Counts consecutive failures and decides when to abort.

    Consecutive rather than a failure rate. A rate threshold needs a second
    knob for minimum sample size (3 of 3 is 100% and means nothing) and is
    laggy: over 50 sessions a 50% threshold cannot trip until roughly session
    25, even when everything has been broken since session 1. Consecutive
    trips at N, unambiguously, and clustering is exactly what a real outage
    looks like.

    Mutated from concurrent tasks without a lock: there is no `await` between
    reading and writing `_consecutive`, so no other coroutine can interleave.
    """

    __slots__ = ("_consecutive", "_limit", "_reason")

    def __init__(self, policy: FailurePolicy) -> None:
        self._limit = policy.abort_after_consecutive_failures
        self._consecutive = 0
        self._reason: str | None = None

    def record(self, result: SessionResult) -> None:
        if result.ok:
            self._consecutive = 0
            return

        self._consecutive += 1
        if self._limit and self._consecutive >= self._limit and self._reason is None:
            self._reason = (
                f"{self._consecutive} consecutive failures "
                f"(limit {self._limit}); last was {result.status.value}"
            )

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive

    @property
    def abort_reason(self) -> str | None:
        """Set once the limit is reached; None while the run should continue."""
        return self._reason

    def __repr__(self) -> str:
        return f"<FailureTracker consecutive={self._consecutive} limit={self._limit}>"
