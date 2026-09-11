"""Turn proxy text into validated `Proxy` objects.

    proxies.txt -> parse_text() -> ParseReport(proxies=..., errors=...)

Supported entry formats::

    host:port                        the format proxies.txt actually uses
    scheme://host:port
    user:pass@host:port
    scheme://user:pass@host:port

`host:port:user:pass` is deliberately NOT supported -- see `_split_entry`.

Parsing strategy: build a URL-shaped string and hand it to `urllib.parse`,
rather than splitting on ":" by hand. The stdlib already knows about IPv6
brackets, percent-encoded credentials and port range limits; reimplementing
that with str.split is how you end up with a parser that works on the happy
path and quietly mangles everything else.
"""

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import unquote, urlsplit

from app.errors import ProxyParseError

SUPPORTED_SCHEMES: Final = ("http", "https", "socks5")

_MAX_HOSTNAME_LENGTH: Final = 253
_HOSTNAME_LABEL: Final = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_SCHEME_SEPARATOR: Final = "://"
_IPV6_LITERAL: Final = re.compile(r"\[[^\]]*\]")
_COMMENT_PREFIX: Final = "#"
_MAX_ECHOED_CHARS: Final = 60


# --------------------------------------------------------------------------- #
# The validated entry                                                          #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, repr=False)
class Proxy:
    """One validated proxy endpoint.

    repr=False and a hand-written `__repr__` are deliberate. Setting
    `field(repr=False)` on `password` alone would be fail-open: add a `token`
    field in six months, forget the flag, and it leaks into every log line,
    traceback and pytest assertion diff. Owning `__repr__` outright is
    fail-safe -- it prints an allowlist, so any field added later is excluded
    until someone consciously decides otherwise.
    """

    host: str
    port: int
    scheme: str = "http"
    username: str | None = None
    password: str | None = None

    @property
    def server(self) -> str:
        """Endpoint URL with NO credentials.

        Automation libraries take the server address and the credentials as
        separate arguments, so credentials must never be embedded here.
        """
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    @property
    def label(self) -> str:
        """Short, credential-free identifier for logs, metrics and JSONL."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def has_credentials(self) -> bool:
        return self.username is not None

    def __repr__(self) -> str:
        auth = " auth=yes" if self.has_credentials else ""
        return f"<Proxy {self.scheme}://{self.label}{auth}>"

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
class ParseError:
    """One unparseable entry. `raw` is redacted and truncated."""

    line_number: int
    raw: str
    reason: str

    def __str__(self) -> str:
        return f"line {self.line_number}: {self.reason} ({self.raw})"


@dataclass(frozen=True, slots=True)
class ParseReport:
    """The outcome of parsing a whole file.

    Both halves matter: `proxies` is what you use, `errors` is what you must be
    told about. Returning them together is how the parser reports problems
    without aborting the run over them.
    """

    proxies: tuple[Proxy, ...]
    errors: tuple[ParseError, ...]
    blank_lines: int = 0
    comment_lines: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        return (
            f"{len(self.proxies)} proxies, {len(self.errors)} malformed, "
            f"{self.blank_lines} blank, {self.comment_lines} comments"
        )


# --------------------------------------------------------------------------- #
# Redaction                                                                    #
# --------------------------------------------------------------------------- #


def redact(entry: str) -> str:
    """Make a raw entry safe to put in a log line or an exception message.

    Without this, a malformed `bob:hunter2@host:port` would have its password
    copied verbatim into an error message and from there into a log file --
    the exact leak Phase 16 forbids, arriving through the error path rather
    than the happy path.
    """
    text = entry.strip()
    if "@" in text:
        scheme, separator, remainder = text.partition(_SCHEME_SEPARATOR)
        prefix = f"{scheme}{separator}" if separator else ""
        _credentials, _, endpoint = (remainder if separator else text).rpartition("@")
        text = f"{prefix}***@{endpoint}"
    if len(text) > _MAX_ECHOED_CHARS:
        text = f"{text[:_MAX_ECHOED_CHARS]}..."
    return text


# --------------------------------------------------------------------------- #
# Validation                                                                   #
# --------------------------------------------------------------------------- #


def _validate_host(host: str) -> str:
    if not host:
        raise ProxyParseError("missing host")
    try:
        # Accepts IPv4 and IPv6 literals. urlsplit has already stripped the
        # brackets from [::1], so this sees a bare address either way.
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass  # not an IP literal, so it must be a hostname

    if len(host) > _MAX_HOSTNAME_LENGTH:
        raise ProxyParseError(f"hostname is longer than {_MAX_HOSTNAME_LENGTH} characters")
    labels = host.rstrip(".").split(".")
    if not all(_HOSTNAME_LABEL.match(label) for label in labels):
        raise ProxyParseError(f"invalid hostname {host!r}")
    return host


def _split_entry(entry: str) -> tuple[str, str]:
    """Return (scheme, authority), rejecting the ambiguous 4-part form.

    `host:port:user:pass` is rejected rather than supported because it cannot
    be disambiguated: given `a:b:c:d`, there is no way to tell a host with a
    colon-bearing password from a malformed entry, and the guess is silent
    either way. The `@` form says what it means, so we require it.
    """
    scheme, separator, remainder = entry.partition(_SCHEME_SEPARATOR)
    if separator:
        scheme = scheme.lower()
        if scheme not in SUPPORTED_SCHEMES:
            raise ProxyParseError(
                f"unsupported scheme {scheme!r}; expected one of {', '.join(SUPPORTED_SCHEMES)}"
            )
        return scheme, remainder

    # An IPv6 literal is made of colons, so [::1]:3128 must not be mistaken for
    # the ambiguous four-part form. Blank out anything bracketed before counting.
    outside_brackets = _IPV6_LITERAL.sub("", entry)
    if outside_brackets.count(":") > 1 and "@" not in entry:
        raise ProxyParseError("too many ':' separators; write credentials as user:pass@host:port")
    return "", entry


def parse_line(entry: str, *, default_scheme: str = "http") -> Proxy:
    """Parse one entry, or raise `ProxyParseError`.

    Raising (rather than returning an error object) keeps this function simple
    to compose and strict by default. `parse_text` is the layer that decides a
    single failure is survivable.
    """
    text = entry.strip()
    if not text:
        raise ProxyParseError("empty entry")
    if default_scheme not in SUPPORTED_SCHEMES:
        raise ProxyParseError(f"unsupported default_scheme {default_scheme!r}")

    scheme, authority = _split_entry(text)

    # "//" makes urlsplit treat the remainder as an authority rather than a
    # path, which is what gives us hostname/port/username/password for free.
    parts = urlsplit(f"//{authority}")

    if parts.path or parts.query or parts.fragment:
        raise ProxyParseError("a proxy entry must be host:port with no path or query")

    try:
        port = parts.port
    except ValueError as exc:
        raise ProxyParseError(str(exc).lower()) from exc
    if port is None:
        raise ProxyParseError("missing port")
    if port == 0:
        raise ProxyParseError("port must be between 1 and 65535, got 0")

    host = _validate_host(parts.hostname or "")

    username = parts.username
    password = parts.password
    if username is not None and not username:
        raise ProxyParseError("credentials are present but the username is empty")
    if username is None and password is not None:
        raise ProxyParseError("a password was given without a username")

    return Proxy(
        host=host,
        port=port,
        scheme=scheme or default_scheme,
        # urlsplit leaves credentials percent-encoded, so a password of
        # "p@ss" arrives as "p%40ss" and must be decoded before use.
        username=unquote(username) if username else None,
        password=unquote(password) if password else None,
    )


def parse_text(text: str, *, default_scheme: str = "http") -> ParseReport:
    """Parse a whole document, collecting failures rather than raising.

    Order is preserved and duplicates are kept: this function reports what the
    file says, it does not edit it. Deduplication, if ever wanted, is
    `dict.fromkeys(report.proxies)` -- `Proxy` is frozen, so it is hashable.
    """
    proxies: list[Proxy] = []
    errors: list[ParseError] = []
    blank = 0
    comments = 0

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            blank += 1
            continue
        if line.startswith(_COMMENT_PREFIX):
            comments += 1
            continue
        try:
            proxies.append(parse_line(line, default_scheme=default_scheme))
        except ProxyParseError as exc:
            errors.append(ParseError(line_number, redact(line), str(exc)))

    return ParseReport(tuple(proxies), tuple(errors), blank_lines=blank, comment_lines=comments)


def load_proxies(path: Path, *, default_scheme: str = "http") -> ParseReport:
    """Read a proxy file from disk and parse it.

    utf-8-sig rather than utf-8: a Windows editor may prepend a byte-order
    mark, which would otherwise attach itself to the first entry and produce a
    baffling "invalid hostname '\\ufeff1.2.3.4'" on line 1 only.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        raise ProxyParseError(f"proxy file not found: {path}") from exc
    except OSError as exc:
        raise ProxyParseError(f"could not read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ProxyParseError(f"{path} is not valid UTF-8: {exc}") from exc

    return parse_text(text, default_scheme=default_scheme)


def labels(proxies: Iterable[Proxy]) -> list[str]:
    """Credential-free identifiers, for logging a pool without leaking it."""
    return [proxy.label for proxy in proxies]
