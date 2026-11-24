from __future__ import annotations

import inspect
import time

import numpy as np
import pytest

from open_speech.cache.audio import AudioCache
from open_speech.cache.keys import audio_key
from open_speech.config import Paths
from open_speech.playback.stretch import Stretcher, stretch
from open_speech.playback.timing import count_syllables, estimate_word_timings, voiced_bounds, word_spans
from open_speech.providers.base import WordTiming
from open_speech.providers.pcm import encode_ogg_opus, resample, tone
from open_speech.store.assets import AssetStore
from open_speech.store.db import Database

from .helpers import fixture_bytes

# ------------------------------------------------------------------ keys


def test_audio_key_excludes_speed_and_covers_what_changes_audio() -> None:
    params = inspect.signature(audio_key).parameters
    assert "speed" not in params
    assert "speed" not in inspect.signature(AudioCache.key).parameters
    k = AudioCache.key("Hello there.", "openai", "gpt-4o-mini-tts", "coral", None)
    assert k == AudioCache.key("Hello there.", "openai", "gpt-4o-mini-tts", "coral", "")
    assert len(k) == 64
    variants = {
        AudioCache.key("Hello there!", "openai", "gpt-4o-mini-tts", "coral"),
        AudioCache.key("Hello there.", "gemini", "gpt-4o-mini-tts", "coral"),
        AudioCache.key("Hello there.", "openai", "other", "coral"),
        AudioCache.key("Hello there.", "openai", "gpt-4o-mini-tts", "ash"),
        AudioCache.key("Hello there.", "openai", "gpt-4o-mini-tts", "coral", "calm"),
        AudioCache.key("Hello there.", "openai", "gpt-4o-mini-tts", "coral", fmt="mp3"),
    }
    assert k not in variants and len(variants) == 6
    # NFC normalization: composed and decomposed é share audio
    assert AudioCache.key("café", "k", "m", "v") == AudioCache.key("café", "k", "m", "v")


# ------------------------------------------------------------------ cache


def _words() -> list[WordTiming]:
    return [WordTiming(0, 5, 0.05, 0.4), WordTiming(6, 11, 0.45, 0.9)]


def test_put_get_roundtrip_preserves_length_and_timings(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    x = tone(220, 1.0, 24000)
    key = AudioCache.key("hello world", "fixture", "tone-v1", "alto")
    put = cache.put(key, x, 24000, _words())
    assert put.path == paths.audio_cache / key[:2] / key[2:4] / f"{key}.ogg"
    assert put.path.read_bytes()[:4] == b"OggS"
    got = cache.get(key)
    assert got is not None and got.sample_rate == 24000
    assert got.words == _words()
    pcm = got.load_pcm()
    assert pcm.dtype == np.float32 and pcm.size == x.size  # libsndfile keeps the exact length
    assert abs(got.duration_s - 1.0) < 1e-6
    assert cache.size_bytes() > 0 and cache.count() == 1


def test_put_resamples_non_opus_rates(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    key = AudioCache.key("x", "p", "m", "v")
    got = cache.put(key, tone(220, 0.5, 22050), 22050, [])
    assert got.sample_rate == 24000
    assert abs(got.load_pcm().size / 24000 - 0.5) < 0.01


def test_put_encoded_keeps_vendor_bytes(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    data = fixture_bytes("openai_speech.ogg")
    key = AudioCache.key("y", "openai", "gpt-4o-mini-tts", "coral")
    got = cache.put_encoded(key, data, _words())
    assert got.path.read_bytes() == data
    assert got.sample_rate == 48000 and abs(got.duration_s - 0.8) < 0.01


def test_lru_eviction_respects_pins_and_access(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    keys = [AudioCache.key(f"utt {i}", "fixture", "tone-v1", "alto") for i in range(5)]
    for k in keys:
        cache.put(k, tone(220, 0.5, 24000), 24000, [])
        time.sleep(0.002)
    each = cache.size_bytes() // 5
    cache.get(keys[0])  # most recently used now
    removed = cache.evict(each * 2 + each // 2, pinned={keys[1]})
    remaining = {k for k in keys if cache.contains(k)}
    assert removed == 3
    assert remaining == {keys[0], keys[1]}  # pinned survives even though it's old; recent survives
    assert cache.size_bytes() <= each * 3
    assert cache.evict(10**9) == 0


def test_clear_keeps_pins_and_index_is_rebuilt_after_reset(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    a, b = AudioCache.key("a", "p", "m", "v"), AudioCache.key("b", "p", "m", "v")
    cache.put(a, tone(220, 0.2, 24000), 24000, [])
    cache.put(b, tone(220, 0.2, 24000), 24000, [])
    db.execute("DELETE FROM assets")  # simulate a lost index: files are adopted on access
    assert cache.size_bytes() == 0
    assert cache.get(a) is not None and cache.size_bytes() > 0
    assert cache.clear(pinned={a}) == 1
    assert cache.contains(a) and not cache.contains(b)


def test_corrupt_sidecar_is_a_miss(paths: Paths, db: Database) -> None:
    cache = AudioCache(AssetStore(paths.cache, db))
    k = AudioCache.key("c", "p", "m", "v")
    got = cache.put(k, tone(220, 0.2, 24000), 24000, [])
    got.path.with_suffix(".json").write_text("{not json")
    assert cache.get(k) is None and not got.path.exists()


# ------------------------------------------------------------------ pcm


def test_resample_and_opus_rates() -> None:
    x = tone(440, 1.0, 48000)
    y = resample(x, 48000, 24000)
    assert abs(y.size - 24000) <= 1
    spec = np.abs(np.fft.rfft(y))
    assert abs(np.argmax(spec) * 24000 / y.size - 440) < 3
    data, sr = encode_ogg_opus(tone(440, 0.2, 44100), 44100)
    assert sr == 48000 and data[:4] == b"OggS"


# ------------------------------------------------------------------ timing


def test_syllables_and_spans() -> None:
    assert count_syllables("cat") == 1
    assert count_syllables("reading") == 2
    assert count_syllables("syllable") == 3
    assert count_syllables("SQL") == 3
    assert count_syllables("2026") >= 4
    assert word_spans("Hi, it's 3.5 well-known.") == [(0, 2), (4, 8), (9, 12), (13, 23)]


def test_estimate_word_timings_monotonic_and_bounded() -> None:
    text = "The quick brown fox, unexpectedly, jumped over the extraordinarily lazy dog."
    words = estimate_word_timings(text, 4.0, speech_start_s=0.2, speech_end_s=3.8)
    assert len(words) == 11
    assert words[0].t0 == pytest.approx(0.2) and words[-1].t1 == pytest.approx(3.8, abs=1e-3)
    for a, b in zip(words, words[1:], strict=False):
        assert a.t0 < a.t1 <= b.t0
    dur = {text[w.start : w.end]: w.t1 - w.t0 for w in words}
    assert dur["extraordinarily"] > dur["fox"] * 3  # syllable weighting
    assert estimate_word_timings("", 1.0) == []


def test_voiced_bounds_trims_silence() -> None:
    sr = 24000
    x = np.concatenate([np.zeros(int(0.3 * sr)), tone(200, 1.0, sr), np.zeros(int(0.5 * sr))]).astype(np.float32)
    a, b = voiced_bounds(x, sr)
    assert 0.28 <= a <= 0.4 and 1.2 <= b <= 1.32


# ------------------------------------------------------------------ WSOLA


@pytest.mark.parametrize("speed", [0.5, 1.0, 1.5, 2.0, 3.0, 5.0])
def test_wsola_length_tracks_speed(speed: float) -> None:
    sr = 24000
    x = tone(220, 3.0, sr, amp=0.3) + tone(330, 3.0, sr, amp=0.1)
    y = stretch(x, speed, sr)
    expected = x.size / speed
    assert abs(y.size - expected) / expected < 0.04


def test_wsola_preserves_pitch() -> None:
    sr = 24000
    t = np.arange(3 * sr) / sr
    x = (0.4 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    for speed in (0.5, 2.0, 4.0):
        y = stretch(x, speed, sr)
        mid = y[y.size // 4 : 3 * y.size // 4]
        f = np.argmax(np.abs(np.fft.rfft(mid))) * sr / mid.size
        assert abs(f - 300) < 6, (speed, f)


def test_streaming_stretcher_speed_change_mid_stream() -> None:
    sr = 24000
    s = Stretcher(sr, 1.0)
    x = tone(220, 4.0, sr)
    out = []
    block = 2000
    for i in range(0, x.size, block):
        if i == x.size // 2:
            s.set_speed(2.0)
        out.append(s.process(x[i : i + block]))
    out.append(s.flush())
    total = sum(o.size for o in out)
    expected = x.size / 2 + x.size / 2 / 2  # first half at 1x, second half at 2x
    assert abs(total - expected) / expected < 0.05
    assert s.speed == 2.0
    s.set_speed(9.0)
    assert s.speed == 5.0


# ------------------------------------------------------------------ pause-anchored word timing


def _speech(parts: list[tuple[str, float]], sr: int = 24000) -> np.ndarray:
    """("word"/"pause", seconds) → a signal: voiced stretches (a buzzy tone) and digital silence."""
    out = []
    for kind, secs in parts:
        n = int(secs * sr)
        if kind == "pause":
            out.append(np.zeros(n, np.float32))
        else:
            t = np.arange(n) / sr
            out.append((0.3 * np.sign(np.sin(2 * np.pi * 140 * t)) * np.hanning(n) ** 0.1).astype(np.float32))
    return np.concatenate(out)


def test_detect_pauses_finds_the_silences_inside_speech() -> None:
    from open_speech.playback.timing import detect_pauses

    sr = 24000
    x = _speech([("pause", 0.2), ("w", 0.8), ("pause", 0.3), ("w", 0.6), ("pause", 0.05), ("w", 0.5),
                 ("pause", 0.4)], sr)
    start, end, pauses = detect_pauses(x, sr)
    assert start == pytest.approx(0.2, abs=0.02) and end == pytest.approx(2.45, abs=0.02)
    assert len(pauses) == 1  # the 50 ms gap is too short to be a pause (a stop consonant's closure)
    assert pauses[0].t0 == pytest.approx(1.0, abs=0.02) and pauses[0].t1 == pytest.approx(1.3, abs=0.02)


def test_pause_anchored_timing_pins_words_to_the_pauses_at_punctuation() -> None:
    from open_speech.playback.timing import estimate_word_timings_from_audio

    sr = 24000
    text = "Before you begin, make sure it is charged. Then press the button."
    # before you begin | 0.25 s | make sure it is charged | 0.5 s | then press the button
    x = _speech([("pause", 0.15), ("w", 0.9), ("pause", 0.25), ("w", 1.4), ("pause", 0.5), ("w", 1.1),
                 ("pause", 0.2)], sr)
    words = estimate_word_timings_from_audio(text, x, sr)
    by = {text[w.start : w.end]: w for w in words}
    assert len(words) == 12
    assert by["Before"].t0 == pytest.approx(0.15, abs=0.02)
    assert by["begin"].t1 == pytest.approx(1.05, abs=0.02)  # ends where the comma's pause starts
    assert by["make"].t0 == pytest.approx(1.30, abs=0.02)  # starts where it ends
    assert by["charged"].t1 == pytest.approx(2.70, abs=0.02)
    assert by["Then"].t0 == pytest.approx(3.20, abs=0.02)
    assert by["button"].t1 == pytest.approx(4.30, abs=0.02)
    for a, b in zip(words, words[1:], strict=False):
        assert a.t0 < a.t1 <= b.t0 + 1e-6
    # The old estimate spreads words over the whole span and drifts at the pauses.
    old = {text[w.start : w.end]: w for w in estimate_word_timings(text, x.size / sr, speech_start_s=0.15,
                                                                  speech_end_s=4.3)}
    assert abs(old["Then"].t0 - 3.20) > abs(by["Then"].t0 - 3.20)


def test_a_pause_with_no_punctuation_near_its_place_is_not_a_word_boundary_pin() -> None:
    from open_speech.playback.timing import estimate_word_timings_from_audio

    sr = 24000
    text = "Strengths and weaknesses aside, the plan was approved."
    # A short gap early in "weaknesses" (a closure), then the real pause at the comma.
    x = _speech([("w", 0.9), ("pause", 0.09), ("w", 0.6), ("pause", 0.35), ("w", 1.5)], sr)
    words = estimate_word_timings_from_audio(text, x, sr)
    by = {text[w.start : w.end]: w for w in words}
    assert by["the"].t0 == pytest.approx(1.94, abs=0.02)  # pinned at the comma's pause
    assert by["aside"].t1 == pytest.approx(1.59, abs=0.02)


def test_pause_anchored_timing_edge_cases() -> None:
    from open_speech.playback.timing import estimate_word_timings_from_audio

    sr = 24000
    assert estimate_word_timings_from_audio("", np.zeros(100, np.float32), sr) == []
    assert estimate_word_timings_from_audio("Hello.", np.zeros(0, np.float32), sr) == []
    silent = estimate_word_timings_from_audio("Hello there.", np.zeros(sr, np.float32), sr)
    assert len(silent) == 2  # nothing to measure: the plain estimate
    one = estimate_word_timings_from_audio("Hello.", _speech([("pause", 0.1), ("w", 0.5)], sr), sr)
    assert one[0].t0 == pytest.approx(0.1, abs=0.02) and one[0].t1 == pytest.approx(0.6, abs=0.02)
