"""Proxy parsing and selection.

Knows nothing about browsers. `Proxy` exposes the pieces a caller needs
(`server`, `username`, `password`); assembling them into whatever shape an
automation library wants is the browser layer's job.
"""

from app.proxy.parser import ParseError, ParseReport, Proxy, load_proxies, parse_line, parse_text
from app.proxy.pool import ProxyPool

__all__ = [
    "ParseError",
    "ParseReport",
    "Proxy",
    "ProxyPool",
    "load_proxies",
    "parse_line",
    "parse_text",
]
