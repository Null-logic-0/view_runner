"""Unit tests for proxy parsing.

No network. The only filesystem access is tmp_path plus the real proxies.txt,
which is used deliberately: a parser validated only against fixtures I invented
is a parser validated against my own assumptions.
"""

from pathlib import Path

import pytest

from app.errors import ProxyParseError
from app.proxy.parser import Proxy, load_proxies, parse_line, parse_text, redact

REAL_PROXY_FILE = Path("proxies.txt")


# appy paths


def test_bare_host_port_is_the_format_the_real_file_uses() -> None:
    proxy = parse_line("103.229.247.202:37927")
    assert proxy == Proxy(host="103.229.247.202", port=37927, scheme="http")


def test_default_scheme_is_applied_when_the_entry_omits_one() -> None:
    assert parse_line("1.2.3.4:8080", default_scheme="socks5").scheme == "socks5"


def test_explicit_scheme_wins_over_the_default() -> None:
    assert parse_line("https://1.2.3.4:8080", default_scheme="socks5").scheme == "https"


@pytest.mark.parametrize("scheme", ["http", "https", "socks5"])
def test_every_supported_scheme_parses(scheme: str) -> None:
    assert parse_line(f"{scheme}://1.2.3.4:8080").scheme == scheme


def test_scheme_is_case_insensitive() -> None:
    assert parse_line("HTTP://1.2.3.4:8080").scheme == "http"


def test_credentials_are_extracted() -> None:
    proxy = parse_line("bob:hunter2@1.2.3.4:8080")
    assert proxy.username == "bob"
    assert proxy.password == "hunter2"
    assert proxy.has_credentials is True


def test_percent_encoded_credentials_are_decoded() -> None:
    """urlsplit leaves them encoded, so a password of 'p@ss' arrives as 'p%40ss'."""
    proxy = parse_line("bob:p%40ss@1.2.3.4:8080")
    assert proxy.password == "p@ss"


def test_hostnames_are_accepted_not_just_ip_literals() -> None:
    assert parse_line("proxy.internal.example:3128").host == "proxy.internal.example"


def test_ipv6_literals_are_accepted() -> None:
    proxy = parse_line("[::1]:3128")
    assert proxy.host == "::1"
    assert proxy.label == "[::1]:3128"  # brackets restored for the URL form


def test_surrounding_whitespace_is_ignored() -> None:
    assert parse_line("  1.2.3.4:8080\t") == parse_line("1.2.3.4:8080")


# rejection


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("1.2.3.4", "missing port"),
        ("1.2.3.4:0", "port must be between 1 and 65535"),
        ("1.2.3.4:65536", "port out of range"),
        ("1.2.3.4:abc", "could not be cast to integer"),
        (":8080", "missing host"),
        ("gopher://1.2.3.4:8080", "unsupported scheme"),
        ("1.2.3.4:8080:bob:hunter2", "too many ':' separators"),
        (":hunter2@1.2.3.4:8080", "username is empty"),
        ("1.2.3.4:8080/path", "no path or query"),
        ("not a host:8080", "invalid hostname"),
        ("", "empty entry"),
    ],
)
def test_malformed_entries_are_rejected_with_a_useful_reason(entry: str, expected: str) -> None:
    with pytest.raises(ProxyParseError, match=expected):
        parse_line(entry)


def test_unsupported_default_scheme_is_rejected() -> None:
    with pytest.raises(ProxyParseError, match="unsupported default_scheme"):
        parse_line("1.2.3.4:8080", default_scheme="gopher")


# secrecy


SECRET = "hunter2"
WITH_CREDENTIALS = f"bob:{SECRET}@proxy.internal:1080"


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda p: f"{p}",
        lambda p: f"{p!r}",
        lambda p: p.server,
        lambda p: p.label,
        lambda p: repr([p]),
        lambda p: repr({"proxy": p}),
    ],
    ids=["repr", "str", "fstring", "fstring-repr", "server", "label", "in-list", "in-dict"],
)
def test_password_never_appears_in_any_rendering(render) -> None:  # type: ignore[no-untyped-def]
    assert SECRET not in render(parse_line(WITH_CREDENTIALS))


def test_username_is_also_withheld_from_rendering() -> None:
    """Fail-safe: __repr__ prints an allowlist, so half a credential leaks too."""
    assert "bob" not in repr(parse_line(WITH_CREDENTIALS))


def test_password_is_still_available_when_explicitly_requested() -> None:
    """Redaction must hide secrets from accidental output, not from callers."""
    assert parse_line(WITH_CREDENTIALS).password == SECRET


def test_credentials_are_redacted_in_parse_errors() -> None:
    """The error path is a leak vector too: it echoes the offending line."""
    report = parse_text(f"bob:{SECRET}@1.2.3.4:notaport")
    assert SECRET not in str(report.errors[0])
    assert "***@1.2.3.4:notaport" in report.errors[0].raw


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("1.2.3.4:8080", "1.2.3.4:8080"),
        ("bob:pw@1.2.3.4:8080", "***@1.2.3.4:8080"),
        ("http://bob:pw@1.2.3.4:8080", "http://***@1.2.3.4:8080"),
    ],
)
def test_redact(entry: str, expected: str) -> None:
    assert redact(entry) == expected


def test_long_entries_are_truncated_before_being_echoed() -> None:
    assert redact("x" * 500).endswith("...")
    assert len(redact("x" * 500)) < 100


# whole file


def test_blank_lines_and_comments_are_skipped_not_reported_as_errors() -> None:
    report = parse_text("\n# a comment\n1.2.3.4:8080\n\n   \n# another\n5.6.7.8:3128\n")
    assert len(report.proxies) == 2
    assert report.blank_lines == 3
    assert report.comment_lines == 2
    assert report.ok


def test_crlf_line_endings_parse_correctly() -> None:
    """The real file is CRLF; a stray '\\r' would break int(port)."""
    report = parse_text("1.2.3.4:8080\r\n5.6.7.8:3128\r\n")
    assert len(report.proxies) == 2
    assert report.proxies[1].port == 3128


def test_one_bad_line_does_not_discard_the_good_ones() -> None:
    """The contrast with config: bad data that invalidates only itself."""
    report = parse_text("1.2.3.4:8080\nGARBAGE\n5.6.7.8:3128\n")
    assert len(report.proxies) == 2
    assert len(report.errors) == 1
    assert report.ok is False


def test_errors_carry_the_original_line_number() -> None:
    report = parse_text("# header\n1.2.3.4:8080\nGARBAGE\n")
    assert report.errors[0].line_number == 3


def test_order_and_duplicates_are_preserved() -> None:
    """The parser reports what the file says; it does not edit it."""
    report = parse_text("1.1.1.1:80\n2.2.2.2:80\n1.1.1.1:80\n")
    assert [p.label for p in report.proxies] == ["1.1.1.1:80", "2.2.2.2:80", "1.1.1.1:80"]


def test_proxies_are_hashable_so_deduplication_is_a_one_liner() -> None:
    report = parse_text("1.1.1.1:80\n2.2.2.2:80\n1.1.1.1:80\n")
    assert len(dict.fromkeys(report.proxies)) == 2


def test_summary_is_human_readable() -> None:
    assert parse_text("1.2.3.4:8080\n# c\n\n").summary() == (
        "1 proxies, 0 malformed, 1 blank, 1 comments"
    )


# files --


def test_load_proxies_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "p.txt"
    path.write_text("1.2.3.4:8080\n", encoding="utf-8")
    assert len(load_proxies(path).proxies) == 1


def test_load_proxies_strips_a_byte_order_mark(tmp_path: Path) -> None:
    """A BOM would otherwise glue itself to the first entry's hostname."""
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf1.2.3.4:8080\r\n5.6.7.8:3128\r\n")
    report = load_proxies(path)
    assert report.ok
    assert report.proxies[0].host == "1.2.3.4"


def test_load_proxies_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProxyParseError, match="proxy file not found"):
        load_proxies(tmp_path / "absent.txt")


def test_load_proxies_reports_undecodable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "binary.txt"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(ProxyParseError, match="not valid UTF-8"):
        load_proxies(path)


# real data


@pytest.mark.skipif(not REAL_PROXY_FILE.exists(), reason="proxies.txt not present")
def test_the_real_proxy_file_parses_without_errors() -> None:
    report = load_proxies(REAL_PROXY_FILE)
    assert report.ok, f"unexpected malformed entries: {report.errors[:5]}"
    assert len(report.proxies) == 6854
    assert report.blank_lines == 0
    assert report.comment_lines == 0


@pytest.mark.skipif(not REAL_PROXY_FILE.exists(), reason="proxies.txt not present")
def test_the_real_proxy_file_contains_no_credentials() -> None:
    """If this ever fails, proxies.txt has become a secret and must leave git."""
    report = load_proxies(REAL_PROXY_FILE)
    assert not any(p.has_credentials for p in report.proxies)
