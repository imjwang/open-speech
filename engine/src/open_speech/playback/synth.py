"""Synthesizer (05 § 7, FR-CACHE-1, FR-COST-1/4, FR-VOICE-4/5, NFR-2/8).

`synth(text, voice_id)`:
1. Resolve the voice. Unusable (no key, weights missing) → fall back to the local voice with a notice.
2. Cache first: a hit writes a `$0` ledger row (`cache_hit`, with the avoided cost) and makes zero
   provider calls — so replaying a document costs nothing (M0 exit).
3. Paid voice → `BudgetGuard.check_paid()` first; over budget → local voice with a notice.
4. Provider call with jittered exponential backoff (tenacity) on 429 / 5xx / network errors.
   Persistent failure → `settings.playback.fallback`: "kokoro" (default: the local voice), "another_voice"
   (first available voice of another provider), or "same_provider" (no fallback; the error propagates).
   A rejected key (401/403) marks the key invalid.
5. Word timings: provider-exact when the adapter reports them, else syllable-weighted over the measured,
   silence-trimmed duration (`playback/timing.py`).
6. Store as Ogg/Opus (vendor Ogg kept verbatim), write the ledger row, return `SynthResult`.

Concurrent requests for the same audio key share one provider call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_random_exponential

from open_speech.cache.audio import AudioCache, CachedAudio
from open_speech.config import UserSettings
from open_speech.errors import BudgetExceeded, EngineError
from open_speech.playback.timing import estimate_word_timings, voiced_bounds
from open_speech.providers.base import TtsProvider, TtsRequest, WordTiming
from open_speech.providers.errors import is_retryable, map_provider_error, provider_name
from open_speech.providers.pcm import bytes_to_f32, decode_audio
from open_speech.providers.registry import ProviderRegistry
from open_speech.service.events import EventBus
from open_speech.usage.budget import BudgetGuard
from open_speech.usage.ledger import Ledger

log = logging.getLogger(__name__)


@dataclass
class SynthResult:
    audio: CachedAudio
    voice_id: str  # the voice actually used (differs from the request after a fallback)
    notice: str | None = None  # spoken/bar notice, e.g. "Budget reached; reading with the local voice."
    cache_hit: bool = False
    usd: Decimal = Decimal(0)
    style_applied: bool = True


@dataclass
class _Resolved:
    voice_id: str
    provider: TtsProvider
    req: TtsRequest
    local: bool


class Synthesizer:
    def __init__(
        self,
        registry: ProviderRegistry,
        audio_cache: AudioCache,
        ledger: Ledger,
        budget: BudgetGuard,
        settings: Callable[[], UserSettings],
        events: EventBus,
        *,
        retry_attempts: int = 3,
        retry_base_s: float = 0.5,
        retry_max_s: float = 8.0,
        local_models: Any | None = None,
    ) -> None:
        self.registry = registry
        self.cache = audio_cache
        self.ledger = ledger
        self.budget = budget
        self._settings = settings
        self._events = events
        self._attempts = max(1, retry_attempts)
        self._base = retry_base_s
        self._max = retry_max_s
        self._local_models = local_models  # LocalModelManager, to install Kokoro on first use (FR-SETUP-3)
        self._inflight: dict[str, asyncio.Future[CachedAudio]] = {}

    # ------------------------------------------------------------------ public

    async def synth(
        self,
        text: str,
        voice_id: str,
        *,
        style: str | None = None,
        document_id: str | None = None,
        turn_id: str | None = None,
        op: str = "tts",
    ) -> SynthResult:
        notices: list[str] = []
        try:
            res = self._resolve(voice_id)
        except EngineError as exc:
            res = await self._fallback_resolve(voice_id, exc, notices)
        try:
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices)
        except BudgetExceeded as exc:
            if res.local:
                raise
            res = await self._fallback_resolve(res.voice_id, exc, notices, reason="budget")
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices)
        except EngineError as exc:
            if res.local:
                raise
            if getattr(exc, "key_invalid", False):
                self.registry.keys.mark_invalid(res.provider.id)
            res = await self._fallback_resolve(res.voice_id, exc, notices, reason="failure")
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices)

    def cached(self, text: str, voice_id: str, *, style: str | None = None) -> CachedAudio | None:
        """Cache lookup without side effects on the ledger (used for prefetch decisions and estimates)."""
        try:
            prov, tmpl = self.registry.tts_for(voice_id)
        except EngineError:
            return None
        style = style if prov.limits(tmpl.model).style else None
        key = AudioCache.key(text, prov.id, tmpl.model, tmpl.voice, style)
        return self.cache.get(key, touch=False)

    # ------------------------------------------------------------------ resolution

    def _resolve(self, voice_id: str) -> _Resolved:
        prov, tmpl = self.registry.tts_for(voice_id)
        return _Resolved(voice_id, prov, tmpl, self.registry.is_local(voice_id))

    async def _fallback_resolve(
        self, failed_voice: str, exc: EngineError, notices: list[str], *, reason: str = "unavailable"
    ) -> _Resolved:
        policy = self._settings().playback.fallback
        if reason == "failure" and policy == "same_provider":
            raise exc
        candidates: list[str] = []
        if reason == "failure" and policy == "another_voice":
            failed_provider = failed_voice.split(":", 1)[0]
            for v in self.registry.voices():
                if v.available and v.provider != failed_provider and not v.local:
                    candidates.append(v.id)
        candidates.append(self.registry.default_local_voice())
        for cand in candidates:
            if cand == failed_voice:
                continue
            try:
                res = self._resolve(cand)
            except EngineError:
                if cand.startswith("kokoro:") and self._local_models is not None:
                    try:
                        await self._local_models.ensure_installed(cand.split(":")[1])
                        res = self._resolve(cand)
                    except EngineError:
                        continue
                else:
                    continue
            notices.append(self._notice(reason, failed_voice, exc, res.local))
            return res
        raise exc

    @staticmethod
    def _notice(reason: str, failed_voice: str, exc: EngineError, local: bool) -> str:
        target = "the local voice" if local else "another voice"
        name = provider_name(failed_voice.split(":", 1)[0])
        if reason == "budget":
            return f"Budget reached; reading with {target}."
        if exc.code == "offline":
            return f"I couldn't reach {name}; reading with {target}."
        if exc.code == "missing_key":
            article = "an" if name[:1] in "AEIOU" else "a"
            return f"That voice needs {article} {name} key; reading with {target}."
        if reason == "unavailable":
            return f"That voice isn't available right now; reading with {target}."
        return f"{name} isn't responding; reading with {target}."

    # ------------------------------------------------------------------ synthesis

    async def _synth_with(
        self,
        res: _Resolved,
        text: str,
        style: str | None,
        document_id: str | None,
        turn_id: str | None,
        op: str,
        notices: list[str],
    ) -> SynthResult:
        prov, model, voice = res.provider, res.req.model, res.req.voice
        style_ok = bool(style) and prov.limits(model).style
        if style and not style_ok and not any("style" in n for n in notices):
            notices.append("This voice doesn't take style instructions, so I'm reading without them.")
        eff_style = style if style_ok else None
        key = AudioCache.key(text, prov.id, model, voice, eff_style)
        req = TtsRequest(text=text, model=model, voice=voice, style=eff_style, language=res.req.language)
        notice = " ".join(notices) or None

        hit = self.cache.get(key)
        if hit is not None:
            self.ledger.record(
                provider=prov.id, model=model, op=op, units=prov.price_units(req, hit.duration_s),
                document_id=document_id, turn_id=turn_id, cache_hit=True, local=res.local,
            )
            return SynthResult(hit, res.voice_id, notice, cache_hit=True, style_applied=style_ok or not style)

        if not res.local:
            self.budget.check_paid()

        fut = self._inflight.get(key)
        if fut is not None:  # another caller is already synthesizing this exact audio
            audio = await asyncio.shield(fut)
            self.ledger.record(
                provider=prov.id, model=model, op=op, units=prov.price_units(req, audio.duration_s),
                document_id=document_id, turn_id=turn_id, cache_hit=True, local=res.local,
            )
            return SynthResult(audio, res.voice_id, notice, cache_hit=True, style_applied=style_ok or not style)

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            audio, usd = await self._call(prov, req, key, res.local, op, document_id, turn_id)
            fut.set_result(audio)
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved for waiters-less futures
            raise
        finally:
            self._inflight.pop(key, None)
        return SynthResult(audio, res.voice_id, notice, usd=usd, style_applied=style_ok or not style)

    async def _call(
        self,
        prov: TtsProvider,
        req: TtsRequest,
        key: str,
        local: bool,
        op: str,
        document_id: str | None,
        turn_id: str | None,
    ) -> tuple[CachedAudio, Decimal]:
        pcm_parts: list[bytes] = []
        encoded: list[bytes] = []
        words: list[WordTiming] = []
        sr = 24000

        async def attempt() -> None:
            nonlocal sr
            pcm_parts.clear()
            encoded.clear()
            words.clear()
            try:
                async for chunk in prov.synthesize(req):
                    if chunk.pcm:
                        pcm_parts.append(chunk.pcm)
                        sr = chunk.sample_rate
                    if chunk.encoded:
                        encoded.append(chunk.encoded)
                    words.extend(chunk.words)
            except EngineError:
                raise
            except Exception as exc:
                raise map_provider_error(prov.id, exc) from exc

        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._attempts),
            wait=wait_random_exponential(multiplier=self._base, max=self._max),
            retry=retry_if_exception(is_retryable),
            reraise=True,
        )
        async for att in retrying:
            with att:
                await attempt()

        pcm = bytes_to_f32(b"".join(pcm_parts)) if pcm_parts else np.zeros(0, dtype=np.float32)
        enc = b"".join(encoded) if encoded else None
        if pcm.size == 0 and enc is not None:
            pcm, sr = await asyncio.to_thread(decode_audio, enc)
        if pcm.size == 0:
            raise EngineError("provider_error", f"{provider_name(prov.id)} returned no audio.", provider=prov.id)
        duration = pcm.size / sr
        if not (prov.timestamps and words):
            start, end = voiced_bounds(pcm, sr)
            words = estimate_word_timings(req.text, duration, speech_start_s=start, speech_end_s=end)
        words.sort(key=lambda w: w.start)
        if enc is not None and enc[:4] == b"OggS":
            audio = await asyncio.to_thread(self.cache.put_encoded, key, enc, words)
        else:
            audio = await asyncio.to_thread(self.cache.put, key, pcm, sr, words)
        usd = self.ledger.record(
            provider=prov.id, model=req.model, op=op, units=prov.price_units(req, duration),
            document_id=document_id, turn_id=turn_id, local=local,
        )
        return audio, usd
