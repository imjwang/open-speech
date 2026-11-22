"""Gemini speech-to-text (07 § STT routing, order 2): batch transcription with the catalog's batch model
(`gemini-3.5-transcribe`) through the Interactions API (`transcription_config`, verbatim mode, optional
word timestamps from `word_info` annotations). `stream()` buffers and transcribes on mic stop, with
optional interim partials (see `stt_common`). Live API streaming is the stretch goal.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import SttEvent, Transcript, Units
from open_speech.providers.errors import map_provider_error
from open_speech.providers.pcm import audio_info_duration
from open_speech.providers.stt_common import BYTES_PER_SEC, UsageSink, buffered_stream, mime_for, parse_offset


class GeminiStt:
    id = "gemini"

    def __init__(
        self,
        catalog: ModelCatalog,
        api_key: str,
        *,
        client: Any | None = None,
        http_client: Any | None = None,
        partial_interval_s: float | None = None,
        usage_sink: UsageSink | None = None,
        base_url: str | None = None,
        timeout_ms: int = 30000,
        retry_attempts: int | None = None,
    ) -> None:
        entry = catalog.stt_for(self.id)
        self._batch = list(entry.batch) if entry else ["gemini-3.5-transcribe"]
        self._api_version = (entry.api_version if entry else None) or "v1beta"
        self.partial_interval_s = partial_interval_s
        self.usage_sink = usage_sink
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
                    retry_options=types.HttpRetryOptions(attempts=retry_attempts) if retry_attempts else None,
                ),
            )
        self._client = client

    @property
    def model(self) -> str:
        return self._batch[0]

    def price_units(self, audio_sec: float) -> Units:
        return Units(audio_sec=round(audio_sec, 3))

    def request_params(self, data: bytes, mime: str, *, words: bool, lang: str | None) -> dict[str, Any]:
        tc: dict[str, Any] = {"mode": {"type": "verbatim"}}
        if words:
            tc["timestamp_granularities"] = ["word"]
        if lang:
            tc["language_codes"] = [lang]
        return {
            "model": self.model,
            "input": [{"type": "audio", "data": base64.b64encode(data).decode("ascii"), "mime_type": mime}],
            "generation_config": {"transcription_config": tc},
            "response_modalities": ["text"],
            "store": False,
        }

    async def _run(self, data: bytes, mime: str, *, words: bool, lang: str | None) -> tuple[str, list[dict[str, Any]]]:
        try:
            it = await self._client.aio.interactions.create(
                api_version=self._api_version, **self.request_params(data, mime, words=words, lang=lang)
            )
        except Exception as exc:
            raise map_provider_error(self.id, exc, feature="transcription") from exc
        texts: list[str] = []
        out_words: list[dict[str, Any]] = []
        for step in getattr(it, "steps", None) or []:
            for c in getattr(step, "content", None) or []:
                if getattr(c, "type", None) != "text":
                    continue
                texts.append(getattr(c, "text", "") or "")
                for a in getattr(c, "annotations", None) or []:
                    if getattr(a, "type", None) == "word_info":
                        out_words.append(
                            {
                                "word": getattr(a, "text", "") or "",
                                "start": parse_offset(getattr(a, "start_offset", None)),
                                "end": parse_offset(getattr(a, "end_offset", None)),
                            }
                        )
        text = "".join(texts) or (getattr(it, "output_text", None) or "")
        return text.strip(), out_words

    async def transcribe(self, audio: Path, words: bool = False) -> Transcript:
        data = await asyncio.to_thread(audio.read_bytes)
        try:
            secs = audio_info_duration(data)
        except Exception:
            secs = 0.0
        text, out_words = await self._run(data, mime_for(audio.name), words=words, lang=None)
        if self.usage_sink is not None and secs > 0:
            self.usage_sink(self.id, self.model, "stt", self.price_units(secs))
        return Transcript(text=text, words=out_words, audio_sec=round(secs, 3))

    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None = None) -> AsyncIterator[SttEvent]:
        async def run(wav: bytes, interim: bool) -> str:
            text, _ = await self._run(wav, "audio/wav", words=False, lang=lang)
            secs = (len(wav) - 44) / BYTES_PER_SEC
            if self.usage_sink is not None and secs > 0:
                self.usage_sink(self.id, self.model, "stt", self.price_units(secs))
            return text

        async for ev in buffered_stream(pcm, run, partial_interval_s=self.partial_interval_s):
            yield ev
