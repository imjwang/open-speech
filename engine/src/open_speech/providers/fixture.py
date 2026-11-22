"""Deterministic, offline providers for tests and the e2e suite (implementation-plan § B).

Registered only when `EngineEnv.open_speech_fixture_providers` is true. They report provider id
"fixture" and label their voices "Test tone voice" so they can't be mistaken for real voices.

- `FixtureTts`: a soft tone burst per word (≈0.18 s per syllable at 1x, short gaps, longer pauses at
  punctuation) with EXACT word timings. Model "tone-v1" is free/local (stands in for Kokoro);
  "tone-paid-v1" bills like OpenAI TTS so budget and estimate flows work end to end.
- `FixtureStt`: scripted transcripts. `FixtureStt.enqueue("pause")`, then a mic session streams PCM and
  gets word-by-word partials followed by the final text (an empty final when nothing is queued).
- `FixtureLlm` / `FixtureVision`: deterministic annotate / compile / transform / vision.

Test hooks (class-level, reset with `reset()`): `FixtureTts.latency_s`, `FixtureTts.fail_next(*statuses)`
(e.g. 503, 429, 401; 0 simulates being offline), `FixtureTts.calls`.
"""

from __future__ import annotations

import asyncio
import os
import hashlib
import math
import re
from collections import deque
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np

from open_speech.document.model import Block, BlockType, MarkKind
from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.plan.script import SpanOp
from open_speech.playback.timing import count_syllables, pause_after, word_spans
from open_speech.providers.base import (
    AudioChunk,
    CompiledRuleDraft,
    LlmCall,
    RuleBrief,
    SttEvent,
    Transcript,
    TransformOutput,
    TtsLimits,
    TtsRequest,
    Units,
    VisionBlock,
    Voice,
    WordTiming,
)
from open_speech.providers.pcm import f32_to_bytes, read_audio_file, tone

FIXTURE_LLM_MODEL = "fixture-llm"
FIXTURE_STT_MODEL = "fixture-stt"


class FixtureHTTPError(Exception):
    """Mimics an SDK HTTP error (has `status_code`) so the normal error mapping applies."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"fixture provider returned HTTP {status_code}")
        self.status_code = status_code


def _stable_int(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "big")


# --------------------------------------------------------------------------------------------- TTS


class FixtureTts:
    id = "fixture"
    timestamps = True

    SAMPLE_RATE = 24000
    SYLLABLE_S = 0.18
    GAP_S = 0.06
    PAUSE_S = 0.2  # per pause-weight unit after punctuation
    EDGE_S = 0.05  # leading/trailing silence
    VOICE_HZ: ClassVar[dict[str, float]] = {"alto": 330.0, "bass": 165.0, "soprano": 523.0}

    latency_s: ClassVar[float] = 0.0
    calls: ClassVar[int] = 0
    _failures: ClassVar[deque[int]] = deque()

    def __init__(self, catalog: ModelCatalog) -> None:
        self._models: dict[str, TtsModel] = {
            m.model: m for m in catalog.tts_models("fixture", include_test=True)
        }

    # ------------------------------------------------------------------ test hooks

    @classmethod
    def fail_next(cls, *statuses: int) -> None:
        cls._failures.extend(statuses)

    @classmethod
    def reset(cls) -> None:
        cls._failures.clear()
        cls.latency_s = 0.0
        cls.calls = 0

    # ------------------------------------------------------------------ protocol

    def voices(self) -> list[Voice]:
        out: list[Voice] = []
        for m in self._models.values():
            for v in m.voices:
                out.append(
                    Voice(
                        id=m.voice_id(v),
                        provider=self.id,
                        model=m.model,
                        name=f"{m.name or 'Test tone voice'}: {v}",
                        languages=list(m.languages),
                        styles=m.style is not None,
                        local=m.local,
                        timestamps=True,
                    )
                )
        return out

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else 4000,
            formats=["pcm"],
            style=bool(m and m.style),
            languages=list(m.languages) if m else ["en"],
        )

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        type(self).calls += 1
        if self._failures:
            status = self._failures.popleft()
            if status == 0:
                raise ConnectionError("fixture provider is offline")
            raise FixtureHTTPError(status)
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        pcm, words = self.render(req.text, req.voice, req.style)
        half = (pcm.size // 2) if pcm.size > 4800 else pcm.size
        if half < pcm.size:
            yield AudioChunk(pcm=f32_to_bytes(pcm[:half]), sample_rate=self.SAMPLE_RATE)
        yield AudioChunk(pcm=f32_to_bytes(pcm[half:]), sample_rate=self.SAMPLE_RATE, words=words, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), audio_sec=round(audio_sec, 3))

    # ------------------------------------------------------------------ rendering

    @classmethod
    def render(cls, text: str, voice: str, style: str | None = None) -> tuple[np.ndarray, list[WordTiming]]:
        sr = cls.SAMPLE_RATE
        freq = cls.VOICE_HZ.get(voice, 262.0)
        if style:
            freq *= 1.0 + (_stable_int(style) % 7) / 100.0
        parts: list[np.ndarray] = [np.zeros(int(cls.EDGE_S * sr), dtype=np.float32)]
        n = parts[0].size
        words: list[WordTiming] = []
        spans = word_spans(text)
        for i, (s, e) in enumerate(spans):
            dur = cls.SYLLABLE_S * count_syllables(text[s:e])
            burst = tone(freq, dur, sr)
            t0 = n / sr
            parts.append(burst)
            n += burst.size
            words.append(WordTiming(start=s, end=e, t0=round(t0, 6), t1=round(n / sr, 6)))
            if i < len(spans) - 1:
                gap = cls.GAP_S + cls.PAUSE_S * pause_after(text, e)
                g = np.zeros(int(round(gap * sr)), dtype=np.float32)
                parts.append(g)
                n += g.size
        parts.append(np.zeros(int(cls.EDGE_S * sr), dtype=np.float32))
        return np.concatenate(parts).astype(np.float32), words


# --------------------------------------------------------------------------------------------- STT


class FixtureStt:
    id = "fixture"
    model = FIXTURE_STT_MODEL
    BYTES_PER_WORD = 8000  # one partial word per 0.25 s of 16 kHz PCM16

    _queue: ClassVar[deque[str]] = deque()

    def __init__(self) -> None:
        self.usage_sink: Any | None = None  # (provider, model, op, Units) — same contract as cloud STT

    def _bill(self, audio_sec: float) -> None:
        if self.usage_sink is not None and audio_sec > 0:
            self.usage_sink(self.id, self.model, "stt", self.price_units(audio_sec))

    @classmethod
    def enqueue(cls, text: str) -> None:
        cls._queue.append(text)

    @classmethod
    def clear(cls) -> None:
        cls._queue.clear()

    @classmethod
    def pending(cls) -> int:
        return len(cls._queue)

    def _next(self) -> str:
        if self._queue:
            return self._queue.popleft()
        # UI automation drives the bar from outside the process: one utterance per line, consumed in order.
        script = os.environ.get("OPEN_SPEECH_FIXTURE_STT_FILE")
        if script and os.path.exists(script):
            with open(script, encoding="utf-8") as f:
                lines = f.read().splitlines()
            if lines:
                with open(script, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines[1:]))
                return lines[0]
        return ""

    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None = None) -> AsyncIterator[SttEvent]:
        text = self._next()
        words = text.split()
        received = 0
        shown = 0
        async for frame in pcm:
            received += len(frame)
            target = min(len(words), received // self.BYTES_PER_WORD)
            while shown < target:
                shown += 1
                yield SttEvent(kind="partial", text=" ".join(words[:shown]))
        while shown < len(words):
            shown += 1
            yield SttEvent(kind="partial", text=" ".join(words[:shown]))
        self._bill(received / 32000)
        yield SttEvent(kind="final", text=text)

    async def transcribe(self, audio: Path, words: bool = False) -> Transcript:
        text = self._next()
        try:
            x, sr = await asyncio.to_thread(read_audio_file, audio)
            secs = x.size / sr
        except Exception:
            secs = 0.0
        self._bill(secs)
        out_words: list[dict[str, Any]] = []
        if words and text:
            toks = text.split()
            step = secs / max(1, len(toks))
            out_words = [
                {"word": w, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
                for i, w in enumerate(toks)
            ]
        return Transcript(text=text, words=out_words, audio_sec=round(secs, 3))

    def price_units(self, audio_sec: float) -> Units:
        return Units(audio_sec=round(audio_sec, 3))


# --------------------------------------------------------------------------------------------- LLM

_AUTHOR = r"[A-Z][A-Za-z'’\-]+"
_ONE_CITE = rf"{_AUTHOR}(?:\s+(?:et\s+al\.?|and\s+{_AUTHOR}|&\s+{_AUTHOR}))?,?\s+\d{{4}}[a-z]?"
AUTHOR_YEAR_RE = re.compile(rf"\s?\((?:{_ONE_CITE})(?:;\s*{_ONE_CITE})*\)")
# "Smith (2020) showed" — the author is the subject: keep the name, drop the year.
NARRATIVE_YEAR_RE = re.compile(r"(?<=[A-Za-z.])\s\(\d{4}[a-z]?\)")
_FIRST_SENTENCE = re.compile(r"^\s*(.+?[.!?])(?=\s|$)", re.DOTALL)

_KEYWORDS: list[tuple[tuple[str, ...], str, list[BlockType], list[MarkKind]]] = [
    (("footnote",), "Skip footnotes", [BlockType.footnote], [MarkKind.footnote_ref]),
    (("page number",), "Skip page numbers", [BlockType.page_number], []),
    (("header",), "Skip headers", [BlockType.header], []),
    (("footer",), "Skip footers", [BlockType.footer], []),
    (("table",), "Skip tables", [BlockType.table], []),
    (("caption",), "Skip captions", [BlockType.caption], []),
    (("code",), "Skip code", [BlockType.code], []),
    (("url", "link"), "Skip URLs", [], [MarkKind.url]),
    (("citation", "cite", "reference"), "Skip citations", [], [MarkKind.citation_ref]),
    (("formula", "equation", "math"), "Skip formulas", [BlockType.equation], [MarkKind.formula]),
    (("parenthes",), "Skip parentheses", [], [MarkKind.parenthetical]),
    (("bracket",), "Skip brackets", [], [MarkKind.bracket]),
    (("brace",), "Skip braces", [], [MarkKind.brace]),
]
_JUDGMENT = ("unless", "but keep", "except", "when ", "author-year", "author year", "boilerplate", "legal")
_TRANSFORM = ("summar", "explain", "describe", "only the abstract", "in two sentences", "in one sentence")


def _occurrence(text: str, quote: str, at: int) -> int:
    n, i = 0, text.find(quote)
    while i != -1 and i <= at:
        n += 1
        if i == at:
            return n
        i = text.find(quote, i + len(quote))
    return max(1, n)


def _tokens(*texts: str) -> int:
    return math.ceil(sum(len(t) for t in texts) / 4)


class FixtureLlm:
    id = "fixture"
    model = FIXTURE_LLM_MODEL
    SYSTEM_TOKENS = 900

    async def annotate(self, blocks: Sequence[Block], rules: Sequence[RuleBrief], speech_md: str) -> LlmCall:
        rules_text = " ".join(f"{r.name} {r.instruction}" for r in rules).lower()
        wants_citations = any(k in rules_text for k in ("citation", "cite", "reference"))
        ops: list[SpanOp] = []
        if wants_citations:
            rule_id = next(
                (r.id for r in rules if any(k in f"{r.name} {r.instruction}".lower() for k in ("citation", "cite"))),
                None,
            )
            for b in blocks:
                for rx in (AUTHOR_YEAR_RE, NARRATIVE_YEAR_RE):
                    for m in rx.finditer(b.text):
                        quote = m.group(0)
                        ops.append(
                            SpanOp(
                                block_id=b.id,
                                op="skip",
                                quote=quote,
                                occurrence=_occurrence(b.text, quote, m.start()),
                                text="",
                                rule_id=rule_id,
                            )
                        )
        units = Units(
            in_tok=self.SYSTEM_TOKENS + _tokens(speech_md, rules_text, *(b.text for b in blocks)),
            out_tok=40 + 30 * len(ops),
        )
        return LlmCall(output=ops, units=units, model=self.model, stop_reason="end_turn")

    async def transform(self, blocks: Sequence[Block], instruction: str, speech_md: str) -> LlmCall:
        utterances: list[str] = []
        anchors: list[str] = []
        for b in blocks:
            text = b.text.strip()
            if not text:
                continue
            m = _FIRST_SENTENCE.match(text)
            utterances.append((m.group(1) if m else text).strip())
            anchors.append(b.id)
        units = Units(
            in_tok=self.SYSTEM_TOKENS + _tokens(instruction, speech_md, *(b.text for b in blocks)),
            out_tok=20 + _tokens(*utterances),
        )
        return LlmCall(
            output=TransformOutput(utterances=utterances, anchor_block_ids=anchors),
            units=units,
            model=self.model,
            stop_reason="end_turn",
        )

    async def compile_rule(self, instruction: str, context: dict[str, Any]) -> LlmCall:
        low = instruction.lower()
        drop_blocks: list[str] = []
        drop_marks: list[str] = []
        names: list[str] = []
        for keys, name, blocks, marks in _KEYWORDS:
            if any(k in low for k in keys):
                names.append(name)
                drop_blocks += [b.value for b in blocks if b.value not in drop_blocks]
                drop_marks += [m.value for m in marks if m.value not in drop_marks]
        keep_only: list[str] | None = None
        if "only read the body" in low or "only the body" in low:
            keep_only = ["heading", "paragraph", "list_item", "quote"]
            names.append("Only read the body")
        tier: Literal["filter", "annotate", "transform"]
        if any(k in low for k in _TRANSFORM):
            tier = "transform"
        elif any(k in low for k in _JUDGMENT) or not (drop_blocks or drop_marks or keep_only):
            tier = "annotate"
        else:
            tier = "filter"
        compiled = (
            {"drop_blocks": drop_blocks, "drop_marks": drop_marks, "replace": [], "keep_blocks_only": keep_only}
            if tier == "filter"
            else None
        )
        scope: dict[str, list[str]] = {}
        if context.get("site"):
            scope["sites"] = [str(context["site"])]
        elif context.get("doc_kind"):
            scope["types"] = [str(context["doc_kind"])]
        name = " and ".join(names) if names else instruction.strip().rstrip(".").capitalize()[:60]
        draft = CompiledRuleDraft(
            name=name,
            description=f"Fixture compile of: {instruction.strip()}",
            tier=tier,
            compiled=compiled,
            scope_suggestion=scope,
            instruction=instruction.strip(),
        )
        units = Units(in_tok=self.SYSTEM_TOKENS + _tokens(instruction, str(context)), out_tok=120)
        return LlmCall(output=draft, units=units, model=self.model, stop_reason="end_turn")


class FixtureVision:
    id = "fixture"
    model = FIXTURE_LLM_MODEL
    DESCRIPTION = "A test image showing a simple diagram with three labelled boxes connected by arrows."
    TRANSCRIPT = (
        "This is the fixture transcript of the image. It stands in for the text a vision model would read, "
        "so the rest of the pipeline can be tested offline."
    )

    async def describe_or_transcribe(
        self,
        image: bytes,
        media_type: str,
        rules: Sequence[RuleBrief],
        mode: Literal["auto", "transcribe", "describe"] = "auto",
    ) -> LlmCall:
        if mode == "transcribe":
            blocks = [VisionBlock(type="paragraph", text=self.TRANSCRIPT)]
        else:
            blocks = [VisionBlock(type="figure", text=self.DESCRIPTION)]
        units = Units(in_tok=1200 + _tokens(*(r.instruction for r in rules)), out_tok=60, images=1)
        return LlmCall(output=blocks, units=units, model=self.model, stop_reason="end_turn")


def reset_fixtures() -> None:
    FixtureTts.reset()
    FixtureStt.clear()
