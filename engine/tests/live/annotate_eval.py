"""The AI Enhanced accuracy gate (05 § Tier 2): documents, hand labels, request variants and scoring.

Used live by `test_annotate_eval.py` (OPEN_SPEECH_LIVE=1) and offline by
`tests/unit/pipeline/test_annotate_replay.py`, which replays the responses recorded for the shipped variant.

Every document goes through the real pipeline: ingest, the default reading instructions with Claude, the Planner and
`AnthropicLlm`, so what is scored is what a listener would hear.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from open_speech.api.schemas import HtmlBlocksPayload
from open_speech.cache.extract import ExtractCache
from open_speech.config import Paths
from open_speech.document.model import Block, BlockType, Document, Mark, SourceKind
from open_speech.ingest import SourceInput, ingest
from open_speech.ingest.marks import detect_marks
from open_speech.local.catalog import BUNDLED_MODELS, ModelCatalog, deep_merge
from open_speech.plan import build as plan_build
from open_speech.plan.annotate import DroppedOp, LocatedOp
from open_speech.plan.planner import PlanContext, Planner
from open_speech.plan.script import SpeechScript
from open_speech.providers.base import LlmProvider, Units
from open_speech.rules.files import RuleStore
from open_speech.usage.pricing import Pricing
from tests.golden import make_fixtures as fx

HERE = Path(__file__).resolve().parent
EXPECTATIONS = HERE / "annotate_expectations.json"
PIPELINE = HERE.parent / "fixtures" / "pipeline"
REPLAY_DIR = HERE.parent / "fixtures" / "providers"

# Pass bar (the owner's): no must-keep violation in any run, must-drop recall at least the Sonnet baseline's,
# and at most 5 % of the quotes not found in their block.
MAX_UNMATCHED = 0.05


@dataclass(frozen=True)
class Variant:
    name: str
    model: str
    annotate_thinking: str  # "off" (the model's thinking-off form) or "adaptive"
    effort: str
    label: str

    def catalog(self) -> ModelCatalog:
        import tomllib

        data = tomllib.loads(BUNDLED_MODELS.read_text("utf-8"))
        over = {"llm": {"anthropic": {"models": {self.model: {"annotate_thinking": self.annotate_thinking}}}}}
        return ModelCatalog.from_dict(deep_merge(data, over))

    def replay_path(self) -> Path:
        return REPLAY_DIR / f"anthropic_annotate_eval_{self.name}.json"


VARIANTS: dict[str, Variant] = {
    v.name: v
    for v in (
        Variant("sonnet-baseline", "claude-sonnet-5-5", "off", "low", "Sonnet 5.5, thinking off (between_tools), low"),
        Variant("haiku-off-low", "claude-haiku-5-5", "off", "low", "Haiku 5.5, thinking disabled, low"),
        Variant("haiku-adaptive-low", "claude-haiku-5-5", "adaptive", "low", "Haiku 5.5, adaptive thinking, low"),
        Variant("haiku-adaptive-medium", "claude-haiku-5-5", "adaptive", "medium",
                "Haiku 5.5, adaptive thinking, medium"),
    )
}


def shipped_variant() -> Variant:
    """The variant the bundled catalog and default settings produce for Haiku (what users get)."""
    from open_speech.config import LlmSettings

    catalog = ModelCatalog.load()
    entry = catalog.llm_for("anthropic")
    assert entry is not None
    traits = entry.traits(entry.default)
    effort = LlmSettings().annotate_effort
    for v in VARIANTS.values():
        if v.model == entry.default and v.annotate_thinking == traits.annotate_thinking and v.effort == effort:
            return v
    raise AssertionError(f"no eval variant matches the shipped config ({entry.default}, {traits}, {effort})")


# ------------------------------------------------------------------ documents and labels


def expectations() -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = json.loads(EXPECTATIONS.read_text("utf-8"))["documents"]
    return data


def _labelled_doc(name: str, spec: dict[str, Any]) -> Document:
    blocks: list[Block] = []
    for i, (btype, text) in enumerate(spec["blocks"]):
        bt = BlockType(btype)
        found = detect_marks(text, block_type=bt)
        marks = [Mark(id=f"m{i}_{j}", kind=k, start=s, end=e) for j, (k, s, e) in enumerate(found)]
        blocks.append(Block(id=f"blk_{i}", type=bt, text=text, order=i, marks=marks))
    return Document(
        id=f"doc_{name}", source_id=f"src_{name}", kind=SourceKind.text, title=spec.get("title", name), blocks=blocks
    )


async def load_documents(paths: Paths) -> dict[str, Document]:
    """The pipeline fixtures (scanned.pdf has no text layer and is left out) plus the hand-written hard cases."""
    sources = {
        "academic.pdf": SourceInput(kind="file", data=fx.academic_pdf(), filename="academic.pdf"),
        "report.pdf": SourceInput(kind="file", data=fx.report_pdf(), filename="report.pdf"),
        "footnotes.docx": SourceInput(kind="file", data=fx.footnotes_docx(), filename="footnotes.docx"),
        "news.html_blocks": SourceInput(
            kind="html_blocks", html_blocks=HtmlBlocksPayload.model_validate(fx.news_blocks())
        ),
        "notes.md": SourceInput(kind="file", data=fx.notes_md().encode(), filename="notes.md"),
        "wikipedia_lead": SourceInput(
            kind="html_blocks",
            html_blocks=HtmlBlocksPayload.model_validate(
                json.loads((PIPELINE / "wikipedia_lead_blocks.json").read_text("utf-8"))
            ),
        ),
    }
    cache = ExtractCache(paths)
    docs = {name: (await ingest(src, extract_cache=cache)).document for name, src in sources.items()}
    for name, spec in expectations().items():
        if "blocks" in spec:
            docs[name] = _labelled_doc(name, spec)
    assert set(docs) == set(expectations()), "every document needs labels and every label set a document"
    return docs


def rule_store(paths: Paths) -> RuleStore:
    store = RuleStore(paths)
    store.ensure_defaults()
    return store


async def plan(doc: Document, store: RuleStore, llm: LlmProvider | None, mode: str = "ai_enhanced") -> SpeechScript:
    ctx = PlanContext(
        document=doc,
        rules=store.plan_rules(doc),
        lexicon=store.lexicon(),
        speech_md=store.plan_speech_md(),
        mode=mode,  # type: ignore[arg-type]
        llm_model=getattr(llm, "model", "") or "",
    )
    result = await Planner(None).plan(ctx, llm, first_batch_only=False)
    return result.script


def spoken(script: SpeechScript) -> str:
    return " ".join(" ".join(u.text for u in script.utterances).split())


@dataclass
class DocScore:
    document: str
    keep_violations: list[str]
    dropped: list[str]
    missed: list[str]


def score(name: str, script: SpeechScript) -> DocScore:
    labels = expectations()[name]
    said = spoken(script)
    return DocScore(
        document=name,
        keep_violations=[k for k in labels["must_keep"] if " ".join(k.split()) not in said],
        dropped=[d for d in labels["must_drop"] if d not in said],
        missed=[d for d in labels["must_drop"] if d in said],
    )


# ------------------------------------------------------------------ instrumentation


@dataclass
class OpCounts:
    located: int = 0
    unmatched: int = 0  # the quote is not in its block
    overlap: int = 0
    refused: int = 0  # the guard (plan/guard.py) refused it: it removes more than its rule covers
    refusal_reasons: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.located + self.unmatched + self.overlap + self.refused


_LOCATE_OPS = plan_build.locate_ops  # the real one, captured before any test patches it


def count_ops(counts: OpCounts) -> Callable[..., tuple[list[LocatedOp], list[DroppedOp]]]:
    """A drop-in for `plan.build.locate_ops` that tallies what happened to every op Claude returned. Always wraps
    the real function, so patching it again for the next variant never chains two counters."""

    def wrapped(view: Any, ops: Sequence[Any], guard: Any = None) -> tuple[list[LocatedOp], list[DroppedOp]]:
        located, dropped = _LOCATE_OPS(view, ops, guard)
        counts.located += len(located)
        for d in dropped:
            if d.overreach:
                counts.refused += 1
                counts.refusal_reasons.append(d.reason)
            elif "not found" in d.reason:
                counts.unmatched += 1
            else:
                counts.overlap += 1
        return located, dropped

    return wrapped


def request_key(params: dict[str, Any]) -> str:
    """Identifies an annotate request by everything that decides its answer."""
    parts = [
        params["model"],
        json.dumps(params.get("thinking"), sort_keys=True),
        json.dumps(params.get("output_config", {}).get("effort")),
        json.dumps([s["text"] for s in params["system"]], ensure_ascii=False),
        json.dumps(params["messages"], ensure_ascii=False, sort_keys=True),
    ]
    return hashlib.sha256("\x1e".join(parts).encode()).hexdigest()


@dataclass
class Call:
    key: str
    latency_s: float
    stop_reason: str | None
    units: Units
    response: dict[str, Any]


class RecordingClient:
    """Wraps an `AsyncAnthropic` for `AnthropicLlm`: times every `messages.create` and keeps the response."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[Call] = []

    @property
    def messages(self) -> RecordingClient:
        return self

    async def create(self, **params: Any) -> Any:
        t0 = time.perf_counter()
        try:
            resp = await self._client.messages.create(**params)
        except Exception as exc:
            self.calls.append(Call(request_key(params), time.perf_counter() - t0, "error", Units(),
                                   {"error": f"{type(exc).__name__}: {exc}"[:500]}))
            raise
        dt = time.perf_counter() - t0
        u = resp.usage
        self.calls.append(
            Call(
                key=request_key(params),
                latency_s=dt,
                stop_reason=resp.stop_reason,
                units=Units(
                    in_tok=u.input_tokens or 0,
                    out_tok=u.output_tokens or 0,
                    cache_read_tok=u.cache_read_input_tokens or 0,
                    cache_write_tok=u.cache_creation_input_tokens or 0,
                ),
                response=resp.model_dump(mode="json", exclude_none=True),
            )
        )
        return resp


# ------------------------------------------------------------------ one run, and the summary


@dataclass
class RunResult:
    variant: str
    run: int
    scores: list[DocScore]
    ops: OpCounts
    calls: list[Call]

    @property
    def keep_violations(self) -> list[str]:
        return [f"{s.document}: {k}" for s in self.scores for k in s.keep_violations]

    @property
    def drop_total(self) -> int:
        return sum(len(s.dropped) + len(s.missed) for s in self.scores)

    @property
    def dropped(self) -> int:
        return sum(len(s.dropped) for s in self.scores)

    def cost(self, pricing: Pricing, model: str) -> Decimal:
        return sum((pricing.cost("anthropic", model, c.units) for c in self.calls), Decimal(0))


def summarize(variant: Variant, runs: Sequence[RunResult], pricing: Pricing) -> dict[str, Any]:
    calls = [c for r in runs for c in r.calls]
    ops = OpCounts()
    for r in runs:
        ops.located += r.ops.located
        ops.unmatched += r.ops.unmatched
        ops.overlap += r.ops.overlap
        ops.refused += r.ops.refused
    drop_total = sum(r.drop_total for r in runs)
    cost = sum((r.cost(pricing, variant.model) for r in runs), Decimal(0))
    return {
        "variant": variant.name,
        "label": variant.label,
        "runs": len(runs),
        "keep_violations": sum(len(r.keep_violations) for r in runs),
        "keep_violation_list": sorted({v for r in runs for v in r.keep_violations}),
        "drop_recall": round(sum(r.dropped for r in runs) / drop_total, 3) if drop_total else 1.0,
        "missed_drops": sorted({f"{s.document}: {m}" for r in runs for s in r.scores for m in s.missed}),
        "guard_refusals": ops.refused,
        "refusal_reasons": sorted({reason for r in runs for reason in r.ops.refusal_reasons}),
        "unmatched": ops.unmatched,
        "ops": ops.total,
        "unmatched_rate": round(ops.unmatched / ops.total, 3) if ops.total else 0.0,
        "model_refusals": sum(1 for c in calls if c.stop_reason == "refusal"),
        "errors": sum(1 for c in calls if c.stop_reason in ("error", "max_tokens")),
        "error_list": sorted({str(c.response.get("error", c.stop_reason)) for c in calls
                              if c.stop_reason in ("error", "max_tokens")})[:5],
        "calls": len(calls),
        "latency_p50_s": round(statistics.median(c.latency_s for c in calls), 2) if calls else 0.0,
        "latency_p90_s": round(statistics.quantiles([c.latency_s for c in calls], n=10)[-1], 2) if len(calls) > 1
        else 0.0,
        "out_tok_mean": round(statistics.mean(c.units.out_tok for c in calls), 1) if calls else 0.0,
        "cache_read_tok": sum(c.units.cache_read_tok for c in calls),
        "cache_write_tok": sum(c.units.cache_write_tok for c in calls),
        "in_tok": sum(c.units.in_tok for c in calls),
        "cost_usd": str(cost),
        "cost_per_run_usd": str((cost / len(runs)).quantize(Decimal("0.000001"))) if runs else "0",
    }


def passes(summary: dict[str, Any], baseline: dict[str, Any] | None) -> bool:
    recall_ok = baseline is None or summary["drop_recall"] >= baseline["drop_recall"]
    clean = summary.get("errors", 0) == 0  # a failed call reads Classic text: it would flatter the keep count
    return clean and summary["keep_violations"] == 0 and recall_ok and summary["unmatched_rate"] <= MAX_UNMATCHED


def table(summaries: Sequence[dict[str, Any]], baseline: dict[str, Any] | None) -> str:
    head = (
        "| Variant | Must-keep violations | Must-drop recall | Guard refusals | Unmatched quotes | Model refusals "
        "| p50 latency | Output tokens / call | Cache reads | Cost / run | Pass |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = []
    for s in summaries:
        ok = "baseline" if baseline is not None and s["variant"] == baseline["variant"] else (
            "yes" if passes(s, baseline) else "no"
        )
        rows.append(
            f"| {s['label']} | {s['keep_violations']} | {s['drop_recall']:.0%} | {s['guard_refusals']} "
            f"| {s['unmatched']}/{s['ops']} ({s['unmatched_rate']:.1%}) | {s['model_refusals']} "
            f"| {s['latency_p50_s']:.2f} s | {s['out_tok_mean']:.0f} | {s['cache_read_tok']} "
            f"| ${Decimal(s['cost_per_run_usd']):.4f} | {ok} |"
        )
    return head + "\n".join(rows) + "\n"
