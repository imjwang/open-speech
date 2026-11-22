"""Claude agent harness (07 § Agent core): Anthropic Python SDK Tool Runner with async function tools.

Lives in `providers/` because it imports the vendor SDK. The tools themselves are vendor-neutral functions from
`agent/tools.py`; this adapter wraps them with `beta_async_tool` and runs the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from open_speech.providers.base import Units

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class AgentResult:
    text: str
    units: Units = field(default_factory=Units)
    model: str = ""
    stop_reason: str | None = None
    tool_calls: list[str] = field(default_factory=list)


class AnthropicAgentRunner:
    def __init__(
        self, api_key: str | None = None, *, client: Any | None = None, stream: bool = True, max_iterations: int = 8
    ) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(api_key=api_key, max_retries=2)
        self.client = client
        self.stream = stream
        self.max_iterations = max_iterations

    async def run(
        self,
        *,
        system: str,
        history: Sequence[dict[str, Any]],
        user: str,
        tools: Sequence[Callable[..., Awaitable[str]]],
        model: str,
        effort: str,
        on_delta: Callable[[str], None],
    ) -> AgentResult:
        from anthropic import beta_async_tool

        runnable = [beta_async_tool(fn) for fn in tools]
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": 1024,
            # System prompt + tool definitions form the stable cached prefix; per-turn context lives in the user
            # message after the breakpoint (07 § Agent core).
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": runnable,
            "messages": [*history, {"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
            "max_iterations": self.max_iterations,
        }
        if effort != "auto":
            params["output_config"] = {"effort": effort}

        result = AgentResult(text="", model=model)
        last_text = ""
        runner = self.client.beta.messages.tool_runner(**params, stream=self.stream)
        if self.stream:
            async for message_stream in runner:
                async for event in message_stream:
                    if (
                        getattr(event, "type", "") == "content_block_delta"
                        and getattr(event.delta, "type", "") == "text_delta"
                    ):
                        on_delta(event.delta.text)
                message = await message_stream.get_final_message()
                last_text = self._absorb(message, result) or last_text
        else:
            async for message in runner:
                text = self._absorb(message, result)
                if text:
                    on_delta(text)
                    last_text = text
        result.text = last_text.strip()
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
