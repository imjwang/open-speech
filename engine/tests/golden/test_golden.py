"""Golden corpus (10 § Testing strategy): "what is spoken" per fixture, without Claude, for three versions of the
reading instructions: the default, every optional skip line added, and no skip lines.

Snapshots live in tests/golden/snapshots/ and are reviewed on change. To regenerate after an intended
change: `OPEN_SPEECH_UPDATE_GOLDEN=1 uv run pytest tests/golden` and review the diff.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_speech.api.schemas import HtmlBlocksPayload
from open_speech.cache.extract import ExtractCache
from open_speech.config import Paths
from open_speech.document.model import Document
from open_speech.ingest import SourceInput, ingest
from open_speech.plan.planner import PlanContext, Planner
from open_speech.plan.script import SpeechScript
from open_speech.rules.files import RuleStore
from open_speech.rules.prompt import prompt_from_presets
from tests.golden import make_fixtures as fx

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("OPEN_SPEECH_UPDATE_GOLDEN") == "1"

CONFIGS = ("classic-default", "classic-all-on", "classic-all-off")


def _sources() -> dict[str, SourceInput]:
    return {
        "academic.pdf": SourceInput(kind="file", data=fx.academic_pdf(), filename="academic.pdf"),
        "report.pdf": SourceInput(kind="file", data=fx.report_pdf(), filename="report.pdf"),
        "scanned.pdf": SourceInput(kind="file", data=fx.scanned_pdf(), filename="scanned.pdf"),
        "footnotes.docx": SourceInput(kind="file", data=fx.footnotes_docx(), filename="footnotes.docx"),
        "news.html_blocks": SourceInput(
            kind="html_blocks", html_blocks=HtmlBlocksPayload.model_validate(fx.news_blocks())
        ),
        "notes.md": SourceInput(kind="file", data=fx.notes_md().encode(), filename="notes.md"),
    }


@pytest.fixture(scope="module")
def documents(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Document]:
    import asyncio

    paths = Paths(tmp_path_factory.mktemp("golden-home")).ensure()
    cache = ExtractCache(paths)

    async def run() -> dict[str, Document]:
        return {name: (await ingest(src, extract_cache=cache)).document for name, src in _sources().items()}

    return asyncio.run(run())


_ALL = ("headers", "footers", "page-numbers", "footnotes", "citations", "urls", "parentheses", "brackets", "braces",
        "tables", "formulas", "captions", "code")


def _store(tmp_path: Path, config: str) -> RuleStore:
    paths = Paths(tmp_path).ensure()
    store = RuleStore(paths)
    store.ensure_defaults()
    if config != "classic-default":
        paths.speech_md.write_text(prompt_from_presets(dict.fromkeys(_ALL, config == "classic-all-on")), "utf-8")
        store.reload()
    return store


def render(script: SpeechScript, doc: Document) -> str:
    lines = [f"# {doc.title} ({doc.kind.value}, {len(doc.blocks)} blocks) · {script.mode}"]
    if doc.flags:
        lines.append(f"# flags: {', '.join(doc.flags)}")
    for u in script.utterances:
        tag = "H" if u.is_heading else ("¶" if u.paragraph_start else " ")
        lines.append(f"{tag} {u.text}")
    lines.append("")
    lines.append(f"--- removed ({len(script.removed)})")
    for h in script.removed:
        orig = " ".join(h.original.split())
        lines.append(f"{h.rule_name} {h.action} {h.block_id}: {orig[:90]}")
    if script.warnings:
        lines.append("--- warnings")
        lines.extend(script.warnings)
    return "\n".join(lines) + "\n"


async def _plan(doc: Document, store: RuleStore) -> SpeechScript:
    ctx = PlanContext(
        document=doc, rules=store.plan_rules(doc), lexicon=store.lexicon(), speech_md=store.plan_speech_md()
    )
    return (await Planner(None).plan(ctx, None)).script


@pytest.mark.parametrize("config", CONFIGS)
@pytest.mark.parametrize(
    "name", ["academic.pdf", "report.pdf", "scanned.pdf", "footnotes.docx", "news.html_blocks", "notes.md"]
)
async def test_snapshot(name: str, config: str, documents: dict[str, Document], tmp_path: Path) -> None:
    doc = documents[name]
    script = await _plan(doc, _store(tmp_path, config))
    got = render(script, doc)
    path = SNAPSHOTS / f"{name}__{config}.txt"
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got, "utf-8")
        if not UPDATE:
            pytest.fail(f"snapshot {path.name} was missing and has been written; review and re-run")
    assert got == path.read_text("utf-8"), f"spoken output changed for {path.name}"


# ------------------------------------------------------------------ M1 exit criterion


async def test_two_column_paper_reads_without_furniture(documents: dict[str, Document], tmp_path: Path) -> None:
    doc = documents["academic.pdf"]
    script = await _plan(doc, _store(tmp_path, "classic-default"))
    spoken = " ".join(u.text for u in script.utterances)
    for furniture in ("Journal of Synthetic Results", "Lee and Park", "Page 1 of", "Page 2 of", "CC BY 4.0"):
        assert furniture not in spoken
    # Citations, footnotes and footnote markers are skipped by the default presets.
    for skipped in ("[1]", "[2, 3]", "(Smith et al., 2020)", "Code and data are available", "data1"):
        assert skipped not in spoken
    # Reading order: column by column, page by page, with the cross-column/page sentences intact.
    order = [
        "Listening to Papers",
        "We study how a speech engine",
        "Reading documents aloud is an old idea",
        "This paper makes three contributions",
        "We first group characters into lines",
        "Smith et al. showed that the gap between columns",
        "where h(g) is the height of the empty band",
        "Repeated page furniture is detected",
        "by their label and by their distance to the nearest figure or table on the page",
        "On our synthetic corpus",
        "Simple geometric heuristics",
    ]
    positions = [spoken.find(s) for s in order]
    assert all(p >= 0 for p in positions), [s for s, p in zip(order, positions, strict=True) if p < 0]
    assert positions == sorted(positions)
    assert "recognized" in spoken and "recog-" not in spoken  # hyphenated line break re-joined
    # Every utterance maps back to the page: word boxes give highlight rectangles in PDF points.
    assert all(a.bbox is not None for u in script.utterances for a in u.anchors if a.block_id)


async def test_snapshot_inputs_are_json_serializable(documents: dict[str, Document]) -> None:
    for doc in documents.values():
        json.loads(doc.model_dump_json())
