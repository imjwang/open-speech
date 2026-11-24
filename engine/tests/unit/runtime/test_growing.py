"""Growing utterances (a streaming local voice, Qwen3): the renderer plays an utterance while it is still being
synthesized, waits (with faded edges) when synthesis falls behind, and ends it only when it is complete; the
transport starts it once the audio already there covers the projected shortfall, tints the sentence until its word
timings exist, then highlights words; a jump stops generations nobody will hear."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import numpy as np
import pytest

from open_speech.config import Paths
from open_speech.document.model import Anchor
from open_speech.local.catalog import ModelCatalog
from open_speech.plan.script import SpeechScript, TextSegment, Utterance
from open_speech.playback import metrics as m
from open_speech.playback.output import NullOutput
from open_speech.playback.render import GrowingPcm, Renderer, RenderItem
from open_speech.playback.selftest import synthetic_voice
from open_speech.playback.transport import Transport, _Prepared
from open_speech.providers import qwen3_worker as qw
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units
from open_speech.providers.qwen3 import InProcessBackend, Qwen3Tts
from open_speech.providers.registry import ProviderFactories
from open_speech.service.events import EventBus
from open_speech.store.db import Database

from .helpers import SettingsBox
from .test_qwen3 import SMALL, install
from .test_registry_synth import build

SR = 24000


async def until(pred: Any, timeout: float, step: float = 0.01) -> None:
    for _ in range(int(timeout / step)):
        if pred():
            return
        await asyncio.sleep(step)


def pull_all(r: Renderer, n_blocks: int, block: int = 480) -> np.ndarray:
    out = []
    for _ in range(n_blocks):
        time.sleep(0.0005)
        out.append(r.pull(block))
    return np.concatenate(out)


def test_renderer_plays_a_growing_utterance_and_waits_with_faded_edges() -> None:
    r = Renderer(SR, rate=SR)
    r.start()
    r.pause_reading(False)
    voice = synthetic_voice(3.0)
    g = GrowingPcm(on_change=r.notify)
    g.append(voice[: SR])  # the first second
    r.enqueue(RenderItem(0, g))
    time.sleep(0.05)  # let the render thread start on it
    first = pull_all(r, 70)  # 1.4 s pulled: plays the second it has, then waits mid-utterance
    assert r.diagnostics.synthesis_waits == 1 and r.diagnostics.render_starvations == 0
    assert not r.snapshot().finished and r.queued_indices() == [0]
    g.append(voice[SR:])
    g.finish()
    r.mark_end_of_stream()
    rest = pull_all(r, 160)
    r.close()
    audio = np.concatenate([first, rest])
    played = audio[np.abs(audio) > 0]
    # everything was played, in order, once
    assert played.size == pytest.approx(np.count_nonzero(voice), abs=SR * 0.02)
    assert m.hard_edges(audio, SR) == 0  # the wait faded out and back in
    assert r.snapshot().finished


def test_a_growing_item_does_not_spin_the_render_thread() -> None:
    r = Renderer(SR, rate=SR)
    r.start()
    r.pause_reading(False)
    g = GrowingPcm(on_change=r.notify)
    r.enqueue(RenderItem(0, g))
    t0 = time.process_time()
    time.sleep(0.3)
    assert time.process_time() - t0 < 0.15  # waiting, not busy-looping
    g.append(np.zeros(100, np.float32))  # less than a worthwhile piece: still waiting
    assert r.buffered_frames() == 0
    g.finish()
    time.sleep(0.05)
    assert r.rendering_done()
    r.close()


# ------------------------------------------------------------------ transport


class StreamingVoice:
    """A stand-in for Qwen3 in the transport tests: pieces arrive on a schedule."""

    id = "qwen3"
    timestamps = False
    max_concurrency = 1
    streams = True
    cancel_on_seek = True
    falls_back = True

    def __init__(self, *, piece_s: float = 0.5, delay_s: float = 0.0, rtf: float = 2.0) -> None:
        self.piece_s, self.delay_s, self.rtf = piece_s, delay_s, rtf
        self.started: list[str] = []
        self.cancelled: list[str] = []

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        self.started.append(req.text)
        audio = synthetic_voice(0.065 * len(req.text))
        step = int(self.piece_s * SR)
        try:
            for a in range(0, audio.size, step):
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                yield AudioChunk(pcm=audio[a : a + step].astype("<f4").tobytes(), sample_rate=SR)
        except BaseException:
            self.cancelled.append(req.text)
            raise

    def limits(self, _model: str) -> TtsLimits:
        return TtsLimits(formats=["pcm"])

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), audio_sec=audio_sec)

    def realtime_factor(self, _model: str = "") -> float:
        return self.rtf

    def rtf_measured(self, _model: str = "") -> bool:
        return True

    def smallest(self, _model: str = "") -> bool:
        return False


def script(*texts: str) -> SpeechScript:
    utts = []
    for i, text in enumerate(texts):
        bid = f"b{i}"
        utts.append(Utterance(id=f"utt_{i}", index=i, text=text, block_ids=[bid],
                              anchors=[Anchor(block_id=bid, char_start=0, char_end=len(text))],
                              segments=[TextSegment(block_id=bid, src_start=0, src_end=len(text), spoken_start=0,
                                                    spoken_end=len(text))]))
    return SpeechScript(document_id="doc", plan_key="k", mode="classic", utterances=utts)


async def qwen_transport(paths: Paths, db: Database, settings: SettingsBox, events: EventBus,
                         voice: StreamingVoice, time_scale: float = 1.0) -> Transport:
    def make(catalog: ModelCatalog, p: Paths, **_kw: Any) -> Any:
        return voice

    rt = build(paths, db, settings, events, factories=ProviderFactories(qwen3=make))
    install(paths, rt.registry.catalog, SMALL)
    rt.registry.tts_for = lambda vid: (voice, TtsRequest(text="", model="qwen3-tts", voice=vid.split(":")[2]))  # type: ignore[method-assign]
    rt.registry.is_local = lambda vid: True  # type: ignore[method-assign]
    return Transport(rt.synth, NullOutput(time_scale=time_scale), events, settings=settings,
                     on_position=lambda *_a: None)


def test_start_threshold_covers_the_projected_shortfall() -> None:
    t = Transport.__new__(Transport)
    t._script = script("x" * 150)  # ~10 s at 15 characters a second
    t._index, t._offset_ms, t._speed, t._cps, t._cps_voice = 0, 0, 1.0, 15.0, {}
    t._voice_id, t._voice_override = "qwen3:qwen3-tts:Ryan", None
    t._prepared = {}
    t._style = None
    t.sample_rate = SR
    voice = StreamingVoice(rtf=2.0)
    t._provider_obj = lambda _v: voice  # type: ignore[method-assign]
    g = GrowingPcm()
    p = _Prepared("qwen3:qwen3-tts:Ryan", np.zeros(0, np.float32), [], 0.0, growing=g, done=False)
    g.append(np.zeros(int(0.5 * SR), np.float32))
    assert not t._ready_to_start(0, p, 0.0)  # under the 0.8 s floor
    g.append(np.zeros(int(0.4 * SR), np.float32))
    assert t._ready_to_start(0, p, 0.0)  # fast enough (2x at 1x speed): 0.8 s is plenty
    voice.rtf = 1.2
    t._speed = 2.0  # can't keep up at 2x: needs ~9.1 s x (1 - 1.2 / 2.2) of audio first
    assert not t._ready_to_start(0, p, 0.0)
    g.append(np.zeros(int(3.4 * SR), np.float32))
    assert t._ready_to_start(0, p, 0.0)
    done = _Prepared("v", np.zeros(10, np.float32), [], 0.0)
    assert t._ready_to_start(0, done, 0.0)


async def test_transport_starts_before_the_utterance_is_finished_then_highlights_words(
        paths: Paths, db: Database, settings: SettingsBox, events: EventBus,
        captured: list[dict[str, Any]]) -> None:
    voice = StreamingVoice(piece_s=0.5, delay_s=0.12)  # 0.5 s of audio every 0.12 s: ~4x real time
    t = await qwen_transport(paths, db, settings, events, voice, time_scale=2.0)
    text = "The library will be closed for renovations until the end of March, and reopens in April."
    await t.load(document_id="doc", title="T", page_count=None, script=script(text), voice_id="qwen3:qwen3-tts:Ryan",
                 style=None, speed=1.0)
    t0 = time.monotonic()
    await t.play()
    await until(lambda: t.renderer.buffered_frames() > 0, 5)
    started = time.monotonic() - t0
    total = 0.065 * len(text) / 0.5 * 0.12  # time to generate the whole utterance
    assert started < total * 0.6  # playing long before the utterance was finished
    await until(lambda: t.snapshot().state == "stopped", 12, 0.05)
    words = [e for e in captured if e.get("type") == "playback.word"]
    assert words and words[0]["spoken_start"] == 0 and words[0]["spoken_end"] == len(text)  # sentence tint first
    later = [w for w in words[1:] if w["spoken_end"] - w["spoken_start"] < len(text)]
    assert later  # then word by word, once the timings existed
    assert t.diagnostics.render_starvations == 0
    await t.close()


async def test_a_jump_cancels_generations_nobody_will_hear(paths: Paths, db: Database, settings: SettingsBox,
                                                           events: EventBus) -> None:
    voice = StreamingVoice(piece_s=0.25, delay_s=0.2)  # slow
    t = await qwen_transport(paths, db, settings, events, voice)
    texts = [f"Sentence number {i} has quite a few words in it to take a while." for i in range(6)]
    await t.load(document_id="doc", title="T", page_count=None, script=script(*texts),
                 voice_id="qwen3:qwen3-tts:Ryan", style=None, speed=1.0)
    await t.play()
    await asyncio.sleep(0.3)
    assert voice.started == [texts[0]]
    await t.seek(4)
    await asyncio.sleep(0.3)
    assert texts[0] in voice.cancelled and voice.started[-1] == texts[4]
    await t.close()


async def test_slow_voice_is_announced_once(paths: Paths, db: Database, settings: SettingsBox,
                                            events: EventBus) -> None:
    voice = StreamingVoice(piece_s=0.5, rtf=1.3)
    t = await qwen_transport(paths, db, settings, events, voice)
    said: list[str] = []
    t.speak_notice = said.append
    await t.load(document_id="doc", title="T", page_count=None, script=script("Hello there, this is a test."),
                 voice_id="qwen3:qwen3-tts:Ryan", style=None, speed=1.0)
    await t.play()
    await asyncio.sleep(0.3)
    assert said == []  # 1.3x keeps up at 1x
    await t.set_speed(2.0)
    await t.set_speed(2.5)
    assert said == ["This voice can't keep up at 2 times speed on this Mac. A smaller size or Kokoro keeps up."]
    await t.close()


async def test_real_qwen_provider_streams_into_the_transport(paths: Paths, db: Database, settings: SettingsBox,
                                                             events: EventBus) -> None:
    """End to end with the real provider (fake model in process): the utterance plays and is cached whole."""
    def make(catalog: ModelCatalog, p: Paths, *, settings: Any = None, kv: Any = None, **_kw: Any) -> Qwen3Tts:
        return Qwen3Tts(catalog, p, settings=settings, kv=kv, backend_factory=lambda: InProcessBackend(qw.FakeModel),
                        memory_gb=lambda: 16, platform_ok=lambda: True, bundled_models=p.home / "none")

    rt = build(paths, db, settings, events, factories=ProviderFactories(qwen3=make))
    install(paths, rt.registry.catalog, SMALL)
    out = NullOutput(time_scale=8.0, capture=True)
    t = Transport(rt.synth, out, events, settings=settings, on_position=lambda *_a: None)
    text = "Rivers carve their valleys slowly."
    await t.load(document_id="doc", title="T", page_count=None, script=script(text), voice_id="qwen3:qwen3-tts:Ryan",
                 style=None, speed=1.0)
    await t.play()
    await until(lambda: t.snapshot().state == "stopped", 10, 0.05)
    assert t.snapshot().state == "stopped"
    assert rt.synth.cached(text, "qwen3:qwen3-tts:Ryan") is not None
    audio = np.concatenate(out.captured)
    assert np.count_nonzero(audio) >= int(0.06 * SR * len(text) * 0.95)
    await t.close()
    await rt.registry.aclose()
