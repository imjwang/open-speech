"""Word timing (05 § 8).

Kokoro (and the fixture voice) report exact timings. Cloud voices (Chirp, Gemini, OpenAI) don't, so words inside
an utterance are estimated from the audio: `estimate_word_timings_from_audio` finds the pauses, matches them to
the punctuation they belong to, and spreads words by syllable weight between those pins (measured against
Kokoro's exact times: docs/05 § 8). `estimate_word_timings` is the plain syllable estimate over a duration, used
when there is no audio to look at. Sentence boundaries stay exact because each utterance is its own synthesis
unit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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


# ------------------------------------------------------------------ pause-anchored estimate (voices without timestamps)
#
# The syllable estimate above spreads words evenly over the whole utterance, so every pause the voice makes
# (after a comma, between sentences, to breathe) pushes the highlight ahead of the speech before it and behind
# after it. Pauses are easy to find in the audio itself, and most of them sit at punctuation. So: find the pauses,
# decide which word boundary each one is (a monotonic alignment that may leave pauses unexplained — a stop
# consonant's closure — and punctuation without a pause), pin those boundaries to the pause edges, and spread
# words by syllables only *between* pins.

PAUSE_DB = -40.0  # a pause is quieter than this, relative to the loudest 10 ms frame...
EDGE_DB = -40.0  # where speech starts and ends (word-final sibilants are quiet)
MIN_PAUSE_S = 0.08  # ...for at least this long (shorter silences are mostly stop-consonant closures)
FRAME_S = 0.01
SKIP_PAUSE_BASE, SKIP_PAUSE_PER_S = 0.3, 8.0  # leaving a pause unexplained (long ones rarely are)
SIGMA_S, SIGMA_FRAC = 0.25, 0.05  # how far (speech time) a pause may sit from where the weights put its boundary
BREATH_GAP_S = 0.08  # two pauses split by a short, quiet sound (a breath) are one pause
BREATH_DB = -30.0

# Boundary classes by the punctuation between two words: none, comma, clause (; : — –), sentence (. ! ? …).
_CLASS_SENTENCE = re.compile(r"[.!?…\n]")
_CLASS_CLAUSE = re.compile(r"[;:—–]|\s-\s")
_CLASS_COMMA = re.compile(r"[,()\[\]\"“”«»]")
# Matching a pause to a boundary of this class. Speakers do pause where there is no punctuation, but a pause that
# lands near an unpunctuated boundary is more often a closure inside a word: it must fit the weights well.
_MATCH_BONUS = (-2.0, 1.2, 1.6, 2.2)
_SKIP_SLOT_COST = (0.0, 0.4, 0.9, 1.6)  # punctuation the voice didn't pause at
_GAP_WEIGHT = (0.0, 0.5, 0.8, 1.2)  # weight left between words at punctuation without a detected pause (as above)


@dataclass(frozen=True)
class Pause:
    t0: float
    t1: float

    @property
    def length(self) -> float:
        return self.t1 - self.t0


def frame_levels_db(x: np.ndarray, sr: int, frame_s: float = FRAME_S) -> tuple[np.ndarray, int]:
    """Level of each frame in dB relative to the loudest frame (RMS), and the hop in samples."""
    hop = max(1, int(round(sr * frame_s)))
    n = x.size // hop
    if n == 0:
        return np.zeros(0), hop
    frames = x[: n * hop].astype(np.float64).reshape(n, hop)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    peak = float(rms.max())
    if peak <= 1e-6:
        return np.full(n, -120.0), hop
    return 20 * np.log10(rms / peak), hop


def detect_pauses(
    x: np.ndarray,
    sr: int,
    *,
    threshold_db: float | None = None,
    min_pause_s: float | None = None,
) -> tuple[float, float, list[Pause]]:
    """(speech start, speech end, pauses inside the speech) in seconds. A pause is a run of frames below
    `threshold_db` (re the loudest frame) lasting at least `min_pause_s`; pauses split only by a short quiet sound
    (a breath) are merged."""
    threshold_db = PAUSE_DB if threshold_db is None else threshold_db
    min_pause_s = MIN_PAUSE_S if min_pause_s is None else min_pause_s
    db, hop = frame_levels_db(x, sr)
    if db.size == 0:
        return 0.0, x.size / sr if sr else 0.0, []
    loud = db > threshold_db
    edges = np.nonzero(db > EDGE_DB)[0]
    if edges.size == 0:
        return 0.0, x.size / sr, []
    first, last = int(edges[0]), int(edges[-1])
    runs: list[tuple[int, int]] = []  # [start, end) frame ranges of quiet inside the speech
    i = first
    while i <= last:
        if loud[i]:
            i += 1
            continue
        j = i
        while j <= last and not loud[j]:
            j += 1
        runs.append((i, j))
        i = j
    merged: list[tuple[int, int]] = []
    gap_frames = int(round(BREATH_GAP_S / FRAME_S))
    for a, b in runs:
        if merged:
            pa, pb = merged[-1]
            if a - pb <= gap_frames and float(db[pb:a].max(initial=-120.0)) < BREATH_DB:
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    min_frames = int(round(min_pause_s / FRAME_S))
    pauses = [Pause(a * hop / sr, b * hop / sr) for a, b in merged if b - a >= min_frames]
    return first * hop / sr, min(x.size, (last + 1) * hop) / sr, pauses


def word_weight(word: str) -> float:
    """How long a word takes to say, relative to other words (for spreading words between pins): syllables and
    letters both count. Fitted on Kokoro's exact timings (tools/timing_eval.py): syllables alone put long words
    with few vowels ("strengths", "January") too short; letters alone overweight silent letters."""
    return 0.5 * count_syllables(word) + 0.15 * max(1, sum(c.isalpha() for c in word))


def boundary_class(text: str, end: int, next_start: int) -> int:
    """0 none, 1 comma, 2 clause, 3 sentence: the punctuation between a word ending at `end` and the next."""
    between = text[end:next_start]
    if _CLASS_SENTENCE.search(between):
        return 3
    if _CLASS_CLAUSE.search(between):
        return 2
    if _CLASS_COMMA.search(between):
        return 1
    return 0


def _align(
    pauses: list[Pause],
    classes: list[int],
    cum_syl: list[float],
    speech_before: list[float],
    total_speech: float,
) -> list[int | None]:
    """For each pause, the word boundary it is (index into `classes`) or None. Monotonic dynamic programming:
    a match scores the boundary's punctuation, the pause's length and how close the boundary's share of the
    syllables is to the pause's share of the speech time; leaving out a pause or a punctuated boundary costs."""
    m, n = len(pauses), len(classes)
    total_syl = cum_syl[-1] if cum_syl else 1.0
    sigma = SIGMA_S + SIGMA_FRAC * total_speech  # seconds of speech time: the rate prior drifts along the way
    neg = -1e18
    score = np.full((m + 1, n + 1), neg)
    back = np.zeros((m + 1, n + 1), dtype=np.int8)  # 1 skip pause, 2 skip boundary, 3 match
    score[0, 0] = 0.0

    def skip_pause(p: Pause) -> float:
        return SKIP_PAUSE_BASE + SKIP_PAUSE_PER_S * max(0.0, p.length - MIN_PAUSE_S)

    for i in range(m + 1):
        for j in range(n + 1):
            cur = score[i, j]
            if cur <= neg / 2:
                continue
            if i < m:
                v = cur - skip_pause(pauses[i])
                if v > score[i + 1, j]:
                    score[i + 1, j], back[i + 1, j] = v, 1
            if j < n:
                v = cur - _SKIP_SLOT_COST[classes[j]]
                if v > score[i, j + 1]:
                    score[i, j + 1], back[i, j + 1] = v, 2
            if i < m and j < n:
                expected = cum_syl[j] / total_syl * total_speech
                d = (speech_before[i] - expected) / sigma
                length_bonus = min(1.5, 4.0 * pauses[i].length)
                v = cur + _MATCH_BONUS[classes[j]] + length_bonus - d * d
                if v > score[i + 1, j + 1]:
                    score[i + 1, j + 1], back[i + 1, j + 1] = v, 3
    out: list[int | None] = [None] * m
    i, j = m, n
    while i > 0 or j > 0:
        step = back[i, j]
        if step == 3:
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    return out


def estimate_word_timings_from_audio(text: str, x: np.ndarray, sr: int) -> list[WordTiming]:
    """Word boundaries for audio without provider timestamps (Chirp, Gemini, OpenAI…): syllable weights between
    pauses found in the audio and matched to punctuation (see above). Spoken-text offsets, seconds at 1x."""
    spans = word_spans(text)
    if not spans or x.size == 0 or sr <= 0:
        return []
    start, end, pauses = detect_pauses(x, sr)
    if end - start <= 1e-3:
        return estimate_word_timings(text, x.size / sr)
    syl = [word_weight(text[s:e]) for s, e in spans]
    classes = [boundary_class(text, spans[k][1], spans[k + 1][0]) for k in range(len(spans) - 1)]
    cum_syl = list(np.cumsum(syl))
    match: list[int | None] = [None] * len(pauses)
    removed = [True] * len(pauses)  # first pass: every pause is believed to be silence between words
    for _ in range(2):
        speech_before: list[float] = []
        acc, last_t = 0.0, start
        for p, rm in zip(pauses, removed, strict=True):
            acc += p.t0 - last_t
            speech_before.append(acc)
            last_t = p.t1 if rm else p.t0  # a pause not believed to be between words is speech time
        total_speech = acc + (end - last_t)
        match = _align(pauses, classes, cum_syl, speech_before, max(1e-3, total_speech))
        removed = [m is not None for m in match]
    # Pins: boundary k → (end of word k, start of word k + 1) at the matched pause's edges.
    pins = {k: p for p, k in zip(pauses, match, strict=True) if k is not None}
    out: list[WordTiming] = []
    seg_first, seg_t0 = 0, start
    for k in range(len(spans)):
        boundary_pin = pins.get(k) if k < len(spans) - 1 else None
        if boundary_pin is None and k < len(spans) - 1:
            continue
        seg_t1 = boundary_pin.t0 if boundary_pin is not None else end
        words = range(seg_first, k + 1)
        gaps = [_GAP_WEIGHT[classes[w]] if w < k else 0.0 for w in words]
        total = sum(syl[w] for w in words) + sum(gaps)
        scale = max(0.0, seg_t1 - seg_t0) / total if total > 0 else 0.0
        t = seg_t0
        for w, gap in zip(words, gaps, strict=True):
            t1 = t + syl[w] * scale
            out.append(WordTiming(start=spans[w][0], end=spans[w][1], t0=round(t, 4), t1=round(t1, 4)))
            t = t1 + gap * scale
        if boundary_pin is not None:
            seg_first, seg_t0 = k + 1, boundary_pin.t1
    return out
