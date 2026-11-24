"""What people read to clone their voice, the sentence a new voice is checked with, and the neutral passage a designed
voice is rendered with (08 § Voice cloning).

A passage is two everyday sentences, about 8-10 seconds read at a normal pace: long enough for a 6-10 s reference,
short enough to read in one go. No numbers, names or rare words, so the speech recognizer that checks the reading
has nothing to spell differently. Each language has several, so "try again" can offer another.
"""

from __future__ import annotations

from dataclasses import dataclass

CONSENT_VERSION = "consent-v1"


@dataclass(frozen=True)
class Passage:
    id: str
    language: str
    text: str


PASSAGES: tuple[Passage, ...] = (
    Passage("en-1", "en", "I like to start my mornings slowly, with a cup of coffee and the news. "
                          "Then I go for a short walk, even when the sky is gray and cloudy."),
    Passage("en-2", "en", "Every summer we drive up the coast to visit my grandmother. "
                          "She bakes fresh bread, tells funny stories, and always asks about my work."),
    Passage("en-3", "en", "The library is quiet in the morning, so that is when I like to go. "
                          "I find a table by the window and read until it is time for lunch."),
    Passage("es-1", "es", "Me gusta empezar el día con calma, con un café y las noticias. "
                          "Después salgo a caminar un rato, aunque el cielo esté nublado."),
    Passage("es-2", "es", "Cada verano viajamos a la costa para visitar a mi abuela. "
                          "Ella hace pan casero, cuenta historias divertidas y siempre pregunta por mi trabajo."),
    Passage("es-3", "es", "La biblioteca está tranquila por la mañana, por eso me gusta ir temprano. "
                          "Busco una mesa junto a la ventana y leo hasta la hora de comer."),
)

# Said by a new voice right after it is made: the user hears it, and the speech recognizer checks it (word error
# rate → good / fair / poor). Different words from every passage, so a voice that only parrots its reference fails.
CHECK_TEXT = {
    "en": "This is how I sound with your voice. I can read your articles, emails and books aloud, at whatever pace "
          "you like.",
    "es": "Así sueno con tu voz. Puedo leerte artículos, correos y libros en voz alta, al ritmo que prefieras.",
}

# What a designed voice says in its candidates; the one kept becomes its reference, so this is also its ref_text.
DESIGN_TEXT = {
    "en": "Hello there. I can read you the news, a long report or a good book, and I will always keep a calm, "
          "steady pace.",
    "es": "Hola. Puedo leerte las noticias, un informe largo o un buen libro, y siempre mantendré un ritmo "
          "tranquilo y constante.",
}


def base_language(language: str | None) -> str:
    code = (language or "en").lower().replace("_", "-").split("-")[0]
    return code if code in CHECK_TEXT else "en"


def passages(language: str | None = None) -> list[Passage]:
    lang = base_language(language) if language else None
    return [p for p in PASSAGES if lang is None or p.language == lang]


def passage(passage_id: str) -> Passage | None:
    return next((p for p in PASSAGES if p.id == passage_id), None)


def next_passage(language: str, after: str | None = None) -> Passage:
    """The first passage in `language`, or the one after `after` (another one to try)."""
    mine = passages(language)
    if after is None:
        return mine[0]
    ids = [p.id for p in mine]
    i = ids.index(after) + 1 if after in ids else 0
    return mine[i % len(mine)]


def check_text(language: str | None) -> str:
    return CHECK_TEXT[base_language(language)]


def design_text(language: str | None) -> str:
    return DESIGN_TEXT[base_language(language)]


__all__ = ["CONSENT_VERSION", "PASSAGES", "Passage", "check_text", "design_text", "next_passage", "passage", "passages"]
