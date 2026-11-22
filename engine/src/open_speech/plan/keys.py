"""Plan cache keys (05 § Caches): `sha256(source_id, rules_hash, model_id, prompt_version, mode)`.

The rules hash used here is *mode-effective*: Classic never calls Claude, so a Classic plan's key covers only
the filter rules and the lexicon (editing an annotate rule, SPEECH.md or the model doesn't invalidate it).
AI Enhanced keys cover every matching rule, the lexicon, SPEECH.md and the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from open_speech.api.schemas import LexiconEntry
from open_speech.document.model import Document
from open_speech.ids import sha256_hex
from open_speech.rules.hashing import compute_rules_hash
from open_speech.rules.model import Rule
from open_speech.rules.presets import preset_spec
from open_speech.rules.scope import by_precedence, scope_matches

PROMPT_VERSION = "annotate-2/transform-1"
PLANNER_VERSION = "planner-1"

Mode = Literal["classic", "ai_enhanced"]


@dataclass
class Tiering:
    filters: list[Rule]
    annotate: list[Rule]  # rules whose instruction goes to the annotate tier
    transform: Rule | None
    inactive: list[Rule]  # rules that need AI Enhanced but the mode is Classic
    extra_transforms: list[Rule]


def active_rules(rules: Sequence[Rule], doc: Document) -> list[Rule]:
    """Enabled, scope-matching rules in precedence order (defensive: the store already does this)."""
    return by_precedence(r for r in rules if r.enabled and scope_matches(r.scope, doc))


def tiering(rules: Sequence[Rule], mode: Mode) -> Tiering:
    filters = [r for r in rules if r.tier == "filter" and r.compiled is not None]
    if mode == "classic":
        inactive = [r for r in rules if r.tier in ("annotate", "transform")]
        return Tiering(filters, [], None, inactive, [])
    annotate: list[Rule] = []
    for r in rules:
        if r.tier == "annotate":
            annotate.append(r)
        elif r.preset and r.tier == "filter":
            spec = preset_spec(r.name)
            if spec is not None and spec.ai_annotate:
                annotate.append(r)
    transforms = [r for r in rules if r.tier == "transform"]
    return Tiering(filters, annotate, transforms[0] if transforms else None, [], transforms[1:])


def effective_rules_hash(
    rules: Sequence[Rule], lexicon_entries: Sequence[LexiconEntry], speech_md: str, mode: Mode
) -> str:
    if mode == "classic":
        return compute_rules_hash([r for r in rules if r.tier == "filter"], lexicon_entries, "")
    return compute_rules_hash(rules, lexicon_entries, speech_md)


def plan_key(source_id: str, rules_hash: str, model_id: str, prompt_version: str, mode: str) -> str:
    return sha256_hex(source_id, rules_hash, model_id, prompt_version, mode, PLANNER_VERSION)
