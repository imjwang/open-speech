"""Pre-flight estimates (08 § Budgets and estimates, FR-COST-3; target ±25% for text documents).

- Speech: characters that will be spoken (the plan's utterances when a script exists, otherwise the
  extracted text minus what the default skip lines typically drop), **minus utterances already in the audio
  cache**, priced per the voice's model; audio length ≈ chars / 15 per second. A voice with a free monthly
  allowance (Google's Chirp characters) is priced against what is left of it: inside it the line says "within
  the free allowance" at $0; beyond it, characters cost only when the user chose to keep paying
  (`playback.beyond_free_tier = "pay"`), otherwise they are read with the local voice for free.
- AI cleanup: block tokens × 1.1 + an output allowance per ~1.5k-token batch, plus the cached
  prefix (one cache write, then cache reads). Zero when the plan already covers the document.
- Images: ~1.2k input + ~150 output tokens per image on the planning model.
"""

from __future__ import annotations

import math
from decimal import Decimal

from open_speech.api.schemas import Estimate, EstimateLine
from open_speech.cache.audio import AudioCache
from open_speech.document.model import BlockType, Document, MarkKind
from open_speech.ids import split_voice_id
from open_speech.plan.script import SpeechScript
from open_speech.providers.base import Units, Voice
from open_speech.usage.pricing import Pricing, money

CHARS_PER_SECOND = 15
BATCH_TOKENS = 1500
OUTPUT_PER_BATCH = 150
PREFIX_TOKENS = 1200
IMAGE_IN_TOKENS = 1200
IMAGE_OUT_TOKENS = 150
DEFAULT_DROP_BLOCKS = {BlockType.header, BlockType.footer, BlockType.page_number}
DEFAULT_DROP_MARKS = {MarkKind.citation_ref, MarkKind.footnote_ref, MarkKind.url}


def _llm_provider(model: str) -> str:
    return "fixture" if model.startswith("fixture") else "anthropic"


def _default_chars(document: Document) -> tuple[int, list[str]]:
    total = 0
    for b in document.blocks:
        if b.type in DEFAULT_DROP_BLOCKS:
            continue
        dropped = sum(max(0, m.end - m.start) for m in b.marks if m.kind in DEFAULT_DROP_MARKS)
        total += max(0, len(b.text) - dropped)
    return total, [b.text for b in document.blocks if b.type not in DEFAULT_DROP_BLOCKS]


def estimate_read(
    document: Document,
    script: SpeechScript | None,
    *,
    voice: Voice,
    mode: str,
    llm_model: str,
    pricing: Pricing,
    audio_cache: AudioCache,
    threshold_usd: Decimal | str = "0.50",
    style: str | None = None,
    images: int = 0,
    model_identity: str | None = None,
    free_remaining: int | None = None,
    beyond_free: str = "local",
) -> Estimate:
    """`model_identity`: the model as the audio cache keys record it (`providers.base.cache_identity`; Kokoro
    adds its weights' precision); defaults to the voice's model. `free_remaining`: characters left of the voice's
    free monthly allowance (None: it has none); `beyond_free`: "local" or "pay" past it (`FreeTierGuard`)."""
    lines: list[EstimateLine] = []
    assumptions = [
        f"About {CHARS_PER_SECOND} characters per second of speech at 1x.",
        f"Prices from pricing.toml version {pricing.version}; all figures are estimates (target ±25%).",
    ]
    total = Decimal(0)

    # ---------------------------------------------------------------- speech
    _, _, voice_name = split_voice_id(voice.id)
    cached = 0
    if script is not None:
        chars = 0
        for u in script.utterances:
            key = AudioCache.key(u.text, voice.provider, model_identity or voice.model, voice_name, style)
            if audio_cache.contains(key):
                cached += 1
            else:
                chars += len(u.text)
        if cached:
            assumptions.append(f"{cached} of {len(script.utterances)} utterances are already cached ($0).")
    else:
        chars, _ = _default_chars(document)
        assumptions.append("No plan yet: the default skips assumed (headers, footers, page numbers, citations, URLs).")
    tts_units = Units(chars=chars, audio_sec=chars / CHARS_PER_SECOND)
    free_note = ""
    if voice.local:
        tts_usd = Decimal(0)
    elif free_remaining is not None:
        beyond = max(0, chars - max(0, free_remaining))
        if beyond == 0:
            free_note = ", within the free allowance"
            tts_usd = Decimal(0)
            assumptions.append(f"Within this month's free allowance ({max(0, free_remaining):,} characters left).")
        elif beyond_free == "pay":
            paid = Units(chars=beyond, audio_sec=beyond / CHARS_PER_SECOND)
            tts_usd = pricing.cost(voice.provider, voice.model, paid)
            free_note = f", {beyond:,} beyond the free allowance"
            assumptions.append(f"{chars - beyond:,} characters fit in this month's free allowance; the rest is paid.")
        else:
            tts_usd = Decimal(0)
            free_note = f", {beyond:,} beyond the free allowance read with the local voice"
            assumptions.append("Past the free allowance, reading continues with the local voice (free).")
    else:
        tts_usd = pricing.cost(voice.provider, voice.model, tts_units)
    total += tts_usd
    hours = tts_units.audio_sec / 3600
    length = f"{hours:.1f} h" if hours >= 1 else f"{max(1, round(tts_units.audio_sec / 60))} min"
    lines.append(
        EstimateLine(
            item=f"Speech: {voice.name}, {chars:,} characters (~{length} at 1x){free_note}",
            usd_estimate=money(tts_usd),
        )
    )
    if voice.local:
        assumptions.append("The local voice is free.")

    # ---------------------------------------------------------------- AI cleanup (Claude)
    if mode == "ai_enhanced":
        upto = script.annotated_upto_block if (script is not None and script.mode == "ai_enhanced") else None
        blocks = [b for b in document.blocks if b.type not in DEFAULT_DROP_BLOCKS and (upto is None or b.order > upto)]
        block_tokens = math.ceil(sum(len(b.text) for b in blocks) / 4)
        if block_tokens > 0:
            batches = max(1, math.ceil(block_tokens / BATCH_TOKENS))
            units = Units(
                in_tok=math.ceil(block_tokens * 1.1),
                out_tok=batches * OUTPUT_PER_BATCH,
                cache_write_tok=PREFIX_TOKENS,
                cache_read_tok=PREFIX_TOKENS * (batches - 1),
            )
            plan_usd = pricing.cost(_llm_provider(llm_model), llm_model, units)
            total += plan_usd
            lines.append(
                EstimateLine(
                    item=f"AI cleanup: {llm_model}, ~{units.in_tok:,} tokens in {batches} batches",
                    usd_estimate=money(plan_usd),
                )
            )
            assumptions.append("Planning tokens ≈ block tokens × 1.1 plus ~150 output tokens per batch.")
        else:
            lines.append(EstimateLine(item="AI cleanup: already done", usd_estimate="0.00"))

    # ---------------------------------------------------------------- images
    if images:
        units = Units(in_tok=IMAGE_IN_TOKENS * images, out_tok=IMAGE_OUT_TOKENS * images, images=images)
        img_usd = pricing.cost(_llm_provider(llm_model), llm_model, units)
        total += img_usd
        lines.append(EstimateLine(item=f"Image descriptions: {images}", usd_estimate=money(img_usd)))

    threshold = Decimal(str(threshold_usd))
    return Estimate(
        usd_estimate=money(total),
        breakdown=lines,
        assumptions=assumptions,
        requires_confirmation=total > threshold,
    )
