"""Every reply spoken, sentence by sentence (service/reply_speech.py, 07 § Reply speech), and "auto" waiting for
the first Google check right after startup (service/misc.py `VoiceService.settle_auto`).

A real engine with the fixture voices behind a `NullOutput` that plays 8 s of audio per second: nothing is heard,
but the transport's clock, fades and `assistant.speech` events run as they do on a Mac."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_speech.api import schemas as s
from open_speech.errors import EngineError
from open_speech.playback.output import NullOutput
from open_speech.providers.fixture import FixtureTts
from open_speech.providers.pcm import read_audio_file

from .helpers import make_env

VOICE = "fixture:tone-v1:alto"
SCALE = 8.0  # NullOutput plays this many seconds of audio per second


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[Any]:
    from open_speech.service.engine import Engine

    e = Engine(
        make_env(open_speech_home=tmp_path / "home", open_speech_fixture_providers=True,
                 open_speech_audio_output="null"),
        output=NullOutput(time_scale=SCALE),
    )
    try:
        yield e
    finally:
        await e.shutdown()


def capture(engine: Any) -> list[tuple[float, dict[str, Any]]]:
    out: list[tuple[float, dict[str, Any]]] = []
    loop = asyncio.get_running_loop()
    engine.events.add_listener(lambda m: out.append((loop.time(), m)))
    return out


def speech(events: list[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [m for _, m in events if m.get("type") == "assistant.speech"]


def rendered(text: str) -> np.ndarray:
    return FixtureTts.render(text, "alto")[0]


async def until(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.005)


# ------------------------------------------------------------------ streaming, order, the recording


async def test_first_audio_follows_the_first_sentence_while_the_rest_is_written(engine: Any) -> None:
    events = capture(engine)
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("Here is the first sentence of the reply. And the sec")
    await until(lambda: stream.first_audio_at is not None)  # before the second sentence even exists
    await until(lambda: any(m["state"] == "started" for m in speech(events)))
    stream.feed("ond one follows right here.")
    assert stream.will_speak()
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 10) == "finished"
    ev = speech(events)
    assert [m["state"] for m in ev] == ["started", "ended"]
    assert all(m["turn_id"] == "turn_1" and m["kind"] == "reply" for m in ev)
    assert ev[1]["reason"] == "finished" and ev[1]["recording_id"] == stream.recording_id


async def test_order_is_kept_when_a_later_sentence_is_ready_first(engine: Any) -> None:
    texts = [
        "One sentence that takes a while to make.",
        "Two is a sentence that is quick to make.",
        "Three is quick as well, and it comes last.",
    ]
    original = engine.synth.synth
    ready: list[str] = []

    async def slow_first(text: str, voice: str, **kw: Any) -> Any:
        if text.startswith("One"):
            await asyncio.sleep(0.4)
        res = await original(text, voice, **kw)
        ready.append(text[:3])
        return res

    engine.synth.synth = slow_first
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed(" ".join(texts))
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 10) == "finished"
    assert ready[0] == "Two"  # synthesized ahead, in parallel (the fixture voice allows two at once)
    assert [p.size for p in stream._parts] == [rendered(t).size for t in texts]  # ...but heard in order


async def test_the_whole_reply_is_one_recording(engine: Any) -> None:
    events = capture(engine)
    stream = engine.reply_speech.open("turn_9", VOICE, "en")
    stream.feed("The first part of the answer is here. The second part follows it.")
    stream.finish()
    await asyncio.wait_for(stream.wait(), 10)
    rec = stream.recording_id
    assert rec and speech(events)[-1]["recording_id"] == rec
    await until(lambda: engine.db.one("SELECT 1 FROM recordings WHERE id=?", (rec,)) is not None)
    row = engine.db.one("SELECT kind, turn_id, duration_s FROM recordings WHERE id=?", (rec,))
    assert (row["kind"], row["turn_id"]) == ("reply", "turn_9")
    x, sr = read_audio_file(engine.recordings.path(rec))
    expected = sum(rendered(t).size for t in ("The first part of the answer is here.", "The second part follows it."))
    assert abs(x.size / sr - expected / 24000) < 0.05 and abs(row["duration_s"] - expected / 24000) < 0.01


async def test_no_recording_when_recordings_are_not_kept(engine: Any) -> None:
    await engine.settings.set_key("voice_input.recording_retention_days", 0)
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("Nothing of this is kept on disk afterwards.")
    stream.finish()
    await asyncio.wait_for(stream.wait(), 10)
    assert stream.recording_id is None


async def test_started_and_ended_follow_what_is_heard(engine: Any) -> None:
    events = capture(engine)
    text = "A reply long enough to time, with a few words in it."
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed(text)
    stream.finish()
    await asyncio.wait_for(stream.wait(), 10)
    (t_start, started), (t_end, ended) = [(t, m) for t, m in events if m.get("type") == "assistant.speech"]
    assert started["state"] == "started" and ended["state"] == "ended"
    audio_s = rendered(text).size / 24000
    tail = engine.settings.get().voice_input.echo_tail_ms / 1000
    assert stream.first_audio_at is not None and t_start >= stream.first_audio_at
    assert t_end - t_start >= audio_s / SCALE + tail * 0.9  # the last sample, then the echo tail
    assert abs(ended["played_ms"] - audio_s * 1000) < 60


# ------------------------------------------------------------------ stopping, superseding, ducking


async def test_stop_fades_out_and_cancels_pending_synthesis(engine: Any) -> None:
    events = capture(engine)
    original = engine.synth.synth
    finished: list[str] = []

    async def slow_rest(text: str, voice: str, **kw: Any) -> Any:
        if not text.startswith("First"):
            await asyncio.sleep(3)
        res = await original(text, voice, **kw)
        finished.append(text)
        return res

    engine.synth.synth = slow_rest
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("First comes this sentence, which plays at once. Then a slow one. And another slow one at the end.")
    stream.finish()
    await until(lambda: any(m["state"] == "started" for m in speech(events)))
    await engine.stop_speech("turn_1")
    assert await asyncio.wait_for(stream.wait(), 2) == "stopped"
    ended = speech(events)[-1]
    assert ended["state"] == "ended" and ended["reason"] == "stopped" and ended["turn_id"] == "turn_1"
    await asyncio.sleep(0.1)
    assert all(u.task is None or u.task.done() for u in stream._units)
    assert finished == ["First comes this sentence, which plays at once."]  # the rest was never made
    assert not any(k.startswith("speech:") for k in engine.transport._holds)


async def test_a_new_reply_supersedes_the_old_one(engine: Any) -> None:
    events = capture(engine)
    old = engine.reply_speech.open("turn_1", VOICE, "en")
    old.feed("This answer is long, and it is still being said when the next question comes in.")
    assert old.will_speak()
    await until(lambda: old.first_audio_at is not None)
    new = engine.reply_speech.open("turn_2", VOICE, "en")
    assert await asyncio.wait_for(old.wait(), 2) == "superseded"
    new.feed("The new answer.")
    new.finish()
    assert await asyncio.wait_for(new.wait(), 10) == "finished"
    by_turn = {(m["turn_id"], m["state"]): m for m in speech(events)}
    assert by_turn[("turn_1", "ended")]["reason"] == "superseded"
    assert by_turn[("turn_2", "ended")]["reason"] == "finished"


async def test_reading_is_held_while_speaking_and_released_after(engine: Any) -> None:
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("Reading waits while this is said.")
    await until(lambda: stream.first_audio_at is not None)
    assert any(k.startswith("speech:") for k in engine.transport._holds)
    stream.finish()
    await asyncio.wait_for(stream.wait(), 10)
    assert not any(k.startswith("speech:") for k in engine.transport._holds)


async def test_a_turn_that_says_nothing_makes_no_sound_and_no_events(engine: Any) -> None:
    events = capture(engine)
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    assert not stream.will_speak()
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 2) == "finished"
    assert speech(events) == [] and not engine.transport._holds


async def test_a_reply_none_of_which_could_be_said_still_ends(engine: Any) -> None:
    events = capture(engine)

    async def broken(text: str, voice: str, **kw: Any) -> Any:
        raise EngineError("provider_error", "No voice today.")

    engine.synth.synth = broken
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("This will not be heard by anyone.")
    assert stream.will_speak()  # promised to the client...
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 2) == "error"
    assert speech(events) == [  # ...so it hears that it is over
        {"type": "assistant.speech", "state": "ended", "kind": "reply", "turn_id": "turn_1", "reason": "error",
         "played_ms": 0, "ts": speech(events)[0]["ts"]}
    ]


async def test_a_failed_sentence_is_skipped(engine: Any) -> None:
    original = engine.synth.synth

    async def flaky(text: str, voice: str, **kw: Any) -> Any:
        if text.startswith("Broken"):
            raise EngineError("provider_error", "No.")
        return await original(text, voice, **kw)

    engine.synth.synth = flaky
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("The first sentence works fine. Broken sentence in the middle. The last one works too.")
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 10) == "finished"
    assert [p.size for p in stream._parts] == [
        rendered("The first sentence works fine.").size, rendered("The last one works too.").size
    ]


# ------------------------------------------------------------------ prompts, notices, samples


async def test_say_waits_until_heard_and_keeps_no_recording(engine: Any) -> None:
    events = capture(engine)
    await asyncio.wait_for(engine.assistant.say("Hi, I'm listening."), 10)
    ev = speech(events)
    assert [(m["state"], m["kind"]) for m in ev] == [("started", "say"), ("ended", "say")]
    assert "recording_id" not in ev[1]


async def test_a_notice_is_spoken_once_per_reason(engine: Any) -> None:
    events = capture(engine)
    text = "The Google voice isn't available right now, so I'm using the local voice."
    assert engine.transport.announce(text, "unavailable") is True
    assert engine.transport.announce(text, "unavailable") is False
    await until(lambda: any(m["state"] == "ended" for m in speech(events)))
    await asyncio.sleep(0.1)
    assert [(m["state"], m["kind"]) for m in speech(events)] == [("started", "say"), ("ended", "say")]


async def test_a_notice_during_a_reply_is_part_of_it(engine: Any) -> None:
    events = capture(engine)
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("Here is the start of the answer. ")
    engine.transport.announce("This month's free voice is used up.", "free_tier")
    stream.feed("And the end of it comes here.")
    stream.finish()
    await asyncio.wait_for(stream.wait(), 10)
    assert [u.text for u in stream._units] == [
        "Here is the start of the answer.", "This month's free voice is used up.", "And the end of it comes here."
    ]
    assert [(m["state"], m["kind"]) for m in speech(events)] == [("started", "reply"), ("ended", "reply")]


async def test_a_sample_played_by_the_assistant_comes_after_its_words(engine: Any) -> None:
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("Here's the alto voice")
    await engine.voices.preview(VOICE, play=True, turn_id="turn_1")
    stream.feed("Want it?")
    stream.finish()
    assert await asyncio.wait_for(stream.wait(), 10) == "finished"
    units = stream._units
    assert units[0].text == "Here's the alto voice" and units[1].text == "" and units[1].pcm is not None
    assert units[2].text == "Want it?"


async def test_a_sample_outside_a_turn_replaces_what_is_being_said(engine: Any) -> None:
    stream = engine.reply_speech.open("turn_1", VOICE, "en")
    stream.feed("A long answer that is still being said when the user presses play on a voice.")
    await until(lambda: stream.first_audio_at is not None)
    task = asyncio.create_task(engine.voices.preview(VOICE, play=True))
    assert await asyncio.wait_for(stream.wait(), 2) == "superseded"
    await asyncio.wait_for(task, 10)


# ------------------------------------------------------------------ "auto" right after startup


def _google_engine(tmp_path: Path) -> Any:
    from open_speech.service.engine import Engine

    return Engine(make_env(open_speech_home=tmp_path / "home", open_speech_fixture_providers=True,
                           gemini_api_key="g-gem", open_speech_audio_output="null"),
                  output=NullOutput(time_scale=SCALE))


def _answer_after(check: Any, delay: float, *, blocked: bool) -> None:
    from open_speech.providers.errors import ProviderError

    async def listed(key: str, locale: str) -> set[str]:
        await asyncio.sleep(delay)
        if blocked:
            err = ProviderError("unavailable", "Not allowed.", provider="google", status=403)
            err.blocked = "blocked"  # type: ignore[attr-defined]
            raise err
        return {f"{locale}-Chirp3-HD-Zephyr", f"{locale}-Chirp3-HD-Kore"}

    check._list = listed


async def test_a_read_right_after_startup_waits_for_the_google_check_and_says_nothing(tmp_path: Path) -> None:
    """A key the API refuses, a read started 0 ms after startup: the local voice from the first word, and no
    "isn't available" notice (before: Chirp was assumed, failed, and the fallback was announced)."""
    engine = _google_engine(tmp_path)
    said: list[str] = []
    try:
        check = engine.registry.google_check()
        _answer_after(check, 0.25, blocked=True)
        await engine.start()
        engine.transport.speak_notice = said.append
        doc = (await engine.documents.create(s.CreateDocumentRequest(kind="text", text="Read this now."))).document
        t0 = asyncio.get_running_loop().time()
        await engine.playback.play(s.PlayRequest(document_id=doc.id))
        waited = asyncio.get_running_loop().time() - t0
        snap = engine.transport.snapshot()
        assert snap.voice_id == VOICE and snap.fallback_notice is None and said == []
        assert 0.15 < waited < 1.0
    finally:
        await engine.shutdown()


async def test_auto_is_the_google_voice_when_the_first_check_says_so(tmp_path: Path) -> None:
    engine = _google_engine(tmp_path)
    try:
        _answer_after(engine.registry.google_check(), 0.2, blocked=False)
        await engine.start()
        await engine.voices.settle_auto()
        assert engine.voices.effective("auto", "en") == "google:chirp3-hd:en-US-Chirp3-HD-Zephyr"
        t0 = asyncio.get_running_loop().time()
        await engine.voices.settle_auto()  # answered: no wait any more
        assert asyncio.get_running_loop().time() - t0 < 0.01
    finally:
        await engine.shutdown()


async def test_the_wait_for_the_first_check_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from open_speech.service import misc

    monkeypatch.setattr(misc, "AUTO_WAIT_S", 0.2)
    engine = _google_engine(tmp_path)
    try:
        _answer_after(engine.registry.google_check(), 30, blocked=False)  # Google never answers
        await engine.start()
        t0 = asyncio.get_running_loop().time()
        await engine.voices.settle_auto()
        assert 0.15 < asyncio.get_running_loop().time() - t0 < 0.5
    finally:
        await engine.shutdown()
