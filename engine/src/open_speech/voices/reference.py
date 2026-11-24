"""From a recording or a file to the reference a cloned voice is made from (08 § Voice cloning).

Qwen3-TTS Base clones a voice from a few seconds of speech and their exact transcript (in-context learning). What it
hears, it imitates: room noise, hum, distortion and level jumps come back in every sentence the clone says. So the
reference is chosen and cleaned, not just trimmed:

1. Decode whatever was given (WAV, FLAC, MP3, Ogg through libsndfile; M4A/AAC/CAF through macOS `afconvert`), mix to
   mono, resample to 24 kHz with soxr (the model's rate) and to 16 kHz for voice activity detection.
2. Find the speech: Silero VAD (shipped with the on-device speech model's weights), or an energy detector when it
   isn't installed. Runs closer than 128 ms are one phrase; shorter than 96 ms is a click, not speech.
3. Measure: original sample rate, clipping (samples at the recording's ceiling, at its own rate), signal-to-noise
   (speech frames against the pauses), total speech. Each has a warning level and a rejection level (`LIMITS`).
4. Choose the window: the stretch of 6-10 s (at most 15 s) that starts and ends in a pause, scored on how much of it
   is speech (no long gaps), its signal-to-noise, how even its level is, no clipping, and how clean its edges are
   (a longer pause at each end is a sentence boundary, so the transcript cut stays exact).
5. Clean it: a 50 Hz high-pass (rumble, DC), voiced RMS to -20 dBFS, a look-ahead limiter at -1 dBFS for the few
   peaks above it, 20 ms raised-cosine fades, and at least 0.3 s of silence before the speech and 0.6 s after it
   (the clone starts its sentences cleanly only after a pause; see PAD_TAIL_S).

The caller decides what the window says (the passage that was read, checked by the speech recognizer, or the
recognizer's transcript for a file) and makes the check synthesis (service/clones.py).
"""

from __future__ import annotations

import contextlib
import math
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import soundfile as sf

from open_speech.providers.pcm import resample

PROCESSING_VERSION = "ref-v2"  # changes when the processing changes audibly (part of every clone's cache identity)
TARGET_SR = 24000  # Qwen3-TTS's rate
VAD_SR = 16000
FRAME_S = 0.032  # 512 samples at 16 kHz: Silero's window
MERGE_GAP_FRAMES = 4  # gaps shorter than 128 ms are inside a phrase
MIN_RUN_FRAMES = 3  # speech shorter than 96 ms is a click
TARGET_RMS_DB = -20.0
PEAK_DB = -1.0
FADE_S = 0.020
HIGHPASS_HZ = (40.0, 70.0)  # raised-cosine transition band of the high-pass
WINDOW_MIN_S = 6.0
WINDOW_MAX_S = 10.0
WINDOW_HARD_MAX_S = 15.0
LEAD_S = 0.15  # silence kept before the first word, and
TAIL_S = 0.25  # after the last one (at most half the pause on that side)
# Silence the reference ends up with around its speech, made up with digital silence when the recording has less.
# The model continues from the reference's last sample: measured on a reference cut right after its last word (a
# trimmed file), 3 of 12 test sentences lost their first words ("Please remember to water" → "Pleater"); with 0.3 s
# before and 0.6 s after, none of 24 did (two voices, 4 sentences x 3 seeds, Parakeet round trip).
PAD_LEAD_S = 0.3
PAD_TAIL_S = 0.6
AFCONVERT = "/usr/bin/afconvert"
AFCONVERT_EXTS = {".m4a", ".mp4", ".aac", ".caf", ".m4b", ".3gp", ".amr"}


@dataclass(frozen=True)
class Limits:
    """Where a recording gets a warning, and where it is turned down (with a reason the user can act on)."""

    min_speech_s: float = 5.0  # reject below: the model needs a few full phrases
    reject_rate: int = 16000  # reject below (telephone audio)
    warn_rate: int = 22050  # warn below: the clone sounds muffled
    warn_clip_pct: float = 0.1  # warn above (share of the window's samples at the ceiling) …
    # … and reject when that ceiling is full scale: digital clipping, distortion every sentence would carry
    reject_snr_db: float = 18.0
    warn_snr_db: float = 25.0
    warn_level_db: float = -40.0  # voiced level below this: a very quiet recording


LIMITS = Limits()


class ReferenceRejected(Exception):  # noqa: N818 — a verdict, with a reason code the service turns into words
    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


@dataclass
class Quality:
    sample_rate: int  # of the original recording
    duration_s: float  # of the original recording
    speech_s: float  # speech found in all of it
    snr_db: float  # speech against pauses, in the chosen window (capped at 80)
    clipping_pct: float  # share of the window's samples at the recording's ceiling
    level_db: float  # voiced RMS of the window before normalization

    def as_dict(self) -> dict[str, float | int]:
        return {
            "sample_rate": self.sample_rate,
            "duration_s": round(self.duration_s, 2),
            "speech_s": round(self.speech_s, 2),
            "snr_db": round(self.snr_db, 1),
            "clipping_pct": round(self.clipping_pct, 3),
            "level_db": round(self.level_db, 1),
        }


@dataclass
class Window:
    start_s: float  # in the original recording, padding included
    end_s: float
    speech_start_s: float  # first and last speech frame of the window
    speech_end_s: float
    score: float = 0.0


@dataclass
class Prepared:
    audio: np.ndarray  # the reference: 24 kHz mono float32, cleaned
    window: Window
    quality: Quality
    warnings: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return self.audio.size / TARGET_SR

    def audio_16k(self) -> np.ndarray:
        return resample(self.audio, TARGET_SR, VAD_SR)


# ---------------------------------------------------------------------------------------------- decoding


def decode(path: Path) -> tuple[np.ndarray, int]:
    """Any audio file → (mono float32, sample rate). M4A/AAC and anything libsndfile can't read go through macOS
    `afconvert` first. Raises ReferenceRejected("unreadable") when neither can."""
    path = Path(path)
    if path.suffix.lower() not in AFCONVERT_EXTS:
        try:
            return _read(path)
        except (sf.LibsndfileError, RuntimeError, ValueError, OSError):
            pass
    if not Path(AFCONVERT).exists():
        raise ReferenceRejected("unreadable", name=path.name)
    with tempfile.TemporaryDirectory(prefix="os-ref-") as tmp:
        out = Path(tmp) / "decoded.wav"
        try:
            subprocess.run([AFCONVERT, "-f", "WAVE", "-d", "LEF32", str(path), str(out)], check=True,
                           capture_output=True, timeout=120)
            return _read(out)
        except (subprocess.SubprocessError, OSError, sf.LibsndfileError, RuntimeError, ValueError) as exc:
            raise ReferenceRejected("unreadable", name=path.name) from exc


def _read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return mono(y), int(sr)


def mono(y: np.ndarray) -> np.ndarray:
    """Average the channels, unless they cancel out (one channel inverted): then the louder one."""
    if y.ndim == 1:
        return np.asarray(y, dtype=np.float32)
    if y.shape[1] == 1:
        return np.ascontiguousarray(y[:, 0], dtype=np.float32)
    a, b = y[:, 0].astype(np.float64), y[:, 1].astype(np.float64)
    denom = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b))) or 1.0
    if float(np.dot(a, b)) / denom < -0.5:
        return np.asarray(a if np.dot(a, a) >= np.dot(b, b) else b, dtype=np.float32)
    return np.asarray(y.mean(axis=1), dtype=np.float32)


# ---------------------------------------------------------------------------------------------- speech detection


class SpeechDetector(Protocol):
    def frames(self, x16: np.ndarray) -> np.ndarray:
        """One bool per 512-sample frame of 16 kHz audio: speech or not."""
        ...


class SileroVad:
    """Silero VAD through sherpa-onnx (the model the bar's speech input ships with). Its detector reports speech
    segments with their true start (the raw per-window decisions lag the audio by ~150 ms), which are put on the
    512-sample frame grid."""

    def __init__(self, model_path: Path, threshold: float = 0.5) -> None:
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.min_silence_duration = 0.1
        cfg.silero_vad.window_size = 512
        cfg.sample_rate = VAD_SR
        cfg.num_threads = 1
        self._cfg = cfg
        self._sherpa = sherpa_onnx

    def frames(self, x16: np.ndarray) -> np.ndarray:
        n = len(x16) // 512
        out = np.zeros(n, dtype=bool)
        det = self._sherpa.VoiceActivityDetector(self._cfg, buffer_size_in_seconds=max(30.0, len(x16) / VAD_SR + 5))
        x = _leveled(x16)
        for i in range(0, len(x), 512):
            det.accept_waveform(x[i : i + 512])
        det.flush()
        while not det.empty():
            seg = det.front
            a = int(seg.start) // 512
            b = -(-(int(seg.start) + len(seg.samples)) // 512)
            out[max(0, a) : min(n, b)] = True
            det.pop()
        return out


class EnergyVad:
    """Speech = frames clearly above the recording's own quiet level (when Silero isn't installed, and in tests)."""

    def __init__(self, rise_db: float = 12.0, below_peak_db: float = 40.0, floor_db: float = -60.0) -> None:
        self.rise_db = rise_db
        self.below_peak_db = below_peak_db
        self.floor_db = floor_db

    def frames(self, x16: np.ndarray) -> np.ndarray:
        size = 512
        n = len(x16) // size
        if n == 0:
            return np.zeros(0, dtype=bool)
        y = highpass(np.asarray(x16, dtype=np.float32), VAD_SR, (70.0, 110.0))  # hum and rumble aren't speech
        f = y[: n * size].reshape(n, size).astype(np.float64)
        db = 10 * np.log10(np.mean(f * f, axis=1) + 1e-12)
        quiet = float(np.percentile(db, 10))
        peak = float(np.max(db))
        threshold = max(quiet + self.rise_db, peak - self.below_peak_db, self.floor_db)
        return np.asarray(db > threshold)


class LevelVad:
    """Anything above a fixed level is speech: for audio our own model generated (a designed voice), which has
    clean silence around it and nothing else."""

    def __init__(self, floor_db: float = -50.0) -> None:
        self.floor_db = floor_db

    def frames(self, x16: np.ndarray) -> np.ndarray:
        n = len(x16) // 512
        if n == 0:
            return np.zeros(0, dtype=bool)
        f = x16[: n * 512].reshape(n, 512).astype(np.float64)
        return np.asarray(10 * np.log10(np.mean(f * f, axis=1) + 1e-12) > self.floor_db)


def _leveled(x: np.ndarray, peak: float = 0.7) -> np.ndarray:
    top = float(np.max(np.abs(x))) if x.size else 0.0
    if top <= 1e-6:
        return np.asarray(x, dtype=np.float32)
    return (x * min(peak / top, 1000.0)).astype(np.float32)


def speech_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Frame ranges [start, end) of speech: gaps under 128 ms closed, runs under 96 ms dropped."""
    runs: list[tuple[int, int]] = []
    i, n = 0, len(mask)
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j < n and mask[j]:
            j += 1
        runs.append((i, j))
        i = j
    merged: list[tuple[int, int]] = []
    for s, e in runs:
        if merged and s - merged[-1][1] < MERGE_GAP_FRAMES:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= MIN_RUN_FRAMES]


# ---------------------------------------------------------------------------------------------- measuring


def _frame_rms(x: np.ndarray, sr: int, n_frames: int) -> np.ndarray:
    out = np.zeros(n_frames)
    for f in range(n_frames):
        a, b = int(round(f * FRAME_S * sr)), int(round((f + 1) * FRAME_S * sr))
        seg = x[a:b].astype(np.float64)
        out[f] = math.sqrt(float(np.mean(seg * seg))) if seg.size else 0.0
    return out


def _db(x: float) -> float:
    return 20 * math.log10(max(x, 1e-10))


def _power_mean(rms: np.ndarray) -> float:
    return math.sqrt(float(np.mean(rms * rms))) if rms.size else 0.0


def noise_level(rms: np.ndarray, speech: np.ndarray, x: np.ndarray | None = None, sr: int = TARGET_SR) -> float:
    """RMS of the pauses: non-speech frames at least 64 ms from any speech (the median, robust to a bump). A
    recording with hardly any pause (speech from end to end) has its noise floor in the gaps between words and in
    stop closures: then the quietest 3 % of 10 ms frames."""
    near = speech.copy()
    for k in (1, 2):
        near[k:] |= speech[:-k]
        near[:-k] |= speech[k:]
    quiet = rms[~near]
    if quiet.size >= 10:
        return float(np.median(quiet))
    if x is not None and x.size >= sr // 10:
        hop = sr // 100
        n = x.size // hop
        f = x[: n * hop].reshape(n, hop).astype(np.float64)
        return float(np.percentile(np.sqrt(np.mean(f * f, axis=1)), 3))
    return float(np.percentile(rms, 10)) if rms.size else 0.0


def flat_tops(x: np.ndarray, sr: int, peak: float | None = None) -> np.ndarray:
    """Samples that sit on a flat top at the recording's ceiling: runs of at least ~0.15 ms within 0.05 % of the peak
    (a clipped waveform holds there for many samples; a natural peak passes through in a couple)."""
    if x.size == 0:
        return np.zeros(0, dtype=bool)
    top = float(np.max(np.abs(x))) if peak is None else peak
    if top < 0.05:
        return np.zeros(x.size, dtype=bool)
    at = np.abs(x) >= 0.9995 * top
    min_run = max(3, int(round(sr * 0.00015)))
    edges = np.diff(np.concatenate([[0], at.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    flat = np.zeros(x.size, dtype=bool)
    for a, b in zip(starts, ends, strict=True):
        if b - a >= min_run:
            flat[a:b] = True
    return flat


def clipping_share(x: np.ndarray, sr: int = TARGET_SR) -> tuple[float, bool]:
    """(share of samples on flat tops at the ceiling, in %, whether that ceiling is full scale)."""
    if x.size == 0:
        return 0.0, False
    peak = float(np.max(np.abs(x)))
    share = float(np.count_nonzero(flat_tops(x, sr, peak))) / x.size * 100.0
    return share, peak >= 0.99


# ---------------------------------------------------------------------------------------------- the window


def _duration_score(span: float) -> float:
    """6-10 s is what the model clones best from (a little longer is a little better); past 10 s the score drops
    below anything in range (12 s: 0.5), and a window is never longer than 15 s."""
    if span < 3.0:
        return 0.0
    if span < WINDOW_MIN_S:
        return 0.4 + 0.5 * (span - 3.0) / (WINDOW_MIN_S - 3.0)
    if span <= WINDOW_MAX_S:
        return 0.9 + 0.1 * (span - WINDOW_MIN_S) / (WINDOW_MAX_S - WINDOW_MIN_S)
    if span <= 12.0:
        return 1.0 - 0.25 * (span - WINDOW_MAX_S)
    return max(0.0, 0.5 - 0.1 * (span - 12.0))


def choose_window(
    runs: Sequence[tuple[int, int]], rms24: np.ndarray, clip_frames: np.ndarray, noise: float, n_frames: int
) -> Window:
    """The best window of whole phrases (see the module docstring). `runs` are speech frame ranges."""
    if not runs:
        raise ReferenceRejected("no_speech")
    best: tuple[float, int, int] | None = None
    max_frames = int(WINDOW_HARD_MAX_S / FRAME_S)
    for i in range(len(runs)):
        for j in range(i, len(runs)):
            s, e = runs[i][0], runs[j][1]
            if e - s > max_frames:
                break
            span = (e - s) * FRAME_S
            frames = np.arange(s, e)
            speech_frames = sum(min(e, b) - max(s, a) for a, b in runs[i : j + 1])
            continuity = speech_frames / max(1, e - s)
            gaps = [(runs[k + 1][0] - runs[k][1]) * FRAME_S for k in range(i, j)]
            long_gap = max(gaps, default=0.0)
            voiced = np.concatenate([np.arange(a, b) for a, b in runs[i : j + 1]])
            level = rms24[voiced]
            snr = _db(_power_mean(level)) - _db(noise) if noise > 0 else 80.0
            snr_score = min(1.0, max(0.0, (min(snr, 80.0) - 15.0) / 25.0))
            level_db = 20 * np.log10(level + 1e-10)
            evenness = 1.0 - min(1.0, float(np.std(level_db)) / 12.0)
            clipped = float(np.sum(clip_frames[frames]))
            before = (runs[i][0] - runs[i - 1][1]) * FRAME_S if i > 0 else 1.0
            after = (runs[j + 1][0] - runs[j][1]) * FRAME_S if j + 1 < len(runs) else 1.0
            edges = min(1.0, before / 0.4) * 0.5 + min(1.0, after / 0.4) * 0.5
            score = (
                3.0 * _duration_score(span)
                + 1.5 * continuity
                - (1.0 if long_gap > 1.2 else 0.0)
                + 1.0 * snr_score
                + 0.5 * evenness
                + 0.75 * edges
                - (2.0 if clipped > 0 else 0.0)
            )
            if best is None or score > best[0]:
                best = (score, i, j)
    if best is None:  # one phrase longer than the hard maximum: cut it at its quietest frame near 10 s
        s, e = runs[0]
        target = s + int(WINDOW_MAX_S / FRAME_S)
        lo, hi = max(s + int(WINDOW_MIN_S / FRAME_S), target - 30), min(e, target + 30)
        cut = lo + int(np.argmin(rms24[lo:hi])) if hi > lo else min(e, target)
        return _padded(s, cut, None, None, n_frames, 0.0)
    score, i, j = best
    prev_end = runs[i - 1][1] if i > 0 else None
    next_start = runs[j + 1][0] if j + 1 < len(runs) else None
    return _padded(runs[i][0], runs[j][1], prev_end, next_start, n_frames, score)


def _padded(s: int, e: int, prev_end: int | None, next_start: int | None, n_frames: int, score: float) -> Window:
    lead = LEAD_S if prev_end is None else min(LEAD_S, (s - prev_end) * FRAME_S / 2)
    tail = TAIL_S if next_start is None else min(TAIL_S, (next_start - e) * FRAME_S / 2)
    start = max(0.0, s * FRAME_S - lead)
    end = min(n_frames * FRAME_S, e * FRAME_S + tail)
    return Window(start_s=start, end_s=end, speech_start_s=s * FRAME_S, speech_end_s=e * FRAME_S, score=score)


# ---------------------------------------------------------------------------------------------- cleaning


def highpass(x: np.ndarray, sr: int, band: tuple[float, float] = HIGHPASS_HZ) -> np.ndarray:
    """Zero-phase high-pass with a raised-cosine transition (rumble and DC out, speech untouched)."""
    if x.size < 16:
        return x
    n = int(2 ** math.ceil(math.log2(x.size + sr // 10)))
    spec = np.fft.rfft(x.astype(np.float64), n)
    f = np.fft.rfftfreq(n, 1.0 / sr)
    lo, hi = band
    gain = np.clip((f - lo) / (hi - lo), 0.0, 1.0)
    gain = 0.5 - 0.5 * np.cos(np.pi * gain)
    return np.fft.irfft(spec * gain, n)[: x.size].astype(np.float32)


def limit(x: np.ndarray, sr: int, ceiling: float, *, lookahead_s: float = 0.002, release_s: float = 0.05) -> np.ndarray:
    """Hold peaks under `ceiling` with a look-ahead gain envelope (instant attack within the look-ahead, smooth
    release). Speech at -20 dBFS has a few peaks above -1 dBFS; this lowers only those."""
    if x.size == 0 or float(np.max(np.abs(x))) <= ceiling:
        return x
    block = max(1, int(sr * 0.001))
    n_blocks = math.ceil(x.size / block)
    padded = np.pad(np.abs(x), (0, n_blocks * block - x.size))
    peaks = padded.reshape(n_blocks, block).max(axis=1)
    need = np.minimum(1.0, ceiling / np.maximum(peaks, 1e-9))
    ahead = max(1, int(round(lookahead_s / 0.001)))
    win = np.lib.stride_tricks.sliding_window_view(np.pad(need, (ahead, ahead), constant_values=1.0), 2 * ahead + 1)
    need = win.min(axis=1)
    rel = math.exp(-1.0 / max(1.0, release_s / 0.001))
    env = np.empty_like(need)
    g = 1.0
    for k, target in enumerate(need):
        g = target if target < g else target + (g - target) * rel
        env[k] = g
    gain = np.interp(np.arange(x.size), np.arange(n_blocks) * block + block / 2, env)
    out: np.ndarray = np.clip(x * gain, -ceiling, ceiling).astype(np.float32)
    return out


def fade_edges(x: np.ndarray, sr: int, seconds: float = FADE_S) -> np.ndarray:
    n = min(int(sr * seconds), x.size // 2)
    if n <= 0:
        return x
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n))).astype(np.float32)
    y = x.copy()
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def normalize(x: np.ndarray, voiced_rms: float, sr: int = TARGET_SR) -> np.ndarray:
    """Voiced RMS to -20 dBFS, peaks held at -1 dBFS, 20 ms fades."""
    gain = 10 ** ((TARGET_RMS_DB - _db(voiced_rms)) / 20) if voiced_rms > 0 else 1.0
    y = (x * gain).astype(np.float32)
    y = limit(y, sr, 10 ** (PEAK_DB / 20))
    return fade_edges(y, sr)


# ---------------------------------------------------------------------------------------------- the whole thing


def prepare(
    x: np.ndarray, sr: int, *, vad: SpeechDetector | None = None, limits: Limits = LIMITS
) -> Prepared:
    """A clean reference from mono float32 audio at `sr` (see the module docstring). Raises ReferenceRejected with a
    reason code: low_rate, no_speech, too_short, clipped, noisy."""
    x = np.asarray(x, dtype=np.float32).reshape(-1)
    duration = x.size / sr if sr else 0.0
    if sr < limits.reject_rate:
        raise ReferenceRejected("low_rate", rate=sr)
    x24 = resample(x, sr, TARGET_SR)
    x16 = resample(x, sr, VAD_SR)
    detector = vad or EnergyVad()
    mask = detector.frames(x16)
    n_frames = len(mask)
    runs = speech_runs(mask)
    speech = np.zeros(n_frames, dtype=bool)
    for a, b in runs:
        speech[a:b] = True
    speech_s = float(np.sum(speech)) * FRAME_S
    if not runs:
        raise ReferenceRejected("no_speech")
    if speech_s < limits.min_speech_s:
        raise ReferenceRejected("too_short", seconds=round(speech_s, 1), needed=limits.min_speech_s)
    hp24 = highpass(x24, TARGET_SR)
    rms24 = _frame_rms(hp24, TARGET_SR, n_frames)
    noise = noise_level(rms24, speech, hp24, TARGET_SR)
    # Clipping is measured at the recording's own rate (resampling rounds off flat tops into overshoots).
    clip_frames = np.zeros(n_frames)
    flat = flat_tops(x, sr)
    if flat.any():
        for f in range(n_frames):
            a, b = int(round(f * FRAME_S * sr)), int(round((f + 1) * FRAME_S * sr))
            clip_frames[f] = np.count_nonzero(flat[a:b]) / max(1, b - a)
    window = choose_window(runs, rms24, clip_frames, noise, n_frames)
    a, b = int(round(window.start_s * TARGET_SR)), int(round(window.end_s * TARGET_SR))
    segment = hp24[a:b]
    f0, f1 = int(round(window.speech_start_s / FRAME_S)), int(round(window.speech_end_s / FRAME_S))
    voiced = rms24[f0:f1][speech[f0:f1]]
    voiced_rms = _power_mean(voiced)
    snr = min(80.0, _db(voiced_rms) - _db(noise)) if noise > 1e-7 else 80.0
    oa, ob = int(round(window.start_s * sr)), int(round(window.end_s * sr))
    clip_pct, full_scale = clipping_share(x[oa:ob], sr)
    if clip_pct <= limits.warn_clip_pct:
        clip_pct = 0.0 if clip_pct < 0.01 else clip_pct
    quality = Quality(sample_rate=sr, duration_s=duration, speech_s=speech_s, snr_db=snr, clipping_pct=clip_pct,
                      level_db=_db(voiced_rms))
    warnings: list[tuple[str, dict[str, Any]]] = []
    if clip_pct > limits.warn_clip_pct:
        if full_scale:
            raise ReferenceRejected("clipped", pct=round(clip_pct, 2))
        warnings.append(("clipped", {"pct": round(clip_pct, 2)}))
    if snr < limits.reject_snr_db:
        raise ReferenceRejected("noisy", snr=round(snr))
    if snr < limits.warn_snr_db:
        warnings.append(("noisy", {"snr": round(snr)}))
    if sr < limits.warn_rate:
        warnings.append(("low_rate", {"rate": sr}))
    if quality.level_db < limits.warn_level_db:
        warnings.append(("quiet", {"level": round(quality.level_db)}))
    if window.speech_end_s - window.speech_start_s > WINDOW_MAX_S + 0.5:
        warnings.append(("long", {"seconds": round(window.speech_end_s - window.speech_start_s, 1)}))
    audio = normalize(segment, voiced_rms)
    lead = max(0.0, PAD_LEAD_S - (window.speech_start_s - window.start_s))
    tail = max(0.0, PAD_TAIL_S - (window.end_s - window.speech_end_s))
    if lead or tail:
        audio = np.concatenate([np.zeros(int(round(lead * TARGET_SR)), np.float32), audio,
                                np.zeros(int(round(tail * TARGET_SR)), np.float32)])
    return Prepared(audio=audio, window=window, quality=quality, warnings=warnings)


def prepare_file(path: Path, *, vad: SpeechDetector | None = None, limits: Limits = LIMITS) -> Prepared:
    x, sr = decode(path)
    return prepare(x, sr, vad=vad, limits=limits)


def write_wav(path: Path, audio: np.ndarray, sr: int = TARGET_SR) -> None:
    """16-bit PCM WAV (what the model's loader and every tool read); at -20 dBFS that is still ~76 dB of headroom."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.wav")
    sf.write(str(tmp), np.clip(audio, -1.0, 1.0), sr, subtype="PCM_16")
    tmp.replace(path)


def copy_source(src: Path, dest_dir: Path, name: str = "source") -> Path:
    """Keep the original as `source.<ext>` (re-processing later needs it)."""
    ext = src.suffix.lower() or ".bin"
    dest = dest_dir / f"{name}{ext}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(shutil.SameFileError):
        shutil.copyfile(src, dest)
    return dest


__all__ = [
    "LIMITS",
    "PROCESSING_VERSION",
    "TARGET_SR",
    "EnergyVad",
    "LevelVad",
    "Limits",
    "Prepared",
    "Quality",
    "ReferenceRejected",
    "SileroVad",
    "SpeechDetector",
    "Window",
    "choose_window",
    "decode",
    "normalize",
    "prepare",
    "prepare_file",
    "speech_runs",
    "write_wav",
]
