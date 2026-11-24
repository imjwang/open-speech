"""Render thread: everything the listener hears is prepared here, at the output device's rate, before the audio
callback asks for it (05 § 9).

Channels
- **reading**: queued utterance PCM (24 kHz) → time-stretch at the current speed (`stretch.Stretcher`, a
  bit-exact bypass at 1x) → resampled to the device rate (soxr, VHQ) → the reading ring (~0.3 s ahead).
- **streams** (`ClipStream`): replies, spoken notices, previews and recordings. PCM is appended in pieces and
  played gapless in order, never stretched; each stream has its own ring. A reply (workstream 3) is one stream
  whose sentences are appended as they are synthesized.

Threads and locks
- The loop calls the control methods (`enqueue`, `flush`, `set_speed`, `pause_reading`, `open_stream`, …):
  bookkeeping under `_lock` for microseconds.
- The render thread stretches and resamples *outside* the lock and copies finished audio into a ring under it.
- The audio thread calls `pull(n)` / `pull_into(buf)`: copy out of the rings, apply gain ramps, sum, soft-limit,
  all into preallocated buffers; it never waits on the loop, never allocates in steady state, and only counts
  things (`diagnostics`).

Every cut is faded (`fade.py`): pause/duck/resume ramp the reading gain in `pull` (12 ms); seek, skip, stop and
voice changes fade the old reading audio out inside the ring while the new audio fades in; a speed change
re-renders from the playhead with a 10 ms crossfade aligned by cross-correlation, so it is heard within ~50 ms
instead of after the whole ring; a stream that runs dry fades over 5 ms at either edge.

Positions are latency-corrected: `snapshot()` reports what is *audible now*, i.e. the reading frame the output
consumed `output_latency_frames` ago (from a short history of pulls), mapped through per-hop markers to
(utterance index, source sample). The same clock times `assistant.speech` started/ended (`clock()`).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from open_speech.playback import fade
from open_speech.playback.diagnostics import AudioDiagnostics
from open_speech.playback.stretch import Stretcher, clamp_speed

log = logging.getLogger(__name__)

SOURCE_RATE = 24000
LIMIT_KNEE = 0.89  # soft limiter: linear below -1 dBFS, smoothly saturating above (never a hard clip)
HISTORY = 1024


class GrowingPcm:
    """An utterance still being synthesized (a streaming local voice, Qwen3): the loop appends pieces as they
    arrive, the render thread reads up to `size`, and the item ends only once `complete` is set. Reads see either
    the old or the new buffer, never a half-written sample: data is written before `size` moves past it, and a
    full buffer is replaced, never changed in place below `size`."""

    MIN_PIECE = 1024  # render-thread steps (source samples) worth taking while more is still coming

    def __init__(self, capacity: int = SOURCE_RATE * 6, on_change: Callable[[], None] | None = None) -> None:
        self._buf = np.zeros(max(1024, capacity), dtype=np.float32)
        self._n = 0
        self.complete = False
        self._on_change = on_change

    @property
    def size(self) -> int:
        return self._n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, key: Any) -> np.ndarray:
        out: np.ndarray = self._buf[: self._n][key]
        return out

    def append(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size == 0 or self.complete:
            return
        n = self._n
        if n + x.size > self._buf.size:
            grown = np.zeros(max(self._buf.size * 2, n + x.size), dtype=np.float32)
            grown[:n] = self._buf[:n]
            self._buf = grown
        self._buf[n : n + x.size] = x
        self._n = n + x.size
        if self._on_change is not None:
            self._on_change()

    def finish(self) -> None:
        self.complete = True
        if self._on_change is not None:
            self._on_change()

    def array(self) -> np.ndarray:
        return self._buf[: self._n]


def _complete(item: RenderItem) -> bool:
    return bool(getattr(item.pcm, "complete", True))


@dataclass
class RenderItem:
    index: int  # utterance index
    pcm: np.ndarray | GrowingPcm  # mono float32 at 24 kHz (a `GrowingPcm` while it is still being synthesized)
    start: int = 0  # first source sample to play (seek offset)


@dataclass
class PlayPosition:
    index: int | None
    offset: int  # source samples (24 kHz) into the utterance
    starved: bool
    finished: bool
    ended: list[int]  # utterances whose last sample became audible since the last snapshot


@dataclass
class _Mark:
    start: int  # absolute reading-ring frame
    end: int
    index: int | None
    src_a: float = 0.0
    src_b: float = 0.0
    kind: str = "audio"  # "audio" | "end" (end of utterance `index`) | "eos"


class _ItemState:
    """Render-thread state for the utterance being rendered (a fresh stretcher and resampler per utterance, so a
    flush never races with a half-finished job: the job's state is simply dropped)."""

    def __init__(self, item: RenderItem, stretcher: Any, resampler: _Resampler) -> None:
        self.item = item
        self.stretcher = stretcher
        self.resampler = resampler
        self.hold = np.zeros(0, dtype=np.float32)  # last 3 ms held back for the end fade
        self.hold_segs: list[tuple[int, float, float]] = []
        self.emitted = False  # placed in the ring yet (base chosen)
        self.out24 = 0.0  # 24 kHz output produced (marker placement)
        self.base = 0  # ring frame where this utterance's audio starts
        self.written = 0  # device frames written
        self.rate = resampler.rate  # device rate that `base` and `written` are counted in
        self.fade_len = 0  # crossfade-in over the old audio's fade-out (after a flush / speed restart) …
        self.fade_pos = 0  # … and how much of it has been applied (it can span several chunks)


class _Ring:
    """Float32 ring at the device rate with absolute frame counters (`w` written, `r` read). Callers hold the
    renderer's lock."""

    def __init__(self, capacity: int) -> None:
        self.buf = np.zeros(capacity, dtype=np.float32)
        self.cap = capacity
        self.w = 0
        self.r = 0

    @property
    def available(self) -> int:
        return self.w - self.r

    def _idx(self, a: int, n: int) -> tuple[slice, slice | None, int]:
        s = a % self.cap
        if s + n <= self.cap:
            return slice(s, s + n), None, n
        k = self.cap - s
        return slice(s, self.cap), slice(0, n - k), k

    def grow(self, need: int) -> None:
        if need <= self.cap:
            return
        cap = self.cap
        while cap < need:
            cap *= 2
        data = self.view(self.r, self.w)
        self.buf = np.zeros(cap, dtype=np.float32)
        self.cap = cap
        a, b, k = self._idx(self.r, data.size)
        self.buf[a] = data[:k]
        if b is not None:
            self.buf[b] = data[k:]

    def write_at(self, a: int, x: np.ndarray, *, mix: bool = False) -> None:
        """Write (or add, for the overlap of a crossfade) `x` at absolute frame `a` (r <= a <= w)."""
        n = x.size
        if n == 0:
            return
        end = a + n
        self.grow(end - self.r)
        overlap = max(0, min(end, self.w) - a) if mix else 0
        if overlap:
            sa, sb, k = self._idx(a, overlap)
            self.buf[sa] += x[:k]
            if sb is not None:
                self.buf[sb] += x[k:overlap]
        rest = x[overlap:]
        if rest.size:
            sa, sb, k = self._idx(a + overlap, rest.size)
            self.buf[sa] = rest[:k]
            if sb is not None:
                self.buf[sb] = rest[k:]
        self.w = max(self.w, end)

    def read_into(self, out: np.ndarray, n: int) -> int:
        k = min(n, self.available)
        if k:
            sa, sb, j = self._idx(self.r, k)
            out[:j] = self.buf[sa]
            if sb is not None:
                out[j:k] = self.buf[sb]
            self.r += k
        return k

    def view(self, a: int, b: int) -> np.ndarray:
        a, b = max(a, self.r), min(b, self.w)
        if b <= a:
            return np.zeros(0, dtype=np.float32)
        sa, sb, k = self._idx(a, b - a)
        if sb is None:
            return self.buf[sa].copy()
        return np.concatenate([self.buf[sa], self.buf[sb]])

    def scale(self, a: int, gains: np.ndarray) -> None:
        a = max(a, self.r)
        n = min(gains.size, self.w - a)
        if n <= 0:
            return
        sa, sb, k = self._idx(a, n)
        self.buf[sa] *= gains[:k]
        if sb is not None:
            self.buf[sb] *= gains[k:n]

    def truncate(self, w: int) -> None:
        self.w = max(self.r, min(self.w, w))

    def clear(self) -> None:
        self.w = self.r


class _Gain:
    """A gain that moves along a raised-cosine ramp (pause/resume, stream stop, starvation edges)."""

    __slots__ = ("cur", "src", "dst", "pos", "length")

    def __init__(self, value: float = 1.0) -> None:
        self.cur = value
        self.src = value
        self.dst = value
        self.pos = 0
        self.length = 0

    def to(self, target: float, n: int) -> None:
        if target == self.dst and (self.length == 0 or self.pos < self.length):
            return
        self.src = self.cur
        self.dst = target
        self.pos = 0
        self.length = max(1, n)

    def snap(self, value: float) -> None:
        self.cur = self.src = self.dst = value
        self.length = 0

    @property
    def steady(self) -> bool:
        return self.length == 0 or self.pos >= self.length

    def apply(self, x: np.ndarray, scratch: np.ndarray) -> None:
        """Multiply `x` in place by the gain over its length (advancing any ramp)."""
        n = x.size
        if self.steady:
            if self.cur != 1.0:
                x *= self.cur
            return
        k = min(n, self.length - self.pos)
        r = fade.ramp(self.length)[self.pos : self.pos + k]
        g = scratch[:k]
        np.multiply(r, self.dst - self.src, out=g)
        g += self.src
        x[:k] *= g
        self.pos += k
        self.cur = float(g[k - 1]) if k else self.cur
        if self.pos >= self.length:
            self.cur = self.dst
            self.length = 0
            if k < n and self.cur != 1.0:
                x[k:] *= self.cur


class _Resampler:  # noqa: D101 (defined before use by _ItemState via annotations)
    """24 kHz → device rate, streaming (python-soxr, VHQ). Identity when the rates match."""

    def __init__(self, rate: int) -> None:
        self.rate = rate
        self._rs: Any = None
        if rate != SOURCE_RATE:
            import soxr

            self._rs = soxr.ResampleStream(SOURCE_RATE, rate, 1, dtype="float32", quality="VHQ")

    def push(self, x: np.ndarray, *, last: bool = False) -> np.ndarray:
        if self._rs is None:
            return np.asarray(x, dtype=np.float32)
        y = self._rs.resample_chunk(np.asarray(x, dtype=np.float32), last=last)
        return np.asarray(y, dtype=np.float32).reshape(-1)


class ClipStream:
    """One thing being said: gapless PCM appended in order, played once, never stretched (previews, recordings,
    `/assistant/say`, replies; workstream 3 appends a reply sentence by sentence). Create with
    `Renderer.open_stream()`. Thread-safe; `append`/`finish`/`stop` never block on the audio thread.

    Timing (device frames on the renderer's clock, see `Renderer.clock()`): `first_frame` is the frame at which
    the first sample was handed to the output, `last_frame` the frame after the last one (or after the stop fade).
    Add the output latency for when the listener hears them.
    """

    def __init__(self, renderer: Renderer, kind: str, rate: int) -> None:
        self._r = renderer
        self.kind = kind
        self.pending: deque[np.ndarray] = deque()  # 24 kHz pieces not yet resampled into the ring
        self.ring = _Ring(max(4096, rate))
        self.resampler = _Resampler(rate)
        self.gain = _Gain(1.0)
        self.finished = False  # no more audio will be appended
        self.fed_all = False  # every appended sample is in the ring (resampler drained)
        self.stopping = False
        self.ended = False
        self.reason: str | None = None
        self.first_frame: int | None = None
        self.last_frame: int | None = None
        self.appended = 0  # 24 kHz samples appended
        self.played = 0  # device frames handed to the output
        self.starved = False
        self.edge_started = False  # the 3 ms start fade has been considered
        self.tail_held = False  # the resampler holds back the end of what was appended (flushed when running low)
        self.done = threading.Event()

    # -------------------------------------------------------------- loop side

    def append(self, pcm: np.ndarray) -> None:
        """Queue 24 kHz mono float32 to play right after what was appended before."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        with self._r._cond:
            if self.finished or self.stopping or self.ended:
                return
            self.pending.append(x.copy())
            self.appended += x.size
            self._r._cond.notify_all()

    def finish(self) -> None:
        """Nothing more will be appended: the stream ends once its last sample has played."""
        with self._r._cond:
            self.finished = True
            self._r._cond.notify_all()

    def stop(self, fade_ms: float = fade.STOP_MS) -> None:
        """Fade out over `fade_ms` and end; the rest is dropped."""
        with self._r._cond:
            if self.ended or self.stopping:
                return
            self.stopping = True
            self.finished = True
            self.pending.clear()
            if self.first_frame is None or self.starved:  # silent right now (not started, or ran dry): end at once
                self.ring.clear()
                self._r._end_stream(self, "stopped")
            else:
                self.gain.to(0.0, fade.frames(fade_ms, self._r.rate))
            self._r._cond.notify_all()

    @property
    def played_ms(self) -> int:
        return int(round(1000 * self.played / max(1, self._r.rate)))

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


class Renderer:
    def __init__(
        self,
        sample_rate: int = SOURCE_RATE,
        *,
        rate: int | None = None,
        ahead_s: float = 0.3,
        stretcher_factory: Callable[..., Any] | None = None,
        diagnostics: AudioDiagnostics | None = None,
    ) -> None:
        self.source_rate = sample_rate  # rate of the PCM handed in (24 kHz)
        self.rate = rate or sample_rate  # rate of what pull() returns (the device's)
        self.ahead_s = ahead_s
        self._stretcher_factory = stretcher_factory or Stretcher
        self.diagnostics = diagnostics or AudioDiagnostics()
        self.diagnostics.rate = self.rate
        self.diagnostics.resampler = "soxr VHQ" if self.rate != SOURCE_RATE else "none"
        self.output_latency_frames = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._thread: threading.Thread | None = None
        self._speed = 1.0
        self._gen = 0
        # reading channel (render-thread side)
        self._queue: deque[RenderItem] = deque()
        self._cur: RenderItem | None = None
        self._cur_pos = 0
        self._state: _ItemState | None = None
        self._inflight: list[RenderItem] = []  # recent items whose audio may still be in the ring (render order)
        self._eos_requested = False
        self._eos_sent = False
        self._eos_frame: int | None = None
        self._speed_restart = False
        self._xfade_at: int | None = None  # next write mixes in here with a fade-in (after a flush)
        self._xfade_len = 0
        # reading channel (shared with the audio thread)
        self._ring = _Ring(max(8192, int(self.rate * 1.5)))
        self._marks: deque[_Mark] = deque()
        self._read_gain = _Gain(0.0)
        self._paused = True
        self._starved = False
        self._was_starved = False
        self._finished = False
        self._pos_index: int | None = None
        self._pos_offset = 0
        self._reported_end = -1  # ring frame up to which "end"/"eos" marks were reported
        # streams
        self._streams: list[ClipStream] = []
        # clock (audio thread writes, loop reads)
        self._dev_frames = 0
        self._hist_dev = np.zeros(HISTORY, dtype=np.int64)
        self._hist_read = np.zeros(HISTORY, dtype=np.int64)
        self._hist_i = 0
        self._scratch = np.zeros(16384, dtype=np.float32)
        self._chan = np.zeros(16384, dtype=np.float32)
        self._gain_buf = np.zeros(16384, dtype=np.float32)

    # ------------------------------------------------------------------ lifecycle / configuration

    @property
    def ahead_frames(self) -> int:
        return int(self.ahead_s * self.rate)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="open-speech-render", daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            for s in self._streams:
                s.ended = True
                s.reason = s.reason or "stopped"
                s.done.set()
            self._streams.clear()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def configure(self, rate: int) -> None:
        """Change the output rate (device opened or switched). Unplayed audio is resampled to the new rate so
        nothing is lost or repeated."""
        with self._cond:
            if rate == self.rate:
                return
            old = self.rate
            ratio = rate / old

            def convert(ring: _Ring) -> _Ring:
                data = ring.view(ring.r, ring.w)
                new = _Ring(max(8192, int(rate * 1.5)))
                new.r = new.w = int(round(ring.r * ratio))
                if data.size:
                    import soxr

                    new.write_at(new.r, np.asarray(soxr.resample(data, old, rate, quality="VHQ"), dtype=np.float32))
                return new

            self._ring = convert(self._ring)
            for m in self._marks:
                m.start = int(round(m.start * ratio))
                m.end = int(round(m.end * ratio))
            if self._state is not None:
                self._state.base = int(round(self._state.base * ratio))
                self._state.written = int(round(self._state.written * ratio))
                self._state.resampler = _Resampler(rate)
                self._state.rate = rate
            if self._eos_frame is not None:
                self._eos_frame = int(round(self._eos_frame * ratio))
            if self._xfade_at is not None:
                self._xfade_at = int(round(self._xfade_at * ratio))
                self._xfade_len = int(round(self._xfade_len * ratio))
            self._reported_end = int(round(self._reported_end * ratio))
            for s in self._streams:
                s.ring = convert(s.ring)
                s.resampler = _Resampler(rate)
                s.tail_held = False
                if s.first_frame is not None:
                    s.first_frame = int(round(s.first_frame * ratio))
                if s.last_frame is not None:
                    s.last_frame = int(round(s.last_frame * ratio))
                s.played = int(round(s.played * ratio))
            self._dev_frames = int(round(self._dev_frames * ratio))
            self._hist_dev = (self._hist_dev * ratio).astype(np.int64)
            self._hist_read = (self._hist_read * ratio).astype(np.int64)
            self.rate = rate
            self.diagnostics.rate = rate
            self.diagnostics.resampler = "soxr VHQ" if rate != SOURCE_RATE else "none"
            self._cond.notify_all()

    def set_output_latency(self, frames: int) -> None:
        self.output_latency_frames = max(0, int(frames))

    # ------------------------------------------------------------------ control (loop side): reading

    @property
    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        s = clamp_speed(speed)
        with self._cond:
            if s == self._speed:
                return
            self._speed = s
            self._speed_restart = True
            self._cond.notify_all()

    def enqueue(self, item: RenderItem) -> None:
        with self._cond:
            self._queue.append(item)
            self._finished = False
            self._cond.notify_all()

    def notify(self) -> None:
        """More audio arrived for a growing utterance: wake the render thread."""
        with self._cond:
            self._cond.notify_all()

    def mark_end_of_stream(self) -> None:
        with self._cond:
            self._eos_requested = True
            self._cond.notify_all()

    def flush(self, *, index: int | None = None, offset: int = 0) -> None:
        """Drop everything queued and buffered for reading (seek/skip/stop/voice change); the position becomes
        (index, offset). Audio still playing fades out over 12 ms while the new audio, once rendered, fades in."""
        with self._cond:
            self._gen += 1
            self._queue.clear()
            self._cur = None
            self._cur_pos = 0
            self._state = None
            self._inflight.clear()
            self._speed_restart = False
            ring = self._ring
            r = ring.r
            g = self._read_gain
            self._xfade_at, self._xfade_len = None, 0
            if not self._paused and g.cur > 0 and ring.available:
                # Playing: the old audio fades out over 12 ms in the ring; the new audio will fade in over it.
                n = min(ring.available, fade.frames(fade.SEEK_MS, self.rate))
                ring.scale(r, fade.ramp(n)[::-1].copy())
                ring.truncate(r + n)
                self._xfade_at, self._xfade_len = r, n
            elif not g.steady and g.dst == 0.0:
                ring.truncate(r + (g.length - g.pos))  # a pause/stop fade is playing: keep what it still needs
            else:
                ring.clear()
            self._marks.clear()
            self._eos_requested = False
            self._eos_sent = False
            self._eos_frame = None
            self._finished = False
            self._starved = False
            self._was_starved = False
            self._pos_index = index
            self._pos_offset = offset
            self._reported_end = ring.w
            self._cond.notify_all()

    def pause_reading(self, paused: bool, *, fade_ms: float | None = None) -> None:
        with self._cond:
            if paused == self._paused:
                return
            self._paused = paused
            if not paused and self._ring.available == 0 and self._read_gain.cur == 0.0:
                # Nothing buffered to resume into: the next audio fades itself in. An utterance from its start has
                # its edge fade (1x stays bit-exact), one from a saved offset or a seek its 12 ms start fade
                # (`_render_job`), and audio that resumes after running dry mid-utterance its 5 ms starvation
                # fade-in (`_was_starved` is kept for that).
                self._read_gain.snap(1.0)
                return
            ms = fade_ms if fade_ms is not None else (fade.PAUSE_MS if paused else fade.RESUME_MS)
            self._read_gain.to(0.0 if paused else 1.0, fade.frames(ms, self.rate))
            self._cond.notify_all()

    @property
    def reading_paused(self) -> bool:
        return self._paused

    def queued_indices(self) -> list[int]:
        with self._lock:
            out = [self._cur.index] if self._cur is not None else []
            return out + [i.index for i in self._queue]

    def last_queued_index(self) -> int | None:
        with self._lock:
            if self._queue:
                return self._queue[-1].index
            return self._cur.index if self._cur is not None else None

    def buffered_frames(self) -> int:
        """Reading audio rendered ahead of the output (device frames)."""
        with self._lock:
            return self._ring.available

    def waiting_for_synthesis(self) -> bool:
        """Reading ran dry inside an utterance that is still being synthesized (a growing utterance outrun by the
        playhead): the listener hears nothing until more arrives."""
        with self._lock:
            return self._starved and self._cur is not None and not _complete(self._cur)

    def rendering_done(self) -> bool:
        """Everything handed in has been rendered (nothing queued, nothing held)."""
        with self._lock:
            return self._cur is None and not self._queue and (not self._eos_requested or self._eos_sent)

    # ------------------------------------------------------------------ control (loop side): streams

    def open_stream(self, kind: str = "clip") -> ClipStream:
        s = ClipStream(self, kind, self.rate)
        with self._cond:
            self._streams.append(s)
            self._cond.notify_all()
        return s

    def play_clip(self, pcm: np.ndarray, kind: str = "clip") -> threading.Event:
        """A whole clip as one stream (compatibility helper)."""
        s = self.open_stream(kind)
        s.append(pcm)
        s.finish()
        if s.appended == 0:
            with self._cond:
                self._end_stream(s, "finished")
        return s.done

    def stop_clips(self, fade_ms: float = fade.STOP_MS) -> None:
        for s in list(self._streams):
            s.stop(fade_ms)

    def has_clips(self) -> bool:
        with self._lock:
            return any(not s.ended for s in self._streams)

    def streams(self) -> list[ClipStream]:
        with self._lock:
            return list(self._streams)

    def _end_stream(self, s: ClipStream, reason: str) -> None:
        """Under the lock."""
        if s.ended:
            return
        s.ended = True
        s.reason = reason
        if s.last_frame is None:
            s.last_frame = self._dev_frames
        s.done.set()

    # ------------------------------------------------------------------ clock and positions (loop side)

    def clock(self) -> tuple[int, int]:
        """(device frames handed to the output so far, output latency in frames). A frame handed over at F is
        heard at about F + latency."""
        return self._dev_frames, self.output_latency_frames

    def quiet(self) -> bool:
        """Nothing audible is left to hand out (reading paused/finished and faded, no live stream)."""
        with self._lock:
            reading = not self._paused and self._ring.available > 0
            fading = self._read_gain.cur > 0 and not self._read_gain.steady
            return not reading and not fading and not any(not s.ended for s in self._streams)

    def _audible_read_frame(self) -> int:
        """Reading-ring frame being heard now (under the lock): the one handed out `output_latency_frames` ago,
        from the pull history (reading frames advance with device frames while reading plays)."""
        lat = self.output_latency_frames
        if lat <= 0 or self._hist_i == 0:
            return self._ring.r
        target = self._dev_frames - lat
        n = min(self._hist_i, HISTORY)
        idx = np.arange(self._hist_i - n, self._hist_i) % HISTORY
        dev = self._hist_dev[idx]
        rd = self._hist_read[idx]
        k = int(np.searchsorted(dev, target, side="right"))  # the pull that handed out device frame `target`
        if k == 0:
            return int(rd[0])
        if k >= n:
            return int(rd[-1])
        return int(rd[k - 1]) + min(int(target - dev[k - 1]), int(rd[k] - rd[k - 1]))

    def snapshot(self) -> PlayPosition:
        """What is audible now: utterance and source offset at the playhead, utterances that just ended, whether
        reading starved (waiting for audio) or finished (the end-of-stream mark is audible)."""
        with self._lock:
            frame = self._audible_read_frame()
            ended: list[int] = []
            marks = self._marks
            while marks:
                m = marks[0]
                if m.kind == "audio":
                    if m.end <= frame:
                        if m.index is not None:
                            self._pos_index, self._pos_offset = m.index, int(m.src_b)
                        marks.popleft()
                        continue
                    if m.start <= frame and m.index is not None:
                        frac = (frame - m.start) / (m.end - m.start) if m.end > m.start else 0.0
                        self._pos_index = m.index
                        self._pos_offset = int(m.src_a + (m.src_b - m.src_a) * frac)
                    break
                if m.start > frame:
                    break
                marks.popleft()
                if m.kind == "end" and m.index is not None:
                    ended.append(m.index)
                    self._pos_index, self._pos_offset = m.index, int(m.src_b)
                elif m.kind == "eos":
                    self._finished = True
            return PlayPosition(self._pos_index, self._pos_offset, self._starved and not self._finished,
                                self._finished, ended)

    # ------------------------------------------------------------------ audio thread

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        self.pull_into(out)
        return out

    def pull_into(self, out: np.ndarray) -> None:
        """Fill `out` (float32, 1-D) with the next frames: reading + streams, ramped, summed, soft-limited."""
        t0 = time.perf_counter()
        n = out.size
        if n > self._chan.size:  # an unusually large device buffer: grow once
            self._chan = np.zeros(n, dtype=np.float32)
            self._scratch = np.zeros(n, dtype=np.float32)
            self._gain_buf = np.zeros(n, dtype=np.float32)
        out[:] = 0.0
        chan = self._chan
        with self._lock:
            self._pull_reading(out, chan, n)
            for s in self._streams:
                if not s.ended:
                    self._pull_stream(s, out, chan, n)
            self._dev_frames += n
            i = self._hist_i % HISTORY
            self._hist_dev[i] = self._dev_frames
            self._hist_read[i] = self._ring.r
            self._hist_i += 1
            if self._ring.available < self.ahead_frames or any(
                    not s.ended and s.ring.available < self.ahead_frames for s in self._streams):
                self._cond.notify_all()
        self._limit(out, n)
        self.diagnostics.note_pull(n, (time.perf_counter() - t0) * 1000)

    def _pull_reading(self, out: np.ndarray, chan: np.ndarray, n: int) -> None:
        g = self._read_gain
        if g.cur == 0.0 and g.steady:
            return  # paused and faded: nothing consumed
        ring = self._ring
        if self._was_starved and ring.available:
            g.snap(0.0)  # coming back from starvation: 5 ms fade-in
            g.to(0.0 if self._paused else 1.0, fade.frames(fade.STARVE_MS, self.rate))
            self._was_starved = False
        want = n
        if not g.steady and g.dst == 0.0:
            want = min(n, g.length - g.pos)  # a pause fade consumes only the fade itself
        level = ring.available
        if not self._paused:
            self.diagnostics.note_ring(level)
        k = ring.read_into(chan, want)
        if k < want and not self._paused:
            ending = self._eos_frame is not None and ring.r >= self._eos_frame
            if not self._finished and not ending:
                # Nothing left to render, or the utterance being rendered is still being synthesized: synthesis
                # is behind. Mid-utterance (render thread late, or a growing utterance outrun) it fades out now
                # and back in when audio returns; between utterances their own edge fades do it.
                growing = self._cur is not None and not _complete(self._cur)
                waiting = (self._cur is None and not self._queue) or growing
                if k or not self._starved:  # audio was flowing and ran out: one starvation
                    if waiting:
                        self.diagnostics.synthesis_waits += 1
                    else:
                        self.diagnostics.render_starvations += 1
                self._starved = True
                if not waiting or growing:
                    # Ran dry mid-utterance (render thread late): 5 ms out now, 5 ms back in when audio returns.
                    # Waiting for synthesis happens between utterances, whose ends carry their own edge fades.
                    self._was_starved = True
                    fade.fade_out(chan[:k], fade.frames(fade.STARVE_MS, self.rate))
        elif k:
            self._starved = False
        if k:
            seg = chan[:k]
            g.apply(seg, self._gain_buf)
            out[:k] += seg
        elif not g.steady and g.dst == 0.0:
            g.snap(0.0)

    def _pull_stream(self, s: ClipStream, out: np.ndarray, chan: np.ndarray, n: int) -> None:
        g = s.gain
        if s.starved and s.ring.available:
            if not s.stopping:  # a stop's fade-out must not be replaced by the starvation fade-in
                g.snap(0.0)
                g.to(1.0, fade.frames(fade.STARVE_MS, self.rate))
            s.starved = False
        want = n
        if s.stopping and not g.steady:
            want = min(n, g.length - g.pos)
        k = s.ring.read_into(chan, want)
        if k:
            if s.first_frame is None:
                s.first_frame = self._dev_frames
            if k < want and not s.fed_all and not s.stopping:
                fade.fade_out(chan[:k], fade.frames(fade.STARVE_MS, self.rate))
            seg = chan[:k]
            g.apply(seg, self._gain_buf)
            out[:k] += seg
            s.played += k
        if s.stopping and (g.steady or k == 0):
            s.last_frame = self._dev_frames + k
            self._end_stream(s, "stopped")
        elif k < want and s.fed_all and s.ring.available == 0:
            s.last_frame = self._dev_frames + k
            self._end_stream(s, "finished")
        elif k < want and not s.fed_all and s.first_frame is not None and not s.starved:
            s.starved = True
            self.diagnostics.stream_starvations += 1

    def _limit(self, out: np.ndarray, n: int) -> None:
        """Soft limiter: transparent below -1 dBFS, a smooth tanh knee above (no hard clipping). Speech is
        normalized well below the knee, so the (allocating) knee path runs only on an actual overload."""
        if n == 0:
            return
        s = self._scratch[:n]
        np.abs(out, out=s)
        if float(s.max()) <= LIMIT_KNEE:
            return
        t = LIMIT_KNEE
        mask = s > t
        over = (s[mask] - t) / (1 - t)
        np.tanh(over, out=over)
        out[mask] = np.copysign(t + (1 - t) * over, out[mask])

    # ------------------------------------------------------------------ render thread

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._work_ready():
                    self._cond.wait(timeout=0.01)
                if self._closed:
                    return
                self._reap_streams()
                if self._speed_restart:
                    self._restart_for_speed()
                job = self._take_job()
                stream_jobs = self._take_stream_jobs()
                gen = self._gen
                speed = self._speed
            for s, pieces, last in stream_jobs:
                self._render_stream(s, pieces, last)
            if job is None:
                continue
            try:
                self._render_job(job, gen, speed)
            except Exception:
                log.exception("rendering failed; dropping this block")

    def _work_ready(self) -> bool:
        if self._speed_restart:
            return True
        if any(s.ended and not s.done.is_set() for s in self._streams):
            return True
        if any(self._stream_needs(s) for s in self._streams):
            return True
        if any(s.ended for s in self._streams):
            return True
        if self._ring.available >= self.ahead_frames:
            return False
        if self._cur is not None:
            return self._has_data(self._cur, self._cur_pos)
        if self._queue:
            return True
        return self._eos_requested and not self._eos_sent

    @staticmethod
    def _has_data(item: RenderItem, pos: int) -> bool:
        """Something to render at `pos`: always for whole audio; for a growing utterance, a worthwhile piece or its
        end."""
        if _complete(item):
            return True
        return item.pcm.size - pos >= GrowingPcm.MIN_PIECE

    def _reap_streams(self) -> None:
        for s in [s for s in self._streams if s.ended]:
            s.done.set()
            self._streams.remove(s)

    def _stream_needs(self, s: ClipStream) -> bool:
        if s.ended or s.stopping:
            return False
        if s.tail_held and not s.pending and s.ring.available < self.ahead_frames // 4:
            return True  # about to run dry between appends: the resampler's held-back tail must come out now
        if s.ring.available >= self.ahead_frames:
            return False
        return bool(s.pending) or (s.finished and not s.fed_all)

    def _take_stream_jobs(self) -> list[tuple[ClipStream, list[np.ndarray], bool]]:
        jobs = []
        for s in self._streams:
            if not self._stream_needs(s):
                continue
            pieces: list[np.ndarray] = []
            budget = int(self.ahead_s * SOURCE_RATE)
            while s.pending and budget > 0:
                p = s.pending.popleft()
                if p.size > budget:
                    s.pending.appendleft(p[budget:])
                    p = p[:budget]
                pieces.append(p)
                budget -= p.size
            last = s.finished and not s.pending
            jobs.append((s, pieces, last))
        return jobs

    def _render_stream(self, s: ClipStream, pieces: list[np.ndarray], last: bool) -> None:
        x = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
        if x.size:
            x = np.array(x, dtype=np.float32, copy=True)
            fade.edge_fades(x, SOURCE_RATE, start=not s.edge_started, end=last)
            s.edge_started = True
        elif last:
            with self._cond:  # finished after its audio was rendered: fade the end in the ring if still unplayed
                n = fade.frames(fade.EDGE_MS, self.rate)
                tail = s.ring.view(s.ring.w - n, s.ring.w)
                if tail.size == n and float(np.max(np.abs(tail))) > fade.EDGE_SILENT:
                    s.ring.scale(s.ring.w - n, fade.ramp(n)[::-1].copy())
        rs = s.resampler
        try:
            y = rs.push(x, last=last)
        except Exception:
            log.exception("resampling a stream failed")
            y = x
        with self._cond:
            if s.ended or s.stopping:
                return
            if rs.rate != self.rate:
                y = _convert_rate(y, rs.rate, self.rate)  # the device rate changed meanwhile (`configure`)
            elif x.size and rs.rate != SOURCE_RATE:
                s.tail_held = True
            if y.size:
                s.ring.write_at(s.ring.w, y)
            if last:
                s.fed_all = True
                s.tail_held = False
                if s.ring.available == 0 and s.played == 0 and s.first_frame is None:
                    self._end_stream(s, "finished")  # an empty stream
            elif s.tail_held and not s.pending and s.ring.available < self.ahead_frames // 4:
                # About to starve between appends: let the resampler's held-back tail out.
                tail = s.resampler.push(np.zeros(0, dtype=np.float32), last=True)
                if tail.size:
                    s.ring.write_at(s.ring.w, tail)
                s.resampler = _Resampler(self.rate)
                s.tail_held = False

    def _take_job(self) -> tuple[str, _ItemState | None, np.ndarray, int, bool] | None:
        """Under the lock: the next reading step (a ~40 ms x speed chunk of the current utterance)."""
        if self._ring.available >= self.ahead_frames:
            return None
        if self._cur is None and self._queue:
            item = self._queue.popleft()
            self._cur = item
            self._cur_pos = max(0, item.start if not _complete(item) else min(item.start, item.pcm.size))
            self._state = _ItemState(item, self._stretcher_factory(self.source_rate, self._speed),
                                     _Resampler(self.rate))
            self._inflight.append(item)
            del self._inflight[:-8]
        if self._cur is not None and self._state is not None:
            item = self._cur
            complete = _complete(item)  # read before the size: once complete, the size is final
            avail = item.pcm.size
            n = max(256, int(round(0.04 * SOURCE_RATE * max(1.0, self._speed))))
            a = self._cur_pos
            if not complete and not self._has_data(item, a):
                return None  # still being synthesized: wait for more (or its end)
            b = min(avail, a + n)
            self._cur_pos = b
            done = complete and b >= avail
            state = self._state
            if done:
                self._cur = None
                self._state = None
            return ("item", state, item.pcm[a:b], a, done)
        if self._eos_requested and not self._eos_sent:
            self._eos_sent = True
            return ("eos", None, np.zeros(0, dtype=np.float32), 0, True)
        return None

    def _render_job(self, job: tuple[str, _ItemState | None, np.ndarray, int, bool], gen: int, speed: float) -> None:
        kind, state, chunk, a, done = job
        if kind == "eos" or state is None:
            with self._cond:
                if gen == self._gen:
                    w = self._ring.w
                    self._eos_frame = w
                    self._marks.append(_Mark(w, w, None, kind="eos"))
                    self._cond.notify_all()
            return
        item = state.item
        st = state.stretcher
        st.set_speed(speed)
        y24, segs = _mapped(st, chunk, a - item.start)
        if done:
            t24, tsegs = _mapped_flush(st)
            if t24.size:
                y24 = np.concatenate([y24, t24])
                segs = segs + tsegs
        # Hold back the last 3 ms so the utterance's end fade can be applied once the end is known.
        hold_n = fade.frames(fade.EDGE_MS, SOURCE_RATE)
        if state.hold.size:
            y24 = np.concatenate([state.hold, y24])
            segs = state.hold_segs + segs
        if not done:
            if y24.size <= hold_n:
                state.hold, state.hold_segs = np.array(y24, dtype=np.float32, copy=True), segs
                return
            keep = y24.size - hold_n
            state.hold = np.array(y24[keep:], dtype=np.float32, copy=True)
            segs, state.hold_segs = _split_segs(segs, keep)
            y24 = y24[:keep]
        else:
            state.hold, state.hold_segs = np.zeros(0, dtype=np.float32), []
        y24 = np.array(y24, dtype=np.float32, copy=True)
        fade.edge_fades(y24, SOURCE_RATE, start=not state.emitted and item.start == 0, end=done)
        rs = state.resampler
        y = rs.push(y24, last=done)
        with self._cond:
            if gen != self._gen:
                return  # flushed or restarted while rendering
            if rs.rate != self.rate:
                # The device rate changed while this chunk was being resampled (`configure`): bring it to the new
                # rate rather than writing old-rate audio into the new-rate ring.
                y = _convert_rate(y, rs.rate, self.rate)
            if state.rate != self.rate:  # ring coordinates of an utterance `configure` didn't see (its last chunk)
                f = self.rate / state.rate
                state.base, state.written = int(round(state.base * f)), int(round(state.written * f))
                state.rate = self.rate
            ratio = self.rate / SOURCE_RATE
            ring = self._ring
            if not state.emitted:
                state.emitted = True
                state.base = ring.w
                if self._xfade_at is not None:
                    # First audio after a flush or speed restart: it fades in over the old audio's fade-out.
                    state.base = max(ring.r, self._xfade_at)
                    state.fade_len = self._xfade_len
                    self._xfade_at = None
                elif item.start > 0:
                    # Starting mid-utterance with nothing to crossfade from (a saved position, a seek or stop
                    # then play while paused): fade in like a resume instead of starting at full level.
                    state.fade_len = fade.frames(fade.RESUME_MS, self.rate)
            if state.fade_pos < state.fade_len and y.size:
                k = min(state.fade_len - state.fade_pos, y.size)
                y = y.copy()
                y[:k] *= fade.ramp(state.fade_len)[state.fade_pos : state.fade_pos + k]
                state.fade_pos += k
            if y.size:
                at = max(ring.r, state.base + state.written)
                ring.write_at(at, y, mix=at < ring.w)
            state.written += y.size
            out24 = state.out24
            for cnt, sa, sb in segs:
                ma = state.base + int(round(out24 * ratio))
                out24 += cnt
                mb = state.base + int(round(out24 * ratio))
                if mb > ma:
                    self._marks.append(_Mark(ma, mb, item.index, item.start + sa, item.start + sb))
            state.out24 = out24
            if done:
                end = state.base + state.written
                self._marks.append(_Mark(end, end, item.index, item.pcm.size, item.pcm.size, kind="end"))
            self._cond.notify_all()

    def _restart_for_speed(self) -> None:
        """Under the lock: re-render from just ahead of the playhead at the new speed, crossfading 10 ms into it,
        so a speed change is heard within ~50 ms rather than after the whole ring has played."""
        self._speed_restart = False
        ring = self._ring
        lead = fade.frames(40.0, self.rate)  # room for the render thread to get the new audio in place
        xf = fade.frames(fade.SPEED_MS, self.rate)
        c0 = ring.r + lead
        if ring.w < c0 + xf:
            return  # almost nothing buffered: the next utterance chunk simply uses the new speed
        mark = next((m for m in self._marks if m.kind == "audio" and m.start <= c0 < m.end), None)
        if mark is None or mark.index is None:
            return
        item = next((it for it in self._inflight if it.index == mark.index), None)
        if item is None:
            return
        frac = (c0 - mark.start) / max(1, mark.end - mark.start)
        src = int(round(mark.src_a + (mark.src_b - mark.src_a) * frac))
        old = ring.view(c0, c0 + xf)
        if old.size >= xf // 2:
            src = _align(old, self.rate, item.pcm, src)
        later = [it for it in self._inflight if it.index > item.index]
        if self._cur is not None and self._cur is not item and all(it is not self._cur for it in later):
            later.append(self._cur)
        queued = list(self._queue)
        self._gen += 1
        ring.scale(c0, fade.ramp(xf)[::-1].copy())
        ring.truncate(c0 + xf)
        self._xfade_at, self._xfade_len = c0, xf
        while self._marks and self._marks[-1].start >= c0:
            self._marks.pop()
        if self._marks and self._marks[-1].kind == "audio" and self._marks[-1].end > c0:
            m = self._marks[-1]
            f = (c0 - m.start) / max(1, m.end - m.start)
            m.src_b = m.src_a + (m.src_b - m.src_a) * f
            m.end = c0
        restart = replace(item, start=max(0, min(src, item.pcm.size)))
        self._queue = deque([restart, *later, *queued])
        self._cur = None
        self._state = None
        self._inflight = [it for it in self._inflight if it.index < item.index]
        if self._eos_sent:
            self._eos_sent = False
            self._eos_frame = None


def _convert_rate(y: np.ndarray, src: int, dst: int) -> np.ndarray:
    """One-shot resampling of a chunk prepared for another device rate (a device switch mid-render)."""
    if src == dst or y.size == 0:
        return y
    import soxr

    return np.asarray(soxr.resample(np.asarray(y, dtype=np.float32), src, dst, quality="VHQ"), dtype=np.float32)


def _mapped(st: Any, chunk: np.ndarray, a: int) -> tuple[np.ndarray, list[tuple[int, float, float]]]:
    """Stretch a chunk; segments map output to source offsets (relative to the utterance's start offset). A
    stretcher without `process_mapped` (the legacy one in the audio lab) gets a linear map over the chunk."""
    if chunk.size == 0:
        return np.zeros(0, dtype=np.float32), []
    if hasattr(st, "process_mapped"):
        y, segs = st.process_mapped(chunk)
        return y, [(int(n), float(sa), float(sb)) for n, sa, sb in segs]
    y = st.process(chunk)
    return y, ([(int(y.size), float(a), float(a + chunk.size))] if y.size else [])


def _mapped_flush(st: Any) -> tuple[np.ndarray, list[tuple[int, float, float]]]:
    if hasattr(st, "flush_mapped"):
        y, segs = st.flush_mapped()
        return y, [(int(n), float(sa), float(sb)) for n, sa, sb in segs]
    y = st.flush()
    return y, []


def _split_segs(segs: list[tuple[int, float, float]], keep: int) -> tuple[list[tuple[int, float, float]],
                                                                          list[tuple[int, float, float]]]:
    """Split output segments at `keep` output samples."""
    head: list[tuple[int, float, float]] = []
    tail: list[tuple[int, float, float]] = []
    acc = 0
    for cnt, a, b in segs:
        if acc + cnt <= keep:
            head.append((cnt, a, b))
        elif acc >= keep:
            tail.append((cnt, a, b))
        else:
            k = keep - acc
            mid = a + (b - a) * k / cnt if cnt else a
            head.append((k, a, mid))
            tail.append((cnt - k, mid, b))
        acc += cnt
    return head, tail


def _align(old: np.ndarray, rate: int, pcm: np.ndarray | GrowingPcm, src: int, *, search_ms: float = 6.0) -> int:
    """The source position (24 kHz) whose waveform best matches `old` (device rate), within ±`search_ms`."""
    x = np.asarray(old, dtype=np.float64)
    if rate != SOURCE_RATE:
        t = np.arange(int(x.size * SOURCE_RATE / rate)) * (rate / SOURCE_RATE)
        x = np.interp(t, np.arange(x.size), x)
    m = x.size
    w = int(SOURCE_RATE * search_ms / 1000)
    lo, hi = max(0, src - w), min(pcm.size - m, src + w)
    if hi <= lo or m < 16 or float(x @ x) < 1e-9:
        return src
    region = np.asarray(pcm[lo : hi + m], dtype=np.float64)
    corr = np.correlate(region, x, "valid")
    c = np.concatenate([[0.0], np.cumsum(region * region)])
    energy = c[m:] - c[:-m]
    score = corr / np.sqrt(np.maximum(energy, 1e-12) * float(x @ x))
    return lo + int(np.argmax(score))


__all__ = ["ClipStream", "GrowingPcm", "PlayPosition", "RenderItem", "Renderer", "field"]
