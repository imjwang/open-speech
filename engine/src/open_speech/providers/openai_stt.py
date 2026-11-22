"""OpenAI speech-to-text (07 § STT routing, order 1).

- `transcribe(path)`: batch `audio.transcriptions` with the catalog's first batch model
  (`gpt-4o-transcribe`); with `words=True` it uses the catalog's word-timestamp model (`whisper-1`,
  `verbose_json`, word granularity).
- `stream(pcm)`: buffers the mic PCM and transcribes when the mic stops; optional interim partials by
  re-transcribing the growing buffer with the cheaper batch model (see `stt_common`).
- Every billable call is reported to `usage_sink(provider, model, "stt", Units(audio_sec=...))` when set.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import SttEvent, Transcript, Units
from open_speech.providers.errors import map_provider_error
from open_speech.providers.pcm import audio_info_duration
from open_speech.providers.stt_common import BYTES_PER_SEC, UsageSink, buffered_stream


class OpenAIStt:
    id = "openai"

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
        timeout_s: float = 30.0,
        max_retries: int = 2,
    ) -> None:
        entry = catalog.stt_for(self.id)
        self._batch = list(entry.batch) if entry else ["gpt-4o-transcribe"]
        self._words_models = list(entry.word_timestamps) if entry else ["whisper-1"]
        self.partial_interval_s = partial_interval_s
        self.usage_sink = usage_sink
        if client is None:
            import openai

            client = openai.AsyncOpenAI(
                api_key=api_key, timeout=timeout_s, max_retries=max_retries, http_client=http_client, base_url=base_url
            )
        self._client = client

    @property
    def model(self) -> str:
        return self._batch[0]

    @property
    def interim_model(self) -> str:
        mini = [m for m in self._batch if "mini" in m]
        return mini[0] if mini else self._batch[0]

    def price_units(self, audio_sec: float) -> Units:
        return Units(audio_sec=round(audio_sec, 3))

    def _bill(self, model: str, audio_sec: float) -> None:
        if self.usage_sink is not None and audio_sec > 0:
            self.usage_sink(self.id, model, "stt", self.price_units(audio_sec))

    async def _transcribe_bytes(
        self, data: bytes, filename: str, *, model: str, words: bool = False, lang: str | None = None
    ) -> Any:
        params: dict[str, Any] = {"file": (filename, data), "model": model}
        if words:
            params["response_format"] = "verbose_json"
            params["timestamp_granularities"] = ["word"]
        else:
            params["response_format"] = "json"
        if lang:
            params["language"] = lang
        try:
            return await self._client.audio.transcriptions.create(**params)
        except Exception as exc:
            raise map_provider_error(self.id, exc, feature="transcription") from exc

    async def transcribe(self, audio: Path, words: bool = False) -> Transcript:
        data = await asyncio.to_thread(audio.read_bytes)
        try:
            secs = audio_info_duration(data)
        except Exception:
            secs = 0.0
        model = self._words_models[0] if words and self._words_models else self.model
        resp = await self._transcribe_bytes(data, audio.name, model=model, words=words)
        self._bill(model, secs)
        text = resp if isinstance(resp, str) else (getattr(resp, "text", "") or "")
        out_words: list[dict[str, Any]] = []
        for w in getattr(resp, "words", None) or []:
            out_words.append(
                {
                    "word": getattr(w, "word", ""),
                    "start": float(getattr(w, "start", 0.0) or 0.0),
                    "end": float(getattr(w, "end", 0.0) or 0.0),
                }
            )
        return Transcript(text=text.strip(), words=out_words, audio_sec=round(secs, 3))

    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None = None) -> AsyncIterator[SttEvent]:
        async def run(wav: bytes, interim: bool) -> str:
            model = self.interim_model if interim else self.model
            resp = await self._transcribe_bytes(wav, "speech.wav", model=model, lang=lang)
            self._bill(model, (len(wav) - 44) / BYTES_PER_SEC)
            return resp if isinstance(resp, str) else (getattr(resp, "text", "") or "")

        async for ev in buffered_stream(pcm, run, partial_interval_s=self.partial_interval_s):
            yield ev
