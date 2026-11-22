"""Kokoro-82M v1.0, local, onnxruntime with the *timestamped* ONNX export (05 § 7–8, 08 § Local models).

- Input: phoneme ids `[0, ...ids, 0]`, a 256-d voice style vector chosen by token count, speed 1.0 (speed
  is applied later in the transport, so it never enters the cache key).
- Output: waveform (24 kHz float32) + per-token durations. Each word's time span is the sum of its own
  phoneme tokens' durations, so word timings are exact (FR-READ-6), not estimated.
- G2P: `providers/g2p.LexiconPhonemizer` (misaki dictionaries + rules, no espeak). Injectable.
- Weights live in `paths.models/kokoro/<model>/` and are installed by `local/manager.py` (sha256-verified,
  resumable, with job progress). Without them the voices are listed with `available=False` and a reason.
- The ONNX session loads lazily on first use and unloads after 10 idle minutes (NFR-9).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import numpy as np

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.catalog import ModelCatalog, TtsModel
from open_speech.playback.timing import estimate_word_timings
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice, WordTiming
from open_speech.providers.g2p import LexiconPhonemizer, PhonemeToken, Phonemizer
from open_speech.providers.pcm import f32_to_bytes

SAMPLE_RATE = 24000
STYLE_DIM = 256
SessionFactory = Callable[[Path], Any]

_ACCENTS = {"a": ("American English", "en-US"), "b": ("British English", "en-GB")}


def _default_session_factory(path: Path) -> Any:
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def voice_display(voice: str) -> tuple[str, str]:
    """af_heart → ("Heart (Kokoro, American English)", "en-US")."""
    accent, lang = _ACCENTS.get(voice[:1], ("English", "en"))
    nice = voice.split("_", 1)[-1].replace("_", " ").title()
    return f"{nice} (Kokoro, {accent})", lang


class KokoroTts:
    id = "kokoro"
    timestamps = True

    def __init__(
        self,
        catalog: ModelCatalog,
        paths: Paths,
        *,
        phonemizer: Phonemizer | None = None,
        session_factory: SessionFactory | None = None,
        idle_unload_s: float = 600.0,
    ) -> None:
        self._models: dict[str, TtsModel] = {m.model: m for m in catalog.tts_models(self.id)}
        self._paths = paths
        self._phonemizer = phonemizer
        self._session_factory = session_factory
        self._idle_unload_s = idle_unload_s
        self._lock = threading.Lock()
        self._sessions: dict[str, Any] = {}
        self._vocab: dict[str, dict[str, int]] = {}
        self._styles: dict[tuple[str, str], np.ndarray] = {}
        self._last_used = 0.0
        self._unload_handle: asyncio.TimerHandle | None = None

    # ------------------------------------------------------------------ installation state

    def model_dir(self, model: str) -> Path:
        return self._paths.models / self.id / model

    def runtime_available(self) -> bool:
        return self._session_factory is not None or importlib.util.find_spec("onnxruntime") is not None

    def _weights(self, model: str) -> tuple[Path, Path | None, str | None]:
        m = self._models[model]
        w = m.weights
        d = self.model_dir(model)
        if w is None:
            return d / "model.onnx", d / "tokenizer.json", "voices/{voice}.bin"
        return d / w.model_file, (d / w.tokenizer_file) if w.tokenizer_file else None, w.voice_file

    def voice_path(self, model: str, voice: str) -> Path:
        _, _, pattern = self._weights(model)
        return self.model_dir(model) / (pattern or "voices/{voice}.bin").format(voice=voice)

    def installed(self, model: str | None = None) -> bool:
        model = model or self.default_model()
        if model not in self._models:
            return False
        mpath, tok, _ = self._weights(model)
        return mpath.exists() and (tok is None or tok.exists())

    def default_model(self) -> str:
        return next(iter(self._models), "kokoro-82m-v1.0")

    def unavailable_reason(self, model: str, voice: str | None = None) -> str | None:
        m = self._models.get(model)
        if m is None:
            return "This local model isn't in the catalog."
        if not self.runtime_available():
            return "The local voice needs onnxruntime. Install open-speech[local-kokoro]."
        if not self.installed(model):
            size = int(round(m.size_mb)) if m.size_mb else 92
            return f"Download the local Kokoro voice (about {size} MB) to use it."
        if voice is not None and not self.voice_path(model, voice).exists():
            return "This Kokoro voice file isn't downloaded yet. Reinstall the local voice."
        return None

    # ------------------------------------------------------------------ protocol

    def voices(self) -> list[Voice]:
        out: list[Voice] = []
        for m in self._models.values():
            for v in m.voices:
                name, lang = voice_display(v)
                reason = self.unavailable_reason(m.model, v)
                out.append(
                    Voice(
                        id=m.voice_id(v),
                        provider=self.id,
                        model=m.model,
                        name=name,
                        languages=[lang],
                        styles=False,
                        local=True,
                        available=reason is None,
                        unavailable_reason=reason,
                        timestamps=True,
                    )
                )
        return out

    def limits(self, model: str) -> TtsLimits:
        m = self._models.get(model)
        return TtsLimits(
            max_input_chars=m.max_input_chars if m else 4000,
            formats=["pcm"],
            style=False,
            languages=list(m.languages) if m else ["en-US", "en-GB"],
        )

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        reason = self.unavailable_reason(req.model, req.voice)
        if reason:
            raise EngineError("offline", reason, provider=self.id)
        pcm, words = await asyncio.to_thread(self.synthesize_sync, req.text, req.model, req.voice)
        self._schedule_unload()
        yield AudioChunk(pcm=f32_to_bytes(pcm), sample_rate=SAMPLE_RATE, words=words, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), audio_sec=round(audio_sec, 3))

    # ------------------------------------------------------------------ model state

    def _phon(self) -> Phonemizer:
        if self._phonemizer is None:
            self._phonemizer = LexiconPhonemizer("us")
        return self._phonemizer

    def _load(self, model: str, voice: str) -> tuple[Any, dict[str, int], np.ndarray]:
        with self._lock:
            sess = self._sessions.get(model)
            if sess is None:
                mpath, _, _ = self._weights(model)
                factory = self._session_factory or _default_session_factory
                sess = factory(mpath)
                self._sessions[model] = sess
            vocab = self._vocab.get(model)
            if vocab is None:
                vocab = self._read_vocab(model)
                self._vocab[model] = vocab
            style = self._styles.get((model, voice))
            if style is None:
                raw = np.fromfile(self.voice_path(model, voice), dtype="<f4")
                style = raw.reshape(-1, STYLE_DIM).astype(np.float32)
                self._styles[(model, voice)] = style
            self._last_used = time.monotonic()
            return sess, vocab, style

    def _read_vocab(self, model: str) -> dict[str, int]:
        _, tok, _ = self._weights(model)
        candidates = [p for p in (tok, self.model_dir(model) / "config.json") if p is not None and p.exists()]
        for p in candidates:
            data = json.loads(p.read_text("utf-8"))
            vocab = data.get("vocab") or (data.get("model") or {}).get("vocab")
            if isinstance(vocab, dict) and vocab:
                return {str(k): int(v) for k, v in vocab.items()}
        raise EngineError("offline", "The Kokoro tokenizer is missing. Reinstall the local voice.", provider=self.id)

    def unload(self) -> None:
        with self._lock:
            self._sessions.clear()
            self._styles.clear()

    def loaded(self) -> bool:
        return bool(self._sessions)

    def _schedule_unload(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._unload_handle is not None:
            self._unload_handle.cancel()
        self._unload_handle = loop.call_later(self._idle_unload_s, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        if time.monotonic() - self._last_used >= self._idle_unload_s * 0.99:
            self.unload()

    # ------------------------------------------------------------------ synthesis

    def synthesize_sync(self, text: str, model: str, voice: str) -> tuple[np.ndarray, list[WordTiming]]:
        sess, vocab, style = self._load(model, voice)
        max_tokens = self._models[model].max_tokens or 510
        tokens = self._phon().phonemize(text)
        seq, spans = self._encode(tokens, vocab)
        if not seq:
            return np.zeros(int(0.1 * SAMPLE_RATE), dtype=np.float32), []
        audio: list[np.ndarray] = []
        words: list[WordTiming] = []
        offset_s = 0.0
        for lo, hi in _chunks(seq, spans, max_tokens):
            ids = seq[lo:hi]
            wave, frame_ends = self._run(sess, ids, style)
            dur = wave.size / SAMPLE_RATE
            for tok, (a, b) in spans:
                if a < lo or b > hi or b <= a:
                    continue
                if frame_ends is not None:
                    # start = end of the previous token (index -1 → end of the leading pad)
                    t0 = float(frame_ends[a - lo - 1])
                    t1 = float(frame_ends[b - lo - 1])
                    words.append(WordTiming(tok.start, tok.end, round(offset_s + t0, 4), round(offset_s + t1, 4)))
            if frame_ends is None:
                chunk_text_words = [t for t, (a, b) in spans if a >= lo and b <= hi and b > a]
                if chunk_text_words:
                    first, last = chunk_text_words[0].start, chunk_text_words[-1].end
                    for wt in estimate_word_timings(text[first:last], dur):
                        t0, t1 = round(offset_s + wt.t0, 4), round(offset_s + wt.t1, 4)
                        words.append(WordTiming(wt.start + first, wt.end + first, t0, t1))
            audio.append(wave)
            offset_s += dur
        return np.concatenate(audio).astype(np.float32), words

    @staticmethod
    def _encode(
        tokens: list[PhonemeToken], vocab: dict[str, int]
    ) -> tuple[list[int], list[tuple[PhonemeToken, tuple[int, int]]]]:
        """Phoneme ids plus, for each *word*, its [start, end) range in the id sequence."""
        space = vocab.get(" ")
        seq: list[int] = []
        spans: list[tuple[PhonemeToken, tuple[int, int]]] = []
        for tok in tokens:
            a = len(seq)
            seq.extend(vocab[c] for c in tok.phonemes if c in vocab)
            if tok.is_word:
                spans.append((tok, (a, len(seq))))
            if tok.whitespace and space is not None and seq and seq[-1] != space:
                seq.append(space)
        while seq and space is not None and seq[-1] == space:
            seq.pop()
        return seq, spans

    def _run(self, sess: Any, ids: list[int], style: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        """Returns the waveform and, per token in `ids`, the time (s) at which that token ends; index -1
        maps to the end of the leading pad (use `frame_ends[i]` for the end of ids[i], `[i-1]` for its start)."""
        input_ids = np.array([[0, *ids, 0]], dtype=np.int64)
        ref = style[min(len(ids), style.shape[0] - 1)].reshape(1, STYLE_DIM)
        feeds: dict[str, Any] = {}
        for inp in sess.get_inputs():
            n = inp.name.lower()
            if "style" in n or "ref" in n:
                feeds[inp.name] = ref
            elif "speed" in n:
                feeds[inp.name] = np.array([1.0], dtype=np.float32)
            else:
                feeds[inp.name] = input_ids
        names = [o.name for o in sess.get_outputs()]
        results = sess.run(None, feeds)
        by_name = dict(zip(names, results, strict=False))
        wave = None
        durations = None
        for n, v in by_name.items():
            ln = n.lower()
            if "dur" in ln:
                durations = np.asarray(v, dtype=np.float64).reshape(-1)
            elif wave is None:
                wave = np.asarray(v, dtype=np.float32).reshape(-1)
        if wave is None:
            raise EngineError("provider_error", "The local voice returned no audio.", provider=self.id)
        if durations is None or durations.size != input_ids.shape[1] or durations.sum() <= 0:
            return wave, None
        seconds_per_unit = (wave.size / SAMPLE_RATE) / float(durations.sum())
        ends = np.cumsum(durations) * seconds_per_unit  # ends[k] = end time of input token k (k=0 is pad)
        # frame_ends[i] for ids[i] is ends[i+1]; frame_ends[-1] (start of ids[0]) is ends[0].
        frame_ends = np.empty(len(ids) + 1, dtype=np.float64)
        frame_ends[: len(ids)] = ends[1 : len(ids) + 1]
        frame_ends[-1] = ends[0]
        return wave, frame_ends


def _chunks(
    seq: list[int], spans: list[tuple[PhonemeToken, tuple[int, int]]], max_tokens: int
) -> list[tuple[int, int]]:
    """Split the id sequence at word boundaries so every chunk fits the model context."""
    if len(seq) <= max_tokens:
        return [(0, len(seq))]
    out: list[tuple[int, int]] = []
    lo = 0
    boundaries = [b for _, (_, b) in spans]
    while lo < len(seq):
        limit = lo + max_tokens
        if limit >= len(seq):
            out.append((lo, len(seq)))
            break
        cut = max((b for b in boundaries if lo < b <= limit), default=limit)
        out.append((lo, cut))
        lo = cut
    return out
