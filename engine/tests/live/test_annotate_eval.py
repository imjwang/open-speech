"""AI Enhanced accuracy gate, live (05 § Tier 2): may Haiku 5.5 annotate instead of Sonnet 5.5?

Runs only with OPEN_SPEECH_LIVE=1 and an Anthropic key in the environment or the repo `.env` (read by the
engine's own config). Every variant plans every labelled document (tests/live/annotate_expectations.json)
through the real pipeline, `OPEN_SPEECH_EVAL_RUNS` times (default 3), interleaved so no variant gets a warmer
cache or a quieter minute than another. It reports, per variant: must-keep violations, must-drop recall, guard
refusals (plan/guard.py), unmatched quotes, model refusals, latency, cost and prompt-cache reads.

Pass bar: 0 must-keep violations in every run, recall at least the Sonnet baseline's, unmatched quotes ≤ 5 %.
The test fails if the variant the catalog ships for Haiku (`annotate_thinking` + the default effort) misses it.

    OPEN_SPEECH_LIVE=1 uv run pytest tests/live/test_annotate_eval.py -s
        OPEN_SPEECH_EVAL_VARIANTS=sonnet-baseline,haiku-off-low   (default: all four)
        OPEN_SPEECH_EVAL_RUNS=3
        OPEN_SPEECH_EVAL_MAX_USD=0.45      stop early when the gate has spent this much
        OPEN_SPEECH_EVAL_RECORD=haiku-off-low   write that variant's first run as a replay fixture
                                                ("passing" = the cheapest passing Haiku variant)
        OPEN_SPEECH_EVAL_OUT=/some/dir      where the report (JSON + Markdown table) goes
"""

from __future__ import annotations

import datetime as dt
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from open_speech.config import EngineEnv, Paths
from open_speech.plan import build as plan_build
from open_speech.plan.keys import PROMPT_VERSION
from open_speech.providers.anthropic_llm import AnthropicLlm
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.registry import _anthropic_client
from open_speech.usage.pricing import Pricing
from tests.live.annotate_eval import (
    VARIANTS,
    OpCounts,
    RecordingClient,
    RunResult,
    count_ops,
    load_documents,
    passes,
    plan,
    rule_store,
    score,
    shipped_variant,
    summarize,
    table,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("OPEN_SPEECH_LIVE") != "1", reason="live provider test: set OPEN_SPEECH_LIVE=1"),
]


def _record(variant_name: str, run: RunResult, baseline: dict[str, Any] | None, summary: dict[str, Any]) -> Path:
    variant = VARIANTS[variant_name]
    path = variant.replay_path()
    seen: set[str] = set()
    calls = []
    for c in run.calls:
        if c.key not in seen:
            seen.add(c.key)
            calls.append({"key": c.key, "response": c.response})
    path.write_text(
        json.dumps(
            {
                "about": "Responses recorded by tests/live/test_annotate_eval.py for the AI Enhanced accuracy gate; "
                "replayed by tests/unit/pipeline/test_annotate_replay.py.",
                "variant": variant.name,
                "model": variant.model,
                "annotate_thinking": variant.annotate_thinking,
                "effort": variant.effort,
                "prompt_version": PROMPT_VERSION,
                "recorded_on": dt.date.today().isoformat(),
                "baseline_drop_recall": baseline["drop_recall"] if baseline else None,
                "live_summary": {k: summary[k] for k in ("keep_violations", "drop_recall", "unmatched_rate", "runs")},
                "calls": calls,
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        "utf-8",
    )
    return path


async def test_annotate_accuracy_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = EngineEnv()
    keys = ProviderKeys(env)
    if not keys.configured("anthropic"):
        pytest.skip("no Anthropic key in the environment or the repo .env")
    key = keys.get("anthropic")
    assert key is not None
    names = [n.strip() for n in os.environ.get("OPEN_SPEECH_EVAL_VARIANTS", ",".join(VARIANTS)).split(",") if n]
    unknown = set(names) - set(VARIANTS)
    assert not unknown, f"unknown variants {unknown}; choose from {list(VARIANTS)}"
    runs = int(os.environ.get("OPEN_SPEECH_EVAL_RUNS", "3"))
    max_usd = Decimal(os.environ.get("OPEN_SPEECH_EVAL_MAX_USD", "0.45"))
    out_dir = Path(os.environ.get("OPEN_SPEECH_EVAL_OUT") or tmp_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    paths = Paths(tmp_path / "home").ensure()
    store = rule_store(paths)
    docs = await load_documents(paths)
    pricing = Pricing.load()
    client = _anthropic_client(key, lambda: None)  # the engine's own client: one keep-alive pool
    results: dict[str, list[RunResult]] = {n: [] for n in names}
    spent = Decimal(0)
    stopped_early = False
    try:
        for run in range(runs):
            for name in names:
                variant = VARIANTS[name]
                counts = OpCounts()
                monkeypatch.setattr(plan_build, "locate_ops", count_ops(counts))
                rec = RecordingClient(client)
                llm = AnthropicLlm(
                    variant.catalog(), "", model=variant.model, annotate_effort=variant.effort, client=rec
                )
                scores = [score(doc_name, await plan(doc, store, llm)) for doc_name, doc in docs.items()]
                result = RunResult(name, run, scores, counts, rec.calls)
                results[name].append(result)
                spent += result.cost(pricing, variant.model)
                print(f"run {run + 1} {name}: {len(rec.calls)} calls, keep violations "
                      f"{len(result.keep_violations)}, dropped {result.dropped}/{result.drop_total}, ${spent:.4f}")
                if spent > max_usd:
                    stopped_early = True
                    break
            if stopped_early:
                break
    finally:
        await client.close()

    summaries = [summarize(VARIANTS[n], results[n], pricing) for n in names if results[n]]
    baseline = next((s for s in summaries if s["variant"] == "sonnet-baseline"), None)
    haiku_passing = sorted(
        (s for s in summaries if s["variant"].startswith("haiku") and passes(s, baseline)),
        key=lambda s: Decimal(s["cost_per_run_usd"]),
    )
    report = {
        "date": dt.date.today().isoformat(),
        "prompt_version": PROMPT_VERSION,
        "documents": list(docs),
        "spent_usd": str(spent),
        "stopped_early": stopped_early,
        "summaries": summaries,
        "cheapest_passing_haiku": haiku_passing[0]["variant"] if haiku_passing else None,
        "shipped": shipped_variant().name,
    }
    (out_dir / "annotate_eval.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", "utf-8")
    ops_seen = {  # every op each call returned, for reading the misses (stays in the output directory)
        name: [
            [json.loads(next((b["text"] for b in c.response.get("content", []) if b.get("type") == "text"), "{}"))
             for c in r.calls if c.stop_reason not in ("error",)]
            for r in results[name]
        ]
        for name in names
    }
    (out_dir / "annotate_eval_ops.json").write_text(json.dumps(ops_seen, indent=1, ensure_ascii=False) + "\n")
    md = table(summaries, baseline)
    (out_dir / "annotate_eval.md").write_text(md, "utf-8")
    print("\n" + md)
    for s in summaries:
        if s["keep_violation_list"] or s["missed_drops"] or s["refusal_reasons"]:
            print(f"{s['variant']}: kept-text lost {s['keep_violation_list']}; missed {s['missed_drops']}; "
                  f"guard refusals {s['refusal_reasons'][:5]}")
    print(f"spent ${spent:.4f}; cheapest passing Haiku variant: {report['cheapest_passing_haiku']}")

    record = os.environ.get("OPEN_SPEECH_EVAL_RECORD", "")
    if record == "passing":
        record = report["cheapest_passing_haiku"] or ""
    for name in [n for n in record.split(",") if n]:
        summary = next(s for s in summaries if s["variant"] == name)
        print("recorded", _record(name, results[name][0], baseline, summary))

    assert not stopped_early, f"stopped at ${spent:.4f} (OPEN_SPEECH_EVAL_MAX_USD={max_usd})"
    shipped = next((s for s in summaries if s["variant"] == shipped_variant().name), None)
    if shipped is not None and baseline is not None:
        assert passes(shipped, baseline), f"the shipped annotate variant misses the gate: {shipped}"
