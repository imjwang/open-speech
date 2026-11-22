"""Pitch-preserving time-stretch with WSOLA (audiotsm), 0.5–5x (FR-READ-12, 05 § 9).

`Stretcher` is streaming: push source blocks, get output blocks, change speed at any time (the next
analysis frame uses the new hop). The transport runs it in its render thread on a ~120 ms ring buffer,
so a speed change is audible in well under 300 ms and nothing is re-synthesized. `stretch()` is the
one-shot form (tests, exports).
"""

from __future__ import annotations

import numpy as np
from audiotsm import wsola
from audiotsm.io.base import Reader, Writer

MIN_SPEED = 0.5
MAX_SPEED = 5.0


def clamp_speed(speed: float) -> float:
    return float(min(MAX_SPEED, max(MIN_SPEED, round(speed, 3))))


class _ArrayReader(Reader):  # type: ignore[misc]
    def __init__(self, data: np.ndarray) -> None:
        self._data = data.reshape(1, -1)
        self._pos = 0

    @property
    def channels(self) -> int:
        return 1

    @property
    def empty(self) -> bool:
        return self._pos >= self._data.shape[1]

    def read(self, buffer: np.ndarray) -> int:
        n = min(buffer.shape[1], self._data.shape[1] - self._pos)
        buffer[:, :n] = self._data[:, self._pos : self._pos + n]
        self._pos += n
        return int(n)

    def skip(self, n: int) -> int:
        k = min(n, self._data.shape[1] - self._pos)
        self._pos += k
        return int(k)


class _Collect(Writer):  # type: ignore[misc]
    def __init__(self) -> None:
        self.parts: list[np.ndarray] = []

    @property
    def channels(self) -> int:
        return 1

    def write(self, buffer: np.ndarray) -> int:
        if buffer.shape[1]:
            self.parts.append(np.array(buffer[0], dtype=np.float32, copy=True))
        return int(buffer.shape[1])

    def data(self) -> np.ndarray:
        return np.concatenate(self.parts) if self.parts else np.zeros(0, dtype=np.float32)


def frame_length_for(sample_rate: int) -> int:
    """~43 ms analysis frames (1024 at 24 kHz, 2048 at 48 kHz)."""
    n = 1
    target = max(256, int(sample_rate * 0.042))
    while n < target:
        n <<= 1
    return n


class Stretcher:
    def __init__(self, sample_rate: int = 24000, speed: float = 1.0) -> None:
        self.sample_rate = sample_rate
        self.frame_length = frame_length_for(sample_rate)
        self._speed = clamp_speed(speed)
        # audiotsm skips extra input when analysis_hop > frame_length (it ignores the WSOLA tolerance
        # margin), which makes speeds above 2x play too fast with the default hop of frame/2. A hop of
        # frame/5 keeps analysis_hop ≤ frame_length up to 5x; the extra overlap also smooths the output.
        self.synthesis_hop = self.frame_length // 5
        self._tsm = wsola(
            1,
            speed=self._speed,
            frame_length=self.frame_length,
            synthesis_hop=self.synthesis_hop,
            tolerance=self.synthesis_hop,
        )

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
        """Feed source samples; returns whatever output is ready (may be empty)."""
        reader = _ArrayReader(np.asarray(x, dtype=np.float32))
        writer = _Collect()
        finished = False
        while not (finished and reader.empty):
            self._tsm.read_from(reader)
            _, finished = self._tsm.write_to(writer)
        return writer.data()

    def flush(self) -> np.ndarray:
        """Drain the frame still held inside WSOLA (end of stream); the stretcher is reset afterwards.
        A frame of silence is pushed through first, because audiotsm's own flush drops whatever is still in
        its input buffer (up to one frame of the utterance's tail)."""
        pushed = self.process(np.zeros(self.frame_length, dtype=np.float32))
        writer = _Collect()
        writer.parts.append(pushed)
        finished = False
        guard = 0
        while not finished and guard < 64:
            _, finished = self._tsm.flush_to(writer)
            guard += 1
        self._tsm.clear()
        return writer.data()


def stretch(x: np.ndarray, speed: float, sample_rate: int = 24000) -> np.ndarray:
    """One-shot pitch-preserving stretch; output length ≈ len(x) / speed."""
    s = Stretcher(sample_rate, speed)
    out = s.process(x)
    tail = s.flush()
    return np.concatenate([out, tail]).astype(np.float32)
