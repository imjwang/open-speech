"""Anthropic (plan/vision) and OpenAI/Gemini STT adapters at the HTTP level (httpx2.MockTransport)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import numpy as np
import pytest

from open_speech.document.model import Block, BlockType
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.anthropic_llm import AnthropicLlm
from open_speech.providers.base import LlmProvider, RuleBrief, SttProvider, Units, VisionProvider
from open_speech.providers.errors import ProviderError
from open_speech.providers.gemini_stt import GeminiStt
from open_speech.providers.openai_stt import OpenAIStt
from open_speech.providers.pcm import wav_bytes

from .helpers import Recorder, SettingsBox, fixture_text

ANTHROPIC_BASE = "https://anthropic.test"


def _json(name: str) -> Any:
    return json.loads(fixture_text(name))


def _llm(catalog: ModelCatalog, rec: Recorder, **kw: Any) -> AnthropicLlm:
    return AnthropicLlm(catalog, "sk-ant-test", http_client=rec.client(), base_url=ANTHROPIC_BASE, max_retries=0, **kw)


BLOCKS = [
    Block(id="blk_1", type=BlockType.paragraph, text="Models (Smith et al., 2020) are e.g. small.", order=1),
    Block(id="blk_2", type=BlockType.heading, text="Methods", order=2, level=2),
]
RULES = [RuleBrief(id="rule_citations", name="Citations", instruction="Skip author-year citations.")]


async def test_annotate_request_shape(catalog: ModelCatalog, recorder: Recorder, settings: SettingsBox) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(200, json=_json("anthropic_annotate.json")))
    llm = _llm(catalog, recorder, model=lambda: settings().llm.model,
               annotate_effort=lambda: settings().llm.annotate_effort)
    assert isinstance(llm, LlmProvider) and isinstance(llm, VisionProvider)
    call = await llm.annotate(BLOCKS, RULES, "Say numbers naturally.")
    req = recorder.requests[0]
    assert str(req.url) == f"{ANTHROPIC_BASE}/v1/messages"
    auth = req.headers.get("x-api-key") or req.headers.get("authorization", "")
    assert auth.endswith("sk-ant-test")
    body = json.loads(req.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["thinking"] == {"type": "between_tools"}
    assert body["output_config"]["effort"] == "low"
    fmt = body["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    item = fmt["schema"]["properties"]["ops"]["items"]
    assert set(item["required"]) == {"block_id", "op", "quote", "occurrence", "text", "rule_id"}
    assert "tools" not in body and "tool_choice" not in body  # plan calls are tool-less (07)
    system = body["system"]
    assert [("cache_control" in s) for s in system] == [False, True, True]
    assert "quote" in system[0]["text"] and "SPEECH.md" in system[1]["text"] and "rule_citations" in system[2]["text"]
    user = body["messages"][0]["content"]
    assert user.startswith("<document_content>") and '"id": "blk_1"' in user
    # parsed output: unknown block ids dropped, empty rule_id → None
    assert [(o.block_id, o.op, o.quote, o.occurrence) for o in call.output] == [
        ("blk_1", "skip", " (Smith et al., 2020)", 1),
        ("blk_1", "replace", "e.g.", 1),
    ]
    assert call.output[0].rule_id == "rule_citations" and call.output[1].rule_id is None
    assert call.units == Units(in_tok=812, out_tok=138, cache_read_tok=1450, cache_write_tok=0)
    assert call.model == "claude-sonnet-5-5" and call.stop_reason == "end_turn"


async def test_annotate_uses_settings_model_and_effort(catalog: ModelCatalog, recorder: Recorder,
                                                       settings: SettingsBox) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(200, json=_json("anthropic_annotate.json")))
    settings.set("llm.model", "claude-opus-5-5")
    settings.set("llm.annotate_effort", "medium")
    llm = _llm(catalog, recorder, model=lambda: settings().llm.model,
               annotate_effort=lambda: settings().llm.annotate_effort)
    call = await llm.annotate(BLOCKS, RULES, "")
    body = recorder.json_body()
    assert body["model"] == "claude-opus-5-5" and body["output_config"]["effort"] == "medium"
    assert call.model == "claude-opus-5-5"


async def test_annotate_refusal_degrades_and_truncation_raises(catalog: ModelCatalog, recorder: Recorder) -> None:
    responses = iter([_json("anthropic_refusal.json"), _json("anthropic_truncated.json")])
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(200, json=next(responses)))
    llm = _llm(catalog, recorder)
    refused = await llm.annotate(BLOCKS, RULES, "")
    assert refused.output == [] and refused.stop_reason == "refusal" and refused.units.in_tok == 812
    with pytest.raises(ProviderError) as ei:
        await llm.annotate(BLOCKS, RULES, "")
    assert "cut off" in ei.value.detail
    assert ei.value.llm_call.units.out_tok == 138  # type: ignore[attr-defined]  # billable even on failure


async def test_transform_streams_with_adaptive_thinking(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(
        200, content=fixture_text("anthropic_transform.sse").encode(), headers={"content-type": "text/event-stream"}))
    call = await _llm(catalog, recorder).transform(BLOCKS, "Summarize in two sentences.", "Be brief.")
    body = recorder.json_body()
    assert body["stream"] is True
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["schema"]["required"] == ["utterances", "anchor_block_ids"]
    assert "tools" not in body
    assert call.output.utterances == ["The methods section compares two models.", "Both are small."]
    assert call.output.anchor_block_ids == ["blk_1"]  # unknown ids dropped
    assert call.units.in_tok == 900 and call.units.out_tok == 210 and call.units.cache_write_tok == 300


async def test_compile_rule(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(200, json=_json("anthropic_compile.json")))
    call = await _llm(catalog, recorder).compile_rule(
        "stop reading the footnotes on this site", {"site": "arxiv.org", "doc_kind": "pdf", "doc_id": None})
    body = recorder.json_body()
    assert body["thinking"] == {"type": "between_tools"} and body["output_config"]["effort"] == "low"
    assert "arxiv.org" in body["messages"][0]["content"] and "doc_id" not in body["messages"][0]["content"]
    d = call.output
    assert d.name == "Skip footnotes" and d.tier == "filter"
    assert d.compiled == {"drop_blocks": ["footnote"], "drop_marks": ["footnote_ref"], "replace": [],
                          "keep_blocks_only": None}
    assert d.scope_suggestion == {"sites": ["arxiv.org"]}


async def test_vision(catalog: ModelCatalog, recorder: Recorder) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(200, json=_json("anthropic_vision.json")))
    call = await _llm(catalog, recorder).describe_or_transcribe(b"\x89PNGfake", "image/png", RULES, "describe")
    body = recorder.json_body()
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[1]["text"] == "mode: describe"
    assert "tools" not in body
    assert [b.text for b in call.output] == ["A bar chart comparing three quarters of revenue."]
    assert call.units.images == 1 and call.units.in_tok == 1250


@pytest.mark.parametrize(("status", "code"), [(429, "rate_limited"), (529, "provider_error"), (401, "provider_error")])
async def test_anthropic_errors(catalog: ModelCatalog, recorder: Recorder, status: int, code: str) -> None:
    recorder.add("POST", "/v1/messages", lambda r: httpx2.Response(
        status, json={"type": "error", "error": {"type": "x", "message": "no"}}))
    with pytest.raises(ProviderError) as ei:
        await _llm(catalog, recorder).annotate(BLOCKS, RULES, "")
    assert ei.value.code == code


# ------------------------------------------------------------------ STT


async def _frames(seconds: float) -> AsyncIterator[bytes]:
    for _ in range(int(seconds * 10)):
        yield b"\x01\x00" * 1600  # 100 ms of 16 kHz PCM16


def _wav(tmp_path: Path, seconds: float = 1.2) -> Path:
    p = tmp_path / "speech.wav"
    p.write_bytes(wav_bytes(np.zeros(int(16000 * seconds), dtype=np.float32), 16000))
    return p


async def test_openai_stt_transcribe_and_words(catalog: ModelCatalog, recorder: Recorder, tmp_path: Path) -> None:
    def handler(r: httpx2.Request) -> httpx2.Response:
        body = r.content.decode("latin-1")
        name = "openai_transcription_verbose.json" if "verbose_json" in body else "openai_transcription.json"
        return httpx2.Response(200, json=_json(name))

    recorder.add("POST", "/audio/transcriptions", handler)
    billed: list[tuple[str, str, float]] = []
    stt = OpenAIStt(catalog, "sk-test", http_client=recorder.client(), base_url="https://openai.test/v1",
                    max_retries=0, usage_sink=lambda p, m, op, u: billed.append((p, m, u.audio_sec)))
    assert isinstance(stt, SttProvider)
    t = await stt.transcribe(_wav(tmp_path))
    body = recorder.requests[0].content.decode("latin-1")
    assert 'name="model"' in body and "gpt-4o-transcribe" in body and 'filename="speech.wav"' in body
    assert t.text == "read my clipboard" and t.audio_sec == 1.2
    tw = await stt.transcribe(_wav(tmp_path), words=True)
    body = recorder.requests[1].content.decode("latin-1")
    assert "whisper-1" in body and "verbose_json" in body and "word" in body
    assert [w["word"] for w in tw.words] == ["read", "my", "clipboard"]
    assert billed == [("openai", "gpt-4o-transcribe", 1.2), ("openai", "whisper-1", 1.2)]


async def test_openai_stt_stream_buffers_then_final(catalog: ModelCatalog, recorder: Recorder) -> None:
    transcription = _json("openai_transcription.json")
    recorder.add("POST", "/audio/transcriptions", lambda r: httpx2.Response(200, json=transcription))
    stt = OpenAIStt(catalog, "sk-test", http_client=recorder.client(), base_url="https://openai.test/v1", max_retries=0)
    events = [e async for e in stt.stream(_frames(1.0))]
    assert [(e.kind, e.text) for e in events] == [("final", "read my clipboard")]
    assert len(recorder.requests) == 1
    # interim partials by re-transcribing the growing buffer with the mini model
    stt.partial_interval_s = 0.4
    events = [e async for e in stt.stream(_frames(1.0))]
    assert [e.kind for e in events] == ["partial", "final"]  # identical interim texts are deduplicated
    interim_bodies = [r.content.decode("latin-1") for r in recorder.requests[1:-1]]
    assert interim_bodies and all("gpt-4o-mini-transcribe" in b for b in interim_bodies)
    # too little audio → empty final, no API call
    n = len(recorder.requests)
    events = [e async for e in stt.stream(_frames(0.1))]
    assert [(e.kind, e.text) for e in events] == [("final", "")] and len(recorder.requests) == n


async def test_gemini_stt_transcribe(catalog: ModelCatalog, recorder: Recorder, tmp_path: Path) -> None:
    recorder.add("POST", "/interactions", lambda r: httpx2.Response(200, json=_json("gemini_transcribe.json")))
    stt = GeminiStt(
        catalog, "AIza-test", http_client=recorder.client(), base_url="https://gemini.test/", retry_attempts=1
    )
    t = await stt.transcribe(_wav(tmp_path), words=True)
    body = recorder.json_body()
    assert body["model"] == "gemini-3.5-transcribe" and body["store"] is False
    assert body["generation_config"]["transcription_config"] == {
        "mode": {"type": "verbatim"},
        "timestamp_granularities": ["word"],
    }
    assert body["input"][0]["content"][0]["mime_type"] == "audio/wav"
    assert t.text == "skip the footnotes"
    assert t.words[2] == {"word": "footnotes", "start": 0.54, "end": 1.1}
    events = [e async for e in stt.stream(_frames(0.5), lang="en-US")]
    assert events[-1].text == "skip the footnotes"
    assert recorder.json_body()["generation_config"]["transcription_config"]["language_codes"] == ["en-US"]
