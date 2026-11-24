"""How `edit_prompt` edits the reading instructions (SPEECH.md): the smallest change, in the right place."""

from __future__ import annotations

from open_speech.agent.tools import _add_to_section, _find_once, _replace_once

SPEECH = """\
# Reading instructions

Prepare each document so it sounds like a person reading it to me.

## Leave out
- Skip headers, footers and page numbers.
- Skip footnotes and footnote markers.
- Skip URLs. When a link's text is the address itself, say only the short domain.
<!--
More lines you can add to "Leave out" (move one above this comment):
- Skip captions.
-->

## Read with care
- Read tables, formulas and code as they appear.

## About me
<!-- Anything that helps. -->
"""


def test_a_new_line_joins_the_end_of_its_section_above_the_note() -> None:
    out = _add_to_section(SPEECH, "leave out", "- Skip captions.")  # headings match without regard to case
    lines = out.splitlines()
    i = lines.index("- Skip captions.")
    assert lines[i - 1].startswith("- Skip URLs.")
    assert lines[i + 1] == "<!--", "after the section's last line, not after the note that closes it"
    assert out.count("- Skip captions.") == 2  # the commented example is untouched
    assert out.replace("- Skip captions.\n", "", 1) == SPEECH, "nothing else changes"


def test_a_line_goes_before_the_next_heading_and_at_the_end_of_the_file() -> None:
    out = _add_to_section(SPEECH, "Read with care", "- Read drug names slowly.")
    assert "- Read tables, formulas and code as they appear.\n- Read drug names slowly.\n\n## About me" in out
    out = _add_to_section(SPEECH, "About me", "I'm a nurse.")
    assert out.endswith("## About me\nI'm a nurse.\n<!-- Anything that helps. -->\n")


def test_a_missing_section_is_added_at_the_end() -> None:
    out = _add_to_section(SPEECH, "Pronunciation", "- Say the letters of acronyms.")
    assert out == SPEECH.rstrip() + "\n\n## Pronunciation\n- Say the letters of acronyms.\n"
    assert _add_to_section("", "Leave out", "- Skip tables.") == "## Leave out\n- Skip tables.\n"


def test_an_exact_quote_is_replaced() -> None:
    out = _replace_once(SPEECH, "- Skip footnotes and footnote markers.", "- Skip footnote markers only.")
    assert "- Skip footnote markers only.\n- Skip URLs." in out
    assert "Skip footnotes and" not in out


def test_deleting_a_line_takes_its_line_break_with_it() -> None:
    out = _replace_once(SPEECH, "- Skip footnotes and footnote markers.", "")
    assert "- Skip headers, footers and page numbers.\n- Skip URLs." in out
    # A quote without its bullet still deletes the whole line, not leaving a stray "- ".
    out = _replace_once(SPEECH, "Skip headers, footers and page numbers.", "")
    assert "## Leave out\n- Skip footnotes" in out
    # Part of a line is only that part.
    out = _replace_once(SPEECH, " When a link's text is the address itself, say only the short domain.", "")
    assert "- Skip URLs.\n<!--" in out


def test_a_quote_must_occur_once_but_notes_for_the_reader_dont_count() -> None:
    assert _find_once(SPEECH, "- Skip the weather.") == (None, 0)
    assert _find_once(SPEECH, "- Skip") == (None, 4)  # ambiguous: the model is asked for an exact quote
    added = _add_to_section(SPEECH, "Leave out", "- Skip captions.")
    at, found = _find_once(added, "- Skip captions.")  # once in "Leave out", once in its note
    assert found == 2 and at is not None
    out = _replace_once(added, "- Skip captions.", "", at)
    assert out == SPEECH, "the line the user added goes; the example in the note stays"
