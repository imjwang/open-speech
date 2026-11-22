"""One Claude agent turn (07 § Turn flow, Agent core)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_speech.api import schemas as s
from open_speech.api.ws_schema import AssistantDelta
from open_speech.errors import EngineError, MissingKey

if TYPE_CHECKING:
    from open_speech.service.assistant import AssistantService, Reply, _Session
    from open_speech.service.engine import Engine

HISTORY_TURNS = 12  # user+assistant pairs kept per bar session


@cache
def system_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "system.md").read_text("utf-8")


def _turn_context(e: Engine, req: s.TurnRequest) -> str:
    snap = e.playback.state()
    ctx: dict[str, Any] = {
        "playing": snap.state,
        "document": snap.document_title,
        "document_id": req.context.document_id or snap.document_id,
        "speed": snap.speed,
        "voice": snap.voice_id,
        "has_pending_confirmation": e.assistant.latest_pending() is not None,
        "input": "spoken" if req.source == "spoken" else "typed",
    }
    if req.context.selection:
        ctx["selection"] = f"<document_content>{req.context.selection[:2000]}</document_content>"
    return json.dumps(ctx)


async def run_agent_turn(
    e: Engine, assistant: AssistantService, *, turn_id: str, session: _Session, req: s.TurnRequest
) -> Reply:
    from open_speech.agent.tools import build_tools
    from open_speech.service.assistant import Reply

    runner = e.agent_runner()
    if runner is None:
        raise MissingKey("anthropic", "That request")
    e.budget.check_paid()
    settings = e.settings.get()
    tools = build_tools(e, turn_id=turn_id, context=req.context)
    user = f"<turn_context>{_turn_context(e, req)}</turn_context>\n\n{req.text}"

    def on_delta(text: str) -> None:
        e.events.publish(AssistantDelta(turn_id=turn_id, text=text))

    try:
        result = await runner.run(
            system=system_prompt(),
            history=session.history[-2 * HISTORY_TURNS :],
            user=user,
            tools=tools,
            model=settings.llm.model,
            effort=settings.llm.assistant_effort,
            on_delta=on_delta,
        )
    except EngineError:
        raise
    except Exception as err:  # noqa: BLE001 — SDK/network errors become a spoken error with the next step
        name = type(err).__name__
        if "Authentication" in name or "Permission" in name:
            raise EngineError("missing_key", "Your Anthropic key was rejected. Paste a new one into the bar.") from err
        if "RateLimit" in name:
            raise EngineError("rate_limited", "Claude is busy right now. Try again in a moment.") from err
        if "Connection" in name or "Timeout" in name:
            raise EngineError(
                "offline", "I can't reach Claude right now. Playback and simple commands still work."
            ) from err
        raise EngineError("provider_error", "Claude had a problem with that request. Please try again.") from err

    e.ledger.record(
        provider="anthropic", model=result.model or settings.llm.model, op="chat", units=result.units, turn_id=turn_id
    )
    e.budget.after_record()
    text = result.text
    if result.stop_reason == "refusal":
        text = text or "I can't help with that one."
    if not text:
        text = "Done." if result.tool_calls else "Sorry, I don't have an answer for that."
    session.history.extend([{"role": "user", "content": req.text}, {"role": "assistant", "content": text}])
    return Reply(text=text)
