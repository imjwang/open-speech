"""Time to first audio (NFR-1): nothing the listener doesn't need runs before the first frame.

- Fresh audio reaches the player before it is encoded and cached (`Synthesizer.synth(on_audio=…)`).
- With a local voice the playhead utterance is synthesized alone; prefetch starts once it has audio.
- Opening a document starts loading the local voice in the background.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from open_speech.cache.audio import AudioCache
from open_speech.config import Paths
from open_speech.errors import NotFound
from open_speech.local.catalog import ModelCatalog
from open_speech.plan.script import SpeechScript, Utterance
from open_speech.playback.output import NullOutput
from open_speech.playback.transport import Transport
from open_speech.providers.base import WordTiming
from open_speech.providers.fixture import FixtureTts
from open_speech.service.documents import DocumentService
from open_speech.service.events import EventBus
from open_speech.store.db import Database

from .helpers import SettingsBox
from .test_kokoro import MODEL, install_fake_weights, make_kokoro
from .test_registry_synth import LOCAL, PAID, Runtime, build

TEXTS = ("First sentence of the page.", "Second sentence follows it.", "Third sentence ends it.")


@pytest.fixture
def rt(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> Runtime:
    return build(paths, db, settings, events)


@pytest.fixture
async def transport(rt: Runtime, settings: SettingsBox, events: EventBus) -> AsyncIterator[Transport]:
    t = Transport(rt.synth, NullOutput(), events, settings=settings, on_position=lambda *_a: None)
    yield t
    await t.close()


async def _load(transport: Transport, voice: str) -> None:
    utts = [Utterance(id=f"utt_{i}", index=i, text=t, block_ids=["blk_0"]) for i, t in enumerate(TEXTS)]
    script = SpeechScript(document_id="doc_x", plan_key="k", mode="classic", utterances=utts)
    await transport.load(
        document_id="doc_x", title="T", page_count=None, script=script, voice_id=voice, style=None, speed=1.0
    )


async def _until(cond: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached"
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ synthesizer hook


async def test_fresh_audio_is_handed_over_before_it_is_cached(rt: Runtime) -> None:
    key = AudioCache.key(TEXTS[0], "fixture", "tone-v1", "alto")
    seen: list[tuple[int, int, int, str, bool]] = []

    def on_audio(pcm: np.ndarray, sr: int, words: list[WordTiming], voice_id: str) -> None:
        seen.append((pcm.size, sr, len(words), voice_id, rt.cache.contains(key)))

    res = await rt.synth.synth(TEXTS[0], LOCAL, on_audio=on_audio)
    assert len(seen) == 1
    size, sr, n_words, voice_id, already_cached = seen[0]
    assert size > 0 and sr == 24000 and n_words == 5 and voice_id == LOCAL
    assert not already_cached and rt.cache.contains(key)  # the hook ran first; the cache write still happens
    assert size == res.audio.load_pcm().size  # same samples as the cached copy: word timings line up

    again = await rt.synth.synth(TEXTS[0], LOCAL, on_audio=on_audio)
    assert again.cache_hit and len(seen) == 1  # a cache hit has no fresh audio to hand over


async def test_a_failing_hook_does_not_break_synthesis(rt: Runtime) -> None:
    def boom(*_a: Any) -> None:
        raise RuntimeError("player went away")

    res = await rt.synth.synth(TEXTS[0], LOCAL, on_audio=boom)
    assert res.audio.duration_s > 0 and not res.cache_hit


# ------------------------------------------------------------------ transport


async def test_playback_starts_while_the_cache_write_is_still_running(rt: Runtime, transport: Transport) -> None:
    release = threading.Event()
    real_put = rt.cache.put

    def slow_put(*a: Any, **k: Any) -> Any:
        release.wait(5.0)  # an Opus encode that takes "forever"
        return real_put(*a, **k)

    rt.cache.put = slow_put  # type: ignore[method-assign]
    try:
        await _load(transport, LOCAL)
        await transport.play()
        # The first utterance is playing although nothing has been written to the cache yet.
        await _until(lambda: transport.snapshot().state == "playing" and transport.renderer.snapshot().offset > 0)
        assert rt.cache.count() == 0
    finally:
        release.set()
    await _until(lambda: rt.cache.count() >= 1)


async def test_local_voice_synthesizes_the_playhead_alone_then_prefetches(rt: Runtime, transport: Transport) -> None:
    FixtureTts.latency_s = 0.15
    await _load(transport, LOCAL)
    await transport.play()
    await asyncio.sleep(0.06)  # several scheduler ticks into the first synthesis
    assert FixtureTts.calls == 1, "nothing else is synthesized while the listener waits for the first audio"
    await _until(lambda: FixtureTts.calls >= 2)  # prefetch resumes as soon as the playhead has audio
    assert transport.renderer.queued_indices() or transport.renderer.snapshot().index == 0


async def test_cloud_voice_still_prefetches_in_parallel(rt: Runtime, transport: Transport) -> None:
    FixtureTts.latency_s = 0.15
    await _load(transport, PAID)
    await transport.play()
    await asyncio.sleep(0.06)
    assert FixtureTts.calls == 2  # waiting on the network, not on this machine's CPU


async def test_playhead_after_a_skip_does_not_queue_behind_prefetch(rt: Runtime, transport: Transport) -> None:
    FixtureTts.latency_s = 0.3
    await _load(transport, PAID)
    await transport.play()
    await asyncio.sleep(0.03)
    assert FixtureTts.calls == 2  # utterances 0 and 1 are in flight: the parallel cap is reached
    await transport.seek(2)
    await asyncio.sleep(0.03)
    assert FixtureTts.calls == 3  # the sentence the listener skipped to starts now, not when a slot frees up


async def test_cached_audio_still_plays(rt: Runtime, transport: Transport) -> None:
    for text in TEXTS:
        await rt.synth.synth(text, LOCAL)
    calls = FixtureTts.calls
    await _load(transport, LOCAL)
    await transport.play()
    await _until(lambda: transport.snapshot().state == "playing" and transport.renderer.snapshot().offset > 0)
    assert FixtureTts.calls == calls


# ------------------------------------------------------------------ warming the local voice


async def test_kokoro_warm_loads_the_session_and_unloads_when_idle(catalog: ModelCatalog, paths: Paths) -> None:
    install_fake_weights(paths)
    tts, _ = make_kokoro(catalog, paths, idle_unload_s=0.05)
    assert not tts.loaded()
    await tts.prewarm(MODEL, "af_heart")
    assert tts.loaded()
    await asyncio.sleep(0.12)
    assert not tts.loaded()  # warming counts as use: the idle timer still frees the memory (NFR-9)


async def test_kokoro_warm_is_quiet_when_the_voice_is_not_installed(catalog: ModelCatalog, paths: Paths) -> None:
    tts, _ = make_kokoro(catalog, paths)
    await tts.prewarm(MODEL, "af_heart")
    assert not tts.loaded()


async def test_opening_a_document_starts_warming_its_voice() -> None:
    warmed: list[tuple[str, str]] = []

    class Local:
        async def prewarm(self, model: str, voice: str) -> None:
            warmed.append((model, voice))

    class Cloud:  # no local model to load
        pass

    def tts_for(voice_id: str) -> tuple[Any, Any]:
        if voice_id == "missing":
            raise NotFound("a voice called missing")
        provider = Local() if voice_id.startswith("kokoro:") else Cloud()
        return provider, SimpleNamespace(model=voice_id.split(":")[1], voice=voice_id.split(":")[2])

    engine = SimpleNamespace(
        registry=SimpleNamespace(tts_for=tts_for),
        settings=SimpleNamespace(get=lambda: SimpleNamespace(defaults=SimpleNamespace(voice="kokoro:k-1:af_heart"))),
    )
    docs = DocumentService(engine)  # type: ignore[arg-type]
    docs._warm_voice(None)  # the default voice
    docs._warm_voice("openai:gpt-4o-mini-tts:coral")
    docs._warm_voice("missing")
    await asyncio.sleep(0.01)
    assert warmed == [("k-1", "af_heart")]
