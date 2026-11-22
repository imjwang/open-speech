"""OpenAI TTS (05 § 7): `audio.speech` streaming, `gpt-4o-mini-tts`, `response_format="opus"`,
`instructions` for style.

Format decision: OpenAI's "opus" is Ogg/Opus. The adapter streams the bytes, keeps them verbatim
(`AudioChunk.encoded`, stored in the cache "as received" — no re-encode, no generational loss) and also
decodes them once with libsndfile so the synthesizer can measure the duration, find the voiced region for
word-timing estimates, and the transport can time-stretch PCM. Decoding happens after the stream ends
(an utterance is 1–3 sentences, so the whole response arrives in ~2 s).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Iterable
from typing import Any

from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.errors import map_provider_error
from open_speech.providers.pcm import decode_audio, f32_to_bytes


def catalog_voices(models: Iterable[TtsModel]) -> list[Voice]:
    """OpenAI voices straight from the catalog (no client needed, so the registry can list them as
    unavailable when there is no key)."""
    out: list[Voice] = []
    for m in models:
        for v in m.voices:
            out.append(
                Voice(
                    id=m.voice_id(v),
                    provider="openai",
                    model=m.model,
                    name=f"{v.capitalize()} ({m.name or 'OpenAI'})",
                    languages=list(m.languages),
                    styles=m.style is not None,
                    local=False,
                    timestamps=False,
                )
            )
    return out


class OpenAITts:
    id = "openai"
    timestamps = False

    def __init__(
        self,
        catalog: ModelCatalog,
        api_key: str,
        *,
        client: Any | None = None,
        http_client: Any | None = None,
        base_url: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._models: dict[str, TtsModel] = {m.model: m for m in catalog.tts_models(self.id)}
        if client is None:
            import openai

            # Retries happen in the synthesizer (jittered backoff + fallback), so the SDK doesn't retry.
            client = openai.AsyncOpenAI(
                api_key=api_key, max_retries=0, timeout=timeout_s, http_client=http_client, base_url=base_url
            )
        self._client = client

    def voices(self) -> list[Voice]:
        return catalog_voices(self._models.values())

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else 4000,
            formats=list(m.formats) if m else ["opus"],
            style=bool(m and m.style == "instructions"),
            languages=list(m.languages) if m else [],
        )

    def request_params(self, req: TtsRequest) -> dict[str, Any]:
        m = self._models.get(req.model)
        params: dict[str, Any] = {
            "model": (m.snapshot if m and m.snapshot else req.model),
            "voice": req.voice,
            "input": req.text,
            "response_format": "opus",
        }
        if req.style and (m is None or m.style == "instructions"):
            params["instructions"] = req.style
        return params

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        params = self.request_params(req)
        buf = bytearray()
        try:
            async with self._client.audio.speech.with_streaming_response.create(**params) as resp:
                async for piece in resp.iter_bytes():
                    buf.extend(piece)
        except Exception as exc:
            raise map_provider_error(self.id, exc) from exc
        if not buf:
            raise map_provider_error(self.id, RuntimeError("empty audio response"))
        data = bytes(buf)
        try:
            pcm, sr = await asyncio.to_thread(decode_audio, data)
        except Exception as exc:
            raise map_provider_error(self.id, exc) from exc
        yield AudioChunk(pcm=f32_to_bytes(pcm), sample_rate=sr, encoded=data, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        # Text tokens are estimated from chars; audio output tokens are derived from the measured audio
        # seconds by the pricing catalog (`audio_tokens_per_sec`).
        return Units(chars=len(req.text), in_tok=math.ceil(len(req.text) / 4), audio_sec=round(audio_sec, 3))

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
