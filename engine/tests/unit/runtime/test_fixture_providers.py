from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np

from open_speech.document.model import Block, BlockType
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import (
    LlmProvider,
    RuleBrief,
    SttProvider,
    TtsProvider,
    TtsRequest,
    VisionProvider,
)
from open_speech.providers.fixture import FixtureLlm, FixtureStt, FixtureTts, FixtureVision
from open_speech.providers.pcm import bytes_to_f32, wav_bytes


async def _collect(tts: FixtureTts, req: TtsRequest) -> tuple[np.ndarray, list]:  # type: ignore[type-arg]
    pcm = []
    words = []
    async for c in tts.synthesize(req):
        pcm.append(bytes_to_f32(c.pcm))
        words += c.words
    return np.concatenate(pcm), words


async def test_fixture_tts_is_deterministic_with_exact_timings(catalog: ModelCatalog) -> None:
    tts = FixtureTts(catalog)
    assert isinstance(tts, TtsProvider)
    text = "Reading is wonderful, isn't it? Yes."
    req = TtsRequest(text=text, model="tone-v1", voice="alto")
    a, wa = await _collect(tts, req)
    b, wb = await _collect(tts, req)
    assert np.array_equal(a, b) and wa == wb
    assert [text[w.start : w.end] for w in wa] == ["Reading", "is", "wonderful", "isn't", "it", "Yes"]
    sr = FixtureTts.SAMPLE_RATE
    # exact: energy inside each word span, silence in the gaps
    for w in wa:
        seg = a[int(w.t0 * sr) : int(w.t1 * sr)]
        assert np.sqrt(np.mean(seg**2)) > 0.05
    syl = {"Reading": 2, "is": 1, "wonderful": 3}
    for w in wa[:3]:
        assert abs((w.t1 - w.t0) - 0.18 * syl[text[w.start : w.end]]) < 0.002
    gap = a[int(wa[0].t1 * sr) + 10 : int(wa[1].t0 * sr) - 10]
    assert np.max(np.abs(gap)) < 1e-6
    # longer pause after the question mark than between plain words
    assert (wa[5].t0 - wa[4].t1) > (wa[1].t0 - wa[0].t1)
    other, _ = await _collect(tts, TtsRequest(text=text, model="tone-v1", voice="bass"))
    assert not np.array_equal(a, other)
    assert tts.price_units(req, 2.0).chars == len(text)


async def test_fixture_tts_lists_labelled_voices_and_fails_on_demand(catalog: ModelCatalog) -> None:
    tts = FixtureTts(catalog)
    voices = {v.id: v for v in tts.voices()}
    assert set(voices) == {"fixture:tone-v1:alto", "fixture:tone-v1:bass", "fixture:tone-paid-v1:soprano"}
    assert all("Test tone voice" in v.name for v in voices.values())
    assert voices["fixture:tone-v1:alto"].local and not voices["fixture:tone-paid-v1:soprano"].local
    FixtureTts.fail_next(503)
    try:
        await _collect(tts, TtsRequest(text="x", model="tone-v1", voice="alto"))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 503
    else:  # pragma: no cover
        raise AssertionError
    await _collect(tts, TtsRequest(text="x", model="tone-v1", voice="alto"))
    assert FixtureTts.calls == 2


async def _pcm_frames(seconds: float, frame_ms: int = 100) -> AsyncIterator[bytes]:
    n = int(seconds * 1000 / frame_ms)
    for _ in range(n):
        yield b"\x00\x00" * (16 * frame_ms)


async def test_fixture_stt_streams_partials_then_final() -> None:
    stt = FixtureStt()
    assert isinstance(stt, SttProvider)
    billed: list[float] = []
    stt.usage_sink = lambda p, m, op, u: billed.append(u.audio_sec)
    FixtureStt.enqueue("skip the footnotes please")
    events = [e async for e in stt.stream(_pcm_frames(0.6))]
    kinds = [e.kind for e in events]
    assert kinds[-1] == "final" and kinds.count("final") == 1
    partials = [e.text for e in events if e.kind == "partial"]
    assert partials == ["skip", "skip the", "skip the footnotes", "skip the footnotes please"]
    assert events[-1].text == "skip the footnotes please"
    assert billed == [0.6]
    # empty queue → empty final
    events = [e async for e in stt.stream(_pcm_frames(0.2))]
    assert [(e.kind, e.text) for e in events] == [("final", "")]


async def test_fixture_stt_transcribe(tmp_path: Path) -> None:
    p = tmp_path / "rec.wav"
    p.write_bytes(wav_bytes(np.zeros(16000, dtype=np.float32), 16000))
    FixtureStt.enqueue("read my clipboard")
    t = await FixtureStt().transcribe(p, words=True)
    assert t.text == "read my clipboard" and t.audio_sec == 1.0 and len(t.words) == 3


def _block(i: int, text: str, type_: BlockType = BlockType.paragraph) -> Block:
    return Block(id=f"blk_{i}", type=type_, text=text, order=i)


async def test_fixture_llm_annotate_quote_ops() -> None:
    llm = FixtureLlm()
    assert isinstance(llm, LlmProvider)
    text = (
        "Prior work (Smith et al., 2020) and (Lee & Park, 2019; Chen, 2021) agree. "
        "Smith (2020) showed it (Smith et al., 2020)."
    )
    blocks = [_block(1, text)]
    rules = [RuleBrief(id="rule_cite", name="Citations", instruction="Skip author-year citations")]
    call = await llm.annotate(blocks, rules, "Read numbers naturally.")
    quotes = [(o.quote, o.occurrence) for o in call.output]
    assert (" (Smith et al., 2020)", 1) in quotes and (" (Smith et al., 2020)", 2) in quotes
    assert (" (Lee & Park, 2019; Chen, 2021)", 1) in quotes
    assert (" (2020)", 1) in quotes  # narrative citation keeps the author's name
    for op in call.output:
        assert op.op == "skip" and op.block_id == "blk_1" and op.rule_id == "rule_cite"
        # the quote + occurrence locate exactly one span
        idx = -1
        for _ in range(op.occurrence):
            idx = text.find(op.quote, idx + 1)
        assert idx >= 0
    assert call.units.in_tok > 900 and call.units.out_tok > 40 and call.model == "fixture-llm"
    none = await llm.annotate(blocks, [RuleBrief(id="r", name="Tables", instruction="skip tables")], "")
    assert none.output == []


async def test_fixture_llm_compile_and_transform() -> None:
    llm = FixtureLlm()
    c = await llm.compile_rule("Stop reading the footnotes on this site", {"site": "arxiv.org"})
    d = c.output
    assert d.tier == "filter" and d.compiled["drop_blocks"] == ["footnote"]
    assert d.compiled["drop_marks"] == ["footnote_ref"] and d.scope_suggestion == {"sites": ["arxiv.org"]}
    assert (await llm.compile_rule("Summarize each section in two sentences", {})).output.tier == "transform"
    assert (await llm.compile_rule("skip citations unless the author is the subject", {})).output.tier == "annotate"
    t = await llm.transform([_block(1, "First one. Second one."), _block(2, "Only sentence")], "summarize", "")
    assert t.output.utterances == ["First one.", "Only sentence"]
    assert t.output.anchor_block_ids == ["blk_1", "blk_2"]


async def test_fixture_vision() -> None:
    v = FixtureVision()
    assert isinstance(v, VisionProvider)
    d = await v.describe_or_transcribe(b"png", "image/png", [], "auto")
    assert d.output[0].text.startswith("A test image showing") and d.units.images == 1
    t = await v.describe_or_transcribe(b"png", "image/png", [], "transcribe")
    assert t.output[0].type == "paragraph"
