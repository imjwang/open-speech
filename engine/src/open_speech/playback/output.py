"""Audio outputs. The transport hands an output a `pull(n) -> float32[n]` callable that only copies from a
ring buffer, so it is safe to call from a real-time audio thread and never waits on the event loop.

- `SoundDeviceOutput`: PortAudio via `sounddevice` (lazy import; CoreAudio on macOS).
- `NullOutput`: headless. A thread consumes frames on a real-time clock (optionally scaled for fast tests),
  so word events and highlights still happen at the right times without a sound card.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import numpy as np

log = logging.getLogger(__name__)
Pull = Callable[[int], np.ndarray]


@runtime_checkable
class AudioOutput(Protocol):
    name: str

    def start(self, sample_rate: int, pull: Pull) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    @property
    def running(self) -> bool: ...


class NullOutput:
    """Consumes audio on a wall clock. `time_scale=20` plays 20 s of audio per real second (tests).
    `capture=True` keeps everything pulled (for asserting on rendered audio)."""

    name = "null"

    def __init__(self, *, time_scale: float = 1.0, block_frames: int = 480, capture: bool = False) -> None:
        self.time_scale = max(0.01, float(time_scale))
        self.block_frames = block_frames
        self.capture = capture
        self.captured: list[np.ndarray] = []
        self.frames_played = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pull: Pull | None = None
        self._sr = 24000

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, sample_rate: int, pull: Pull) -> None:
        if self.running:
            self._pull = pull
            return
        self._sr = sample_rate
        self._pull = pull
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="open-speech-null-output", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = self.block_frames / self._sr / self.time_scale
        next_t = time.perf_counter()
        while not self._stop.is_set():
            pull = self._pull
            if pull is not None:
                try:
                    block = pull(self.block_frames)
                    if self.capture:
                        self.captured.append(np.array(block, dtype=np.float32, copy=True))
                except Exception:
                    log.exception("null output pull failed")
            self.frames_played += self.block_frames
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -0.25:
                next_t = time.perf_counter()  # fell far behind (debugger, CI hiccup): don't burst

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def close(self) -> None:
        self.stop()

    def captured_audio(self) -> np.ndarray:
        return np.concatenate(self.captured) if self.captured else np.zeros(0, dtype=np.float32)


class SoundDeviceOutput:
    name = "sounddevice"

    def __init__(self, *, device: Any | None = None, latency: str | float = "low", blocksize: int = 0) -> None:
        self._device = device
        self._latency = latency
        self._blocksize = blocksize
        self._stream: Any | None = None
        self._pull: Pull | None = None

    @staticmethod
    def available() -> bool:
        try:
            import sounddevice  # noqa: F401
        except (OSError, ImportError):
            return False
        return True

    @property
    def running(self) -> bool:
        return self._stream is not None and bool(getattr(self._stream, "active", False))

    def start(self, sample_rate: int, pull: Pull) -> None:
        self._pull = pull
        if self._stream is not None:
            if not self._stream.active:
                self._stream.start()
            return
        import sounddevice as sd

        def callback(outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
            p = self._pull
            outdata[:, 0] = p(frames) if p is not None else 0.0

        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=self._blocksize,
            latency=self._latency,
            device=self._device,
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None and self._stream.active:
            self._stream.stop()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


def default_output(*, prefer_device: bool = True) -> AudioOutput:
    """`SoundDeviceOutput` when PortAudio is present, otherwise `NullOutput` (headless)."""
    if prefer_device and SoundDeviceOutput.available():
        return SoundDeviceOutput()
    return NullOutput()
