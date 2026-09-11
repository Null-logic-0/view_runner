"""Structured logging with credential scrubbing.

Two output shapes from the same call sites:

    text   a human watching a 25-minute run
    json   one object per line, for `jq` and for later analysis

"Structured" means a record carries fields, not just a sentence::

    logger.info("session finished", extra={"session_id": 3, "total_ms": 30012.4})

In text mode that reads as prose; in JSON mode the fields are queryable. With
print(f"Session {i} done") the fields exist only inside English.

Scrubbing lives in the FORMATTER rather than in a logging.Filter. A filter sees
record.msg and record.args before formatting, so it never sees an exception
traceback -- and a traceback is exactly where a proxy URL is most likely to
surface. The formatter sees the final string, tracebacks included.

This is defence in depth, not the defence. The primary protection is that
`Proxy.__repr__` prints an allowlist, so a password cannot reach a log line
through an object at all. The scrubber catches the other path: a hand-written
message that interpolated a URL someone assembled by hand.
"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Final, TextIO

REDACTED: Final = "***"

# user:secret@host -- credentials embedded in a URL authority.
_URL_CREDENTIALS: Final = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)?[^\s:/@]+:[^\s:/@]+@")

# password=..., token: ..., api_key = "..." and friends.
_KEYWORD_SECRET: Final = re.compile(
    r"(?P<key>\b(?:password|passwd|secret|token|api[_-]?key)\b)"
    r"(?P<sep>\s*[=:]\s*)"
    r"(?P<quote>[\"']?)(?P<value>[^\s\"',}]+)(?P=quote)",
    re.IGNORECASE,
)

LOG_FORMATS: Final = ("text", "json")
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Everything the logging module puts on a record itself. Anything else came
# from `extra=` and is therefore one of our own structured fields.
_STANDARD_FIELDS: Final = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def scrub(text: str) -> str:
    """Replace anything that looks like a credential."""
    text = _URL_CREDENTIALS.sub(lambda m: f"{m.group('scheme') or ''}{REDACTED}@", text)
    return _KEYWORD_SECRET.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{m.group('quote')}{REDACTED}{m.group('quote')}",
        text,
    )


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in record.__dict__.items() if key not in _STANDARD_FIELDS}


class TextFormatter(logging.Formatter):
    """Human-readable, with structured fields appended as key=value."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = _extra_fields(record)
        if fields:
            rendered = " ".join(f"{key}={value}" for key, value in sorted(fields.items()))
            base = f"{base}  {rendered}"
        return scrub(base)


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Scrub the serialised line rather than each field: one pass, and it
        # covers the exception text too.
        return scrub(json.dumps(payload, default=str))


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "text",
    stream: TextIO | None = None,
) -> logging.Logger:
    """Attach one handler to the `app` logger.

    Deliberately not the root logger. Configuring root would hijack logging for
    the whole process, including any library the user imports alongside us.
    The cost is that third-party logs (Playwright's own, for instance) are not
    routed here -- which for this project is a feature, not a gap.

    Idempotent: existing handlers are cleared, so calling it twice in a test
    does not produce doubled output.
    """
    if fmt not in LOG_FORMATS:
        raise ValueError(f"unknown log format {fmt!r}; expected one of {', '.join(LOG_FORMATS)}")

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            TextFormatter("%(asctime)s %(levelname)-7s %(name)-14s %(message)s", "%H:%M:%S")
        )

    logger = logging.getLogger("app")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False  # do not also emit through root
    return logger
