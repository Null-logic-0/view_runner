"""Unit tests for structured logging and credential scrubbing."""

import io
import json
import logging

import pytest

from app.telemetry.logger import configure_logging, scrub


def capture(fmt: str = "text", level: str = "INFO") -> tuple[io.StringIO, logging.Logger]:
    buffer = io.StringIO()
    configure_logging(level=level, fmt=fmt, stream=buffer)
    return buffer, logging.getLogger("app.test")


# ---------------------------------------------------------------- scrubbing --


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://bob:hunter2@proxy:1080", "http://***@proxy:1080"),
        ("socks5://u:p@1.2.3.4:9050", "socks5://***@1.2.3.4:9050"),
        ("bob:hunter2@proxy:1080", "***@proxy:1080"),
        ("password=s3cr3t", "password=***"),
        ("token: abc123", "token: ***"),
        ('api_key="xyz"', 'api_key="***"'),
        ("PASSWORD = letmein", "PASSWORD = ***"),
    ],
)
def test_credentials_are_scrubbed(text: str, expected: str) -> None:
    assert scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "http://1.2.3.4:8080",
        "connected to proxy 1.2.3.4:8080 in 12 ms",
        "ratio 3:1 at 10:30",
        "session 3 completed",
    ],
)
def test_ordinary_text_is_left_alone(text: str) -> None:
    """A scrubber that mangles normal messages gets turned off, and then protects nothing."""
    assert scrub(text) == text


# ------------------------------------------------------------------ formats --


def test_text_format_appends_structured_fields() -> None:
    buffer, log = capture("text")
    log.info("session finished", extra={"session_id": 3, "status": "completed"})

    line = buffer.getvalue()
    assert "session finished" in line
    assert "session_id=3" in line
    assert "status=completed" in line


def test_json_format_emits_one_object_per_line() -> None:
    buffer, log = capture("json")
    log.info("session finished", extra={"session_id": 3, "total_ms": 30012.4})

    payload = json.loads(buffer.getvalue().strip())
    assert payload["message"] == "session finished"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["session_id"] == 3
    assert payload["total_ms"] == 30012.4
    assert payload["ts"].endswith("+00:00")


def test_json_lines_are_independently_parseable() -> None:
    buffer, log = capture("json")
    for session_id in range(3):
        log.info("tick", extra={"session_id": session_id})

    lines = buffer.getvalue().strip().splitlines()
    assert [json.loads(line)["session_id"] for line in lines] == [0, 1, 2]


# ----------------------------------------------------------------- secrecy --


def test_secrets_are_scrubbed_in_text_output() -> None:
    buffer, log = capture("text")
    log.warning("connecting via http://bob:hunter2@proxy.internal:1080")
    assert "hunter2" not in buffer.getvalue()


def test_secrets_are_scrubbed_in_json_output() -> None:
    buffer, log = capture("json")
    log.warning("connecting via http://bob:hunter2@proxy.internal:1080")
    assert "hunter2" not in buffer.getvalue()
    assert json.loads(buffer.getvalue().strip())  # still valid JSON


def test_secrets_are_scrubbed_from_exception_tracebacks() -> None:
    """The case a logging.Filter would miss: a filter never sees the traceback."""
    buffer, log = capture("text")
    try:
        raise ValueError("bad proxy http://bob:hunter2@proxy:1080")
    except ValueError:
        log.exception("session failed")

    output = buffer.getvalue()
    assert "hunter2" not in output
    assert "ValueError" in output


def test_secrets_in_structured_fields_are_scrubbed() -> None:
    buffer, log = capture("json")
    log.info("connecting", extra={"url": "http://bob:hunter2@proxy:1080"})
    assert "hunter2" not in buffer.getvalue()


# ------------------------------------------------------------ configuration --


def test_configure_logging_is_idempotent() -> None:
    """Calling it twice must not double every line."""
    buffer = io.StringIO()
    configure_logging(fmt="text", stream=buffer)
    configure_logging(fmt="text", stream=buffer)
    logging.getLogger("app.test").info("once")

    assert len(buffer.getvalue().strip().splitlines()) == 1


def test_level_filtering_works() -> None:
    buffer, log = capture("text", level="WARNING")
    log.info("quiet")
    log.warning("loud")

    assert "quiet" not in buffer.getvalue()
    assert "loud" in buffer.getvalue()


def test_an_unknown_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown log format"):
        configure_logging(fmt="yaml")
