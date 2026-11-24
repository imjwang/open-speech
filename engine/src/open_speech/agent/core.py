"""One Claude agent turn (07 § Turn flow, Agent core)."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from open_speech.api.ws_schema import AssistantDelta
from open_speech.errors import EngineError, Unavailable
from open_speech.i18n import current_language, t
from open_speech.service.commands import Reply, voice_display

if TYPE_CHECKING:
    from open_speech.service.assistant import Conversation
    from open_speech.service.commands import Turn
    from open_speech.service.engine import Engine

INSTRUCTIONS_CHARS = 2000


@cache
def system_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "system.md").read_text("utf-8")


def _voice_name(e: Engine, voice_id: str) -> str:
    """How a voice is called aloud ("Heart"), not its id."""
    voice = next((v for v in e.registry.voices() if v.id == voice_id), None)
    return voice_display(voice)[0] if voice is not None else voice_id


def turn_context(e: Engine, turn: Turn, failures: list[str]) -> str:
    """What changes every turn. It goes after the cached prefix (system prompt + tools), in the user message."""
    req = turn.request
    snap = e.playback.state()
    ctx: dict[str, Any] = {
        "language": current_language(),  # reply in this language
        # How the user asked (spoken, typed or a button). The reply is heard either way.
        "input": req.source,
        "playing": snap.state,
        "document": snap.document_title,
        "document_id": req.context.document_id or snap.document_id,
        "queue": [q.title for q in snap.queue],
        "speed": snap.speed,
        "voice": snap.voice_id,
        "voice_name": _voice_name(e, snap.voice_id or e.documents.voice_for(snap.document_id)),  # say this one
        "theme": e.effective_theme(),
        "has_pending_confirmation": turn.pending is not None,
    }
    if turn.documents:
        ctx["attachments"] = [{"document_id": d, "title": e.documents.title(d)} for d in turn.documents]
    if failures:
        ctx["attachments_that_failed"] = failures
    if req.context.selection:
        ctx["selection"] = f"<document_content>{req.context.selection[:2000]}</document_content>"
    out = f"<turn_context>{json.dumps(ctx, ensure_ascii=False)}</turn_context>"
    instructions = e.rule_store.speech_md().strip()  # SPEECH.md, the prompt that drives cleanup
    if instructions:
        out += f"\n<reading_instructions>{instructions[:INSTRUCTIONS_CHARS]}</reading_instructions>"
    return out


async def run_agent_turn(
    e: Engine,
    *,
    turn: Turn,
    conversation: Conversation,
    failures: list[str],
    on_text: Callable[[str], None] | None = None,
) -> Reply:
    """One turn with Claude. The reply streams to clients (`assistant.delta`) and, through `on_text`, into the
    reply's voice as it is written, so the first sentence is heard while the rest is still coming.

    Each turn stands alone: Claude gets this turn's context and words, never earlier turns. `conversation` only
    carries what the app itself remembers while the bar is open (the hosts the user named)."""
    from open_speech.agent.tools import TurnGuard, build_tools

    runner = e.agent_runner()
    if runner is None:
        raise Unavailable("assistant")
    e.budget.check_paid()
    settings = e.settings.get()
    req = turn.request
    guard = TurnGuard(hosts=set(conversation.hosts))
    if req.context.document_id or e.playback.current_document_id():
        try:
            site = e.documents.load(req.context.document_id or e.playback.current_document_id() or "").site
            if site:
                guard.hosts.add(site.lower())
        except EngineError:
            pass
    tools = build_tools(e, turn=turn, guard=guard)
    user = f"{turn_context(e, turn, failures)}\n\n{req.text}"

    def on_delta(text: str) -> None:
        e.events.publish(AssistantDelta(turn_id=turn.id, text=text))
        if on_text is not None:
            on_text(text)

    try:
        result = await runner.run(
            system=system_prompt(),
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
            e.keys.mark_invalid("anthropic")
            raise Unavailable("assistant") from err
        if "RateLimit" in name:
            raise EngineError("rate_limited", t("agent.busy")) from err
        if "Connection" in name or "Timeout" in name:
            raise EngineError("offline", t("agent.offline")) from err
        raise EngineError("provider_error", t("agent.problem")) from err

    e.ledger.record(  # the budget guard and the usage watcher hear about it from the ledger
        provider="anthropic", model=result.model or settings.llm.model, op="chat", units=result.units, turn_id=turn.id
    )
    text = result.text
    if result.stop_reason == "refusal":
        text = text or t("agent.refused")
    if not text:
        text = t("done.generic") if result.tool_calls else t("agent.no_answer")
    return Reply(text=text)
