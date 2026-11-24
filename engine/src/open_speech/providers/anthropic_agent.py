"""Claude agent harness (07 § Agent core): Anthropic Python SDK Tool Runner with async function tools.

Lives in `providers/` because it imports the vendor SDK. The tools themselves are vendor-neutral functions from
`agent/tools.py`; this adapter wraps them with `beta_async_tool` and runs the loop.

Request shape per model comes from the catalog (`LlmTraits`): adaptive thinking on every model, `max_tokens`
with room for that thinking, and the server-side refusal fallback only on models that have one (Haiku 5.5
doesn't: there a declined turn ends with `stop_reason == "refusal"` and the caller says so). Text from every
tool-loop iteration is part of the reply, so what streams, what is shown and what is spoken are the same.

Streamed turns set `eager_input_streaming` on every tool (the API then streams tool input without buffering
and validating it first). `beta_async_tool` validates the parsed input against the function's typed
parameters before calling it, so a malformed input is never acted on; input the SDK can't parse at all raises
`ValueError` from the runner's iteration, and the turn is asked again once, only when nothing has been said
or done yet (otherwise it would repeat words already spoken or a tool that already ran).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from open_speech.local.catalog import LlmTraits, ModelCatalog
from open_speech.providers.base import Units

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MALFORMED_RETRIES = 1  # re-asks after tool input the SDK couldn't parse (eager streaming), when nothing ran yet
AGENT_MAX_TOKENS = 4096  # adaptive thinking counts toward it; replies themselves are a few sentences
ITERATION_GAP = " "  # between the texts of two tool-loop iterations ("Let me check." + " " + "It's 3 pm.")


@dataclass
class AgentResult:
    text: str
    units: Units = field(default_factory=Units)
    model: str = ""
    stop_reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)


class AnthropicAgentRunner:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        catalog: ModelCatalog | None = None,
        stream: bool = True,
        max_iterations: int = 8,
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, max_retries=2)
        self.client = client
        self.catalog = catalog if catalog is not None else ModelCatalog.load()
        self.stream = stream
        self.max_iterations = max_iterations

    def traits(self, model: str) -> LlmTraits:
        return self.catalog.llm_traits(model)

    def params(
        self,
        *,
        system: str,
        user: str,
        tools: Sequence[Any],
        model: str,
        effort: str,
    ) -> dict[str, Any]:
        traits = self.traits(model)
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": AGENT_MAX_TOKENS,
            # System prompt + tool definitions form the stable cached prefix; per-turn context lives in the user
            # message after the breakpoint (07 § Agent core).
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": list(tools),
            "messages": [{"role": "user", "content": user}],  # each turn stands alone: no history
            "thinking": {"type": "adaptive"},
            "max_iterations": self.max_iterations,
        }
        if traits.server_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        if effort != "auto":
            params["output_config"] = {"effort": effort}
        return params

    async def run(
        self,
        *,
        system: str,
        user: str,
        tools: Sequence[Callable[..., Awaitable[str]]],
        model: str,
        effort: str,
        on_delta: Callable[[str], None],
    ) -> AgentResult:
        from anthropic import beta_async_tool

        eager = True if self.stream else None  # ignored without streaming: leave it off there
        params = self.params(
            system=system,
            user=user,
            tools=[beta_async_tool(fn, eager_input_streaming=eager) for fn in tools],
            model=model,
            effort=effort,
        )
        attempts = 0
        while True:
            result = AgentResult(text="", model=model)
            said: list[bool] = [False]
            try:
                return await self._loop(params, result, on_delta, said)
            except ValueError:
                # Tool input that couldn't be parsed (eager input streaming). Ask again only if nothing has been
                # said or run yet; the SDK's API errors are not ValueErrors and pass straight through.
                if attempts >= MALFORMED_RETRIES or said[0] or result.tool_calls:
                    raise
                attempts += 1

    async def _loop(
        self, params: dict[str, Any], result: AgentResult, on_delta: Callable[[str], None], said: list[bool]
    ) -> AgentResult:
        def emit(text: str) -> None:
            said[0] = True
            on_delta(text)

        parts: list[str] = []  # the text of each iteration that said something
        # Read by duck typing (event.type / block.type), like the rest of this module: the SDK's event unions are
        # wider than what a tool-runner stream yields.
        runner: Any = self.client.beta.messages.tool_runner(**params, stream=self.stream)
        if self.stream:
            async for message_stream in runner:
                started = False
                async for event in message_stream:
                    if (
                        getattr(event, "type", "") == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"
                        and event.delta.text
                    ):
                        if not started and parts:
                            emit(ITERATION_GAP)  # keep the streamed text identical to the final one
                        started = True
                        emit(event.delta.text)
                message = await message_stream.get_final_message()
                text = self._absorb(message, result)
                if text:
                    parts.append(text)
        else:
            async for message in runner:
                text = self._absorb(message, result)
                if text:
                    emit((ITERATION_GAP if parts else "") + text)
                    parts.append(text)
        result.text = ITERATION_GAP.join(parts).strip()
        return result

    @staticmethod
    def _absorb(message: Any, result: AgentResult) -> str:
        usage = getattr(message, "usage", None)
        if usage is not None:
            result.units.in_tok += int(getattr(usage, "input_tokens", 0) or 0)
            result.units.out_tok += int(getattr(usage, "output_tokens", 0) or 0)
            result.units.cache_read_tok += int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            result.units.cache_write_tok += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        result.stop_reason = getattr(message, "stop_reason", None)
        result.model = getattr(message, "model", result.model) or result.model
        texts: list[str] = []
        for block in getattr(message, "content", []) or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                texts.append(block.text)
            elif btype == "tool_use":
                result.tool_calls.append(block.name)
        return "".join(texts)
