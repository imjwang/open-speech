"""OpenAI / Gemini / OpenAI-compatible TTS: request shaping and error mapping, at the HTTP level through
`httpx2.MockTransport` (the SDKs use httpx2, which respx doesn't intercept)."""

from __future__ import annotations

import json

import httpx2
import numpy as np
import pytest

from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import AudioChunk, TtsProvider, TtsRequest
from open_speech.providers.errors import ProviderError
from open_speech.providers.gemini_tts import GeminiTts
from open_speech.providers.openai_compatible import OpenAICompatibleTts
from open_speech.providers.openai_tts import OpenAITts
from open_speech.providers.pcm import bytes_to_f32

from .helpers import Recorder, fixture_bytes, fixture_text

OPENAI_BASE = "https://openai.test/v1"
GEMINI_BASE = "https://gemini.test/"


async def _drain(tts: TtsProvider, req: TtsRequest) -> list[AudioChunk]:
    return [c async for c in tts.synthesize(req)]


# ------------------------------------------------------------------ OpenAI


def _openai(catalog: ModelCatalog, rec: Recorder) -> OpenAITts:
    return OpenAITts(catalog, "sk-test", http_client=rec.client(), base_url=OPENAI_BASE)


async def test_openai_tts_request_and_decode(catalog: ModelCatalog, recorder: Recorder) -> None:
    ogg = fixture_bytes("openai_speech.ogg")
    recorder.add("POST", "/audio/speech", lambda r: httpx2.Response(200, content=ogg,
                                                                      headers={"content-type": "audio/ogg"}))
    tts = _openai(catalog, recorder)
    req = TtsRequest(text="Hello there.", model="gpt-4o-mini-tts", voice="coral", style="calm and warm")
    chunks = await _drain(tts, req)
    sent = recorder.requests[0]
    assert str(sent.url) == f"{OPENAI_BASE}/audio/speech"
    assert sent.headers["authorization"] == "Bearer sk-test"
    body = json.loads(sent.content)
    assert body == {
        "model": "gpt-4o-mini-tts-2025-12-15",
        "voice": "coral",
        "input": "Hello there.",
        "response_format": "opus",
        "instructions": "calm and warm",
    }
    assert len(chunks) == 1 and chunks[0].final
    assert chunks[0].encoded == ogg and chunks[0].sample_rate == 48000
    pcm = bytes_to_f32(chunks[0].pcm)
    assert abs(pcm.size / 48000 - 0.8) < 0.01 and np.max(np.abs(pcm)) > 0.1
    units = tts.price_units(req, 0.8)
    assert units.chars == 12 and units.in_tok == 3 and units.audio_sec == 0.8


async def test_openai_tts_no_style_omits_instructions(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/audio/speech", lambda r: httpx2.Response(200, content=fixture_bytes("openai_speech.ogg")))
    await _drain(_openai(catalog, recorder), TtsRequest(text="Hi.", model="gpt-4o-mini-tts", voice="ash"))
    assert "instructions" not in recorder.json_body()


@pytest.mark.parametrize(
    ("status", "code", "retryable", "key_invalid"),
    [(429, "rate_limited", True, False), (500, "provider_error", True, False), (503, "provider_error", True, False),
     (401, "provider_error", False, True), (400, "provider_error", False, False)],
)
async def test_openai_tts_error_mapping(catalog: ModelCatalog, recorder: Recorder, status: int, code: str,
                                        retryable: bool, key_invalid: bool) -> None:
    recorder.add("POST", "/audio/speech", lambda r: httpx2.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(ProviderError) as ei:
        await _drain(_openai(catalog, recorder), TtsRequest(text="x", model="gpt-4o-mini-tts", voice="coral"))
    assert ei.value.code == code and ei.value.retryable is retryable and ei.value.key_invalid is key_invalid
    assert len(recorder.requests) == 1  # no SDK-level retries: the synthesizer owns backoff
    if key_invalid:
        assert "key was rejected" in ei.value.detail


async def test_openai_tts_network_error_is_offline(catalog: ModelCatalog) -> None:
    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    tts = OpenAITts(catalog, "sk-test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(boom)),
                    base_url=OPENAI_BASE)
    with pytest.raises(ProviderError) as ei:
        await _drain(tts, TtsRequest(text="x", model="gpt-4o-mini-tts", voice="coral"))
    assert ei.value.code == "offline" and ei.value.retryable


def test_openai_voices_and_limits(catalog: ModelCatalog) -> None:
    tts = OpenAITts(catalog, "sk-test", http_client=httpx2.AsyncClient(), base_url=OPENAI_BASE)
    vs = tts.voices()
    assert {v.id for v in vs} >= {"openai:gpt-4o-mini-tts:coral", "openai:gpt-4o-mini-tts:marin"}
    assert all(v.styles and not v.local and not v.timestamps for v in vs)
    lim = tts.limits("gpt-4o-mini-tts")
    assert lim.style and lim.max_input_chars == 4000 and "opus" in lim.formats


# ------------------------------------------------------------------ Gemini


def _gemini(catalog: ModelCatalog, rec: Recorder) -> GeminiTts:
    return GeminiTts(catalog, "AIza-test", http_client=rec.client(), base_url=GEMINI_BASE)


def _sse(name: str) -> httpx2.Response:
    return httpx2.Response(200, content=fixture_text(name).encode(), headers={"content-type": "text/event-stream"})


async def test_gemini_tts_interactions_request_and_stream(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/interactions", lambda r: _sse("gemini_tts_stream.sse"))
    tts = _gemini(catalog, recorder)
    req = TtsRequest(text="Héllo there.", model="gemini-3.8-flash-tts", voice="Kore", style="whisper softly")
    chunks = await _drain(tts, req)
    sent = recorder.requests[0]
    assert sent.url.path.endswith("/v1beta/interactions")
    assert sent.headers["x-goog-api-key"] == "AIza-test"
    body = json.loads(sent.content)
    assert body["model"] == "gemini-3.8-flash-tts"
    assert body["stream"] is True and body["store"] is False
    assert body["response_modalities"] == ["audio"]
    assert body["response_format"] == {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000}
    assert body["generation_config"] == {"speech_config": [{"voice": "Kore"}]}
    text_part = body["input"][0]["content"][0]
    assert text_part["text"] == "Héllo there."
    assert text_part["annotations"] == [
        {"type": "speech_metadata", "style": "whisper softly", "start_index": 0, "end_index": 13}  # bytes
    ]
    audio = [c for c in chunks if c.pcm]
    assert len(audio) == 2 and all(c.sample_rate == 24000 for c in audio)
    total = sum(bytes_to_f32(c.pcm).size for c in audio)
    assert abs(total / 24000 - 0.6) < 0.001
    assert chunks[-1].final
    units = tts.price_units(req, 0.6)
    assert (units.in_tok, units.out_tok, units.audio_sec) == (14, 15, 0.6)  # exact usage from the stream
    assert tts.price_units(req, 0.6).in_tok == 3  # consumed: falls back to an estimate (12 chars / 4)


async def test_gemini_tts_without_style_has_no_annotations(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/interactions", lambda r: _sse("gemini_tts_stream.sse"))
    await _drain(_gemini(catalog, recorder), TtsRequest(text="Hi.", model="gemini-3.8-flash-lite-tts", voice="Puck"))
    part = recorder.json_body()["input"][0]["content"][0]
    assert "annotations" not in part
    assert recorder.json_body()["model"] == "gemini-3.8-flash-lite-tts"


async def test_gemini_tts_stream_error_event(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/interactions", lambda r: _sse("gemini_tts_error.sse"))
    with pytest.raises(ProviderError) as ei:
        await _drain(_gemini(catalog, recorder), TtsRequest(text="x", model="gemini-3.8-flash-tts", voice="Kore"))
    assert ei.value.upstream_status == 503 and ei.value.retryable


@pytest.mark.parametrize(("status", "code"), [(429, "rate_limited"), (500, "provider_error"), (403, "provider_error")])
async def test_gemini_tts_http_errors(catalog: ModelCatalog, recorder: Recorder, status: int, code: str) -> None:
    recorder.add("POST", "/interactions", lambda r: httpx2.Response(
        status, json={"error": {"code": status, "message": "bad", "status": "X"}}))
    with pytest.raises(ProviderError) as ei:
        await _drain(_gemini(catalog, recorder), TtsRequest(text="x", model="gemini-3.8-flash-tts", voice="Kore"))
    assert ei.value.code == code
    assert len(recorder.requests) == 1


# ------------------------------------------------------------------ OpenAI-compatible (P1)


async def test_openai_compatible_server(recorder: Recorder) -> None:
    cat = ModelCatalog.from_dict({"tts": {"openai_compatible": {"kokoro-fastapi": {
        "base_url": "http://127.0.0.1:8880/v1", "api_model": "kokoro", "voices": ["af_heart"],
        "response_format": "wav"}}}})
    from open_speech.providers.pcm import tone, wav_bytes

    recorder.add("POST", "/audio/speech", lambda r: httpx2.Response(200, content=wav_bytes(tone(200, 0.5, 24000), 24000)))
    tts = OpenAICompatibleTts(cat, http_client=recorder.client())
    assert [v.id for v in tts.voices()] == ["openai_compatible:kokoro-fastapi:af_heart"]
    chunks = await _drain(tts, TtsRequest(text="Hi.", model="kokoro-fastapi", voice="af_heart"))
    assert str(recorder.requests[0].url) == "http://127.0.0.1:8880/v1/audio/speech"
    assert recorder.json_body() == {"model": "kokoro", "voice": "af_heart", "input": "Hi.", "response_format": "wav"}
    assert chunks[0].sample_rate == 24000 and abs(bytes_to_f32(chunks[0].pcm).size - 12000) <= 1
