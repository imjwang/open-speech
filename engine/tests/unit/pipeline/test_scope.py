"""Where a rule applies (rules/scope.py)."""

from __future__ import annotations

import pytest

from open_speech.rules.scope import normalize_host, site_matches


@pytest.mark.parametrize(
    ("pattern", "host", "expected"),
    [
        # A site named by its domain covers what is under it: "on Wikipedia" reaches the English article.
        ("wikipedia.org", "en.wikipedia.org", True),
        ("wikipedia.org", "wikipedia.org", True),
        ("wikipedia.org", "www.wikipedia.org", True),
        ("arxiv.org", "export.arxiv.org", True),
        ("*.wikipedia.org", "en.wikipedia.org", True),
        ("*.wikipedia.org", "wikipedia.org", True),
        # A full host name stays precise.
        ("en.wikipedia.org", "en.wikipedia.org", True),
        ("en.wikipedia.org", "es.wikipedia.org", False),
        ("en.wikipedia.org", "wikipedia.org", False),
        # Not a suffix match on letters: another site that merely ends the same way is not covered.
        ("wikipedia.org", "notwikipedia.org", False),
        ("arxiv.org", "arxiv.org.evil.example", False),
        ("example.org", "example.com", False),
        # Addresses and ports are normalised first.
        ("https://Arxiv.org/abs/1706.03762", "arxiv.org:443", True),
        ("docs.*.example", "docs.team.example", True),
        ("docs.*.example", "team.example", False),
        ("wikipedia.org", None, False),
        ("", "wikipedia.org", False),
    ],
)
def test_site_matches(pattern: str, host: str | None, expected: bool) -> None:
    assert site_matches(pattern, host) is expected


def test_normalize_host() -> None:
    assert normalize_host("https://WWW.Example.org:8443/path?q=1") == "example.org"
    assert normalize_host("example.org.") == "example.org"
    assert normalize_host(None) == ""
