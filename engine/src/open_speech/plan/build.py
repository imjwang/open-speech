"""The planning core shared by `Planner` and `speech_diff` (05 § 4, § 6).

Per block: tier 1 (drop block / drop marks / regex) → [tier 2 span ops] → lexicon → cleanup → chunk into
utterances with exact `segments`, `anchors` and `rule_trace`. Tier 3 transforms replace whole sections
with generated, section-anchored utterances. Claude output is pinned per block / per section in a memo, so
re-planning after a rule edit only calls Claude for blocks whose input actually changed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from open_speech.api.schemas import LexiconEntry
from open_speech.document.model import Anchor, BBox, Block, BlockType, Document
from open_speech.errors import EngineError
from open_speech.ids import sha256_hex
from open_speech.plan.annotate import apply_ops, locate_ops, parse_ops
from open_speech.plan.chunk import chunk_text
from open_speech.plan.keys import PROMPT_VERSION
from open_speech.plan.script import RuleHit, SpanOp, SpeechScript, Utterance
from open_speech.plan.textmap import MappedText
from open_speech.providers.base import LlmCall, LlmProvider, TransformOutput
from open_speech.rules.filters import (
    FilterPlan,
    apply_lexicon,
    apply_mark_drops,
    apply_replacements,
    block_drop_hit,
    resolve_filters,
)
from open_speech.rules.lexicon import Lexicon
from open_speech.rules.model import Rule

log = logging.getLogger("open_speech.plan")

BATCH_CHARS = 6000  # ≈ 1.5k tokens of blocks per annotate call
LOOKAHEAD_BATCHES = 2
Mode = Literal["classic", "ai_enhanced"]


@dataclass
class BlockState:
    block: Block
    drop: RuleHit | None = None
    mt: MappedText | None = None
    hits: list[RuleHit] = field(default_factory=list)
    annotate_input: str = ""

    @property
    def speakable(self) -> bool:
        return self.drop is None and self.mt is not None and bool(self.mt.text.strip())


@dataclass
class BuildSpec:
    document: Document
    filters: list[Rule]
    annotate: list[Rule]
    transform: Rule | None
    lexicon: list[LexiconEntry]
    speech_md: str
    mode: Mode
    model: str
    plan_key: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class Memo:
    annotate: dict[str, Any] = field(default_factory=dict)
    transform: dict[str, Any] = field(default_factory=dict)
    new_annotate: dict[str, Any] = field(default_factory=dict)
    new_transform: dict[str, Any] = field(default_factory=dict)

    def put_annotate(self, key: str, ops: list[dict[str, Any]]) -> None:
        self.annotate[key] = ops
        self.new_annotate[key] = ops

    def put_transform(self, key: str, out: dict[str, Any]) -> None:
        self.transform[key] = out
        self.new_transform[key] = out


@dataclass
class BuildResult:
    script: SpeechScript
    calls: list[tuple[str, LlmCall]]
    complete: bool  # every needed LLM batch is done (or none was needed)
    degraded: bool  # an LLM tier was needed but couldn't run (no key / provider error)


# ------------------------------------------------------------------ tier 1


def stage1(doc: Document, plan: FilterPlan) -> list[BlockState]:
    states: list[BlockState] = []
    for block in sorted(doc.blocks, key=lambda b: b.order):
        st = BlockState(block)
        decision = plan.drop_for(block)
        if decision is not None:
            st.drop = block_drop_hit(block, decision)
        else:
            mt = MappedText(block.text)
            st.hits = apply_mark_drops(block, mt, plan)
            st.hits += apply_replacements(block, mt, plan)
            mt.cleanup(punctuation=block.type != BlockType.code)
            st.mt = mt
            st.annotate_input = mt.text
        states.append(st)
    return states


def make_batches(states: Sequence[BlockState], batch_chars: int = BATCH_CHARS) -> list[list[BlockState]]:
    out: list[list[BlockState]] = []
    cur: list[BlockState] = []
    size = 0
    for st in states:
        if not st.speakable:
            continue
        n = len(st.annotate_input)
        if cur and size + n > batch_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(st)
        size += n
    if cur:
        out.append(cur)
    return out


def target_order(batches: Sequence[Sequence[BlockState]], playhead_order: int, ahead: int) -> int:
    """Last block order that must be planned with Claude: the playhead's batch plus `ahead` more."""
    if not batches:
        return -1
    idx = 0
    for i, batch in enumerate(batches):
        if batch[-1].block.order >= playhead_order:
            idx = i
            break
    else:
        idx = len(batches) - 1
    return batches[min(len(batches) - 1, idx + ahead)][-1].block.order


# ------------------------------------------------------------------ tier 2


def annotate_signature(spec: BuildSpec) -> str:
    rules = sorted(spec.annotate, key=lambda r: r.id)
    return sha256_hex(
        "annotate", spec.model, PROMPT_VERSION, spec.speech_md.strip(), *(f"{r.id}\x1e{r.instruction}" for r in rules)
    )


def annotate_key(signature: str, st: BlockState) -> str:
    return sha256_hex(signature, st.block.id, st.annotate_input)


async def run_annotate(
    spec: BuildSpec,
    batches: Sequence[Sequence[BlockState]],
    upto_order: int,
    llm: LlmProvider | None,
    memo: Memo,
    calls: list[tuple[str, LlmCall]],
) -> bool:
    """Annotate every batch up to `upto_order` that isn't pinned yet. Returns False if a needed call
    couldn't run (no key, provider error)."""
    sig = annotate_signature(spec)
    briefs = [r.brief() for r in spec.annotate]
    dropped_total = 0
    for batch in batches:
        if batch[0].block.order > upto_order:
            break
        missing = [st for st in batch if annotate_key(sig, st) not in memo.annotate]
        if not missing:
            continue
        if llm is None:
            spec.warnings.append("AI Enhanced needs an Anthropic key; the rest is read with Classic filters.")
            return False
        blocks = [
            st.block.model_copy(update={"text": st.annotate_input, "marks": [], "word_boxes": []}) for st in missing
        ]
        try:
            call = await llm.annotate(blocks, briefs, spec.speech_md)
        except EngineError as e:
            spec.warnings.append(f"AI Enhanced paused: {e.detail}")
            log.warning("annotate failed: %s", e.detail)
            return False
        calls.append(("annotate", call))
        ops, invalid = parse_ops(call.output)
        dropped_total += invalid
        by_block: dict[str, list[SpanOp]] = {}
        known = {st.block.id for st in missing}
        for op in ops:
            if op.block_id not in known:
                dropped_total += 1
                log.warning("dropped span op for unknown block %s", op.block_id)
                continue
            by_block.setdefault(op.block_id, []).append(op)
        for st in missing:
            located, dropped = locate_ops(st.annotate_input, by_block.get(st.block.id, []))
            dropped_total += len(dropped)
            for reason in dropped:
                log.warning("dropped span op %s", reason)
            memo.put_annotate(annotate_key(sig, st), [lo.op.model_dump() for lo in located])
    if dropped_total:
        spec.warnings.append(
            f"Dropped {dropped_total} AI span op{'s' if dropped_total != 1 else ''} that didn't match the text exactly."
        )
    return True


def apply_annotations(spec: BuildSpec, states: Sequence[BlockState], memo: Memo) -> int:
    """Apply pinned span ops; return the highest block order annotated contiguously from the start."""
    sig = annotate_signature(spec)
    rules_by_id = {r.id: r for r in spec.annotate}
    upto = -1
    contiguous = True
    for st in states:
        if not st.speakable:
            if contiguous:
                upto = st.block.order
            continue
        raw = memo.annotate.get(annotate_key(sig, st))
        if raw is None:
            contiguous = False
            continue
        if contiguous:
            upto = st.block.order
        ops = [SpanOp.model_validate(o) for o in raw]
        assert st.mt is not None
        hits, _ = apply_ops(st.block, st.mt, ops, rules_by_id=rules_by_id, annotate_rules=spec.annotate)
        st.hits.extend(hits)
    return upto


# ------------------------------------------------------------------ tier 3


def sections(states: Sequence[BlockState]) -> list[list[BlockState]]:
    """Speakable blocks grouped into sections; each heading starts a new section."""
    out: list[list[BlockState]] = []
    cur: list[BlockState] = []
    for st in states:
        if not st.speakable:
            continue
        if st.block.type == BlockType.heading and cur:
            out.append(cur)
            cur = []
        cur.append(st)
    if cur:
        out.append(cur)
    return out


def transform_key(spec: BuildSpec, section: Sequence[BlockState]) -> str:
    rule = spec.transform
    assert rule is not None
    return sha256_hex(
        "transform",
        spec.model,
        PROMPT_VERSION,
        spec.speech_md.strip(),
        rule.id,
        rule.instruction,
        *(f"{st.block.id}\x1e{st.annotate_input}" for st in section),
    )


async def run_transform(
    spec: BuildSpec,
    secs: Sequence[Sequence[BlockState]],
    upto_order: int,
    llm: LlmProvider | None,
    memo: Memo,
    calls: list[tuple[str, LlmCall]],
) -> bool:
    rule = spec.transform
    assert rule is not None
    for sec in secs:
        if sec[0].block.order > upto_order:
            break
        key = transform_key(spec, sec)
        if key in memo.transform:
            continue
        if llm is None:
            spec.warnings.append(f"“{rule.name}” needs an Anthropic key; reading the original text instead.")
            return False
        blocks = [st.block.model_copy(update={"text": st.annotate_input, "marks": [], "word_boxes": []}) for st in sec]
        try:
            call = await llm.transform(blocks, rule.instruction, spec.speech_md)
        except EngineError as e:
            spec.warnings.append(f"“{rule.name}” paused: {e.detail}")
            return False
        calls.append(("transform", call))
        out = call.output
        try:
            parsed = out if isinstance(out, TransformOutput) else TransformOutput.model_validate(out)
        except ValidationError:
            spec.warnings.append(f"“{rule.name}” returned something unusable for one section; read as is.")
            continue
        ids = {st.block.id for st in sec}
        anchors = [b for b in parsed.anchor_block_ids if b in ids] or [st.block.id for st in sec]
        clean = [u.strip() for u in parsed.utterances if u and u.strip()]
        memo.put_transform(key, {"utterances": clean, "anchor_block_ids": anchors})
    return True


# ------------------------------------------------------------------ assembly


def _bbox_for(block: Block, lo: int, hi: int) -> BBox | None:
    boxes = [wb.bbox for wb in block.word_boxes if wb.start < hi and wb.end > lo]
    if not boxes:
        return block.bbox
    page = boxes[0].page
    same = [b for b in boxes if b.page == page]
    return BBox(
        page=page,
        x0=min(b.x0 for b in same),
        y0=min(b.y0 for b in same),
        x1=max(b.x1 for b in same),
        y1=max(b.y1 for b in same),
    )


def _page_for(block: Block, lo: int, hi: int) -> int | None:
    for wb in block.word_boxes:
        if wb.start < hi and wb.end > lo:
            return wb.bbox.page
    return block.page


class _Assembler:
    def __init__(self, spec: BuildSpec) -> None:
        self.spec = spec
        self.utterances: list[Utterance] = []
        self.removed: list[RuleHit] = []
        self.headings: list[tuple[int, str]] = []

    def heading_path(self) -> list[str]:
        return [t for _, t in self.headings]

    def note_heading(self, block: Block) -> None:
        if block.type != BlockType.heading or not block.text.strip():
            return
        level = block.level or 1
        while self.headings and self.headings[-1][0] >= level:
            self.headings.pop()
        self.headings.append((level, " ".join(block.text.split())))

    def add(self, **kw: Any) -> None:
        i = len(self.utterances)
        self.utterances.append(Utterance(id=f"utt_{i}", index=i, **kw))

    def block(self, st: BlockState) -> None:
        mt = st.mt
        assert mt is not None
        block = st.block
        for h in st.hits:
            if h.action == "skip":
                self.removed.append(h)
        text = mt.text
        if not text.strip():
            return
        if block.type == BlockType.heading:
            chunks = [(0, len(text))]
        else:
            chunks = chunk_text(text, first_single=not self.utterances, boundary_ok=mt.boundary_ok)
        ranges = [mt.source_range(cs, ce) or (0, 0) for cs, ce in chunks]
        traces: list[list[RuleHit]] = [[] for _ in chunks]
        for h in st.hits:
            pos = h.src_start or 0
            k = next((i for i, (_lo, hi) in enumerate(ranges) if pos < hi), len(chunks) - 1)
            traces[k].append(h)
        path = self.heading_path()
        for k, ((cs, ce), (lo, hi)) in enumerate(zip(chunks, ranges, strict=True)):
            self.add(
                text=text[cs:ce],
                block_ids=[block.id],
                anchors=[
                    Anchor(
                        block_id=block.id,
                        char_start=lo,
                        char_end=hi,
                        bbox=_bbox_for(block, lo, hi),
                        dom_anchor=block.dom_anchor,
                    )
                ],
                segments=mt.segments(block.id, cs, ce),
                fidelity="exact",
                rule_trace=traces[k],
                page=_page_for(block, lo, hi),
                heading_path=path,
                is_heading=block.type == BlockType.heading,
                paragraph_start=k == 0,
            )

    def transformed(self, sec: Sequence[BlockState], out: dict[str, Any], rule: Rule) -> None:
        anchors_ids: list[str] = list(out.get("anchor_block_ids") or [st.block.id for st in sec])
        by_id = {st.block.id: st.block for st in sec}
        for st in sec:
            self.removed.append(
                RuleHit(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action="transform",
                    block_id=st.block.id,
                    src_start=0,
                    src_end=len(st.block.text),
                    original=st.block.text,
                    tier="transform",
                )
            )
        fidelity: Literal["block", "section"] = "block" if len(anchors_ids) == 1 else "section"
        first_block = by_id.get(anchors_ids[0]) or sec[0].block
        path = self.heading_path()
        for text in out.get("utterances", []):
            spoken = _respell(text, self.spec.lexicon)
            pieces = chunk_text(spoken, first_single=not self.utterances)
            for k, (cs, ce) in enumerate(pieces):
                self.add(
                    text=spoken[cs:ce],
                    block_ids=anchors_ids,
                    anchors=[
                        Anchor(
                            block_id=b,
                            bbox=by_id[b].bbox if b in by_id else None,
                            dom_anchor=by_id[b].dom_anchor if b in by_id else None,
                        )
                        for b in anchors_ids
                    ],
                    segments=[],
                    fidelity=fidelity,
                    rule_trace=[
                        RuleHit(
                            rule_id=rule.id,
                            rule_name=rule.name,
                            action="transform",
                            block_id=anchors_ids[0],
                            replacement=spoken[cs:ce],
                            tier="transform",
                        )
                    ],
                    page=first_block.page,
                    heading_path=path,
                    is_heading=False,
                    paragraph_start=k == 0,
                )


def _respell(text: str, entries: Sequence[LexiconEntry]) -> str:
    if not entries:
        return text
    out = text
    for m in reversed(Lexicon.find(text, entries)):
        out = out[: m.start] + m.spoken + out[m.end :]
    return out


# ------------------------------------------------------------------ entry point


async def build_script(
    spec: BuildSpec,
    llm: LlmProvider | None,
    memo: Memo,
    *,
    playhead_order: int = -1,
    ahead: int = 0,
    everything: bool = False,
) -> BuildResult:
    doc = spec.document
    states = stage1(doc, resolve_filters(spec.filters))
    calls: list[tuple[str, LlmCall]] = []
    complete = True
    degraded = False
    annotated_upto: int | None = None

    transformed: dict[int, dict[str, Any]] = {}  # first block order of a section → output
    secs: list[list[BlockState]] = []
    if spec.mode == "ai_enhanced" and spec.transform is not None:
        secs = sections(states)
        upto = (
            max((b.order for b in doc.blocks), default=-1) if everything else target_order(secs, playhead_order, ahead)
        )
        ok = await run_transform(spec, secs, upto, llm, memo, calls)
        degraded = degraded or not ok
        covered = -1
        contiguous = True
        for sec in secs:
            out = memo.transform.get(transform_key(spec, sec))
            if out is None:
                contiguous = False
                continue
            transformed[sec[0].block.order] = out
            if contiguous:
                covered = sec[-1].block.order
        complete = len(transformed) == len(secs)
        annotated_upto = covered if not complete else max((b.order for b in doc.blocks), default=-1)
    elif spec.mode == "ai_enhanced" and spec.annotate:
        batches = make_batches(states)
        upto = (
            max((b.order for b in doc.blocks), default=-1)
            if everything
            else target_order(batches, playhead_order, ahead)
        )
        ok = await run_annotate(spec, batches, upto, llm, memo, calls)
        degraded = degraded or not ok
        annotated_upto = apply_annotations(spec, states, memo)
        complete = annotated_upto >= max((b.order for b in doc.blocks), default=-1)

    # Lexicon after filters and span ops, then a final tidy.
    for st in states:
        if st.mt is not None and st.drop is None:
            st.hits.extend(apply_lexicon(st.block, st.mt, spec.lexicon))
            st.mt.cleanup(punctuation=st.block.type != BlockType.code)

    asm = _Assembler(spec)
    in_section: dict[str, int] = {}
    for sec in secs:
        if sec[0].block.order in transformed:
            for st in sec:
                in_section[st.block.id] = sec[0].block.order
    sec_by_first = {sec[0].block.order: sec for sec in secs}
    for st in states:
        asm.note_heading(st.block)
        if st.drop is not None:
            asm.removed.append(st.drop)
            continue
        first = in_section.get(st.block.id)
        if first is not None:
            if st.block.order == first and spec.transform is not None:
                asm.transformed(sec_by_first[first], transformed[first], spec.transform)
            continue
        if st.mt is not None:
            asm.block(st)

    script = SpeechScript(
        document_id=doc.id,
        plan_key=spec.plan_key,
        mode=spec.mode,
        utterances=asm.utterances,
        removed=asm.removed,
        annotated_upto_block=annotated_upto,
        warnings=list(dict.fromkeys(spec.warnings)),
    )
    return BuildResult(script=script, calls=calls, complete=complete, degraded=degraded)
