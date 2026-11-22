"""Provider protocols (08 § Provider protocols). Nothing outside `providers/` imports a vendor SDK.

Shared contract between the runtime workstream (which implements these) and the pipeline / service
workstreams (which call them).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from open_speech.document.model import Block
from open_speech.plan.script import SpanOp


class Voice(BaseModel):
    id: str  # "provider:model:voice", e.g. "openai:gpt-4o-mini-tts:coral"
    provider: str
    model: str
    name: str
    languages: list[str] = Field(default_factory=list)
    styles: bool = False  # supports free-text style instructions
    local: bool = False
    available: bool = True
    unavailable_reason: str | None = None
    timestamps: bool = False  # exact word timing


class TtsLimits(BaseModel):
    max_input_chars: int = 4000
    formats: list[str] = Field(default_factory=lambda: ["opus"])
    style: bool = False
    languages: list[str] = Field(default_factory=list)


class TtsRequest(BaseModel):
    text: str
    model: str
    voice: str
    style: str | None = None
    language: str | None = None


@dataclass
class WordTiming:
    """Word boundary in *spoken text* offsets, seconds from utterance start (at 1x)."""

    start: int
    end: int
    t0: float
    t1: float


@dataclass
class AudioChunk:
    """Streamed synthesis output. Providers yield PCM; the synthesizer encodes to Ogg/Opus for the cache.

    `pcm` is mono float32 little-endian samples at `sample_rate`. A provider that receives Opus from the
    vendor decodes it before yielding (keeps one internal format), or sets `encoded`.
    """

    pcm: bytes = b""
    sample_rate: int = 24000
    encoded: bytes | None = None  # vendor-encoded container bytes (e.g. Ogg/Opus) when not decoded
    words: list[WordTiming] = field(default_factory=list)
    final: bool = False


@dataclass
class Units:
    """Billable units for the ledger (08 § Usage ledger)."""

    chars: int = 0
    in_tok: int = 0
    out_tok: int = 0
    cache_read_tok: int = 0
    cache_write_tok: int = 0
    audio_sec: float = 0.0
    images: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v}


@runtime_checkable
class TtsProvider(Protocol):
    id: str
    timestamps: bool

    def voices(self) -> list[Voice]: ...
    def limits(self, model: str) -> TtsLimits: ...
    def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]: ...
    def price_units(self, req: TtsRequest, audio_sec: float) -> Units: ...


class SttEvent(BaseModel):
    kind: Literal["partial", "final"]
    text: str


class Transcript(BaseModel):
    text: str
    words: list[dict[str, Any]] = Field(default_factory=list)
    audio_sec: float = 0.0


@runtime_checkable
class SttProvider(Protocol):
    id: str

    def stream(self, pcm: AsyncIterator[bytes], lang: str | None = None) -> AsyncIterator[SttEvent]: ...
    async def transcribe(self, audio: Path, words: bool = False) -> Transcript: ...


@dataclass
class LlmCall:
    """Result envelope for any Claude call: parsed output + exact usage for the ledger."""

    output: Any
    units: Units
    model: str
    stop_reason: str | None = None


class RuleBrief(BaseModel):
    """What the planner tells Claude about one active rule."""

    id: str
    name: str
    instruction: str


class CompiledRuleDraft(BaseModel):
    """Compiler output (06 § The compiler)."""

    name: str
    description: str
    tier: Literal["filter", "annotate", "transform"]
    compiled: dict[str, Any] | None = None
    scope_suggestion: dict[str, list[str]] = Field(default_factory=dict)
    instruction: str


class TransformOutput(BaseModel):
    utterances: list[str]
    anchor_block_ids: list[str] = Field(default_factory=list)


@runtime_checkable
class LlmProvider(Protocol):
    """Anthropic only, for now. All plan-side calls are tool-less (07 § Injection boundary)."""

    id: str

    async def annotate(
        self, blocks: Sequence[Block], rules: Sequence[RuleBrief], speech_md: str
    ) -> LlmCall: ...  # output: list[SpanOp]

    async def transform(
        self, blocks: Sequence[Block], instruction: str, speech_md: str
    ) -> LlmCall: ...  # output: TransformOutput

    async def compile_rule(self, instruction: str, context: dict[str, Any]) -> LlmCall: ...
    # output: CompiledRuleDraft


@runtime_checkable
class VisionProvider(Protocol):
    id: str

    async def describe_or_transcribe(
        self,
        image: bytes,
        media_type: str,
        rules: Sequence[RuleBrief],
        mode: Literal["auto", "transcribe", "describe"] = "auto",
    ) -> LlmCall: ...  # output: list[tuple[block_type, text]] as VisionBlocks


class VisionBlock(BaseModel):
    type: Literal["paragraph", "heading", "list_item", "table", "caption", "figure"]
    text: str


__all__ = [
    "AudioChunk",
    "CompiledRuleDraft",
    "LlmCall",
    "LlmProvider",
    "RuleBrief",
    "SpanOp",
    "SttEvent",
    "SttProvider",
    "Transcript",
    "TransformOutput",
    "TtsLimits",
    "TtsProvider",
    "TtsRequest",
    "Units",
    "VisionBlock",
    "VisionProvider",
    "Voice",
    "WordTiming",
]
