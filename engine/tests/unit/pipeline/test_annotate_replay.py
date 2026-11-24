"""The AI Enhanced accuracy gate, offline: the responses recorded live for the shipped annotate configuration
(tests/live/test_annotate_eval.py) replayed through the real request path. Guards the parts that change without
a model call: the prompt, the request shape, the guard, quote location and the labels.

When the request changes (prompt, schema, rules, catalog traits), the recorded keys no longer match: run the live
gate again (`OPEN_SPEECH_LIVE=1 OPEN_SPEECH_EVAL_RECORD=passing uv run pytest tests/live/test_annotate_eval.py -s`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2

from open_speech.config import Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.plan.keys import PROMPT_VERSION
from open_speech.providers.anthropic_llm import AnthropicLlm
from tests.live.annotate_eval import (
    MAX_UNMATCHED,
    OpCounts,
    count_ops,
    load_documents,
    plan,
    request_key,
    rule_store,
    score,
    shipped_variant,
)


async def test_shipped_annotate_config_passes_the_gate_on_recorded_responses(tmp_path: Path, monkeypatch: Any) -> None:
    from anthropic import AsyncAnthropic

    from open_speech.plan import build as plan_build

    variant = shipped_variant()
    path = variant.replay_path()
    assert path.exists(), f"no recorded gate run for the shipped annotate config ({variant.name}): run the live gate"
    recording = json.loads(path.read_text("utf-8"))
    assert recording["prompt_version"] == PROMPT_VERSION, "the prompt changed since the recording: re-run the gate"
    responses = {c["key"]: c["response"] for c in recording["calls"]}
    unknown: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        resp = responses.get(request_key(body))
        if resp is None:
            unknown.append(body["messages"][0]["content"][:120])
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                          "message": "not recorded"}})
        return httpx2.Response(200, json=resp)

    client = AsyncAnthropic(api_key="sk-ant-test", base_url="https://anthropic.test", max_retries=0,
                            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    # The bundled catalog and default settings, not the variant's: the replay proves the shipped request is the
    # one the gate measured.
    llm = AnthropicLlm(ModelCatalog.load(), "", model=variant.model, annotate_effort=variant.effort, client=client)
    counts = OpCounts()
    monkeypatch.setattr(plan_build, "locate_ops", count_ops(counts))
    paths = Paths(tmp_path).ensure()
    store = rule_store(paths)
    scores = [score(name, await plan(doc, store, llm)) for name, doc in (await load_documents(paths)).items()]

    assert unknown == [], "requests changed since the recording: re-run the live gate"
    assert [f"{s.document}: {k}" for s in scores for k in s.keep_violations] == []
    dropped = sum(len(s.dropped) for s in scores)
    total = dropped + sum(len(s.missed) for s in scores)
    assert dropped / total >= (recording["baseline_drop_recall"] or 1.0), [m for s in scores for m in s.missed]
    assert counts.total > 0 and counts.unmatched / counts.total <= MAX_UNMATCHED
