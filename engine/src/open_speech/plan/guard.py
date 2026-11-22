"""A mechanical bound on what an AI span op may remove (05 § Tier 2).

Each built-in preset names a kind of span: a citation, a web address, a parenthetical, a running head. An op
credited to a preset, or to no rule at all, may take that kind of span out of the speech plus at most
`MAX_STRAY_WORDS` other words — the connective that only introduced it ("at", "see also", "available online
at"). An op that removes more is refused: the text is read as written and the refusal is logged, so a model
slip cannot silently drop what the author said next to a URL or a citation.

Ops credited to a rule the user wrote (tier `annotate`, e.g. "never read legal disclaimers") or to SPEECH.md
are not bounded here. There the user asked for judgment over whole passages, and Script view shows everything
that was removed.

The recognisers are deliberately looser than the Classic mark detectors in `ingest/marks.py`: AI Enhanced
exists for the forms those detectors miss ("(for a review, see Smith and Jones, 2020)", "example.org/docs"),
and a recogniser that is too loose only lets through what the prompt already forbids, while one that is too
strict makes the engine read a citation aloud.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from open_speech.document.model import BlockType, Document
from open_speech.ingest.marks import balanced_spans, find_citation_items, find_urls
from open_speech.plan.script import SPEECH_MD_RULE_ID, SpanOp
from open_speech.rules.model import Rule

MAX_STRAY_WORDS = 3
MAX_STRAY_LETTERS = 24  # the same bound for scripts that don't put spaces between words

Span = tuple[int, int]

_WORD = re.compile(r"[^\W\d_]{2,}")
# "example.org", "example.org/docs/a", "name@example.org": addresses the Classic URL detector (http/www/DOI) skips.
_ADDRESS = re.compile(
    r"(?<![\w@.])(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s<>\"]*)?|[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE
)
_FURNITURE = frozenset({BlockType.header, BlockType.footer, BlockType.page_number})


def _address_spans(text: str) -> list[Span]:
    return find_urls(text) + [(m.start(), m.end()) for m in _ADDRESS.finditer(text)]


def _citation_spans(text: str) -> list[Span]:
    """Bracketed groups that carry a year or number, and author-year items outside brackets."""
    groups = [s for pair in ("()", "[]") for s in balanced_spans(text, pair[0], pair[1])]
    return [(a, b) for a, b in groups if any(ch.isdigit() for ch in text[a:b])] + find_citation_items(text)


def _parenthetical_spans(text: str) -> list[Span]:
    return balanced_spans(text, "(", ")") + balanced_spans(text, "（", "）")


# Presets whose kind of span has a shape. "page-numbers" ("Page 4 of 18") and "footnotes" (a note marker)
# need none: such spans hold at most a word or two, which the stray-word allowance already covers.
# "headers" is recognised by repetition instead (see `_is_furniture`), and only for ops credited to it.
_CORES = {"urls": _address_spans, "citations": _citation_spans, "parentheses": _parenthetical_spans}


def _normalize(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join(text.split())).casefold()


class OpGuard:
    """`check(op, removed)` returns why an op must not be applied, or None when it may."""

    def __init__(self, rules: Sequence[Rule], *, speech_md: str = "", document: Document | None = None) -> None:
        self._by_id = {r.id: r for r in rules}
        self._only = rules[0] if len(rules) == 1 else None  # an unattributed op is credited to a lone rule
        self._presets = {r.name for r in rules if r.preset}
        self._speech_md = bool(speech_md.strip())
        self._document = document
        self._body: str | None = None
        self._furniture: set[str] | None = None

    def check(self, op: SpanOp, removed: str) -> str | None:
        rule = self._by_id.get(op.rule_id or "")
        if rule is None and op.rule_id == SPEECH_MD_RULE_ID:
            if self._speech_md:
                return None
        elif rule is None:
            rule = self._only
        if rule is not None and not rule.preset:
            return None
        stray = self._stray_words(removed, op.text if op.op == "replace" else "")
        if len(stray) <= MAX_STRAY_WORDS and sum(len(w) for w in stray) <= MAX_STRAY_LETTERS:
            return None
        if rule is not None and rule.name == "headers" and self._is_furniture(removed):
            return None
        who = f"the {rule.name} preset" if rule is not None else "any active rule"
        return f"removing {removed!r} takes more than {who} covers"

    def _stray_words(self, removed: str, kept_text: str) -> list[str]:
        """Words of `removed` that would be lost and aren't part of a span an active preset names."""
        cores = [s for name, find in _CORES.items() if name in self._presets for s in find(removed)]
        kept = Counter(w.casefold() for w in _WORD.findall(kept_text))
        stray: list[str] = []
        for m in _WORD.finditer(removed):
            if any(a <= m.start() and m.end() <= b for a, b in cores):
                continue
            word = m.group().casefold()
            if kept[word] > 0:  # a replacement that still says the word hasn't removed it
                kept[word] -= 1
                continue
            stray.append(word)
        return stray

    def _is_furniture(self, removed: str) -> bool:
        """A running head is page furniture: it matches a header/footer block or repeats in the document."""
        key = _normalize(removed)
        if len(key) < 4 or self._document is None:
            return False
        if self._body is None or self._furniture is None:
            blocks = sorted(self._document.blocks, key=lambda b: b.order)
            self._furniture = {_normalize(b.text) for b in blocks if b.type in _FURNITURE}
            self._body = " ".join(_normalize(b.text) for b in blocks)
        return key in self._furniture or self._body.count(key) >= 2
