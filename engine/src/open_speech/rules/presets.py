"""Built-in presets (06 § Built-in presets).

The preset *rules* are ordinary rule files in `open_speech/data/presets/*.md`, copied to `rules/` on first
run. This table carries what the files don't: the Speechify display name, the Classic / AI Enhanced
descriptions for the presets panel (`PresetOut`), whether the preset joins the annotate tier in
AI Enhanced mode, and its default state.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import TYPE_CHECKING

from open_speech.api.schemas import PresetOut

if TYPE_CHECKING:
    from open_speech.rules.model import Rule


@dataclass(frozen=True)
class PresetSpec:
    name: str  # file / rule name
    title: str  # Speechify toggle name
    classic: str
    ai_enhanced_adds: str  # "—" when AI Enhanced adds nothing
    ai_annotate: bool  # sends its instruction to the annotate tier in AI Enhanced mode
    default_enabled: bool


PRESETS: tuple[PresetSpec, ...] = (
    PresetSpec("headers", "Headers", "drop_blocks: [header]", "Catches running heads the heuristic missed", True, True),
    PresetSpec("footers", "Footers", "drop_blocks: [footer]", "—", False, True),
    PresetSpec(
        "page-numbers", "Page numbers", "drop_blocks: [page_number]", 'Inline "Page 4 of 18" fragments', True, True
    ),
    PresetSpec(
        "footnotes",
        "Footnotes",
        "drop_blocks: [footnote], drop_marks: [footnote_ref]",
        "Endnote markers in odd formats",
        True,
        True,
    ),
    PresetSpec(
        "citations",
        "Citations",
        "drop_marks: [citation_ref]",
        "Author-year forms, keeping names that are subjects",
        True,
        True,
    ),
    PresetSpec(
        "urls",
        "URLs",
        "drop_marks: [url]",
        'Reads a short domain when the link text is the URL ("see example.org")',
        True,
        True,
    ),
    PresetSpec(
        "parentheses", "Parentheses", "drop_marks: [parenthetical]", "Keeps parentheses that carry meaning", True, False
    ),
    PresetSpec("brackets", "Brackets", "drop_marks: [bracket]", "—", False, False),
    PresetSpec("braces", "Braces", "drop_marks: [brace]", "—", False, False),
    PresetSpec(
        "tables", "Tables", "drop_blocks: [table]", "Optional one-sentence table summary (transform)", False, False
    ),
    PresetSpec(
        "formulas",
        "Formulas",
        "drop_blocks: [equation], drop_marks: [formula]",
        'Optional "explain in words" (transform)',
        False,
        False,
    ),
    PresetSpec("captions", "Captions", "drop_blocks: [caption]", "—", False, False),
    PresetSpec("code", "Code", "drop_blocks: [code]", "Optional one-line summary of each code block", False, False),
)

_BY_KEY: dict[str, PresetSpec] = {}
for _p in PRESETS:
    for _k in (_p.name, _p.title.lower(), _p.title.lower().replace(" ", "-"), f"rule_{_p.name}"):
        _BY_KEY[_k] = _p


def preset_spec(name_or_id: str) -> PresetSpec | None:
    """Look up by file name, rule id or display name ("Page numbers", "page-numbers", "rule_page-numbers")."""
    key = name_or_id.strip().lower()
    return _BY_KEY.get(key) or _BY_KEY.get(key.replace(" ", "-")) or _BY_KEY.get(key.replace("_", "-"))


def preset_dir() -> Traversable:
    return files("open_speech").joinpath("data", "presets")


def preset_out(rule: Rule) -> PresetOut:
    spec = preset_spec(rule.name)
    return PresetOut(
        id=rule.id,
        name=spec.title if spec else rule.name,
        enabled=rule.enabled,
        classic=spec.classic if spec else (rule.description or ""),
        ai_enhanced_adds=spec.ai_enhanced_adds if spec else "—",
    )


def preset_order(rule: Rule) -> int:
    for i, p in enumerate(PRESETS):
        if p.name == rule.name:
            return i
    return len(PRESETS)
