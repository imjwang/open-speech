"""Audio quality metrics for the playback chain (numpy only).

Used by the dev harness (`engine/tools/audiolab`), the tests and `open-speech doctor audio`, so a regression in
what the listener hears shows up as a number. Every function takes mono float32 at `sr`.

- `splice_track` / `splice_stats`: where each stretch of the output came from in the source (normalized
  cross-correlation lag tracking). At 1x a clean chain is one straight line (no jumps, residual -inf dB);
  WSOLA shows a staircase whose steps are its splices.
- `clicks`: spikes in the linear-prediction residual (hard cuts, discontinuities), as events per second.
- `frame_modulation`: how strongly the loudness envelope pulses at a given rate (the stretcher's frame rate),
  relative to the neighbouring modulation frequencies. A WSOLA with badly matched splices "buzzes" there.
- `f0_track` / `f0_jitter_cents`: pitch steadiness (flutter).
- `pause_noise_db`: the 4–12 kHz floor in the pauses (hiss, quantization noise, codec noise).
- `log_spectral_distance`: DTW-aligned log-mel distance between two renditions of the same speech.
- `dropouts`: runs of digital silence inside speech (starvation).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EPS = 1e-12


def _f64(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float64).reshape(-1)


def db(x: float) -> float:
    return float(10.0 * np.log10(max(x, EPS)))


def rms_db(x: np.ndarray) -> float:
    y = _f64(x)
    return db(float(np.mean(y * y))) if y.size else -240.0


# ---------------------------------------------------------------------------------------------- splices


@dataclass
class SpliceTrack:
    t_out: np.ndarray  # output sample index of each analysed window
    src: np.ndarray  # best-matching source sample index
    ncc: np.ndarray  # normalized cross-correlation at the match (1.0 = identical shape)


@dataclass
class SpliceStats:
    windows: int
    jumps: int
    jumps_per_s: float
    median_jump_ms: float
    max_jump_ms: float
    low_ncc_frac: float  # windows whose best match is weak (< 0.9): audio no single source stretch explains
    altered_frac: float  # windows that don't sit on the main line (1x only; 0 at other speeds)
    mean_ncc: float


def _sliding_energy(x: np.ndarray, n: int) -> np.ndarray:
    """Energy of every length-`n` window (by FFT convolution: no cumulative-sum cancellation in quiet parts)."""
    sq = x * x
    m = 1 << int(np.ceil(np.log2(sq.size + n)))
    e = np.fft.irfft(np.fft.rfft(sq, m) * np.fft.rfft(np.ones(n), m), m)[n - 1 : sq.size]
    return np.maximum(np.asarray(e), 0.0)


def splice_track(
    src: np.ndarray,
    out: np.ndarray,
    speed: float,
    sr: int,
    *,
    win_ms: float = 20.0,
    step_ms: float = 5.0,
    search_ms: float | None = None,
    active_db: float = -40.0,
) -> SpliceTrack:
    """For every active output window, the source position it matches best near `t * speed`."""
    s, o = _f64(src), _f64(out)
    n = max(32, int(sr * win_ms / 1000))
    step = max(1, int(sr * step_ms / 1000))
    if search_ms is None:
        search_ms = 40.0 + 60.0 * abs(speed - 1.0)
    w = int(sr * search_ms / 1000)
    peak = float(np.max(np.abs(o))) if o.size else 0.0
    thresh = (peak * 10 ** (active_db / 20)) ** 2 * n * 0.25
    ts: list[int] = []
    ps: list[int] = []
    cs: list[float] = []
    src_energy = _sliding_energy(s, n) if s.size >= n else np.zeros(0)
    for t in range(0, o.size - n, step):
        seg = o[t : t + n]
        e = float(seg @ seg)
        if e < thresh:
            continue
        center = int(round(t * speed))
        lo, hi = max(0, center - w), min(s.size - n, center + w)
        if hi <= lo:
            continue
        region = s[lo : hi + n]
        m = 1 << int(np.ceil(np.log2(region.size + n)))
        corr = np.fft.irfft(np.fft.rfft(region, m) * np.conj(np.fft.rfft(seg, m)), m)[: hi - lo + 1]
        # Floor the candidates' energy (relative to the window's): near-silent source stretches would otherwise
        # divide rounding noise by rounding noise.
        denom = np.sqrt(np.maximum(src_energy[lo : hi + 1], e * 1e-3) * e)
        ncc = np.clip(corr / denom, -1.0, 1.0)
        k = int(np.argmax(ncc))
        ts.append(t)
        ps.append(lo + k)
        cs.append(float(ncc[k]))
    return SpliceTrack(np.asarray(ts), np.asarray(ps), np.asarray(cs))


def splice_stats(track: SpliceTrack, sr: int, speed: float, *, jump_ms: float = 0.5) -> SpliceStats:
    if track.t_out.size < 2:
        return SpliceStats(int(track.t_out.size), 0, 0.0, 0.0, 0.0, 0.0, 0.0, float(np.mean(track.ncc)) if
                           track.ncc.size else 0.0)
    dt = np.diff(track.t_out)
    ds = np.diff(track.src)
    contiguous = dt <= dt.min() * 1.01  # only compare neighbouring windows (no silence in between)
    dev = np.abs(ds - dt)[contiguous]
    jumps = dev > sr * jump_ms / 1000
    active_s = float(contiguous.sum() * dt.min()) / sr
    sizes = dev[jumps] * 1000 / sr
    altered = 0.0
    if abs(speed - 1.0) < 1e-6:
        off = track.src - track.t_out
        main = float(np.median(off))
        altered = float(np.mean((np.abs(off - main) > sr * jump_ms / 1000) | (track.ncc < 0.99)))
    return SpliceStats(
        windows=int(track.t_out.size),
        jumps=int(jumps.sum()),
        jumps_per_s=float(jumps.sum()) / active_s if active_s > 0 else 0.0,
        median_jump_ms=float(np.median(sizes)) if sizes.size else 0.0,
        max_jump_ms=float(np.max(sizes)) if sizes.size else 0.0,
        low_ncc_frac=float(np.mean(track.ncc < 0.9)),
        altered_frac=altered,
        mean_ncc=float(np.mean(track.ncc)),
    )


def residual_db(src: np.ndarray, out: np.ndarray, *, max_lag: int = 2048) -> tuple[float, int]:
    """At 1x: the output minus the best-aligned source, relative to the source, in dB (-240 = identical).
    Returns (dB, lag)."""
    s, o = _f64(src), _f64(out)
    n = min(s.size, o.size)
    if n == 0:
        return 0.0, 0
    best = (np.inf, 0)
    probe = min(n, 48000)
    a = s[:probe]
    for lag in range(-max_lag, max_lag + 1, 1):
        if lag >= 0:
            b = o[lag : lag + probe]
            aa = a[: b.size]
        else:
            b = o[: probe + lag]
            aa = s[-lag : -lag + b.size]
        if b.size < probe // 2:
            continue
        err = float(np.sum((aa - b) ** 2))
        if err < best[0]:
            best = (err, lag)
            if err == 0.0:
                break
    lag = best[1]
    if lag >= 0:
        b = o[lag:]
        aa = s[: b.size]
    else:
        aa = s[-lag:]
        b = o[: aa.size]
    m = min(aa.size, b.size)
    num = float(np.sum((aa[:m] - b[:m]) ** 2))
    den = float(np.sum(aa[:m] ** 2))
    return (db(num / den) if num > 0 else -240.0), lag


# ---------------------------------------------------------------------------------------------- clicks


def _lpc(frame: np.ndarray, order: int) -> np.ndarray:
    r = np.correlate(frame, frame, "full")[frame.size - 1 : frame.size + order]
    if r[0] <= EPS:
        return np.concatenate([[1.0], np.zeros(order)])
    r = r.copy()
    r[0] *= 1.0 + 1e-6
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        acc = r[i] + float(np.dot(a[1:i], r[i - 1 : 0 : -1]))
        k = -acc / err
        a[1:i] = a[1:i] + k * a[i - 1 : 0 : -1]
        a[i] = k
        err *= 1.0 - k * k
        if err <= EPS:
            break
    return a


def lpc_residual(x: np.ndarray, sr: int, *, order: int = 16, frame_ms: float = 20.0) -> np.ndarray:
    """Prediction error of a short-time LPC fit (each frame's predictor fitted on the frame ± half a frame)."""
    y = _f64(x)
    n = max(order * 4, int(sr * frame_ms / 1000))
    out = np.zeros_like(y)
    for start in range(0, y.size, n):
        end = min(y.size, start + n)
        lo, hi = max(0, start - n // 2), min(y.size, end + n // 2)
        ctx = y[lo:hi]
        if ctx.size <= order * 2:
            break
        a = _lpc(ctx * np.hanning(ctx.size), order)
        hist = y[max(0, start - order) : end]
        e = np.convolve(hist, a)[: hist.size]
        out[start:end] = e[hist.size - (end - start) :]
    return out


@dataclass
class ClickStats:
    events: int
    per_s: float
    worst_ratio: float  # largest residual spike relative to its neighbourhood's robust level


def clicks(x: np.ndarray, sr: int, *, ratio: float = 30.0, floor_db: float = -50.0, side_ms: int = 10) -> ClickStats:
    """Discontinuities: 1 ms blocks whose LPC-residual peak is `ratio` times the residual level on *both* sides
    (mean of 1 ms block medians over `side_ms` before and after; the larger side is the reference), merged into
    events 3 ms apart. Calibrated on Kokoro speech, whose own transients reach ~25 (0 events at 30)."""
    y = _f64(x)
    blk = max(1, sr // 1000)
    e = np.abs(lpc_residual(y, sr))
    nb = e.size // blk
    if nb < 3:
        return ClickStats(0, 0.0, 0.0)
    blocks = e[: nb * blk].reshape(nb, blk)
    med = np.median(blocks, axis=1)
    peak = blocks.max(axis=1)
    cm = np.concatenate([[0.0], np.cumsum(med)])
    i = np.arange(nb)
    lo = np.maximum(0, i - side_ms)
    pre = (cm[i] - cm[lo]) / np.maximum(1, i - lo)
    a, b = np.minimum(nb, i + 1), np.minimum(nb, i + 1 + side_ms)
    post = (cm[b] - cm[a]) / np.maximum(1, b - a)
    level = np.maximum(np.maximum(pre, post), 1e-6)
    r = peak / level
    ok = peak > 10 ** (floor_db / 20)
    events = 0
    last = -(10**9)
    for j in np.flatnonzero((r > ratio) & ok):
        if j - last > 3:
            events += 1
        last = int(j)
    worst = float(r[ok].max()) if ok.any() else 0.0
    return ClickStats(events, events / (y.size / sr), worst)


def hard_edges(x: np.ndarray, sr: int, *, min_silence_ms: float = 0.5, ratio: float = 0.25,
               floor_db: float = -50.0) -> int:
    """Edges between audio and digital silence that aren't faded: where a run of exact zeros (≥ 0.5 ms) begins
    or ends, the audio's outermost 0.25 ms is still at `ratio` or more of its RMS over the neighbouring 5 ms.
    A hard cut (pause, stop, seek, starvation without a fade) scores 1 per edge; a raised-cosine fade 0."""
    y = _f64(x)
    if y.size == 0:
        return 0
    silent = y == 0.0
    edges = np.diff(np.concatenate([[0], silent.astype(np.int8), [0]]))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    q = max(1, int(sr * 0.00025))
    w = max(q + 1, int(sr * 0.005))
    floor = 10 ** (floor_db / 20)
    n = 0
    for a0, b0 in zip(starts, ends, strict=True):
        a, b = int(a0), int(b0)
        if b - a < sr * min_silence_ms / 1000:
            continue
        if a > 0:  # audio → silence
            ctx = y[max(0, a - w) : a]
            rms = float(np.sqrt(np.mean(ctx * ctx)))
            if rms > floor and float(np.max(np.abs(y[a - min(q, a) : a]))) > ratio * rms:
                n += 1
        if b < y.size:  # silence → audio
            ctx = y[b : b + w]
            rms = float(np.sqrt(np.mean(ctx * ctx)))
            if rms > floor and float(np.max(np.abs(y[b : b + q]))) > ratio * rms:
                n += 1
    return n


# ---------------------------------------------------------------------------------------------- modulation


def bandpass(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    """Brick-wall FFT band-pass (analysis only)."""
    y = _f64(x)
    spec = np.fft.rfft(y)
    f = np.fft.rfftfreq(y.size, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    return np.asarray(np.fft.irfft(spec, y.size))


def frame_modulation(x: np.ndarray, sr: int, rate_hz: float, *, env_rate: int = 2000, seg_s: float = 1.0,
                     band: tuple[float, float] | None = (1000.0, 4000.0)) -> float:
    """dB by which the RMS envelope's modulation spectrum (of the `band`, default 1–4 kHz) peaks at `rate_hz`
    over its ±15 Hz neighbourhood (Welch average over `seg_s` segments of active speech). Natural speech scatters
    within about ±3 dB; a periodic splice artifact stands out above that."""
    y = bandpass(x, sr, *band) if band is not None else _f64(x)
    hop = max(1, sr // env_rate)
    n = y.size // hop
    if n < 8:
        return 0.0
    env = np.sqrt(np.mean((y[: n * hop] ** 2).reshape(n, hop), axis=1))
    fs = sr / hop
    seg = int(seg_s * fs)
    if seg < 16 or env.size < seg:
        return 0.0
    peak = float(np.max(env))
    win = np.hanning(seg)
    acc = np.zeros(seg // 2 + 1)
    used = 0
    for start in range(0, env.size - seg + 1, seg // 2):
        e = env[start : start + seg]
        if float(np.mean(e)) < peak * 0.05:
            continue
        e = (e - e.mean()) * win
        acc += np.abs(np.fft.rfft(e)) ** 2
        used += 1
    if used == 0:
        return 0.0
    f = np.fft.rfftfreq(seg, 1 / fs)
    k = int(np.argmin(np.abs(f - rate_hz)))
    line = float(np.max(acc[max(0, k - 1) : k + 2]))
    near = (np.abs(f - rate_hz) <= 15) & (np.abs(f - rate_hz) > 3)
    floor = float(np.median(acc[near])) if near.any() else EPS
    return db(line / max(floor, EPS))


# ---------------------------------------------------------------------------------------------- pitch


def f0_track(x: np.ndarray, sr: int, *, hop_s: float = 0.01, fmin: float = 60.0, fmax: float = 450.0,
             threshold: float = 0.15) -> np.ndarray:
    """YIN F0 per frame (Hz; 0 where unvoiced)."""
    y = _f64(x)
    tmax = int(sr / fmin)
    tmin = max(2, int(sr / fmax))
    n = tmax * 2
    hop = max(1, int(round(sr * hop_s)))
    out: list[float] = []
    for start in range(0, y.size - n - tmax, hop):
        frame = y[start : start + n + tmax]
        if float(np.mean(frame * frame)) < 1e-7:
            out.append(0.0)
            continue
        a = frame[:n]
        # difference function via FFT
        m = 1 << int(np.ceil(np.log2(frame.size + n)))
        corr = np.fft.irfft(np.fft.rfft(frame, m) * np.conj(np.fft.rfft(a, m)), m)[: tmax + 1]
        energy = np.concatenate([[0.0], np.cumsum(frame * frame)])
        e_shift = energy[np.arange(tmax + 1) + n] - energy[np.arange(tmax + 1)]
        d = float(a @ a) + e_shift - 2 * corr
        d[0] = 0.0
        cm = np.cumsum(d[1:])
        cmnd = np.ones_like(d)
        cmnd[1:] = d[1:] * np.arange(1, tmax + 1) / np.maximum(cm, EPS)
        cand = np.flatnonzero(cmnd[tmin:] < threshold)
        if cand.size == 0:
            out.append(0.0)
            continue
        tau = int(cand[0]) + tmin
        while tau + 1 <= tmax and cmnd[tau + 1] < cmnd[tau]:
            tau += 1
        if 1 <= tau < tmax:
            a0, b0, c0 = cmnd[tau - 1], cmnd[tau], cmnd[tau + 1]
            den = a0 - 2 * b0 + c0
            frac = 0.5 * (a0 - c0) / den if abs(den) > EPS else 0.0
        else:
            frac = 0.0
        out.append(sr / (tau + frac))
    return np.asarray(out)


def f0_jitter_cents(f0: np.ndarray) -> float:
    """Median absolute pitch change between consecutive voiced frames, in cents."""
    v = f0 > 0
    both = v[1:] & v[:-1]
    if not both.any():
        return 0.0
    ratio = f0[1:][both] / f0[:-1][both]
    return float(np.median(np.abs(1200 * np.log2(ratio))))


def f0_deviation(src: np.ndarray, out: np.ndarray, track: SpliceTrack, sr: int) -> tuple[float, float]:
    """Pitch of the output against the pitch of the source content it came from (via `track`): (median absolute
    deviation in cents, fraction of voiced frames off by more than 50 cents). Flutter shows up here."""
    if track.t_out.size < 2:
        return 0.0, 0.0
    hop_out = 0.01
    hop_src = 0.0025
    f_out = f0_track(out, sr, hop_s=hop_out)
    f_src = f0_track(src, sr, hop_s=hop_src)
    tmax = int(sr / 60.0)
    center = (tmax * 2 + tmax) // 2
    t_frames = np.arange(f_out.size) * int(round(sr * hop_out)) + center
    src_pos = np.interp(t_frames, track.t_out + 0.0, track.src + 0.0)
    k = np.clip(np.round((src_pos - center) / int(round(sr * hop_src))).astype(int), 0, max(0, f_src.size - 1))
    if f_src.size == 0:
        return 0.0, 0.0
    ref = f_src[k]
    both = (f_out > 0) & (ref > 0)
    # Only frames the track actually covers (not silence the interpolation bridged).
    near = np.min(np.abs(t_frames[:, None] - track.t_out[None, :]), axis=1) <= int(sr * 0.01)
    sel = both & near
    if not sel.any():
        return 0.0, 0.0
    dev = np.abs(1200 * np.log2(f_out[sel] / ref[sel]))
    return float(np.median(dev)), float(np.mean(dev > 50))


def median_f0(x: np.ndarray, sr: int) -> float:
    f = f0_track(x, sr)
    v = f[f > 0]
    return float(np.median(v)) if v.size else 0.0


# ---------------------------------------------------------------------------------------------- noise


def _stft_power(y: np.ndarray, sr: int, frame_ms: float = 20.0, hop_ms: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    n = int(sr * frame_ms / 1000)
    hop = int(sr * hop_ms / 1000)
    if y.size < n:
        return np.zeros((0, n // 2 + 1)), np.fft.rfftfreq(n, 1 / sr)
    frames = np.lib.stride_tricks.sliding_window_view(y, n)[::hop]
    spec = np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) ** 2 / (np.sum(np.hanning(n) ** 2) * n / 2)
    return spec, np.fft.rfftfreq(n, 1 / sr)


def pause_noise_db(x: np.ndarray, sr: int, *, lo_hz: float = 4000.0, hi_hz: float = 12000.0,
                   pause_db: float = -45.0) -> float:
    """Median 4–12 kHz level (dBFS) in the pauses: frames whose speech band (100–4000 Hz) is `pause_db` below
    the loudest frame. -inf-like (-240) when the pauses are digital silence; nan when there are no pauses."""
    y = _f64(x)
    spec, f = _stft_power(y, sr)
    if spec.shape[0] == 0:
        return float("nan")
    speech = spec[:, (f >= 100) & (f < 4000)].sum(axis=1)
    hi = spec[:, (f >= lo_hz) & (f <= min(hi_hz, sr / 2))].sum(axis=1)
    ref = float(speech.max())
    pauses = speech < ref * 10 ** (pause_db / 10)
    if not pauses.any():
        return float("nan")
    return db(float(np.median(hi[pauses])))


def snr_db(ref: np.ndarray, test: np.ndarray) -> float:
    a, b = _f64(ref), _f64(test)
    m = min(a.size, b.size)
    noise = float(np.sum((a[:m] - b[:m]) ** 2))
    return db(float(np.sum(a[:m] ** 2)) / noise) if noise > 0 else 240.0


# ---------------------------------------------------------------------------------------------- spectra


def _mel_filters(sr: int, n_fft: int, n_mels: int = 40, fmin: float = 60.0, fmax: float | None = None) -> np.ndarray:
    fmax = fmax or sr / 2

    def hz2mel(h: np.ndarray) -> np.ndarray:
        return np.asarray(2595 * np.log10(1 + h / 700))

    def mel2hz(m: np.ndarray) -> np.ndarray:
        return np.asarray(700 * (10 ** (m / 2595) - 1))

    mels = np.linspace(hz2mel(np.array(fmin)), hz2mel(np.array(fmax)), n_mels + 2)
    hz = mel2hz(mels)
    f = np.fft.rfftfreq(n_fft, 1 / sr)
    fb = np.zeros((n_mels, f.size))
    for i in range(n_mels):
        lo, c, hi = hz[i], hz[i + 1], hz[i + 2]
        fb[i] = np.clip(np.minimum((f - lo) / max(c - lo, EPS), (hi - f) / max(hi - c, EPS)), 0, None)
    return fb


def log_mel(x: np.ndarray, sr: int, *, n_mels: int = 40) -> np.ndarray:
    y = _f64(x)
    spec, _f = _stft_power(y, sr, 25.0, 10.0)
    n_fft = int(sr * 0.025)
    fb = _mel_filters(sr, n_fft, n_mels)
    return np.asarray(10 * np.log10(spec @ fb.T + 1e-10))


def dtw_path(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Classic DTW on Euclidean frame distances; returns the aligned index pairs."""
    d = np.sqrt(((a[:, None, :] - b[None, :, :]) ** 2).mean(axis=2))
    n, m = d.shape
    acc = np.full((n + 1, m + 1), np.inf)
    acc[0, 0] = 0.0
    for i in range(1, n + 1):
        row = d[i - 1]
        prev = acc[i - 1]
        best = np.minimum(prev[1:], prev[:-1]) + row  # from (i-1, j) or (i-1, j-1)
        # Horizontal moves: cur[j] = min(best[j-1], cur[j-1] + row[j-1]), solved as a prefix-min scan.
        c = np.concatenate([[0.0], np.cumsum(row)])
        acc[i, 1:] = c[1:] + np.minimum.accumulate(best - c[1:])
    i, j = n, m
    pi: list[int] = []
    pj: list[int] = []
    while i > 0 and j > 0:
        pi.append(i - 1)
        pj.append(j - 1)
        options = (acc[i - 1, j - 1], acc[i - 1, j], acc[i, j - 1])
        k = int(np.argmin(options))
        if k == 0:
            i, j = i - 1, j - 1
        elif k == 1:
            i -= 1
        else:
            j -= 1
    return np.asarray(pi[::-1]), np.asarray(pj[::-1])


def log_spectral_distance(ref: np.ndarray, test: np.ndarray, sr: int, *, aligned: bool = False) -> float:
    """Mean RMS difference (dB) between log-mel frames of two renditions, DTW-aligned unless `aligned`.
    Only frames where the reference is within 50 dB of its loudest frame count (silence would dominate)."""
    a, b = log_mel(ref, sr), log_mel(test, sr)
    if a.shape[0] == 0 or b.shape[0] == 0:
        return float("nan")
    if aligned:
        m = min(a.shape[0], b.shape[0])
        ia = ib = np.arange(m)
    else:
        ia, ib = dtw_path(a, b)
    energy = a.max(axis=1)
    keep = energy[ia] > energy.max() - 50
    diff = a[ia[keep]] - b[ib[keep]]
    return float(np.mean(np.sqrt(np.mean(diff * diff, axis=1)))) if diff.size else float("nan")


# ---------------------------------------------------------------------------------------------- dropouts


def dropouts(x: np.ndarray, sr: int, *, min_ms: float = 2.0, level_db: float = -40.0) -> int:
    """Runs of digital silence (≥ `min_ms`) with speech-level audio within 10 ms on both sides."""
    y = np.abs(_f64(x))
    if y.size == 0:
        return 0
    silent = y < 1e-7
    edges = np.diff(np.concatenate([[0], silent.astype(np.int8), [0]]))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    lvl = 10 ** (level_db / 20)
    near = int(sr * 0.01)
    n = 0
    for a, b in zip(starts, ends, strict=True):
        if b - a < sr * min_ms / 1000 or a == 0 or b >= y.size:
            continue
        if y[max(0, a - near) : a].max(initial=0.0) > lvl and y[b : b + near].max(initial=0.0) > lvl:
            n += 1
    return n


# ---------------------------------------------------------------------------------------------- loudness


def active_rms_db(x: np.ndarray, sr: int, *, gate_db: float = -50.0) -> float:
    """RMS over 10 ms frames within `gate_db` of the loudest (a simple speech-level meter for matching A/B
    loudness)."""
    y = _f64(x)
    n = max(1, sr // 100)
    k = y.size // n
    if k == 0:
        return rms_db(y)
    p = np.mean((y[: k * n] ** 2).reshape(k, n), axis=1)
    keep = p > p.max() * 10 ** (gate_db / 10)
    return db(float(np.mean(p[keep]))) if keep.any() else -240.0
