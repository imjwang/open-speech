"""`MappedText`: text being edited for speech, with an exact map back to the source block text.

The text is a list of pieces. An *exact* piece is a verbatim slice of the source; an *atomic* piece is
replacement text (lexicon respelling, regex replacement, span-op replace, whitespace collapse) that maps
as a whole to the source range it replaced. Edits are expressed in current-text offsets, so stages can be
layered (drops → regex → annotate → lexicon → cleanup) and the map stays exact throughout. Utterance
`segments` are produced straight from the pieces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from open_speech.plan.script import TextSegment

_CLOSERS = frozenset(",.;:!?)]}”’\"'")
_OPENERS = frozenset("([{“‘\"'")


@dataclass(slots=True)
class Piece:
    text: str
    src_start: int
    src_end: int
    exact: bool


class MappedText:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pieces: list[Piece] = [Piece(source, 0, len(source), True)] if source else []
        self._text: str | None = source

    # ------------------------------------------------------------ views

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = "".join(p.text for p in self.pieces)
        return self._text

    def __len__(self) -> int:
        return len(self.text)

    def _offsets(self) -> list[int]:
        """Current-text start offset of each piece (plus the total length at the end)."""
        out = [0]
        for p in self.pieces:
            out.append(out[-1] + len(p.text))
        return out

    def source_range(self, start: int, end: int) -> tuple[int, int] | None:
        """Source range covered by current text [start, end) (atomic pieces count whole)."""
        offs = self._offsets()
        lo: int | None = None
        hi: int | None = None
        for i, p in enumerate(self.pieces):
            ps, pe = offs[i], offs[i + 1]
            if pe <= start or ps >= end or ps == pe:
                continue
            if p.exact:
                s = p.src_start + max(0, start - ps)
                e = p.src_start + min(len(p.text), end - ps)
            else:
                s, e = p.src_start, p.src_end
            lo = s if lo is None else min(lo, s)
            hi = e if hi is None else max(hi, e)
        if lo is None or hi is None:
            return None
        return lo, hi

    def is_exact_range(self, start: int, end: int) -> bool:
        """True when [start, end) lies entirely in exact pieces."""
        offs = self._offsets()
        for i, p in enumerate(self.pieces):
            ps, pe = offs[i], offs[i + 1]
            if pe <= start or ps >= end:
                continue
            if not p.exact:
                return False
        return True

    def boundary_ok(self, pos: int) -> bool:
        """True if `pos` doesn't fall strictly inside an atomic piece (safe place to cut utterances)."""
        offs = self._offsets()
        return not any(not p.exact and offs[i] < pos < offs[i + 1] for i, p in enumerate(self.pieces))

    def cuts(self) -> list[int]:
        """Current-text positions where source text was removed (gaps in source coverage)."""
        out: list[int] = []
        offs = self._offsets()
        prev_end = 0
        for i, p in enumerate(self.pieces):
            if p.src_start > prev_end:
                out.append(offs[i])
            prev_end = max(prev_end, p.src_end)
        if prev_end < len(self.source):
            out.append(offs[-1])
        return out

    # ------------------------------------------------------------ editing

    def _split(self, pos: int) -> None:
        """Ensure a piece boundary at current offset `pos` if it falls inside an exact piece."""
        offs = self._offsets()
        for i, p in enumerate(self.pieces):
            ps, pe = offs[i], offs[i + 1]
            if ps < pos < pe and p.exact:
                k = pos - ps
                self.pieces[i : i + 1] = [
                    Piece(p.text[:k], p.src_start, p.src_start + k, True),
                    Piece(p.text[k:], p.src_start + k, p.src_end, True),
                ]
                return

    def edit(self, start: int, end: int, new_text: str) -> tuple[int, int] | None:
        """Replace current text [start, end) with `new_text`. Partially covered atomic pieces are covered
        whole. Returns the source range replaced, or None if the range was empty (no insertions)."""
        start = max(0, start)
        end = min(len(self.text), end)
        if end <= start:
            return None
        self._split(start)
        self._split(end)
        offs = self._offsets()
        first = last = -1
        for i in range(len(self.pieces)):
            ps, pe = offs[i], offs[i + 1]
            if pe <= start or ps >= end:
                continue
            if first < 0:
                first = i
            last = i
        if first < 0:
            return None
        covered = self.pieces[first : last + 1]
        src_lo = min(p.src_start for p in covered)
        src_hi = max(p.src_end for p in covered)
        replacement = [Piece(new_text, src_lo, src_hi, False)] if new_text else []
        self.pieces[first : last + 1] = replacement
        self._text = None
        return src_lo, src_hi

    def delete(self, start: int, end: int, *, absorb_space: bool = True) -> tuple[int, int] | None:
        """Delete [start, end), absorbing one side's whitespace so no double space or space-before-
        punctuation is left behind. Returns the source range of the requested span (not the whitespace)."""
        t = self.text
        rng = self.source_range(start, end)
        if rng is None:
            return None
        if absorb_space:
            n = len(t)
            if start > 0 and t[start - 1].isspace() and (end >= n or t[end].isspace() or t[end] in _CLOSERS):
                while start > 0 and t[start - 1].isspace():
                    start -= 1
            elif (start == 0 or t[start - 1] in _OPENERS) and end < n and t[end].isspace():
                while end < n and t[end].isspace():
                    end += 1
        self.edit(start, end, "")
        return rng

    def replace(self, start: int, end: int, new_text: str) -> tuple[int, int] | None:
        if new_text == "":
            return self.delete(start, end)
        return self.edit(start, end, new_text)

    # ------------------------------------------------------------ cleanup

    _WS = re.compile(r"\s+")
    _CLEANUPS: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"\(\s*[,;:]?\s*\)|\[\s*[,;:]?\s*\]|\{\s*[,;:]?\s*\}"), ""),  # emptied brackets
        (re.compile(r"(?<=[(\[{])\s*[,;:]\s*"), ""),  # "( , x" → "(x"
        (re.compile(r"\s*[,;:]\s*(?=[)\]}])"), ""),  # "x ; )" → "x)"
        (re.compile(r"[,;:]\s*(?=[,.;:!?])"), ""),  # ", ." → "."
        (re.compile(r"\s+(?=[,.;:!?)\]}])"), ""),  # space before punctuation
        (re.compile(r"^\s*[,.;:]+\s*"), ""),  # leading punctuation left at the start
    )

    def cleanup(self, *, punctuation: bool = True) -> None:
        """Collapse whitespace to single spaces, trim the ends, and (near cuts only) tidy punctuation left
        behind by deletions: emptied brackets, doubled commas, space before punctuation."""
        if punctuation:
            for _ in range(3):
                changed = False
                cuts = self.cuts()
                if not cuts:
                    break
                for rx, repl in self._CLEANUPS:
                    t = self.text
                    matches = [m for m in rx.finditer(t) if m.end() > m.start()]
                    for m in reversed(matches):
                        if any(m.start() <= c <= m.end() for c in cuts):
                            if repl:
                                self.edit(m.start(), m.end(), repl)
                            else:
                                self.edit(m.start(), m.end(), "")
                            changed = True
                    if changed:
                        cuts = self.cuts()
                if not changed:
                    break
        # Whitespace: trim ends, collapse runs.
        t = self.text
        runs = list(self._WS.finditer(t))
        n = len(t)
        for m in reversed(runs):
            s, e = m.start(), m.end()
            if s == 0 or e == n:
                self.edit(s, e, "")
            elif m.group() != " ":
                self.edit(s, e, " ")

    # ------------------------------------------------------------ export

    def segments(self, block_id: str, start: int = 0, end: int | None = None) -> list[TextSegment]:
        """Segments for current text [start, end), with spoken offsets rebased to `start`.

        Exact pieces are clipped precisely; an atomic piece overlapping the range is included whole on the
        source side and clipped on the spoken side. Adjacent exact pieces that are contiguous in the source
        merge into one segment.
        """
        if end is None:
            end = len(self.text)
        out: list[TextSegment] = []
        pos = 0
        prev_exact = False
        for p in self.pieces:
            ps, pe = pos, pos + len(p.text)
            pos = pe
            if pe <= start or ps >= end or ps == pe:
                prev_exact = False
                continue
            cs, ce = max(ps, start), min(pe, end)
            if p.exact:
                s_src = p.src_start + (cs - ps)
                e_src = p.src_start + (ce - ps)
            else:
                s_src, e_src = p.src_start, p.src_end
            seg = TextSegment(
                spoken_start=cs - start,
                spoken_end=ce - start,
                block_id=block_id,
                src_start=s_src,
                src_end=e_src,
            )
            if (
                out
                and p.exact
                and prev_exact
                and out[-1].spoken_end == seg.spoken_start
                and out[-1].src_end == seg.src_start
            ):
                out[-1] = out[-1].model_copy(update={"spoken_end": seg.spoken_end, "src_end": seg.src_end})
            else:
                out.append(seg)
            prev_exact = p.exact
        return out
