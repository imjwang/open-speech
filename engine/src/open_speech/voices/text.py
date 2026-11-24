"""Comparing what was said with what should have been said (word error rate), and finding which part of a passage a
recording covers.

Words are compared case- and accent-insensitively without punctuation; digits are left out on both sides (the
speech recognizer writes "9" for "nine", the passages spell numbers out or avoid them).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_TOKEN = re.compile(r"\S+")
_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "cero", "uno", "una", "dos",
    "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
}  # fmt: skip


def norm_words(text: str) -> list[str]:
    """"It's GRAY, isn't it?" → ["its", "gray", "isnt", "it"]."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("'", "").replace("’", "").replace("-", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return [w for w in t.split() if not w.isdigit() and w not in _NUMBER_WORDS]


def edit_distance(ref: list[str], hyp: list[str]) -> int:
    prev = list(range(len(hyp) + 1))
    for i in range(1, len(ref) + 1):
        cur = [i] + [0] * len(hyp)
        for j in range(1, len(hyp) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ref[i - 1] != hyp[j - 1]))
        prev = cur
    return prev[len(hyp)]


def wer(ref: str, hyp: str) -> float:
    """Word error rate of `hyp` against `ref` (0 = identical; can exceed 1 with many insertions)."""
    r, h = norm_words(ref), norm_words(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return edit_distance(r, h) / len(r)


@dataclass(frozen=True)
class Span:
    text: str  # the passage's own words (with its punctuation) that were said
    wer: float  # errors per word of `text`
    words: int  # words in `text`
    coverage: float  # share of the passage's words in `text`


def best_span(passage: str, said: str) -> Span:
    """The stretch of `passage` that `said` matches best (free start and end in the passage), as the passage writes
    it. A recording cut out of a longer reading covers only part of the passage; its reference text must be exactly
    that part."""
    tokens = list(_TOKEN.finditer(passage))
    flat: list[tuple[str, int]] = [(w, k) for k, tok in enumerate(tokens) for w in norm_words(tok.group())]
    hyp = norm_words(said)
    n, m = len(flat), len(hyp)
    if n == 0 or m == 0:
        return Span(text="", wer=1.0, words=0, coverage=0.0)
    # dist[i][j]: hyp[:j] aligned to passage words ending at i, starting anywhere; start[i][j]: where it started.
    dist = [[0] * (m + 1) for _ in range(n + 1)]
    start = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        start[i][0] = i
    for j in range(1, m + 1):
        dist[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sub = dist[i - 1][j - 1] + (flat[i - 1][0] != hyp[j - 1])
            dele = dist[i - 1][j] + 1  # a passage word that wasn't said
            ins = dist[i][j - 1] + 1  # a word said that isn't in the passage
            best = min(sub, dele, ins)
            dist[i][j] = best
            start[i][j] = start[i - 1][j - 1] if best == sub else start[i - 1][j] if best == dele else start[i][j - 1]
    # The end with the fewest errors per passage word (ties: the longer span).
    end, best_rate = n, float("inf")
    for i in range(1, n + 1):
        s = start[i][m]
        length = max(1, i - s)
        rate = dist[i][m] / length
        if rate < best_rate - 1e-9 or (abs(rate - best_rate) < 1e-9 and i - s > end - start[end][m]):
            end, best_rate = i, rate
    s = start[end][m]
    if end <= s:
        return Span(text="", wer=1.0, words=0, coverage=0.0)
    first_tok, last_tok = flat[s][1], flat[end - 1][1]
    text = passage[tokens[first_tok].start() : tokens[last_tok].end()]
    return Span(text=text, wer=dist[end][m] / (end - s), words=end - s, coverage=(end - s) / n)


__all__ = ["Span", "best_span", "edit_distance", "norm_words", "wer"]
