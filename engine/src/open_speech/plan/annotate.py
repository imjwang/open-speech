"""Tier 2 (05 § Tier 2): Claude span ops, applied by exact quote + occurrence.

Claude never returns numeric offsets. The engine finds the n-th non-overlapping occurrence of the exact
quote in the block text it sent and computes offsets itself. Ops that don't match exactly, that overlap an
earlier op, or that name a block that wasn't in the batch are dropped and logged — never applied fuzzily.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from open_speech.document.model import Block
from open_speech.plan.script import RuleHit, SpanOp
from open_speech.plan.textmap import MappedText
from open_speech.rules.model import Rule

log = logging.getLogger("open_speech.plan.annotate")

AI_RULE_ID = "ai_enhanced"
AI_RULE_NAME = "AI Enhanced"


@dataclass(frozen=True)
class LocatedOp:
    op: SpanOp
    start: int
    end: int


def find_occurrence(text: str, quote: str, occurrence: int) -> int:
    """Index of the `occurrence`-th (1-based) non-overlapping exact match of `quote`, or -1."""
    if not quote or occurrence < 1:
        return -1
    pos = 0
    idx = -1
    for _ in range(occurrence):
        idx = text.find(quote, pos)
        if idx < 0:
            return -1
        pos = idx + len(quote)
    return idx


def parse_ops(output: Any) -> tuple[list[SpanOp], int]:
    """Coerce provider output (list of SpanOp/dicts, or {"ops": [...]}) into SpanOps; count invalid ones."""
    raw: Iterable[Any]
    if isinstance(output, Mapping):
        raw = output.get("ops", []) or []
    elif isinstance(output, list | tuple):
        raw = output
    else:
        return [], 0
    ops: list[SpanOp] = []
    bad = 0
    for item in raw:
        try:
            ops.append(item if isinstance(item, SpanOp) else SpanOp.model_validate(item))
        except ValidationError:
            bad += 1
    return ops, bad


def locate_ops(text: str, ops: Sequence[SpanOp]) -> tuple[list[LocatedOp], list[str]]:
    """Locate ops for one block. Returns (located ops, reasons for each dropped op)."""
    located: list[LocatedOp] = []
    dropped: list[str] = []
    for op in ops:
        if op.op == "replace" and op.text == op.quote:
            continue  # no-op
        idx = find_occurrence(text, op.quote, op.occurrence)
        if idx < 0:
            dropped.append(f"{op.block_id}: quote {op.quote!r} (occurrence {op.occurrence}) not found")
            continue
        start, end = idx, idx + len(op.quote)
        if any(start < lo.end and end > lo.start for lo in located):
            dropped.append(f"{op.block_id}: quote {op.quote!r} overlaps another op")
            continue
        located.append(LocatedOp(op, start, end))
    return located, dropped


def attribute(op: SpanOp, rules_by_id: Mapping[str, Rule], annotate_rules: Sequence[Rule]) -> tuple[str, str]:
    if op.rule_id and op.rule_id in rules_by_id:
        r = rules_by_id[op.rule_id]
        return r.id, r.name
    if len(annotate_rules) == 1:
        return annotate_rules[0].id, annotate_rules[0].name
    return AI_RULE_ID, AI_RULE_NAME


def apply_ops(
    block: Block,
    mt: MappedText,
    ops: Sequence[SpanOp],
    *,
    rules_by_id: Mapping[str, Rule],
    annotate_rules: Sequence[Rule],
) -> tuple[list[RuleHit], list[str]]:
    """Apply span ops to `mt` (whose text is what Claude saw). Returns (hits, dropped reasons)."""
    located, dropped = locate_ops(mt.text, [o for o in ops if o.block_id == block.id])
    hits: list[RuleHit] = []
    for lo in sorted(located, key=lambda x: x.start, reverse=True):
        rng = mt.source_range(lo.start, lo.end)
        if rng is None:
            continue
        rid, rname = attribute(lo.op, rules_by_id, annotate_rules)
        if lo.op.op == "skip" or not lo.op.text:
            mt.delete(lo.start, lo.end)
            action, replacement = "skip", ""
        else:
            mt.edit(lo.start, lo.end, lo.op.text)
            action, replacement = "replace", lo.op.text
        hits.append(
            RuleHit(
                rule_id=rid,
                rule_name=rname,
                action=action,  # type: ignore[arg-type]
                block_id=block.id,
                src_start=rng[0],
                src_end=rng[1],
                original=block.text[rng[0] : rng[1]],
                replacement=replacement,
                tier="annotate",
            )
        )
    hits.reverse()
    for reason in dropped:
        log.warning("dropped span op %s", reason)
    return hits, dropped
