from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.bundled import bundled_models_dir
from open_speech.local.catalog import ModelCatalog
from open_speech.playback.timing import WORD_RE
from open_speech.providers.base import TtsProvider, TtsRequest
from open_speech.providers.g2p import LexiconPhonemizer, PhonemeToken, letter_to_sound, misaki_available
from open_speech.providers.g2p_es import SpanishPhonemizer
from open_speech.providers.kokoro import KokoroTts, voice_display, voice_language
from open_speech.providers.pcm import bytes_to_f32

MODEL = "kokoro-82m-v1.0"
VOCAB_CHARS = " .,!?;:abcdefghijklmnopqrstuvwxyz"


class _IO:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeSession:
    """Stands in for the timestamped Kokoro ONNX export: 2 frames per token, 3 per pad, 600 samples/frame."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_inputs(self) -> list[_IO]:
        return [_IO("input_ids"), _IO("style"), _IO("speed")]

    def get_outputs(self) -> list[_IO]:
        return [_IO("waveform"), _IO("durations")]

    def run(self, _names: Any, feeds: dict[str, Any]) -> list[np.ndarray]:
        self.calls.append(feeds)
        ids = feeds["input_ids"][0]
        dur = np.array([3] + [2] * (len(ids) - 2) + [3], dtype=np.int64)
        wave = np.zeros(int(dur.sum()) * 600, dtype=np.float32)
        pos = 0
        for i, d in zip(ids, dur, strict=True):
            n = int(d) * 600
            if i not in (0, 1):  # pads and spaces are silent
                wave[pos : pos + n] = 0.2 * np.sin(np.arange(n) * 0.05)
            pos += n
        return [wave[None, :], dur]


class LettersPhonemizer:
    """Deterministic test phonemizer: phonemes are the word's lowercase letters."""

    def phonemize(self, text: str) -> list[PhonemeToken]:
        import re

        out = []
        for m in re.finditer(r"[A-Za-z]+|[.,!?;:]", text):
            tok = m.group(0)
            ws = m.end() < len(text) and text[m.end()].isspace()
            word = tok.isalpha()
            out.append(PhonemeToken(m.start(), m.end(), tok.lower() if word else tok, word, ws))
        return out


def install_fake_weights(paths: Paths, voices: tuple[str, ...] = ("af_heart",)) -> Path:
    d = paths.models / "kokoro" / MODEL
    (d / "onnx").mkdir(parents=True, exist_ok=True)
    (d / "onnx" / "model_quantized.onnx").write_bytes(b"fake")
    vocab = {"$": 0, " ": 1, **{c: i + 2 for i, c in enumerate(VOCAB_CHARS[1:])}}
    (d / "tokenizer.json").write_text(json.dumps({"model": {"type": "WordLevel", "vocab": vocab}}))
    (d / "voices").mkdir(exist_ok=True)
    for v in voices:
        style = np.arange(510 * 256, dtype="<f4").reshape(510, 1, 256) / 1e6
        style.tofile(d / "voices" / f"{v}.bin")
    return d


def make_kokoro(catalog: ModelCatalog, paths: Paths, session: FakeSession | None = None,
                **kw: Any) -> tuple[KokoroTts, FakeSession]:
    sess = session or FakeSession()
    tts = KokoroTts(catalog, paths, phonemizer=LettersPhonemizer(), session_factory=lambda p: sess, **kw)
    return tts, sess


def test_voices_unavailable_without_weights(catalog: ModelCatalog, paths: Paths) -> None:
    tts, _ = make_kokoro(catalog, paths)
    assert isinstance(tts, TtsProvider)
    vs = tts.voices()
    assert len(vs) == 30 and all(not v.available for v in vs)
    assert all(v.local and v.timestamps for v in vs)
    assert "Download the local Kokoro voice" in (vs[0].unavailable_reason or "")
    heart = next(v for v in vs if v.id == "kokoro:kokoro-82m-v1.0:af_heart")
    assert heart.name == "Heart (Kokoro, American English)" and heart.languages == ["en-US"]
    assert next(v for v in vs if v.id.endswith("bm_george")).languages == ["en-GB"]
    dora = next(v for v in vs if v.id.endswith("ef_dora"))
    assert dora.name == "Dora (Kokoro, Spanish)" and dora.languages == ["es"]


async def test_synthesize_without_weights_raises_clear_error(catalog: ModelCatalog, paths: Paths) -> None:
    tts, _ = make_kokoro(catalog, paths)
    with pytest.raises(EngineError) as ei:
        async for _ in tts.synthesize(TtsRequest(text="hi", model=MODEL, voice="af_heart")):
            pass
    assert "Download" in ei.value.detail


async def test_exact_word_timings_from_durations(catalog: ModelCatalog, paths: Paths) -> None:
    install_fake_weights(paths)
    tts, sess = make_kokoro(catalog, paths)
    voices = {v.id: v for v in tts.voices()}
    assert voices["kokoro:kokoro-82m-v1.0:af_heart"].available
    assert not voices["kokoro:kokoro-82m-v1.0:af_bella"].available  # voice file missing
    text = "Hello world."
    chunks = [c async for c in tts.synthesize(TtsRequest(text=text, model=MODEL, voice="af_heart"))]
    assert len(chunks) == 1 and chunks[0].sample_rate == 24000
    pcm = bytes_to_f32(chunks[0].pcm)
    words = chunks[0].words
    assert [(text[w.start : w.end]) for w in words] == ["Hello", "world"]
    # pad 3 frames, "hello" 5×2 frames, space 2, "world" 5×2, "." 2, pad 3 → 1 frame = 1/40 s
    assert words[0].t0 == pytest.approx(3 / 40) and words[0].t1 == pytest.approx(13 / 40)
    assert words[1].t0 == pytest.approx(15 / 40) and words[1].t1 == pytest.approx(25 / 40)
    assert pcm.size == 30 * 600
    feeds = sess.calls[0]
    ids = feeds["input_ids"][0].tolist()
    assert ids[0] == 0 and ids[-1] == 0 and len(ids) == 2 + 5 + 1 + 5 + 1
    assert feeds["style"].shape == (1, 256)
    assert feeds["speed"].tolist() == [1.0]  # speed is applied later, in the transport
    # style vector chosen by token count (12 phoneme tokens)
    assert feeds["style"][0, 0] == pytest.approx(12 * 256 / 1e6)


async def test_long_text_is_chunked_at_word_boundaries(paths: Paths) -> None:
    install_fake_weights(paths)
    cat = ModelCatalog.load(paths)
    cat.tts[f"kokoro:{MODEL}"].max_tokens = 12
    tts, sess = make_kokoro(cat, paths)
    text = "alpha beta gamma delta epsilon"
    chunks = [c async for c in tts.synthesize(TtsRequest(text=text, model=MODEL, voice="af_heart"))]
    words = chunks[0].words
    assert len(sess.calls) >= 3
    assert [text[w.start : w.end] for w in words] == text.split()
    for a, b in zip(words, words[1:], strict=False):
        assert a.t1 <= b.t0  # monotonic across chunk boundaries
    assert words[-1].t1 <= bytes_to_f32(chunks[0].pcm).size / 24000 + 1e-6


async def test_idle_unload(catalog: ModelCatalog, paths: Paths) -> None:
    install_fake_weights(paths)
    tts, _ = make_kokoro(catalog, paths, idle_unload_s=0.05)
    async for _ in tts.synthesize(TtsRequest(text="hi", model=MODEL, voice="af_heart")):
        pass
    assert tts.loaded()
    await asyncio.sleep(0.12)
    assert not tts.loaded()


# ------------------------------------------------------------------ language of a voice

SPANISH_VOCAB = " .,!?;:—…\"()“”aeiouɛjwIWAʊɪptkbdɡβðɣfθsxʧʤʃʦmnɲŋlʎɾrʝːˈˌ"


def install_spanish_weights(paths: Paths, voices: tuple[str, ...] = ("ef_dora", "af_heart")) -> dict[str, int]:
    d = install_fake_weights(paths, voices)
    vocab = {"$": 0, **{c: i + 1 for i, c in enumerate(SPANISH_VOCAB)}}
    (d / "tokenizer.json").write_text(json.dumps({"model": {"type": "WordLevel", "vocab": vocab}}))
    return vocab


def test_spanish_voices_are_listed_by_language(catalog: ModelCatalog, paths: Paths) -> None:
    assert voice_display("ef_dora") == ("Dora (Kokoro, Spanish)", "es")
    assert voice_display("em_santa") == ("Santa (Kokoro, Spanish)", "es")
    assert voice_language("em_alex") == "es" and voice_language("kokoro:kokoro-82m-v1.0:ef_dora") == "es"
    assert voice_language("af_heart") == "en-US" and voice_language("bm_george") == "en-GB"
    install_spanish_weights(paths)
    tts = KokoroTts(catalog, paths, session_factory=lambda p: FakeSession())
    spanish = tts.voices_for_language("es")
    assert [v.id.rsplit(":", 1)[-1] for v in spanish] == ["ef_dora", "em_alex", "em_santa"]
    assert all(v.languages == ["es"] for v in spanish)
    assert tts.voices_for_language("es-MX") == spanish  # matched on the base language
    assert [v.id.rsplit(":", 1)[-1] for v in tts.voices_for_language("es", available_only=True)] == ["ef_dora"]
    english = tts.voices_for_language("en")
    assert len(english) == 27 and english[0].id.endswith(":af_heart")
    assert tts.voices_for_language(None) == english
    assert "es" in tts.limits(MODEL).languages


async def test_spanish_voice_uses_the_spanish_phonemizer(catalog: ModelCatalog, paths: Paths) -> None:
    vocab = install_spanish_weights(paths)
    sess = FakeSession()
    tts = KokoroTts(catalog, paths, session_factory=lambda p: sess)  # no phonemizer injected
    assert isinstance(tts._phon("ef_dora"), SpanishPhonemizer)
    assert isinstance(tts._phon("af_heart"), LexiconPhonemizer)
    assert isinstance(tts._phon("bm_george"), LexiconPhonemizer)

    text = "Hola, ¿qué tal? Un beso."
    chunks = [c async for c in tts.synthesize(TtsRequest(text=text, model=MODEL, voice="ef_dora"))]
    by_id = {i: c for c, i in vocab.items()}
    sent = "".join(by_id[i] for i in sess.calls[0]["input_ids"][0].tolist()[1:-1])
    assert sent == "ˈola, kˈe tˈal? ˈum bˈeso."  # ¿ is not in the vocabulary; "un beso" → "um beso"
    words = chunks[0].words
    assert [text[w.start : w.end] for w in words] == ["Hola", "qué", "tal", "Un", "beso"]
    assert all(a.t0 < a.t1 <= b.t0 for a, b in zip(words, words[1:], strict=False))
    assert words[-1].t1 <= bytes_to_f32(chunks[0].pcm).size / 24000

    # an injected phonemizer still wins, whatever the voice
    injected, sess2 = make_kokoro(catalog, paths)
    assert isinstance(injected._phon("ef_dora"), LettersPhonemizer)


def test_voice_added_after_install_comes_from_bundled_copy(catalog: ModelCatalog, paths: Paths, tmp_path: Path) -> None:
    install_fake_weights(paths, ("af_heart",))  # an app-data copy from before the Spanish voices existed
    runtime = tmp_path / "runtime"
    install_fake_weights(Paths(runtime), ("af_heart", "ef_dora"))
    tts, _ = make_kokoro(catalog, paths, bundled_models=runtime / "models")
    assert tts.model_dir(MODEL) == paths.models / "kokoro" / MODEL
    assert tts.voice_path(MODEL, "ef_dora") == runtime / "models" / "kokoro" / MODEL / "voices" / "ef_dora.bin"
    assert tts.voice_path(MODEL, "af_heart") == paths.models / "kokoro" / MODEL / "voices" / "af_heart.bin"
    voices = {v.id.rsplit(":", 1)[-1]: v for v in tts.voices()}
    assert voices["ef_dora"].available and not voices["em_alex"].available


def _real_weights() -> Path | None:
    """A complete Kokoro model directory with the Spanish voices: $OPEN_SPEECH_KOKORO_DIR, or the bundle."""
    candidates = [os.environ.get("OPEN_SPEECH_KOKORO_DIR")]
    root = bundled_models_dir()
    if root is not None:
        candidates.append(str(root / "kokoro" / MODEL))
    for c in candidates:
        d = Path(c) if c else None
        if d and (d / "onnx" / "model_quantized.onnx").exists() and (d / "voices" / "ef_dora.bin").exists():
            return d
    return None


@pytest.mark.skipif(
    _real_weights() is None or importlib.util.find_spec("onnxruntime") is None,
    reason="real Kokoro weights with the Spanish voices not present (set OPEN_SPEECH_KOKORO_DIR)",
)
async def test_real_weights_speak_spanish_with_word_timings(catalog: ModelCatalog, tmp_path: Path) -> None:
    weights = _real_weights()
    assert weights is not None
    home = Paths(tmp_path / "home").ensure()
    (home.models / "kokoro").mkdir(parents=True)
    (home.models / "kokoro" / MODEL).symlink_to(weights, target_is_directory=True)
    tts = KokoroTts(catalog, home, bundled_models=tmp_path / "none")
    text = "Hola, ¿cómo estás? Hoy cumplo 21 años y vivo en la ciudad de México."
    chunks = [c async for c in tts.synthesize(TtsRequest(text=text, model=MODEL, voice="ef_dora"))]
    pcm = np.concatenate([bytes_to_f32(c.pcm) for c in chunks])
    words = [w for c in chunks for w in c.words]
    duration = pcm.size / 24000
    assert 2.5 < duration < 12 and float(np.abs(pcm).max()) > 0.05
    assert [text[w.start : w.end] for w in words] == [m.group(0) for m in WORD_RE.finditer(text)]
    assert all(a.t0 < a.t1 and a.t1 <= b.t0 + 1e-6 for a, b in zip(words, words[1:], strict=False))
    assert words[0].t0 >= 0 and words[-1].t1 <= duration + 1e-6
    spoken = sum(w.t1 - w.t0 for w in words)
    assert 0.6 * duration < spoken <= duration  # the rest is the pauses at the punctuation


# ------------------------------------------------------------------ G2P


def test_lexicon_phonemizer_rules_and_numbers() -> None:
    lex = {"hello": "həlˈO", "twenty": "twˈɛnti", "six": "sˈɪks", "read": {"DEFAULT": "ɹˈid", "VBD": "ɹˈɛd"}}
    g = LexiconPhonemizer(lexicon=lex, silver={})
    toks = g.phonemize("Hello, read 26 NASA blorft.")
    assert [(t.phonemes, t.is_word) for t in toks[:3]] == [("həlˈO", True), (",", False), ("ɹˈid", True)]
    assert toks[3].phonemes == "twˈɛnti sˈɪks"
    assert toks[4].phonemes.startswith("ˈɛn")  # acronym spelled out: N-A-S-A
    assert toks[5].phonemes and "ˈ" in toks[5].phonemes  # OOV via letter-to-sound, stressed
    assert toks[-1].phonemes == "." and not toks[-1].is_word
    assert [t.whitespace for t in toks[:2]] == [False, True]


def test_letter_to_sound_basics() -> None:
    assert letter_to_sound("cake") == "kˈAk"
    assert letter_to_sound("ship").startswith("ʃ")
    assert letter_to_sound("") == ""


@pytest.mark.skipif(not misaki_available(), reason="misaki dictionaries not installed")
def test_misaki_dictionaries_are_used_without_espeak() -> None:
    g = LexiconPhonemizer()
    words = [t.phonemes for t in g.phonemize("Open speech reads aloud.") if t.is_word]
    assert words[0] == "ˈOpᵊn" and words[1] == "spˈiʧ"
