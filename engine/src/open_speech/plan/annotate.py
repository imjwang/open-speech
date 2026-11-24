"""Tier 2 (05 § Tier 2): Claude span ops, applied by exact quote + occurrence.

Claude never returns numeric offsets. The engine finds the n-th non-overlapping occurrence of the exact
quote in the block text it sent and computes offsets itself. Ops that don't match exactly, that overlap an
earlier op, or that name a block that wasn't in the batch are dropped and logged — never applied fuzzily.

What Claude is sent is the Classic-filtered text with a marker (`⟦url⟧`, `⟦citation⟧`, …) wherever a filter
already removed a span (`MarkedText`). Without the markers a sentence such as "More details are given at
together with the evaluation scripts." looks damaged, and the model "repairs" it by deleting more. Markers
have zero width in the spoken text: a quote may run across one, and a quote that is only a marker changes
nothing. Before an op is applied, an `OpCheck` (plan/guard.py) confirms that it removes no more than its
rule covers; ops that do are dropped and logged like any other op that can't be applied as written.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from open_speech.document.model import Block, MarkKind
from open_speech.plan.script import CUT_CLOSE, CUT_OPEN, SPEECH_MD_RULE_ID, RuleHit, SpanOp
from open_speech.plan.textmap import _CLOSERS, _OPENERS, MappedText
from open_speech.rules.model import Rule

log = logging.getLogger("open_speech.plan.annotate")

AI_RULE_ID = "ai_enhanced"
AI_RULE_NAME = "AI Enhanced"
SPEECH_MD_NAME = "SPEECH.md"

_CUT_LABELS: dict[MarkKind, str] = {
    MarkKind.citation_ref: "citation",
    MarkKind.url: "url",
    MarkKind.footnote_ref: "footnote",
    MarkKind.parenthetical: "parenthetical",
    MarkKind.bracket: "bracket",
    MarkKind.brace: "brace",
    MarkKind.formula: "formula",
    MarkKind.link: "link",
    MarkKind.emphasis: "emphasis",
}
_CUT_DEFAULT = "removed"


@dataclass(frozen=True)
class Cut:
    """A place in the filtered text where a Classic filter removed a span."""

    pos: int  # offset into the filtered (spoken) text
    label: str  # what was there: "url", "citation", …


@dataclass(frozen=True)
class MarkedText:
    """One block as Claude sees it: `plain` (what would be spoken) with a marker inserted at every cut."""

    text: str
    plain: str
    inserts: tuple[tuple[int, int], ...] = ()  # [start, end) of each inserted marker in `text`, ascending

    @classmethod
    def of(cls, plain: str) -> MarkedText:
        return cls(plain, plain)

    def to_plain(self, offset: int) -> int:
        """Offset into `plain` for an offset into `text` (a position inside a marker maps to its cut)."""
        shift = 0
        for a, b in self.inserts:
            if offset <= a:
                break
            shift += min(offset, b) - a
        return offset - shift


def mark_cuts(plain: str, cuts: Sequence[Cut]) -> MarkedText:
    """Insert `⟦label⟧` at each cut, spaced so the marker reads as a word of its own."""
    by_pos: dict[int, list[str]] = {}
    for c in cuts:
        labels = by_pos.setdefault(min(max(c.pos, 0), len(plain)), [])
        if c.label not in labels:
            labels.append(c.label)
    if not by_pos:
        return MarkedText.of(plain)
    parts: list[str] = []
    inserts: list[tuple[int, int]] = []
    last = 0
    size = 0
    for pos in sorted(by_pos):
        parts.append(plain[last:pos])
        size += pos - last
        left = plain[pos - 1] if pos > 0 else ""
        right = plain[pos] if pos < len(plain) else ""
        pre = " " if left and not left.isspace() and left not in _OPENERS else ""
        post = " " if right and not right.isspace() and right not in _CLOSERS else ""
        marker = f"{pre}{CUT_OPEN}{', '.join(by_pos[pos])}{CUT_CLOSE}{post}"
        parts.append(marker)
        inserts.append((size, size + len(marker)))
        size += len(marker)
        last = pos
    parts.append(plain[last:])
    return MarkedText("".join(parts), plain, tuple(inserts))


def cuts_for(block: Block, mt: MappedText, hits: Sequence[RuleHit]) -> list[Cut]:
    """The cuts left in `mt` by Classic skip hits, labelled by the kind of mark that was removed."""
    cuts: list[Cut] = []
    for h in hits:
        if h.action != "skip" or h.src_start is None or h.src_end is None:
            continue
        best: tuple[int, MarkKind] | None = None
        for m in block.marks:
            overlap = min(h.src_end, m.end) - max(h.src_start, m.start)
            if overlap > 0 and (best is None or overlap > best[0]):
                best = (overlap, m.kind)
        label = _CUT_LABELS.get(best[1], _CUT_DEFAULT) if best else _CUT_DEFAULT
        cuts.append(Cut(mt.offset_of_source(h.src_start), label))
    return cuts


class OpCheck(Protocol):
    """Decides whether an op may be applied. `removed` is the spoken text the op would take away."""

    def check(self, op: SpanOp, removed: str) -> str | None: ...  # a reason to refuse, or None


@dataclass(frozen=True)
class LocatedOp:
    op: SpanOp
    start: int  # offsets into the plain (spoken) text
    end: int


@dataclass(frozen=True)
class DroppedOp:
    reason: str
    overreach: bool = False  # refused by the guard (matched the text, but removes more than its rule covers)


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


def locate_ops(
    view: MarkedText | str, ops: Sequence[SpanOp], guard: OpCheck | None = None
) -> tuple[list[LocatedOp], list[DroppedOp]]:
    """Locate ops for one block in the text Claude saw. Returns (located ops, dropped ops with reasons)."""
    if isinstance(view, str):
        view = MarkedText.of(view)
    located: list[LocatedOp] = []
    dropped: list[DroppedOp] = []
    for op in ops:
        if op.op == "replace" and op.text == op.quote:
            continue  # no-op
        idx = find_occurrence(view.text, op.quote, op.occurrence)
        if idx >= 0:
            start, end = view.to_plain(idx), view.to_plain(idx + len(op.quote))
        else:
            # The model may have quoted the sentence as if the markers weren't there: still an exact match.
            idx = find_occurrence(view.plain, op.quote, op.occurrence) if view.inserts else -1
            if idx < 0:
                dropped.append(DroppedOp(f"{op.block_id}: quote {op.quote!r} (occurrence {op.occurrence}) not found"))
                continue
            start, end = idx, idx + len(op.quote)
        if end <= start:
            continue  # the quote was only a marker: nothing spoken to change
        if any(start < lo.end and end > lo.start for lo in located):
            dropped.append(DroppedOp(f"{op.block_id}: quote {op.quote!r} overlaps another op"))
            continue
        refusal = guard.check(op, view.plain[start:end]) if guard is not None else None
        if refusal is not None:
            dropped.append(DroppedOp(f"{op.block_id}: {refusal}", overreach=True))
            continue
        located.append(LocatedOp(op, start, end))
    return located, dropped


def attribute(op: SpanOp, rules_by_id: Mapping[str, Rule], annotate_rules: Sequence[Rule]) -> tuple[str, str]:
    if op.rule_id and op.rule_id in rules_by_id:
        r = rules_by_id[op.rule_id]
        return r.id, r.label
    if op.rule_id == SPEECH_MD_RULE_ID:
        return SPEECH_MD_RULE_ID, SPEECH_MD_NAME
    if len(annotate_rules) == 1:
        return annotate_rules[0].id, annotate_rules[0].label
    return AI_RULE_ID, AI_RULE_NAME


def apply_ops(
    block: Block,
    mt: MappedText,
    ops: Sequence[SpanOp],
    *,
    rules_by_id: Mapping[str, Rule],
    annotate_rules: Sequence[Rule],
    view: MarkedText | None = None,
    guard: OpCheck | None = None,
) -> tuple[list[RuleHit], list[DroppedOp]]:
    """Apply span ops to `mt` (whose text is what Claude saw, minus markers). Returns (hits, dropped ops)."""
    if view is None or view.plain != mt.text:
        view = MarkedText.of(mt.text)
    located, dropped = locate_ops(view, [o for o in ops if o.block_id == block.id], guard)
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
    for d in dropped:
        log.warning("dropped span op %s", d.reason)
    return hits, dropped
