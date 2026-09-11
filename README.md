# browser-automation-lab

An educational lab for studying browser automation, proxy mechanics,
concurrency, and observability.

> **Status:** under construction, built phase by phase.
> This README is a stub and will be written properly once the system exists.

## Scope

Automated browser sessions are run against a target we own or are authorised to
test, through proxies we control, in order to measure session lifecycle cost,
navigation latency, failure modes, and the effect of concurrency.

This project does not implement detection evasion, CAPTCHA solving, fingerprint
spoofing, or any other mechanism for disguising automated traffic.

## Quickstart (development)

```bash
uv sync                       # create .venv and install everything
uv run pytest                 # run the test suite
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy app tests         # type check
```
