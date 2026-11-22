"""Speech diff (06 § Speech diff): preview what a proposed rule changes on the first ~2 pages (≤ 60 blocks).

The diff is computed by planning the window twice, with and without the proposed rule: removals (whole
blocks or spans), replacements (old → new), transforms (generated script per source section), what the
rule brings back (a "read X" rule overriding a skip), counts, and one spoken summary sentence plus a
scope-aware question, e.g. "On this page that removes 14 citations and 2 footnotes. Save it for arxiv.org?"
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Sequence

from open_speech.api.schemas import DiffRemoval, DiffReplacement, DiffTransform, SpeechDiff
from open_speech.config import DEFAULT_LLM
from open_speech.document.model import Block, BlockType, Document, MarkKind, SourceKind
from open_speech.errors import MissingKey
from open_speech.plan.build import BuildSpec, Memo, build_script
from open_speech.plan.keys import active_rules
from open_speech.plan.script import RuleHit
from open_speech.providers.base import LlmCall, LlmProvider
from open_speech.rules.lexicon import Lexicon
from open_speech.rules.model import Rule
from open_speech.rules.scope import describe_scope

__all__ = ["preview_window", "speech_diff", "spoken_summary"]

_BLOCK_NOUNS: dict[BlockType, tuple[str, str]] = {
    BlockType.heading: ("heading", "headings"),
    BlockType.paragraph: ("paragraph", "paragraphs"),
    BlockType.list_item: ("list item", "list items"),
    BlockType.quote: ("quote", "quotes"),
    BlockType.code: ("code block", "code blocks"),
    BlockType.table: ("table", "tables"),
    BlockType.caption: ("caption", "captions"),
    BlockType.figure: ("figure", "figures"),
    BlockType.footnote: ("footnote", "footnotes"),
    BlockType.header: ("header", "headers"),
    BlockType.footer: ("footer", "footers"),
    BlockType.page_number: ("page number", "page numbers"),
    BlockType.equation: ("equation", "equations"),
    BlockType.toc: ("table of contents entry", "table of contents entries"),
    BlockType.reference_entry: ("reference", "references"),
}
_MARK_NOUNS: dict[MarkKind, tuple[str, str]] = {
    MarkKind.citation_ref: ("citation", "citations"),
    MarkKind.link: ("link", "links"),
    MarkKind.url: ("URL", "URLs"),
    MarkKind.emphasis: ("emphasized phrase", "emphasized phrases"),
    MarkKind.parenthetical: ("parenthetical", "parentheticals"),
    MarkKind.bracket: ("bracketed aside", "bracketed asides"),
    MarkKind.brace: ("braced span", "braced spans"),
    MarkKind.formula: ("formula", "formulas"),
    MarkKind.footnote_ref: ("footnote marker", "footnote markers"),
}
_PASSAGE = ("passage", "passages")


def preview_window(document: Document, max_blocks: int = 60) -> list[Block]:
    """First ~2 pages (paged documents) or the first `max_blocks` blocks."""
    blocks = sorted(document.blocks, key=lambda b: b.order)
    pages = [b.page for b in blocks if b.page is not None]
    if pages:
        first = min(pages)
        blocks = [b for b in blocks if b.page is None or b.page <= first + 1]
    return blocks[:max_blocks]


def _category(hit: RuleHit, block: Block | None) -> tuple[str, str]:
    if block is None:
        return _PASSAGE
    if hit.action in ("drop_block", "keep_only", "transform"):
        return _BLOCK_NOUNS.get(block.type, _PASSAGE)
    lo, hi = hit.src_start or 0, hit.src_end or 0
    best = None
    for m in block.marks:
        if m.start < hi and m.end > lo:
            overlap = min(hi, m.end) - max(lo, m.start)
            if best is None or overlap > best[0]:
                best = (overlap, m.kind)
    return _MARK_NOUNS[best[1]] if best else _PASSAGE


def _plural(n: int, nouns: tuple[str, str]) -> str:
    return f"{n} {nouns[0] if n == 1 else nouns[1]}"


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _where(doc: Document, window: Sequence[Block]) -> str:
    pages = {b.page for b in window if b.page is not None}
    if doc.kind == SourceKind.pdf:
        return "On these pages" if len(pages) > 1 else "On this page"
    if doc.kind == SourceKind.web:
        return "On this page"
    return "In this document"


def spoken_summary(
    where: str,
    removed: Counter[tuple[str, str]],
    restored: Counter[tuple[str, str]],
    replacements: int,
    transforms: int,
    scope_phrase: str,
) -> str:
    parts: list[str] = []
    if removed:
        cats = dict(removed)
        if any(k[0] == "footnote" for k in cats):
            cats.pop(_MARK_NOUNS[MarkKind.footnote_ref], None)  # "2 footnotes" says enough
        ranked = sorted(cats.items(), key=lambda kv: (-kv[1], kv[0][0]))
        phrases = [_plural(n, nouns) for nouns, n in ranked[:3]]
        rest = sum(n for _, n in ranked[3:])
        if rest:
            phrases.append(_plural(rest, ("other passage", "other passages")))
        parts.append("removes " + _join(phrases))
    if replacements:
        parts.append("changes " + _plural(replacements, ("phrase", "phrases")))
    if transforms:
        parts.append("rewrites " + _plural(transforms, ("section", "sections")))
    if restored:
        ranked_r = sorted(restored.items(), key=lambda kv: (-kv[1], kv[0][0]))
        parts.append("brings back " + _join([_plural(n, nouns) for nouns, n in ranked_r[:3]]))
    if not parts:
        return f"{where} it doesn't change anything. Save it {scope_phrase} anyway?"
    return f"{where} that {_join(parts)}. Save it {scope_phrase}?"


def _covers(hits: Sequence[RuleHit], h: RuleHit) -> bool:
    lo, hi = h.src_start or 0, h.src_end or 0
    return any(o.block_id == h.block_id and (o.src_start or 0) <= lo and (o.src_end or 0) >= hi for o in hits)


async def speech_diff(
    document: Document,
    *,
    current: Sequence[Rule],
    proposed: Rule,
    lexicon: Lexicon,
    speech_md: str,
    llm: LlmProvider | None,
    max_blocks: int = 60,
    llm_model: str = DEFAULT_LLM,
) -> tuple[SpeechDiff, list[tuple[str, LlmCall]]]:
    """Preview `proposed` on the start of `document`. `llm_model` is an addition (pins the memo key)."""
    window = preview_window(document, max_blocks)
    doc = document.model_copy(update={"blocks": window})
    by_id = {b.id: b for b in window}
    # The proposal is the newest edit, so it wins ties within its precedence level.
    prop = proposed.model_copy(update={"updated_at": max(time.time(), proposed.updated_at), "enabled": True})
    base = active_rules([r for r in current if r.id != prop.id], doc)
    entries = lexicon.for_document(doc)
    base_filters = [r for r in base if r.tier == "filter" and r.compiled is not None]

    needs_llm = prop.tier in ("annotate", "transform")
    if needs_llm and llm is None:
        raise MissingKey("anthropic", "Previewing that rule")

    before = await build_script(
        BuildSpec(doc, base_filters, [], None, entries, speech_md, "classic", "", "diff-before"),
        None,
        Memo(),
        everything=True,
    )
    after_filters = base_filters + ([prop] if prop.tier == "filter" and prop.compiled is not None else [])
    after = await build_script(
        BuildSpec(
            doc,
            after_filters,
            [prop] if prop.tier == "annotate" else [],
            prop if prop.tier == "transform" else None,
            entries,
            speech_md,
            "ai_enhanced" if needs_llm else "classic",
            llm_model if needs_llm else "",
            "diff-after",
        ),
        llm,
        Memo(),
        everything=True,
    )

    diff = SpeechDiff(document_id=document.id)
    removed_counts: Counter[tuple[str, str]] = Counter()
    before_removed = before.script.removed
    after_removed = after.script.removed
    for h in after_removed:
        if h.action == "transform" or _covers(before_removed, h):
            continue
        whole = h.action in ("drop_block", "keep_only")
        diff.removals.append(
            DiffRemoval(block_id=h.block_id, text=h.original, rule_name=h.rule_name, whole_block=whole)
        )
        removed_counts[_category(h, by_id.get(h.block_id))] += 1

    restored_counts: Counter[tuple[str, str]] = Counter()
    for h in before_removed:
        if not _covers(after_removed, h):
            restored_counts[_category(h, by_id.get(h.block_id))] += 1

    def _replacements(hits: Sequence[RuleHit]) -> set[tuple[str, int | None, int | None, str]]:
        return {(h.block_id, h.src_start, h.src_end, h.replacement) for h in hits if h.action == "replace"}

    before_repl = _replacements([h for u in before.script.utterances for h in u.rule_trace])
    for u in after.script.utterances:
        for h in u.rule_trace:
            if h.action == "replace" and (h.block_id, h.src_start, h.src_end, h.replacement) not in before_repl:
                diff.replacements.append(
                    DiffReplacement(block_id=h.block_id, old=h.original, new=h.replacement, rule_name=h.rule_name)
                )

    groups: dict[tuple[str, ...], list[str]] = {}
    for u in after.script.utterances:
        if u.fidelity in ("block", "section") and any(h.action == "transform" for h in u.rule_trace):
            groups.setdefault(tuple(u.block_ids), []).append(u.text)
    transformed_blocks = {h.block_id for h in after_removed if h.action == "transform"}
    for ids, texts in groups.items():
        diff.transforms.append(DiffTransform(source_block_ids=list(ids), script=texts))
    # Sections rewritten to nothing ("read only the abstract") still count as transforms.
    covered = {b for ids in groups for b in ids}
    silent = [b for b in transformed_blocks if b not in covered]
    if silent:
        diff.transforms.append(DiffTransform(source_block_ids=sorted(silent, key=lambda b: by_id[b].order), script=[]))

    diff.counts = {
        "removals": len(diff.removals),
        "whole_blocks": sum(1 for r in diff.removals if r.whole_block),
        "spans": sum(1 for r in diff.removals if not r.whole_block),
        "replacements": len(diff.replacements),
        "transforms": len(diff.transforms),
        "restored": sum(restored_counts.values()),
        **{nouns[1]: n for nouns, n in removed_counts.items()},
    }
    diff.summary = spoken_summary(
        _where(document, window),
        removed_counts,
        restored_counts,
        len(diff.replacements),
        len([t for t in diff.transforms if t.script]) or len(diff.transforms),
        describe_scope(prop.scope),
    )
    return diff, after.calls
