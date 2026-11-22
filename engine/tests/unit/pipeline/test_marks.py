from __future__ import annotations

import pytest

from open_speech.document.model import BlockType, MarkKind
from open_speech.ingest.marks import detect_marks, find_citations, find_urls


def _marks(text: str, **kw: object) -> list[tuple[str, str]]:
    return [(k.value, text[s:e]) for k, s, e in detect_marks(text, **kw)]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "cite"),
    [
        ("Results improved [12].", "[12]"),
        ("See prior work [3–5] for details.", "[3–5]"),
        ("See prior work [3-5] for details.", "[3-5]"),
        ("Two studies [1, 2] agree.", "[1, 2]"),
        ("Grouped [1], [2] together.", "[1], [2]"),
        ("Ranges [4]-[6] merge.", "[4]-[6]"),
        ("Alpha style [SKL20, Lam94] too.", "[SKL20, Lam94]"),
        ("Others disagree (Smith et al., 2020).", "(Smith et al., 2020)"),
        ("Others disagree (Smith, 2020; Lee 2019).", "(Smith, 2020; Lee 2019)"),
        ("As shown (e.g., Smith and Jones 2018, p. 4).", "(e.g., Smith and Jones 2018, p. 4)"),
        ("As shown (Lee & Kim, 2021a, 2021b).", "(Lee & Kim, 2021a, 2021b)"),
        ("Dutch names (van der Berg, 2019) work.", "(van der Berg, 2019)"),
    ],
)
def test_citations(text: str, cite: str) -> None:
    assert ("citation_ref", cite) in _marks(text)


def test_narrative_citation_drops_only_the_year() -> None:
    text = "Smith et al. (2020) showed that the pandemic (2020) was hard."
    marks = _marks(text)
    assert marks[0] == ("citation_ref", "(2020)")
    assert text.index("(2020)") == next(s for k, s, _ in detect_marks(text) if k == MarkKind.citation_ref)
    # The second "(2020)" follows a lowercase noun: a parenthetical, not a citation.
    kinds = [k for k, t in marks if t == "(2020)"]
    assert kinds == ["citation_ref", "parenthetical"]


def test_brackets_inside_merged_citations_are_not_separate_marks() -> None:
    marks = _marks("Grouped [1], [2] together.")
    assert marks == [("citation_ref", "[1], [2]")]


def test_parentheticals_brackets_braces() -> None:
    marks = _marks("Keep (this aside) and [an aside] and {braces here} but not ().")
    assert ("parenthetical", "(this aside)") in marks
    assert ("bracket", "[an aside]") in marks
    assert ("brace", "{braces here}") in marks
    assert all(t != "()" for _, t in marks)


def test_nested_parentheses_take_the_outer_span() -> None:
    marks = _marks("A note (with (nested) parts) here.")
    assert marks == [("parenthetical", "(with (nested) parts)")]


def test_urls_trim_trailing_punctuation_and_unbalanced_parens() -> None:
    text = "See https://example.org/a.b, or (https://en.wikipedia.org/wiki/Foo_(bar)). DOI 10.1000/xyz123."
    urls = [text[s:e] for s, e in find_urls(text)]
    assert urls == ["https://example.org/a.b", "https://en.wikipedia.org/wiki/Foo_(bar)", "10.1000/xyz123"]
    marks = _marks(text)
    assert ("parenthetical", "(https://en.wikipedia.org/wiki/Foo_(bar))") in marks


def test_www_urls() -> None:
    assert ("url", "www.example.com/x") in _marks("Visit www.example.com/x today.")


def test_formulas() -> None:
    marks = _marks("We set x = 3 and p < 0.05 with a² + b² = c² but well-known 3/4 remains. Also $E=mc^2$.")
    formulas = [t for k, t in marks if k == "formula"]
    assert formulas == ["x = 3", "p < 0.05", "a² + b² = c²", "$E=mc^2$"]


def test_superscript_footnote_refs_need_a_known_marker() -> None:
    text = "The results¹ were good² overall."
    assert _marks(text, footnote_markers={"1"}) == [("footnote_ref", "¹")]
    # Superscripts inside a formula are exponents, not footnote markers.
    assert all(k != "footnote_ref" for k, _ in _marks("a² + b² = c²", footnote_markers={"2"}))


def test_structural_marks_are_kept() -> None:
    text = "word1 and more"
    marks = detect_marks(text, structural=[(MarkKind.footnote_ref, 4, 5), (MarkKind.emphasis, 10, 14)])
    assert (MarkKind.footnote_ref, 4, 5) in marks and (MarkKind.emphasis, 10, 14) in marks


def test_no_structure_marks_in_code_or_furniture() -> None:
    assert _marks("f(x) = [a, b] {c}", block_type=BlockType.code) == []
    assert _marks("Page 4 of 18 (draft)", block_type=BlockType.footer) == []
    assert _marks("see https://x.org/y", block_type=BlockType.code) == [("url", "https://x.org/y")]


def test_plain_prose_has_no_false_citations() -> None:
    assert find_citations("In 2020 (the year of the pandemic) many things changed.") == []
