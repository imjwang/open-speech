"""`rules_hash` (05 § Caches): fingerprint of the rules that apply to a document, the lexicon rows that
apply to it, and `SPEECH.md`. The rule order is part of the hash because order encodes precedence."""

from __future__ import annotations

import json
from collections.abc import Sequence

from open_speech.api.schemas import LexiconEntry
from open_speech.ids import sha256_hex
from open_speech.rules.lexicon import Lexicon
from open_speech.rules.model import Rule
from open_speech.rules.scope import normalized_scope


def rule_fingerprint(rule: Rule) -> str:
    data = {
        "id": rule.id,
        "enabled": rule.enabled,
        "tier": rule.tier,
        "compiled": rule.compiled.to_file_dict() if rule.compiled is not None else None,
        "instruction": rule.instruction,
        "scope": normalized_scope(rule.scope).model_dump(),
    }
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def compute_rules_hash(rules: Sequence[Rule], lexicon_entries: Sequence[LexiconEntry], speech_md: str) -> str:
    return sha256_hex(
        "rules-v1",
        *(rule_fingerprint(r) for r in rules),
        "lexicon",
        Lexicon.fingerprint(lexicon_entries),
        "speech_md",
        speech_md.strip(),
    )
