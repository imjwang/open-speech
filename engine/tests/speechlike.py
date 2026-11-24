"""Speech-like test audio (no model needed): voiced "syllables" with pauses between phrases, over a noise floor."""

from __future__ import annotations

import io

import numpy as np
import soundfile as sf


def syllables(seconds: float, sr: int, *, f0: float = 140.0, level: float = 0.25, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    total = 0.0
    while total < seconds:
        d = float(rng.uniform(0.12, 0.22))
        n = int(d * sr)
        t = np.arange(n) / sr
        f = f0 * float(rng.uniform(0.9, 1.15))
        tone = sum((0.6**k) * np.sin(2 * np.pi * f * (k + 1) * t) for k in range(5))
        env = np.sin(np.pi * np.arange(n) / n) ** 0.6
        out.append((level * env * tone / 2.0).astype(np.float32))
        gap = int(float(rng.uniform(0.02, 0.05)) * sr)
        out.append(np.zeros(gap, dtype=np.float32))
        total += d + gap / sr
    return np.concatenate(out)


def phrases(lengths: list[float], *, sr: int = 48000, pause: float = 0.5, lead: float = 0.6,
            noise_db: float = -65.0, seed: int = 1) -> np.ndarray:
    parts = [np.zeros(int(lead * sr), dtype=np.float32)]
    for i, seconds in enumerate(lengths):
        parts.append(syllables(seconds, sr, seed=seed + i))
        parts.append(np.zeros(int(pause * sr), dtype=np.float32))
    x = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    return (x + rng.normal(0, 10 ** (noise_db / 20), x.size)).astype(np.float32)


def wav_bytes(x: np.ndarray, sr: int = 48000, subtype: str = "FLOAT") -> bytes:
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV", subtype=subtype)
    return buf.getvalue()
