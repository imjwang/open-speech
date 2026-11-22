"""The real on-device recognizer, end to end. Opt-in by circumstance: runs only where sherpa-onnx is
installed and the default model's weights are on disk; skipped everywhere else (CI has no weights).

Weights are looked for in `$OPEN_SPEECH_TEST_STT_HOME/models/stt/<model>/`, else in the app-data home.
Speech is synthesized to a file with macOS `say -o` (nothing is played, no microphone is opened).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from open_speech.config import Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.base import SttEvent
from open_speech.providers.local_stt import LocalStt

from .helpers import make_env

RATE = 16000


def _home() -> Path:
    override = os.environ.get("OPEN_SPEECH_TEST_STT_HOME")
    return Path(override).expanduser() if override else make_env().home


@pytest.fixture(scope="module")
def stt() -> LocalStt:
    if importlib.util.find_spec("sherpa_onnx") is None:
        pytest.skip("sherpa-onnx is not installed (uv sync --extra local-stt)")
    paths = Paths(_home())
    adapter = LocalStt(ModelCatalog.load(), paths, partial_interval_s=0.3)
    if not adapter.installed():
        pytest.skip(f"speech-to-text weights are not in {paths.models / 'stt' / adapter.model}")
    return adapter


def _say(text: str, voice: str, out: Path) -> np.ndarray:
    if sys.platform != "darwin" or shutil.which("say") is None or shutil.which("afconvert") is None:
        pytest.skip("needs macOS `say` to synthesize test speech")
    aiff = out.with_suffix(".aiff")
    if subprocess.run(["say", "-v", voice, "-o", str(aiff), text], capture_output=True).returncode != 0:
        pytest.skip(f"the {voice} voice is not installed")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", str(aiff), str(out)], check=True)
    samples, rate = sf.read(str(out), dtype="float32")
    assert rate == RATE
    # as a mic session: a moment before the first word, the end-of-speech pause after the last
    return np.concatenate([np.zeros(RATE // 2, np.float32), samples, np.zeros(RATE * 4 // 5, np.float32)])


async def _mic(samples: np.ndarray) -> AsyncIterator[bytes]:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    for i in range(0, len(pcm), 3200):  # 100 ms frames
        yield pcm[i : i + 3200]


async def _listen(stt: LocalStt, samples: np.ndarray) -> list[SttEvent]:
    return [e async for e in stt.stream(_mic(samples))]


def _plain(text: str) -> str:
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


async def test_english_command(stt: LocalStt, tmp_path: Path) -> None:
    events = await _listen(stt, _say("read my clipboard", "Samantha", tmp_path / "en.wav"))
    assert [e.kind for e in events].count("final") == 1 and events[-1].kind == "final"
    assert _plain(events[-1].text) == "read my clipboard"
    assert stt.loaded()


async def test_spanish_command_needs_no_language_hint(stt: LocalStt, tmp_path: Path) -> None:
    events = await _listen(stt, _say("lee mi portapapeles", "Mónica", tmp_path / "es.wav"))
    assert _plain(events[-1].text) == "lee mi portapapeles"


@pytest.mark.parametrize(
    ("word", "voice"), [("yes", "Samantha"), ("one", "Samantha"), ("sí", "Mónica"), ("no", "Mónica")]
)
async def test_a_one_word_answer_after_a_long_silence_is_still_heard(
    stt: LocalStt, tmp_path: Path, word: str, voice: str
) -> None:
    """The bar's microphone is often open for a while before the user answers. Without cutting the audio to
    the speech, a short word after ten seconds of silence came back empty."""
    spoken = _say(word, voice, tmp_path / "word.wav")
    rng = np.random.default_rng(11)
    quiet = (rng.standard_normal(RATE * 10) * 0.0002).astype(np.float32)  # ten seconds of mic floor
    events = await _listen(stt, np.concatenate([quiet, spoken]))
    assert _plain(events[-1].text) == word


async def test_non_speech_comes_back_empty(stt: LocalStt) -> None:
    rng = np.random.default_rng(3)
    hiss = (rng.standard_normal(RATE * 3) * 0.0003).astype(np.float32)  # mic floor: once heard as "Yes."
    t = np.arange(RATE // 4) / RATE
    thump = np.zeros(RATE * 2, np.float32)
    thump[8000 : 8000 + t.size] = np.sin(2 * np.pi * 70 * t) * np.exp(-t * 18) * 0.2  # once heard as "Yeah."
    clicks = np.zeros(RATE * 2, np.float32)
    for at in (4000, 9000, 15000, 22000, 27000):
        clicks[at : at + 120] = rng.standard_normal(120) * 0.3
    for sound in (np.zeros(RATE * 2, np.float32), hiss, thump, clicks):
        events = await _listen(stt, sound)
        assert [(e.kind, e.text) for e in events] == [("final", "")]


async def test_file_and_onnxruntime_in_the_same_process(stt: LocalStt, tmp_path: Path) -> None:
    """Kokoro runs on the onnxruntime wheel; sherpa-onnx ships its own copy. Both must work side by side."""
    samples = _say("go to page twelve", "Samantha", tmp_path / "page.wav")
    path = tmp_path / "padded.wav"
    sf.write(str(path), samples, RATE)
    if importlib.util.find_spec("onnxruntime") is not None:
        import onnxruntime

        vad = stt.model_dir() / "silero_vad.onnx"  # any ONNX file will do to prove the wheel's runtime loads
        session = onnxruntime.InferenceSession(str(vad), providers=["CPUExecutionProvider"])
        assert session.get_inputs()
    t = await stt.transcribe(path)
    assert _plain(t.text) in ("go to page twelve", "go to page 12")
    assert t.audio_sec == pytest.approx(len(samples) / RATE, abs=0.01)
