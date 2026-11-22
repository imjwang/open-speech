"""Chunking (05 § 6): synthesis units of 1–3 sentences (≈ 200–400 chars), split only at sentence
boundaries. The first unit of a document is a single sentence (time to first audio). A single sentence
longer than `hard_max` is split at a clause or word boundary as a safety net for provider input limits.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from open_speech.document.segment import sentences

TARGET_MAX = 400
TARGET_MIN = 200
MAX_SENTENCES = 3
HARD_MAX = 1000

_CLAUSE = re.compile(r"[;:,—–]\s+|\s+[-–—]\s+")


def _split_long(text: str, s: int, e: int, hard_max: int, ok: Callable[[int], bool]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    while e - s > hard_max:
        window_end = s + hard_max
        cut = -1
        for m in _CLAUSE.finditer(text, s + hard_max // 3, window_end):
            if ok(m.end()):
                cut = m.end()
        if cut < 0:
            sp = text.rfind(" ", s + hard_max // 3, window_end)
            while sp > s and not ok(sp + 1):
                sp = text.rfind(" ", s, sp)
            cut = sp + 1 if sp > s else window_end
        piece_end = cut
        while piece_end > s and text[piece_end - 1].isspace():
            piece_end -= 1
        out.append((s, piece_end))
        s = cut
        while s < e and text[s].isspace():
            s += 1
    if e > s:
        out.append((s, e))
    return out


def chunk_text(
    text: str,
    *,
    first_single: bool = False,
    boundary_ok: Callable[[int], bool] | None = None,
    target_max: int = TARGET_MAX,
    max_sentences: int = MAX_SENTENCES,
    hard_max: int = HARD_MAX,
) -> list[tuple[int, int]]:
    """Return chunk spans `(start, end)` into `text` (whitespace between chunks excluded)."""
    ok = boundary_ok or (lambda _pos: True)
    spans: list[tuple[int, int]] = []
    for s, e in sentences(text):
        spans.extend(_split_long(text, s, e, hard_max, ok) if e - s > hard_max else [(s, e)])
    if not spans:
        return []

    # Never cut inside an atomic replacement: glue sentences across unsafe boundaries.
    glued: list[tuple[int, int]] = []
    for s, e in spans:
        if glued and not ok(s):
            glued[-1] = (glued[-1][0], e)
        else:
            glued.append((s, e))
    spans = glued

    chunks: list[tuple[int, int, int]] = []  # (start, end, sentence count)
    i = 0
    if first_single:
        chunks.append((spans[0][0], spans[0][1], 1))
        i = 1
    while i < len(spans):
        cs, ce = spans[i]
        count = 1
        i += 1
        while i < len(spans) and count < max_sentences:
            _ns, ne = spans[i]
            if ne - cs > target_max:
                break
            ce = ne
            count += 1
            i += 1
        chunks.append((cs, ce, count))

    # Fold a short tail into the previous chunk when it still fits (never into the first-single unit).
    if len(chunks) >= 2 and not (first_single and len(chunks) == 2):
        (ps, _pe, pc), (ts, te, tc) = chunks[-2], chunks[-1]
        if te - ts < 60 and te - ps <= target_max + 60 and pc + tc <= max_sentences:
            chunks[-2:] = [(ps, te, pc + tc)]
    return [(s, e) for s, e, _ in chunks]
