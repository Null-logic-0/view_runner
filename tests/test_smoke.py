"""Smoke tests for the project harness itself.

These assert almost nothing about behaviour. Their job is to prove that the
package is importable, that pytest collects tests, and that async tests run --
so that when a real test fails later, we know it is the code and not the setup.
"""

import asyncio

import app


def test_package_is_importable() -> None:
    assert app.__version__ == "0.1.0"


async def test_async_tests_actually_run() -> None:
    """Verifies pytest-asyncio's `asyncio_mode = "auto"` is in effect.

    With no decorator on this function, a misconfigured harness would either
    skip it or warn that a coroutine was never awaited -- and silently pass.
    """
    before = asyncio.get_running_loop()
    await asyncio.sleep(0)
    assert asyncio.get_running_loop() is before
