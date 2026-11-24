"""Pitch-preserving time-stretch, 0.5–5x (FR-READ-12, 05 § 9). numpy only.

At exactly 1.0x the stretcher is a **bit-exact bypass**: the output is the input, sample for sample, whatever the
chunking. At any other speed it is WSOLA (waveform-similarity overlap-add):

- Hann-windowed frames (`FRAME_MS`) overlapping `OVERLAP` times (2 = 50 %, 4 = 75 %); the periodic Hann windows
  sum to a constant, so no gain normalization is needed and a well-aligned splice is inaudible;
- each new frame is taken from where the ideal analysis position says (a *fractional* hop of `speed x synthesis
  hop`, accumulated exactly, so the average speed is exact), shifted by up to `TOLERANCE_MS` to the position whose
  shape best continues the frames before it — chosen by *normalized* cross-correlation over the overlap
  (computed by FFT), so loud audio doesn't win over matching shape;
- silence keeps the plain position (nothing to align).

The bypass keeps the WSOLA state warm (the frame grid simply advances along the input), so a speed change in
either direction is a crossfade between copies of the same waveform: no transition is audible.

Streaming API: `process(x)` → output ready so far; `flush()` at the end of the stream; `reset()` after a seek.
`process_mapped(x)` also returns, for every output segment, the source range it plays (`(n_out, src_a, src_b)`,
in samples since the last reset), so the renderer can place words to within ~10 ms instead of ~43 ms.
`stretch()` is the one-shot form (tests, exports).
"""

from __future__ import annotations

import numpy as np

MIN_SPEED = 0.5
MAX_SPEED = 5.0

# Chosen by measurement in the audio lab (`python -m tools.audiolab grid --v2`, docs/05 § 9): splice quality on
# real Kokoro passages and Parakeet round-trip intelligibility. Up to 2x, 40 ms frames at 50 % overlap with a
# ±15 ms search splice most cleanly and lose nothing (WER 1.8–2.5 % vs 1.8 % unstretched). Above 2x the source is
# skipped in big steps, and short sounds get lost: 30 ms frames at 75 % overlap (±10 ms) keep the most (WER at 3x
# 37 % vs 43 % for the old stretcher and 50 % for 40 ms / 50 %).
FRAME_MS = 40.0
TOLERANCE_MS = 15.0
OVERLAP = 2
FAST = 2.0  # above this speed: FAST_GEOMETRY
FAST_GEOMETRY = (30.0, 10.0, 4)  # frame ms, tolerance ms, overlap


def geometry_for(speed: float) -> tuple[float, float, int]:
    """(frame ms, tolerance ms, overlap) for a stretcher started at `speed`."""
    return FAST_GEOMETRY if speed > FAST else (FRAME_MS, TOLERANCE_MS, OVERLAP)

Segment = tuple[int, float, float]  # (output samples, source position at start, source position at end)


def clamp_speed(speed: float) -> float:
    return float(min(MAX_SPEED, max(MIN_SPEED, round(speed, 3))))


def frame_length_for(sample_rate: int, frame_ms: float = FRAME_MS, overlap: int = OVERLAP) -> int:
    """A frame length of about `frame_ms` that divides into `overlap` equal hops (960 at 24 kHz)."""
    n = int(round(sample_rate * frame_ms / 1000))
    return max(64, n + (-n % overlap))


class Stretcher:
    def __init__(
        self,
        sample_rate: int = 24000,
        speed: float = 1.0,
        *,
        frame_ms: float | None = None,
        tolerance_ms: float | None = None,
        overlap: int | None = None,
        match: str = "overlap",
    ) -> None:
        """Frame geometry defaults to `geometry_for(speed)`; it stays fixed for the stretcher's life (the renderer
        starts a new stretcher when the speed changes)."""
        g = geometry_for(clamp_speed(speed))
        frame_ms = g[0] if frame_ms is None else frame_ms
        tolerance_ms = g[1] if tolerance_ms is None else tolerance_ms
        overlap = g[2] if overlap is None else overlap
        if overlap not in (2, 4):
            raise ValueError("overlap must be 2 or 4")
        self.sample_rate = sample_rate
        self.overlap = overlap
        self.frame_length = frame_length_for(sample_rate, frame_ms, overlap)
        n = self.frame_length
        hs = self.synthesis_hop = n // overlap
        self.tolerance = max(1, int(round(sample_rate * tolerance_ms / 1000)))
        self.match = match
        w = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)) * (2.0 / overlap)  # phases sum to exactly 1
        self._w = w.astype(np.float32)
        acc = np.zeros(n - hs)
        for k in range(1, overlap):  # what earlier frames on the same grid still add to the next n - hs samples
            acc[: n - k * hs] += w[k * hs :]
        self._w_acc = acc.astype(np.float32)
        self._match_len = (n - hs) if match == "overlap" else n
        mw = 0.5 - 0.5 * np.cos(2 * np.pi * (np.arange(self._match_len) + 0.5) / self._match_len)
        self._match_w = mw  # emphasise the middle of the compared region, where the crossfade is strongest
        self._fft = 1 << int(np.ceil(np.log2(2 * self._match_len + 2 * self.tolerance)))
        self._speed = clamp_speed(speed)
        self.reset()

    # ------------------------------------------------------------------ state

    @property
    def speed(self) -> float:
        return self._speed

    def set_speed(self, speed: float) -> None:
        self._speed = clamp_speed(speed)

    @property
    def bypass(self) -> bool:
        return self._speed == 1.0

    def reset(self) -> None:
        hs = self.synthesis_hop
        self._buf = np.zeros(0, dtype=np.float32)  # input not yet discarded
        self._base = 0  # absolute source index of _buf[0]
        self._end = 0  # absolute index one past the last input sample
        # The last committed frame starts at _c. Virtual frames sit on the grid before the stream, so the first
        # hop of output is the plain first hop of input.
        self._c = -hs
        # Pending overlap from frames already committed (length frame - hop), or None while it is implicitly the
        # input itself (frames on the plain grid: bypass).
        self._acc: np.ndarray | None = None
        self._settle = 0  # grid frames still to commit before a return to bypass is exact
        self._pos = 0.0  # ideal analysis position of the next frame

    @property
    def held(self) -> int:
        """Source samples taken in but not yet audible in the output (the renderer's lookahead)."""
        return max(0, self._end - (self._c + self.synthesis_hop))

    # ------------------------------------------------------------------ streaming

    def process(self, x: np.ndarray) -> np.ndarray:
        return self.process_mapped(x)[0]

    def process_mapped(self, x: np.ndarray) -> tuple[np.ndarray, list[Segment]]:
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if x.size:
            self._buf = np.concatenate([self._buf, x]) if self._buf.size else x.copy()
            self._end += x.size
        return self._run(final=False)

    def flush(self) -> np.ndarray:
        """End of stream: everything still held comes out (the stretcher is reset afterwards)."""
        return self.flush_mapped()[0]

    def flush_mapped(self) -> tuple[np.ndarray, list[Segment]]:
        out, segs = self._run(final=True)
        self.reset()
        return out, segs

    # ------------------------------------------------------------------ core

    def _x(self, a: int, b: int) -> np.ndarray:
        """Source samples [a, b) (absolute); zeros outside what exists (before the start, past the end)."""
        out = np.zeros(b - a, dtype=np.float32)
        lo, hi = max(a, self._base), min(b, self._base + self._buf.size)
        if hi > lo:
            out[lo - a : hi - a] = self._buf[lo - self._base : hi - self._base]
        return out

    def _commit(self, c: int, natural: int) -> np.ndarray:
        """Overlap-add the frame starting at source `c`; returns the finished hop."""
        hs, n = self.synthesis_hop, self.frame_length
        acc = self._acc if self._acc is not None else self._x(natural, natural + n - hs) * self._w_acc
        frame = self._x(c, c + n) * self._w
        hop = acc[:hs] + frame[:hs]
        nxt = np.zeros(n - hs, dtype=np.float32)
        nxt[: n - 2 * hs] = acc[hs:]
        nxt += frame[hs:]
        self._acc = nxt
        self._c = c
        return hop

    def _tail(self, natural: int) -> tuple[np.ndarray, Segment] | None:
        """At the end of the stream: what the pending overlap still holds, up to where the source ends."""
        k = self._end - natural
        if k <= 0:
            return None
        k = min(k, self.frame_length - self.synthesis_hop)
        tail = (self._acc[:k] if self._acc is not None else self._x(natural, natural + k)).copy()
        return tail, (tail.size, float(natural), float(natural + k))

    def _run(self, *, final: bool) -> tuple[np.ndarray, list[Segment]]:
        hs, n, tol = self.synthesis_hop, self.frame_length, self.tolerance
        parts: list[np.ndarray] = []
        segs: list[Segment] = []
        while True:
            natural = self._c + hs
            if self.bypass and self._acc is None:
                # Plain copy of the next hop (bit-exact); the frame grid advances with it.
                c = natural
                if c + hs > self._end:
                    if final and c < self._end:
                        hop = self._x(c, self._end)
                        parts.append(hop)
                        segs.append((hop.size, float(c), float(self._end)))
                        self._c = self._end - hs
                    break
                parts.append(self._buf[c - self._base : c + hs - self._base].copy())
                segs.append((hs, float(c), float(c + hs)))
                self._c = c
                self._pos = float(c + hs)
            elif self.bypass:
                # Back from WSOLA: frames on the plain grid until the old frames have faded out of the overlap.
                c = natural
                if c + n > self._end and not final:
                    break
                if final and c >= self._end:
                    t = self._tail(natural)
                    if t is not None:
                        parts.append(t[0])
                        segs.append(t[1])
                    self._acc = None
                    self._c = self._end
                    break
                parts.append(self._commit(c, natural))
                segs.append((hs, float(c), float(c + hs)))
                self._pos = float(c + hs)
                self._settle -= 1
                if self._settle <= 0:
                    self._acc = None
            else:
                target = int(round(self._pos))
                if max(target + tol + n, natural + self._match_len) > self._end and not final:
                    break
                if final and target >= self._end:
                    t = self._tail(natural)
                    if t is not None:
                        parts.append(t[0])
                        segs.append(t[1])
                    self._acc = None
                    self._c = self._end
                    break
                c = target + self._best_shift(target, natural)
                hop = self._commit(c, natural)
                self._settle = self.overlap - 1
                self._pos += hs * self._speed
                a, b = float(natural), float(c + hs)
                if final and b > self._end:  # the stream ends inside this hop: stop where the source does
                    keep = max(0, min(hs, int(round(hs * (self._end - a) / (b - a))) if b > a else 0))
                    if keep:
                        parts.append(hop[:keep])
                        segs.append((keep, a, float(self._end)))
                    self._acc = None
                    self._c = self._end
                    break
                parts.append(hop)
                segs.append((hs, a, b))
            self._discard()
        out = np.concatenate(parts).astype(np.float32, copy=False) if parts else np.zeros(0, dtype=np.float32)
        return out, segs

    def _best_shift(self, target: int, natural: int) -> int:
        """δ in [-tol, tol] maximizing the normalized cross-correlation between the candidate frame's start and
        the natural continuation of the frames already laid down (over the region they overlap)."""
        tol, m = self.tolerance, self._match_len
        lo = max(target - tol, self._base, 0)
        hi = target + tol
        if hi < lo:
            return lo - target
        tmpl = self._x(natural, natural + m).astype(np.float64) * self._match_w
        e_t = float(tmpl @ tmpl)
        if e_t < 1e-10:  # silence: nothing to align to
            return max(lo, target) - target
        region = self._x(lo, hi + m).astype(np.float64)
        k = hi - lo + 1
        f = self._fft
        corr = np.fft.irfft(np.fft.rfft(region, f) * np.conj(np.fft.rfft(tmpl, f)), f)[:k]
        # Candidate energies under the same weighting: sum(w² x²) over each window, also by correlation.
        e_c = np.fft.irfft(np.fft.rfft(region * region, f) * np.conj(np.fft.rfft(self._match_w ** 2, f)), f)[:k]
        score = corr / np.sqrt(np.maximum(e_c, e_t * 1e-3) * e_t)
        return lo + int(np.argmax(score)) - target

    def _discard(self) -> None:
        keep_from = min(self._c, int(np.floor(self._pos)) - self.tolerance)
        drop = keep_from - self._base
        if drop > 4096:
            self._buf = self._buf[drop:]
            self._base += drop


def stretch(x: np.ndarray, speed: float, sample_rate: int = 24000, **kw: object) -> np.ndarray:
    """One-shot pitch-preserving stretch; output length ≈ len(x) / speed (exactly len(x) at 1.0x)."""
    s = Stretcher(sample_rate, speed, **kw)  # type: ignore[arg-type]
    out = s.process(x)
    tail = s.flush()
    return np.concatenate([out, tail]).astype(np.float32)
