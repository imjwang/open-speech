"""Synthesizer (05 § 7, FR-CACHE-1, FR-COST-1/4, FR-VOICE-4/5, NFR-2/8).

`synth(text, voice_id)`:
1. Resolve the voice. Unusable (no key, weights missing) → fall back to the local voice with a notice.
2. Cache first: a hit writes a `$0` ledger row (`cache_hit`, with the avoided cost) and makes zero
   provider calls — so replaying a document costs nothing (M0 exit).
3. Paid voice → a free monthly allowance first (`FreeTierGuard`, Google's Chirp characters): inside it the call
   is free and the budget doesn't apply; used up with `playback.beyond_free_tier = "local"` → the local voice
   with a notice. Otherwise `BudgetGuard.check_paid()`; over budget → local voice with a notice.
4. Provider call with jittered exponential backoff (tenacity) on 429 / 5xx / network errors.
   Persistent failure → `settings.playback.fallback`: "kokoro" (default: the local voice), "another_voice"
   (first available voice of another provider), or "same_provider" (no fallback; the error propagates).
   A rejected key (401/403) marks the key invalid — unless the provider says the key is fine but may not call
   this API (Google's 403 `API_KEY_SERVICE_BLOCKED`): then only that provider's voices become unavailable.
   Fallback voices follow the language of the voice that failed (`registry.default_local_voice(language)`).
5. Word timings: provider-exact when the adapter reports them, else estimated from the audio: pauses matched
   to punctuation pin word boundaries, syllable/letter weights in between (`playback/timing.py`).
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

from open_speech.cache.audio import AudioCache, CachedAudio, VoiceRef
from open_speech.config import UserSettings
from open_speech.errors import BudgetExceeded, EngineError
from open_speech.i18n import t
from open_speech.playback.timing import estimate_word_timings_from_audio
from open_speech.providers.base import TtsProvider, TtsRequest, WordTiming, cache_identity
from open_speech.providers.errors import is_retryable, map_provider_error, provider_name
from open_speech.providers.pcm import bytes_to_f32, decode_audio
from open_speech.providers.registry import ProviderRegistry
from open_speech.service.events import EventBus
from open_speech.usage.budget import BudgetGuard
from open_speech.usage.free_tier import FreeTierExhausted, FreeTierGuard
from open_speech.usage.ledger import Ledger

log = logging.getLogger(__name__)

# (pcm float32 mono, sample rate, word timings, voice actually used). See `Synthesizer.synth(on_audio=…)`.
AudioReady = Callable[[np.ndarray, int, list[WordTiming], str], None]
# (pcm piece, sample rate, voice actually used, samples of this utterance before the piece). See `on_chunk`.
ChunkReady = Callable[[np.ndarray, int, str, int], None]


@dataclass
class SynthResult:
    audio: CachedAudio
    voice_id: str  # the voice actually used (differs from the request after a fallback)
    notice: str | None = None  # spoken/bar notice, e.g. "Budget reached; reading with the local voice."
    # Why `notice` was given: "budget" | "free_tier" | "offline" | "unavailable" | "failure" | "no_style".
    # Spoken notices are said once per session per reason (`Transport.show_notice`).
    notice_reason: str | None = None
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
        free_tier: FreeTierGuard | None = None,
    ) -> None:
        self.registry = registry
        self.free_tier = free_tier  # free monthly allowances (Google's Chirp characters), checked before the budget
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
        on_audio: AudioReady | None = None,
        on_chunk: ChunkReady | None = None,
    ) -> SynthResult:
        """`on_audio` is called on the loop as soon as freshly synthesized audio exists, before it is encoded
        and stored, so a player can start without waiting for the Opus encode and a decode of the file just
        written (NFR-1, time to first audio). It is not called on a cache hit.

        `on_chunk` is called with each piece a streaming local voice (`provider.streams`, Qwen3) yields while it is
        still generating, so a player can start an utterance before it is finished (growing utterances). A retried
        attempt starts again at offset 0 (the same audio: Qwen3 seeds each utterance)."""
        notices: list[tuple[str, str]] = []  # (reason, text)
        try:
            res = self._resolve(voice_id)
        except EngineError as exc:
            res = await self._fallback_resolve(voice_id, exc, notices)
        try:
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices, on_audio, on_chunk)
        except FreeTierExhausted as exc:
            if res.local:
                raise
            res = await self._fallback_resolve(res.voice_id, exc, notices, reason="free_tier")
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices, on_audio, on_chunk)
        except BudgetExceeded as exc:
            if res.local:
                raise
            res = await self._fallback_resolve(res.voice_id, exc, notices, reason="budget")
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices, on_audio, on_chunk)
        except EngineError as exc:
            # A local voice that fails reads on with the default local voice when it isn't that voice itself
            # (`provider.falls_back`: Qwen3 → Kokoro); Kokoro's own failures are reported.
            if res.local and not getattr(res.provider, "falls_back", False):
                raise
            if getattr(exc, "key_invalid", False):
                self.registry.keys.mark_invalid(res.provider.id)
            res = await self._fallback_resolve(res.voice_id, exc, notices, reason="failure", local=res.local)
            return await self._synth_with(res, text, style, document_id, turn_id, op, notices, on_audio, on_chunk)

    def cached(self, text: str, voice_id: str, *, style: str | None = None) -> CachedAudio | None:
        """Cache lookup without side effects on the ledger (used for prefetch decisions and estimates)."""
        try:
            prov, tmpl = self.registry.tts_for(voice_id)
        except EngineError:
            return None
        style = style if prov.limits(tmpl.model).style else None
        key = AudioCache.key(text, prov.id, cache_identity(prov, tmpl), tmpl.voice, style)
        return self.cache.get(key, touch=False)

    # ------------------------------------------------------------------ resolution

    def _resolve(self, voice_id: str) -> _Resolved:
        prov, tmpl = self.registry.tts_for(voice_id)
        return _Resolved(voice_id, prov, tmpl, self.registry.is_local(voice_id))

    async def _fallback_resolve(
        self, failed_voice: str, exc: EngineError, notices: list[tuple[str, str]], *, reason: str = "unavailable",
        local: bool = False,
    ) -> _Resolved:
        """The voice to read on with. `local`: the voice that failed runs on this Mac (Qwen3), so the local default
        voice reads on whatever `playback.fallback` says; that policy is about cloud outages (a paid cloud voice must
        never be chosen behind the user's back for a free local one, and "same provider" can't help a crashed model)."""
        policy = self._settings().playback.fallback
        if reason == "failure" and policy == "same_provider" and not local:
            raise exc
        candidates: list[str] = []
        if reason == "failure" and policy == "another_voice" and not local:
            failed_provider = failed_voice.split(":", 1)[0]
            for v in self.registry.voices():
                if v.available and v.provider != failed_provider and not v.local:
                    candidates.append(v.id)
        candidates.append(self.registry.default_local_voice(self._language_of(failed_voice)))
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

    def _language_of(self, voice_id: str) -> str | None:
        """The language a voice reads (its first declared one), so a fallback keeps reading that language."""
        try:
            languages = self.registry.voice(voice_id).languages
        except EngineError:
            return None
        return languages[0].split("-")[0].lower() if languages else None

    @staticmethod
    def _notice(reason: str, failed_voice: str, exc: EngineError, local: bool) -> tuple[str, str]:
        target = t("notice.local_voice") if local else t("notice.another_voice")
        name = provider_name(failed_voice.split(":", 1)[0])
        if reason == "budget":
            return "budget", t("notice.budget", target=target)
        if reason == "free_tier":
            return "free_tier", t("notice.free_tier", target=target)
        if exc.code == "offline":
            return "offline", t("notice.offline", name=name, target=target)
        if exc.code == "unavailable" or reason == "unavailable":
            return "unavailable", t("notice.unavailable", target=target)
        return "failure", t("notice.not_responding", name=name, target=target)

    # ------------------------------------------------------------------ synthesis

    async def _synth_with(
        self,
        res: _Resolved,
        text: str,
        style: str | None,
        document_id: str | None,
        turn_id: str | None,
        op: str,
        notices: list[tuple[str, str]],
        on_audio: AudioReady | None = None,
        on_chunk: ChunkReady | None = None,
    ) -> SynthResult:
        prov, model, voice = res.provider, res.req.model, res.req.voice
        style_ok = bool(style) and prov.limits(model).style
        no_style = ("no_style", t("notice.no_style"))
        if style and not style_ok and no_style not in notices:
            notices.append(no_style)
        eff_style = style if style_ok else None
        req = TtsRequest(text=text, model=model, voice=voice, style=eff_style, language=res.req.language)
        key = AudioCache.key(text, prov.id, cache_identity(prov, req), voice, eff_style)
        notice = " ".join(text for _, text in notices) or None
        reason = notices[0][0] if notices else None

        hit = self.cache.get(key)
        if hit is not None:
            self.ledger.record(
                provider=prov.id, model=model, op=op, units=prov.price_units(req, hit.duration_s),
                document_id=document_id, turn_id=turn_id, cache_hit=True, local=res.local,
            )
            return SynthResult(hit, res.voice_id, notice, reason, cache_hit=True, style_applied=style_ok or not style)

        if not res.local:
            covered = self.free_tier.check(prov.id, model, len(text)) if self.free_tier is not None else "none"
            if covered != "free":  # inside a free allowance the call costs nothing, so the budget doesn't apply
                self.budget.check_paid()

        fut = self._inflight.get(key)
        if fut is not None:  # another caller is already synthesizing this exact audio
            audio = await asyncio.shield(fut)
            self.ledger.record(
                provider=prov.id, model=model, op=op, units=prov.price_units(req, audio.duration_s),
                document_id=document_id, turn_id=turn_id, cache_hit=True, local=res.local,
            )
            return SynthResult(
                audio, res.voice_id, notice, reason, cache_hit=True, style_applied=style_ok or not style
            )

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            audio, usd = await self._call(
                prov, req, key, res.local, op, document_id, turn_id, on_audio=on_audio, voice_id=res.voice_id,
                on_chunk=on_chunk if getattr(prov, "streams", False) else None,
            )
            fut.set_result(audio)
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved for waiters-less futures
            raise
        finally:
            self._inflight.pop(key, None)
        return SynthResult(audio, res.voice_id, notice, reason, usd=usd, style_applied=style_ok or not style)

    async def _call(
        self,
        prov: TtsProvider,
        req: TtsRequest,
        key: str,
        local: bool,
        op: str,
        document_id: str | None,
        turn_id: str | None,
        *,
        on_audio: AudioReady | None = None,
        voice_id: str = "",
        on_chunk: ChunkReady | None = None,
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
            delivered = 0
            try:
                async for chunk in prov.synthesize(req):
                    if chunk.pcm:
                        pcm_parts.append(chunk.pcm)
                        sr = chunk.sample_rate
                        if on_chunk is not None:
                            piece = bytes_to_f32(chunk.pcm)
                            try:
                                on_chunk(piece, sr, voice_id, delivered)
                            except Exception:
                                log.exception("on_chunk hook failed")
                            delivered += piece.size
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
        if not (prov.timestamps and words):  # pauses in the audio matched to punctuation (playback/timing.py)
            words = estimate_word_timings_from_audio(req.text, pcm, sr)
        words.sort(key=lambda w: w.start)
        if on_audio is not None:
            try:
                on_audio(pcm, sr, list(words), voice_id)
            except Exception:
                log.exception("on_audio hook failed")
        ref = VoiceRef(prov.id, req.model, req.voice, req.style)
        if enc is not None and enc[:4] == b"OggS":
            audio = await asyncio.to_thread(self.cache.put_encoded, key, enc, words, text=req.text, voice=ref)
        else:
            audio = await asyncio.to_thread(self.cache.put, key, pcm, sr, words, text=req.text, voice=ref)
        usd = self.ledger.record(
            provider=prov.id, model=req.model, op=op, units=prov.price_units(req, duration),
            document_id=document_id, turn_id=turn_id, local=local,
        )
        return audio, usd
