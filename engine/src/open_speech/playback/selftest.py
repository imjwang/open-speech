"""`open-speech doctor audio`: check the playback chain end to end without playing a sound.

Renders speech (the local voice when it is installed, else a synthetic voice-like signal) through the real
`Renderer` at the output device's native rate into memory, and checks what the listener would hear: 1x is
bit-exact, the resampler adds no images, speeds are accurate, pause/resume/seek/stop have no hard edges or
clicks, and an audio callback at the configured latency meets its deadlines while the local voice synthesizes.
The device is only *queried* (name, native rate); no stream is opened and the microphone is never touched.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from open_speech.playback import metrics as m
from open_speech.playback.output import DEFAULT_LATENCY_S, DEFAULT_MODE, DeadlineOutput, use_audio_switch_interval
from open_speech.playback.render import SOURCE_RATE, Renderer, RenderItem

TEST_TEXT = "Before you begin, read every question carefully; some of them have more than one correct answer."


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Report:
    device: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(Check(name, ok, detail))


def device_facts() -> dict[str, Any]:
    out: dict[str, Any] = {"latency_ms": DEFAULT_LATENCY_S * 1000, "mode": DEFAULT_MODE}
    try:
        import sounddevice as sd

        info = sd.query_devices(kind="output")
        out.update(name=info["name"], native_rate=int(info["default_samplerate"]),
                   default_low_latency_ms=round(1000 * float(info["default_low_output_latency"]), 1))
    except Exception as exc:  # noqa: BLE001
        out.update(name=None, native_rate=None, error=str(exc))
    return out


def synthetic_voice(seconds: float = 4.0, sr: int = SOURCE_RATE) -> np.ndarray:
    """A voice-like test signal: a harmonic series (F0 140 Hz with 4.5 Hz vibrato) shaped into smooth syllables
    with pauses between words. Starts and ends in silence, with no abrupt onsets of its own."""
    t = np.arange(int(seconds * sr)) / sr
    f0 = 140 * (1 + 0.04 * np.sin(2 * np.pi * 4.5 * t))
    ph = 2 * np.pi * np.cumsum(f0) / sr
    x = sum((0.5 / k) * np.sin(k * ph) for k in range(1, 30))
    syllables = np.sin(np.pi * 3.0 * t) ** 2  # raised-cosine bumps, 3 per second
    word = np.clip((np.sin(2 * np.pi * t / 1.3 + 1.2) + 0.35) * 3, 0, 1) ** 2  # smooth pauses between words
    env = syllables * word
    n = min(t.size, int(0.02 * sr))
    env[:n] *= np.linspace(0, 1, n) ** 2
    env[-n:] *= np.linspace(1, 0, n) ** 2
    return (0.25 * x * env).astype(np.float32)


def speech_source() -> tuple[np.ndarray, str, Any]:
    """Local-voice speech when available (and the voice, for load during the deadline check)."""
    try:
        from open_speech.config import EngineEnv, Paths
        from open_speech.local.catalog import ModelCatalog
        from open_speech.providers.kokoro import KokoroTts

        paths = Paths(EngineEnv().home)
        tts = KokoroTts(ModelCatalog.load(paths), paths)
        model = tts.default_model()
        if tts.unavailable_reason(model, "af_heart") is None:
            pcm, _ = tts.synthesize_sync(TEST_TEXT, model, "af_heart")
            return np.asarray(pcm, dtype=np.float32), "local voice (Kokoro af_heart)", tts
    except Exception:  # noqa: BLE001
        pass
    return synthetic_voice(), "synthetic voice-like signal (the local voice isn't installed)", None


def render_offline(x: np.ndarray, *, rate: int, speed: float = 1.0, events: list[tuple[float, str, int]] | None = None,
                   block: int = 512) -> tuple[np.ndarray, list[int]]:
    """Pull everything the renderer produces for `x`, the way an output would, without a clock."""
    r = Renderer(SOURCE_RATE, rate=rate)
    r.set_speed(speed)
    r.start()
    r.enqueue(RenderItem(0, x))
    r.mark_end_of_stream()
    r.pause_reading(False)
    pending = sorted(events or [])
    out: list[np.ndarray] = []
    at: list[int] = []
    pulled = 0
    waited = 0.0
    idle_until: int | None = None
    try:
        while pulled < rate * 60:
            while pending and pulled >= pending[0][0] * rate:
                _t, action, offset = pending.pop(0)
                at.append(pulled)
                if action == "pause":
                    r.pause_reading(True)
                elif action == "resume":
                    r.pause_reading(False)
                elif action in ("seek", "stop"):
                    r.flush(index=0, offset=offset)
                    if action == "seek":
                        r.enqueue(RenderItem(0, x, offset))
                        r.mark_end_of_stream()
                    else:
                        r.pause_reading(True, fade_ms=15.0)
            if r.snapshot().finished and not pending:
                break
            if not r.reading_paused and r.buffered_frames() < block and waited < 0.3:
                time.sleep(0.0005)
                waited += 0.0005
                continue
            waited = 0.0
            out.append(r.pull(block))
            pulled += block
            if r.reading_paused and not pending:
                idle_until = idle_until or pulled + rate // 10
                if pulled >= idle_until:
                    break
    finally:
        r.close()
    return (np.concatenate(out) if out else np.zeros(0, dtype=np.float32)), at


def run(*, deadline_s: float = 5.0) -> Report:
    rep = Report(device=device_facts())
    x, rep.source, tts = speech_source()
    native = int(rep.device.get("native_rate") or SOURCE_RATE)

    y, _ = render_offline(x, rate=SOURCE_RATE)
    exact = y.size >= x.size and np.array_equal(y[: x.size], x)
    rep.add("1x is bit-exact (24 kHz)", exact, "output == input" if exact else "output differs from the input")

    if native != SOURCE_RATE:
        y, _ = render_offline(x, rate=native)
        spec = np.abs(np.fft.rfft(y.astype(np.float64))) ** 2
        f = np.fft.rfftfreq(y.size, 1 / native)
        img = spec[f > 12500].sum() / max(spec[f <= 11500].sum(), 1e-20)
        img_db = 10 * np.log10(max(img, 1e-20))
        rep.add(f"resampling to {native} Hz adds no images", img_db < -70, f"energy above 12.5 kHz: {img_db:.0f} dB")
        lerr = abs(y.size / native - x.size / SOURCE_RATE)
        rep.add("resampled length", lerr < 0.02, f"{1000 * lerr:.1f} ms off")

    for speed in (1.5, 2.0, 3.0):
        y, _ = render_offline(x, rate=SOURCE_RATE, speed=speed)
        err = abs(y.size * speed / x.size - 1)
        c = m.clicks(y, SOURCE_RATE).events
        ok = err < 0.02 and c == 0
        rep.add(f"{speed:g}x: length and clicks", ok, f"length off {100 * err:.2f} %, {c} clicks")

    dur = x.size / SOURCE_RATE
    events = [(dur * 0.25, "pause", 0), (dur * 0.35, "resume", 0),
              (dur * 0.5, "seek", int(x.size * 0.1)), (dur * 0.65, "stop", 0)]
    y, _ = render_offline(x, rate=native, events=events)
    edges = m.hard_edges(y, native)
    c = m.clicks(y, native).events
    rep.add("pause/resume/seek/stop are faded", edges == 0 and c == 0, f"{edges} hard edges, {c} clicks")

    rep.add(*_deadline_check(x, native, tts, deadline_s))
    return rep


def _deadline_check(x: np.ndarray, rate: int, tts: Any, seconds: float) -> tuple[str, bool, str]:
    use_audio_switch_interval()  # as a real output would
    r = Renderer(SOURCE_RATE, rate=rate)
    r.set_speed(1.5)
    r.start()
    r.pause_reading(False)
    stop = threading.Event()

    def feed() -> None:
        i = 0
        while not stop.is_set():
            if len(r.queued_indices()) < 2:
                r.enqueue(RenderItem(i, x))
                i += 1
            stop.wait(0.05)

    def load() -> None:
        if tts is None:
            return
        model = tts.default_model()
        while not stop.is_set():
            tts.synthesize_sync(TEST_TEXT, model, "af_heart")

    threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=load, daemon=True)]
    for t in threads:
        t.start()
    out = DeadlineOutput(period_s=DEFAULT_LATENCY_S, mode=DEFAULT_MODE)
    out.start(rate, r.pull)
    time.sleep(seconds)
    out.stop()
    stop.set()
    for t in threads:
        t.join(timeout=30)
    r.close()
    st = out.stats()
    starved = r.diagnostics.render_starvations
    ok = st["misses"] == 0 and starved == 0
    load_note = "with the local voice synthesizing" if tts is not None else "without synthesis load"
    return (f"audio callback deadlines at {DEFAULT_LATENCY_S * 1000:g} ms ({DEFAULT_MODE})", ok,
            f"{int(st['misses'])} misses in {int(st['calls'])} callbacks, worst {st['max_ms']:.1f} ms late, "
            f"{starved} render starvations, {seconds:g} s {load_note}")
