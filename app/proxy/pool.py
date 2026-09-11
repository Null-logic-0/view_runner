"""Decide which proxy each session uses.

Rotation only. The pool does not parse, validate, connect, or know whether a
proxy works -- it hands out endpoints in a predictable order.

Why a class and not `itertools.cycle`?
    `cycle(proxies)` is genuinely the right tool for pure round-robin, and if
    rotation were all we needed, a class here would be over-engineering. It
    earns its place for three concrete reasons:

      1. Introspection. Tests and telemetry want `len(pool)` and `position`;
         `cycle` exposes neither and cannot be rewound.
      2. An explicit empty-pool failure. `cycle(())` yields nothing and a
         `for` loop over it simply does nothing -- a silent no-op experiment.
         We want a loud, typed error instead.
      3. It is the natural home for per-proxy failure tracking when the
         failure policy lands in a later phase.
"""

from collections.abc import Sequence

from app.errors import ProxyPoolEmptyError
from app.proxy.parser import Proxy


class ProxyPool:
    """Round-robin over a fixed list of proxies.

    Deterministic by construction: given the same list, session N always gets
    the same proxy. That is what makes an experiment reproducible and lets a
    test assert an exact sequence -- the opposite of the old project's
    `random.choice`, which sampled with replacement and could not be pinned
    down or repeated.
    """

    __slots__ = ("_index", "_proxies")

    def __init__(self, proxies: Sequence[Proxy]) -> None:
        if not proxies:
            raise ProxyPoolEmptyError("no usable proxies; disable proxies or fix the proxy file")
        self._proxies: tuple[Proxy, ...] = tuple(proxies)
        self._index = 0

    def next_proxy(self) -> Proxy:
        """Return the next proxy, wrapping around at the end.

        No lock, deliberately. asyncio runs coroutines on a single thread and
        switches only at an `await`; there is no `await` between reading and
        writing `self._index`, so the read-modify-write cannot be interleaved.
        Under real threads this would be a race and would need a lock.
        """
        proxy = self._proxies[self._index]
        self._index = (self._index + 1) % len(self._proxies)
        return proxy

    @property
    def position(self) -> int:
        """Index that the next call to `next_proxy` will return."""
        return self._index

    @property
    def proxies(self) -> tuple[Proxy, ...]:
        return self._proxies

    def reset(self) -> None:
        self._index = 0

    def __len__(self) -> int:
        return len(self._proxies)

    def __repr__(self) -> str:
        return f"<ProxyPool size={len(self._proxies)} position={self._index}>"
