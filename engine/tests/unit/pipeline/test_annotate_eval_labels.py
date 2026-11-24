"""The AI Enhanced accuracy gate's hand labels (tests/live/annotate_expectations.json) are sound: every
must-keep span is spoken by Classic, and every must-drop span is one Classic leaves in, so the live gate
measures only what the model does."""

from __future__ import annotations

from pathlib import Path

from open_speech.config import Paths
from tests.live.annotate_eval import expectations, load_documents, plan, rule_store, spoken


async def test_labels_hold_in_the_classic_output(tmp_path: Path) -> None:
    paths = Paths(tmp_path).ensure()
    store = rule_store(paths)
    docs = await load_documents(paths)
    problems: list[str] = []
    for name, doc in docs.items():
        said = spoken(await plan(doc, store, None, mode="classic"))
        labels = expectations()[name]
        problems += [f"{name}: must_keep not spoken by Classic: {k!r}" for k in labels["must_keep"] if k not in said]
        problems += [f"{name}: must_drop already gone in Classic: {d!r}" for d in labels["must_drop"] if d not in said]
    assert problems == []
    assert sum(len(v["must_drop"]) for v in expectations().values()) >= 8
    assert sum(len(v["must_keep"]) for v in expectations().values()) >= 40
