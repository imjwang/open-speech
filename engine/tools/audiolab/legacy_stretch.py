"""The time-stretcher Open Speech shipped before round 3, kept so before/after measurements can be re-run.

This is audiotsm 0.1.2's WSOLA (https://github.com/Muges/audiotsm, MIT License, Copyright (c) 2017 Muges),
condensed into one mono module with the same arithmetic, plus the `Stretcher` wrapper and parameters that
`open_speech/playback/stretch.py` used (frame 1024 @ 24 kHz, synthesis hop = tolerance = frame // 5). The
engine no longer depends on audiotsm; nothing outside `tools/audiolab` imports this file.

The MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
documentation files (the "Software"), to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions
of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF
CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
IN THE SOFTWARE.
"""

from __future__ import annotations

from typing import Any

import numpy as np

EPSILON = 0.0001
MIN_SPEED = 0.5
MAX_SPEED = 5.0


def clamp_speed(speed: float) -> float:
    return float(min(MAX_SPEED, max(MIN_SPEED, round(speed, 3))))


def _hanning(length: int) -> np.ndarray:
    """audiotsm's periodic Hann window (float64)."""
    if length <= 0:
        return np.zeros(0)
    t = np.arange(length)
    return 0.5 * (1 - np.cos(2 * np.pi * t / length))


class _CBuffer:
    """Mono circular buffer with audiotsm's `ready` semantics (float32 storage, like the original)."""

    def __init__(self, max_length: int) -> None:
        self._data = np.zeros(max_length, dtype=np.float32)
        self._max = max_length
        self._offset = 0
        self._ready = 0
        self._length = 0

    def _span(self, start: int, n: int) -> tuple[slice, slice | None, int]:
        end = start + n
        if end <= self._max:
            return slice(start, end), None, n
        return slice(start, self._max), slice(0, end - self._max), self._max - start

    def add(self, buf: np.ndarray) -> None:
        n = buf.shape[0]
        if n > self._length:
            raise ValueError("not enough space remaining in CBuffer")
        a, b, k = self._span(self._offset, n)
        self._data[a] += buf[:k]
        if b is not None:
            self._data[b] += buf[k:n]

    def divide(self, arr: np.ndarray) -> None:
        n = len(arr)
        if n > self._length:
            raise ValueError("not enough space remaining in the CBuffer")
        a, b, k = self._span(self._offset, n)
        self._data[a] /= arr[:k]
        if b is not None:
            self._data[b] /= arr[k:n]

    @property
    def length(self) -> int:
        return self._length

    @property
    def ready(self) -> int:
        return self._ready

    @property
    def remaining_length(self) -> int:
        return self._max - self._ready

    def peek(self, buf: np.ndarray) -> int:
        n = min(buf.shape[0], self._ready)
        a, b, k = self._span(self._offset, n)
        np.copyto(buf[:k], self._data[a])
        if b is not None:
            np.copyto(buf[k:n], self._data[b])
        return n

    def read_from(self, reader: _Reader) -> int:
        start = (self._offset + self._length) % self._max
        end = start + self._max - self._length
        if end <= self._max:
            n = reader.read(self._data[start:end])
        else:
            n = reader.read(self._data[start:])
            n += reader.read(self._data[: end - self._max])
        self._length += n
        self._ready = self._length
        return n

    def remove(self, n: int) -> int:
        n = min(n, self._length)
        a, b, _k = self._span(self._offset, n)
        self._data[a] = 0
        if b is not None:
            self._data[b] = 0
        self._offset = (self._offset + n) % self._max
        self._length -= n
        self._ready = max(0, self._ready - n)
        return n

    def right_pad(self, n: int) -> None:
        if n > self._max - self._length:
            raise ValueError("not enough space remaining in CBuffer")
        self._length += n

    def set_ready(self, n: int) -> None:
        if self._ready + n > self._length:
            raise ValueError("not enough samples to be marked as ready")
        self._ready += n

    def write(self, buf: np.ndarray) -> int:
        n = min(buf.shape[0], self._max - self._length)
        start = (self._offset + self._length) % self._max
        a, b, k = self._span(start, n)
        np.copyto(self._data[a], buf[:k])
        if b is not None:
            np.copyto(self._data[b], buf[k:n])
        self._length += n
        self._ready = self._length
        return n

    def write_to(self, writer: _Writer) -> int:
        start = self._offset
        end = self._offset + self._ready
        if end <= self._max:
            n = writer.write(self._data[start:end])
        else:
            n = writer.write(self._data[start:])
            n += writer.write(self._data[: end - self._max])
        self.remove(n)
        return n


class _NormalizeBuffer:
    def __init__(self, length: int) -> None:
        self._data = np.zeros(length)
        self._offset = 0
        self._length = length

    @property
    def length(self) -> int:
        return self._length

    def add(self, window: np.ndarray) -> None:
        n = len(window)
        start, end = self._offset, self._offset + n
        if end <= self._length:
            self._data[start:end] += window
        else:
            self._data[start:] += window[: self._length - start]
            self._data[: end - self._length] += window[self._length - start :]

    def remove(self, n: int) -> None:
        n = min(n, self._length)
        if n == 0:
            return
        start, end = self._offset, self._offset + n
        if end <= self._length:
            self._data[start:end] = 0
        else:
            self._data[start:] = 0
            self._data[: end - self._length] = 0
        self._offset = (self._offset + n) % self._length

    def to_array(self, end: int) -> np.ndarray:
        start, stop = self._offset, self._offset + end
        if stop <= self._length:
            return np.copy(self._data[start:stop])
        return np.concatenate((self._data[start:], self._data[: stop - self._length]))


class _Reader:
    def __init__(self, data: np.ndarray) -> None:
        self._data = data
        self._pos = 0

    @property
    def empty(self) -> bool:
        return self._pos >= self._data.shape[0]

    def read(self, buf: np.ndarray) -> int:
        n = min(buf.shape[0], self._data.shape[0] - self._pos)
        buf[:n] = self._data[self._pos : self._pos + n]
        self._pos += n
        return int(n)

    def skip(self, n: int) -> int:
        k = min(n, self._data.shape[0] - self._pos)
        self._pos += k
        return int(k)


class _Writer:
    def __init__(self) -> None:
        self.parts: list[np.ndarray] = []

    def write(self, buf: np.ndarray) -> int:
        if buf.shape[0]:
            self.parts.append(np.array(buf, dtype=np.float32, copy=True))
        return int(buf.shape[0])

    def data(self) -> np.ndarray:
        return np.concatenate(self.parts) if self.parts else np.zeros(0, dtype=np.float32)


class _Wsola:
    """audiotsm `AnalysisSynthesisTSM` + `WSOLAConverter`, mono. `splices` records, per analysis frame, the
    source position the frame was taken from (for the harness's splice maps)."""

    def __init__(self, speed: float, frame_length: int, synthesis_hop: int, tolerance: int) -> None:
        self._frame = frame_length
        self._hs = synthesis_hop
        self._ha = int(synthesis_hop * speed)
        self._tol = tolerance
        self._before = tolerance
        self._after = tolerance + synthesis_hop
        self._window = _hanning(frame_length)
        self._normalize_window = self._window
        delta = self._before + self._after
        self._in = _CBuffer(frame_length + delta)
        self._analysis = np.empty(frame_length + delta, dtype=np.float64)
        self._out = _CBuffer(frame_length)
        self._norm = _NormalizeBuffer(frame_length)
        self._synthesis = np.empty(frame_length)
        self._natural = np.empty(frame_length)
        self._first = True
        self._skip_in = 0
        self._skip_out = 0
        self.clear()

    def clear(self) -> None:
        self._in.remove(self._in.length)
        self._out.remove(self._out.length)
        self._out.right_pad(self._frame)
        self._norm.remove(self._norm.length)
        self._in.write(np.zeros(self._before + self._frame // 2, dtype=np.float32))
        self._skip_out = self._frame // 2
        self._first = True

    def set_speed(self, speed: float) -> None:
        self._ha = int(self._hs * speed)

    def _convert(self, frame: np.ndarray) -> np.ndarray:
        delta = 0 if self._first else int(np.argmax(np.correlate(frame[: -self._hs], self._natural)))
        np.copyto(self._synthesis, frame[delta : delta + self._frame])
        delta += self._hs
        np.copyto(self._natural, frame[delta : delta + self._frame])
        self._first = False
        return self._synthesis

    def _process_frame(self) -> None:
        self._in.peek(self._analysis)
        self._in.remove(self._ha)
        synth = self._convert(self._analysis)
        synth *= self._window
        self._out.add(synth)
        self._norm.add(self._normalize_window)
        normalize = self._norm.to_array(end=self._hs)
        normalize[normalize < EPSILON] = 1
        self._out.divide(normalize)
        self._out.set_ready(self._hs)
        self._norm.remove(self._hs)

    def read_from(self, reader: _Reader) -> int:
        n = reader.skip(self._skip_in)
        self._skip_in -= n
        if self._skip_in > 0:
            return n
        n += self._in.read_from(reader)
        if self._in.remaining_length == 0 and self._out.remaining_length >= self._hs:
            self._process_frame()
            skipped = self._out.remove(self._skip_out)
            self._out.right_pad(skipped)
            self._skip_out -= skipped
            self._skip_in = max(0, self._ha - self._frame)
        return n

    def write_to(self, writer: _Writer) -> tuple[int, bool]:
        n = self._out.write_to(writer)
        self._out.right_pad(n)
        if self._in.remaining_length > 0 and self._out.ready == 0:
            return n, True
        return n, False

    def flush_to(self, writer: _Writer) -> tuple[int, bool]:
        if self._in.remaining_length == 0:
            raise RuntimeError("flush_to called with unprocessed input")
        n = self._out.write_to(writer)
        if self._out.ready == 0:
            self.clear()
            return n, True
        return n, False


def frame_length_for(sample_rate: int) -> int:
    n = 1
    target = max(256, int(sample_rate * 0.042))
    while n < target:
        n <<= 1
    return n


class LegacyStretcher:
    """Drop-in for the engine's `Stretcher` (same methods), with the pre-round-3 algorithm and parameters."""

    name = "legacy-audiotsm"

    def __init__(self, sample_rate: int = 24000, speed: float = 1.0) -> None:
        self.sample_rate = sample_rate
        self.frame_length = frame_length_for(sample_rate)
        self._speed = clamp_speed(speed)
        self.synthesis_hop = self.frame_length // 5
        self._tsm = _Wsola(self._speed, self.frame_length, self.synthesis_hop, self.synthesis_hop)

    @property
    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        s = clamp_speed(speed)
        if s != self._speed:
            self._speed = s
            self._tsm.set_speed(s)

    def reset(self) -> None:
        self._tsm.clear()

    def process(self, x: np.ndarray) -> np.ndarray:
        reader = _Reader(np.asarray(x, dtype=np.float32).reshape(-1))
        writer = _Writer()
        finished = False
        while not (finished and reader.empty):
            self._tsm.read_from(reader)
            _, finished = self._tsm.write_to(writer)
        return writer.data()

    def flush(self) -> np.ndarray:
        pushed = self.process(np.zeros(self.frame_length, dtype=np.float32))
        writer = _Writer()
        writer.parts.append(pushed)
        finished = False
        guard = 0
        while not finished and guard < 64:
            _, finished = self._tsm.flush_to(writer)
            guard += 1
        self._tsm.clear()
        return writer.data()

    # The engine's new stretcher reports per-hop source positions; the legacy one only knows its input count.
    def source_positions(self) -> Any:
        return None


def legacy_stretch(x: np.ndarray, speed: float, sample_rate: int = 24000) -> np.ndarray:
    s = LegacyStretcher(sample_rate, speed)
    out = s.process(x)
    tail = s.flush()
    return np.concatenate([out, tail]).astype(np.float32)
