from __future__ import annotations

import pytest

from open_speech.document.segment import sentences, syllables, words


def _texts(text: str) -> list[str]:
    return [text[s:e] for s, e in sentences(text)]


def _assert_spans_valid(text: str) -> None:
    spans = sentences(text)
    prev_end = 0
    for s, e in spans:
        assert 0 <= prev_end <= s < e <= len(text)
        frag = text[s:e]
        assert frag == frag.strip(), "spans are whitespace-trimmed"
        assert not text[prev_end:s].strip(), "no non-whitespace text is skipped"
        prev_end = e
    assert not text[prev_end:].strip()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Smith et al. showed this in 2020. The value was 3.14 m. Next sentence here.",
            ["Smith et al. showed this in 2020.", "The value was 3.14 m.", "Next sentence here."],
        ),
        (
            "We use e.g. apples and i.e. oranges. Dr. Lee agreed with Prof. Kim.",
            ["We use e.g. apples and i.e. oranges.", "Dr. Lee agreed with Prof. Kim."],
        ),
        (
            "Prices rose 2.5% in Q3. They fell 0.75 points later.",
            ["Prices rose 2.5% in Q3.", "They fell 0.75 points later."],
        ),
        ("No terminal punctuation", ["No terminal punctuation"]),
        (
            "Results improved [12]. Others disagree (Smith et al., 2020). Done.",
            ["Results improved [12].", "Others disagree (Smith et al., 2020).", "Done."],
        ),
    ],
)
def test_sentences_abbreviation_aware(text: str, expected: list[str]) -> None:
    assert _texts(text) == expected
    _assert_spans_valid(text)


def test_sentence_spans_are_exact_with_irregular_whitespace() -> None:
    text = "  First one.   Second\tone!\n\nThird one?  Trailing "
    assert _texts(text) == ["First one.", "Second\tone!", "Third one?", "Trailing"]
    _assert_spans_valid(text)


def test_sentences_handle_repeated_sentences() -> None:
    text = "Yes. Yes. Yes. No."
    spans = sentences(text)
    assert [text[s:e] for s, e in spans] == ["Yes.", "Yes.", "Yes.", "No."]
    assert len({s for s, _ in spans}) == 4


def test_sentences_letterless_fragment_joins_previous() -> None:
    assert _texts("It works. [12].") == ["It works. [12]."]


def test_long_text_is_windowed_but_exact() -> None:
    text = " ".join(f"Sentence number {i} has e.g. a few words, et al. included." for i in range(400))
    spans = sentences(text)
    assert len(spans) == 400
    _assert_spans_valid(text)
    assert text[spans[-1][0] : spans[-1][1]].startswith("Sentence number 399")


def test_empty_text() -> None:
    assert sentences("") == []
    assert sentences("   \n ") == []


def test_words_unicode_boundaries() -> None:
    text = "Don't stop: state-of-the-art costs $3.14 or 1,000 Kč — naïve café_au_lait."
    got = [text[s:e] for s, e in words(text)]
    assert got == [
        "Don't",
        "stop",
        "state",
        "of",
        "the",
        "art",
        "costs",
        "3.14",
        "or",
        "1,000",
        "Kč",
        "naïve",
        "café_au_lait",
    ]


def test_words_offsets_slice_back() -> None:
    text = "αβγ test, ¹ résumé"
    for s, e in words(text):
        assert text[s:e].strip() == text[s:e]
    assert [text[s:e] for s, e in words(text)] == ["αβγ", "test", "¹", "résumé"]


@pytest.mark.parametrize(
    ("word", "n"),
    [
        ("the", 1),
        ("make", 1),
        ("table", 2),
        ("coffee", 2),
        ("played", 1),
        ("wanted", 2),
        ("SQL", 3),
        ("2020", 5),
        ("", 0),
        ("—", 0),
        ("internationalization", 8),
        ("café", 2),
    ],
)
def test_syllables(word: str, n: int) -> None:
    assert syllables(word) == n
