"""Unit tests for proxy selection.

The whole point of this module is determinism, so these tests assert exact
sequences. If a test here can only say "it returned *a* proxy", the pool is not
reproducible and the experiments built on it are not either.
"""

import pytest

from app.errors import ProxyPoolEmptyError
from app.proxy.parser import Proxy
from app.proxy.pool import ProxyPool


def make_proxies(count: int) -> list[Proxy]:
    return [Proxy(host=f"10.0.0.{n}", port=8000 + n) for n in range(1, count + 1)]


def test_rotation_is_round_robin_and_wraps() -> None:
    pool = ProxyPool(make_proxies(3))
    drawn = [pool.next_proxy().label for _ in range(7)]
    assert drawn == [
        "10.0.0.1:8001",
        "10.0.0.2:8002",
        "10.0.0.3:8003",
        "10.0.0.1:8001",
        "10.0.0.2:8002",
        "10.0.0.3:8003",
        "10.0.0.1:8001",
    ]


def test_two_pools_over_the_same_list_agree_exactly() -> None:
    """Determinism: session N gets the same proxy on every run of the experiment."""
    proxies = make_proxies(4)
    first = [ProxyPool(proxies).next_proxy() for _ in range(1)]
    a, b = ProxyPool(proxies), ProxyPool(proxies)
    assert [a.next_proxy() for _ in range(9)] == [b.next_proxy() for _ in range(9)]
    assert first[0] == proxies[0]


def test_a_single_proxy_pool_returns_it_every_time() -> None:
    pool = ProxyPool(make_proxies(1))
    assert {pool.next_proxy().label for _ in range(5)} == {"10.0.0.1:8001"}


def test_an_empty_pool_is_a_loud_error_not_a_silent_no_op() -> None:
    """itertools.cycle(()) would yield nothing and the experiment would do nothing."""
    with pytest.raises(ProxyPoolEmptyError, match="no usable proxies"):
        ProxyPool([])


def test_position_reports_what_comes_next() -> None:
    pool = ProxyPool(make_proxies(3))
    assert pool.position == 0
    pool.next_proxy()
    assert pool.position == 1
    pool.next_proxy()
    pool.next_proxy()
    assert pool.position == 0  # wrapped


def test_reset_rewinds_to_the_start() -> None:
    pool = ProxyPool(make_proxies(3))
    pool.next_proxy()
    pool.next_proxy()
    pool.reset()
    assert pool.next_proxy().label == "10.0.0.1:8001"


def test_len_reports_the_pool_size() -> None:
    assert len(ProxyPool(make_proxies(6854))) == 6854


def test_pool_snapshots_its_input() -> None:
    """Mutating the caller's list afterwards must not change rotation."""
    proxies = make_proxies(2)
    pool = ProxyPool(proxies)
    proxies.append(Proxy(host="10.0.0.99", port=9999))
    assert len(pool) == 2


def test_repr_is_informative_and_leaks_nothing() -> None:
    pool = ProxyPool([Proxy(host="1.2.3.4", port=8080, username="bob", password="hunter2")])
    assert repr(pool) == "<ProxyPool size=1 position=0>"
    assert "hunter2" not in repr(pool)
