"""The playback chain (05 § 9): stretcher, renderer, fades, streams, latency-corrected positions, diagnostics,
resampling. Nothing here opens a sound device: audio is pulled by the test (or a NullOutput thread)."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from open_speech.config import Paths
from open_speech.playback import fade
from open_speech.playback import metrics as m
from open_speech.playback.output import NullOutput, SoundDeviceOutput
from open_speech.playback.render import Renderer, RenderItem
from open_speech.playback.selftest import synthetic_voice
from open_speech.playback.stretch import Stretcher, stretch
from open_speech.playback.transport import Transport
from open_speech.providers.pcm import encode_ogg_opus, resample
from open_speech.service.events import EventBus
from open_speech.store.db import Database

from .helpers import SettingsBox
from .test_registry_synth import Runtime, build

SR = 24000


def sine(f: float, seconds: float, sr: int = SR, amp: float = 0.3) -> np.ndarray:
    return (amp * np.sin(2 * np.pi * f * np.arange(int(seconds * sr)) / sr)).astype(np.float32)


def drain(r: Renderer, *, block: int = 480, events: dict[int, Any] | None = None, max_blocks: int = 4000,
          stop_when_paused: bool = False) -> np.ndarray:
    """Pull like an output would, but wait for the render thread instead of starving it. `events` maps a block
    number to a callable run before that block."""
    out = []
    for i in range(max_blocks):
        if events and i in events:
            events[i]()
        if r.snapshot().finished:
            break
        deadline = time.monotonic() + 1.0
        while not r.reading_paused and r.buffered_frames() < block and not r.rendering_done():
            if time.monotonic() > deadline:
                break
            time.sleep(0.0005)
        out.append(r.pull(block))
        if stop_when_paused and r.reading_paused and r.quiet() and not (events and any(k > i for k in events)):
            out.append(r.pull(block))
            break
    return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)


def renderer(rate: int = SR, **kw: Any) -> Renderer:
    r = Renderer(SR, rate=rate, **kw)
    r.start()
    return r


# ------------------------------------------------------------------------------------------- stretcher


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("overlap", [2, 4])
def test_bypass_is_bit_exact_for_any_chunking(seed: int, overlap: int) -> None:
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(60000) * 0.2).astype(np.float32)
    s = Stretcher(SR, 1.0, overlap=overlap)
    out, i = [], 0
    while i < x.size:
        n = int(rng.choice([1, 7, 480, int(rng.integers(1, 6000))]))
        out.append(s.process(x[i : i + n]))
        i += n
    out.append(s.flush())
    y = np.concatenate(out)
    assert y.size == x.size and np.array_equal(y, x)


@pytest.mark.parametrize("speed", [0.5, 0.8, 1.25, 1.5, 2.0, 3.0, 5.0])
def test_speed_is_exact_in_the_steady_state(speed: float) -> None:
    x = synthetic_voice(6.0)
    s = Stretcher(SR, speed)
    y, segs = s.process_mapped(x)
    # Over whole hops, output advances hop samples while the source advances speed x hop on average.
    mid = segs[5:-5]
    out_n = sum(n for n, _a, _b in mid)
    src_n = mid[-1][2] - mid[0][1]
    assert abs(src_n / out_n - speed) / speed < 0.01
    total = stretch(x, speed)
    assert abs(total.size * speed / x.size - 1) < 0.02  # ends: at most a hop or so


def test_pitch_is_preserved_for_low_and_high_voices() -> None:
    for f0 in (95.0, 210.0):
        x = sine(f0, 3.0)
        for speed in (0.5, 1.5, 2.0, 3.0):
            y = stretch(x, speed)
            mid = y[y.size // 4 : 3 * y.size // 4]
            f = np.argmax(np.abs(np.fft.rfft(mid))) * SR / mid.size
            assert abs(f - f0) < 3, (f0, speed, f)


def test_speed_changes_are_continuous_and_return_to_exact_bypass() -> None:
    x = sine(220.0, 6.0)
    s = Stretcher(SR, 1.0)
    out = []
    plan = {10: 1.5, 20: 1.0, 30: 0.75, 40: 1.0}
    for k, i in enumerate(range(0, x.size, 2400)):
        if k in plan:
            s.set_speed(plan[k])
        out.append(s.process(x[i : i + 2400]))
    out.append(s.flush())
    y = np.concatenate(out)
    slope = 2 * np.pi * 220 / SR * 0.3
    assert float(np.max(np.abs(np.diff(y)))) <= slope * 1.05  # no step anywhere, including the switches
    tail = y[-4000:-500]  # back at 1x and settled: an exact copy of some stretch of the input
    hits = np.flatnonzero(np.isclose(x, tail[0], atol=0, rtol=0))
    assert any(np.array_equal(x[h : h + tail.size], tail) for h in hits)


def test_splices_match_the_waveform_better_than_a_naive_cut() -> None:
    """WSOLA picks splice points by shape: at 2x the output is explained by the source far better than plain
    overlap-add without the search would be."""
    x = synthetic_voice(4.0)
    y = stretch(x, 2.0)
    st = m.splice_stats(m.splice_track(x, y, 2.0, SR), SR, 2.0)
    assert st.low_ncc_frac < 0.05 and st.mean_ncc > 0.95


# ------------------------------------------------------------------------------------------- renderer


def test_renderer_is_bit_exact_and_gapless_at_1x() -> None:
    a, b = synthetic_voice(1.3), synthetic_voice(0.9)
    r = renderer()
    r.enqueue(RenderItem(0, a))
    r.enqueue(RenderItem(1, b))
    r.mark_end_of_stream()
    r.pause_reading(False)
    y = drain(r)
    r.close()
    ref = np.concatenate([a, b])
    assert np.array_equal(y[: ref.size], ref) and not np.any(y[ref.size :])


def test_renderer_resamples_to_the_device_rate_without_images() -> None:
    x = sine(9000.0, 1.0, amp=0.5)
    r = renderer(48000)
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    y = drain(r, block=960)
    r.close()
    core = y[4800:-9600].astype(np.float64)
    spec = np.abs(np.fft.rfft(core * np.hanning(core.size))) ** 2
    f = np.fft.rfftfreq(core.size, 1 / 48000)
    image = spec[(f > 14000)].sum() / spec[(f > 8000) & (f < 10000)].sum()
    assert 10 * np.log10(image) < -90  # linear interpolation left an image at 39 kHz around -20 dB
    assert abs(y.size / 48000 - 1.0) < 0.03


def test_pause_and_resume_fade_over_12_ms() -> None:
    x = sine(300.0, 2.0)
    r = renderer()
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    y = drain(r, events={20: lambda: r.pause_reading(True), 40: lambda: r.pause_reading(False)})
    r.close()
    assert m.hard_edges(y, SR) == 0 and m.clicks(y, SR).events == 0
    k = 20 * 480
    n12 = fade.frames(fade.PAUSE_MS, SR)
    assert np.max(np.abs(y[k : k + n12 // 2])) > 0.2  # still fading
    assert not np.any(y[k + n12 : k + n12 + 2000])  # silent once the fade is done


def test_stop_fades_over_15_ms_and_seek_crossfades() -> None:
    x = sine(300.0, 3.0)
    r = renderer()
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)

    def seek() -> None:
        r.flush(index=0, offset=SR)
        r.enqueue(RenderItem(0, x, SR))
        r.mark_end_of_stream()

    def stop() -> None:
        r.pause_reading(True, fade_ms=fade.STOP_MS)
        r.flush(index=0, offset=0)

    y = drain(r, events={20: seek, 60: stop}, stop_when_paused=True)
    r.close()
    assert m.hard_edges(y, SR) == 0 and m.clicks(y, SR).events == 0
    k = 60 * 480
    n15 = fade.frames(fade.STOP_MS, SR)
    assert np.max(np.abs(y[k : k + n15 // 3])) > 0.1 and not np.any(y[k + n15 :])


def test_speed_change_is_heard_quickly_and_without_a_step() -> None:
    x = sine(250.0, 4.0)
    r = renderer()
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    y = drain(r, events={25: lambda: r.set_speed(2.0)})
    r.close()
    slope = 2 * np.pi * 250 / SR * 0.3
    assert float(np.max(np.abs(np.diff(y)))) <= slope * 1.3
    # 1.2 s at 1x, then the rest at 2x: the speed change took effect within ~0.1 s, not after the 0.3 s ring.
    expected = 25 * 480 + (x.size - 25 * 480) / 2
    assert abs(y.size - expected) < 0.1 * SR


def test_positions_are_latency_corrected() -> None:
    x = synthetic_voice(3.0)
    r = renderer()
    r.set_output_latency(int(0.1 * SR))
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    while r.buffered_frames() < 4800:
        time.sleep(0.001)
    for _ in range(10):
        r.pull(480)
    pos = r.snapshot()
    r.close()
    assert pos.index == 0
    assert abs(pos.offset - (4800 - 2400)) <= 480  # what is audible now, not what was handed out


def test_starvation_is_counted_by_cause() -> None:
    class Slow(Stretcher):
        def process_mapped(self, x: np.ndarray) -> Any:
            time.sleep(0.02)
            return super().process_mapped(x)

    r = Renderer(SR, rate=SR, stretcher_factory=Slow)
    r.start()
    r.pause_reading(False)
    for _ in range(5):
        r.pull(480)  # nothing queued: waiting for synthesis
    assert r.diagnostics.synthesis_waits == 1 and r.diagnostics.render_starvations == 0
    r.set_speed(1.5)
    r.enqueue(RenderItem(0, synthetic_voice(2.0)))
    time.sleep(0.05)
    for _ in range(40):
        r.pull(2400)  # pulls faster than the slowed render thread can stretch
    r.close()
    assert r.diagnostics.render_starvations >= 1


# ------------------------------------------------------------------------------------------- streams


def _soft(x: np.ndarray) -> np.ndarray:
    """A piece whose own ends are silent (as TTS sentences are), so no edge fade touches it."""
    y = x.copy()
    fade.fade_in(y, 480)
    fade.fade_out(y, 480)
    pad = np.zeros(240, dtype=np.float32)
    return np.concatenate([pad, y, pad])


def test_clip_stream_plays_pieces_in_order_without_gaps() -> None:
    r = renderer()
    pieces = [_soft(sine(200, 0.2)), _soft(sine(300, 0.15)), _soft(sine(400, 0.1))]
    s = r.open_stream("reply")
    for p in pieces:
        s.append(p)
    s.finish()
    out = []
    while not s.done.is_set():
        time.sleep(0.002)
        out.append(r.pull(480))
        assert len(out) < 2000
    r.close()
    y = np.concatenate(out)
    ref = np.concatenate(pieces)
    assert s.first_frame is not None
    assert np.array_equal(y[s.first_frame : s.first_frame + ref.size], ref)
    assert s.first_frame is not None and s.last_frame is not None
    assert s.last_frame - s.first_frame == ref.size and s.reason == "finished"


def test_clip_stream_stop_fades_out_and_ends() -> None:
    r = renderer()
    s = r.open_stream("reply")
    s.append(sine(300, 2.0))
    out = [r.pull(480)]
    while s.first_frame is None:
        time.sleep(0.002)
        out.append(r.pull(480))
    for _ in range(5):
        out.append(r.pull(480))
    s.stop(15.0)
    for _ in range(5):
        out.append(r.pull(480))
    assert s.done.wait(1.0) and s.reason == "stopped"
    r.close()
    y = np.concatenate(out)
    assert m.hard_edges(y, SR) == 0
    assert s.played < int(0.5 * SR)


def test_two_overlapping_clips_of_different_length_both_finish() -> None:
    r = renderer()
    long_done = r.play_clip(sine(220, 0.05))
    short_done = r.play_clip(sine(330, 0.02))
    for _ in range(200):
        if long_done.is_set() and short_done.is_set():
            break
        time.sleep(0.002)
        r.pull(480)
    r.close()
    assert short_done.is_set() and long_done.is_set() and not r.has_clips()


# ------------------------------------------------------------------------------------------- transport speech


@pytest.fixture
def rt(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> Runtime:
    return build(paths, db, settings, events)


async def _transport(rt: Runtime, settings: SettingsBox, events: EventBus, **kw: Any) -> Transport:
    return Transport(rt.synth, NullOutput(time_scale=kw.pop("time_scale", 4.0)), events, settings=settings,
                     on_position=lambda *_a: None, **kw)


def _speech(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("type") == "assistant.speech"]


async def test_play_clip_emits_started_then_ended_after_the_echo_tail(
    rt: Runtime, settings: SettingsBox, events: EventBus, captured: list[dict[str, Any]], tmp_path: Path
) -> None:
    wav = tmp_path / "clip.wav"
    sf.write(str(wav), sine(330, 0.6), SR)
    settings.set("voice_input.echo_tail_ms", 150)
    t = await _transport(rt, settings, events)
    t0 = asyncio.get_running_loop().time()
    reason = await t.play_clip(wav, kind="preview", recording_id="rec_1")
    elapsed = asyncio.get_running_loop().time() - t0
    await t.close()
    ev = _speech(captured)
    assert reason == "finished"
    assert [e["state"] for e in ev] == ["started", "ended"]
    assert all(e["kind"] == "preview" for e in ev)
    assert ev[1]["reason"] == "finished" and ev[1]["recording_id"] == "rec_1"
    assert abs(ev[1]["played_ms"] - 600) < 30
    assert elapsed >= 0.6 / 4 + 0.15 * 0.9  # the clip (at 4x wall clock) plus the echo tail


async def test_stop_speech_fades_out_and_reports_stopped(
    rt: Runtime, settings: SettingsBox, events: EventBus, captured: list[dict[str, Any]]
) -> None:
    t = await _transport(rt, settings, events, time_scale=1.0)
    h = t.open_speech("reply", turn_id="turn_1")
    h.append(sine(250, 3.0))
    h.finish(recording_id="rec_r")
    for _ in range(200):
        if h.started:
            break
        await asyncio.sleep(0.01)
    assert h.started
    assert t.stop_speech("other_turn") == 0
    assert t.stop_speech("turn_1") == 1
    reason = await asyncio.wait_for(h.wait(), 2.0)
    await t.close()
    ev = _speech(captured)
    assert reason == "stopped" and ev[-1]["state"] == "ended" and ev[-1]["reason"] == "stopped"
    assert ev[-1]["turn_id"] == "turn_1" and ev[-1]["kind"] == "reply"
    assert ev[-1]["played_ms"] < 1500


async def test_speech_ducks_reading_and_releases_it(
    rt: Runtime, settings: SettingsBox, events: EventBus
) -> None:
    t = await _transport(rt, settings, events, time_scale=8.0)
    h = t.open_speech("say")
    assert any(k.startswith("speech:") for k in t._holds)
    h.append(sine(300, 0.2))
    h.finish()
    await asyncio.wait_for(h.wait(), 3.0)
    assert not any(k.startswith("speech:") for k in t._holds)
    await t.close()


# ------------------------------------------------------------------------------------------- output, resampling, cache


def test_output_counts_portaudio_status_flags() -> None:
    from open_speech.playback.diagnostics import AudioDiagnostics

    d = AudioDiagnostics()
    out = SoundDeviceOutput(diagnostics=d)
    out._channels = 2
    out._pull = lambda n: np.full(n, 0.25, dtype=np.float32)
    buf = np.zeros((256, 2), dtype=np.float32)
    status = SimpleNamespace(output_underflow=True, output_overflow=False, priming_output=False,
                             __bool__=lambda self: True)
    out._callback(buf, 256, None, status)
    out._callback(buf, 256, None, None)
    assert d.output_underflows == 1 and np.all(buf == 0.25)


def test_resample_is_band_limited_both_ways() -> None:
    up = resample(sine(10000.0, 1.0, amp=0.5), 24000, 48000)[4800:-4800]
    spec = np.abs(np.fft.rfft(up.astype(np.float64) * np.hanning(up.size))) ** 2
    f = np.fft.rfftfreq(up.size, 1 / 48000)
    assert 10 * np.log10(spec[f > 13000].sum() / spec.sum()) < -90  # no image at 38 kHz
    down = resample(sine(15000.0, 1.0, 48000, amp=0.5), 48000, 24000)[2400:-2400]  # above the new Nyquist
    assert 20 * np.log10(np.sqrt(np.mean(down.astype(np.float64) ** 2)) / 0.35) < -80  # no alias at 9 kHz


def test_opus_cache_is_encoded_above_the_speech_mode_threshold() -> None:
    x = synthetic_voice(3.0)
    data, sr = encode_ogg_opus(x, SR)
    kbps = len(data) * 8 / 3.0 / 1000
    assert sr == 24000 and kbps > 45


def test_soft_limiter_never_clips_hard() -> None:
    r = Renderer(SR, rate=SR)
    s1, s2 = r.open_stream(), r.open_stream()
    r.start()
    s1.append(sine(200, 0.3, amp=0.8))
    s2.append(sine(200, 0.3, amp=0.8))
    s1.finish()
    s2.finish()
    out = []
    for _ in range(100):
        time.sleep(0.002)
        out.append(r.pull(480))
        if s1.done.is_set() and s2.done.is_set():
            break
    r.close()
    y = np.concatenate(out)
    assert float(np.max(np.abs(y))) < 1.0 and float(np.max(np.abs(y))) > 0.89


def test_metrics_detect_hard_cuts_and_pass_fades() -> None:
    x = synthetic_voice(2.0)
    i = int(np.flatnonzero(np.abs(x) > 0.1)[100])
    cut = np.concatenate([x[:i], np.zeros(SR // 4, np.float32), x[i:]])
    faded = x.copy()
    n = fade.frames(12.0, SR)
    fade.fade_out(faded[: i], n)
    faded = np.concatenate([faded[:i], np.zeros(SR // 4, np.float32), x[i:]])
    fade.fade_in(faded[i + SR // 4 :], n)
    assert m.hard_edges(cut, SR) == 2 and m.hard_edges(faded, SR) == 0


def test_threads_do_not_leak() -> None:
    before = threading.active_count()
    r = renderer()
    r.close()
    assert threading.active_count() <= before


@pytest.fixture
def _switch_interval() -> Any:
    """A real output sets a 1 ms GIL switch interval process-wide; put the default back after the test."""
    import sys as _sys

    before = _sys.getswitchinterval()
    yield
    _sys.setswitchinterval(before)


class _FakeStream:
    def __init__(self, **kw: Any) -> None:
        self.kw = kw
        self.samplerate = float(kw["samplerate"])
        self.latency = float(kw["latency"])
        self.active = False
        self.closed = False
        self.written: list[np.ndarray] = []

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True

    def write(self, buf: np.ndarray) -> bool:
        self.written.append(buf.copy())
        time.sleep(0.005)
        return False


class _FakeSd:
    """Stands in for `sounddevice`: records the streams the output would open (no device is touched)."""

    def __init__(self, rate: int = 48000) -> None:
        self.rate = rate
        self.streams: list[_FakeStream] = []
        self.reinit = 0

    def query_devices(self, device: Any = None, kind: str | None = None) -> dict[str, Any]:
        return {"name": "Fake Speakers", "max_output_channels": 2, "default_samplerate": float(self.rate)}

    def OutputStream(self, **kw: Any) -> _FakeStream:  # noqa: N802 — sounddevice's name
        s = _FakeStream(**kw)
        self.streams.append(s)
        return s

    def _terminate(self) -> None:
        self.reinit += 1

    def _initialize(self) -> None:
        pass


def test_device_output_opens_at_the_native_rate_and_reopens_on_device_change(
    monkeypatch: pytest.MonkeyPatch, _switch_interval: None
) -> None:
    import sys as _sys

    from open_speech.playback.diagnostics import AudioDiagnostics
    from open_speech.playback.output import DEFAULT_LATENCY_S

    fake = _FakeSd(48000)
    monkeypatch.setitem(_sys.modules, "sounddevice", fake)
    d = AudioDiagnostics()
    out = SoundDeviceOutput(diagnostics=d)
    device = {"now": (71, 48000.0)}
    out._ca.default_output = lambda: device["now"]  # type: ignore[method-assign]
    r = Renderer(SR, rate=out.preferred_rate())
    assert out.preferred_rate() == 48000
    out.start(48000, r.pull)
    s = fake.streams[-1]
    assert s.kw["samplerate"] == 48000 and s.kw["channels"] == 2 and s.kw["latency"] == DEFAULT_LATENCY_S
    assert s.kw["callback"] is not None and s.active
    assert out.latency_frames == int(round(DEFAULT_LATENCY_S * 48000)) and d.device == "Fake Speakers"
    assert out.check() is False  # nothing changed
    device["now"] = (93, 24000.0)  # AirPods in headset mode
    fake.rate = 24000
    out._last_check = 0.0
    assert out.check() is True and out.rate == 24000
    assert s.closed and fake.reinit == 1 and d.device_reopens == 1 and fake.streams[-1].active
    assert sys.getswitchinterval() <= 0.001
    out.close()
    r.close()


def test_blocking_mode_feeds_a_writer_thread(monkeypatch: pytest.MonkeyPatch, _switch_interval: None) -> None:
    import sys as _sys

    fake = _FakeSd(48000)
    monkeypatch.setitem(_sys.modules, "sounddevice", fake)
    out = SoundDeviceOutput(mode="blocking")
    out.start(48000, lambda n: np.full(n, 0.5, dtype=np.float32))
    s = fake.streams[-1]
    assert "callback" not in s.kw
    deadline = time.monotonic() + 2.0
    while len(s.written) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    out.close()
    assert len(s.written) >= 3 and s.written[0].shape[1] == 2 and np.all(s.written[0] == 0.5)


# ------------------------------------------------------------------------------------------- review regressions


def _first_audio_ramp(y: np.ndarray, after: int = 0) -> float:
    """Peak of the first millisecond of audio after a stretch of digital silence (0 = faded in from silence)."""
    nz = np.flatnonzero(y[after:])
    i = after + int(nz[0])
    return float(np.max(np.abs(y[i : i + SR // 1000])))


@pytest.mark.parametrize("how", ["seek_while_paused", "stop_then_play", "reopen_at_saved_offset"])
def test_starting_mid_utterance_with_nothing_to_crossfade_fades_in(how: str) -> None:
    x = sine(300.0, 3.0)
    r = renderer()
    if how == "reopen_at_saved_offset":
        r.flush(index=0, offset=SR)
        r.enqueue(RenderItem(0, x, SR))
        r.mark_end_of_stream()
        r.pause_reading(False)
        y = drain(r)
    else:
        r.enqueue(RenderItem(0, x))
        r.mark_end_of_stream()
        r.pause_reading(False)

        def halt() -> None:
            if how == "stop_then_play":
                r.pause_reading(True, fade_ms=fade.STOP_MS)
                r.flush(index=0, offset=0)
            else:
                r.pause_reading(True)

        def restart() -> None:
            r.flush(index=0, offset=SR)  # while paused: nothing audible to crossfade from
            r.enqueue(RenderItem(0, x, SR))
            r.mark_end_of_stream()
            r.pause_reading(False)

        y = drain(r, events={20: halt, 30: restart})
    r.close()
    assert m.hard_edges(y, SR) == 0 and m.clicks(y, SR).events == 0
    start = 30 * 480 if how != "reopen_at_saved_offset" else 0
    assert _first_audio_ramp(y, start) < 0.05  # rises from silence instead of starting at 0.3


def test_resuming_after_running_dry_mid_utterance_fades_in() -> None:
    from open_speech.playback.render import GrowingPcm

    x = sine(300.0, 2.0)
    pcm = GrowingPcm()
    pcm.append(x[: SR // 2])
    r = renderer()
    r.enqueue(RenderItem(0, pcm))
    r.mark_end_of_stream()
    r.pause_reading(False)
    out = []
    for _ in range(200):  # play until the growing utterance runs dry
        time.sleep(0.002)
        out.append(r.pull(480))
        if r.snapshot().starved:
            break
    assert r.snapshot().starved
    r.pause_reading(True)  # paused while starved …
    out.append(r.pull(480))
    pcm.append(x[SR // 2 :])
    pcm.finish()
    r.pause_reading(False)  # … and resumed with nothing buffered
    resumed = sum(o.size for o in out)
    out.append(drain(r))
    r.close()
    y = np.concatenate(out)
    assert m.hard_edges(y, SR) == 0
    assert _first_audio_ramp(y, resumed) < 0.05


def test_stop_just_after_a_stream_ran_dry_never_plays_at_full_level() -> None:
    r = renderer()
    s = r.open_stream("reply")
    s.append(_soft(sine(300, 0.1)))
    out = []
    for _ in range(200):
        time.sleep(0.002)
        out.append(r.pull(480))
        if s.starved:
            break
    assert s.starved
    s.append(sine(300, 1.0))  # the next sentence arrives …
    deadline = time.monotonic() + 1.0
    while s.ring.available == 0 and time.monotonic() < deadline:
        time.sleep(0.002)
    s.stop(15.0)  # … and the listener stops it before it is heard
    for _ in range(5):
        out.append(r.pull(480))
    r.close()
    y = np.concatenate(out)
    assert s.reason == "stopped" and m.hard_edges(y, SR) == 0
    assert s.played < int(0.15 * SR)


def test_a_device_rate_change_mid_render_keeps_the_pitch(monkeypatch: pytest.MonkeyPatch) -> None:
    from open_speech.playback import render as render_mod

    entered, release = threading.Event(), threading.Event()
    armed = {"on": False}
    real_push = render_mod._Resampler.push

    def gated_push(self: Any, x: np.ndarray, *, last: bool = False) -> np.ndarray:
        y = real_push(self, x, last=last)
        if armed["on"] and x.size:  # hold one resampled chunk outside the lock while the device rate changes
            armed["on"] = False
            entered.set()
            release.wait(2.0)
        return y

    monkeypatch.setattr(render_mod._Resampler, "push", gated_push)

    x = sine(400.0, 2.0)
    r = Renderer(SR, rate=SR)
    r.start()
    r.set_speed(1.5)
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    while r.buffered_frames() < r.ahead_frames:
        time.sleep(0.002)
    armed["on"] = True
    for _ in range(200):
        r.pull(240)
        if entered.wait(0.005):
            break
    assert entered.is_set()
    r.configure(48000)
    release.set()
    out = []
    for _ in range(2000):
        if r.snapshot().finished:
            break
        deadline = time.monotonic() + 1.0
        while r.buffered_frames() < 960 and not r.rendering_done() and time.monotonic() < deadline:
            time.sleep(0.001)
        out.append(r.pull(960))
    r.close()
    y = np.concatenate(out).astype(np.float64)
    # Zero-crossing rate in 20 ms windows of the audio after the switch: a chunk prepared at 24 kHz and written
    # as 48 kHz would play twice as fast and read as 800 Hz here.
    w = 960
    freqs = []
    for i in range(0, y.size - w, w):
        seg = y[i : i + w]
        if np.max(np.abs(seg)) < 0.2:
            continue
        zc = np.count_nonzero(np.signbit(seg[1:]) != np.signbit(seg[:-1]))
        freqs.append(zc / 2 / (w / 48000))
    assert freqs and min(freqs) > 360 and max(freqs) < 440


def test_a_streams_resampler_tail_comes_out_before_it_runs_dry() -> None:
    r = Renderer(SR, rate=48000)
    r.start()
    s = r.open_stream("reply")
    s.append(_soft(sine(300, 0.2)))  # 5280 source samples (with padding): 10560 frames at 48 kHz, ~500 held back
    total = 0
    for _ in range(400):
        time.sleep(0.002)
        r.pull(480)
        total += 480
        if s.starved:
            break
    r.close()
    assert abs(s.played - 10560) < 40, s.played  # the held-back end played in time, not glued to the next piece


async def test_stopping_speech_that_already_finished_reports_finished(
    rt: Runtime, settings: SettingsBox, events: EventBus, captured: list[dict[str, Any]]
) -> None:
    settings.set("voice_input.echo_tail_ms", 400)
    t = await _transport(rt, settings, events, time_scale=8.0)
    h = t.open_speech("preview")
    h.append(_soft(sine(300, 0.2)))
    h.finish()
    for _ in range(300):
        if h.stream.ended:
            break
        await asyncio.sleep(0.01)
    assert h.stream.ended and not h.ended  # played out; only the echo tail is left
    h.stop(reason="superseded")
    reason = await asyncio.wait_for(h.wait(), 3.0)
    await t.close()
    assert reason == "finished" and _speech(captured)[-1]["reason"] == "finished"


def test_device_output_prepares_the_renderer_before_the_stream_starts(
    monkeypatch: pytest.MonkeyPatch, _switch_interval: None
) -> None:
    fake = _FakeSd(44100)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    out = SoundDeviceOutput()
    seen: list[tuple[int, int, bool]] = []
    out.prepare = lambda rate, lat: seen.append((rate, lat, any(st.active for st in fake.streams)))
    out._ca.default_output = lambda: (1, 44100.0)  # type: ignore[method-assign]
    out.start(44100, lambda n: np.zeros(n, dtype=np.float32))
    assert seen == [(44100, int(round(0.1 * 44100)), False)]  # configured while no stream was running yet
    out.close()


def test_device_output_reopens_on_start_after_an_idle_device_change(
    monkeypatch: pytest.MonkeyPatch, _switch_interval: None
) -> None:
    fake = _FakeSd(48000)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    out = SoundDeviceOutput()
    device = {"now": (71, 48000.0)}
    out._ca.default_output = lambda: device["now"]  # type: ignore[method-assign]
    rates: list[int] = []
    out.prepare = lambda rate, _lat: rates.append(rate)
    out.start(48000, lambda n: np.zeros(n, dtype=np.float32))
    out.stop()
    first = fake.streams[-1]
    device["now"] = (93, 24000.0)  # headphones connected while nothing played
    fake.rate = 24000
    out.start(48000, lambda n: np.zeros(n, dtype=np.float32))
    assert first.closed and fake.reinit == 1 and len(fake.streams) == 2
    assert out.rate == 24000 and rates == [48000, 24000] and fake.streams[-1].active
    # A stream that can't restart is replaced rather than raising out of play().
    out.stop()

    def broken() -> None:
        raise RuntimeError("PortAudio error -9986: device unavailable")

    fake.streams[-1].start = broken  # type: ignore[method-assign]
    out.start(48000, lambda n: np.zeros(n, dtype=np.float32))
    assert len(fake.streams) == 3 and fake.streams[-1].active
    out.close()
