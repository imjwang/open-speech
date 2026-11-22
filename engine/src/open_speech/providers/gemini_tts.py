"""Gemini TTS through the Interactions API (05 § 7, 08 § Verified): `client.aio.interactions.create(...,
stream=True)` with `api_version="v1beta"`, an audio response format (headerless L16, 24 kHz mono),
`generation_config.speech_config=[{"voice": ...}]`, and style as a `speech_metadata` annotation on the
text input (`generateContent` + `speechConfig` is rejected for these models).

Audio deltas are yielded as they arrive. Exact token usage from the final `interaction.completed` event is
returned by `price_units(req, ...)` for the same request object.
"""

from __future__ import annotations

import base64
import math
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable
from typing import Any

from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.errors import ProviderError, map_provider_error
from open_speech.providers.pcm import f32_to_bytes, int16_to_f32

DEFAULT_RATE = 24000


class _StreamError(Exception):
    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


def catalog_voices(models: Iterable[TtsModel]) -> list[Voice]:
    out: list[Voice] = []
    for m in models:
        for v in m.voices:
            out.append(
                Voice(
                    id=m.voice_id(v),
                    provider="gemini",
                    model=m.model,
                    name=f"{v} ({m.name or 'Gemini'})",
                    languages=list(m.languages),
                    styles=m.style is not None,
                    local=False,
                    timestamps=False,
                )
            )
    return out


class GeminiTts:
    id = "gemini"
    timestamps = False

    def __init__(
        self,
        catalog: ModelCatalog,
        api_key: str,
        *,
        client: Any | None = None,
        http_client: Any | None = None,
        base_url: str | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        self._models: dict[str, TtsModel] = {m.model: m for m in catalog.tts_models(self.id)}
        self._api_version = next((m.api_version for m in self._models.values() if m.api_version), "v1beta")
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    api_version=self._api_version,
                    base_url=base_url,
                    timeout=timeout_ms,
                    httpx_async_client=http_client,
                    retry_options=types.HttpRetryOptions(attempts=1),  # the synthesizer retries
                ),
            )
        self._client = client
        self._usage: OrderedDict[int, Units] = OrderedDict()

    def voices(self) -> list[Voice]:
        return catalog_voices(self._models.values())

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else 4000,
            formats=["l16"],
            style=bool(m and m.style == "speech_metadata"),
            languages=list(m.languages) if m else [],
        )

    def request_params(self, req: TtsRequest) -> dict[str, Any]:
        m = self._models.get(req.model)
        rate = (m.sample_rate if m and m.sample_rate else None) or DEFAULT_RATE
        text_part: dict[str, Any] = {"type": "text", "text": req.text}
        if req.style:
            # Annotation indices are byte offsets into the text.
            text_part["annotations"] = [
                {
                    "type": "speech_metadata",
                    "style": req.style,
                    "start_index": 0,
                    "end_index": len(req.text.encode("utf-8")),
                }
            ]
        speech: dict[str, Any] = {"voice": req.voice}
        if req.language:
            speech["language"] = req.language
        return {
            "model": req.model,
            "input": [text_part],
            "response_modalities": ["audio"],
            "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": rate},
            "generation_config": {"speech_config": [speech]},
            "stream": True,
            "store": False,
        }

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        params = self.request_params(req)
        rate = int(params["response_format"]["sample_rate"])
        usage: Units | None = None
        got_audio = False
        carry = b""
        try:
            stream = await self._client.aio.interactions.create(api_version=self._api_version, **params)
            async for ev in stream:
                etype = getattr(ev, "event_type", None)
                if etype == "step.delta":
                    delta = ev.delta
                    if getattr(delta, "type", None) != "audio" or not getattr(delta, "data", None):
                        continue
                    raw = carry + base64.b64decode(delta.data)
                    carry = raw[-1:] if len(raw) % 2 else b""
                    raw = raw[: len(raw) - len(carry)]
                    sr = getattr(delta, "sample_rate", None) or rate
                    got_audio = True
                    yield AudioChunk(pcm=f32_to_bytes(int16_to_f32(raw)), sample_rate=int(sr))
                elif etype == "interaction.completed":
                    inter = ev.interaction
                    status = getattr(inter, "status", None)
                    if status in ("failed", "cancelled"):
                        raise _StreamError(None, f"interaction {status}")
                    u = getattr(inter, "usage", None)
                    if u is not None:
                        usage = Units(
                            chars=len(req.text),
                            in_tok=int(getattr(u, "total_input_tokens", 0) or 0),
                            out_tok=int(getattr(u, "total_output_tokens", 0) or 0),
                        )
                elif etype == "error":
                    err = getattr(ev, "error", None)
                    code = getattr(err, "code", None)
                    digits = isinstance(code, int) or (isinstance(code, str) and code.isdigit())
                    status_code = int(code) if digits else None
                    raise _StreamError(status_code, str(getattr(err, "message", "stream error")))
        except ProviderError:
            raise
        except Exception as exc:
            raise map_provider_error(self.id, exc) from exc
        if not got_audio:
            raise map_provider_error(self.id, RuntimeError("no audio in response"))
        if usage is not None:
            self._usage[id(req)] = usage
            while len(self._usage) > 256:
                self._usage.popitem(last=False)
        yield AudioChunk(sample_rate=rate, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        exact = self._usage.pop(id(req), None)
        if exact is not None:
            exact.audio_sec = round(audio_sec, 3)
            return exact
        return Units(chars=len(req.text), in_tok=math.ceil(len(req.text) / 4), audio_sec=round(audio_sec, 3))
