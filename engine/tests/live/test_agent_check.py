"""Live agent check (07 § Agent core): twenty commands, each straight through `run_agent_turn` with the real tools,
on the model the settings name (Haiku 5.5 by default). Checks the right tool or card, reply length (replies are
spoken: one or two short sentences), refusals and latency, and reports the ledger cost.

Runs only with OPEN_SPEECH_LIVE=1 and an Anthropic key in the environment or the repo `.env`. The engine has no
fixture providers, a temporary home and no audio output (OPEN_SPEECH_AUDIO_OUTPUT=null): nothing is played and no
paid voice is used (no command previews a voice or starts reading).

    OPEN_SPEECH_LIVE=1 uv run pytest tests/live/test_agent_check.py -s
"""

from __future__ import annotations

import json
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from open_speech.api import schemas as s
from open_speech.config import EngineEnv
from open_speech.ids import new_id
from open_speech.providers.keys import ProviderKeys

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("OPEN_SPEECH_LIVE") != "1", reason="live provider test: set OPEN_SPEECH_LIVE=1"),
]

DOCUMENT = (
    "Quarterly Operations Report. Output grew steadily while costs stayed flat, and the new roasting line reached "
    "full capacity in August. Production rose to 41 tonnes, up 12 percent from the second quarter. Energy use per "
    "kilogram fell for the third quarter in a row. Two new wholesale partners signed annual contracts. The main "
    "risk is the price of green coffee, which rose sharply in September. We will review contracts in January."
)


@dataclass
class Case:
    say: str
    tools: set[str]  # any of these counts as right; "" = answering without a tool is right
    card: str | None = None  # the confirmation card that must be created (action kind)
    max_sentences: int = 2
    new_conversation: bool = True


CASES = [
    Case("read a bit faster", {"set_speed", "playback"}),
    Case("which voices do you have that sound British?", {"list_voices"}, max_sentences=3),
    Case("use a calmer voice for everything", {"set_voice"}, card="set_voice", max_sentences=3),
    Case("stop reading footnotes on arxiv.org", {"propose_rule", "update_rule"}, card="save_rule",
         max_sentences=3),
    Case("always say SQL as sequel", {"add_pronunciation"}, card="add_pronunciation"),
    Case("how much have I spent this month?", {"get_usage"}),
    Case("set my monthly budget to five dollars", {"set_budget"}, card="set_budget"),
    Case("switch to the dark theme", {"update_setting", ""}),  # "" when the Mac is in dark mode already
    Case("make the text bigger", {"update_setting"}),
    Case("stop reading the figure captions", {"edit_prompt"}, card="write_rule"),
    Case("summarize this document in two sentences", {"read_document", "document_text", "get_context"},
         max_sentences=3),
    Case("where am I in the document?", {"where_am_i", "get_context", ""}),
    Case("what's the main risk the report mentions?", {"read_document", "document_text", "get_context"}),
    Case("what are my reading rules?", {"list_rules"}, max_sentences=3),
    Case("open the rules editor", {"open_rules_editor"}),
    Case("what can you do?", {""}, max_sentences=3),
    Case("uh", {""}),
    Case("what's the capital of Portugal?", {""}),
    Case("is reading with the local voice free?", {"", "get_usage", "list_voices", "estimate_cost"}),
    Case("lee un poco más despacio", {"set_speed", "playback"}),
]


@dataclass
class Outcome:
    case: Case
    text: str
    tools: list[str]
    cards: list[str]
    stop_reason: str | None
    seconds: float
    first_text_s: float | None
    sentences: int
    errors: list[str] = field(default_factory=list)

    @property
    def right_tool(self) -> bool:
        used = set(self.tools)
        if "" in self.case.tools and not used:
            return True
        return bool(used & (self.case.tools - {""}))

    @property
    def right_card(self) -> bool:
        if self.case.card is None:
            return True
        return bool(self.cards) if self.case.card == "any" else self.case.card in self.cards


def _sentences(text: str) -> int:
    return len([p for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p])


async def test_agent_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from open_speech.agent.core import run_agent_turn
    from open_speech.service.assistant import Conversation
    from open_speech.service.commands import Turn
    from open_speech.service.engine import Engine

    monkeypatch.setenv("OPEN_SPEECH_AUDIO_OUTPUT", "null")
    monkeypatch.setenv("OPEN_SPEECH_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPEN_SPEECH_FIXTURE_PROVIDERS", raising=False)
    env = EngineEnv()
    if not ProviderKeys(env).configured("anthropic"):
        pytest.skip("no Anthropic key in the environment or the repo .env")
    assert env.open_speech_audio_output == "null" and not env.open_speech_fixture_providers
    engine = Engine(env)
    await engine.start()
    outcomes: list[Outcome] = []
    try:
        created = await engine.documents.create(s.CreateDocumentRequest(kind="text", text=DOCUMENT, play=False))
        doc_id = created.document.id
        actions: list[dict[str, Any]] = []
        engine.events.add_listener(lambda m: actions.append(m) if m.get("type") == "assistant.action" else None)

        real = engine.agent_runner
        timing: dict[str, Any] = {}

        class Timed:
            def __init__(self, inner: Any) -> None:
                self.inner = inner

            async def run(self, **kw: Any) -> Any:
                on_delta = kw["on_delta"]
                t0 = time.perf_counter()
                timing["first"] = None

                def delta(text: str) -> None:
                    if timing["first"] is None and text.strip():
                        timing["first"] = time.perf_counter() - t0
                    on_delta(text)

                result = await self.inner.run(**{**kw, "on_delta": delta})
                timing["result"] = result
                return result

        monkeypatch.setattr(engine, "agent_runner", lambda: Timed(real()))
        conversation = Conversation()
        for case in CASES:
            if case.new_conversation:
                conversation = Conversation()
            turn = Turn(id=new_id("turn"), request=s.TurnRequest(text=case.say, source="spoken",
                                                                  context=s.TurnContext(document_id=doc_id)))
            before = len(actions)
            t0 = time.perf_counter()
            errors: list[str] = []
            try:
                reply = await run_agent_turn(engine, turn=turn, conversation=conversation, failures=[])
                text = reply.text
            except Exception as exc:  # noqa: BLE001 — reported, not raised: one bad turn shouldn't hide the rest
                text, errors = "", [f"{type(exc).__name__}: {exc}"]
            seconds = time.perf_counter() - t0
            result = timing.get("result")
            outcomes.append(
                Outcome(
                    case=case,
                    text=text,
                    tools=list(result.tool_calls) if result else [],
                    cards=[a["kind"] for a in actions[before:]],
                    stop_reason=result.stop_reason if result else None,
                    seconds=seconds,
                    first_text_s=timing.get("first"),
                    sentences=_sentences(text),
                    errors=errors,
                )
            )
            timing.clear()
            for pending in engine.assistant.pending_actions():  # never confirm anything on the user's behalf
                await engine.assistant.cancel(pending.action_id)
        cost = engine.ledger.month_spent()
        model = engine.settings.get().llm.model
    finally:
        await engine.shutdown()

    rows = []
    for o in outcomes:
        ok = o.right_tool and o.right_card and not o.errors
        rows.append(
            f"| {o.case.say} | {', '.join(o.tools) or '-'} | {', '.join(o.cards) or '-'} | {o.sentences} "
            f"| {o.seconds:.1f} s | {'' if o.first_text_s is None else f'{o.first_text_s:.1f} s'} "
            f"| {'ok' if ok else 'MISS'} | {o.text[:90]!r} |"
        )
    right = sum(1 for o in outcomes if o.right_tool and o.right_card and not o.errors)
    long = [o.case.say for o in outcomes if o.sentences > o.case.max_sentences]
    refusals = [o.case.say for o in outcomes if o.stop_reason == "refusal"]
    report = {
        "model": model,
        "turns": len(outcomes),
        "right_tool_or_card": right,
        "too_long": long,
        "refusals": refusals,
        "errors": [e for o in outcomes for e in o.errors],
        "turn_p50_s": round(statistics.median(o.seconds for o in outcomes), 2),
        "turn_p90_s": round(statistics.quantiles([o.seconds for o in outcomes], n=10)[-1], 2),
        "first_text_p50_s": round(statistics.median(o.first_text_s for o in outcomes if o.first_text_s), 2),
        "ledger_cost_usd": str(cost),
    }
    table = (
        "| Said | Tools | Card | Sentences | Turn | First words | Result | Reply |\n|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
    )
    out = Path(os.environ.get("OPEN_SPEECH_EVAL_OUT") or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    (out / "agent_check.md").write_text(table + "\n\n" + json.dumps(report, indent=1) + "\n", "utf-8")
    print("\n" + table + "\n" + json.dumps(report, indent=1))

    assert not report["errors"], report["errors"]
    assert not refusals, refusals
    assert right >= len(outcomes) - 3, f"{right}/{len(outcomes)} right"
