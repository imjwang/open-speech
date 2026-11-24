"""Tier 1 filters (05 § Tier 1, 06 § Compiled filters): deterministic, free, instant.

Decisions are made per block type and per mark kind by the highest-precedence rule that mentions it
(drop, keep, or keep-only), so a document-scoped "read footnotes" beats the global Footnotes preset.
Regex replacements from every active filter rule apply in precedence order to the remaining text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from open_speech.document.model import Block, BlockType, MarkKind
from open_speech.plan.script import RuleHit
from open_speech.plan.textmap import MappedText
from open_speech.rules.lexicon import Lexicon, LexiconEntry
from open_speech.rules.model import Rule
from open_speech.rules.scope import by_precedence

LEXICON_RULE_ID = "lexicon"
LEXICON_RULE_NAME = "Lexicon"


@dataclass
class BlockDecision:
    rule: Rule
    action: str  # "drop_block" | "keep_only"


@dataclass
class FilterPlan:
    blocks: dict[BlockType, BlockDecision] = field(default_factory=dict)
    marks: dict[MarkKind, Rule] = field(default_factory=dict)
    replacements: list[tuple[Rule, re.Pattern[str], str]] = field(default_factory=list)

    def drop_for(self, block: Block) -> BlockDecision | None:
        return self.blocks.get(block.type)


def resolve_filters(rules: Sequence[Rule]) -> FilterPlan:
    """Turn active filter rules (any order) into per-type decisions."""
    plan = FilterPlan()
    decided_blocks: set[BlockType] = set()
    decided_marks: set[MarkKind] = set()
    for rule in by_precedence(r for r in rules if r.enabled and r.compiled is not None and r.tier == "filter"):
        c = rule.compiled
        assert c is not None
        for bt in BlockType:
            if bt in decided_blocks:
                continue
            if bt in c.drop_blocks:
                plan.blocks[bt] = BlockDecision(rule, "drop_block")
                decided_blocks.add(bt)
            elif bt in c.keep_blocks:
                decided_blocks.add(bt)
            elif c.keep_blocks_only is not None:
                if bt not in c.keep_blocks_only:
                    plan.blocks[bt] = BlockDecision(rule, "keep_only")
                decided_blocks.add(bt)
        for mk in MarkKind:
            if mk in decided_marks:
                continue
            if mk in c.drop_marks:
                plan.marks[mk] = rule
                decided_marks.add(mk)
            elif mk in c.keep_marks:
                decided_marks.add(mk)
        for spec in c.replace:
            plan.replacements.append((rule, re.compile(spec.pattern), spec.with_))
    return plan


def _hit(
    rule_id: str,
    rule_name: str,
    action: str,
    block: Block,
    rng: tuple[int, int],
    replacement: str = "",
    tier: str = "filter",
) -> RuleHit:
    return RuleHit(
        rule_id=rule_id,
        rule_name=rule_name,
        action=action,  # type: ignore[arg-type]
        block_id=block.id,
        src_start=rng[0],
        src_end=rng[1],
        original=block.text[rng[0] : rng[1]],
        replacement=replacement,
        tier=tier,  # type: ignore[arg-type]
    )


def block_drop_hit(block: Block, decision: BlockDecision) -> RuleHit:
    return _hit(decision.rule.id, decision.rule.label, decision.action, block, (0, len(block.text)))


def apply_mark_drops(block: Block, mt: MappedText, plan: FilterPlan) -> list[RuleHit]:
    """Delete dropped marks (source offsets; `mt` must still be untouched)."""
    spans: list[tuple[int, int, Rule]] = []
    for mark in block.marks:
        rule = plan.marks.get(mark.kind)
        if rule is not None and 0 <= mark.start < mark.end <= len(block.text):
            spans.append((mark.start, mark.end, rule))
    if not spans:
        return []
    # Union overlapping spans; the enclosing (earliest, longest) span's rule gets the credit.
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[tuple[int, int, Rule]] = []
    for s, e, r in spans:
        if merged and s < merged[-1][1]:
            ms, me, mr = merged[-1]
            merged[-1] = (ms, max(me, e), mr)
        else:
            merged.append((s, e, r))
    hits: list[RuleHit] = []
    for s, e, rule in reversed(merged):
        rng = mt.delete(s, e)
        if rng is not None:
            hits.append(_hit(rule.id, rule.label, "skip", block, rng))
    hits.reverse()
    return hits


def apply_replacements(block: Block, mt: MappedText, plan: FilterPlan) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for rule, rx, template in plan.replacements:
        text = mt.text
        matches = [m for m in rx.finditer(text) if m.end() > m.start()]
        for m in reversed(matches):
            new = m.expand(template)
            rng = mt.source_range(m.start(), m.end())
            if rng is None:
                continue
            if new:
                mt.edit(m.start(), m.end(), new)
                hits.append(_hit(rule.id, rule.label, "replace", block, rng, new))
            else:
                mt.delete(m.start(), m.end())
                hits.append(_hit(rule.id, rule.label, "skip", block, rng))
    return hits


def apply_lexicon(block: Block, mt: MappedText, entries: Sequence[LexiconEntry]) -> list[RuleHit]:
    """Respell lexicon terms in the remaining text (only where the text is still verbatim source)."""
    if not entries:
        return []
    hits: list[RuleHit] = []
    for m in reversed(Lexicon.find(mt.text, entries)):
        if not mt.is_exact_range(m.start, m.end):
            continue
        rng = mt.edit(m.start, m.end, m.spoken)
        if rng is not None:
            hits.append(_hit(LEXICON_RULE_ID, LEXICON_RULE_NAME, "replace", block, rng, m.spoken))
    hits.reverse()
    return hits
