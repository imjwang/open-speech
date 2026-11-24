"""Render speech through the engine's real `Renderer` (render thread, stretcher, ring, fades, limiter) and collect
exactly what an output would have pulled. No sound device: the pull loop here plays the part of the audio
callback, block by block, without a wall clock (it waits for the render thread instead of starving it)."""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from open_speech.playback.render import Renderer, RenderItem

SR = 24000


@dataclass
class Event:
    at_s: float  # output time (seconds of audio pulled) at which to act
    action: str  # "pause" | "resume" | "seek" | "speed" | "stop"
    index: int = 0
    offset: int = 0
    speed: float = 1.0


@dataclass
class Rendered:
    audio: np.ndarray
    positions: list[tuple[int, int | None, int]] = field(default_factory=list)  # (out sample, utt index, src off)
    event_samples: list[int] = field(default_factory=list)  # output sample at which each event took effect


def make_renderer(stretcher_factory: Callable[..., Any] | str | None = None, sr: int = SR) -> Any:
    """The engine's renderer; `"legacy"` = the whole pre-round-3 renderer (old stretcher, ring and cuts)."""
    if stretcher_factory == "legacy":
        from tools.audiolab.legacy_render import Renderer as LegacyRenderer

        return LegacyRenderer(sr)
    if callable(stretcher_factory):
        params = inspect.signature(Renderer).parameters
        if "stretcher_factory" not in params:
            raise SystemExit("this Renderer can't take another stretcher")
        return Renderer(sr, stretcher_factory=stretcher_factory)  # type: ignore[call-arg]
    return Renderer(sr)


def render(
    pcms: list[np.ndarray],
    speed: float = 1.0,
    *,
    stretcher_factory: Callable[..., Any] | str | None = None,
    events: list[Event] | None = None,
    block: int = 480,
    max_s: float = 120.0,
    tail_s: float = 0.0,
) -> Rendered:
    r = make_renderer(stretcher_factory)
    r.set_speed(speed)
    r.start()
    for i, p in enumerate(pcms):
        r.enqueue(RenderItem(i, np.asarray(p, dtype=np.float32)))
    r.mark_end_of_stream()
    r.pause_reading(False)
    pending = sorted(events or [], key=lambda e: e.at_s)
    out: list[np.ndarray] = []
    positions: list[tuple[int, int | None, int]] = []
    event_samples: list[int] = []
    pulled = 0
    finished_at: int | None = None
    idle_until: int | None = None  # paused for good: keep pulling 100 ms (fade tails), then stop
    waited = 0.0
    try:
        while pulled < max_s * SR:
            while pending and pulled >= pending[0].at_s * SR:
                ev = pending.pop(0)
                event_samples.append(pulled)
                if ev.action == "pause":
                    r.pause_reading(True)
                elif ev.action == "resume":
                    r.pause_reading(False)
                elif ev.action == "speed":
                    r.set_speed(ev.speed)
                elif ev.action == "stop":
                    r.pause_reading(True)
                    r.flush(index=ev.index, offset=ev.offset)
                elif ev.action == "seek":
                    r.flush(index=ev.index, offset=ev.offset)
                    for j in range(ev.index, len(pcms)):
                        r.enqueue(RenderItem(j, pcms[j], ev.offset if j == ev.index else 0))
                    r.mark_end_of_stream()
                    r.pause_reading(False)
            snap = r.snapshot()
            if snap.finished and finished_at is None:
                finished_at = pulled
            if finished_at is not None and pulled - finished_at >= tail_s * SR:
                break
            paused = r.reading_paused
            if not paused and finished_at is None and r.buffered_frames() < block and waited < 0.2:
                time.sleep(0.0005)  # give the render thread time (an offline render never starves)
                waited += 0.0005
                continue
            waited = 0.0
            blk = r.pull(block)
            out.append(np.array(blk, dtype=np.float32, copy=True))
            pulled += block
            s = r.snapshot()
            positions.append((pulled, s.index, s.offset))
            if paused and not pending and finished_at is None:
                if idle_until is None:
                    idle_until = pulled + SR // 10
                elif pulled >= idle_until:
                    break  # paused for good (a "stop" scenario)
    finally:
        r.close()
    return Rendered(np.concatenate(out) if out else np.zeros(0, dtype=np.float32), positions, event_samples)
