"""Exception hierarchy for the lab.

Every error we raise *deliberately* inherits from `LabError`. That gives the CLI
a single clean boundary::

    try:
        main()
    except LabError as exc:      # expected: print the message, exit non-zero
        ...
    # anything else propagates as a traceback, because it is a bug in our code

Without a common base, the CLI would have to choose between `except Exception`
(which hides real bugs behind a tidy message) and listing every error type by
hand (which goes stale the moment someone adds one).

Types are added in the phase that first needs them rather than declared up front.
"""

from collections.abc import Sequence


class LabError(Exception):
    """Base class for all deliberate, expected failures in this project."""


class ConfigurationError(LabError):
    """The configuration is invalid; nothing should run.

    Carries *every* problem found rather than only the first, so one run of the
    program tells the user everything they need to fix.
    """

    def __init__(self, problems: Sequence[str], *, source: str | None = None) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        self.source = source
        super().__init__(self._render())

    def _render(self) -> str:
        where = f" in {self.source}" if self.source else ""
        count = len(self.problems)
        noun = "problem" if count == 1 else "problems"
        lines = [f"{count} configuration {noun}{where}:"]
        lines.extend(f"  - {problem}" for problem in self.problems)
        return "\n".join(lines)


class ProxyError(LabError):
    """Base class for proxy problems."""


class ProxyParseError(ProxyError):
    """A single proxy entry could not be parsed.

    Raised by `parse_line` for one entry. `parse_text` catches it and records
    the failure instead of propagating, because one bad line out of thousands
    should not abort a run.
    """


class ProxyPoolEmptyError(ProxyError):
    """Proxies are enabled but there are none usable.

    Fatal: unlike a single bad line, this invalidates the whole experiment.
    """


class BrowserError(LabError):
    """Base class for browser problems."""


class BrowserLaunchError(BrowserError):
    """The browser process could not be started.

    Fatal for the experiment, not just one session: if Chromium will not start,
    retrying 49 more times will not help.
    """


class ExperimentAbortedError(LabError):
    """The failure policy stopped the experiment early.

    Not a crash: the results collected before the abort are still valid and are
    still returned. Raised inside a task so the TaskGroup cancels its siblings,
    then caught by the runner, which reports it on the metrics.
    """
