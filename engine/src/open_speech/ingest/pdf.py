"""PDF → blocks (05 § 2 PDF heuristics; FR-READ-1). pypdfium2 char boxes + pdfplumber for ruled tables.

Pipeline per document:
1. Chars (loose boxes, size, baseline, bold) → words (superscripts kept as separate tokens) → line
   fragments in content order; big horizontal gaps split fragments so row-wise column text separates.
2. Header/footer bands (top/bottom 8%): normalized text repeating on ≥ 30% of pages → header/footer;
   short numeric/roman lines and "Page 4 of 18" → page_number.
3. Footnotes: small font (≤ 85% of body median) in the lower part of the page, nothing body-sized below,
   starting with a marker that matches an in-text superscript reference.
4. Columns: zero-coverage gutters among narrow lines; lines crossing a gutter are full-width and break the
   page into bands; each band is read column by column.
5. Lines → blocks by spacing, indentation, size and markers; typed as heading (size/bold rank → level),
   list_item, caption (Figure/Table N next to a graphic or in small type), equation, toc, table (ruled,
   via pdfplumber) or paragraph; a References heading turns what follows into reference_entry blocks.
6. Hyphenated line breaks re-join ("exam-" + "ple" → "example") with one word box per line part; a
   paragraph continuing into the next column/page is merged. Every word gets a `WordBox` (PDF points,
   top-left origin) so the reader can draw highlights.
"""

from __future__ import annotations

import io
import logging
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from open_speech.document.model import BBox, BlockType, Document, MarkKind, SourceKind, WordBox
from open_speech.errors import EngineError
from open_speech.ingest.common import (
    DraftBlock,
    build_document,
    clean_title,
    finalize_blocks,
    infer_title,
    normalize_space,
    source_id_bytes,
)

log = logging.getLogger("open_speech.ingest.pdf")

BAND = 0.08  # header/footer band height (fraction of page height)
REPEAT = 0.30  # fraction of pages a band line must repeat on
SMALL = 0.85  # footnote font size ratio to the body median

_PAGE_NUM = re.compile(
    r"^(?:(?:page|p\.|pg\.?)\s*)?(?:\d{1,4}|[ivxlcdm]{1,7}|[IVXLCDM]{1,7})(?:\s*(?:of|/)\s*\d{1,4})?$"
    r"|^[-–—]\s*\d{1,4}\s*[-–—]$",
    re.IGNORECASE,
)
_ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m{0,4}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.IGNORECASE)
_BULLET_GLYPHS = {"•", "◦", "▪", "▫", "●", "○", "■", "□", "‣", "∙", "·", "–", "—", "-", "*", "➢", "►", "✓"}
_LIST_START = re.compile(r"^(?:\(?\d{1,3}[.)]|\(?[a-z][.)]|\[\d{1,4}\])\s")
_CAPTION = re.compile(
    r"^(?:Figure|Fig\.|Table|Tab\.|Listing|Algorithm|Chart|Plate|Exhibit|Scheme)\s*"
    r"(?:[0-9]+|[IVX]+)[a-z]?(?:\.\d+)?\s*[:.|—–-]",
)
_TOC_LINE = re.compile(r"(?:\.\s?){4,}\s*\d{1,4}\s*$|\s(?:\.\s?){3,}\s*[ivxlcdm\d]{1,5}\s*$", re.IGNORECASE)
_REFS_HEADING = re.compile(
    r"^(?:[0-9IVX]+(?:\.\d+)*\.?\s*)?(?:references|bibliography|works cited|literature cited|reference list|"
    r"citations)\s*$",
    re.IGNORECASE,
)
_FOOT_MARKER = re.compile(r"^(\d{1,3}|[*†‡§¶#]{1,3})(?=\s|[A-Za-z]|$)")
_MATH_CHARS = set("=+−×÷∑∏∫√≤≥≈≠∞∂∇αβγδεθλμπσφψωΔΣΩ^_/<>|")
_TERMINAL = re.compile(r"[.!?:][\"'”’)\]]*$")
_CONTINUABLE = {BlockType.paragraph, BlockType.reference_entry, BlockType.list_item}


# ------------------------------------------------------------------ data


@dataclass(slots=True)
class PChar:
    c: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    base: float
    bold: bool


@dataclass
class PWord:
    chars: list[PChar]
    sup: bool = False

    @property
    def text(self) -> str:
        return "".join(c.c for c in self.chars)

    @property
    def x0(self) -> float:
        return min(c.x0 for c in self.chars)

    @property
    def x1(self) -> float:
        return max(c.x1 for c in self.chars)

    @property
    def top(self) -> float:
        return min(c.top for c in self.chars)

    @property
    def bottom(self) -> float:
        return max(c.bottom for c in self.chars)


@dataclass
class PLine:
    words: list[PWord]
    page: int
    kind: str = "body"  # body | header | footer | page_number | footnote | table
    table_text: str = ""
    _cache: dict[str, float] = field(default_factory=dict, repr=False)

    def _main(self) -> list[PChar]:
        chars = [c for w in self.words if not w.sup for c in w.chars]
        return chars or [c for w in self.words for c in w.chars]

    @property
    def size(self) -> float:
        if "size" not in self._cache:
            self._cache["size"] = statistics.median(c.size for c in self._main()) if self.words else 0.0
        return self._cache["size"]

    @property
    def bold(self) -> bool:
        chars = [c for c in self._main() if not c.c.isspace()]
        return bool(chars) and sum(c.bold for c in chars) >= 0.8 * len(chars)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def top(self) -> float:
        return min(c.top for c in self._main())

    @property
    def bottom(self) -> float:
        return max(c.bottom for c in self._main())

    @property
    def text(self) -> str:
        out = ""
        for w in self.words:
            out += w.text if (w.sup and out) or not out else " " + w.text
        return out


@dataclass
class PageInfo:
    number: int
    width: float
    height: float
    lines: list[PLine]
    graphics: list[tuple[float, float, float, float]]  # x0, top, x1, bottom
    images: list[tuple[float, float, float, float]]
    n_chars: int
    path_count: int


# ------------------------------------------------------------------ char extraction


def _font_bold(name: str, weight: int) -> bool:
    if weight >= 600:
        return True
    n = name.lower()
    return any(k in n for k in ("bold", "black", "heavy", "semibold", "demi")) or n.endswith((",b", "-b"))


def _read_page(pdf: object, index: int) -> PageInfo:
    import ctypes

    import pypdfium2.raw as c

    page = pdf[index]  # type: ignore[index]
    width, height = page.get_size()
    tp = page.get_textpage()
    n = tp.count_chars()
    chars: list[PChar | None] = []  # None = word break
    buf = ctypes.create_string_buffer(128)
    flags = ctypes.c_int(0)
    ox, oy = ctypes.c_double(), ctypes.c_double()
    matrix = c.FS_MATRIX() if hasattr(c, "FS_MATRIX") else None
    get_matrix = getattr(c, "FPDFText_GetMatrix", None)
    non_space = 0
    for i in range(n):
        code = c.FPDFText_GetUnicode(tp, i)
        ch = chr(code) if 0 < code < 0x110000 else ""
        generated = c.FPDFText_IsGenerated(tp, i) == 1
        if not ch or ch in "\r\n\x00" or ch.isspace():
            chars.append(None)
            continue
        if generated:
            continue
        if ch in "\x02\xad":
            ch = "-"
        if code in (0xFFFE, 0xFFFF) or (ord(ch) < 32):
            continue
        if (
            get_matrix is not None
            and matrix is not None
            and get_matrix(tp, i, ctypes.byref(matrix))
            and abs(matrix.b) > abs(matrix.a) + 1e-6
        ):
            continue  # rotated ≥ 45°: margin stamps, axis labels
        left, bottom, right, top = tp.get_charbox(i, loose=True)
        size = c.FPDFText_GetFontSize(tp, i)
        c.FPDFText_GetCharOrigin(tp, i, ctypes.byref(ox), ctypes.byref(oy))
        c.FPDFText_GetFontInfo(tp, i, buf, 128, ctypes.byref(flags))
        weight = c.FPDFText_GetFontWeight(tp, i)
        name = buf.value.decode("latin-1", "replace")
        chars.append(
            PChar(
                c=ch,
                x0=left,
                x1=right,
                top=height - top,
                bottom=height - bottom,
                size=round(size, 2) or 10.0,
                base=height - oy.value,
                bold=_font_bold(name, weight),
            )
        )
        non_space += 1
    graphics: list[tuple[float, float, float, float]] = []
    images: list[tuple[float, float, float, float]] = []
    path_count = 0
    for obj in page.get_objects():
        if obj.type not in (2, 3):  # path, image
            continue
        try:
            l_, b_, r_, t_ = obj.get_bounds()
        except Exception:
            continue
        rect = (l_, height - t_, r_, height - b_)
        if obj.type == 2:
            path_count += 1
        area = (r_ - l_) * (t_ - b_)
        if area < 0.002 * width * height:
            continue
        graphics.append(rect)
        if obj.type == 3:
            images.append(rect)
    lines = _build_lines(chars, index + 1)
    return PageInfo(index + 1, width, height, lines, graphics, images, non_space, path_count)


def _build_lines(chars: Sequence[PChar | None], page: int) -> list[PLine]:
    lines: list[PLine] = []
    words: list[PWord] = []
    cur: list[PChar] = []
    cur_sup = False
    base = 0.0
    lsize = 0.0
    prev: PChar | None = None

    def close_word() -> None:
        nonlocal cur
        if cur:
            words.append(PWord(cur, cur_sup))
            cur = []

    def close_line() -> None:
        nonlocal words, prev
        close_word()
        if words and any(not w.sup for w in words):
            lines.append(PLine(words, page))
        elif words and lines:
            lines[-1].words.extend(words)  # a stray superscript: attach to the previous line
        words = []
        prev = None

    for ch in chars:
        if ch is None:
            close_word()
            continue
        if prev is None:
            close_line()
            base, lsize = ch.base, ch.size
            cur_sup = False
            cur = [ch]
            prev = ch
            continue
        raised = base - ch.base
        is_sup = ch.size < 0.85 * lsize and 0.12 * lsize < raised < 0.75 * lsize
        same_line = is_sup or abs(ch.base - base) <= 0.35 * max(lsize, ch.size)
        gap = ch.x0 - prev.x1
        if (
            not same_line
            and not words
            and len(cur) <= 3
            and all(c.size < 0.85 * ch.size for c in cur)
            and 0.1 * ch.size < ch.base - base < 0.75 * ch.size
            and -1.0 <= gap < ch.size
        ):
            # The line started with a superscript marker ("¹Code and data…"): re-base on the text.
            cur_sup = True
            close_word()
            base, lsize = ch.base, ch.size
            cur_sup = False
            cur = [ch]
            prev = ch
            continue
        if not same_line or gap < -max(lsize, 2.0) or gap > max(1.5 * lsize, 12.0):
            close_line()
            base, lsize = ch.base, ch.size
            cur_sup = False
            cur = [ch]
            prev = ch
            continue
        if is_sup != cur_sup or gap > 0.25 * min(lsize, ch.size):
            close_word()
            cur_sup = is_sup
        if not is_sup and ch.size > lsize * 1.2 and not words and len(cur) <= 1:
            base, lsize = ch.base, ch.size  # drop caps / first char of a larger run
        cur.append(ch)
        prev = ch
    close_line()
    return _merge_fragments(lines)


def _merge_fragments(lines: list[PLine]) -> list[PLine]:
    """Join fragments on the same baseline that are horizontally adjacent (content-order artifacts)."""
    out: list[PLine] = []
    for ln in lines:
        if out:
            p = out[-1]
            same = abs(p.bottom - ln.bottom) < 0.3 * max(p.size, ln.size) and abs(p.size - ln.size) < 0.15 * p.size
            gap = ln.x0 - p.x1
            if same and -1.0 <= gap <= max(1.0 * p.size, 6.0):
                p.words.extend(ln.words)
                p._cache.clear()
                continue
        out.append(ln)
    return out


# ------------------------------------------------------------------ cross-page classification


def _norm_band_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\d+", "#", t)
    t = " ".join("#" if _ROMAN.match(tok) else tok for tok in t.split())
    return re.sub(r"\s+", " ", t).strip()


def _classify_bands(pages: Sequence[PageInfo]) -> None:
    n = len(pages)
    threshold = max(2, math.ceil(REPEAT * n))
    counts: dict[tuple[str, str], set[int]] = defaultdict(set)
    banded: list[tuple[PLine, str, PageInfo]] = []
    for pg in pages:
        for ln in pg.lines:
            mid = (ln.top + ln.bottom) / 2
            band = "top" if mid < BAND * pg.height else "bottom" if mid > (1 - BAND) * pg.height else ""
            if not band:
                continue
            banded.append((ln, band, pg))
            counts[(band, _norm_band_text(ln.text))].add(pg.number)
    for ln, band, _pg in banded:
        text = normalize_space(ln.text)
        if _PAGE_NUM.match(text) and (not text.isalpha() or _ROMAN.match(text)):
            ln.kind = "page_number"
        elif n > 1 and len(counts[(band, _norm_band_text(ln.text))]) >= threshold:
            ln.kind = "header" if band == "top" else "footer"


def _body_size(pages: Sequence[PageInfo]) -> float:
    sizes: Counter[float] = Counter()
    for pg in pages:
        for ln in pg.lines:
            if ln.kind != "body":
                continue
            for w in ln.words:
                if not w.sup:
                    for ch in w.chars:
                        sizes[round(ch.size * 2) / 2] += 1
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _sup_markers(pages: Sequence[PageInfo]) -> set[str]:
    return {
        w.text.strip()
        for pg in pages
        for ln in pg.lines
        if ln.kind == "body"
        for i, w in enumerate(ln.words)
        if w.sup and i > 0 and re.fullmatch(r"\d{1,3}|[*†‡§¶#]{1,3}", w.text.strip())
    }


def _footnote_marker(ln: PLine) -> str | None:
    if not ln.words:
        return None
    first = ln.words[0]
    if first.sup:
        return first.text.strip()
    m = _FOOT_MARKER.match(ln.text)
    return m.group(1) if m else None


def _classify_footnotes(pages: Sequence[PageInfo], body: float, refs: set[str]) -> set[str]:
    """Flag footnote lines; return the set of footnote markers found."""
    found: set[str] = set()
    for pg in pages:
        body_lines = [ln for ln in pg.lines if ln.kind == "body"]
        small = sorted(
            (ln for ln in body_lines if ln.size <= SMALL * body and ln.top > 0.45 * pg.height),
            key=lambda ln: ln.top,
        )
        started: list[PLine] = []
        for ln in small:
            if ln.kind == "footnote":
                continue
            marker = _footnote_marker(ln)
            if marker is None or marker not in refs:
                continue
            below = [
                o for o in body_lines if o.top > ln.top + 1 and o.size > SMALL * body and o.x0 < ln.x1 and o.x1 > ln.x0
            ]
            if below:
                continue
            started.append(ln)
            found.add(marker)
            ln.kind = "footnote"
            for o in small:
                if o.top > ln.top and o.kind == "body" and o.x0 < ln.x1 + 5 and o.x1 > ln.x0 - 5:
                    o.kind = "footnote"
                    m2 = _footnote_marker(o)
                    if m2 is not None and m2 in refs:
                        found.add(m2)
    return found


def _footnote_groups(lines: Sequence[PLine], markers: set[str]) -> list[list[PLine]]:
    """Footnote lines → one group per footnote, column by column (stacks of horizontally overlapping lines)."""
    stacks: list[list[PLine]] = []
    for ln in sorted(lines, key=lambda ln: ln.x0):
        for st in stacks:
            if ln.x0 < max(o.x1 for o in st) and ln.x1 > min(o.x0 for o in st):
                st.append(ln)
                break
        else:
            stacks.append([ln])
    groups: list[list[PLine]] = []
    for st in sorted(stacks, key=lambda st: min(o.x0 for o in st)):
        cur: list[PLine] = []
        for ln in sorted(st, key=lambda ln: ln.top):
            if cur and _footnote_marker(ln) in markers:
                groups.append(cur)
                cur = []
            cur.append(ln)
        if cur:
            groups.append(cur)
    return groups


# ------------------------------------------------------------------ layout


def _gutters(body: Sequence[PLine]) -> list[tuple[float, float]]:
    """Vertical gutters (x-intervals with (almost) no narrow-line coverage) separating text columns."""
    left = min(ln.x0 for ln in body)
    right = max(ln.x1 for ln in body)
    width = right - left
    if width <= 0:
        return []
    narrow = [ln for ln in body if ln.x1 - ln.x0 < 0.6 * width and ln.kind in ("body", "table")]
    if len(narrow) < 6:
        return []
    nb = int(width) + 2
    cov = [0] * nb
    for ln in narrow:
        for x in range(max(0, int(ln.x0 - left)), min(nb, int(ln.x1 - left) + 1)):
            cov[x] += 1
    # A gutter may be crossed by a few full-width lines (centered titles, author lines, wide captions).
    allowed = max(1, int(0.08 * len(narrow)))
    gutters: list[tuple[float, float]] = []
    x = int(0.15 * width)
    end = int(0.85 * width)
    while x < end:
        if cov[x] <= allowed:
            s = x
            while x < nb and cov[x] <= allowed:
                x += 1
            gs, ge = left + s, left + x
            if x - s >= 6:
                lhs = [ln for ln in narrow if ln.x1 <= ge + 1 and ln.x0 < gs - 1]
                rhs = [ln for ln in narrow if ln.x0 >= gs - 1 and ln.x1 > ge + 1]
                crossing = [ln for ln in narrow if ln.x0 < gs - 1 and ln.x1 > ge + 1]
                if _aligned(lhs, "x0") and _aligned(rhs, "x0") and len(crossing) < 0.25 * min(len(lhs), len(rhs)):
                    gutters.append((gs, ge))
        x += 1
    return gutters


def _aligned(lines: Sequence[PLine], edge: str, *, minimum: int = 3, tol: float = 2.0) -> bool:
    """At least `minimum` lines share a left (or right) edge — the signature of a text column."""
    if len(lines) < minimum:
        return False
    xs = sorted(getattr(ln, edge) for ln in lines)
    best = 1
    j = 0
    for i in range(len(xs)):
        while xs[i] - xs[j] > tol:
            j += 1
        best = max(best, i - j + 1)
    return best >= minimum


def _flows(lines: Sequence[PLine]) -> list[list[PLine]]:
    """Reading-order flows: bands split by full-width lines, each band read column by column."""
    body = [ln for ln in lines if ln.kind in ("body", "table")]
    if not body:
        return []
    gutters = _gutters(body)
    if not gutters:
        return [sorted(body, key=lambda ln: (round(ln.top, 1), ln.x0))]

    def col_of(ln: PLine) -> int | None:
        k = 0
        for gs, ge in gutters:
            if ln.x0 < gs - 1 and ln.x1 > ge + 1:
                return None  # crosses a gutter: full-width
            if ln.x0 >= gs - 1:
                k += 1
        return k

    flows: list[list[PLine]] = []
    cols = range(len(gutters) + 1)
    band: list[list[PLine]] = [[] for _ in cols]
    span: list[PLine] = []
    for ln in sorted(body, key=lambda ln: (ln.top, ln.x0)):
        k = col_of(ln)
        if k is None:
            if any(band):
                flows.extend(sorted(c, key=lambda ln: ln.top) for c in band if c)
                band = [[] for _ in cols]
            span.append(ln)
        else:
            if span:
                flows.append(span)
                span = []
            band[k].append(ln)
    if span:
        flows.append(span)
    flows.extend(sorted(c, key=lambda ln: ln.top) for c in band if c)
    return flows


def _is_heading_line(ln: PLine, body: float) -> bool:
    if ln.kind != "body":
        return False
    text = ln.text.strip()
    if not text or len(text) > 150 or len(text.split()) > 20:
        return False
    if _CAPTION.match(text) or _TOC_LINE.search(text):
        return False
    big = ln.size >= 1.15 * body
    bold = ln.bold and ln.size >= 0.95 * body
    if not (big or bold):
        return False
    if bold and not big and _TERMINAL.search(text) and len(text.split()) > 6:
        return False  # a bold sentence, not a heading
    return not (sum(ch.isalpha() for ch in text) < 2)


def _is_item_start(ln: PLine) -> bool:
    return bool(ln.words) and (ln.words[0].text in _BULLET_GLYPHS or bool(_LIST_START.match(ln.text.strip())))


@dataclass
class _FlowStats:
    body: float
    left: float
    right: float
    width: float
    pitch: float
    justified: bool


def _flow_stats(flow: Sequence[PLine], body: float) -> _FlowStats:
    left = statistics.median(ln.x0 for ln in flow) if len(flow) > 2 else min(ln.x0 for ln in flow)
    left = min(left, min(ln.x0 for ln in flow) + 1.0)
    right = max(ln.x1 for ln in flow)
    pitches = [
        b.top - a.top
        for a, b in zip(flow, flow[1:], strict=False)
        if abs(a.size - b.size) < 0.1 * a.size and 0 < b.top - a.top < 3 * a.size and not _is_heading_line(a, body)
    ]
    justified = len(flow) >= 4 and sum(1 for ln in flow if right - ln.x1 < 2.5) >= 0.6 * len(flow)
    return _FlowStats(
        body=body,
        left=left,
        right=right,
        width=max(1.0, right - left),
        pitch=statistics.median(pitches) if pitches else 1.2 * body,
        justified=justified,
    )


def _starts_block(ln: PLine, prev: PLine, first: PLine, st: _FlowStats) -> bool:
    """Does line `ln` start a new block, given the previous line and the current block's first line?"""
    if ln.kind == "table" or prev.kind == "table":
        return True
    h, ph = _is_heading_line(ln, st.body), _is_heading_line(prev, st.body)
    if h != ph:
        return True
    if h:  # consecutive heading lines merge when they share a size and sit close together
        return abs(ln.size - prev.size) > 0.1 * prev.size or ln.top - prev.bottom > 0.9 * ln.size
    if abs(ln.size - prev.size) > 0.08 * max(ln.size, prev.size):
        return True
    if ln.top < prev.top - 1 or ln.top - prev.top > 1.3 * max(st.pitch, 0.9 * ln.size):
        return True  # went back up (new column) or extra vertical space
    text, ptext = ln.text.strip(), prev.text.strip()
    if _is_item_start(ln) or _CAPTION.match(text):
        return True
    if _is_item_start(first) and ln.x0 > first.x0 + 0.5 * ln.size:
        return False  # hanging indent: continuation of a list item or reference entry
    if (
        ln.x0 > st.left + 0.8 * ln.size
        and prev.x0 <= st.left + 0.5 * ln.size
        and (_TERMINAL.search(ptext) or st.right - prev.x1 > 0.12 * st.width)
    ):
        return True  # first-line indent after a finished paragraph
    # Short last line of a justified paragraph.
    return st.justified and st.right - prev.x1 > max(1.5 * prev.size, 0.1 * st.width) and bool(_TERMINAL.search(ptext))


def _group(flow: Sequence[PLine], body: float) -> list[list[PLine]]:
    if not flow:
        return []
    st = _flow_stats(flow, body)
    groups: list[list[PLine]] = []
    for ln in flow:
        if groups and not _starts_block(ln, groups[-1][-1], groups[-1][0], st):
            groups[-1].append(ln)
        else:
            groups.append([ln])
    return groups


Rect = tuple[float, float, float, float]


def _near_graphic(bbox: Rect, graphics: Sequence[Rect]) -> bool:
    x0, top, x1, bottom = bbox
    for gx0, gtop, gx1, gbottom in graphics:
        if gx1 < x0 - 5 or gx0 > x1 + 5:
            continue
        if -40 <= top - gbottom <= 40 or -40 <= gtop - bottom <= 40:
            return True
    return False


def _math_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    return sum(c in _MATH_CHARS for c in chars) / len(chars) if chars else 0.0


@dataclass
class _Group:
    lines: list[PLine]
    type: BlockType
    level_size: float = 0.0  # heading font size (for level ranking)
    bold: bool = False


def _type_group(lines: list[PLine], body: float, flow_center: float, graphics: Sequence) -> _Group:
    first = lines[0]
    text = " ".join(ln.text.strip() for ln in lines)
    if first.kind == "table":
        return _Group(lines, BlockType.table)
    if first.kind == "footnote":
        return _Group(lines, BlockType.footnote)
    if all(_is_heading_line(ln, body) for ln in lines):
        return _Group(lines, BlockType.heading, level_size=first.size, bold=first.bold)
    bbox = (
        min(ln.x0 for ln in lines),
        min(ln.top for ln in lines),
        max(ln.x1 for ln in lines),
        max(ln.bottom for ln in lines),
    )
    if _CAPTION.match(text) and (_near_graphic(bbox, graphics) or first.size < 0.95 * body):
        return _Group(lines, BlockType.caption)
    if all(_TOC_LINE.search(ln.text) for ln in lines):
        return _Group(lines, BlockType.toc)
    if first.words and first.words[0].text in _BULLET_GLYPHS and len(first.words) > 1:
        return _Group(lines, BlockType.list_item)
    if _LIST_START.match(text) and not re.match(r"^\[\d", text):
        return _Group(lines, BlockType.list_item)
    center = (bbox[0] + bbox[2]) / 2
    if (
        len(lines) <= 3
        and abs(center - flow_center) < 0.2 * max(1.0, bbox[2] - bbox[0] + 40)
        and ("=" in text or _math_ratio(text) > 0.15)
        and (re.search(r"\(\d{1,3}[a-z]?\)\s*$", text) or _math_ratio(text) > 0.15)
    ):
        return _Group(lines, BlockType.equation)
    return _Group(lines, BlockType.paragraph)


def _assemble(group: _Group) -> tuple[str, list[WordBox], list[tuple[int, int, str]], BBox]:
    """Text, word boxes, superscript spans (start, end, text) and bbox for a group of lines."""
    parts: list[str] = []
    boxes: list[WordBox] = []
    sups: list[tuple[int, int, str]] = []
    pos = 0
    page = group.lines[0].page
    skip_first = group.type == BlockType.list_item and group.lines[0].words[0].text in _BULLET_GLYPHS
    prev_word: PWord | None = None
    for li, ln in enumerate(group.lines):
        for wi, w in enumerate(ln.words):
            if skip_first and li == 0 and wi == 0:
                continue
            t = w.text
            sep = " " if parts else ""
            if parts and w.sup:
                sep = ""  # superscript marker attaches to the word before it
            if prev_word is not None and prev_word.sup and len(boxes) == 1:
                sep = " "  # leading footnote marker: "¹ Text" → "1 Text"
            joined = False
            if (
                li > 0
                and wi == 0
                and prev_word is not None
                and not prev_word.sup
                and len(prev_word.text) > 2
                and prev_word.text.endswith("-")
                and prev_word.text[-2].isalpha()
                and "-" not in prev_word.text[:-1]
                and t[:1].islower()
            ):
                # Re-join a hyphenated line break; trim the hyphen from the previous word's box.
                parts[-1] = parts[-1][:-1]
                pos -= 1
                last = boxes[-1]
                hyphen = prev_word.chars[-1]
                boxes[-1] = WordBox(
                    start=last.start,
                    end=last.end - 1,
                    bbox=last.bbox.model_copy(update={"x1": max(last.bbox.x0, hyphen.x0)}),
                )
                sep = ""
                joined = True
            parts.append(sep + t)
            start = pos + len(sep)
            pos = start + len(t)
            wbox = BBox(page=ln.page, x0=w.x0, y0=w.top, x1=w.x1, y1=w.bottom)
            boxes.append(WordBox(start=start, end=pos, bbox=wbox))
            if w.sup and len(boxes) > 1 and not joined:
                sups.append((start, pos, t))
            prev_word = w
    text = "".join(parts)
    same = [ln for ln in group.lines if ln.page == page]
    bbox = BBox(
        page=page,
        x0=min(ln.x0 for ln in same),
        y0=min(ln.top for ln in same),
        x1=max(ln.x1 for ln in same),
        y1=max(ln.bottom for ln in same),
    )
    return text, boxes, sups, bbox


# ------------------------------------------------------------------ tables (pdfplumber)


def _detect_tables(data: bytes, pages: Sequence[PageInfo]) -> None:
    """Replace lines inside ruled tables with one table pseudo-line per table (pdfplumber)."""
    candidates = [pg for pg in pages if pg.path_count >= 8]
    if not candidates:
        return
    try:
        import pdfplumber
    except ImportError:  # pragma: no cover
        return
    try:
        with pdfplumber.open(io.BytesIO(data)) as plumber:
            for pg in candidates:
                try:
                    tables = plumber.pages[pg.number - 1].find_tables()
                except Exception:
                    continue
                for t in tables:
                    x0, top, x1, bottom = t.bbox
                    rows = t.extract() or []
                    if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
                        continue
                    inside = [
                        ln
                        for ln in pg.lines
                        if ln.kind == "body"
                        and x0 - 2 <= (ln.x0 + ln.x1) / 2 <= x1 + 2
                        and top - 2 <= (ln.top + ln.bottom) / 2 <= bottom + 2
                    ]
                    if not inside:
                        continue
                    lines_text = []
                    for r in rows:
                        cells = [normalize_space(c or "") for c in r]
                        cells = [c for c in cells if c]
                        if cells:
                            line = ", ".join(cells)
                            lines_text.append(line if line.endswith((".", "!", "?")) else line + ".")
                    words = [w for ln in sorted(inside, key=lambda ln: (ln.top, ln.x0)) for w in ln.words]
                    for ln in inside:
                        ln.kind = "table_member"
                    pg.lines.append(PLine(words, pg.number, kind="table", table_text="\n".join(lines_text)))
    except Exception as e:  # never fail extraction because of table detection
        log.warning("table detection failed: %s", e)


# ------------------------------------------------------------------ document


def _heading_levels(groups: Sequence[_Group], body: float) -> dict[float, int]:
    sizes = sorted({round(g.level_size * 2) / 2 for g in groups if g.type == BlockType.heading}, reverse=True)
    levels: dict[float, int] = {}
    for i, s in enumerate(sizes):
        levels[s] = min(6, i + 1)
    return levels


def _title_from_metadata(pdf: object) -> str | None:
    try:
        meta = pdf.get_metadata_dict()  # type: ignore[attr-defined]
    except Exception:
        return None
    return clean_title(meta.get("Title"))


def extract_pdf(data: bytes, *, filename: str | None = None, title: str | None = None) -> Document:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise EngineError("invalid_request", "That PDF couldn't be opened. It may be damaged or encrypted.") from e
    try:
        pages = [_read_page(pdf, i) for i in range(len(pdf))]
        meta_title = _title_from_metadata(pdf)
    finally:
        pdf.close()
    return _document_from_pages(data, pages, filename=filename, title=title, meta_title=meta_title)


def _document_from_pages(
    data: bytes,
    pages: list[PageInfo],
    *,
    filename: str | None,
    title: str | None,
    meta_title: str | None,
) -> Document:
    scanned = [
        pg.number
        for pg in pages
        if pg.n_chars < 20 and sum((x1 - x0) * (b - t) for x0, t, x1, b in pg.images) >= 0.5 * pg.width * pg.height
    ]
    _classify_bands(pages)
    body = _body_size(pages)
    refs = _sup_markers(pages)
    foot_markers = _classify_footnotes(pages, body, refs)
    _detect_tables(data, pages)

    # Group every page first so heading levels can be ranked document-wide.
    per_page: list[tuple[PageInfo, list[tuple[_Group, bool, bool]]]] = []  # (group, first_in_flow, last_in_flow)
    all_groups: list[_Group] = []
    for pg in pages:
        items: list[tuple[_Group, bool, bool]] = []
        for flow in _flows(pg.lines):
            center = (min(ln.x0 for ln in flow) + max(ln.x1 for ln in flow)) / 2
            gs = _group(flow, body)
            for i, g in enumerate(gs):
                tg = _type_group(g, body, center, pg.graphics)
                items.append((tg, i == 0, i == len(gs) - 1))
                all_groups.append(tg)
        for f in _footnote_groups([ln for ln in pg.lines if ln.kind == "footnote"], foot_markers):
            items.append((_Group(f, BlockType.footnote), True, True))
        per_page.append((pg, items))
    levels = _heading_levels(all_groups, body)

    drafts: list[DraftBlock] = []
    sup_spans: list[tuple[DraftBlock, list[tuple[int, int, str]]]] = []
    in_refs: int | None = None
    carry: tuple[DraftBlock, list[tuple[int, int, str]]] | None = None  # paragraph that may continue
    for pg, items in per_page:
        header_drafts = []
        for kind in ("header",):
            for ln in sorted((ln for ln in pg.lines if ln.kind == kind), key=lambda ln: (ln.top, ln.x0)):
                header_drafts.append(_line_draft(ln, BlockType.header))
        drafts.extend(header_drafts)
        page_body: list[DraftBlock] = []
        for g, first_in_flow, last_in_flow in items:
            if g.type == BlockType.table:
                ln = g.lines[0]
                bb = BBox(page=pg.number, x0=ln.x0, y0=ln.top, x1=ln.x1, y1=ln.bottom)
                d = DraftBlock(type=BlockType.table, text=ln.table_text, page=pg.number, bbox=bb)
                page_body.append(d)
                carry = None
                continue
            text, boxes, sups, bbox = _assemble(g)
            if not text.strip():
                continue
            btype = g.type
            level = None
            if btype == BlockType.heading:
                level = levels.get(round(g.level_size * 2) / 2, 3)
                if _REFS_HEADING.match(text.strip()):
                    in_refs = level
                elif in_refs is not None and level <= in_refs:
                    in_refs = None
            elif in_refs is not None and btype in (BlockType.paragraph, BlockType.list_item):
                btype = BlockType.reference_entry
            # Merge a paragraph that continues from the previous column or page.
            if (
                carry is not None
                and first_in_flow
                and btype in _CONTINUABLE
                and carry[0].type == btype
                and not _TERMINAL.search(carry[0].text)
                and text[:1].islower()
            ):
                _append(carry[0], carry[1], text, boxes, sups)
                carry = carry if last_in_flow else None
                continue
            d = DraftBlock(type=btype, text=text, level=level, page=pg.number, bbox=bbox, word_boxes=boxes)
            page_body.append(d)
            sup_spans.append((d, sups))
            if btype != BlockType.footnote:  # footnotes sit between a paragraph and its continuation
                carry = (d, sups) if (last_in_flow and btype in _CONTINUABLE) else None
        # Figures (raster images) go before the caption next to them, else after the page's text.
        for k, (x0, top, x1, bottom) in enumerate(pg.images):
            fig = DraftBlock(
                type=BlockType.figure,
                text="",
                page=pg.number,
                bbox=BBox(page=pg.number, x0=x0, y0=top, x1=x1, y1=bottom),
                image_id=f"img_p{pg.number}_{k}",
            )
            idx = next(
                (
                    i
                    for i, d in enumerate(page_body)
                    if d.type == BlockType.caption
                    and d.bbox is not None
                    and _near_graphic((d.bbox.x0, d.bbox.y0, d.bbox.x1, d.bbox.y1), [(x0, top, x1, bottom)])
                ),
                len(page_body),
            )
            page_body.insert(idx, fig)
        drafts.extend(page_body)
        for kind, btype in (("footer", BlockType.footer), ("page_number", BlockType.page_number)):
            for ln in sorted((ln for ln in pg.lines if ln.kind == kind), key=lambda ln: (ln.top, ln.x0)):
                drafts.append(_line_draft(ln, btype))
        # Footnote blocks are emitted by items (type footnote) inside page_body.

    for d, sups in sup_spans:
        for s, e, t in sups:
            t = t.strip()
            if t in foot_markers:
                d.marks.append((MarkKind.footnote_ref, s, e))
            elif re.fullmatch(r"\d{1,3}(?:[,–-]\d{1,3})*", t) and d.type != BlockType.footnote:
                d.marks.append((MarkKind.citation_ref, s, e))

    blocks = finalize_blocks(drafts, footnote_markers=foot_markers)
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0] if filename else None
    explicit = title or meta_title
    if not explicit:
        first_page = [b for b in blocks if b.page == 1 and b.type == BlockType.heading]
        if first_page:
            explicit = normalize_space(min(first_page, key=lambda b: b.level or 9).text)
    flags = [f"scanned_pages:{','.join(str(p) for p in scanned)}"] if scanned else []
    return build_document(
        source_id=source_id_bytes(data),
        kind=SourceKind.pdf,
        blocks=blocks,
        title=infer_title(blocks, explicit=explicit, fallback=stem),
        page_count=len(pages),
        flags=flags,
    )


def _line_draft(ln: PLine, btype: BlockType) -> DraftBlock:
    text, boxes, _sups, bbox = _assemble(_Group([ln], btype))
    return DraftBlock(type=btype, text=text, page=ln.page, bbox=bbox, word_boxes=boxes)


def _append(
    target: DraftBlock,
    target_sups: list[tuple[int, int, str]],
    text: str,
    boxes: list[WordBox],
    sups: list[tuple[int, int, str]],
) -> None:
    """Append a continuation (next column/page) to a paragraph, re-joining a trailing hyphen."""
    base = target.text
    if base.endswith("-") and len(base) > 2 and base[-2].isalpha() and text[:1].islower():
        base = base[:-1]
        sep = ""
        if target.word_boxes:
            last = target.word_boxes[-1]
            target.word_boxes[-1] = last.model_copy(update={"end": min(last.end, len(base))})
    else:
        sep = " "
    offset = len(base) + len(sep)
    target.text = base + sep + text
    target.word_boxes.extend(WordBox(start=b.start + offset, end=b.end + offset, bbox=b.bbox) for b in boxes)
    target_sups.extend((s + offset, e + offset, t) for s, e, t in sups)
