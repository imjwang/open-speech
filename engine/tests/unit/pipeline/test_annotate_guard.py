"""AI Enhanced span ops (05 § Tier 2): Claude sees where Classic already cut, and an op can't remove more
than its rule covers (plan/annotate.py, plan/guard.py).

The over-deletion case replays what claude-sonnet-5-5 returned for tests/fixtures/pipeline/academic.pdf on
2026-11-21 with the previous prompt (fixtures/providers/anthropic_annotate_overreach.json: ops and usage
verbatim): it saw "More details are given at together with the evaluation scripts." — the URL already
removed by the Classic filter, with nothing to show for it — and skipped " together with the evaluation
scripts". The legitimate cases are the AI Enhanced behaviours listed in 06 § Built-in presets.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from open_speech.cache.extract import ExtractCache
from open_speech.config import Paths
from open_speech.document.model import Block, BlockType, Document, Mark, SourceKind
from open_speech.ingest import SourceInput, ingest
from open_speech.ingest.marks import detect_marks
from open_speech.local.catalog import ModelCatalog
from open_speech.plan.annotate import Cut, MarkedText, apply_ops, locate_ops, mark_cuts
from open_speech.plan.guard import OpGuard
from open_speech.plan.planner import PlanContext, Planner
from open_speech.plan.script import SpanOp, SpeechScript
from open_speech.plan.textmap import MappedText
from open_speech.providers.anthropic_llm import ANNOTATE_SYSTEM, AnthropicLlm
from open_speech.providers.base import RuleBrief
from open_speech.rules.files import RuleStore
from open_speech.rules.model import Rule
from tests.unit.pipeline.fakes import AnnotateFn, FakeLlm
from tests.unit.runtime.helpers import Recorder, fixture_text

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
URL_SENTENCE = "More details are given at https://example.org/method together with the evaluation scripts."
CLASSIC_SENTENCE = "More details are given at together with the evaluation scripts."


def _doc(*blocks: str | tuple[BlockType, str]) -> Document:
    out: list[Block] = []
    for i, b in enumerate(blocks):
        btype, text = (BlockType.paragraph, b) if isinstance(b, str) else b
        marks = [
            Mark(id=f"m{i}_{j}", kind=k, start=s, end=e)
            for j, (k, s, e) in enumerate(detect_marks(text, block_type=btype))
        ]
        out.append(Block(id=f"blk_{i}", type=btype, text=text, order=i, marks=marks))
    return Document(id="doc_test", source_id="src_test", kind=SourceKind.text, title="Test", blocks=out)


def _ops(*ops: dict[str, Any]) -> AnnotateFn:
    """A recorded answer: the same ops whatever is asked (ops for blocks not in the batch are left out)."""

    def fn(blocks: Sequence[Block], _rules: Sequence[RuleBrief], _speech_md: str) -> list[SpanOp]:
        ids = {b.id for b in blocks}
        return [SpanOp(**{"block_id": "blk_0", "occurrence": 1, **o}) for o in ops if o.get("block_id", "blk_0") in ids]

    return fn


async def _plan(
    doc: Document,
    tmp_path: Path,
    annotate: AnnotateFn,
    *,
    speech_md: str = "",
    presets: Sequence[str] = (),
    rules: Sequence[Rule] = (),
) -> tuple[SpeechScript, list[str]]:
    """Plan in AI Enhanced mode with the default presets. Returns the script and the block texts Claude saw."""
    store = RuleStore(Paths(tmp_path).ensure())
    store.ensure_defaults()
    for name in presets:
        store.set_preset(name, True)
    for rule in rules:
        store.save(rule)
    seen: list[str] = []

    def recording(blocks: Sequence[Block], briefs: Sequence[RuleBrief], md: str) -> Any:
        seen.extend(b.text for b in blocks)
        return annotate(blocks, briefs, md)

    ctx = PlanContext(
        document=doc, rules=store.rules_for(doc), lexicon=store.lexicon(), speech_md=speech_md, mode="ai_enhanced"
    )
    result = await Planner(None).plan(ctx, FakeLlm(annotate=recording), first_batch_only=False)
    return result.script, seen


def _spoken(script: SpeechScript) -> str:
    return " ".join(u.text for u in script.utterances)


def _ai_hits(script: SpeechScript) -> list[tuple[str, str, str]]:
    return [(h.rule_id, h.action, h.original) for u in script.utterances for h in u.rule_trace if h.tier == "annotate"]


def _assert_segments_exact(script: SpeechScript, doc: Document) -> None:
    """Every spoken word still maps to the source text it came from (highlighting stays exact)."""
    for u in script.utterances:
        assert u.segments, u.text
        for seg in u.segments:
            src = doc.block(seg.block_id).text[seg.src_start : seg.src_end]
            said = u.text[seg.spoken_start : seg.spoken_end]
            if len(src) == len(said):
                assert src == said


# ------------------------------------------------------------------ what Claude is shown


async def test_claude_sees_a_marker_where_classic_removed_something(tmp_path: Path) -> None:
    doc = _doc(f"Reading aloud is an old idea [1], yet tools read everything (Smith et al., 2020). {URL_SENTENCE}")
    script, seen = await _plan(doc, tmp_path, _ops())
    assert seen == [
        "Reading aloud is an old idea ⟦citation⟧, yet tools read everything ⟦citation⟧. "
        "More details are given at ⟦url⟧ together with the evaluation scripts."
    ]
    # Markers are for Claude only: they are never spoken and leave the Classic result untouched.
    assert _spoken(script) == f"Reading aloud is an old idea, yet tools read everything. {CLASSIC_SENTENCE}"
    assert "⟦" in ANNOTATE_SYSTEM and "⟦" not in _spoken(script)
    _assert_segments_exact(script, doc)


def test_marked_text_maps_quotes_back_to_the_spoken_text() -> None:
    plain = "More details are given at together with the scripts."
    view = mark_cuts(plain, [Cut(plain.index(" together"), "url")])
    assert view.text == "More details are given at ⟦url⟧ together with the scripts."
    assert view.plain == plain

    def located(quote: str) -> list[str]:
        ops, dropped = locate_ops(view, [SpanOp(block_id="b", op="skip", quote=quote)])
        assert not dropped
        return [plain[lo.start : lo.end] for lo in ops]

    assert located(" at") == [" at"]
    assert located(" at ⟦url⟧") == [" at"]  # a quote may run across a marker; the marker has no width
    assert located("at ⟦url⟧ together") == ["at together"]
    assert located("given at together") == ["given at together"]  # quoted as if the marker weren't there
    assert located("⟦url⟧") == [] and located(" ⟦url⟧") == []  # only a marker: nothing to change
    _, dropped = locate_ops(view, [SpanOp(block_id="b", op="skip", quote="given at the")])
    assert [d.overreach for d in dropped] == [False]


def test_markers_are_spaced_like_words_and_merged_per_position() -> None:
    assert mark_cuts("see x", [Cut(0, "url")]).text == "⟦url⟧ see x"
    assert mark_cuts("More at", [Cut(7, "url")]).text == "More at ⟦url⟧"
    assert mark_cuts("idea, yet", [Cut(4, "citation"), Cut(4, "citation"), Cut(4, "footnote")]).text == (
        "idea ⟦citation, footnote⟧, yet"
    )
    assert mark_cuts("(see)", [Cut(4, "url")]).text == "(see ⟦url⟧)"
    assert mark_cuts("plain", []) == MarkedText.of("plain")


# ------------------------------------------------------------------ the over-deletion


@pytest.mark.parametrize(
    ("quote", "rule_id"),
    [
        (" together with the evaluation scripts", None),  # recorded live: no rule named at all
        (" together with the evaluation scripts", "rule_urls"),  # the same slip, credited to the URL preset
        (" at ⟦url⟧ together with the evaluation scripts", "rule_urls"),  # reaching across the marker
        (" at together with the evaluation scripts", "rule_urls"),
    ],
)
async def test_skip_next_to_a_removed_url_cannot_take_the_neighbouring_content(
    tmp_path: Path, quote: str, rule_id: str | None
) -> None:
    doc = _doc(f"Digits are normalized before the comparison. {URL_SENTENCE}")
    script, _ = await _plan(doc, tmp_path, _ops({"op": "skip", "quote": quote, "rule_id": rule_id}))
    assert _spoken(script) == f"Digits are normalized before the comparison. {CLASSIC_SENTENCE}"
    assert _ai_hits(script) == []
    assert script.warnings == [
        "Ignored 1 AI edit that would have removed more than the rule covers; that text is read as written."
    ]


async def test_recorded_response_on_the_academic_pdf_keeps_the_sentence(tmp_path: Path) -> None:
    """The real request path: AnthropicLlm over HTTP, answering with the recorded over-deleting response."""
    paths = Paths(tmp_path).ensure()
    pdf = (FIXTURES / "pipeline" / "academic.pdf").read_bytes()
    doc = (
        await ingest(SourceInput(kind="file", data=pdf, filename="academic.pdf"), extract_cache=ExtractCache(paths))
    ).document
    store = RuleStore(paths)
    store.ensure_defaults()
    rec = Recorder()
    rec.add(
        "POST",
        "/v1/messages",
        lambda r: httpx2.Response(200, json=json.loads(fixture_text("anthropic_annotate_overreach.json"))),
    )
    llm = AnthropicLlm(
        ModelCatalog.load(), "sk-ant-test", http_client=rec.client(), base_url="https://anthropic.test", max_retries=0
    )
    ctx = PlanContext(
        document=doc, rules=store.rules_for(doc), lexicon=store.lexicon(), speech_md=store.speech_md(),
        mode="ai_enhanced",
    )
    result = await Planner(None).plan(ctx, llm, first_batch_only=False)

    sent = rec.json_body(0)["messages"][0]["content"]
    assert "More details are given at ⟦url⟧ together with the evaluation scripts." in sent
    spoken = _spoken(result.script)
    assert CLASSIC_SENTENCE in spoken  # exactly what Classic reads: nothing more was lost
    assert _ai_hits(result.script) == []
    assert any(w.startswith("Ignored 1 AI edit") for w in result.script.warnings)


def test_guard_also_refuses_an_op_that_was_pinned_earlier() -> None:
    """`apply_ops` re-checks, so an op in the plan cache can't bypass the bound."""
    text = "More details are given at together with the evaluation scripts."
    block = Block(id="blk_0", type=BlockType.paragraph, text=text, order=0)
    mt = MappedText(text)
    op = SpanOp(block_id="blk_0", op="skip", quote=" together with the evaluation scripts")
    hits, dropped = apply_ops(block, mt, [op], rules_by_id={}, annotate_rules=[], guard=OpGuard([]))
    assert hits == [] and mt.text == text
    assert [d.overreach for d in dropped] == [True]


# ------------------------------------------------------------------ legitimate AI Enhanced behaviour


async def test_connective_that_only_introduced_the_url_may_go(tmp_path: Path) -> None:
    doc = _doc(f"Digits are normalized before the comparison. {URL_SENTENCE}")
    for quote in (" at", " at ⟦url⟧"):
        op = {"op": "skip", "quote": quote, "rule_id": "rule_urls"}
        script, _ = await _plan(doc, tmp_path / quote.strip(), _ops(op))
        assert _spoken(script) == (
            "Digits are normalized before the comparison. More details are given together with the evaluation scripts."
        )
        assert _ai_hits(script) == [("rule_urls", "skip", " at")]
        assert script.warnings == []
        _assert_segments_exact(script, doc)


async def test_author_year_citations_keep_the_subject_name(tmp_path: Path) -> None:
    doc = _doc(
        "Smith and colleagues (2020) showed that gaps matter (for a review, see Lee and Kim, 2019, and Garcia 2021)."
    )
    script, seen = await _plan(
        doc,
        tmp_path,
        _ops(
            {"op": "skip", "quote": " (2020)", "rule_id": "rule_citations"},
            {
                "op": "skip",
                "quote": " (for a review, see Lee and Kim, 2019, and Garcia 2021)",
                "rule_id": "rule_citations",
            },
        ),
    )
    assert seen == [doc.blocks[0].text]  # forms the Classic detector doesn't recognise: nothing was cut yet
    assert _spoken(script) == "Smith and colleagues showed that gaps matter."
    assert [h[2] for h in _ai_hits(script)] == [" (2020)", " (for a review, see Lee and Kim, 2019, and Garcia 2021)"]
    assert script.warnings == []


async def test_citation_skip_cannot_take_the_clause_after_it(tmp_path: Path) -> None:
    doc = _doc("Smith and colleagues (2020) showed that the gap between columns is the strongest signal.")
    script, _ = await _plan(
        doc,
        tmp_path,
        _ops({"op": "skip", "quote": " (2020) showed that the gap between columns", "rule_id": "rule_citations"}),
    )
    assert _spoken(script) == doc.blocks[0].text
    assert len(script.warnings) == 1


async def test_inline_page_number_fragment_is_skipped(tmp_path: Path) -> None:
    doc = _doc("The method is described below. Page 4 of 18 It relies on simple geometry.")
    op = {"op": "skip", "quote": "Page 4 of 18 ", "rule_id": "rule_page-numbers"}
    script, _ = await _plan(doc, tmp_path, _ops(op))
    assert _spoken(script) == "The method is described below. It relies on simple geometry."
    assert _ai_hits(script) == [("rule_page-numbers", "skip", "Page 4 of 18 ")]
    assert script.warnings == []


async def test_bare_address_is_read_as_its_short_domain(tmp_path: Path) -> None:
    doc = _doc("For the data, see example.org/docs/latest/index.html today.")
    op = {"op": "replace", "quote": "example.org/docs/latest/index.html", "text": "example.org", "rule_id": "rule_urls"}
    script, seen = await _plan(doc, tmp_path, _ops(op))
    assert seen == [doc.blocks[0].text]  # no scheme, so the Classic URL filter left it alone
    assert _spoken(script) == "For the data, see example.org today."
    assert script.warnings == []


async def test_replace_cannot_be_used_to_drop_content(tmp_path: Path) -> None:
    doc = _doc("For the data, see example.org/docs together with the evaluation scripts.")
    script, _ = await _plan(
        doc,
        tmp_path,
        _ops(
            {
                "op": "replace",
                "quote": "example.org/docs together with the evaluation scripts",
                "text": "example.org",
                "rule_id": "rule_urls",
            }
        ),
    )
    assert _spoken(script) == doc.blocks[0].text
    assert len(script.warnings) == 1


async def test_parentheses_preset_skips_parentheticals_but_not_what_follows(tmp_path: Path) -> None:
    text = "The corpus (about a third of it) is noisy （see the appendix） and was collected over three years."
    doc = _doc(text)
    script, seen = await _plan(
        doc,
        tmp_path,
        _ops({"op": "skip", "quote": " （see the appendix）", "rule_id": "rule_parentheses"}),
        presets=["parentheses"],
    )
    assert seen == ["The corpus ⟦parenthetical⟧ is noisy （see the appendix） and was collected over three years."]
    assert _spoken(script) == "The corpus is noisy and was collected over three years."
    assert script.warnings == []

    script, _ = await _plan(
        doc,
        tmp_path / "overreach",
        _ops(
            {
                "op": "skip",
                "quote": " （see the appendix） and was collected over three years",
                "rule_id": "rule_parentheses",
            }
        ),
        presets=["parentheses"],
    )
    assert _spoken(script) == "The corpus is noisy （see the appendix） and was collected over three years."
    assert len(script.warnings) == 1


async def test_running_head_fused_into_a_paragraph_is_skipped(tmp_path: Path) -> None:
    head = "Journal of Synthetic Results · Vol. 7"
    doc = _doc(
        (BlockType.header, head),
        "We first group characters into lines and lines into columns.",
        (BlockType.header, head),
        f"{head} Lines that cross every gutter are treated as full-width.",
    )
    script, _ = await _plan(
        doc, tmp_path, _ops({"block_id": "blk_3", "op": "skip", "quote": f"{head} ", "rule_id": "rule_headers"})
    )
    assert _spoken(script) == (
        "We first group characters into lines and lines into columns. "
        "Lines that cross every gutter are treated as full-width."
    )
    assert script.warnings == []

    # Text that appears once isn't a running head, whatever the op says.
    script, _ = await _plan(
        doc,
        tmp_path / "once",
        _ops({"block_id": "blk_3", "op": "skip", "quote": " Lines that cross every gutter", "rule_id": "rule_headers"}),
    )
    assert "Lines that cross every gutter are treated as full-width." in _spoken(script)
    assert len(script.warnings) == 1


async def test_rules_the_user_wrote_and_speech_md_may_remove_whole_passages(tmp_path: Path) -> None:
    disclaimer = " This document does not constitute legal advice and may not be relied upon by any party."
    doc = _doc(f"The contract runs for two years.{disclaimer} Either side may renew it.")
    rule = Rule.new("skip-legal-disclaimers", instruction="Never read legal disclaimers.", tier="annotate")
    script, _ = await _plan(
        doc, tmp_path / "rule", _ops({"op": "skip", "quote": disclaimer, "rule_id": rule.id}), rules=[rule]
    )
    assert _spoken(script) == "The contract runs for two years. Either side may renew it."
    assert _ai_hits(script) == [(rule.id, "skip", disclaimer)]

    op = {"op": "skip", "quote": disclaimer, "rule_id": "speech_md"}
    script, _ = await _plan(doc, tmp_path / "md", _ops(op), speech_md="Never read legal disclaimers.")
    assert _spoken(script) == "The contract runs for two years. Either side may renew it."
    assert _ai_hits(script) == [("speech_md", "skip", disclaimer)]

    # Without anything in SPEECH.md there is no preference to appeal to.
    script, _ = await _plan(doc, tmp_path / "empty", _ops(op))
    assert disclaimer.strip() in _spoken(script)
    assert len(script.warnings) == 1


async def test_small_edits_without_a_rule_still_apply(tmp_path: Path) -> None:
    doc = _doc("Models are, e.g., small and fast.")
    script, _ = await _plan(doc, tmp_path, _ops({"op": "replace", "quote": "e.g.", "text": "for example"}))
    assert _spoken(script) == "Models are, for example, small and fast."
    assert script.warnings == []
