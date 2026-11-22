"""On-device speech-to-text (providers/local_stt.py) with a fake recognizer: no weights, no sherpa-onnx."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.catalog import LocalSttModel, ModelCatalog
from open_speech.providers.base import SttEvent, SttProvider
from open_speech.providers.local_stt import LocalStt, has_sound, leveled

STT_MODEL = "parakeet-tdt-0.6b-v3-int8"
RATE = 16000


def install_fake_stt_weights(paths: Paths, catalog: ModelCatalog, model: str = STT_MODEL) -> Path:
    """Every file the catalog entry needs, empty, under `<models>/stt/<model>/`."""
    entry = catalog.local_stt_model(model)
    assert entry is not None
    d = paths.models / "stt" / model
    d.mkdir(parents=True, exist_ok=True)
    for name in entry.required_files():
        (d / name).write_bytes(b"fake")
    return d


class FakeRecognizer:
    """Stands in for a loaded model: "heard 1.2s" for 1.2 s of audio. Records calls and how many ran at once."""

    def __init__(self, *, delay: float = 0.0, hold: threading.Event | None = None, fail: int = 0) -> None:
        self.calls: list[tuple[float, str | None]] = []
        self.delay = delay
        self.hold = hold  # when given, every decode waits for it
        self.fail = fail  # how many of the first calls raise
        self.in_flight = 0
        self.max_in_flight = 0
        self.started = threading.Event()

    def transcribe(self, samples: np.ndarray, lang: str | None) -> str:
        assert samples.dtype == np.float32 and samples.ndim == 1 and float(np.abs(samples).max()) <= 1.0
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        try:
            if self.hold is not None:
                assert self.hold.wait(5)
            if self.delay:
                time.sleep(self.delay)
            secs = len(samples) / RATE
            self.calls.append((secs, lang))
            if len(self.calls) <= self.fail:
                raise RuntimeError("model exploded")
            return f"heard {secs:.1f}s"
        finally:
            self.in_flight -= 1


class Factory:
    def __init__(self, rec: FakeRecognizer | None = None) -> None:
        self.rec = rec or FakeRecognizer()
        self.loads: list[tuple[str, Path]] = []

    def __call__(self, entry: LocalSttModel, model_dir: Path) -> FakeRecognizer:
        self.loads.append((entry.model, model_dir))
        return self.rec


def make_stt(catalog: ModelCatalog, paths: Paths, rec: FakeRecognizer | None = None, *, weights: bool = True,
             **kw: Any) -> tuple[LocalStt, Factory]:
    if weights:
        install_fake_stt_weights(paths, catalog)
    factory = Factory(rec)
    return LocalStt(catalog, paths, recognizer_factory=factory, **kw), factory


def speech_pcm(seconds: float, level: float = 0.2) -> bytes:
    """Speech-shaped PCM16: 0.2 s of quiet, then a tone that comes and goes like syllables."""
    t = np.arange(int(seconds * RATE)) / RATE
    syllables = (np.sin(2 * np.pi * 3 * t) > -0.3) & (t > 0.2)
    x = np.sin(2 * np.pi * 180 * t) * syllables * level
    return (x * 32767).astype("<i2").tobytes()


async def frames(pcm: bytes, *, frame_s: float = 0.02, pause: float = 0.001) -> AsyncIterator[bytes]:
    """Mic frames; the short pause lets worker threads hand their results back between frames."""
    n = int(frame_s * RATE) * 2
    for i in range(0, len(pcm), n):
        yield pcm[i : i + n]
        await asyncio.sleep(pause)


async def collect(stt: LocalStt, pcm: bytes, **kw: Any) -> list[SttEvent]:
    return [e async for e in stt.stream(frames(pcm, **kw), lang="en")]


# ------------------------------------------------------------------ availability


def test_unavailable_reasons_are_plain_sentences(catalog: ModelCatalog, paths: Paths,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    stt, _ = make_stt(catalog, paths, weights=False)
    assert isinstance(stt, SttProvider) and stt.id == "local" and stt.model == STT_MODEL
    assert not stt.installed() and stt.runtime_available()
    assert stt.unavailable_reason() == (
        "Download Parakeet speech recognition (about 642 MB) to use speech input on this Mac."
    )
    assert stt.unavailable_reason("no-such-model") == "This speech recognition model isn't in the catalog."
    install_fake_stt_weights(paths, catalog)
    assert stt.installed() and stt.unavailable_reason() is None
    # without sherpa-onnx (and without an injected recognizer) the runtime is what's missing
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda n, *a: None if n == "sherpa_onnx" else real(n, *a))
    bare = LocalStt(catalog, paths)
    assert not bare.runtime_available()
    assert bare.unavailable_reason() == (
        "On-device speech recognition needs sherpa-onnx. Install open-speech[local-stt]."
    )
    for reason in (stt.unavailable_reason("no-such-model"), bare.unavailable_reason()):
        assert reason is not None and "key" not in reason.lower()


async def test_stream_without_weights_raises_the_reason(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, weights=False)
    with pytest.raises(EngineError, match="Download Parakeet") as ei:
        await collect(stt, speech_pcm(0.5))
    assert ei.value.code == "offline" and not factory.loads
    with pytest.raises(EngineError, match="Download Parakeet"):
        await stt.transcribe(paths.home / "missing.wav")


def test_incomplete_weights_are_not_installed(catalog: ModelCatalog, paths: Paths) -> None:
    stt, _ = make_stt(catalog, paths)
    (stt.model_dir() / "silero_vad.onnx").unlink()
    assert not stt.installed()


def test_bundled_copy_counts_and_app_data_wins(catalog: ModelCatalog, paths: Paths, tmp_path: Path) -> None:
    runtime = Paths(tmp_path / "runtime")
    bundled = install_fake_stt_weights(runtime, catalog)
    stt = LocalStt(catalog, paths, recognizer_factory=Factory(), bundled_models=runtime.models)
    assert stt.installed() and stt.model_dir() == bundled
    user = install_fake_stt_weights(paths, catalog)
    assert stt.model_dir() == user


def test_model_falls_back_to_one_that_is_installed(paths: Paths) -> None:
    raw = ModelCatalog.load(paths).raw
    other = dict(raw["stt"]["local"][STT_MODEL], name="Small recognizer")
    cat = ModelCatalog.from_dict({**raw, "stt": {**raw["stt"], "local": {**raw["stt"]["local"], "small-stt": other}}})
    stt = LocalStt(cat, paths, recognizer_factory=Factory())
    assert stt.model == STT_MODEL  # nothing installed: the catalog default
    install_fake_stt_weights(paths, cat, "small-stt")
    assert stt.model == "small-stt" and stt.installed()
    install_fake_stt_weights(paths, cat)
    assert stt.model == STT_MODEL  # the default wins once it is on disk
    assert LocalStt(cat, paths, model="small-stt", recognizer_factory=Factory()).model == "small-stt"


# ------------------------------------------------------------------ streaming


async def test_stream_partials_then_exactly_one_final(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, partial_interval_s=0.4)
    events = await collect(stt, speech_pcm(2.0))
    kinds = [e.kind for e in events]
    assert kinds.count("final") == 1 and kinds[-1] == "final" and kinds.count("partial") >= 2
    assert events[-1].text == "heard 2.0s"  # the final decodes the whole buffer
    partials = [e.text for e in events[:-1]]
    assert len(set(partials)) == len(partials) and all(partials)  # non-empty, never repeated
    # partials grow with the buffer and each decode saw the language hint
    heard = [float(p.split()[1][:-1]) for p in partials]
    assert heard == sorted(heard) and heard[0] >= 0.4
    rec = factory.rec
    assert {lang for _, lang in rec.calls} == {"en"} and rec.max_in_flight == 1
    assert factory.loads == [(STT_MODEL, paths.models / "stt" / STT_MODEL)]


async def test_no_partials_when_interval_is_off(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, partial_interval_s=None)
    events = await collect(stt, speech_pcm(1.0))
    assert [(e.kind, e.text) for e in events] == [("final", "heard 1.0s")]
    assert len(factory.rec.calls) == 1


async def test_silence_noise_and_taps_never_reach_the_model(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, partial_interval_s=0.2)
    rng = np.random.default_rng(1)
    hiss = (rng.standard_normal(RATE * 2) * 0.05 * 32767).astype("<i2").tobytes()
    for pcm in (b"", b"\x00\x00" * RATE * 2, hiss, speech_pcm(0.1, level=0.5)):
        events = await collect(stt, pcm, pause=0)
        assert [(e.kind, e.text) for e in events] == [("final", "")]
    assert factory.rec.calls == []


async def test_slow_decodes_never_overlap_or_queue_up(catalog: ModelCatalog, paths: Paths) -> None:
    """A decode that takes longer than the partial interval: later partials are skipped, not stacked."""
    rec = FakeRecognizer(delay=0.05)
    stt, _ = make_stt(catalog, paths, rec, partial_interval_s=0.1)
    events = await collect(stt, speech_pcm(2.0))  # 100 frames, ~1 ms apart: far faster than the decodes
    assert events[-1] == SttEvent(kind="final", text="heard 2.0s")
    assert rec.max_in_flight == 1
    assert len(rec.calls) < 10  # 19 interval boundaries passed; most were skipped


async def test_interim_failure_does_not_end_the_utterance(catalog: ModelCatalog, paths: Paths) -> None:
    rec = FakeRecognizer(fail=1)
    stt, _ = make_stt(catalog, paths, rec, partial_interval_s=0.4)
    events = await collect(stt, speech_pcm(1.5))
    assert events[-1] == SttEvent(kind="final", text="heard 1.5s")


async def test_final_failure_is_an_engine_error(catalog: ModelCatalog, paths: Paths) -> None:
    stt, _ = make_stt(catalog, paths, FakeRecognizer(fail=5), partial_interval_s=None)
    with pytest.raises(EngineError, match="Speech recognition failed") as ei:
        await collect(stt, speech_pcm(1.0))
    assert ei.value.code == "provider_error"

    def broken(entry: LocalSttModel, model_dir: Path) -> Any:
        raise RuntimeError("bad weights")

    stt = LocalStt(catalog, paths, recognizer_factory=broken, partial_interval_s=None)
    with pytest.raises(EngineError, match="couldn't be loaded"):
        await collect(stt, speech_pcm(1.0))


async def test_new_utterance_while_the_previous_decode_is_finishing(catalog: ModelCatalog, paths: Paths) -> None:
    """The service abandons a session whose decode is stuck and starts the next one: the old decode finishes
    on its own, the new one waits its turn, and nothing runs concurrently."""
    hold = threading.Event()
    rec = FakeRecognizer(hold=hold)
    stt, _ = make_stt(catalog, paths, rec, partial_interval_s=None)
    first = asyncio.create_task(collect(stt, speech_pcm(1.0)))
    assert await asyncio.to_thread(rec.started.wait, 5)  # the first final decode is running
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(collect(stt, speech_pcm(2.0)))
    await asyncio.sleep(0.05)
    assert not second.done() and rec.in_flight == 1  # queued behind the decode still in flight
    hold.set()
    events = await asyncio.wait_for(second, 5)
    assert events == [SttEvent(kind="final", text="heard 2.0s")]
    assert rec.max_in_flight == 1 and [round(s) for s, _ in rec.calls] == [1, 2]


async def test_abandoned_partials_are_skipped(catalog: ModelCatalog, paths: Paths) -> None:
    """An interim decode still waiting for its turn when the utterance ends is dropped, not computed."""
    hold = threading.Event()
    rec = FakeRecognizer(hold=hold)
    stt, _ = make_stt(catalog, paths, rec, partial_interval_s=0.2)
    other = asyncio.create_task(stt.transcribe(_wav(paths.home / "busy.wav", 1.0)))  # holds the model
    assert await asyncio.to_thread(rec.started.wait, 5)
    stream = asyncio.create_task(collect(stt, speech_pcm(1.0)))
    await asyncio.sleep(0.2)  # the stream has ended; its partial and its final wait behind `other`
    hold.set()
    events = await asyncio.wait_for(stream, 5)
    await other
    assert events == [SttEvent(kind="final", text="heard 1.0s")]
    assert [round(s, 1) for s, _ in rec.calls] == [1.0, 1.0]  # the file, then the final: no partial decode


# ------------------------------------------------------------------ load / unload


async def test_lazy_load_and_idle_unload(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, idle_unload_s=0.05, partial_interval_s=None)
    assert not stt.loaded() and factory.loads == []
    await collect(stt, speech_pcm(0.5))
    assert stt.loaded() and len(factory.loads) == 1
    await collect(stt, speech_pcm(0.5))
    assert len(factory.loads) == 1  # reused while warm
    await asyncio.sleep(0.15)
    assert not stt.loaded()
    await collect(stt, speech_pcm(0.5))
    assert len(factory.loads) == 2


async def test_model_loads_as_soon_as_the_mic_opens(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths, partial_interval_s=None)

    async def quiet_mic() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"\x00\x00" * 320
            await asyncio.sleep(0.005)
            if stt.loaded():
                return

    events = [e async for e in stt.stream(quiet_mic())]
    assert stt.loaded() and factory.rec.calls == []  # loaded before any speech arrived; nothing decoded
    assert events == [SttEvent(kind="final", text="")]


async def test_unload_never_blocks_on_a_running_decode(catalog: ModelCatalog, paths: Paths) -> None:
    hold = threading.Event()
    rec = FakeRecognizer(hold=hold)
    stt, factory = make_stt(catalog, paths, rec, partial_interval_s=None)
    task = asyncio.create_task(collect(stt, speech_pcm(1.0)))
    assert await asyncio.to_thread(rec.started.wait, 5)
    t0 = time.monotonic()
    stt.unload()  # e.g. the weights were removed mid-utterance
    assert time.monotonic() - t0 < 0.1 and stt.loaded()
    hold.set()
    assert (await task)[-1].text == "heard 1.0s"
    assert not stt.loaded()  # dropped as soon as the decode finished


# ------------------------------------------------------------------ files


def _wav(path: Path, seconds: float, rate: int = RATE) -> Path:
    x = np.frombuffer(speech_pcm(seconds), dtype="<i2").astype(np.float32) / 32768.0
    if rate != RATE:
        x = np.interp(np.arange(int(seconds * rate)) * RATE / rate, np.arange(x.size), x).astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), x, rate)
    return path


async def test_transcribe_file_resamples_to_the_mic_rate(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths)
    t = await stt.transcribe(_wav(paths.home / "a.wav", 1.5, rate=24000))
    assert t.text == "heard 1.5s" and t.audio_sec == 1.5
    assert factory.rec.calls == [(1.5, None)]
    # the protocol's `words` flag is accepted; this recognizer has no trustworthy word times to give
    assert (await stt.transcribe(_wav(paths.home / "a.wav", 1.5), words=True)).words == []
    silent = paths.home / "silent.wav"
    sf.write(str(silent), np.zeros(RATE, np.float32), RATE)
    assert (await stt.transcribe(silent)).text == "" and len(factory.rec.calls) == 2


async def test_transcribe_long_file_in_pieces(catalog: ModelCatalog, paths: Paths) -> None:
    stt, factory = make_stt(catalog, paths)
    t = await stt.transcribe(_wav(paths.home / "long.wav", 65.0))
    assert [round(s) for s, _ in factory.rec.calls] == [30, 30, 5]
    assert t.text == "heard 30.0s heard 30.0s heard 5.0s" and t.audio_sec == 65.0


# ------------------------------------------------------------------ helpers


def test_has_sound() -> None:
    rng = np.random.default_rng(0)
    quiet = np.zeros(RATE, np.float32)
    assert not has_sound(quiet) and not has_sound(quiet[:100])
    assert not has_sound(rng.standard_normal(RATE * 3).astype(np.float32) * 0.001)  # mic floor, -60 dBFS
    assert not has_sound(rng.standard_normal(RATE * 3).astype(np.float32) * 0.1)  # steady hiss, however loud
    t = np.arange(RATE * 3) / RATE
    assert not has_sound((np.sin(2 * np.pi * 60 * t) * 0.05).astype(np.float32))  # steady hum
    word = quiet.copy()
    word[4000:8000] = np.sin(2 * np.pi * 200 * t[:4000]) * 0.005  # a quarter-second word at about -49 dBFS
    assert has_sound(word)
    assert has_sound(word + rng.standard_normal(RATE).astype(np.float32) * 0.0005)
    click = quiet.copy()
    click[8000:8500] = 0.5  # 30 ms: too short to be a word
    assert not has_sound(click)


def test_leveled_brings_quiet_audio_to_a_full_level() -> None:
    t = np.arange(RATE) / RATE
    quiet = (np.sin(2 * np.pi * 200 * t) * 0.02).astype(np.float32)
    out = leveled(quiet)
    assert out.dtype == np.float32 and float(np.abs(out).max()) == pytest.approx(0.9, abs=1e-3)
    assert float(np.abs(leveled(quiet * 40)).max()) == pytest.approx(0.9, abs=1e-3)  # loud audio comes down
    faint = quiet * 0.01  # 40 dB is the most it adds: a faint rustle doesn't become a shout
    assert float(np.abs(leveled(faint)).max()) == pytest.approx(0.02, abs=1e-3)
    silence = np.zeros(100, np.float32)
    assert leveled(silence) is silence and leveled(silence[:0]).size == 0
