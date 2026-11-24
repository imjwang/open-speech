"""The Spanish fast-path grammar, and the commands added with themes, languages, attachments and the queue.

Everything here is answered without the assistant, so it works in a build that has no assistant at all.
"""

from __future__ import annotations

import pytest

from open_speech.agent.intents import parse

ES: list[tuple[str, str | None, dict[str, object]]] = [
    # transport
    ("pausa", "pause", {}),
    ("Pausa.", "pause", {}),
    ("para", "stop", {}),
    ("detente", "stop", {}),
    ("sigue", "resume", {}),
    ("continúa", "resume", {}),
    ("reproduce", "play", {}),
    ("más rápido", "change_speed", {"delta": 0.25}),
    ("un poco más lento", "change_speed", {"delta": -0.25}),
    ("velocidad 2", "set_speed", {"speed": 2.0}),
    ("velocidad uno coma cinco", "set_speed", {"speed": 1.5}),
    ("pon la velocidad a 1,8", "set_speed", {"speed": 1.8}),
    ("siguiente", "skip", {"unit": "sentence", "count": 1}),
    ("atrás", "skip", {"unit": "sentence", "count": -1}),
    ("siguiente párrafo", "skip", {"unit": "paragraph", "count": 1}),
    ("párrafo anterior", "skip", {"unit": "paragraph", "count": -1}),
    ("siguiente título", "skip", {"unit": "heading", "count": 1}),
    ("retrocede 10 segundos", "skip", {"unit": "seconds", "count": -10}),
    ("avanza treinta segundos", "skip", {"unit": "seconds", "count": 30}),
    ("ve a la página 12", "go_to_page", {"page": 12}),
    ("¿dónde estoy?", "where_am_i", {}),
    ("cuánto queda", "where_am_i", {}),
    # reading
    ("lee el portapapeles", "read_clipboard", {"image_mode": "auto"}),
    ("describe lo que hay en el portapapeles", "read_clipboard", {"image_mode": "describe"}),
    ("siguiente documento", "next_document", {}),
    ("vacía la cola", "clear_queue", {}),
    # lists, by sentence or by bare name
    ("muestra las voces", "show", {"what": "voices"}),
    ("qué voces hay", "show", {"what": "voices"}),
    ("voces", "show", {"what": "voices"}),
    ("muestra las reglas", "show", {"what": "rules"}),
    ("muestra la cola", "show", {"what": "queue"}),
    ("muestra los ajustes", "show", {"what": "settings"}),
    ("ajustes", "show", {"what": "settings"}),
    ("cuánto he gastado", "usage", {"period": "month"}),
    ("¿cuánto llevo gastado este mes?", "usage", {"period": "month"}),
    # settings
    ("tema oscuro", "set_theme", {"theme": "ember"}),
    ("tema claro", "set_theme", {"theme": "lavender"}),
    ("pon el tema lavanda", "set_theme", {"theme": "lavender"}),
    ("usa el tema del sistema", "set_theme", {"theme": "system"}),
    ("sigue el sistema", "set_theme", {"theme": "system"}),
    ("cambia de tema", "set_theme", {"theme": "toggle"}),
    ("habla en inglés", "set_language", {"language": "en"}),
    ("cambia el idioma a español", "set_language", {"language": "es"}),
    ("español", "set_language", {"language": "es"}),
    ("en inglés", "set_language", {"language": "en"}),
    ("letra más grande", "text_size", {"delta": 2}),
    ("texto más pequeño", "text_size", {"delta": -2}),
    ("pon mi presupuesto en 20 dólares", "set_budget", {"monthly_usd": "20"}),
    ("presupuesto de 15", "set_budget", {"monthly_usd": "15"}),
    # rules and pronunciations
    ("abre las reglas", "open_rules", {"item": None}),
    ("edita las reglas", "open_rules", {"item": None}),
    ("abre las pronunciaciones", "open_rules", {"item": "lexicon"}),
    ("abre mis instrucciones de lectura", "open_rules", {"item": "preferences"}),
    ("di SQL como sequel", "lexicon", {"term": "SQL", "spoken": "sequel"}),
    ("pronuncia Nguyen como güin", "lexicon", {"term": "Nguyen", "spoken": "güin"}),
    ("olvida la pronunciación de SQL", "remove_pronunciation", {"term": "sql"}),
    ("olvida cómo se dice SQL", "remove_pronunciation", {"term": "sql"}),
    # open-ended requests go to the assistant
    ("resume este artículo", None, {}),
    ("qué opinas de este texto", None, {}),
    # what is read everywhere is the reading instructions' business: the assistant edits them
    ("deja de leer las notas al pie", None, {}),
    ("no leas los encabezados", None, {}),
    ("vuelve a leer las citas", None, {}),
    ("modo inteligente", None, {}),
    # a rule limited to this site or this document is a card
    ("omite las tablas en este sitio", "propose_rule",
     {"instruction": "omite las tablas en este sitio", "scope_hint": "site"}),
    ("vuelve a leer las citas en este documento", "propose_rule",
     {"instruction": "vuelve a leer las citas en este documento", "scope_hint": "document"}),
    ("salta al final en este documento", None, {}),
]

EN_NEW: list[tuple[str, str | None, dict[str, object]]] = [
    ("dark theme", "set_theme", {"theme": "ember"}),
    ("light theme", "set_theme", {"theme": "lavender"}),
    ("use the lavender theme", "set_theme", {"theme": "lavender"}),
    ("switch to ember", "set_theme", {"theme": "ember"}),
    ("match the system theme", "set_theme", {"theme": "system"}),
    ("toggle the theme", "set_theme", {"theme": "toggle"}),
    ("speak spanish", "set_language", {"language": "es"}),
    ("switch to english", "set_language", {"language": "en"}),
    ("Spanish", "set_language", {"language": "es"}),
    ("in English", "set_language", {"language": "en"}),
    ("bigger text", "text_size", {"delta": 2}),
    ("make the text smaller", "text_size", {"delta": -2}),
    ("set my budget to $20", "set_budget", {"monthly_usd": "20"}),
    ("budget of $15 a month", "set_budget", {"monthly_usd": "15"}),
    ("next document", "next_document", {}),
    ("clear the queue", "clear_queue", {}),
    ("show the queue", "show", {"what": "queue"}),
    ("show settings", "show", {"what": "settings"}),
    ("settings", "show", {"what": "settings"}),
    ("voices", "show", {"what": "voices"}),
    ("show rules", "show", {"what": "rules"}),
    ("open the rules", "open_rules", {"item": None}),
    ("edit my rules", "open_rules", {"item": None}),
    ("open my pronunciations", "open_rules", {"item": "lexicon"}),
    ("edit my preferences", "open_rules", {"item": "preferences"}),
    ("forget how to say SQL", "remove_pronunciation", {"term": "sql"}),
    ("forget the pronunciation of SQL", "remove_pronunciation", {"term": "sql"}),
    ("budget options", None, {}),
]


@pytest.mark.parametrize(("text", "kind", "params"), ES, ids=[c[0] for c in ES])
def test_spanish_commands(text: str, kind: str | None, params: dict[str, object]) -> None:
    intent = parse(text, language="es")
    assert (intent.kind if intent else None) == kind
    if intent:
        assert intent.params == params


@pytest.mark.parametrize(("text", "kind", "params"), EN_NEW, ids=[c[0] for c in EN_NEW])
def test_english_settings_and_queue_commands(text: str, kind: str | None, params: dict[str, object]) -> None:
    intent = parse(text, language="en")
    assert (intent.kind if intent else None) == kind
    if intent:
        assert intent.params == params


@pytest.mark.parametrize("text", ["sí", "vale", "de acuerdo", "hazlo"])
def test_spanish_yes_only_confirms_a_pending_card(text: str) -> None:
    assert parse(text, language="es") is None
    intent = parse(text, language="es", has_pending_action=True)
    assert intent is not None and intent.kind == "confirm"


@pytest.mark.parametrize("text", ["no", "cancela", "mejor no"])
def test_spanish_no_only_cancels_a_pending_card(text: str) -> None:
    assert parse(text, language="es") is None
    intent = parse(text, language="es", has_pending_action=True)
    assert intent is not None and intent.kind == "cancel"


ATTACHED = [
    ("en", "read these"),
    ("en", "read them"),
    ("en", "read this file"),
    ("en", "read these files"),
    ("en", "read this pdf"),
    ("en", "read me these links"),
    ("en", "read both"),
    ("en", "read all three pages"),
    ("en", "open the document"),
    ("en", "read this page aloud"),
    ("es", "lee esto"),
    ("es", "léelos"),
    ("es", "lee este archivo"),
    ("es", "lee estos archivos"),
    ("es", "léeme estos enlaces"),
    ("es", "lee las tres páginas"),
    ("es", "abre el documento"),
    ("es", "lee esto en voz alta"),
]


@pytest.mark.parametrize(("language", "text"), ATTACHED, ids=[c[1] for c in ATTACHED])
def test_read_this_means_the_attachments_only_when_something_is_attached(language: str, text: str) -> None:
    """A dropped PDF plus "read this file" must work without the assistant; with nothing attached the same
    words are not a fast-path command (the assistant, or the reader's context, decides)."""
    with_files = parse(text, language=language, has_attachments=True)
    assert with_files is not None and with_files.kind == "read_attachments"
    without = parse(text, language=language)
    assert without is None or without.kind != "read_attachments"


@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "summarize this file"),
        ("en", "read the first one"),
        ("es", "resume este archivo"),
        ("es", "lee el primer archivo"),
    ],
)
def test_anything_more_specific_about_attachments_goes_to_the_assistant(language: str, text: str) -> None:
    assert parse(text, language=language, has_attachments=True) is None


def test_each_language_understands_the_other() -> None:
    """The interface language is tried first, but a Spanish speaker saying "pause" (or the reverse) still works."""
    assert parse("pause", language="es").kind == "pause"  # type: ignore[union-attr]
    assert parse("pausa", language="en").kind == "pause"  # type: ignore[union-attr]
    assert parse("tema oscuro", language="en").params == {"theme": "ember"}  # type: ignore[union-attr]


# ------------------------------------------------------------------ one-word commands as a recognizer hears them


@pytest.mark.parametrize(
    ("heard", "kind"),
    [
        ("Cause.", "pause"),
        ("calls", "pause"),
        ("Hose", "pause"),
        ("holds", "pause"),
        ("Clay.", "play"),
        ("pauza", "pause"),
        ("Pau.", "pause"),
    ],
)
def test_measured_mishearings_of_one_word_commands_still_work_when_spoken(heard: str, kind: str) -> None:
    intent = parse(heard, spoken=True)
    assert intent is not None and intent.kind == kind
    assert parse(heard) is None, "typed text is taken as written"


def test_slow_means_slower_when_spoken() -> None:
    assert parse("slow", spoken=True).params == {"delta": -0.25}  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("heard", "kind"),
    [("Now.", "cancel"), ("know", "cancel"), ("See.", "confirm"), ("C", "confirm"), ("si", "confirm")],
)
def test_misheard_yes_and_no_count_only_while_a_card_is_waiting(heard: str, kind: str) -> None:
    intent = parse(heard, spoken=True, has_pending_action=True)
    assert intent is not None and intent.kind == kind
    assert parse(heard, spoken=True) is None


def test_a_misheard_word_inside_a_sentence_is_left_alone() -> None:
    assert parse("what was the cause", spoken=True) is None
    assert parse("see the next page", spoken=True) is None
