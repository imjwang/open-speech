"""Test doubles for the provider protocols the pipeline calls (no network, deterministic)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from open_speech.document.model import Block
from open_speech.plan.script import SpanOp
from open_speech.providers.base import (
    CompiledRuleDraft,
    LlmCall,
    RuleBrief,
    TransformOutput,
    Units,
    VisionBlock,
)

AnnotateFn = Callable[[Sequence[Block], Sequence[RuleBrief], str], list[SpanOp] | list[dict[str, Any]]]
TransformFn = Callable[[Sequence[Block], str, str], TransformOutput]


def skip_quotes(*quotes: str) -> AnnotateFn:
    """An annotate function that skips every occurrence of each quote in every block it sees."""

    def fn(blocks: Sequence[Block], _rules: Sequence[RuleBrief], _speech_md: str) -> list[SpanOp]:
        ops: list[SpanOp] = []
        for b in blocks:
            for q in quotes:
                n = b.text.count(q)
                ops.extend(SpanOp(block_id=b.id, op="skip", quote=q, occurrence=i + 1) for i in range(n))
        return ops

    return fn


class FakeLlm:
    id = "fake"

    def __init__(
        self,
        *,
        annotate: AnnotateFn | None = None,
        transform: TransformFn | None = None,
        compile_draft: CompiledRuleDraft | dict[str, Any] | None = None,
        model: str = "claude-sonnet-5-5",
    ) -> None:
        self._annotate = annotate or (lambda _b, _r, _s: [])
        self._transform = transform or (lambda blocks, _i, _s: TransformOutput(utterances=[], anchor_block_ids=[]))
        self._draft = compile_draft
        self.model = model
        self.calls: list[tuple[str, Any]] = []

    async def annotate(self, blocks: Sequence[Block], rules: Sequence[RuleBrief], speech_md: str) -> LlmCall:
        self.calls.append(("annotate", [b.id for b in blocks]))
        out = self._annotate(blocks, rules, speech_md)
        return LlmCall(output=out, units=Units(in_tok=100 * len(blocks), out_tok=20), model=self.model)

    async def transform(self, blocks: Sequence[Block], instruction: str, speech_md: str) -> LlmCall:
        self.calls.append(("transform", [b.id for b in blocks]))
        return LlmCall(
            output=self._transform(blocks, instruction, speech_md), units=Units(in_tok=50, out_tok=30), model=self.model
        )

    async def compile_rule(self, instruction: str, context: dict[str, Any]) -> LlmCall:
        self.calls.append(("compile_rule", (instruction, context)))
        if self._draft is None:
            raise AssertionError("compile_rule was not expected")
        return LlmCall(output=self._draft, units=Units(in_tok=300, out_tok=80), model=self.model)

    def count(self, op: str) -> int:
        return sum(1 for o, _ in self.calls if o == op)


class FakeVision:
    id = "fake-vision"

    def __init__(self, blocks: list[VisionBlock] | Any) -> None:
        self.blocks = blocks
        self.seen: list[tuple[bytes, str, str]] = []

    async def describe_or_transcribe(
        self,
        image: bytes,
        media_type: str,
        rules: Sequence[RuleBrief],
        mode: Literal["auto", "transcribe", "describe"] = "auto",
    ) -> LlmCall:
        self.seen.append((image, media_type, mode))
        return LlmCall(output=self.blocks, units=Units(in_tok=1200, out_tok=60, images=1), model="claude-sonnet-5-5")
