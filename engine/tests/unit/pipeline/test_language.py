"""Which language a document is in (document/language.py): it picks the voice and the sentence rules."""

from __future__ import annotations

from open_speech.document.language import detect_language, language_of_blocks

ENGLISH = (
    "The committee met on Tuesday to review the proposal. It was agreed that the work should continue, "
    "and that a report would be sent to the board before the end of the month."
)
SPANISH = (
    "El comité se reunió el martes para revisar la propuesta. Se acordó que el trabajo debía continuar "
    "y que se enviaría un informe a la junta antes de fin de mes."
)


def test_english_and_spanish_prose() -> None:
    assert detect_language(ENGLISH) == "en"
    assert detect_language(SPANISH) == "es"


def test_spanish_without_accents_is_still_spanish() -> None:
    assert detect_language("El gato de la casa es muy grande y los perros no lo quieren para nada en el patio") == "es"


def test_too_little_to_tell_is_unknown() -> None:
    assert detect_language("") is None
    assert detect_language("42 17 2026") is None
    assert detect_language("Kubernetes") is None
    assert detect_language("x = y + 1") is None


def test_code_and_names_do_not_look_like_a_language() -> None:
    assert detect_language("def main(): return os.path.join(base, name)") is None


def test_a_document_is_judged_by_how_it_starts() -> None:
    # Only the first pages are sampled: a long Spanish report with an English appendix is Spanish.
    blocks = ["Informe anual"] + [SPANISH] * 90 + [ENGLISH] * 500
    assert language_of_blocks(blocks) == "es"
    assert language_of_blocks([]) is None
    assert language_of_blocks(iter([ENGLISH] * 2000)) == "en"  # stops reading early, even for a generator


def test_a_quotation_in_another_language_does_not_flip_the_document() -> None:
    mostly_english = [ENGLISH, ENGLISH, "As the saying goes: «el que no arriesga no gana».", ENGLISH]
    assert language_of_blocks(mostly_english) == "en"
