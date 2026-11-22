"""Bring-your-own OpenAI-compatible speech server (P1, FR-VOICE-7): Kokoro-FastAPI, mlx-audio server,
Speaches, vLLM-Omni… anything serving `POST {base}/audio/speech`.

Configured in the user's `models.toml`:

    [tts.openai_compatible."kokoro-fastapi"]
    base_url = "http://127.0.0.1:8880/v1"
    api_model = "kokoro"            # model name the server expects (defaults to the table key)
    voices = ["af_heart"]
    response_format = "wav"         # wav | flac | opus | mp3 (anything libsndfile decodes)

No key is needed unless the server wants one (`OPEN_SPEECH_COMPAT_KEY`-style keys are passed via the
`api_key` argument). Thin by design: no word timings (estimated downstream), no style unless declared.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from typing import Any

from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.errors import map_provider_error
from open_speech.providers.pcm import decode_audio, f32_to_bytes


class OpenAICompatibleTts:
    id = "openai_compatible"
    timestamps = False

    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        api_key: str | None = None,
        clients: dict[str, Any] | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._models: dict[str, TtsModel] = {m.model: m for m in catalog.tts_models(self.id)}
        self._clients: dict[str, Any] = dict(clients or {})
        self._api_key = api_key or "not-needed"
        self._http_client = http_client

    def _client(self, m: TtsModel) -> Any:
        c = self._clients.get(m.model)
        if c is None:
            import openai

            c = openai.AsyncOpenAI(
                api_key=self._api_key, base_url=m.base_url, max_retries=0, timeout=60.0, http_client=self._http_client
            )
            self._clients[m.model] = c
        return c

    def voices(self) -> list[Voice]:
        out: list[Voice] = []
        for m in self._models.values():
            ok = bool(m.base_url)
            for v in m.voices:
                out.append(
                    Voice(
                        id=m.voice_id(v),
                        provider=self.id,
                        model=m.model,
                        name=f"{v} ({m.name or m.model})",
                        languages=list(m.languages),
                        styles=m.style is not None,
                        local=True,
                        available=ok,
                        unavailable_reason=None if ok else "Set base_url for this server in models.toml.",
                    )
                )
        return out

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else 4000,
            formats=[m.response_format or "wav"] if m else ["wav"],
            style=bool(m and m.style),
            languages=list(m.languages) if m else [],
        )

    def request_params(self, req: TtsRequest) -> dict[str, Any]:
        m = self._models[req.model]
        params: dict[str, Any] = {
            "model": m.api_model or m.model,
            "voice": req.voice,
            "input": req.text,
            "response_format": m.response_format or "wav",
        }
        if req.style and m.style == "instructions":
            params["instructions"] = req.style
        return params

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        m = self._models.get(req.model)
        if m is None or not m.base_url:
            raise map_provider_error(self.id, RuntimeError("server not configured"))
        buf = bytearray()
        try:
            async with self._client(m).audio.speech.with_streaming_response.create(**self.request_params(req)) as resp:
                async for piece in resp.iter_bytes():
                    buf.extend(piece)
            pcm, sr = await asyncio.to_thread(decode_audio, bytes(buf))
        except Exception as exc:
            raise map_provider_error(self.id, exc) from exc
        yield AudioChunk(pcm=f32_to_bytes(pcm), sample_rate=sr, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), in_tok=math.ceil(len(req.text) / 4), audio_sec=round(audio_sec, 3))
