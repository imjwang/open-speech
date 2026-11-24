"""The reading instructions (06 § The reading instructions): SPEECH.md is the one prompt that drives cleanup, its
skip lines also compile to filters (rules/prompt.py), and an earlier version's presets become lines of it."""

from __future__ import annotations

from pathlib import Path

from open_speech.config import Paths
from open_speech.document.model import BlockType, MarkKind
from open_speech.rules.files import INSTALLED_PRESETS_FILE, LEGACY_MODES_FILE, LEGACY_SPEECH_MD, RuleStore
from open_speech.rules.prompt import default_prompt, derive_rules


def _store(tmp_path: Path) -> tuple[RuleStore, Paths]:
    paths = Paths(tmp_path).ensure()
    return RuleStore(paths), paths


def test_default_instructions_skip_furniture_notes_citations_and_urls() -> None:
    rules, rest = derive_rules(default_prompt())
    drops = {b for r in rules for b in r.compiled.drop_blocks}  # type: ignore[union-attr]
    marks = {m for r in rules for m in r.compiled.drop_marks}  # type: ignore[union-attr]
    assert drops == {BlockType.header, BlockType.footer, BlockType.page_number, BlockType.footnote}
    assert marks == {MarkKind.footnote_ref, MarkKind.citation_ref, MarkKind.url}
    assert all(r.preset and r.id.startswith("speech_") and r.scope.model_dump() == {
        "sites": [], "types": [], "docs": [], "languages": []} for r in rules)
    # The whole line is the rule's instruction, so Claude gets its nuance ("keep the name").
    citations = next(r for r in rules if MarkKind.citation_ref in r.compiled.drop_marks)  # type: ignore[union-attr]
    assert "keep the name" in citations.instruction
    # Claude reads the rest: no comments, no lines that became filters, no heading left empty.
    assert "## Never change" in rest and "Never summarize" in rest
    assert "<!--" not in rest and "Skip citations" not in rest and "## Leave out" not in rest
    assert "## About me" not in rest  # only a comment under it


def test_only_skip_lines_become_filters() -> None:
    text = """## Leave out
- Skip captions in PDFs.
- Never read legal disclaimers.
- Read citations.
- Say SQL as sequel.
- Skip tables; summarize them in one sentence instead.
Skip formulas.
"""
    rules, rest = derive_rules(text)
    assert [(r.name, r.scope.types) for r in rules] == [("skip-captions-pdf", ["pdf"]), ("skip-tables", [])]
    assert rules[1].instruction == "Skip tables; summarize them in one sentence instead."
    for line in ("Never read legal disclaimers.", "Read citations.", "Say SQL as sequel.", "Skip formulas."):
        assert line in rest  # judgment, "read" lines, rewrites and prose are Claude's


def test_a_fresh_profile_gets_the_default_and_an_edit_takes_effect_on_reload(tmp_path: Path) -> None:
    store, paths = _store(tmp_path)
    store.ensure_defaults()
    assert paths.speech_md.read_text("utf-8") == default_prompt()
    assert len(store.derived_rules()) == 4 and store.all() == []  # filters live in memory, not as files
    paths.speech_md.write_text(default_prompt().replace("- Skip URLs.", "- Keep URLs."), "utf-8")
    store.reload()
    assert not any(MarkKind.url in r.compiled.drop_marks for r in store.derived_rules())  # type: ignore[union-attr]
    # An emptied file is the user's choice: it is not filled in again.
    paths.speech_md.write_text("", "utf-8")
    store.ensure_defaults()
    assert paths.speech_md.read_text("utf-8") == "" and store.derived_rules() == []


def _preset(paths: Paths, name: str, enabled: bool) -> None:
    (paths.rules / f"{name}.md").write_text(
        f"---\nname: {name}\nscope: {{}}\nenabled: {str(enabled).lower()}\ntier: filter\n"
        f"compiled: {{drop_marks: [url]}}\ncreated_by: preset\n---\nSkip it.\n",
        "utf-8",
    )


def test_presets_become_lines_of_the_instructions_on_upgrade(tmp_path: Path) -> None:
    store, paths = _store(tmp_path)
    paths.rules.mkdir(parents=True, exist_ok=True)
    _preset(paths, "citations", False)  # turned off
    _preset(paths, "parentheses", True)  # an optional one turned on
    _preset(paths, "headers", True)
    # footers was installed, then deleted by the user
    (paths.rules / INSTALLED_PRESETS_FILE).write_text("citations\nheaders\nparentheses\nfooters\n", "utf-8")
    (paths.rules / LEGACY_MODES_FILE).write_text('[[overrides]]\nmode = "classic"\n', "utf-8")
    mine = "---\nname: mine\ncreated_by: user\n---\nSkip captions on arxiv.org\n"
    (paths.rules / "mine.md").write_text(mine, "utf-8")
    paths.speech_md.write_text("I'm a nurse; read drug names carefully.\n", "utf-8")

    store.ensure_defaults()

    text = paths.speech_md.read_text("utf-8")
    assert "- Skip citations." not in text
    assert "- Skip headers and page numbers." in text  # footers were deleted, so they stay off
    leave_out = text.split("## Leave out", 1)[1].split("<!--", 1)[0]
    assert "- Skip text in parentheses." in leave_out
    assert text.rstrip().endswith("I'm a nurse; read drug names carefully.")
    assert sorted(p.name for p in paths.rules.iterdir()) == ["SPEECH.md", "lexicon.csv", "mine.md"]  # rule files stay
    assert [r.name for r in store.all()] == ["mine"]
    # Upgrading runs once: a later start leaves the file as the user keeps it.
    paths.speech_md.write_text("Read everything.\n", "utf-8")
    store.ensure_defaults()
    assert paths.speech_md.read_text("utf-8") == "Read everything.\n"


def test_the_old_explanatory_comment_is_not_kept_as_the_users_words(tmp_path: Path) -> None:
    store, paths = _store(tmp_path)
    paths.rules.mkdir(parents=True, exist_ok=True)
    (paths.rules / INSTALLED_PRESETS_FILE).write_text("", "utf-8")
    paths.speech_md.write_text(LEGACY_SPEECH_MD, "utf-8")
    store.ensure_defaults()
    assert paths.speech_md.read_text("utf-8") == default_prompt()
