"""Which language a document is written in, for choosing a voice that can pronounce it.

A count of very common function words is enough to tell the supported languages apart and costs nothing; a
document too short or too mixed to call stays `None` and is read with the user's default voice.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from open_speech.config import Language

_STOPWORDS: dict[Language, frozenset[str]] = {
    "en": frozenset(
        [
            "the",
            "of",
            "and",
            "to",
            "in",
            "is",
            "that",
            "it",
            "was",
            "for",
            "on",
            "are",
            "as",
            "with",
            "his",
            "they",
            "be",
            "at",
            "this",
            "have",
            "from",
            "or",
            "had",
            "by",
            "not",
            "but",
            "what",
            "all",
            "were",
            "we",
            "when",
            "your",
            "can",
            "said",
            "there",
            "use",
            "an",
            "each",
            "which",
            "she",
            "do",
            "how",
            "their",
            "if",
            "will",
            "up",
            "other",
            "about",
            "out",
            "many",
            "then",
            "them",
            "these",
            "so",
            "some",
            "her",
            "would",
            "make",
            "like",
            "him",
            "into",
            "has",
            "more",
            "than",
            "been",
            "who",
            "its",
            "now",
            "my",
            "could",
            "no",
            "over",
            "only",
            "also",
            "after",
            "where",
            "most",
            "through",
            "before",
            "between",
            "should",
            "because",
            "while",
            "any",
        ]
    ),
    "es": frozenset(
        [
            "el",
            "la",
            "de",
            "que",
            "y",
            "en",
            "los",
            "del",
            "se",
            "las",
            "por",
            "un",
            "para",
            "con",
            "no",
            "una",
            "su",
            "al",
            "lo",
            "como",
            "más",
            "pero",
            "sus",
            "le",
            "ya",
            "o",
            "este",
            "sí",
            "porque",
            "esta",
            "entre",
            "cuando",
            "muy",
            "sin",
            "sobre",
            "también",
            "me",
            "hasta",
            "hay",
            "donde",
            "quien",
            "desde",
            "todo",
            "nos",
            "durante",
            "todos",
            "uno",
            "les",
            "ni",
            "contra",
            "otros",
            "ese",
            "eso",
            "ante",
            "ellos",
            "esto",
            "mí",
            "antes",
            "algunos",
            "qué",
            "unos",
            "yo",
            "otro",
            "otras",
            "otra",
            "él",
            "tanto",
            "esa",
            "estos",
            "mucho",
            "quienes",
            "nada",
            "muchos",
            "cual",
            "poco",
            "ella",
            "estar",
            "estas",
            "algunas",
            "algo",
            "nosotros",
            "es",
            "son",
            "fue",
            "era",
            "ser",
            "han",
            "está",
            "están",
            "tiene",
            "puede",
        ]
    ),
}
_SPANISH_MARKS = re.compile(r"[ñ¿¡áéíóúü]", re.IGNORECASE)
_WORD = re.compile(r"[^\W\d_]+")
SAMPLE_WORDS = 1500


def detect_language(text: str) -> Language | None:
    words = [w.lower() for w in _WORD.findall(text)[:SAMPLE_WORDS]]
    if not words:
        return None
    scores = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    # "a", "no", "me", "o"… are words in both; spelling that only one language uses settles close calls.
    if _SPANISH_MARKS.search(text[:8000]):
        scores["es"] += 2
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top < 2 or top < second * 1.5:
        return None
    return best


def language_of_blocks(texts: Iterable[str]) -> Language | None:
    """Detect from the start of a document (headings and the first paragraphs are plenty)."""
    sample: list[str] = []
    size = 0
    for text in texts:
        sample.append(text)
        size += len(text)
        if size > 12_000:
            break
    return detect_language("\n".join(sample))
