"""Replies made for the ear, cut into pieces a voice can start on early (07 § Reply speech).

`speakable(text, language)`: what a reply written for the screen sounds like. Markdown is dropped (emphasis,
code marks, headings, bullets, table pipes), a markdown link reads as its text, a bare URL as "link" ("enlace"
in Spanish), emoji are left out, and a line that ends without punctuation (a list item, a heading) gets a
full stop so the voice pauses there.

`Splitter`: the reply as it streams in, cut into units that are synthesized one by one while the rest is
still being written:
- Sentences come from pySBD (`document.segment.sentences`, per language, abbreviation-aware). A boundary
  counts only once a complete word has arrived after it, so "Dr.", "3.5", "e.g.", "Sra." and "p. ej." are
  never cut while the next word is still on its way. A line break always ends a sentence. Links still
  arriving are never cut into.
- A sentence shorter than 20 characters is merged with the next. The first one waits at most 300 ms for
  that: then it goes alone, so a reply starts quickly.
- The first unit is cut at a clause (", ", "; ", ": ", " — "), the first one at least 40 characters long, when
  no sentence has ended within 120 characters; no unit is longer than 300 characters.
- When the text stops arriving (the model is running a tool), what is complete is said after 300 ms, and
  anything else after a second. `finish()` says the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from open_speech.document.segment import sentences

SHORT_CHARS = 20  # shorter sentences are merged with the next
FIRST_WAIT_S = 0.3  # how long a short first sentence waits for the next one
FIRST_CLAUSE_AT = 120  # the first unit is cut at a clause when no sentence has ended by then
MIN_CLAUSE_CHARS = 40  # ...at its first clause at least this long ("Well, " is not one)
MAX_CHARS = 300  # no unit is longer
IDLE_DONE_S = 0.3  # text stopped arriving: what ends like a sentence is one
IDLE_ANY_S = 1.0  # stopped for this long: whatever is waiting is said

LINK_WORD = {"en": "link", "es": "enlace"}

_URL = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'“”‘’«»]+", re.I)
_URL_TRAIL = ".,;:!?…'\"”’)]}»"
_URL_ARRIVING = re.compile(r"(?:https?://|www\.)\S*$|\bhttps?:?/?/?$", re.I)
_MD_LINK_ARRIVING = re.compile(r"!?\[[^\]\n]{0,200}(?:\]\(?[^)\s]*)?$")
_MD_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)\s]*\)")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((?:[^()\s]|\([^()\s]*\))*\)")
_FENCE = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])([*_])(?=\S)(.+?)(?<=\S)\1(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_QUOTE = re.compile(r"^\s*(?:>\s?)+")
_BULLET = re.compile(r"^\s*(?:[-*+•▪◦‣]|\d{1,3}[.)])\s+")
_RULE_LINE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff⭐⭕⬛⬜‍️⃣]"
)
_TERMINAL = re.compile(r"[.!?…:;][\"'”’»)\]]*$")
_ENDS_SENTENCE = re.compile(r"[.!?…][\"'”’»)\]]*$")
_WORD_AFTER = re.compile(r"\S\s")
_CLAUSE = re.compile(r"[,;:](?=\s)|\s[—–-](?=\s)")


def _lang(language: str | None) -> str:
    return "es" if (language or "").lower().startswith("es") else "en"


def _url_spans(text: str) -> list[tuple[int, int]]:
    """Where the links are, without the punctuation that ends the sentence around them."""
    out: list[tuple[int, int]] = []
    for m in _URL.finditer(text):
        end = m.end()
        while end > m.start() and text[end - 1] in _URL_TRAIL:
            end -= 1
        if end > m.start():
            out.append((m.start(), end))
    return out


def _line(line: str) -> str:
    if _RULE_LINE.match(line) or _TABLE_SEP.match(line):
        return ""
    line = _HEADING.sub("", line)
    line = _QUOTE.sub("", line)
    line = _BULLET.sub("", line)
    if line.count("|") >= 2:  # a table row: its cells, as a list
        line = ", ".join(c.strip() for c in line.strip().strip("|").split("|") if c.strip())
    return line.strip()


def speakable(text: str, language: str | None = "en") -> str:
    """`text` as it should be heard (see the module docstring)."""
    lang = _lang(language)
    s = text.replace("\r", "")
    s = _FENCE.sub(lambda m: m.group(1), s)
    s = _MD_IMAGE.sub(lambda m: m.group(1), s)
    s = _MD_LINK.sub(lambda m: m.group(1), s)
    spans = _url_spans(s)
    for a, b in reversed(spans):
        s = s[:a] + LINK_WORD[lang] + s[b:]
    s = _INLINE_CODE.sub(r"\1", s)
    s = _BOLD.sub(r"\2", s)
    s = _STRIKE.sub(r"\1", s)
    s = _ITALIC.sub(r"\2", s)
    lines = [ln for ln in (_line(x) for x in s.split("\n")) if ln]
    for i in range(len(lines) - 1):  # a line that ends without punctuation still ends there
        if not _TERMINAL.search(lines[i]):
            lines[i] += "."
    s = " ".join(lines)
    s = _EMOJI.sub("", s)
    s = s.replace("`", "").replace("**", "")
    s = re.sub(r"(?<!\w)#(?=\s)", "", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([.,;:!?…])", r"\1", s)
    return s.strip()


@dataclass
class _Sentence:
    text: str  # as written (cleaned when the unit is made)
    at: float  # when its end was confirmed
    line_end: bool = False  # it ended at a line break: say it with a full stop


class Splitter:
    """The reply as it streams in → units to synthesize, in order (see the module docstring). Pure: the caller
    passes the time (`loop.time()`), and calls `ready(now)` again at `deadline()` when no text has come."""

    def __init__(self, language: str | None = "en") -> None:
        self.lang = _lang(language)
        self.units = 0  # units handed out so far
        self._buf = ""  # text not yet cut into sentences
        self._sents: list[_Sentence] = []
        self._last_feed: float | None = None
        self._finished = False

    # ------------------------------------------------------------------ public

    def feed(self, text: str, now: float) -> list[str]:
        if self._finished or not text:
            return []
        self._buf += text
        self._last_feed = now
        self._cut(now)
        return self.ready(now)

    def ready(self, now: float) -> list[str]:
        """Units that can be said now."""
        if self._finished:
            return []
        force = False
        idle = None if self._last_feed is None else now - self._last_feed
        if idle is not None and idle >= IDLE_DONE_S:
            force = True  # nothing more is coming for now: say what is complete
            tail = self._buf.strip()
            if tail and (idle >= IDLE_ANY_S or _ENDS_SENTENCE.search(tail)):
                self._cut(now, final=True)
        out = self._assemble(now, force=force)
        out.extend(self._cut_long_tail())
        return out

    def flush(self, now: float, *, partial: bool = False) -> list[str]:
        """What is waiting, as units now, short sentences included, because something else is about to be said
        after it (a notice, a voice sample). `partial`: also text that doesn't end like a sentence (the model
        stopped writing to run a tool)."""
        if self._finished:
            return []
        tail = self._buf.strip()
        if tail and (partial or _ENDS_SENTENCE.search(tail)):
            self._cut(now, final=True)
        return self._assemble(now, force=True)

    def finish(self) -> list[str]:
        """Everything that is left."""
        if self._finished:
            return []
        self._cut(self._last_feed or 0.0, final=True)
        out = self._assemble(0.0, force=True)
        self._finished = True
        return out

    def pending(self) -> bool:
        """Text is waiting that hasn't been handed out as a unit yet."""
        return not self._finished and (bool(self._sents) or bool(self._buf.strip()))

    def deadline(self) -> float | None:
        """When `ready()` may hand something out even if no more text comes."""
        if self._finished or self._last_feed is None:
            return None
        cands: list[float] = []
        if self._sents and self.units == 0:
            cands.append(self._sents[0].at + FIRST_WAIT_S)
        tail = self._buf.strip()
        if self._sents or tail:
            complete = bool(self._sents) or bool(_ENDS_SENTENCE.search(tail))
            cands.append(self._last_feed + (IDLE_DONE_S if complete else IDLE_ANY_S))
            if self._sents and tail and not _ENDS_SENTENCE.search(tail):
                cands.append(self._last_feed + IDLE_ANY_S)
        return min(cands) if cands else None

    # ------------------------------------------------------------------ sentences

    def _cut(self, now: float, *, final: bool = False) -> None:
        """Move every sentence whose end is certain from the buffer to `_sents` (`final`: all of it)."""
        buf = self._buf
        work = buf if final else buf[: self._safe_end(buf)]
        masked = self._mask_links(work)
        consumed = 0
        while True:  # complete lines: every sentence in them is complete
            nl = masked.find("\n", consumed)
            if nl < 0:
                break
            spans = sentences(masked[consumed:nl], self.lang) if masked[consumed:nl].strip() else []
            for i, (a, b) in enumerate(spans):
                self._sents.append(_Sentence(work[consumed + a : consumed + b], now, line_end=i == len(spans) - 1))
            consumed = nl + 1
        tail = masked[consumed:]
        spans = sentences(tail, self.lang) if tail.strip() else []
        taken = 0
        for i in range(len(spans)):
            a, b = spans[i]
            if not final and (i == len(spans) - 1 or not _WORD_AFTER.search(tail, b)):
                break  # the last one may go on, and "Dr." + "Smith" isn't a sentence end until "Smith" is here
            self._sents.append(_Sentence(work[consumed + a : consumed + b], now))
            taken = spans[i + 1][0] if i + 1 < len(spans) else len(tail)
        self._buf = "" if final else buf[consumed + taken :]

    @staticmethod
    def _safe_end(buf: str) -> int:
        """Up to where the buffer may be cut: not into a link that is still arriving."""
        ends = [len(buf)]
        for pat in (_URL_ARRIVING, _MD_LINK_ARRIVING):
            m = pat.search(buf)
            if m:
                ends.append(m.start())
        return min(ends)

    @staticmethod
    def _mask_links(text: str) -> str:
        """Links with their punctuation hidden from the sentence splitter ("…/a.b?c=1" is not three sentences)."""
        for a, b in _url_spans(text):
            text = text[:a] + "x" * (b - a) + text[b:]
        return text

    # ------------------------------------------------------------------ units

    def _clean(self, s: _Sentence) -> str:
        c = speakable(s.text, self.lang)
        if c and s.line_end and not _TERMINAL.search(c):
            c += "."
        return c

    def _assemble(self, now: float, *, force: bool) -> list[str]:
        out: list[str] = []
        while self._sents:
            first = self.units == 0 and not out
            lengths = [len(self._clean(s)) for s in self._sents]
            n, total = 1, lengths[0]
            while total < SHORT_CHARS and n < len(self._sents):
                if first and total + 1 + lengths[n] > FIRST_CLAUSE_AT:
                    break  # a short first sentence goes alone rather than wait for a long second one
                total += 1 + lengths[n]
                n += 1
            if total < SHORT_CHARS and not force:
                # A first sentence that can't be merged without passing 120 characters goes alone at once.
                long_next = first and (
                    n < len(self._sents) or total + 1 + len(self._buf.strip()) > FIRST_CLAUSE_AT
                )
                waited = first and now - self._sents[0].at >= FIRST_WAIT_S
                if not (long_next or waited):
                    break  # wait for the next sentence to merge with
            parts = [c for c in (self._clean(s) for s in self._sents[:n]) if c]
            del self._sents[:n]
            text = " ".join(parts)
            if first and len(text) > FIRST_CLAUSE_AT:  # a long first sentence starts at its first long clause
                cut = _first_clause_cut(text, min_at=MIN_CLAUSE_CHARS, max_at=MAX_CHARS)
                if cut is not None:
                    out.append(text[:cut].strip())
                    self.units += 1
                    text = text[cut:].strip()
            for piece in _cap(text):
                out.append(piece)
                self.units += 1
        return out

    def _cut_long_tail(self) -> list[str]:
        """The first unit at a clause after 120 characters without a sentence end; any unit at 300."""
        out: list[str] = []
        while True:
            if self._sents:
                return out
            limit = FIRST_CLAUSE_AT if self.units == 0 else MAX_CHARS
            buf = self._buf
            work = buf[: self._safe_end(buf)]
            if len(work.strip()) < limit:
                return out
            if self.units == 0:  # the earliest clause that is long enough: the listener hears it sooner
                cut = _first_clause_cut(work, min_at=MIN_CLAUSE_CHARS, max_at=MAX_CHARS)
            else:
                cut = _clause_cut(work, min_at=100, max_at=MAX_CHARS)
            if cut is None and len(work.strip()) >= MAX_CHARS:
                cut = _space_cut(work, MAX_CHARS)
            if cut is None:
                return out
            piece = speakable(work[:cut], self.lang)
            self._buf = buf[cut:].lstrip()
            if piece:
                out.append(piece)
                self.units += 1


def _clause_cut(text: str, *, min_at: int, max_at: int) -> int | None:
    """The end of the last clause in `text[:max_at]` that starts at least `min_at` in and has a complete word
    after it."""
    best = None
    for m in _CLAUSE.finditer(text):
        end = m.end()
        if end > max_at:
            break
        if end >= min_at and _WORD_AFTER.search(text, end):
            best = end
    return best


def _first_clause_cut(text: str, *, min_at: int, max_at: int) -> int | None:
    """The end of the first clause in `text[:max_at]` at least `min_at` long with a complete word after it."""
    for m in _CLAUSE.finditer(text):
        end = m.end()
        if end > max_at:
            return None
        if end >= min_at and _WORD_AFTER.search(text, end):
            return end
    return None


def _space_cut(text: str, max_at: int) -> int | None:
    cut = text.rfind(" ", 0, max_at)
    return cut if cut > 0 else None


def _cap(text: str) -> list[str]:
    """`text` in pieces of at most `MAX_CHARS`, cut at clauses where possible."""
    out: list[str] = []
    while len(text) > MAX_CHARS:
        cut = _clause_cut(text, min_at=100, max_at=MAX_CHARS) or _space_cut(text, MAX_CHARS) or MAX_CHARS
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


__all__ = ["LINK_WORD", "Splitter", "speakable"]
