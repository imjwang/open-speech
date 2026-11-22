"""Segmentation (05 § 3): sentences and words with stable char offsets into a text.

Offsets are Python string indices into the text passed in, and every span slices back to the original
text exactly (`text[start:end]`), with surrounding whitespace trimmed. They are the backbone of
highlighting, chunking and span ops.

- `sentences` uses pySBD (rule-based, abbreviation-aware: "et al.", "e.g.", "Dr.", decimals) and then
  re-locates each segment in the original text, so pySBD's own (occasionally wrong) char spans are never
  trusted. Long texts are windowed first because pySBD is super-linear on long inputs. It takes an
  optional `language`: English rules by default; "es" uses pySBD's Spanish rules plus a pass that keeps
  Spanish abbreviations with what follows ("EE.UU. tiene", "s. XVI", "n.º 5", "p. ej.").
- `words` follows Unicode word-boundary rules closely enough for timing and highlighting: letters, digits
  and combining marks form words; apostrophes join letters ("don't"); "." and "," join digits ("3.14",
  "1,000"); hyphens and other punctuation separate words (UAX #29).
- `syllables` is a heuristic counter used to weight estimated word timings.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from functools import lru_cache

import pysbd

__all__ = ["sentences", "syllables", "words"]

_WINDOW = 2000  # chars per pySBD call; pySBD slows down sharply on long inputs
_local = threading.local()

# A conservative sentence-boundary candidate used only to cut long texts into windows.
_WINDOW_CUT = re.compile(r"[.!?][\"'”’)\]]*\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_WINDOW_CUT_ES = re.compile(r"[.!?…][\"'”’»)\]]*\s+(?=[\"'“‘«(\[¿¡—]*[A-ZÁÉÍÓÚÑÜ0-9])")
_HAS_LETTER = re.compile(r"[^\W\d_]")

# Spanish abbreviations pySBD's Spanish rules can leave at the end of a "sentence".
# Never sentence-final: a name, a number or a numeral always follows.
_ES_GLUE = re.compile(
    r"sr|sra|srta|sres|sras|dr|dra|dres|dras|dña|prof|profa|lic|ing|gral|mons|sto|sta|excmo|ilmo|avda|av"
    r"|pág|págs|núm|núms|art|arts|cap|caps|vol|vols|tel|tlf|vs|pp"
)
# Sentence-final or not: kept with what follows only when that starts in lowercase or with a digit.
_ES_GLUE_SOFT = re.compile(r"ud|uds|vd|vds|etc|aprox|ej")
_ES_LAST_TOKEN = re.compile(r"([^\W\d_]+)\.$")
_ES_ERA = re.compile(r"\b[ad]\.\s?C\.$")  # 300 a. C.
_ES_ROMAN = re.compile(r"(?:[IVXLCDM]+|[ivxlcdm]+)\b")
_ES_NO_CUT = re.compile(r"(?:\b[^\W\d_]|\bEE)\.$")  # an initial or "EE." before the period
_ES_OPENERS = "«\"“‘'¿¡(—"


def _language(language: str | None) -> str:
    """The segmentation rules for a language tag: "es", "es-MX" → "es"; anything else → "en"."""
    base = (language or "").replace("_", "-").split("-")[0].lower()
    return "es" if base == "es" else "en"


def _segmenter(lang: str = "en") -> pysbd.Segmenter:
    segs: dict[str, pysbd.Segmenter] | None = getattr(_local, "segs", None)
    if segs is None:
        segs = _local.segs = {}
    seg = segs.get(lang)
    if seg is None:
        seg = segs[lang] = pysbd.Segmenter(language=lang, clean=False)
    return seg


def _es_continues(prev: str, nxt: str) -> bool:
    """True when `prev` ends in a Spanish abbreviation whose period does not end the sentence before `nxt`."""
    if nxt[:1] in ("º", "ª"):  # n.º 5, D.ª Inés, 2.º piso
        return True
    m = _ES_LAST_TOKEN.search(prev)
    if m is None:
        return False
    tok = m.group(1)
    first = nxt.lstrip(_ES_OPENERS)[:1]
    soft = first.islower() or first.isdigit()
    if tok == "UU":  # EE.UU., EE. UU.
        return soft
    if tok == "EE":
        return nxt.startswith("UU")
    if tok == "C":
        return soft and _ES_ERA.search(prev) is not None
    if not (tok.islower() or tok.istitle()):
        return False  # "PP.", "SS.": acronyms, not "páginas"
    if len(tok) == 1:  # lowercase only: capitals are initials, which pySBD handles
        if tok == "p":  # p. 34, p. ej.
            return first.isdigit() or nxt.startswith("ej")
        if tok == "s":  # s. XVI
            return _ES_ROMAN.match(nxt) is not None
        return tok in "ad" and nxt.startswith("C.")
    low = tok.lower()
    return _ES_GLUE.fullmatch(low) is not None or (soft and _ES_GLUE_SOFT.fullmatch(low) is not None)


def _windows(text: str, lang: str = "en") -> list[tuple[int, int]]:
    """Cut `text` into windows of about `_WINDOW` chars at likely sentence boundaries."""
    n = len(text)
    if n <= _WINDOW * 1.5:
        return [(0, n)]
    out: list[tuple[int, int]] = []
    start = 0
    cutter = _WINDOW_CUT_ES if lang == "es" else _WINDOW_CUT
    while n - start > _WINDOW * 1.5:
        limit = start + _WINDOW
        cut = -1
        for m in cutter.finditer(text, start + _WINDOW // 2, min(n, limit + _WINDOW // 2)):
            before, after = text[max(0, m.start() - 16) : m.start() + 1], text[m.end() : m.end() + 8]
            if lang == "es" and (_es_continues(before, after) or _ES_NO_CUT.search(before)):
                continue  # "… el Sr. García", "D. Juan", "EE. UU.": not a place to cut a window
            cut = m.end()
            if cut >= limit:
                break
        if cut <= start:
            # No sentence boundary nearby: fall back to a newline or a space.
            nl = text.rfind("\n", start + _WINDOW // 2, limit)
            sp = text.rfind(" ", start + _WINDOW // 2, limit)
            cut = (nl if nl > 0 else sp) + 1 if max(nl, sp) > 0 else limit
        out.append((start, cut))
        start = cut
    out.append((start, n))
    return out


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


@lru_cache(maxsize=4096)
def _sentences_cached(text: str, lang: str = "en") -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    seg = _segmenter(lang)
    for ws, we in _windows(text, lang):
        chunk = text[ws:we]
        if not chunk.strip():
            continue
        try:
            parts = seg.segment(chunk)
        except Exception:  # pySBD can raise on pathological input; degrade to one span
            parts = [chunk]
        cursor = 0
        for part in parts:
            if not part:
                continue
            idx = chunk.find(part, cursor)
            if idx < 0:
                stripped = part.strip()
                idx = chunk.find(stripped, cursor) if stripped else -1
                if idx < 0:
                    continue  # pySBD altered the text; the gap filler below recovers it
                part = stripped
            s, e = _trim(chunk, idx, idx + len(part))
            cursor = idx + len(part)
            if e > s:
                spans.append((ws + s, ws + e))

    # Fill gaps: any non-whitespace text not covered by a span joins its neighbor (or becomes one).
    filled: list[tuple[int, int]] = []
    pos = 0
    for s, e in spans:
        if s < pos:  # overlap (shouldn't happen) — clip
            s = pos
            if s >= e:
                continue
        gap_s, gap_e = _trim(text, pos, s)
        if gap_e > gap_s:
            if filled:
                filled[-1] = (filled[-1][0], gap_e)
            else:
                s = gap_s
        filled.append((s, e))
        pos = e
    tail_s, tail_e = _trim(text, pos, len(text))
    if tail_e > tail_s:
        if filled:
            filled[-1] = (filled[-1][0], tail_e)
        else:
            filled.append((tail_s, tail_e))

    # Merge fragments with no letters ("[12].", "2.") into the previous sentence.
    merged: list[tuple[int, int]] = []
    for s, e in filled:
        frag = text[s:e]
        joins = len(frag) <= 12 and not _HAS_LETTER.search(frag)
        if merged and not joins and lang == "es":
            joins = _es_continues(text[merged[-1][0] : merged[-1][1]], frag)
        if merged and joins:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return tuple(merged)


def sentences(text: str, language: str | None = None) -> list[tuple[int, int]]:
    """Sentence spans `(start, end)` into `text`, whitespace-trimmed, in order, non-overlapping.

    Every non-whitespace character of `text` belongs to exactly one span. `language` is a language tag
    ("es", "es-MX", "en-GB"); None and languages without their own rules use the English ones.
    """
    if not text or not text.strip():
        return []
    return list(_sentences_cached(text, _language(language)))


# ------------------------------------------------------------------ words


def _is_word_char(c: str) -> bool:
    if c == "_":
        return True
    cat = unicodedata.category(c)
    return cat[0] in "LNM"


def _is_letter(c: str) -> bool:
    return unicodedata.category(c)[0] in "LM"


_MID_LETTER = frozenset("'’")
_MID_NUM = frozenset(".,")


def words(text: str) -> list[tuple[int, int]]:
    """Word spans `(start, end)` into `text` by Unicode word boundaries (UAX #29, simplified)."""
    out: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        if not _is_word_char(text[i]):
            i += 1
            continue
        j = i + 1
        while j < n:
            c = text[j]
            if _is_word_char(c):
                j += 1
                continue
            if j + 1 < n:
                nxt = text[j + 1]
                if c in _MID_LETTER and _is_letter(text[j - 1]) and _is_letter(nxt):
                    j += 2
                    continue
                if c in _MID_NUM and text[j - 1].isdigit() and nxt.isdigit():
                    j += 2
                    continue
            break
        out.append((i, j))
        i = j
    return out


# ------------------------------------------------------------------ syllables

_VOWEL_GROUPS = re.compile(r"[aeiouy]+")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
_LETTER_NAMES_3 = frozenset("w")  # "double-u"


def syllables(word: str) -> int:
    """Approximate spoken syllables in one word (≥ 1 for any word with letters or digits)."""
    if not word:
        return 0
    cjk = len(_CJK.findall(word))
    if cjk:
        return cjk
    digits = sum(ch.isdigit() for ch in word)
    folded = "".join(ch for ch in unicodedata.normalize("NFKD", word) if not unicodedata.combining(ch))
    letters = re.sub(r"[^A-Za-z]", "", folded)
    count = 0
    if digits:
        # "2020" → "twenty twenty" (4); "5" → 1; "100" → "one hundred" (3).
        count += max(1, round(digits * 1.3))
    if not letters:
        if count:
            return count
        return 1 if any(_is_word_char(ch) for ch in word) else 0
    # Acronyms without vowels are spelled out: "SQL" → ess-cue-ell.
    if letters.isupper() and len(letters) <= 5 and not re.search(r"[AEIOUY]", letters):
        return count + sum(3 if ch.lower() in _LETTER_NAMES_3 else 1 for ch in letters)
    w = letters.lower()
    groups = len(_VOWEL_GROUPS.findall(w))
    accented_e = word.lower().endswith(("é", "è", "ê", "ë"))
    if len(w) > 2 and w.endswith("e") and not w.endswith(("le", "ee", "ye")) and groups > 1 and not accented_e:
        groups -= 1  # silent final e: "make", "time"
    elif len(w) > 2 and w.endswith("le") and w[-3] not in "aeiouy":
        pass  # "table": "ble" already counted by the final e group
    if len(w) > 3 and w.endswith("ed") and w[-3] not in "td" and groups > 1:
        groups -= 1  # "walked", "played"
    return count + max(1, groups)
