"""Deterministic mark detection (05 § 2): candidates that presets and rules decide about.

- citation_ref: numeric `[12]`, `[3–5]`, `[1, 2]` (adjacent groups merged: `[1], [2]`), alpha `[SKL20]`,
  author-year `(Smith et al., 2020)`, `(Smith, 2020; Lee 2019)`, `(e.g., Smith and Jones 2018, p. 4)`, and
  the year of a narrative citation `Smith et al. (2020)`.
- url: http(s)/www URLs (trailing punctuation and unbalanced parens trimmed), DOIs.
- parenthetical / bracket / brace: balanced outermost spans that aren't citations.
- formula: `$…$`, `\\(…\\)` and simple operator expressions (`x = 3`, `p < 0.05`, `a² + b²`).
- footnote_ref: supplied structurally by extractors (superscripts, DOCX references, `[^1]`), plus Unicode
  superscript digits matching a known footnote marker.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable

from open_speech.document.model import BlockType, MarkKind

Span = tuple[MarkKind, int, int]

_NUM_CITE = re.compile(
    r"\[\s*\d{1,4}[a-z]?(?:\s*[-–—]\s*\d{1,4}[a-z]?)?(?:\s*[,;]\s*\d{1,4}[a-z]?(?:\s*[-–—]\s*\d{1,4}[a-z]?)?)*\s*\]"
)
_ALPHA_CITE = re.compile(
    r"\[\s*[A-Z][A-Za-z+]{0,5}\d{2}(?:\d{2})?[a-z]?(?:\s*[,;]\s*[A-Z][A-Za-z+]{0,5}\d{2}(?:\d{2})?[a-z]?)*\s*\]"
)
# Wikipedia-style editorial tags read as noise: "[citation needed]", "[clarification needed]", "[who?]".
_EDITORIAL_CITE = re.compile(
    r"\[\s*(?:(?:citation|clarification|verification|page|year|date|source)\s+needed|"
    r"who\??|when\??|which\??|by whom\??|according to whom\??|dubious|disputed|"
    r"failed verification|better source needed|not in citation given|original research\??)\s*\]",
    re.IGNORECASE,
)
_CITE_GAP = re.compile(r"^\s*(?:[,;–—-]\s*)?$")

_NAME = r"(?:(?:van|von|der|den|de|del|da|di|le|la|du|dos|al|el|bin|ibn|ter|ten)\s+)*[A-Z][\w'’\-]*[a-z][\w'’\-]*"
_AUTHORS = rf"{_NAME}(?:\s*,\s*{_NAME})*(?:,?\s+(?:and|&)\s+{_NAME})?(?:\s+et\s+al\.?)?"
_YEAR = r"(?:1[5-9]\d{2}|20\d{2})[a-z]?|n\.\s?d\.|in\s+press|forthcoming"
_LOCATOR = r"(?:pp?\.|chap\.|ch\.|sec\.|§)\s*\d+(?:\s*[-–]\s*\d+)?"
_CITE_ITEM = re.compile(
    rf"(?:(?:see|e\.g\.|cf\.|i\.e\.|see also|but see|also)\s*,?\s+)?{_AUTHORS}\s*,?\s*(?:{_YEAR})"
    rf"(?:\s*,\s*(?:{_YEAR}|{_LOCATOR}))*(?:\s*,?\s*{_LOCATOR})?"
)
_YEAR_ONLY = re.compile(rf"^\s*(?:{_YEAR})(?:\s*[,;]\s*(?:{_YEAR}))*(?:\s*,\s*{_LOCATOR})?\s*$")
_NARRATIVE_BEFORE = re.compile(rf"(?:et\s+al\.?|{_NAME}(?:\s+(?:and|&)\s+{_NAME})?)\s*$")

_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"]+", re.IGNORECASE)
_DOI = re.compile(r"\b(?:doi:\s*|https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/[^\s<>\"]+", re.IGNORECASE)
_TRAIL = ".,;:!?'\"”’"

_LATEX_INLINE = re.compile(r"\$[^$\n]{1,200}\$|\\\([^\n]{1,200}?\\\)")
_MATH_OPS = "=<>≤≥≈≠±×÷∑∏∫√∂∇∞∈∉⊂⊆∪∩→⇒↔∀∃^"
_OPERAND = r"(?:[A-Za-z]{1,3}(?:_\w+)?|\d+(?:[.,]\d+)?%?|[α-ωΑ-Ω]|[²³¹⁰⁴-⁹]+|\([^()\s]{1,20}\))"
_FORMULA = re.compile(
    rf"(?<![\w.]){_OPERAND}(?:[²³¹⁰⁴-⁹]+)?(?:\s*[{re.escape(_MATH_OPS)}+\-*/]\s*{_OPERAND}(?:[²³¹⁰⁴-⁹]+)?)+(?![\w])"
)
_STRONG_OP = re.compile(rf"[{re.escape(_MATH_OPS)}]|[²³¹⁰⁴-⁹]")
_SUPERSCRIPT_DIGITS = re.compile(r"(?<=[^\s\d])[¹²³⁴⁵⁶⁷⁸⁹⁰]+")
_SUP_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")

_NO_STRUCTURE = {BlockType.code, BlockType.equation, BlockType.table}


def _balanced(text: str, open_c: str, close_c: str) -> list[tuple[int, int]]:
    """Outermost balanced spans for one bracket pair (unbalanced openers are ignored)."""
    out: list[tuple[int, int]] = []
    depth = 0
    start = -1
    for i, c in enumerate(text):
        if c == open_c:
            if depth == 0:
                start = i
            depth += 1
        elif c == close_c and depth > 0:
            depth -= 1
            if depth == 0:
                out.append((start, i + 1))
    return out


def _trim_url(text: str, s: int, e: int) -> int:
    while e > s and text[e - 1] in _TRAIL:
        e -= 1
    # Drop unbalanced closing brackets ("(see https://x.org/a)").
    for o, c in ("()", "[]", "{}"):
        while e > s and text[e - 1] == c and text[s:e].count(c) > text[s:e].count(o):
            e -= 1
            while e > s and text[e - 1] in _TRAIL:
                e -= 1
    return e


def find_urls(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for rx in (_URL, _DOI):
        for m in rx.finditer(text):
            s, e = m.start(), _trim_url(text, m.start(), m.end())
            if e - s < 6 or any(s < te and e > ts for ts, te in spans):
                continue
            spans.append((s, e))
    return sorted(spans)


def _merge_adjacent(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans = sorted(spans)
    out: list[tuple[int, int]] = []
    for s, e in spans:
        if out and _CITE_GAP.match(text[out[-1][1] : s]):
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def find_citations(text: str) -> list[tuple[int, int]]:
    brackets = [(m.start(), m.end()) for rx in (_NUM_CITE, _ALPHA_CITE, _EDITORIAL_CITE) for m in rx.finditer(text)]
    spans = _merge_adjacent(text, brackets)
    for s, e in _balanced(text, "(", ")"):
        inner = text[s + 1 : e - 1]
        items = [i.strip() for i in re.split(r"\s*;\s*", inner) if i.strip()]
        if items and all(_CITE_ITEM.fullmatch(i) for i in items):
            spans.append((s, e))
        elif _YEAR_ONLY.match(inner) and _NARRATIVE_BEFORE.search(text[max(0, s - 80) : s]):
            # "Smith et al. (2020) showed" → the year is the citation; the name stays.
            spans.append((s, e))
    return sorted(spans)


def balanced_spans(text: str, open_c: str, close_c: str) -> list[tuple[int, int]]:
    """Outermost balanced `open_c … close_c` spans, brackets included."""
    return _balanced(text, open_c, close_c)


def find_citation_items(text: str) -> list[tuple[int, int]]:
    """Author-year items anywhere in `text`, bracketed or not: "Smith et al., 2020", "see Lee and Kim 2019, p. 4"."""
    return [(m.start(), m.end()) for m in _CITE_ITEM.finditer(text)]


def find_formulas(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _LATEX_INLINE.finditer(text)]
    for m in _FORMULA.finditer(text):
        frag = m.group()
        if not _STRONG_OP.search(frag):
            continue  # "a-b" or "3/4" alone aren't formulas
        s, e = m.start(), m.end()
        if any(s < te and e > ts for ts, te in spans):
            continue
        spans.append((s, e))
    return sorted(spans)


def detect_marks(
    text: str,
    *,
    block_type: BlockType = BlockType.paragraph,
    footnote_markers: Collection[str] = (),
    structural: Iterable[Span] = (),
) -> list[Span]:
    """All mark candidates for one block's text, sorted by start."""
    marks: list[Span] = [(k, s, e) for k, s, e in structural if 0 <= s < e <= len(text)]
    if not text.strip() or block_type in (BlockType.header, BlockType.footer, BlockType.page_number):
        return sorted(marks, key=lambda m: (m[1], -m[2]))
    urls = find_urls(text)
    marks += [(MarkKind.url, s, e) for s, e in urls]
    if block_type in _NO_STRUCTURE:
        return sorted(marks, key=lambda m: (m[1], -m[2]))

    formulas = find_formulas(text)
    taken_footnotes = {(s, e) for k, s, e in marks if k == MarkKind.footnote_ref}
    for m in _SUPERSCRIPT_DIGITS.finditer(text):
        if any(fs <= m.start() < fe for fs, fe in formulas):
            continue
        if m.group().translate(_SUP_MAP) in footnote_markers and (m.start(), m.end()) not in taken_footnotes:
            marks.append((MarkKind.footnote_ref, m.start(), m.end()))

    cites = [(s, e) for s, e in find_citations(text) if not any(us <= s and e <= ue for us, ue in urls)]
    marks += [(MarkKind.citation_ref, s, e) for s, e in cites]
    for kind, (o, c) in ((MarkKind.parenthetical, "()"), (MarkKind.bracket, "[]"), (MarkKind.brace, "{}")):
        for s, e in _balanced(text, o, c):
            if any(cs <= s and e <= ce for cs, ce in cites) or any(us <= s and e <= ue for us, ue in urls):
                continue
            if e - s <= 2:
                continue
            marks.append((kind, s, e))
    for s, e in formulas:
        if any(us <= s < ue for us, ue in urls) or any(cs <= s < ce for cs, ce in cites):
            continue
        marks.append((MarkKind.formula, s, e))
    # De-duplicate identical spans of the same kind.
    uniq = sorted(set(marks), key=lambda m: (m[1], -m[2], m[0].value))
    return uniq
