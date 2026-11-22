"""Word timing (05 § 8).

Kokoro (and the fixture voice) report exact timings. Cloud voices don't, so words inside an utterance are
estimated by syllable weight over the *measured* duration, after trimming leading/trailing silence found
in the audio itself. Sentence boundaries stay exact because each utterance is its own synthesis unit.
"""

from __future__ import annotations

import re

import numpy as np

from open_speech.providers.base import WordTiming

# Words: letters/digits with inner apostrophes or hyphens, and decimals like 3.5 or 1,200.
WORD_RE = re.compile(r"[^\W_]+(?:(?:['’\-]|(?<=\d)[.,](?=\d))[^\W_]+)*")
_VOWEL_GROUPS = re.compile(r"[aeiouyàáâäèéêëìíîïòóôöùúûü]+", re.IGNORECASE)

# Pause weights (in syllable-equivalents) for punctuation that follows a word.
_PAUSES = ((re.compile(r"^[^\w]*[.!?…]"), 1.2), (re.compile(r"^[^\w]*[;:—–]"), 0.8), (re.compile(r"^[^\w]*,"), 0.5))


def word_spans(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in WORD_RE.finditer(text)]


def count_syllables(word: str) -> int:
    """Heuristic English syllable count (good enough to weight timing; never below 1)."""
    w = word.strip("'’-")
    if not w:
        return 1
    digits = sum(c.isdigit() for c in w)
    if digits:
        # "2026" → "twenty twenty-six": numbers speak long.
        return max(1, round(digits * 1.5))
    if w.isupper() and len(w) <= 5 and not _VOWEL_GROUPS.search(w[1:-1] or "x"):
        return len(w) + 2 * w.upper().count("W")  # acronyms are spelled out: S-Q-L
    lw = w.lower()
    groups = len(_VOWEL_GROUPS.findall(lw))
    if lw.endswith("e") and not lw.endswith(("le", "ee", "ye")) and groups > 1:
        groups -= 1
    if lw.endswith(("ed",)) and not lw.endswith(("ted", "ded")) and groups > 1:
        groups -= 1
    return max(1, groups)


def pause_after(text: str, end: int) -> float:
    tail = text[end : end + 4]
    for rx, w in _PAUSES:
        if rx.match(tail):
            return w
    return 0.0


def voiced_bounds(x: np.ndarray, sr: int, threshold_db: float = -38.0) -> tuple[float, float]:
    """First and last moment (seconds) the signal rises within `threshold_db` of its peak frame energy."""
    if x.size == 0 or sr <= 0:
        return 0.0, 0.0
    hop = max(1, sr // 100)  # 10 ms frames
    n = x.size // hop
    if n == 0:
        return 0.0, x.size / sr
    frames = x[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1) + 1e-12)
    peak = float(rms.max())
    if peak <= 1e-6:
        return 0.0, x.size / sr
    voiced = np.nonzero(20 * np.log10(rms / peak) > threshold_db)[0]
    if voiced.size == 0:
        return 0.0, x.size / sr
    return float(voiced[0] * hop) / sr, float(min(x.size, (voiced[-1] + 1) * hop)) / sr


def estimate_word_timings(
    text: str,
    duration_s: float,
    *,
    speech_start_s: float | None = None,
    speech_end_s: float | None = None,
) -> list[WordTiming]:
    """Syllable-weighted word boundaries over the measured duration (spoken-text offsets)."""
    spans = word_spans(text)
    if not spans or duration_s <= 0:
        return []
    start = max(0.0, speech_start_s if speech_start_s is not None else min(0.05, duration_s * 0.05))
    end = speech_end_s if speech_end_s is not None else duration_s - min(0.05, duration_s * 0.05)
    end = min(duration_s, max(end, start + 1e-3))
    weights = [float(count_syllables(text[s:e])) for s, e in spans]
    pauses = [pause_after(text, e) if i < len(spans) - 1 else 0.0 for i, (_, e) in enumerate(spans)]
    total = sum(weights) + sum(pauses)
    scale = (end - start) / total if total > 0 else 0.0
    out: list[WordTiming] = []
    t = start
    for (s, e), w, p in zip(spans, weights, pauses, strict=True):
        t1 = t + w * scale
        out.append(WordTiming(start=s, end=e, t0=round(t, 4), t1=round(t1, 4)))
        t = t1 + p * scale
    return out
