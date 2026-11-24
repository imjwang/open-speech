"""The renderer Open Speech shipped before round 3 (engine/src/open_speech/playback/render.py at 411bca6), kept
verbatim apart from imports so before/after measurements can be re-run: audiotsm WSOLA always (also at 1x), a
120 ms ring, hard cuts on pause/seek/stop, clips mixed with np.clip.

Original docstring: Render thread: queued utterance PCM → WSOLA at the current speed → a small ring buffer that the
output pulls from.

Threads and locks (the transport's safety story):
- The asyncio loop only calls the thread-safe methods here (`enqueue`, `flush`, `set_speed`, …), which
  hold `_lock` for a few microseconds of bookkeeping.
- The render thread does the WSOLA work *outside* the lock and appends finished blocks under it.
- The audio thread calls `pull(n)`: copy-out under the lock, never blocking on the loop or on I/O.
- Played position (utterance index + source sample offset) is updated in `pull`, i.e. at the moment the
  samples are handed to the device, and read by the loop's ticker to emit word events.

The source stream is continuous across utterances (gapless). Each output block remembers which
utterance and source range it came from, so a position is accurate to about one WSOLA frame (~43 ms).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from tools.audiolab.legacy_stretch import LegacyStretcher as Stretcher
from tools.audiolab.legacy_stretch import clamp_speed

log = logging.getLogger(__name__)


@dataclass
class RenderItem:
    index: int  # utterance index
    pcm: np.ndarray  # mono float32 at the renderer's sample rate
    start: int = 0  # first source sample to play (seek offset)


@dataclass
class _Block:
    out: np.ndarray
    index: int | None  # utterance index; None for gap/tail audio
    src_a: int = 0
    src_b: int = 0
    end_of_item: bool = False
    eos: bool = False


@dataclass(eq=False)  # compared by identity: `list.remove` must not compare the sample arrays of two clips
class _Clip:
    pcm: np.ndarray
    pos: int = 0
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class PlayPosition:
    index: int | None
    offset: int  # source samples into the utterance
    starved: bool
    finished: bool
    ended: list[int]  # utterances whose last sample was handed to the device since the last snapshot


class Renderer:
    def __init__(self, sample_rate: int = 24000, *, block_out: int = 1024, ahead_s: float = 0.12) -> None:
        self.sample_rate = sample_rate
        self.block_out = block_out
        self.ahead_frames = int(ahead_s * sample_rate)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: deque[RenderItem] = deque()
        self._cur: RenderItem | None = None
        self._cur_pos = 0
        self._ring: deque[_Block] = deque()
        self._ring_frames = 0
        self._head = 0
        self._speed = 1.0
        self._gen = 0
        self._need_reset = False
        self._gap_fed = True
        self._eos_requested = False
        self._eos_sent = False
        self._reading_paused = False
        self._clips: list[_Clip] = []
        self._pos_index: int | None = None
        self._pos_offset = 0
        self._starved = False
        self._finished = False
        self._ended: list[int] = []
        self._closed = False
        self._thread: threading.Thread | None = None
        self._stretcher = Stretcher(sample_rate, 1.0)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="open-speech-render", daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            for c in self._clips:
                c.done.set()
            self._clips.clear()
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------ control (loop side)

    @property
    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        with self._cond:
            self._speed = clamp_speed(speed)
            self._cond.notify_all()

    def enqueue(self, item: RenderItem) -> None:
        with self._cond:
            self._queue.append(item)
            self._finished = False
            self._cond.notify_all()

    def mark_end_of_stream(self) -> None:
        with self._cond:
            self._eos_requested = True
            self._cond.notify_all()

    def flush(self, *, index: int | None = None, offset: int = 0) -> None:
        """Drop everything queued and buffered (seek/skip/stop); the position becomes (index, offset)."""
        with self._cond:
            self._gen += 1
            self._queue.clear()
            self._cur = None
            self._cur_pos = 0
            self._ring.clear()
            self._ring_frames = 0
            self._head = 0
            self._need_reset = True
            self._gap_fed = True
            self._eos_requested = False
            self._eos_sent = False
            self._finished = False
            self._starved = False
            self._ended.clear()
            self._pos_index = index
            self._pos_offset = offset
            self._cond.notify_all()

    @property
    def reading_paused(self) -> bool:
        return self._reading_paused

    def pause_reading(self, paused: bool) -> None:
        with self._cond:
            self._reading_paused = paused
            self._cond.notify_all()

    def play_clip(self, pcm: np.ndarray) -> threading.Event:
        clip = _Clip(np.asarray(pcm, dtype=np.float32).reshape(-1))
        if clip.pcm.size == 0:
            clip.done.set()
            return clip.done
        with self._cond:
            self._clips.append(clip)
        return clip.done

    def stop_clips(self) -> None:
        with self._cond:
            for c in self._clips:
                c.done.set()
            self._clips.clear()

    def has_clips(self) -> bool:
        with self._lock:
            return bool(self._clips)

    def queued_indices(self) -> list[int]:
        with self._lock:
            out = [self._cur.index] if self._cur is not None else []
            return out + [i.index for i in self._queue]

    def last_queued_index(self) -> int | None:
        with self._lock:
            if self._queue:
                return self._queue[-1].index
            return self._cur.index if self._cur is not None else None

    def snapshot(self) -> PlayPosition:
        with self._lock:
            ended, self._ended = self._ended, []
            return PlayPosition(self._pos_index, self._pos_offset, self._starved, self._finished, ended)

    def buffered_frames(self) -> int:
        with self._lock:
            return self._ring_frames

    # ------------------------------------------------------------------ audio thread

    def pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, dtype=np.float32)
        with self._cond:
            filled = 0
            if not self._reading_paused:
                while filled < n and self._ring:
                    blk = self._ring[0]
                    if blk.out.size == 0:
                        self._marker(blk)
                        self._ring.popleft()
                        continue
                    k = min(blk.out.size - self._head, n - filled)
                    out[filled : filled + k] = blk.out[self._head : self._head + k]
                    self._head += k
                    filled += k
                    self._ring_frames -= k
                    if blk.index is not None:
                        frac = self._head / blk.out.size
                        self._pos_index = blk.index
                        self._pos_offset = int(blk.src_a + (blk.src_b - blk.src_a) * frac)
                    if self._head >= blk.out.size:
                        self._ring.popleft()
                        self._head = 0
                while self._ring and self._ring[0].out.size == 0:
                    self._marker(self._ring.popleft())
                self._starved = filled < n and not self._finished
            for clip in list(self._clips):
                k = min(n, clip.pcm.size - clip.pos)
                out[:k] += clip.pcm[clip.pos : clip.pos + k]
                clip.pos += k
                if clip.pos >= clip.pcm.size:
                    clip.done.set()
                    self._clips.remove(clip)
            self._cond.notify_all()
        np.clip(out, -1.0, 1.0, out=out)
        return out

    def _marker(self, blk: _Block) -> None:
        if blk.end_of_item and blk.index is not None:
            self._ended.append(blk.index)
            self._pos_index = blk.index
            self._pos_offset = blk.src_b
        if blk.eos:
            self._finished = True

    # ------------------------------------------------------------------ render thread

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._work_ready():
                    self._cond.wait(timeout=0.05)
                if self._closed:
                    return
                reset = self._need_reset
                self._need_reset = False
                gen = self._gen
                speed = self._speed
                job = self._take_job()
            if reset:
                self._stretcher.reset()
            if job is None:
                continue
            kind, index, chunk, a, b, done = job
            try:
                self._stretcher.set_speed(speed)
                out = self._stretcher.flush() if kind == "eos" else self._stretcher.process(chunk)
            except Exception:
                log.exception("time-stretch failed; passing audio through")
                out = chunk
            with self._cond:
                if gen != self._gen:
                    continue  # flushed while rendering
                if out.size:
                    self._ring.append(_Block(out, index if kind == "item" else None, a, b))
                    self._ring_frames += out.size
                if kind == "item" and done:
                    self._ring.append(_Block(np.zeros(0, dtype=np.float32), index, b, b, end_of_item=True))
                if kind == "eos":
                    self._ring.append(_Block(np.zeros(0, dtype=np.float32), None, eos=True))
                self._cond.notify_all()

    def _work_ready(self) -> bool:
        if self._need_reset:
            return True
        if self._ring_frames >= self.ahead_frames:
            return False
        if self._cur is not None or self._queue:
            return True
        if self._eos_requested and not self._eos_sent:
            return True
        return not self._gap_fed

    def _take_job(self) -> tuple[str, int | None, np.ndarray, int, int, bool] | None:
        """Called under the lock: decide the next render step."""
        if self._ring_frames >= self.ahead_frames:
            return None
        if self._cur is None and self._queue:
            self._cur = self._queue.popleft()
            self._cur_pos = max(0, min(self._cur.start, self._cur.pcm.size))
        if self._cur is not None:
            item = self._cur
            n = max(64, int(self.block_out * self._speed))
            a = self._cur_pos
            b = min(item.pcm.size, a + n)
            chunk = item.pcm[a:b]
            self._cur_pos = b
            done = b >= item.pcm.size
            if done:
                self._cur = None
            self._gap_fed = False
            return ("item", item.index, chunk, a, b, done)
        if self._eos_requested and not self._eos_sent:
            self._eos_sent = True
            self._gap_fed = True
            return ("eos", None, np.zeros(0, dtype=np.float32), 0, 0, True)
        if not self._gap_fed:
            # Starved mid-stream: push WSOLA's held frame out with a little silence so the tail of the
            # last utterance isn't stuck while the next one is still synthesizing.
            self._gap_fed = True
            return ("gap", None, np.zeros(self._stretcher.frame_length, dtype=np.float32), 0, 0, False)
        return None
