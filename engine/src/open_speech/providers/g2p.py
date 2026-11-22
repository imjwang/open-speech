"""English grapheme-to-phoneme for Kokoro WITHOUT espeak (08 § Model manager, license note; NFR-10).

Kokoro v1.0 was trained on misaki's phoneme set. The default `LexiconPhonemizer` uses misaki's
Apache-2.0 gold/silver pronunciation dictionaries (shipped inside the `misaki` wheel; the base package
pulls only `addict` and `regex` — no spaCy, no espeak-ng/phonemizer, which are GPL) and falls back to
small letter-to-sound rules for out-of-vocabulary words, numbers and acronyms. Heteronyms use the
dictionary's DEFAULT reading (no POS tagger).

The phonemizer is injectable (`KokoroTts(phonemizer=...)`): anything with `phonemize(text) ->
list[PhonemeToken]` works, e.g. a full misaki `en.G2P` or an espeak-backed extra the user installs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any, Protocol

from open_speech.playback.timing import WORD_RE

PUNCT = ';:,.!?—…"()“”'
_TOKEN_RE = re.compile(rf"{WORD_RE.pattern}|[{re.escape(PUNCT)}]")


@dataclass
class PhonemeToken:
    start: int  # spoken-text offsets of the source token
    end: int
    phonemes: str
    is_word: bool
    whitespace: bool  # followed by whitespace in the text


class Phonemizer(Protocol):
    def phonemize(self, text: str) -> list[PhonemeToken]: ...


# ----------------------------------------------------------------------------------- lexicons


@cache
def _misaki_dicts(variant: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """misaki's gold and silver dictionaries, or empty dicts when misaki isn't installed."""
    try:
        from importlib.resources import files

        base = files("misaki").joinpath("data")
        gold = json.loads(base.joinpath(f"{variant}_gold.json").read_text("utf-8"))
        silver = json.loads(base.joinpath(f"{variant}_silver.json").read_text("utf-8"))
        return gold, silver
    except Exception:
        return {}, {}


def misaki_available() -> bool:
    return bool(_misaki_dicts("us")[0])


LETTERS: dict[str, str] = {
    "a": "ˈA", "b": "bˈi", "c": "sˈi", "d": "dˈi", "e": "ˈi", "f": "ˈɛf", "g": "ʤˈi", "h": "ˈAʧ",
    "i": "ˈI", "j": "ʤˈA", "k": "kˈA", "l": "ˈɛl", "m": "ˈɛm", "n": "ˈɛn", "o": "ˈO", "p": "pˈi",
    "q": "kjˈu", "r": "ˈɑɹ", "s": "ˈɛs", "t": "tˈi", "u": "jˈu", "v": "vˈi", "w": "dˈʌbᵊljˌu",
    "x": "ˈɛks", "y": "wˈI", "z": "zˈi",
}  # fmt: skip

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]  # fmt: skip
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> list[str]:
    if n < 0:
        return ["minus", *number_words(-n)]
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_ONES[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_ONES[n // 100], "hundred"] + (number_words(n % 100) if n % 100 else [])
    for size, name in ((10**9, "billion"), (10**6, "million"), (1000, "thousand")):
        if n >= size:
            return number_words(n // size) + [name] + (number_words(n % size) if n % size else [])
    return [str(n)]


def year_words(n: int) -> list[str]:
    """2026 → twenty twenty-six; 1905 → nineteen oh five; 2000 → two thousand."""
    hi, lo = divmod(n, 100)
    if n % 1000 == 0 or (2000 <= n <= 2009):
        return number_words(n)
    if lo == 0:
        return number_words(hi) + ["hundred"]
    return number_words(hi) + (["oh"] if lo < 10 else []) + number_words(lo)


# --------------------------------------------------------------------------- letter-to-sound

_RULES: list[tuple[str, str]] = sorted(
    [
        ("tion", "ʃən"), ("sion", "ʒən"), ("ture", "ʧəɹ"), ("ough", "ʌf"), ("augh", "ɔ"), ("eigh", "A"),
        ("igh", "I"), ("tch", "ʧ"), ("dge", "ʤ"), ("ph", "f"), ("sh", "ʃ"), ("ch", "ʧ"), ("th", "θ"),
        ("ng", "ŋ"), ("ck", "k"), ("qu", "kw"), ("wh", "w"), ("wr", "ɹ"), ("kn", "n"), ("ee", "i"),
        ("ea", "i"), ("oo", "u"), ("ou", "W"), ("ow", "O"), ("oi", "Y"), ("oy", "Y"), ("ai", "A"),
        ("ay", "A"), ("au", "ɔ"), ("aw", "ɔ"), ("ie", "i"), ("ue", "u"), ("er", "əɹ"), ("ir", "ɜɹ"),
        ("ur", "ɜɹ"), ("ar", "ɑɹ"), ("or", "ɔɹ"), ("a", "æ"), ("e", "ɛ"), ("i", "ɪ"), ("o", "ɑ"),
        ("u", "ʌ"), ("j", "ʤ"), ("x", "ks"), ("r", "ɹ"), ("q", "k"), ("c", "k"), ("y", "ɪ"),
        ("b", "b"), ("d", "d"), ("f", "f"), ("g", "g"), ("h", "h"), ("k", "k"), ("l", "l"), ("m", "m"),
        ("n", "n"), ("p", "p"), ("s", "s"), ("t", "t"), ("v", "v"), ("w", "w"), ("z", "z"),
    ],
    key=lambda r: -len(r[0]),
)  # fmt: skip
_LONG = {"a": "A", "e": "i", "i": "I", "o": "O", "u": "u"}
_VOWEL_PHONES = set("æɑəɛɪiʊuʌɔAIOWYɜ")


def letter_to_sound(word: str) -> str:
    w = word.lower()
    if not w.isalpha():
        return ""
    out: list[str] = []
    i = 0
    # magic e: V C e at the end → long vowel, silent e
    magic = len(w) >= 3 and w[-1] == "e" and w[-2] not in "aeiouy" and w[-3] in _LONG
    body = w[:-1] if magic else w
    while i < len(body):
        ch = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if magic and i == len(body) - 2 and ch in _LONG:
            out.append(_LONG[ch])
            i += 1
            continue
        if ch == "c" and nxt in ("e", "i", "y"):
            out.append("s")
            i += 1
            continue
        if ch == "g" and nxt in ("e", "i", "y"):
            out.append("ʤ")
            i += 1
            continue
        if ch == "y":
            out.append("j" if i == 0 else ("i" if i == len(body) - 1 else "ɪ"))
            i += 1
            continue
        for g, p in _RULES:
            if body.startswith(g, i):
                out.append(p)
                i += len(g)
                break
        else:
            i += 1
    phones = "".join(out)
    # primary stress before the first vowel
    for j, c in enumerate(phones):
        if c in _VOWEL_PHONES:
            return phones[:j] + "ˈ" + phones[j:]
    return phones


# ------------------------------------------------------------------------------ phonemizer


class LexiconPhonemizer:
    """Dictionary-first English G2P (misaki gold → silver → rules). No espeak."""

    def __init__(
        self,
        variant: str = "us",
        *,
        lexicon: Mapping[str, Any] | None = None,
        silver: Mapping[str, Any] | None = None,
    ) -> None:
        if lexicon is None:
            gold, silv = _misaki_dicts(variant)
            lexicon, silver = gold, silv if silver is None else silver
        self._gold: Mapping[str, Any] = lexicon
        self._silver: Mapping[str, Any] = silver or {}

    def _lookup(self, word: str) -> str | None:
        for cand in (word, word.lower(), word.capitalize()):
            for d in (self._gold, self._silver):
                v = d.get(cand)
                if isinstance(v, dict):
                    v = v.get("DEFAULT") or next((x for x in v.values() if isinstance(x, str)), None)
                if isinstance(v, str) and v:
                    return v
        return None

    def word(self, word: str) -> str:
        hit = self._lookup(word)
        if hit:
            return hit
        clean = word.replace("’", "'")
        if clean.lower().endswith("'s") and len(clean) > 2:
            base = self.word(clean[:-2])
            return base + ("s" if base and base[-1] in "ptkfθ" else "z")
        if "-" in clean:
            return " ".join(self.word(p) for p in clean.split("-") if p)
        digits = clean.replace(",", "")
        if digits.isdigit():
            n = int(digits)
            words = year_words(n) if len(digits) == 4 and 1100 <= n <= 2099 else number_words(n)
            return " ".join(self.word(w) for w in words)
        if re.fullmatch(r"\d+\.\d+", digits):
            whole, frac = digits.split(".")
            parts = [*number_words(int(whole)), "point", *(_ONES[int(d)] for d in frac)]
            return " ".join(self.word(w) for w in parts)
        if clean.isupper() and len(clean) <= 5:
            return "".join(LETTERS.get(c.lower(), "") for c in clean)
        lts = letter_to_sound(clean)
        if lts:
            return lts
        return "".join(LETTERS.get(c.lower(), "") for c in clean if c.isalpha())

    def phonemize(self, text: str) -> list[PhonemeToken]:
        out: list[PhonemeToken] = []
        for m in _TOKEN_RE.finditer(text):
            s, e = m.span()
            tok = m.group(0)
            ws = e < len(text) and text[e].isspace()
            if len(tok) == 1 and tok in PUNCT:
                out.append(PhonemeToken(s, e, tok, is_word=False, whitespace=ws))
            else:
                out.append(PhonemeToken(s, e, self.word(tok), is_word=True, whitespace=ws))
        return out
