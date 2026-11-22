"""Rule scopes and precedence (06 § Rule file format).

Scope keys are ANDed; an empty scope is global. Precedence: `docs` > `sites` > `types` (and
`languages`) > global; within a level the most recently edited rule wins.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from open_speech import i18n
from open_speech.api.schemas import RuleScope
from open_speech.document.model import Document

if TYPE_CHECKING:
    from open_speech.rules.model import Rule

LEVEL_DOCS = 3
LEVEL_SITES = 2
LEVEL_TYPES = 1
LEVEL_GLOBAL = 0

TYPE_NOUNS = {
    "pdf": "PDFs",
    "web": "web pages",
    "docx": "Word documents",
    "text": "text documents",
    "image": "images",
    "audio": "audio files",
}


def normalize_host(host: str | None) -> str:
    """Lowercase hostname without port or a leading `www.`; accepts a URL too."""
    if not host:
        return ""
    h = host.strip().lower()
    if "://" in h:
        h = urlsplit(h).hostname or ""
    h = h.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return h[4:] if h.startswith("www.") else h


def site_matches(pattern: str, host: str | None) -> bool:
    """A site covers itself and everything under it: `wikipedia.org` matches wikipedia.org, www.wikipedia.org
    and en.wikipedia.org (a rule "on Wikipedia" must reach the article the user is looking at), while
    `en.wikipedia.org` leaves es.wikipedia.org alone. `*.acm.org` says the same as `acm.org`; other
    wildcards (`docs.*.example`) are matched as written."""
    h = normalize_host(host)
    p = normalize_host(pattern) if "*" not in pattern else pattern.strip().lower()
    if not h or not p:
        return False
    if p.startswith("*."):
        p = p[2:]
    if "*" in p:
        return fnmatch.fnmatchcase(h, p)
    return h == p or h.endswith("." + p)


def _lang_matches(pattern: str, lang: str | None) -> bool:
    if not lang:
        return False
    p, lg = pattern.lower(), lang.lower()
    return lg == p or lg.startswith(p + "-")


def scope_matches(scope: RuleScope, doc: Document) -> bool:
    if scope.docs and doc.id not in scope.docs:
        return False
    if scope.sites and not any(site_matches(p, doc.site) for p in scope.sites):
        return False
    if scope.types and str(doc.kind) not in {t.lower() for t in scope.types}:
        return False
    return not (scope.languages and not any(_lang_matches(p, doc.language) for p in scope.languages))


def scope_level(scope: RuleScope) -> int:
    if scope.docs:
        return LEVEL_DOCS
    if scope.sites:
        return LEVEL_SITES
    if scope.types or scope.languages:
        return LEVEL_TYPES
    return LEVEL_GLOBAL


def precedence_key(rule: Rule) -> tuple[int, float, str]:
    """Sort key: higher level first, then most recently edited, then name (for determinism)."""
    return (-scope_level(rule.scope), -rule.updated_at, rule.name)


def by_precedence(rules: Iterable[Rule]) -> list[Rule]:
    return sorted(rules, key=precedence_key)


def normalized_scope(scope: RuleScope) -> RuleScope:
    return RuleScope(
        sites=sorted({normalize_host(s) if "*" not in s else s.strip().lower() for s in scope.sites}),
        types=sorted({t.strip().lower() for t in scope.types}),
        docs=sorted(set(scope.docs)),
        languages=sorted({lg.strip().lower() for lg in scope.languages}),
    )


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + i18n.t("list.and") + items[-1]


def describe_scope(scope: RuleScope) -> str:
    """Spoken scope phrase: "for arxiv.org", "for this document", "for PDFs", "everywhere"."""
    if scope.docs:
        return (
            i18n.t("scope.for_document") if len(scope.docs) == 1 else i18n.t("scope.for_documents", n=len(scope.docs))
        )
    if scope.sites:
        return i18n.t("scope.for", what=_join(list(scope.sites)))
    if scope.types:
        types = [i18n.t(f"type.{x}") if i18n.has(f"type.{x}") else TYPE_NOUNS.get(x, x) for x in scope.types]
        return i18n.t("scope.for", what=_join(types))
    if scope.languages:
        return i18n.t("scope.for_languages", what=_join(list(scope.languages)))
    return i18n.t("scope.everywhere")


def level_reason(level: int) -> str:
    return {
        LEVEL_DOCS: "it is scoped to the document",
        LEVEL_SITES: "it is scoped to the site",
        LEVEL_TYPES: "it is scoped to the document type",
        LEVEL_GLOBAL: "it applies everywhere",
    }[level]
