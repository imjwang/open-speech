"""Regressions from the playback review of growing utterances and local fallbacks: a streaming voice failing part-way
never leaves the renderer waiting forever; a growing utterance outrun by the playhead reports "loading"; a failing
local voice reads on with the local default whatever the cloud fallback policy says; a fallback adopted inside a
synthesis task never cancels that task, and a finished task never unregisters a newer one for the same key."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.catalog import ModelCatalog
from open_speech.providers import qwen3_worker as qw
from open_speech.providers.base import AudioChunk, TtsRequest
from open_speech.providers.qwen3 import InProcessBackend, Qwen3Tts
from open_speech.providers.registry import FIXTURE_VOICE, ProviderFactories
from open_speech.service.events import EventBus
from open_speech.store.db import Database

from .helpers import SettingsBox
from .test_growing import SR, StreamingVoice, qwen_transport, script, until
from .test_qwen3 import SMALL, install
from .test_registry_synth import build

VOICE = "qwen3:qwen3-tts:Ryan"


class FailingVoice(StreamingVoice):
    """Streams `good` pieces, then fails; no fallback (like a voice whose fallback is impossible)."""

    falls_back = False

    def __init__(self, good: int = 3, **kw: Any) -> None:
        super().__init__(**kw)
        self.good = good

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        n = 0
        async for chunk in super().synthesize(req):
            if n == self.good:
                raise EngineError("provider_error", "The test voice broke.", provider="qwen3")
            n += 1
            yield chunk


class StallingVoice(StreamingVoice):
    """Two quick pieces, then a long pause, then the rest."""

    def __init__(self, stall_s: float, **kw: Any) -> None:
        super().__init__(**kw)
        self.stall_s = stall_s

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        n = 0
        async for chunk in super().synthesize(req):
            if n == 2:
                await asyncio.sleep(self.stall_s)
            n += 1
            yield chunk


async def test_a_streaming_failure_part_way_is_reported_not_left_waiting(
        paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> None:
    voice = FailingVoice(good=3, piece_s=0.5, delay_s=0.02)
    t = await qwen_transport(paths, db, settings, events, voice, time_scale=4.0)
    text = "This sentence is long enough to need several pieces before the voice gives up on it entirely."
    await t.load(document_id="doc", title="T", page_count=None, script=script(text), voice_id=VOICE, style=None,
                 speed=1.0)
    await t.play()
    await until(lambda: t.snapshot().state == "error", 5)
    snap = t.snapshot()
    assert snap.state == "error" and snap.error == "The test voice broke."
    key = t._key(0)
    assert key not in t._prepared and key in t._failed  # forgotten, so a retry makes it again
    r = t.renderer
    fed = [i for i in [r._cur, *r._queue] if i is not None]
    assert all(getattr(i.pcm, "complete", True) for i in fed)  # what was fed has an end: nothing waits on it
    assert not r.waiting_for_synthesis()
    voice.good = 1000  # the voice works again: play retries the sentence from the start
    await t.play()
    await until(lambda: t.snapshot().state == "stopped", 10)
    assert t.snapshot().state == "stopped" and t._prepared[key].done
    await t.close()


async def test_an_outrun_growing_utterance_reports_loading(paths: Paths, db: Database, settings: SettingsBox,
                                                           events: EventBus) -> None:
    voice = StallingVoice(stall_s=3.0, piece_s=0.5, rtf=10.0)  # "fast" by its measured speed, then it stalls
    t = await qwen_transport(paths, db, settings, events, voice, time_scale=4.0)
    text = "A sentence that will run out of audio halfway through, while its voice catches its breath for a while."
    await t.load(document_id="doc", title="T", page_count=None, script=script(text), voice_id=VOICE, style=None,
                 speed=1.0)
    await t.play()
    await until(lambda: t.renderer.waiting_for_synthesis(), 5)
    assert t.renderer.waiting_for_synthesis() and t.renderer.queued_indices() == [0]  # the utterance is still `_cur`
    await until(lambda: t.snapshot().state == "loading", 1)
    assert t.snapshot().state == "loading"  # not "playing" over silence
    await until(lambda: t.snapshot().state == "playing", 5)
    assert t.snapshot().state in ("playing", "stopped")  # audio came back
    await t.close()


@pytest.mark.parametrize("policy", ["same_provider", "another_voice", "kokoro"])
async def test_a_failing_local_voice_reads_on_locally_whatever_the_policy(
        paths: Paths, db: Database, settings: SettingsBox, events: EventBus, policy: str) -> None:
    def make_qwen(catalog: ModelCatalog, p: Paths, *, settings: Any = None, kv: Any = None, **_kw: Any) -> Qwen3Tts:
        return Qwen3Tts(catalog, p, settings=settings, backend_factory=lambda: InProcessBackend(qw.FakeModel),
                        memory_gb=lambda: 16, platform_ok=lambda: True, bundled_models=p.home / "none")

    settings.set("playback.fallback", policy)
    rt = build(paths, db, settings, events, factories=ProviderFactories(qwen3=make_qwen))
    install(paths, rt.registry.catalog, SMALL)
    assert any(v.id == "fixture:tone-paid-v1:soprano" and not v.local for v in rt.registry.voices())  # a paid one
    res = await rt.synth.synth("__error__ in the model", VOICE)
    assert res.voice_id == FIXTURE_VOICE and res.notice_reason == "failure"  # local, never the paid cloud voice
    await rt.registry.aclose()


async def test_a_fallback_adopted_inside_its_task_does_not_cancel_it(paths: Paths, db: Database,
                                                                    settings: SettingsBox, events: EventBus) -> None:
    voice = StreamingVoice()
    t = await qwen_transport(paths, db, settings, events, voice)
    texts = [f"Utterance {i} of the test script." for i in range(6)]
    await t.load(document_id="doc", title="T", page_count=None, script=script(*texts), voice_id=VOICE, style=None,
                 speed=1.0)
    started = asyncio.Event()
    release = asyncio.Event()

    async def synthesis_task() -> str:
        started.set()
        await release.wait()
        t._cancel_unneeded(5)  # what `_adopt` → `_replace_fed` → `_reposition` does, from inside the task
        await asyncio.sleep(0)
        return "finished"

    key2 = t._key(2)
    me = asyncio.create_task(synthesis_task())
    t._tasks[key2] = me
    await started.wait()
    sleeper = asyncio.create_task(asyncio.sleep(30))
    t._tasks[t._key(1)] = sleeper  # another utterance outside the kept range: cancelled
    release.set()
    assert await me == "finished"  # its own task wasn't cancelled (index 2 is outside 5..7, yet it's running)
    await asyncio.sleep(0)
    assert sleeper.cancelled()
    # the fed range is kept: from the playhead, every utterance `_feed` may hand over
    t._tasks.clear()
    keepers = {i: asyncio.create_task(asyncio.sleep(30)) for i in range(6)}
    for i, task in keepers.items():
        t._tasks[t._key(i)] = task
    t._cancel_unneeded(1)
    await asyncio.sleep(0)
    assert [i for i, task in keepers.items() if task.cancelled()] == [0, 4, 5]
    for task in keepers.values():
        task.cancel()
    await t.close()


async def test_a_finished_task_never_unregisters_a_newer_one(paths: Paths, db: Database, settings: SettingsBox,
                                                            events: EventBus) -> None:
    gate = asyncio.Event()

    class Gated(StreamingVoice):
        async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
            await gate.wait()
            async for chunk in super().synthesize(req):
                yield chunk

    t = await qwen_transport(paths, db, settings, events, Gated())
    await t.load(document_id="doc", title="T", page_count=None, script=script("Only one sentence here."),
                 voice_id=VOICE, style=None, speed=1.0)
    key = t._key(0)
    first = asyncio.create_task(t._prepare(0, key))
    t._tasks[key] = first
    await asyncio.sleep(0.01)
    newer = asyncio.create_task(asyncio.sleep(30))
    t._tasks[key] = newer  # a newer synthesis for the same utterance was started meanwhile
    gate.set()
    await first
    assert t._tasks.get(key) is newer
    newer.cancel()
    assert t._prepared[key].done and t._prepared[key].pcm.size > 0.5 * SR
    await t.close()
