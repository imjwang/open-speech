"""Reply text made for the ear and cut into units as it streams (service/speakable.py, 07 § Reply speech)."""

from __future__ import annotations

from open_speech.service.speakable import (
    FIRST_WAIT_S,
    IDLE_ANY_S,
    IDLE_DONE_S,
    MAX_CHARS,
    Splitter,
    speakable,
)


def stream(text: str, *, language: str = "en", step: float = 0.002, chunk: int = 1) -> list[str]:
    """Feed `text` the way a model streams it (a few characters at a time, quickly), then finish."""
    sp = Splitter(language)
    out: list[str] = []
    now = 0.0
    for i in range(0, len(text), chunk):
        now += step
        out += sp.feed(text[i : i + chunk], now)
    return out + sp.finish()


# ------------------------------------------------------------------ what is said


def test_markdown_is_dropped_and_lines_end_with_a_pause() -> None:
    said = speakable("**Heart** is the *warmest* voice, `af_heart`.\n## Others\n- Bella\n- Emma 🎉\n1. Nova", "en")
    assert said == "Heart is the warmest voice, af_heart. Others. Bella. Emma. Nova"


def test_links_read_as_a_word_in_the_reply_language() -> None:
    assert speakable("See https://example.com/a.b?c=1, or [the docs](https://x.org/d).", "en") == (
        "See link, or the docs."
    )
    assert speakable("Más en www.ejemplo.es/guia.", "es") == "Más en enlace."


def test_identifiers_with_underscores_are_left_alone() -> None:
    assert speakable("Use af_heart or bf_emma, 2 * 3 = 6.", "en") == "Use af_heart or bf_emma, 2 * 3 = 6."


# ------------------------------------------------------------------ where it is cut


def test_abbreviations_and_decimals_are_never_cut_while_the_next_word_arrives() -> None:
    units = stream("Call Dr. Smith about the 3.5 percent figure, e.g. tomorrow. Then rest for a while.")
    assert units == ["Call Dr. Smith about the 3.5 percent figure, e.g. tomorrow.", "Then rest for a while."]
    # Fed one word at a time with the boundary arriving last, "Dr." still holds.
    sp = Splitter("en")
    assert sp.feed("Please call Dr.", 0.0) == []
    assert sp.feed(" ", 0.01) == []
    assert sp.feed("Smith now", 0.02) == []
    assert sp.finish() == ["Please call Dr. Smith now"]


def test_spanish_openers_and_abbreviations() -> None:
    units = stream("¿Qué tal está usted, Sra. García? ¡Muy bien, gracias por preguntar! Ver p. ej. la guía.",
                   language="es")
    assert units == [
        "¿Qué tal está usted, Sra. García?",
        "¡Muy bien, gracias por preguntar!",
        "Ver p. ej. la guía.",
    ]


def test_a_link_is_one_word_and_its_punctuation_is_not_a_sentence_end() -> None:
    units = stream("The full list is at https://example.com/voices?lang=en.sort=1 for now. It is short, too.")
    assert units == ["The full list is at link for now.", "It is short, too."]


def test_short_sentences_merge_with_the_next() -> None:
    units = stream("This first sentence is long enough. Yes. No. Maybe so, then we can go.")
    assert units == ["This first sentence is long enough.", "Yes. No. Maybe so, then we can go."]


def test_markdown_list_items_become_sentences() -> None:
    units = stream("Here are three voices for you:\n- **Heart**, warm\n- Bella\n- Emma, British\n")
    assert units == ["Here are three voices for you:", "Heart, warm. Bella. Emma, British."]


def test_a_short_first_sentence_waits_300_ms_for_the_next_then_goes_alone() -> None:
    sp = Splitter("en")
    assert sp.feed("Sure. Let ", 0.0) == []  # "Sure." is confirmed by the next word, but short
    assert sp.feed("me look ", 0.1) == []
    assert sp.feed("that up ", 0.2) == []
    assert sp.deadline() == FIRST_WAIT_S
    assert sp.feed("for ", FIRST_WAIT_S - 0.01) == []
    assert sp.feed("you", FIRST_WAIT_S + 0.01) == ["Sure."]
    assert sp.finish() == ["Let me look that up for you"]


def test_a_short_first_sentence_merges_when_the_next_arrives_in_time() -> None:
    sp = Splitter("en")
    assert sp.feed("Sure. Here is what I found for you. Next one", 0.0) == ["Sure. Here is what I found for you."]


def test_a_short_first_sentence_goes_alone_when_the_next_is_long() -> None:
    long_second = "This second sentence is long, " * 5 + "and it ends here."
    sp = Splitter("en")
    assert sp.feed(f"Sure. {long_second} And", 0.0)[0] == "Sure."


def test_the_first_unit_is_cut_at_a_clause_when_no_sentence_ends_early() -> None:
    text = ("When you open a page from the web, the reader keeps its layout and its links, and it reads the "
            "words in order while the highlight follows along, skipping the parts you asked it to skip.")
    sp = Splitter("en")
    out: list[str] = []
    for i, ch in enumerate(text):
        out += sp.feed(ch, i * 0.001)
        if out:
            break
    first = out[0]
    assert first == "When you open a page from the web, the reader keeps its layout and its links,"
    assert text.startswith(first)
    rest = sp.feed(text[i + 1 :], 1.0) + sp.finish()
    assert " ".join([first, *rest]) == text


def test_a_long_first_sentence_given_whole_starts_at_its_first_long_clause() -> None:
    sp = Splitter("en")
    text = ("You can say: read my clipboard, pause, faster, two x, next paragraph, go to page twelve, where am I, "
            "skip footnotes, say SQL as sequel, show voices, dark theme, speak Spanish, or how much have I spent.")
    units = sp.feed(text, 0.0) + sp.finish()
    assert units[0] == "You can say: read my clipboard, pause, faster,"
    assert " ".join(units) == text
    sp = Splitter("en")  # also when it is complete, with more after it
    units = sp.feed(text + " Or just ask.", 0.0) + sp.finish()
    assert units[0] == "You can say: read my clipboard, pause, faster," and units[-1].endswith("Or just ask.")


def test_no_unit_is_longer_than_300_characters() -> None:
    text = " ".join(["word"] * 400) + "."
    units = stream(text, chunk=7)
    assert all(len(u) <= MAX_CHARS for u in units) and len(units) >= 7
    assert " ".join(units) == text


def test_a_pause_in_the_text_says_what_is_complete_then_the_rest() -> None:
    sp = Splitter("en")
    assert sp.feed("I will look it up now.", 0.0) == []  # complete-looking, but the boundary isn't confirmed
    assert sp.deadline() == IDLE_DONE_S
    assert sp.ready(IDLE_DONE_S + 0.01) == ["I will look it up now."]  # the model went off to run a tool
    assert sp.feed(" Checking the queue", 1.0) == []
    assert sp.ready(1.0 + IDLE_DONE_S + 0.01) == []  # not a sentence yet
    assert sp.ready(1.0 + IDLE_ANY_S + 0.01) == ["Checking the queue"]


def test_flush_before_a_sample_says_what_was_written_first() -> None:
    sp = Splitter("en")
    assert sp.feed("Here's Kore", 0.0) == []
    assert sp.flush(0.05, partial=True) == ["Here's Kore"]
    assert sp.feed("Want it?", 0.1) == []
    assert sp.finish() == ["Want it?"]


def test_finish_says_the_rest_and_nothing_twice() -> None:
    sp = Splitter("en")
    out = sp.feed("Paused.", 0.0)
    out += sp.finish()
    assert out == ["Paused."] and sp.finish() == [] and not sp.pending()
