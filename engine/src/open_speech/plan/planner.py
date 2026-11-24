"""Planner (05 § 4): Document + rules → SpeechScript, cached by plan key.

- Cleanup is Claude following the reading instructions (SPEECH.md) and the rules (mode "ai_enhanced"). The
  first batch (~1.5k tokens of blocks) runs synchronously in `plan()`, then `extend()` keeps ≥ 2 batches ahead
  of the playhead. Blocks not yet annotated are read with tier-1 text, so the script is always complete and
  playable.
- Without Claude (mode "classic": no Claude in this build) the document is planned deterministically (tier 1 +
  lexicon, including the filters from SPEECH.md's skip lines), with no API calls.
- A cached plan is returned without LLM calls. Claude output is also pinned per block / section, so after
  a rule edit only blocks whose annotate input changed cost a call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from open_speech.cache.plans import PlanCache
from open_speech.config import DEFAULT_LLM
from open_speech.document.model import Document
from open_speech.i18n import t
from open_speech.plan.build import LOOKAHEAD_BATCHES, BuildSpec, Memo, build_script
from open_speech.plan.keys import PROMPT_VERSION, active_rules, effective_rules_hash, plan_key, tiering
from open_speech.plan.script import SpeechScript
from open_speech.providers.base import LlmCall, LlmProvider
from open_speech.rules.lexicon import Lexicon
from open_speech.rules.model import Rule

__all__ = ["PlanContext", "PlanResult", "Planner", "plan_key_for"]


class PlanContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: Document
    rules: list[Rule] = Field(default_factory=list)
    lexicon: Lexicon = Field(default_factory=Lexicon)
    speech_md: str = ""
    mode: Literal["classic", "ai_enhanced"] = "classic"
    llm_model: str = DEFAULT_LLM


class PlanResult(BaseModel):
    script: SpeechScript
    llm_calls: list[tuple[str, LlmCall]] = Field(default_factory=list)
    # AI Enhanced was wanted but could not run (unavailable, over budget, provider error): worth trying again.
    degraded: bool = False

    model_config = ConfigDict(arbitrary_types_allowed=True)


def _spec(ctx: PlanContext) -> BuildSpec:
    doc = ctx.document
    rules = active_rules(ctx.rules, doc)
    tiers = tiering(rules, ctx.mode)
    entries = ctx.lexicon.for_document(doc)
    model = ctx.llm_model if ctx.mode == "ai_enhanced" else ""
    rh = effective_rules_hash(rules, entries, ctx.speech_md, ctx.mode)
    key = plan_key(doc.source_id, rh, model, PROMPT_VERSION if ctx.mode == "ai_enhanced" else "", ctx.mode)
    warnings: list[str] = []
    for r in tiers.inactive:
        warnings.append(t("plan.rule_needs_ai", name=r.name))
    for r in tiers.extra_transforms:
        warnings.append(t("plan.one_rewrite", name=r.name))
    for flag in doc.flags:
        if flag.startswith("scanned_pages:"):
            pages = flag.split(":", 1)[1].replace(",", ", ")
            warnings.append(t("plan.scanned", pages=pages))
    return BuildSpec(
        document=doc,
        filters=tiers.filters,
        annotate=tiers.annotate,
        transform=tiers.transform,
        lexicon=entries,
        speech_md=ctx.speech_md,
        mode=ctx.mode,
        model=model,
        plan_key=key,
        warnings=warnings,
    )


def plan_key_for(ctx: PlanContext) -> str:
    """Addition: the plan key a context maps to (05 § Caches)."""
    return _spec(ctx).plan_key


def _last_order(doc: Document) -> int:
    return max((b.order for b in doc.blocks), default=-1)


class Planner:
    def __init__(self, plan_cache: PlanCache | None) -> None:
        self.cache = plan_cache

    def _memo(self, doc_id: str) -> Memo:
        if self.cache is None:
            return Memo()
        return Memo(
            annotate=self.cache.get_memo(doc_id, "annotate"),
            transform=self.cache.get_memo(doc_id, "transform"),
        )

    def _save(self, doc_id: str, memo: Memo, script: SpeechScript | None) -> None:
        if self.cache is None:
            return
        self.cache.put_memo(doc_id, "annotate", memo.new_annotate)
        self.cache.put_memo(doc_id, "transform", memo.new_transform)
        if script is not None:
            self.cache.put(script)

    @staticmethod
    def _needs_llm(spec: BuildSpec) -> bool:
        return spec.mode == "ai_enhanced" and (
            bool(spec.annotate) or spec.transform is not None or bool(spec.speech_md.strip())
        )

    async def plan(self, ctx: PlanContext, llm: LlmProvider | None, *, first_batch_only: bool = True) -> PlanResult:
        spec = _spec(ctx)
        cached = self.cache.get(spec.plan_key) if self.cache else None
        if cached is not None:
            done = cached.annotated_upto_block is None or cached.annotated_upto_block >= _last_order(ctx.document)
            if first_batch_only or done or not self._needs_llm(spec):
                return PlanResult(script=cached)
        memo = self._memo(ctx.document.id)
        result = await build_script(spec, llm, memo, playhead_order=-1, ahead=0, everything=not first_batch_only)
        # Don't pin a plan made without the Claude cleanup it should have had (unavailable, over budget).
        self._save(ctx.document.id, memo, None if result.degraded else result.script)
        return PlanResult(script=result.script, llm_calls=result.calls, degraded=result.degraded)

    async def extend(
        self, ctx: PlanContext, llm: LlmProvider | None, script: SpeechScript, upto_utterance: int
    ) -> PlanResult:
        """Keep Claude ≥ 2 batches ahead of utterance `upto_utterance` (the playhead). Returns the same script
        when nothing new was needed, else a rebuilt script whose utterances before the playhead are
        unchanged."""
        spec = _spec(ctx)
        if not self._needs_llm(spec) or not script.utterances:
            return PlanResult(script=script)
        last = _last_order(ctx.document)
        if script.annotated_upto_block is not None and script.annotated_upto_block >= last:
            return PlanResult(script=script)
        idx = max(0, min(upto_utterance, len(script.utterances) - 1))
        block_id = script.utterances[idx].block_ids[0] if script.utterances[idx].block_ids else None
        try:
            playhead_order = ctx.document.block(block_id).order if block_id else -1
        except KeyError:
            playhead_order = -1

        # Cheap check: is the target already covered?
        from open_speech.plan.build import make_batches, sections, stage1, target_order
        from open_speech.rules.filters import resolve_filters

        states = stage1(ctx.document, resolve_filters(spec.filters))
        units = sections(states) if spec.transform is not None else make_batches(states)
        target = target_order(units, playhead_order, LOOKAHEAD_BATCHES)
        if script.annotated_upto_block is not None and script.annotated_upto_block >= target:
            return PlanResult(script=script)

        memo = self._memo(ctx.document.id)
        result = await build_script(spec, llm, memo, playhead_order=playhead_order, ahead=LOOKAHEAD_BATCHES)
        self._save(ctx.document.id, memo, None if result.degraded else result.script)
        if not result.calls and result.script.annotated_upto_block == script.annotated_upto_block:
            return PlanResult(script=script)
        return PlanResult(script=result.script, llm_calls=result.calls)
