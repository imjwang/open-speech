"""Sentence segmentation and chunking with `language="es"` (document/segment.py, plan/chunk.py)."""

from __future__ import annotations

import pytest

from open_speech.document.segment import sentences, words
from open_speech.plan.chunk import chunk_text
from open_speech.playback.timing import WORD_RE


def _es(text: str) -> list[str]:
    return [text[s:e] for s, e in sentences(text, "es")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("¿Cómo estás? ¡Muy bien! Me alegro mucho.", ["¿Cómo estás?", "¡Muy bien!", "Me alegro mucho."]),
        (
            "El Sr. García y la Sra. López llegaron tarde. El Dr. Pérez no vino.",
            ["El Sr. García y la Sra. López llegaron tarde.", "El Dr. Pérez no vino."],
        ),
        ("Ud. tiene razón. Uds. también.", ["Ud. tiene razón.", "Uds. también."]),
        ("Gracias a Ud. Mañana vuelvo.", ["Gracias a Ud.", "Mañana vuelvo."]),
        ("Véase la pág. 34 del libro. El núm. 5 es mejor.", ["Véase la pág. 34 del libro.", "El núm. 5 es mejor."]),
        ("En EE. UU. hay muchos estados. También en México.", ["En EE. UU. hay muchos estados.", "También en México."]),
        ("En EE.UU. hay muchos estados. También en México.", ["En EE.UU. hay muchos estados.", "También en México."]),
        ("Vive en EE.UU. Trabaja en México.", ["Vive en EE.UU.", "Trabaja en México."]),
        ("Compró frutas, etc. Luego se fue a casa.", ["Compró frutas, etc.", "Luego se fue a casa."]),
        ("Compró frutas, etc. y luego se fue.", ["Compró frutas, etc. y luego se fue."]),
        ("Nació en el s. XVI. Murió en 1616.", ["Nació en el s. XVI.", "Murió en 1616."]),
        ("Está en la c/ Mayor, n.º 5, 2.º izq. Llama antes.", ["Está en la c/ Mayor, n.º 5, 2.º izq.", "Llama antes."]),
        ("p. ej. el agua. Otro caso es el aire.", ["p. ej. el agua.", "Otro caso es el aire."]),
        ("Fue en el 300 a. C. en Grecia. Después vino Roma.", ["Fue en el 300 a. C. en Grecia.", "Después vino Roma."]),
        ("D. Juan y D.ª Inés. Fin.", ["D. Juan y D.ª Inés.", "Fin."]),
        ("La Avda. de América está cerca. Vamos.", ["La Avda. de América está cerca.", "Vamos."]),
        ("Ángel llegó. Óscar se fue. Úrsula sí.", ["Ángel llegó.", "Óscar se fue.", "Úrsula sí."]),
        ("—¿Vienes? —No. —¡Qué pena!", ["—¿Vienes?", "—No.", "—¡Qué pena!"]),
        ("«No lo sé», dijo. Nadie respondió.", ["«No lo sé», dijo.", "Nadie respondió."]),
        ("Costó 3,5 millones. Pagó 1.200 € en total.", ["Costó 3,5 millones.", "Pagó 1.200 € en total."]),
        # a lone letter or an acronym before the period is not an abbreviation
        ("El valor de n. Por tanto, sigue.", ["El valor de n.", "Por tanto, sigue."]),
        ("Los miembros del PP. Luego votaron.", ["Los miembros del PP.", "Luego votaron."]),
    ],
)  # fmt: skip
def test_spanish_sentences(text: str, expected: list[str]) -> None:
    assert _es(text) == expected
    assert [text[s:e] for s, e in sentences(text, "es-MX")] == expected


def test_default_language_is_unchanged() -> None:
    text = "Dr. Smith went home. He was tired. See p. 5 for details. It costs $3.5 million."
    english = sentences(text)
    assert [text[s:e] for s, e in english] == [
        "Dr. Smith went home.", "He was tired.", "See p. 5 for details.", "It costs $3.5 million.",
    ]  # fmt: skip
    for language in (None, "en", "en-GB", "fr"):
        assert sentences(text, language) == english
    # Spanish abbreviations are only understood when the language says so
    spanish = "El Sr. García llegó tarde."
    assert len(sentences(spanish)) == 2 and len(sentences(spanish, "es")) == 1


def test_long_spanish_text_is_windowed_without_losing_text() -> None:
    para = (
        "El Sr. García llegó a las 12:30 h. ¿Qué pasó después? ¡Nadie lo sabe! "
        "Óscar, según dicen, vio a la Dra. López en EE. UU. durante el s. XX. Ella no dijo nada. "
    )
    text = para * 60  # several pySBD windows
    spans = sentences(text, "es")
    assert len(spans) == 5 * 60
    pos = 0
    for s, e in spans:
        assert not text[pos:s].strip() and text[s:e] == text[s:e].strip()
        pos = e
    assert not text[pos:].strip()
    assert {text[s:e] for s, e in spans} == {
        "El Sr. García llegó a las 12:30 h.", "¿Qué pasó después?", "¡Nadie lo sabe!",
        "Óscar, según dicen, vio a la Dra. López en EE. UU. durante el s. XX.", "Ella no dijo nada.",
    }  # fmt: skip


def test_chunk_text_takes_the_language() -> None:
    text = "El Sr. García llegó tarde. La Sra. López ya estaba allí. ¿Y ahora qué? Nadie dijo nada más aquella tarde."
    first = chunk_text(text, first_single=True, language="es")[0]
    assert text[first[0] : first[1]] == "El Sr. García llegó tarde."
    assert text[slice(*chunk_text(text, first_single=True)[0])] == "El Sr."  # English rules cut after "Sr."
    joined = [text[s:e] for s, e in chunk_text(text, language="es")]
    assert "".join(joined).replace(" ", "") == text.replace(" ", "")


def test_words_cover_spanish_letters() -> None:
    text = "¿Cómo estás, señor Núñez? El pingüino añadió 3,5 kg… ¡Ay!"
    expected = ["Cómo", "estás", "señor", "Núñez", "El", "pingüino", "añadió", "3,5", "kg", "Ay"]
    assert [text[s:e] for s, e in words(text)] == expected
    assert [m.group(0) for m in WORD_RE.finditer(text)] == expected
