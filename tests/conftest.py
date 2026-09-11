"""Shared fixtures.

The target server is the real lab bench (`lab/target_server.py`), not a
second copy defined here. One definition means the tests exercise the same
server a human runs, so the bench cannot silently drift from what the suite
verifies.
"""

import socket
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import Config, build_config
from lab.target_server import TargetServer


@pytest.fixture
def closed_port() -> int:
    """A port on loopback with nothing listening.

    Binding to port 0 lets the OS pick a free one; closing it immediately
    leaves an address that will refuse connections. Do not simply hardcode a
    low port: Chromium refuses to navigate to ~80 "unsafe" ports (1, 7, 22,
    25, 6000 ...) with ERR_UNSAFE_PORT, which is a different failure entirely.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def local_server() -> Iterator[TargetServer]:
    """The lab target, on a port the OS picks so parallel runs cannot collide."""
    with TargetServer() as server:
        yield server


def make_config(**sections: dict[str, Any]) -> Config:
    """Build a valid Config with targeted overrides.

    Goes through build_config rather than constructing dataclasses directly, so
    tests exercise the same validation the real program does.
    """
    data: dict[str, Any] = {"target": {"url": "http://127.0.0.1:8000/"}}
    for name, values in sections.items():
        data.setdefault(name, {}).update(values)
    return build_config(data)
