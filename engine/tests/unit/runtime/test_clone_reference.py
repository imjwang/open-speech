"""Clone references (voices/reference.py, voices/text.py): which window is chosen, how it is cleaned, and every reason
a recording is turned down. Synthetic "speech": harmonic syllables with pauses between phrases, no model needed."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from open_speech.voices import passages as P
from open_speech.voices import reference as R
from open_speech.voices.store import VoiceRecord, VoiceStore, ref_hash, slugify
from open_speech.voices.text import best_span, norm_words, wer

SR = 48000


def syllables(seconds: float, sr: int = SR, *, f0: float = 140.0, level: float = 0.25, seed: int = 0) -> np.ndarray:
    """Voiced "syllables" (~5 per second): a few harmonics under a raised-cosine envelope, short dips between."""
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    total = 0.0
    while total < seconds:
        d = float(rng.uniform(0.12, 0.22))
        n = int(d * sr)
        t = np.arange(n) / sr
        f = f0 * float(rng.uniform(0.9, 1.15))
        tone = sum((0.6 ** k) * np.sin(2 * np.pi * f * (k + 1) * t) for k in range(5))
        env = np.sin(np.pi * np.arange(n) / n) ** 0.6
        out.append((level * env * tone / 2.0).astype(np.float32))
        gap = int(float(rng.uniform(0.02, 0.05)) * sr)
        out.append(np.zeros(gap, dtype=np.float32))
        total += d + gap / sr
    return np.concatenate(out)


def recording(phrases: list[float], *, pause: float = 0.5, lead: float = 0.6, noise_db: float = -65.0,
              sr: int = SR, seed: int = 1, starts: list[float] | None = None) -> np.ndarray:
    """Phrases of speech (voiced level about -21 dBFS) with pauses, over a steady noise floor. `starts` receives
    where each phrase begins (s)."""
    parts = [np.zeros(int(lead * sr), dtype=np.float32)]
    at = lead
    for i, seconds in enumerate(phrases):
        if starts is not None:
            starts.append(at)
        phrase = syllables(seconds, sr, seed=seed + i)
        parts.append(phrase)
        parts.append(np.zeros(int(pause * sr), dtype=np.float32))
        at += phrase.size / sr + int(pause * sr) / sr
    x = np.concatenate(parts)
    rng = np.random.default_rng(seed)
    return (x + rng.normal(0, 10 ** (noise_db / 20), x.size)).astype(np.float32)


def test_the_window_is_whole_phrases_of_six_to_ten_seconds() -> None:
    # Five phrases (3 s each, 0.5 s pauses): no single phrase is long enough, the whole reading too long.
    starts: list[float] = []
    x = recording([3.0, 3.0, 3.0, 3.0, 3.0], starts=starts)
    p = R.prepare(x, SR)
    span = p.window.speech_end_s - p.window.speech_start_s
    assert 6.0 <= span <= 10.0
    # It starts at a phrase (after a pause): never inside one.
    assert any(abs(p.window.speech_start_s - s) < 0.1 for s in starts)
    assert p.quality.snr_db > 40 and p.warnings == []


def test_the_reference_has_silence_around_its_speech() -> None:
    # A file cut right at its first and last word: silence is added (0.3 s before, 0.6 s after).
    tight = recording([4.0, 4.0], lead=0.0, pause=0.0)
    p = R.prepare(tight, SR)
    speech = p.window.speech_end_s - p.window.speech_start_s
    assert p.duration_s == pytest.approx(speech + R.PAD_LEAD_S + R.PAD_TAIL_S, abs=0.05)
    head = p.audio[: int(0.25 * R.TARGET_SR)]
    tail = p.audio[-int(0.5 * R.TARGET_SR) :]
    assert np.max(np.abs(head)) < 1e-3 and np.max(np.abs(tail)) < 1e-3
    # A recording with pauses of its own keeps them and gets only what is missing.
    roomy = R.prepare(recording([4.0, 4.0], lead=1.0, pause=1.0), SR)
    assert roomy.duration_s == pytest.approx(
        roomy.window.end_s - roomy.window.start_s + (R.PAD_LEAD_S - R.LEAD_S) + (R.PAD_TAIL_S - R.TAIL_S), abs=0.05)


def test_a_short_reading_is_used_whole() -> None:
    x = recording([4.0, 4.0])  # a two-sentence passage: ~8.5 s of speech
    p = R.prepare(x, SR)
    assert p.window.speech_start_s == pytest.approx(0.6, abs=0.1)
    assert p.window.speech_end_s == pytest.approx(0.6 + 8.5, abs=0.2)


def test_one_long_phrase_is_cut_near_ten_seconds() -> None:
    x = recording([20.0])
    p = R.prepare(x, SR)
    assert 6.0 <= p.window.speech_end_s - p.window.speech_start_s <= 10.6


def test_the_reference_is_normalized_and_faded() -> None:
    x = recording([4.0, 4.0]) * 0.05  # quiet: about -40 dBFS voiced
    p = R.prepare(x, SR)
    y = p.audio
    assert p.audio.dtype == np.float32 and p.duration_s > 6
    # Voiced RMS at -20 dBFS (± 1 dB), peaks at or below -1 dBFS.
    frames = y[: y.size // 768 * 768].reshape(-1, 768)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    voiced = rms[rms > np.max(rms) * 0.1]
    level = 20 * np.log10(np.sqrt(np.mean(voiced**2)))
    assert -21.5 < level < -18.5
    assert np.max(np.abs(y)) <= 10 ** (-1 / 20) + 1e-4
    # 20 ms raised-cosine fades: the very first and last samples are silent.
    assert abs(y[0]) < 1e-6 and abs(y[-1]) < 1e-6
    # Rumble below 40 Hz is gone.
    hum = (0.3 * np.sin(2 * np.pi * 25 * np.arange(x.size) / SR)).astype(np.float32)
    q = R.prepare(x + hum * 0.05, SR)
    spec = np.abs(np.fft.rfft(q.audio))
    f = np.fft.rfftfreq(q.audio.size, 1 / R.TARGET_SR)
    assert spec[(f > 20) & (f < 30)].max() < spec[(f > 130) & (f < 170)].max() * 0.01


def test_the_limiter_only_touches_peaks() -> None:
    x = np.full(24000, 0.1, dtype=np.float32)
    x[12000:12010] = 1.5
    y = R.limit(x, 24000, 0.89)
    assert np.max(np.abs(y)) <= 0.89 + 1e-6
    assert abs(float(y[2000]) - 0.1) < 1e-6  # far from the peak: untouched


def test_rejections_say_why() -> None:
    with pytest.raises(R.ReferenceRejected) as low:
        R.prepare(recording([4.0, 4.0], sr=8000), 8000)
    assert low.value.code == "low_rate"
    with pytest.raises(R.ReferenceRejected) as silent:
        R.prepare(np.zeros(SR * 5, dtype=np.float32), SR)
    assert silent.value.code == "no_speech"
    with pytest.raises(R.ReferenceRejected) as short:
        R.prepare(recording([3.0]), SR)
    assert short.value.code == "too_short" and short.value.params["seconds"] < 5
    loud = np.clip(recording([4.0, 4.0]) * 12, -1.0, 1.0)  # digital clipping at full scale
    with pytest.raises(R.ReferenceRejected) as clipped:
        R.prepare(loud, SR)
    assert clipped.value.code == "clipped"
    with pytest.raises(R.ReferenceRejected) as noisy:
        R.prepare(recording([4.0, 4.0], noise_db=-33.0), SR)
    assert noisy.value.code == "noisy"


def test_warnings_for_a_usable_but_imperfect_recording() -> None:
    p = R.prepare(recording([4.0, 4.0], noise_db=-43.0), SR)
    assert [w[0] for w in p.warnings] == ["noisy"]
    lo = R.prepare(recording([4.0, 4.0], sr=16000), 16000)
    assert "low_rate" in [w[0] for w in lo.warnings]


def test_decoding_files(tmp_path: Path) -> None:
    x = recording([4.0, 4.0])
    stereo = np.stack([x, x * 0.5], axis=1)
    path = tmp_path / "take.flac"
    sf.write(str(path), stereo, SR)
    y, sr = R.decode(path)
    assert sr == SR and y.ndim == 1 and y.size == x.size
    # Channels that cancel out (one inverted): the louder one, not silence.
    inv = np.stack([x, -x], axis=1)
    assert np.max(np.abs(R.mono(inv))) > 0.1
    junk = tmp_path / "notes.wav"
    junk.write_text("not audio")
    with pytest.raises(R.ReferenceRejected) as bad:
        R.decode(junk)
    assert bad.value.code == "unreadable"


def test_passage_text_matching() -> None:
    passage = P.passage("en-1")
    assert passage is not None
    # The whole passage, as the recognizer writes it.
    heard = passage.text.lower().replace(",", "").replace(".", "")
    span = best_span(passage.text, heard)
    assert span.text == passage.text and span.wer == 0 and span.coverage == 1
    # Only the second sentence was in the window: its own words, with the passage's punctuation.
    second = "Then I go for a short walk even when the sky is gray and cloudy"
    span = best_span(passage.text, second)
    assert span.text == "Then I go for a short walk, even when the sky is gray and cloudy." and span.wer == 0
    # Something else entirely.
    assert best_span(passage.text, "the quick brown fox jumps over the lazy dog").wer > 0.5
    assert wer("It's GRAY, isn't it?", "its gray isnt it") == 0
    assert norm_words("Está nublado") == ["esta", "nublado"]


def test_every_language_has_passages_check_and_design_text() -> None:
    for lang in ("en", "es"):
        ps = P.passages(lang)
        assert len(ps) >= 3 and all(18 <= len(p.text.split()) <= 32 for p in ps)
        assert P.check_text(lang) and P.design_text(lang)
        # The check sentence shares no sentence with a passage (a voice that parrots its reference fails it).
        assert not any(P.check_text(lang).split(".")[0] in p.text for p in ps)
    assert P.next_passage("en", "en-3").id == "en-1"


def test_the_store(tmp_path: Path) -> None:
    store = VoiceStore(tmp_path / "voices")
    assert slugify("Jeff's voice") == "jeffs-voice" and slugify("Mi voz") == "mi-voz" and slugify("!!!") == "voice"
    a = store.new_slug("My voice")
    b = store.new_slug("My voice")
    assert (a, b) == ("my-voice", "my-voice-2")
    (store.folder(a) / "reference.wav").write_bytes(b"RIFF")
    digest = ref_hash(b"RIFF", "hello", "ref-v1")
    rec = VoiceRecord(slug=a, name="My voice", language="en", source="recording", ref_text="hello",
                      ref_sha256=digest, processing_version="ref-v1", duration_s=8.0,
                      created_at="2026-11-23T10:00:00+00:00")
    store.save(rec)
    assert store.clones() == []  # pending: synthesizable, not listed
    clone = store.clone(a)
    assert clone is not None and clone.ref_text == "hello" and clone.ref_sha256 == digest and clone.available
    store.update(a, kept=True)
    assert [c.slug for c in store.clones()] == [a]
    assert store.find("my voice") is not None and store.find("MY-VOICE") is not None
    assert ref_hash(b"RIFF", "hello", "ref-v1") != ref_hash(b"RIFF", "hello!", "ref-v1")
    assert ref_hash(b"RIFF", "café", "ref-v1") == ref_hash(b"RIFF", "café", "ref-v1")  # NFC
    # A pending voice nobody kept goes after a day; kept ones never do.
    store.save(VoiceRecord(slug=b, name="Old", language="en", source="file", ref_text="x", ref_sha256="y",
                           processing_version="ref-v1", duration_s=1, created_at="2020-01-01T00:00:00+00:00"))
    assert store.prune_pending() == 1 and store.get(b) is None and store.get(a) is not None
