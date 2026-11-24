"""Raised-cosine fades (every cut the listener hears is one of these, never a jump to zero).

Durations used by the renderer (05 § 9): pause/duck 12 ms out and resume 12 ms in; seek, skip and voice change
fade the old audio out over 12 ms while the new fades in; stop 15 ms; a starving stream 5 ms at either edge;
3 ms at the ends of every utterance whose first or last samples aren't already near silence.
"""

from __future__ import annotations

from functools import cache

import numpy as np

PAUSE_MS = 12.0
RESUME_MS = 12.0
SEEK_MS = 12.0
STOP_MS = 15.0
SPEED_MS = 10.0
STARVE_MS = 5.0
EDGE_MS = 3.0
EDGE_SILENT = 1e-3  # an utterance edge quieter than -60 dBFS needs no fade (keeps 1x bit-exact)


@cache
def ramp(n: int) -> np.ndarray:
    """0 → 1 over `n` samples (raised cosine; ramp(n)[i] + ramp(n)[n-1-i] == 1). Cached and read-only."""
    if n <= 0:
        r = np.ones(0, dtype=np.float32)
    else:
        r = (0.5 - 0.5 * np.cos(np.pi * (np.arange(n) + 0.5) / n)).astype(np.float32)
    r.setflags(write=False)
    return r


def frames(ms: float, rate: int) -> int:
    return max(1, int(round(ms * rate / 1000)))


def fade_in(x: np.ndarray, n: int) -> None:
    """In place: the first `n` samples rise from silence."""
    k = min(n, x.size)
    if k:
        x[:k] *= ramp(n)[:k]


def fade_out(x: np.ndarray, n: int) -> None:
    """In place: the last `n` samples fall to silence (a shorter array gets the end of the curve)."""
    k = min(n, x.size)
    if k:
        x[x.size - k :] *= ramp(n)[::-1][n - k :]


def edge_fades(x: np.ndarray, rate: int, *, start: bool, end: bool, ms: float = EDGE_MS) -> None:
    """In place: 3 ms at an utterance's start/end, only where the edge isn't already silent."""
    n = frames(ms, rate)
    if start and x.size and float(np.max(np.abs(x[:n]))) > EDGE_SILENT:
        fade_in(x, n)
    if end and x.size and float(np.max(np.abs(x[-n:]))) > EDGE_SILENT:
        fade_out(x, n)
