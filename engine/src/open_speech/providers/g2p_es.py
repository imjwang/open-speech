"""Spanish grapheme-to-phoneme for Kokoro WITHOUT espeak (08 § Model manager, license note; NFR-10).

Kokoro's Spanish voices (`ef_dora`, `em_alex`, `em_santa`) were trained on phonemes that misaki produced by
running espeak-ng (voice `es`) and applying a small replacement table. espeak-ng and `phonemizer` are GPL
and may not ship with the engine, so this module is an independent, rule-based implementation written
from Spanish phonology. Its output was validated against the outputs of that reference pipeline on a large
word and sentence corpus (frozen examples live in `tests/unit/runtime/test_g2p_es.py`); no espeak rule
file or dictionary was copied.

What the reference does, and this module reproduces:

- Castilian inventory: `θ` for z/ce/ci, `ʎ` for ll, `ʝ` for y, `x` for j/ge/gi, `ɲ` for ñ, tap `ɾ` and
  trill `r`, and the allophones `β ð ɣ` of b d g after anything but a pause or a nasal, across word
  boundaries inside a clause.
- Diphthongs as single Kokoro symbols where the reference has them: ai → `I`, au → `W`, ei → `A`;
  eu → `eʊ`, oi → `oɪ`; rising diphthongs with `j`/`w`.
- Primary stress from the written accent or the default rules, secondary stress on alternate syllables,
  and unstressed function words (articles, prepositions, clitic pronouns) inside a clause.

Text is normalised in Spanish first: numbers, decimals, ordinals, percentages, currency, units, times,
dates, Roman numerals, common abbreviations and acronyms.

Every token keeps the `[start, end)` offsets of the source text it came from, so Kokoro's per-token
durations give exact word timings (FR-READ-6).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from open_speech.providers.g2p import PUNCT, PhonemeToken

__all__ = [
    "SYMBOLS",
    "SpanishPhonemizer",
    "cardinal_words",
    "ordinal_words",
    "spoken_words",
    "word_phonemes",
]

log = logging.getLogger(__name__)


def _ws(words: str) -> frozenset[str]:
    return frozenset(words.split())


# Every symbol this module can emit (all are in Kokoro's vocabulary; checked by the tests).
SYMBOLS = frozenset("aeiouɛjwIWAʊɪptkbdɡβðɣfθsxʧʤʃʦmnɲŋlʎɾrʝːˈˌ " + PUNCT)

# ------------------------------------------------------------------------------------ numbers

_UNITS = [
    "cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce",
    "trece", "catorce", "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno",
    "veintidós", "veintitrés", "veinticuatro", "veinticinco", "veintiséis", "veintisiete", "veintiocho",
    "veintinueve",
]  # fmt: skip
_TENS = ["", "", "", "treinta", "cuarenta", "cincuenta", "sesenta", "setenta", "ochenta", "noventa"]
_HUNDREDS = [
    "", "ciento", "doscientos", "trescientos", "cuatrocientos", "quinientos", "seiscientos", "setecientos",
    "ochocientos", "novecientos",
]  # fmt: skip
_MAX_CARDINAL = 10**18


def _below_1000(n: int, fem: bool, apocope: bool) -> list[str]:
    if n == 100:
        return ["cien"]
    out: list[str] = []
    h, r = divmod(n, 100)
    if h:
        w = _HUNDREDS[h]
        out.append(w[:-2] + "as" if fem and h > 1 else w)
    if not r:
        return out
    if r < 30:
        w = _UNITS[r]
        if r == 1:
            w = "una" if fem else ("un" if apocope else "uno")
        elif r == 21:
            w = "veintiuna" if fem else ("veintiún" if apocope else "veintiuno")
        out.append(w)
        return out
    t, u = divmod(r, 10)
    out.append(_TENS[t])
    if u:
        out += ["y", ("una" if fem else "un" if apocope else "uno") if u == 1 else _UNITS[u]]
    return out


def cardinal_words(n: int, *, fem: bool = False, apocope: bool = False) -> list[str]:
    """21 → veintiuno; fem → veintiuna; apocope (before a masculine noun) → veintiún. Long scale:
    10**9 is "mil millones", 10**12 "un billón"."""
    if n < 0:
        return ["menos", *cardinal_words(-n, fem=fem, apocope=apocope)]
    if n == 0:
        return ["cero"]
    if n >= _MAX_CARDINAL:
        return [_UNITS[int(d)] for d in str(n)]
    out: list[str] = []
    billions, rest = divmod(n, 10**12)
    millions, rest = divmod(rest, 10**6)
    thousands, units = divmod(rest, 1000)
    if billions:
        out += ["un", "billón"] if billions == 1 else [*cardinal_words(billions, apocope=True), "billones"]
    if millions:
        out += ["un", "millón"] if millions == 1 else [*cardinal_words(millions, apocope=True), "millones"]
    if thousands:
        out += ["mil"] if thousands == 1 else [*_below_1000(thousands, fem, True), "mil"]
    if units:
        out += _below_1000(units, fem, apocope)
    return out


_ORD_UNITS = [
    "", "primero", "segundo", "tercero", "cuarto", "quinto", "sexto", "séptimo", "octavo", "noveno", "décimo",
    "undécimo", "duodécimo",
]  # fmt: skip
_ORD_TENS = [
    "", "décimo", "vigésimo", "trigésimo", "cuadragésimo", "quincuagésimo", "sexagésimo", "septuagésimo",
    "octogésimo", "nonagésimo",
]  # fmt: skip


def ordinal_words(n: int, *, fem: bool = False, plural: bool = False, apocope: bool = False) -> list[str]:
    """1 → primero (primer with apocope, primera fem.); 13 → decimotercero; 21 → vigésimo primero.
    Outside 1–100 the cardinal is used, as speakers do."""
    if not 1 <= n <= 100:
        return cardinal_words(n, fem=fem)
    if n == 100:
        words = ["centésimo"]
    elif n <= 12:
        words = [_ORD_UNITS[n]]
    elif n < 20:
        unit = _ORD_UNITS[n - 10]
        words = ["decimoctavo" if n == 18 else "decimo" + unit.replace("é", "e")]
    else:
        t, u = divmod(n, 10)
        words = [_ORD_TENS[t]] + ([_ORD_UNITS[u]] if u else [])

    def inflect(w: str, last: bool) -> str:
        if apocope and last and not fem and not plural and w.endswith(("primero", "tercero")):
            return w[:-1]
        if fem:
            w = w[:-1] + "a"
        return w + "s" if plural else w

    return [inflect(w, i == len(words) - 1) for i, w in enumerate(words)]


# ------------------------------------------------------------------------- letters to phonemes
#
# Internal phones are the output symbols plus:
#   B D G   b/d/g before their allophone is known: stop (b d ɡ) or fricative (β ð ɣ), decided from the
#           neighbouring sounds, possibly in the next word;
#   K X     k written "k" and x written "g" (ge, gi). The reference assimilates a preceding n to ŋ before
#           these across a word boundary ("eŋ kˈenja", "eŋ xˌeneɾˈal") but not before c/qu or j.

_ACUTE = {"á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u"}
_VOWEL_LETTERS = frozenset("aeiouáéíóúü")
_FRONT = frozenset("eiéí")
_VOWEL_PHONES = frozenset("aeiouɛIWA")
_GLIDES = frozenset("jwʊɪ")
_FOLD = {"ç": "s", "ß": "s", "æ": "e", "œ": "e", "ø": "o", "ð": "d", "þ": "t", "ł": "l", "đ": "d"}
_OUT = {"K": "k", "X": "x"}


@dataclass(slots=True)
class _V:
    letter: str  # a e i o u
    acc: bool = False  # written accent
    full: bool = False  # ü: always a syllable of its own
    y: bool = False  # written y (word-final or before a consonant)

    @property
    def weak(self) -> bool:
        return self.letter in "iu" and not self.acc and not self.full


_Unit = str | _V | None  # consonant phone | vowel letter | silent h (keeps vowels apart)


def _fold(word: str) -> str:
    """Lowercase; keep Spanish letters; reduce other diacritics to their base letter; drop the rest."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", word.lower()):
        if ch in "abcdefghijklmnopqrstuvwxyzñáéíóúü":
            out.append(ch)
        elif ch in _FOLD:
            out.append(_FOLD[ch])
        elif ch not in "ºª":  # ordinal marks are not letters
            base = unicodedata.normalize("NFKD", ch)[:1]
            if base and base in "abcdefghijklmnopqrstuvwxyz":
                out.append(base)
    return "".join(out)


def _units(w: str) -> list[_Unit]:
    """Letters → consonant phones and vowel letters (context rules for c g q r x y, digraphs, silent h)."""
    out: list[_Unit] = []
    n = len(w)

    def at(k: int) -> str:
        return w[k] if 0 <= k < n else "#"

    i = 0
    while i < n:
        ch, nxt = w[i], at(i + 1)
        step = 1
        if ch in "aeiou":
            out.append(_V(ch))
        elif ch in _ACUTE:
            out.append(_V(_ACUTE[ch], acc=True))
        elif ch == "ü":
            out.append(_V("u", full=True))
        elif ch in "bv":
            out.append("B")
        elif ch == "c":
            if nxt == "h":
                out.append("ʧ")
                step = 2
            elif nxt in _FRONT or (nxt == "y" and at(i + 2) not in _VOWEL_LETTERS):
                out.append("θ")  # also before a vocalic y: "encyclopedia"
            else:
                out.append("k")
                step = 2 if nxt == "k" else 1
        elif ch == "d":
            # "duelo", "dueño": the reference keeps a stop here even after a vowel
            out.append("d" if i == 0 and nxt == "u" and at(2) in "aeoáéó" else "D")
        elif ch == "g":
            if nxt in _FRONT or (nxt == "y" and at(i + 2) not in _VOWEL_LETTERS):
                out.append("X")
            elif nxt == "u" and at(i + 2) in _FRONT:
                out.append("G")
                step = 2
            elif i == 0 and nxt == "n":
                pass  # gnomo
            else:
                out.append("G")
        elif ch == "h":
            out.append(None)
        elif ch == "j":
            out.append("x")
        elif ch == "k":
            out.append("K")
        elif ch == "l":
            if nxt == "l":
                out.append("ʎ")
                step = 2
            else:
                out.append("l")
        elif ch == "m":
            if not (i == 0 and nxt == "n"):  # mnemotecnia
                out.append("m")
        elif ch == "n":
            if nxt == "y" and at(i + 2) in _VOWEL_LETTERS:
                out.append("ɲ")  # cónyuge, inyectar
                step = 2
            elif nxt == "g" and i + 2 == n:
                out.append("ŋ")  # camping, Bang
                step = 2
            elif nxt in ("g", "j", "k"):
                out.append("ŋ")
            elif nxt in ("b", "v", "p") or (nxt == "f" and at(i + 2) in ("l", "r")):
                out.append("m")
            else:
                out.append("n")
        elif ch == "ñ":
            out.append("ɲ")
        elif ch == "p":
            if nxt == "h":
                out.append("f")
                step = 2
            elif i == 0 and nxt in ("s", "t", "n"):
                pass  # psicología, pterodáctilo
            elif nxt == "t":
                out += ["p", "ː"]  # the reference lengthens p before t: "sepːtjˈembɾe"
            else:
                out.append("p")
        elif ch == "q":
            out.append("k")
            if nxt == "u" and at(i + 2) in _FRONT:
                step = 2
        elif ch == "r":
            if nxt == "r":
                out.append("r")
                step = 2
            elif i == 0 or at(i - 1) in ("n", "l", "s"):
                out.append("r")
            else:
                out.append("ɾ")
        elif ch == "s":
            if nxt == "h" and (i == 0 or i + 2 == n):
                out.append("ʃ")  # show, flash; medial "sh" is s + silent h (deshacer)
                step = 2
            else:
                out.append("s")
        elif ch == "t":
            out.append("t")
            if nxt == "h":
                step = 2
        elif ch == "w":
            out.append("w")
            if nxt == "h":
                step = 2
        elif ch == "x":
            if i == 0:
                out.append("s")  # xilófono
            elif i == 2 and w.startswith(("méxic", "mexic")):
                out.append("x")
            else:
                out += ["k", "s"]
        elif ch == "y":
            if nxt in _VOWEL_LETTERS:
                out.append("ʝ")
            else:
                out.append(_V("i", y=True))
        elif ch == "z":
            if nxt == "z":
                out.append("ʦ")  # pizza
                step = 2
            else:
                out.append("θ")
        elif ch == "f":
            out.append("f")
        i += step
    return out


def _onglide(letter: str, phones: list[str]) -> str:
    """i/u before a vowel: a glide, except after a trill, an initial l or a consonant + l/r cluster (and u
    after any l/r), where the reference keeps a short full vowel: "riˈesɣo", "kliˈɛnte", "luˈeɣo"."""
    glide = "j" if letter == "i" else "w"
    if not phones:
        return glide
    prev = phones[-1]
    if letter == "u":
        return "u" if prev in ("l", "ɾ", "r", "ʎ") else glide
    if prev == "r":
        return "i"
    if prev in ("l", "ɾ"):
        before = phones[-2] if len(phones) >= 2 else None
        if before is None or not (before in _VOWEL_PHONES or before in _GLIDES):
            return "i"
    return glide


@dataclass(slots=True)
class _Syl:
    """A word's phones and its vowels. `fixed` vowels are the short i/u of `_onglide`: they count for the
    rhythm of secondary stress but never carry stress themselves and are no syllable for the accent rule."""

    phones: list[str] = field(default_factory=list)
    nuclei: list[int] = field(default_factory=list)  # indices into phones, in order
    fixed: list[bool] = field(default_factory=list)
    open_e: list[bool] = field(default_factory=list)  # per nucleus: a plain "e" that may open to ɛ
    accent: int | None = None  # index into nuclei of the vowel with a written accent

    def add(self, phone: str, *, fixed: bool = False, open_e: bool = False) -> None:
        self.nuclei.append(len(self.phones))
        self.fixed.append(fixed)
        self.open_e.append(open_e)
        self.phones.append(phone)


def _nuclei(units: list[_Unit]) -> _Syl:
    s = _Syl()
    n = len(units)
    i = 0
    while i < n:
        u = units[i]
        if u is None:
            i += 1
            continue
        if isinstance(u, str):
            s.phones.append(u)
            i += 1
            continue
        v = u
        nxt = units[i + 1] if i + 1 < n else None
        if v.weak and isinstance(nxt, _V):
            if nxt.y and v.letter == "u":  # muy, cuy: u + offglide
                s.add("u")
                s.phones.append("j")
                i += 2
                continue
            if nxt.letter != v.letter or nxt.acc:  # rising diphthong; "ii"/"uu" stay two vowels
                glide = _onglide(v.letter, s.phones)
                if glide in ("i", "u"):
                    s.add(glide, fixed=True)
                else:
                    s.phones.append(glide)
                v = nxt
                i += 1
        i += 1
        if v.acc and s.accent is None:
            s.accent = len(s.nuclei)
        off = units[i] if i < n else None
        if isinstance(off, _V) and off.weak and v.letter in "aeo" and not v.acc:
            # falling diphthong; the reference has single symbols for ai, au, ei
            i += 1
            pair = v.letter + off.letter
            if pair in ("ai", "au", "ei"):
                s.add({"ai": "I", "au": "W", "ei": "A"}[pair])
            else:
                s.add(v.letter)
                s.phones.append({"eu": "ʊ", "oi": "ɪ", "ou": "w"}[pair])
        else:
            s.add(v.letter, open_e=v.letter == "e" and not v.acc)
    return s


def _default_stress(word: str, count: int) -> int:
    """Which syllable (0-based) carries the stress when there is no written accent."""
    if count <= 1:
        return 0
    last, before = word[-1], word[-2:-1]
    if last in "aeiou":
        return count - 2
    if last in "nsm" and (before in _VOWEL_LETTERS or (last == "s" and before == "n")):
        return count - 2  # the reference treats a final m like n: "ˈislam", "maksˈimum"
    if word.endswith("ng"):
        return count - 2  # camping, marketing
    if last == "y" and before not in _VOWEL_LETTERS:
        return count - 2  # whisky, hobby
    return count - 1


def _rhythm(stress: list[int]) -> None:
    """Secondary stress on alternate syllables: a vowel between two unstressed ones, never the last.
    Levels: -1 never stressed, 0 unstressed, 1 secondary, 2 primary."""
    n = len(stress)
    for v in range(n - 1):
        if stress[v] == 0 and (v == 0 or stress[v - 1] <= 0) and stress[v + 1] <= 0:
            stress[v] = 1


_Analysis = tuple[tuple[str, ...], tuple[int, ...], tuple[int, ...], tuple[int, ...]]
# open-e levels per nucleus: 0 never, 1 before n + consonant, 2 whenever it carries the primary stress
_ALWAYS_OPEN = {"méxico": 0, "zeta": 0, "efe": 0, "ele": 0, "eme": 0, "ene": 0, "eñe": 0, "erre": 0, "ese": 0,
                "equis": 0}  # fmt: skip
_MENTE_SHORT_STEMS = frozenset({"vil"})


# A few loanwords the letter rules read badly, in internal phones: B D G are the archiphonemes, never
# plain b d g (Kokoro has no "g"). The reference has entries for most of these.
_LEXICON = {
    "jazz": "ʤˈas", "software": "sˈoftweɾ", "hardware": "xˈaɾDweɾ", "google": "GˈuGel", "facebook": "fˈAsBuk",
    "iphone": "ˈIfon", "microsoft": "mˈIkɾosoft", "youtube": "ʝutˈuB", "whatsapp": "wasˈap", "online": "onlˈIn",
}  # fmt: skip


def _from_phonemes(ps: str) -> _Analysis:
    phones: list[str] = []
    nuclei: list[int] = []
    stress: list[int] = []
    primary = False
    for ch in ps:
        if ch == "ˈ":
            primary = True
            continue
        if ch in _VOWEL_PHONES:
            nuclei.append(len(phones))
            stress.append(2 if primary else 0)
            primary = False
        phones.append(ch)
    _rhythm(stress)
    return tuple(phones), tuple(nuclei), tuple(stress), (0,) * len(nuclei)


@lru_cache(maxsize=20000)
def _analyse(word: str, letter_name: bool = False) -> _Analysis:
    """(phones, nuclei, stress, open_e) for one folded, lowercase word in isolation."""
    if word == "y" and not letter_name:
        return ("i",), (0,), (0,), (0,)
    if word in _LEXICON and not letter_name:
        return _from_phonemes(_LEXICON[word])
    if word.endswith("mente") and len(word) > 6:
        # Adverbs keep the stress of the adjective and add a second one: "rˈapiðamˈente". The reference
        # does this for every stem of two or more syllables, adverb or not.
        stem = _analyse(word[:-5])
        if len(stem[1]) >= 2 or word[:-5] in _MENTE_SHORT_STEMS:
            base = len(stem[0])
            stress = [x if x in (-1, 2) else 0 for x in stem[2]] + [2, 0]
            _rhythm(stress)
            return (*stem[0], "m", "e", "n", "t", "e"), (*stem[1], base + 1, base + 4), tuple(stress), (*stem[3], 0, 0)
    s = _nuclei(_units(word))
    stress = [-1 if f else 0 for f in s.fixed]
    real = [k for k, f in enumerate(s.fixed) if not f]
    if real:
        stress[s.accent if s.accent is not None else real[_default_stress(word, len(real))]] = 2
        _rhythm(stress)
    phones = s.phones
    if word == "ser" and phones:
        phones[-1] = "r"  # the reference has a trill in this one word
    opens = [1 if ok else 0 for ok in s.open_e]
    if word in _ALWAYS_OPEN and (letter_name or word == "méxico") and opens:
        opens[_ALWAYS_OPEN[word]] = 2
    return tuple(phones), tuple(s.nuclei), tuple(stress), tuple(opens)


@dataclass(slots=True)
class _Word:
    phones: list[str]
    nuclei: list[int]
    stress: list[int]  # per vowel: -1 never, 0 none, 1 secondary, 2 primary
    open_e: list[int]
    clitic: bool = False  # unstressed inside a clause
    reduced: bool = False  # a letter of a spelled acronym other than the last: secondary stress only
    glue: bool = False  # written joined to the previous word: no space when rendered


# Words the reference leaves unstressed inside a clause (they get their stress back at the end of one).
_CLITICS = _ws("""
    el la los las lo al del de en con por para sin sobre entre desde hasta hacia tras bajo ante que como
    cuando donde adonde quien quienes cuanto cuantos si e o u a mi tu su mis tus sus nuestro nuestra nuestros
    nuestras vuestro vuestra vuestros vuestras me te se nos os le les pero aunque porque mientras
    """)


def _make_word(text: str, *, content: bool = False, glue: bool = False, reduced: bool = False) -> _Word | None:
    w = _fold(text)
    if not w:
        return None
    phones, nuclei, stress, open_e = _analyse(w, content)
    if not phones:
        return None
    return _Word(
        list(phones), list(nuclei), list(stress), list(open_e),
        clitic=not content and (w in _CLITICS or w == "y"), reduced=reduced, glue=glue,
    )  # fmt: skip


# Stop variants: after a pause or a nasal, and before these sounds; fricatives elsewhere.
_STOP = {"B": "b", "D": "d", "G": "ɡ"}
_FRIC = {"B": "β", "D": "ð", "G": "ɣ"}
_STOP_BEFORE = {
    "B": frozenset("ptkKBDGdmnɲŋʧ"),
    "D": frozenset("ptkKGDdmnɲʧ"),
    "G": frozenset("BDdmnɲxX"),
}
_NASALS = ("m", "n", "ŋ")
_FINAL_D_VOICED_BEFORE = _VOWEL_PHONES | {"j"}


def _phrase(words: list[_Word]) -> None:
    """Clause-level rules, in place: function-word stress, then the sounds that depend on neighbours
    across word boundaries (b d g allophones, nasal place, final d, y after n)."""
    last = len(words) - 1
    for wi, w in enumerate(words):
        if not (w.reduced or (w.clitic and wi != last)) or 2 not in w.stress:
            continue
        was = w.stress.index(2)
        w.stress = [min(s, 0) for s in w.stress]
        # a polysyllable keeps a secondary stress ("pˌaɾa", "ˌentɾe"); a monosyllable none ("de", "la")
        if w.reduced or was < len(w.stress) - 1:
            w.stress[was] = 1
    seq = [(wi, pi) for wi, w in enumerate(words) for pi in range(len(w.phones))]

    def raw(k: int) -> str | None:
        return words[seq[k][0]].phones[seq[k][1]] if 0 <= k < len(seq) else None

    prev: str | None = None
    for k, (wi, pi) in enumerate(seq):
        ph = words[wi].phones[pi]
        nxt = raw(k + 1)
        final = pi == len(words[wi].phones) - 1
        if ph in _STOP:
            if prev is None or prev in _NASALS or nxt is None:
                ph = _STOP[ph]
            elif ph == "D" and final:
                ph = "ð" if nxt in _FINAL_D_VOICED_BEFORE else "d"
            elif nxt in _STOP_BEFORE[ph]:
                ph = _STOP[ph]
            else:
                ph = _FRIC[ph]
        elif ph == "n" and final and nxt is not None:
            if nxt in ("p", "B") or (nxt == "f" and raw(k + 2) in ("l", "ɾ")):
                ph = "m"
            elif nxt in ("G", "K", "X"):
                ph = "ŋ"
        elif ph == "ʝ" and pi == 0 and prev == "n":
            ph = "ʤ"
        prev = ph
        words[wi].phones[pi] = ph


def _render(w: _Word) -> str:
    out: list[str] = []
    at = {idx: k for k, idx in enumerate(w.nuclei)}
    n = len(w.phones)
    for i, ph in enumerate(w.phones):
        k = at.get(i)
        st = w.stress[k] if k is not None else 0
        if st == 2 and k is not None:
            out.append("ˈ")
            # stressed e opens before n + consonant: "xˈɛnte", "tjˈɛnda" (but "bjˈen", "pˈena", "tˈenis")
            if ph == "e" and w.open_e[k] == 2:
                ph = "ɛ"
            elif ph == "e" and w.open_e[k] == 1 and i + 2 < n and w.phones[i + 1] in ("n", "ŋ"):
                after = w.phones[i + 2]
                if after not in _VOWEL_PHONES and after not in _GLIDES:
                    ph = "ɛ"
        elif st == 1:
            out.append("ˌ")
        out.append(_OUT.get(ph, ph))
    return "".join(out)


def _join(words: list[_Word]) -> str:
    out: list[str] = []
    for i, w in enumerate(words):
        if i and not w.glue:
            out.append(" ")
        out.append(_render(w))
    return "".join(out)


@dataclass(slots=True)
class _Spoken:
    text: str
    content: bool = False  # a letter name: never an unstressed function word
    glue: bool = False  # no space before it
    reduced: bool = False  # secondary stress only (letters of an acronym before the last)


def _words_for(sp: _Spoken) -> list[_Word]:
    """One written word (possibly hyphenated) → phonological words."""
    out: list[_Word] = []
    for part in re.split(r"[-‐‑]", sp.text):
        w = _make_word(part, content=sp.content, glue=bool(out) or sp.glue, reduced=sp.reduced)
        if w is not None:
            out.append(w)
    return out


def word_phonemes(text: str) -> str:
    """Phonemes of a Spanish phrase without punctuation, read as one clause and with no text
    normalisation: `word_phonemes("un beso")` → "ˈum bˈeso". For tests and tools; `SpanishPhonemizer`
    is the real entry point."""
    words = [w for part in text.split() for w in _words_for(_Spoken(part))]
    _phrase(words)
    return _join(words)


# ------------------------------------------------------------------------------ normalisation

_LETTER_NAMES = {
    "a": "a", "b": "be", "c": "ce", "d": "de", "e": "e", "f": "efe", "g": "ge", "h": "hache", "i": "i",
    "j": "jota", "k": "ka", "l": "ele", "m": "eme", "n": "ene", "ñ": "eñe", "o": "o", "p": "pe", "q": "cu",
    "r": "erre", "s": "ese", "t": "te", "u": "u", "v": "uve", "w": "uve doble", "x": "equis", "y": "ye",
    "z": "zeta",
}  # fmt: skip

_MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
    "noviembre", "diciembre",
]  # fmt: skip

# abbreviation (lowercase, without the final period) → (expansion, kind)
#   "title": never ends a sentence (Sr. García); "plain": its period may also end the sentence;
#   "dot": only an abbreviation when written with its period (art., cap.), otherwise an ordinary word.
_ABBREVIATIONS: dict[str, tuple[str, str]] = {
    "sr": ("señor", "title"), "sra": ("señora", "title"), "srta": ("señorita", "title"),
    "sres": ("señores", "title"), "sras": ("señoras", "title"), "dr": ("doctor", "title"),
    "dra": ("doctora", "title"), "dres": ("doctores", "title"), "dras": ("doctoras", "title"),
    "dña": ("doña", "title"), "prof": ("profesor", "title"), "profa": ("profesora", "title"),
    "lic": ("licenciado", "title"), "ing": ("ingeniero", "title"), "gral": ("general", "title"),
    "mons": ("monseñor", "title"), "sto": ("santo", "title"), "sta": ("santa", "title"),
    "excmo": ("excelentísimo", "title"), "ilmo": ("ilustrísimo", "title"), "avda": ("avenida", "title"),
    "ud": ("usted", "plain"), "vd": ("usted", "plain"), "uds": ("ustedes", "plain"), "vds": ("ustedes", "plain"),
    "etc": ("etcétera", "plain"), "pág": ("página", "title"), "págs": ("páginas", "title"),
    "pag": ("página", "dot"), "pags": ("páginas", "dot"), "núm": ("número", "title"), "num": ("número", "dot"),
    "núms": ("números", "title"), "aprox": ("aproximadamente", "plain"), "tel": ("teléfono", "dot"),
    "tlf": ("teléfono", "title"), "tfno": ("teléfono", "title"), "art": ("artículo", "dot"),
    "arts": ("artículos", "dot"), "cap": ("capítulo", "dot"), "caps": ("capítulos", "dot"),
    "vol": ("volumen", "dot"), "vols": ("volúmenes", "dot"), "ed": ("edición", "dot"), "av": ("avenida", "dot"),
    "cía": ("compañía", "plain"), "vs": ("versus", "title"), "máx": ("máximo", "plain"),
    "mín": ("mínimo", "plain"), "izq": ("izquierda", "plain"), "dcha": ("derecha", "plain"),
    "dpto": ("departamento", "title"), "admón": ("administración", "plain"), "fdo": ("firmado", "title"),
    "pp": ("páginas", "dot"), "ss": ("siguientes", "dot"), "ej": ("ejemplo", "dot"),
    "eeuu": ("estados unidos", "plain"),
}  # fmt: skip

_CURRENCIES: dict[str, tuple[str, str, bool, str, str]] = {
    # symbol → (singular, plural, feminine, cent singular, cent plural)
    "€": ("euro", "euros", False, "céntimo", "céntimos"),
    "eur": ("euro", "euros", False, "céntimo", "céntimos"),
    "$": ("dólar", "dólares", False, "centavo", "centavos"),
    "us$": ("dólar", "dólares", False, "centavo", "centavos"),
    "usd": ("dólar", "dólares", False, "centavo", "centavos"),
    "£": ("libra", "libras", True, "penique", "peniques"),
    "¥": ("yen", "yenes", False, "", ""),
}

_UNITS_OF_MEASURE: dict[str, tuple[str, str, bool]] = {
    # after a number only: symbol → (singular, plural, feminine)
    "km": ("kilómetro", "kilómetros", False), "m": ("metro", "metros", False),
    "cm": ("centímetro", "centímetros", False), "mm": ("milímetro", "milímetros", False),
    "kg": ("kilogramo", "kilogramos", False), "g": ("gramo", "gramos", False),
    "mg": ("miligramo", "miligramos", False), "l": ("litro", "litros", False),
    "ml": ("mililitro", "mililitros", False), "h": ("hora", "horas", True),
    "min": ("minuto", "minutos", False), "s": ("segundo", "segundos", False),
    "km/h": ("kilómetro por hora", "kilómetros por hora", False),
    "m²": ("metro cuadrado", "metros cuadrados", False), "m2": ("metro cuadrado", "metros cuadrados", False),
    "km²": ("kilómetro cuadrado", "kilómetros cuadrados", False),
    "km2": ("kilómetro cuadrado", "kilómetros cuadrados", False),
    "°c": ("grado", "grados", False), "ºc": ("grado", "grados", False), "°": ("grado", "grados", False),
}  # fmt: skip
_UNIT_SPELLINGS = frozenset({"Km", "KM", "Kg", "KG", "L", "°C", "ºC"}) | frozenset(_UNITS_OF_MEASURE)

_NUM = (
    r"(?:\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)(?!\d)"
)
_RE_NUM = re.compile(_NUM)
_RE_DATE = re.compile(r"(\d{1,2})([/.\-])(\d{1,2})\2(\d{4})(?!\d)")
_RE_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?!\d)")
_RE_TIME = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d:])(\s?(?:h|hs|hrs)\b)?")
_RE_ORDINAL = re.compile(r"(\d{1,3})(?:(\.?)\s?([ºª])(?![Cc]\b)|\.?(er|ro|ra|do|da|to|ta|mo|ma|vo|va|no|na|os|as)\b)")
_RE_FRACTION = re.compile(r"([123])/([234])(?![\d/])")
_FRACTIONS = {"1/2": "un medio", "1/3": "un tercio", "2/3": "dos tercios", "1/4": "un cuarto", "3/4": "tres cuartos"}
_RE_CUR_PREFIX = re.compile(rf"(US\$|\$|€|£|¥)\s?({_NUM})(?:\s(mil(?:es)?|mill[oó]n(?:es)?|bill[oó]n(?:es)?)\b)?")
_RE_CUR_SUFFIX = re.compile(rf"({_NUM})\s?(€|US\$|\$|£|¥|EUR\b|USD\b)")
_RE_PERCENT = re.compile(rf"({_NUM})\s?%")
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNIT_SPELLINGS), key=len, reverse=True))
_RE_UNIT = re.compile(rf"({_NUM})(\s?)({_UNIT_ALT})(?![\w/²])")
_RE_WORD = re.compile(r"[^\W_]+(?:['’\-‐‑][^\W_]+)*")
_RE_NEXT_WORD = re.compile(r"\s*([^\W\d_]+)")
_RE_DOTTED = re.compile(r"(?:[A-Za-zÑñ]\.){2,}")
_RE_EEUU = re.compile(r"EE\.?\s?UU\b\.?")
_RE_ORD_MARK = re.compile(r"\.\s?[ºª]")
_RE_ROMAN = re.compile(r"M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})")
_RE_ALNUM_RUN = re.compile(r"\d+|[^\W\d_]+")
_RE_VOWEL_GROUPS = re.compile(r"[aeiouáéíóúü]+|[^aeiouáéíóúü]+")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
# Words after which a Roman numeral is certain; there it is read as a cardinal ("siglo veinte").
_ROMAN_AFTER = _ws(
    "siglo siglos capítulo capítulos tomo tomos volumen acto título libro canto parte fase apartado anexo "
    "sección artículo"
)
# Nouns a Roman ordinal precedes: "la II Guerra Mundial", "el XIX Festival", "la XXI edición".
_ROMAN_BEFORE = _ws(
    "guerra congreso edición festival república internacional legislatura dinastía milenio reich concilio "
    "jornada jornadas juegos olimpiada olimpiadas feria asamblea cumbre conferencia copa campeonato certamen "
    "centenario aniversario encuentro semana"
)
_ROMAN_NOT_AFTER = _ws("generación malcolm rayos planeta míster mister factor cromosoma vitamina")
_ARTICLES = {"el": (False, False), "del": (False, False), "al": (False, False), "la": (True, False),
             "los": (False, True), "las": (True, True)}  # fmt: skip
_FEMININE_NAMES = _ws("isabel elizabeth beatriz leonor inés")
_LETTER_NOUNS = _ws(
    "vitamina vitaminas plan grupo tipo clase hepatitis letra opción nivel serie categoría modelo división "
    "bloque sector zona anexo apartado"
)
_NO_AGREEMENT_BEFORE = _ws(
    "de del y e o u a al en que por para con sin es son era fue como más menos entre sobre desde hasta "
    "según contra ni pero sino cuando donde si no se le lo la las los el un una"
)
_COUNT_NOUNS = _ws(
    "año años día días mes meses euro euros dólar dólares peso pesos kilo kilos metro metros litro litros "
    "punto puntos minuto minutos segundo segundos grado grados siglo siglos kilómetro kilómetros gramo gramos "
    "habitante habitantes hombre hombres país países voto votos gol goles"
)
_FEM_COUNT_NOUNS = _ws(
    "hora horas semana semanas vez veces persona personas página páginas libra libras parte partes "
    "tonelada toneladas hectárea hectáreas milla millas mujer mujeres pesetas peseta"
)
_MASC_IN_A = _ws(
    "días problemas sistemas temas programas idiomas mapas climas planetas poemas dramas atletas artistas turistas"
)
_TWO_LETTER_WORDS = _ws("al de el en es ha he la le lo me mi ni no os se si su te tu un va ve ya yo")
_SYMBOL_WORDS = {
    "&": "y", "+": "más", "=": "igual a", "@": "arroba", "%": "por ciento", "№": "número", "€": "euros",
    "$": "dólares", "£": "libras", "°": "grados", "×": "por", "±": "más menos",
}  # fmt: skip
_QUOTES = {"«": "“", "»": "”", "„": "“", "‟": "”"}
_BOUNDARY_ONLY = frozenset("¿¡")
_ONSET_CLUSTERS = _ws("bl br cl cr dr fl fr gl gr pl pr tr ch ll rr qu gu")
_WORD_FINALS = frozenset("dlnrszxmctbfgkpj")
_SENTENCE_OPENERS = frozenset('¿¡—«"“')


@dataclass(slots=True)
class _Item:
    start: int
    end: int
    words: list[_Spoken] = field(default_factory=list)
    punct: str = ""  # punctuation symbol to emit ("" with no words: a silent clause boundary)


def _say(*words: str) -> list[_Spoken]:
    return [_Spoken(p) for w in words for p in w.split()]


def _spell(letters: str) -> list[_Spoken]:
    """Letter names, said as one run with the main stress on the last: DNI → "dˌeˌeneˈi"."""
    out: list[_Spoken] = []
    for ch in letters.lower():
        name = _LETTER_NAMES.get(ch) or _LETTER_NAMES.get(_fold(ch))
        if not name:
            continue
        for k, part in enumerate(name.split()):
            out.append(_Spoken(part, content=True, glue=bool(out) and k == 0, reduced=True))
    if out:
        out[-1].reduced = False
    return out


def _pronounceable(word: str) -> bool:
    """Could this all-caps token be read as a Spanish word (ONU, OTAN, UNESCO) rather than spelled (DNI)?"""
    w = _fold(word)
    if len(w) < 3 or not any(c in _VOWEL_LETTERS for c in w):
        return False
    if w.endswith("y") and w[-2] in _VOWEL_LETTERS:
        w = w[:-1]  # hoy, rey
    groups: list[str] = _RE_VOWEL_GROUPS.findall(w)
    for k, g in enumerate(groups):
        if g[0] in _VOWEL_LETTERS:
            continue
        first, final = k == 0, k == len(groups) - 1
        if "w" in g or (first and final):
            return False
        if first:
            ok = len(g) == 1 or g in _ONSET_CLUSTERS
        elif final:
            ok = len(g) == 1 and g in _WORD_FINALS
        else:
            ok = (
                len(g) == 1
                or g in _ONSET_CLUSTERS
                or (len(g) == 2 and g[0] in "lmnrsczxp" and g[1] != "h")
                or (len(g) == 3 and g[0] in "lmnrs" and g[1:] in _ONSET_CLUSTERS)
            )
        if not ok:
            return False
    return True


def _roman_value(s: str) -> int:
    total = 0
    for a, b in zip(s, s[1:] + " ", strict=True):
        v = _ROMAN_VALUES[a]
        total += -v if b != " " and _ROMAN_VALUES[b] > v else v
    return total


def _parse_number(s: str) -> tuple[str, str, str]:
    """ "1.234,5" → ("1234", "5", ","). A separator followed by exactly three digits groups thousands
    ("1.200" and "1,200" are both 1200); otherwise it is the decimal mark ("3,5", "3.5")."""
    s = re.sub(r"[   ]", "", s)
    seps = [c for c in s if c in ".,"]
    if not seps:
        return s, "", ""
    mark = seps[-1]
    head, _, tail = s.rpartition(mark)
    if len(set(seps)) == 2:  # both marks: the last one is the decimal mark
        return re.sub(r"[.,]", "", head), tail, mark
    if len(tail) == 3 and not s.startswith("0"):
        return re.sub(r"[.,]", "", s), "", ""
    return head.replace(mark, ""), tail, mark


def _digits(s: str) -> list[str]:
    return [_UNITS[int(d)] for d in s if d.isdecimal()]


def _number_words(s: str, *, fem: bool = False, apocope: bool = False) -> list[str]:
    whole, frac, mark = _parse_number(s)
    if (len(whole) > 1 and whole.startswith("0")) or len(whole) > 18:
        words = _digits(whole)  # codes and phone numbers: digit by digit
    else:
        words = cardinal_words(int(whole), fem=fem and not frac, apocope=apocope and not frac)
    if frac:
        words.append("coma" if mark == "," else "punto")
        words += _digits(frac) if frac.startswith("0") or len(frac) > 3 else cardinal_words(int(frac))
    return words


def _counted(s: str, singular: str, plural: str, fem: bool) -> list[str]:
    """Number + noun with agreement: "1 km" → un kilómetro; "21 libras" → veintiuna libras;
    "2.000.000 €" → dos millones de euros."""
    whole, frac, _ = _parse_number(s)
    words = _number_words(s, fem=fem, apocope=not fem)
    value = int(whole) if not frac and whole.isdecimal() else -1
    millions = value >= 10**6 and value % 10**6 == 0
    return [*words, *(["de"] if millions else []), *(singular if value == 1 else plural).split()]


class _Scanner:
    """Source text → items: spoken words with their source span, punctuation, silent clause boundaries."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.items: list[_Item] = []
        self._romans: set[int] = set()  # indices of items read as Roman numerals

    # -- helpers

    def _prev(self) -> tuple[str, str]:
        """(source text, last spoken word) of the item just before, when that item is a word."""
        if self.items and self.items[-1].words:
            it = self.items[-1]
            return self.text[it.start : it.end], it.words[-1].text
        return "", ""

    def _next_word(self, pos: int) -> str:
        m = _RE_NEXT_WORD.match(self.text, pos)
        return m.group(1) if m else ""

    def _add(self, start: int, end: int, words: list[_Spoken]) -> int:
        if words:
            self.items.append(_Item(start, end, words))
        return end

    def _abbrev(self, start: int, end: int, words: list[_Spoken], kind: str) -> int:
        """`end` is just past the abbreviation's period. The period is part of the abbreviation unless it
        also ends the sentence, in which case it stays as punctuation."""
        rest = self.text[end:]
        stripped = rest.lstrip()
        closes = kind != "title" and (
            not stripped or (rest[:1].isspace() and (stripped[0].isupper() or stripped[0] in _SENTENCE_OPENERS))
        )
        if closes:
            self._add(start, end - 1, words)
            self.items.append(_Item(end - 1, end, punct="."))
        else:
            self._add(start, end, words)
        return end

    # -- numbers

    def _agreement(self, number: str, end: int) -> tuple[bool, bool]:
        """(feminine, apocope) for a bare number, from the noun right after it: "21 años" → veintiún,
        "200 personas" → doscientas, "1 hora" → una. Conservative: without a clear noun, no change."""
        t = self.text
        nxt = self._next_word(end)
        low = nxt.lower()
        whole, frac, _ = _parse_number(number)
        if t[end : end + 1] != " " or not nxt or nxt != low or low in _NO_AGREEMENT_BEFORE or frac:
            return False, False
        if low in ("mil", "millones", "billones"):
            return False, True
        value = int(whole)
        known = low in _COUNT_NOUNS or low in _FEM_COUNT_NOUNS
        if value == 1:
            return low in _FEM_COUNT_NOUNS, low in _COUNT_NOUNS
        year_like = len(number) == 4 and 1100 <= value <= 2100
        if (year_like and not known) or not low.endswith("s"):
            return False, False
        fem = low in _FEM_COUNT_NOUNS or (low.endswith("as") and low not in _MASC_IN_A)
        return fem, not fem

    def _number(self, i: int) -> int:
        t = self.text
        m = _RE_DATE.match(t, i)
        if m and 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(3)) <= 12:
            return self._date(i, m.end(), int(m.group(1)), int(m.group(3)), int(m.group(4)))
        m = _RE_ISO_DATE.match(t, i)
        if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
            return self._date(i, m.end(), int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = _RE_TIME.match(t, i)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 and (m.group(3) is None or int(m.group(3)) < 60):
            return self._time(i, m)
        m = _RE_FRACTION.match(t, i)
        if m and m.group(0) in _FRACTIONS and (i == 0 or not t[i - 1].isdigit()):
            return self._add(i, m.end(), _say(_FRACTIONS[m.group(0)]))
        m = _RE_ORDINAL.match(t, i)
        if m:
            value, mark = int(m.group(1)), m.group(3) or m.group(4)
            if mark in ("º", "ª") and not m.group(2) and value > 20:
                # "40º" without the period: degrees, not fortieth
                return self._add(i, m.end(), _say(*_counted(m.group(1), "grado", "grados", False)))
            fem = mark in ("ª", "ra", "da", "ta", "ma", "va", "na", "as")
            words = ordinal_words(value, fem=fem, plural=mark in ("os", "as"), apocope=mark == "er")
            return self._add(i, m.end(), _say(*words))
        m = _RE_CUR_SUFFIX.match(t, i)
        if m:
            return self._add(i, m.end(), _say(*self._money(m.group(1), m.group(2), "")))
        m = _RE_PERCENT.match(t, i)
        if m:
            return self._add(i, m.end(), _say(*_number_words(m.group(1)), "por ciento"))
        m = _RE_UNIT.match(t, i)
        if m and not (m.group(3) == "s" and not m.group(2)):
            sing, plur, fem = _UNITS_OF_MEASURE[m.group(3).lower()]
            return self._add(i, m.end(), _say(*_counted(m.group(1), sing, plur, fem)))
        m = _RE_NUM.match(t, i)
        if m:
            fem, apocope = self._agreement(m.group(0), m.end())
            return self._add(i, m.end(), _say(*_number_words(m.group(0), fem=fem, apocope=apocope)))
        return -1

    def _date(self, start: int, end: int, day: int, month: int, year: int) -> int:
        words = [*cardinal_words(day), "de", _MONTHS[month - 1], "de", *cardinal_words(year)]
        return self._add(start, end, _say(*words))

    def _time(self, start: int, m: re.Match[str]) -> int:
        h, mi, sec = int(m.group(1)), int(m.group(2)), m.group(3)
        hours = cardinal_words(h, fem=True)
        if sec is not None:
            words = [*hours, "horas", *cardinal_words(mi), "minutos", "y", *cardinal_words(int(sec)), "segundos"]
        elif mi == 0:
            words = [*hours, *(["horas"] if m.group(4) else ["en", "punto"])]
        else:
            words = [*hours, "y", *cardinal_words(mi)]
        return self._add(start, m.end(), _say(*words))

    def _money(self, number: str, symbol: str, scale: str) -> list[str]:
        sing, plur, fem, cent, cents = _CURRENCIES[symbol.lower()]
        whole, frac, _ = _parse_number(number)
        if scale:  # "US$ 5.000 millones" → cinco mil millones de dólares
            link = [] if scale.lower().startswith("mil") and not scale.lower().startswith("mill") else ["de"]
            return [*_number_words(number, apocope=True), scale.lower(), *link, plur]
        if len(frac) == 2 and cents:
            out = _counted(whole, sing, plur, fem)
            if int(frac):
                out += ["con", *_counted(str(int(frac)), cent, cents, False)]
            return out
        return _counted(number, sing, plur, fem)

    # -- words

    def _roman(self, i: int, tok: str, end: int) -> int:
        """Roman numerals: cardinals after "siglo", "capítulo"… and on their own ("el XX"), ordinals for
        monarchs up to ten (Felipe VI → sexto) and before a noun ("la II Guerra Mundial")."""
        t = self.text
        up = tok.upper()
        if not (tok.isupper() or tok.islower()) or not _RE_ROMAN.fullmatch(up) or t[end : end + 1] in ("'", "’"):
            return -1
        prev_src, prev_said = self._prev()
        low = prev_src.lower()
        value = _roman_value(up)
        century = low in ("siglo", "siglos") or prev_said == "siglo"
        # "siglos XVI y XVII": the numeral after the connector continues the list
        listed = low in ("y", "e", "o", "u", "al", "a") and len(self.items) - 2 in self._romans
        if century or listed or (tok.isupper() and low in _ROMAN_AFTER and not (len(tok) == 1 and tok in "LCDM")):
            # extracts of typeset text often carry small capitals as lowercase: "siglo xx"
            self._romans.add(len(self.items))
            return self._add(i, end, _say(*cardinal_words(value)))
        if not tok.isupper():
            # lowercase on its own only when it cannot be a word: "del xxi" yes, "vi" (I saw) no
            if len(tok) > 1 and set(tok) <= set("xvi") and tok not in ("vi", "xi"):
                self._romans.add(len(self.items))
                return self._add(i, end, _say(*cardinal_words(value)))
            return -1
        simple = all(c in "IVX" for c in tok)
        nxt = self._next_word(end)
        before_noun = nxt.lower() in _ROMAN_BEFORE or (nxt[:1].isupper() and nxt[1:].islower() and len(nxt) > 3)
        if low in _ARTICLES and before_noun and value <= 100 and (simple or len(tok) > 1):
            fem, plural = _ARTICLES[low]
            return self._add(i, end, _say(*ordinal_words(value, fem=fem, plural=plural, apocope=True)))
        named = prev_src[:1].isupper() and prev_src[1:].islower() and len(prev_src) > 2
        if named and simple and low not in _ROMAN_NOT_AFTER:
            if value <= 10:
                fem = low in _FEMININE_NAMES or low.endswith("a")
                return self._add(i, end, _say(*ordinal_words(value, fem=fem)))
            return self._add(i, end, _say(*cardinal_words(value)))
        if simple and len(tok) > 1:
            return self._add(i, end, _say(*cardinal_words(value)))
        return -1

    def _word(self, i: int) -> int:
        t = self.text
        m = _RE_EEUU.match(t, i)
        if m:
            words = _say("estados unidos")
            dotted = m.group(0).endswith(".")
            return self._abbrev(i, m.end(), words, "plain") if dotted else self._add(i, m.end(), words)
        m = _RE_DOTTED.match(t, i)
        if m:
            raw = m.group(0).replace(".", "")
            if raw.lower() in _ABBREVIATIONS:
                expansion, kind = _ABBREVIATIONS[raw.lower()]
                return self._abbrev(i, m.end(), _say(expansion), kind)
            if raw.isupper() or len(raw) <= 3:  # S.A., U.S.A., a.m.
                words = _say(raw.lower()) if _pronounceable(raw) else _spell(raw)
                return self._abbrev(i, m.end(), words, "plain")
        wm = _RE_WORD.match(t, i)
        if wm is None:
            return -1
        tok, end = wm.group(0), wm.end()
        low = tok.lower()
        dotted = t[end : end + 1] == "."
        if dotted and low in ("n", "d") and (om := _RE_ORD_MARK.match(t, end)):
            return self._add(i, om.end(), _say("número" if low == "n" else "doña"))  # n.º 5, D.ª Inés
        if low in ("nº", "n°"):
            return self._add(i, end, _say("número"))
        if low in _ABBREVIATIONS and (tok.islower() or tok.istitle() or tok.isupper()):
            expansion, kind = _ABBREVIATIONS[low]
            if kind == "dot" and tok.isupper() and len(tok) > 1:
                pass  # "las SS.", "el PP.": acronyms, not "siguientes"/"páginas"
            elif dotted:
                return self._abbrev(i, end + 1, _say(expansion), kind)
            elif kind != "dot" and not _pronounceable(low):
                return self._add(i, end, _say(expansion))
        if dotted and len(tok) == 1 and tok.isalpha():
            j = self._letter_dot(i, tok, end)
            if j >= 0:
                return j
        if low == "c" and t[end : end + 1] == "/" and self._next_word(end + 1):
            return self._add(i, end + 1, _say("calle"))
        j = self._roman(i, tok, end)
        if j >= 0:
            return j
        return self._add(i, end, self._plain(tok))

    def _letter_dot(self, i: int, tok: str, end: int) -> int:
        """A single letter followed by a period: D. Juan, p. 34, p. ej., s. XVI, a. C., initials."""
        t = self.text
        low = tok.lower()
        nxt = self._next_word(end + 1)
        after = t[end + 1 :].lstrip()
        honorific = low == "d" and tok.isupper() and nxt[:1].isupper() and len(nxt) > 2
        if honorific and self._prev()[1].lower() not in _LETTER_NOUNS:  # "vitamina D. Luego…" is the letter
            return self._add(i, end + 1, _say("don"))
        if low == "p" and after[:1].isdigit():
            return self._add(i, end + 1, _say("página"))
        if low == "p" and nxt.lower() == "ej":
            return self._add(i, end + 1, _say("por"))
        if low == "s" and nxt.isupper() and _RE_ROMAN.fullmatch(nxt):
            return self._add(i, end + 1, _say("siglo"))
        if low in ("a", "d") and after.startswith("C."):
            stop = t.index("C.", end) + 2
            said = _say("antes de cristo" if low == "a" else "después de cristo")
            return self._abbrev(i, stop, said, "plain")
        prev_src = self._prev()[0]
        if (
            tok.isupper()
            and after[:1].isupper()
            and t[end + 1 : end + 2].isspace()
            and (not prev_src or prev_src[:1].isupper())
        ):
            return self._add(i, end + 1, _spell(tok))  # an initial: J. R. R. Tolkien
        return -1

    def _plain(self, tok: str) -> list[_Spoken]:
        """An ordinary token: word, acronym, lone letter, or letters mixed with digits."""
        parts = [p for p in re.split(r"[-‐‑]", tok) if p]
        if len(parts) > 1 and all(p.isalpha() and len(p) > 1 and not p.isupper() for p in parts):
            return [_Spoken(tok)]  # hyphenated compound: one written word, several phonological ones
        out: list[_Spoken] = []
        for part in parts:
            for run in _RE_ALNUM_RUN.findall(part.replace("'", "").replace("’", "")):
                if run.isdecimal():
                    out += _say(*(_digits(run) if len(run) > 1 and run.startswith("0") else cardinal_words(int(run))))
                    continue
                low = run.lower()
                folded = _fold(run)
                if not folded:
                    continue
                if len(folded) == 1:
                    if low in ("a", "e", "o", "u", "y") and run == tok:
                        out.append(_Spoken(low))
                    else:
                        out += _spell(folded)
                elif run.isupper() and len(run) <= 5:
                    if (len(folded) == 2 and low in _TWO_LETTER_WORDS) or _pronounceable(run):
                        out.append(_Spoken(low))
                    else:
                        out += _spell(folded)
                elif not any(c in _VOWEL_LETTERS or c == "y" for c in folded) and len(folded) <= 5:
                    out += _spell(folded)  # no vowel at all: "pdf", "tv"
                else:
                    out.append(_Spoken(run))
        return out

    # -- main loop

    def scan(self) -> list[_Item]:
        t = self.text
        n = len(t)
        i = 0
        while i < n:
            ch = t[i]
            if ch.isspace():
                i += 1
                continue
            j = -1
            if ch.isdecimal():
                j = self._number(i)
            elif ch in "$€£¥" or (ch in "Uu" and t[i : i + 3].upper() == "US$"):
                m = _RE_CUR_PREFIX.match(t, i)
                if m:
                    j = self._add(i, m.end(), _say(*self._money(m.group(2), m.group(1), m.group(3) or "")))
            if j < 0 and ch.isalnum():
                j = self._word(i)
            if j > i:
                i = j
                continue
            before, after = t[i - 1 : i], t[i + 1 : i + 2]
            if ch in PUNCT:
                self.items.append(_Item(i, i + 1, punct=ch))
            elif ch in _QUOTES:
                self.items.append(_Item(i, i + 1, punct=_QUOTES[ch]))
            elif ch in _BOUNDARY_ONLY:
                self.items.append(_Item(i, i + 1))
            elif ch in "–−" and (not before or before.isspace()) and after.isspace():
                self.items.append(_Item(i, i + 1, punct="—"))  # a spaced en dash used as a dash
            elif ch in "-–" and before.isdecimal() and after.isdecimal() and (ch == "–" or self._year_range(i)):
                self._add(i, i + 1, _say("a"))  # 1936–1939
            elif ch in "-−" and after.isdecimal() and (not before or before.isspace() or before in "(¿¡"):
                self._add(i, i + 1, _say("menos"))
            elif ch in _SYMBOL_WORDS:
                self._add(i, i + 1, _say(_SYMBOL_WORDS[ch]))
            i += 1
        return self.items

    def _year_range(self, i: int) -> bool:
        t = self.text
        return bool(re.fullmatch(r"\d{4}", t[max(0, i - 4) : i])) and bool(re.match(r"\d{4}(?!\d)", t[i + 1 :]))


def _scan(text: str) -> list[_Item]:
    try:
        return _Scanner(text).scan()
    except Exception:  # never let a normalisation bug stop the reading: fall back to plain words
        log.exception("Spanish text normalisation failed; reading the words as written")
        items: list[_Item] = []
        for m in re.finditer(r"[^\W\d_]+|[" + re.escape(PUNCT) + "]", text):
            if m.group(0)[0] in PUNCT:
                items.append(_Item(m.start(), m.end(), punct=m.group(0)))
            else:
                items.append(_Item(m.start(), m.end(), [_Spoken(m.group(0))]))
        return items


def spoken_words(text: str) -> list[str]:
    """The Spanish words the phonemizer will say for `text`, numbers and abbreviations expanded:
    `spoken_words("El 1.º de mayo costó 3,50 €")` → el primero de mayo costó tres euros con cincuenta
    céntimos."""
    return [w.text for it in _scan(text) for w in it.words]


# ---------------------------------------------------------------------------------- phonemizer


class SpanishPhonemizer:
    """Rule-based Spanish G2P for Kokoro's `e*` voices. No espeak, no dictionaries, no network."""

    language = "es"

    def phonemize(self, text: str) -> list[PhonemeToken]:
        items = _scan(text)
        per_item: list[list[_Word]] = []
        clause: list[_Word] = []
        for it in items:
            if not it.words:  # punctuation or ¿ ¡: a clause boundary
                _phrase(clause)
                clause = []
                per_item.append([])
                continue
            words = [w for sp in it.words for w in _words_for(sp)]
            per_item.append(words)
            clause += words
        _phrase(clause)

        emitted = [(it, ws) for it, ws in zip(items, per_item, strict=True) if it.punct or ws]
        out: list[PhonemeToken] = []
        for k, (it, ws) in enumerate(emitted):
            nxt = emitted[k + 1][0] if k + 1 < len(emitted) else None
            if it.punct:
                space = nxt is not None and (nxt.start > it.end or text[it.end : it.end + 1].isspace())
                out.append(PhonemeToken(it.start, it.end, it.punct, is_word=False, whitespace=space))
            else:
                space = nxt is not None and not (nxt.punct and nxt.start == it.end)
                out.append(PhonemeToken(it.start, it.end, _join(ws), is_word=True, whitespace=space))
        return out
