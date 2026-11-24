"""What the audio path did, counted where it happens (05 § 9; `GET /api/v1/diagnostics/audio`,
`open-speech doctor audio`).

The audio thread only adds to integers and compares floats here (no allocation, no locks of its own); the loop
reads a snapshot. Starvation is split by cause: the render thread fell behind (`render_starvations`, a bug or an
overloaded machine) versus the next utterance simply wasn't synthesized yet (`synthesis_waits`, a slow voice).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AudioDiagnostics:
    started_at: float = field(default_factory=time.monotonic)
    callbacks: int = 0  # pulls (audio callbacks or writer blocks)
    frames: int = 0  # device frames handed to the output
    # PortAudio status flags reported by the device
    output_underflows: int = 0
    output_overflows: int = 0
    priming_output: int = 0
    # Reading ran dry mid-stream: the renderer had more to render (its fault) / waited for synthesis.
    render_starvations: int = 0
    synthesis_waits: int = 0
    stream_starvations: int = 0  # a reply/clip stream ran dry before it was finished
    ring_low_water_frames: int = -1  # lowest reading-ring level seen while reading played (-1: never played)
    max_pull_ms: float = 0.0
    total_pull_ms: float = 0.0
    device_reopens: int = 0
    rate: int = 0
    device: str | None = None
    latency_ms: float = 0.0
    mode: str = ""
    resampler: str = ""

    def note_pull(self, frames: int, ms: float) -> None:
        self.callbacks += 1
        self.frames += frames
        self.total_pull_ms += ms
        if ms > self.max_pull_ms:
            self.max_pull_ms = ms

    def note_ring(self, level: int) -> None:
        if self.ring_low_water_frames < 0 or level < self.ring_low_water_frames:
            self.ring_low_water_frames = level

    def reset(self) -> None:
        keep = (self.rate, self.device, self.latency_ms, self.mode, self.resampler, self.device_reopens)
        self.__init__()  # type: ignore[misc]
        self.rate, self.device, self.latency_ms, self.mode, self.resampler, self.device_reopens = keep

    def snapshot(self) -> dict[str, Any]:
        rate = self.rate or 1
        return {
            "uptime_s": round(time.monotonic() - self.started_at, 1),
            "device": self.device,
            "rate": self.rate,
            "latency_ms": round(self.latency_ms, 1),
            "mode": self.mode,
            "resampler": self.resampler,
            "callbacks": self.callbacks,
            "audio_s": round(self.frames / rate, 2),
            "output_underflows": self.output_underflows,
            "output_overflows": self.output_overflows,
            "priming_output": self.priming_output,
            "render_starvations": self.render_starvations,
            "synthesis_waits": self.synthesis_waits,
            "stream_starvations": self.stream_starvations,
            "ring_low_water_ms": round(1000 * self.ring_low_water_frames / rate, 1)
            if self.ring_low_water_frames >= 0 else None,
            "max_pull_ms": round(self.max_pull_ms, 3),
            "mean_pull_ms": round(self.total_pull_ms / self.callbacks, 4) if self.callbacks else None,
            "device_reopens": self.device_reopens,
        }
