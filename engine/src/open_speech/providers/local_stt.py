"""On-device speech-to-text with sherpa-onnx (07 § STT routing, order 1; 08 § Local models).

- Models are the catalog's `[stt.local."<model>"]` entries: offline (whole-utterance) transducers. The
  default, Parakeet TDT 0.6B v3, detects the language itself (English, Spanish and 23 more) and decodes a
  3 s command in ~0.14 s on an Apple M4, so "streaming" works like the cloud adapters: buffer the mic PCM,
  re-decode the growing buffer for interim partials, decode once more when the mic stops for the final.
- Weights live in `paths.models/stt/<model>/`, installed by `local/manager.py` (sha256-verified, resumable).
  Packaged builds may ship a read-only copy (`local/bundled.py`), used when app data has no complete one.
- Inference never runs on the event loop: every decode happens in a worker thread, one at a time (a lock
  serializes loads and decodes). A new utterance may start while the previous one is still decoding; its
  first decode waits its turn, and interim decodes nobody wants any more are skipped, not computed.
- The model loads when the mic opens (ready before the first word ends) and unloads after 10 idle minutes
  (NFR-9).
- Non-speech must come back empty, never as words. Measured on the default model: mic-floor hiss came back
  as "Yes.", a thump as "Yeah.". Two gates stop that before the recognizer runs: `has_sound` (numpy: silence
  and steady hiss/hum) and Silero VAD (clicks, coughs, noise bursts), which ships with the model's weights.
- What does reach the recognizer is brought to a full level first (`leveled`): the mic has no gain control.
- No word timings: the recognizer's token times run about 0.3 s ahead of the audio (measured), so
  `transcribe(path, words=True)` returns the text with an empty word list rather than wrong times.

Mic frames are 16-bit LE PCM, 16 kHz mono (04 § WebSocket protocol).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.bundled import bundled_models_dir
from open_speech.local.catalog import LOCAL_STT, LOCAL_STT_DIR, LocalSttModel, ModelCatalog
from open_speech.providers.base import SttEvent, Transcript, Units
from open_speech.providers.pcm import int16_to_f32, read_audio_file, resample
from open_speech.providers.stt_common import BYTES_PER_SEC, MIC_RATE

log = logging.getLogger(__name__)

MIN_AUDIO_S = 0.15  # shorter than this is a tap on the key, not speech
FILE_CHUNK_S = 30.0  # `transcribe(path)` decodes long files in pieces of at most this length
SPEECH_LEAD_S = 0.3  # audio kept before the first speech the VAD finds, and
SPEECH_TAIL_S = 0.5  # after the last (the VAD reports a word slightly late and lets go slightly early)
_FRAME = MIC_RATE // 50  # 20 ms


class Recognizer(Protocol):
    """One loaded model. `samples` are mono float32 in [-1, 1] at 16 kHz; `lang` is an ISO 639-1 hint
    (models that detect the language themselves ignore it). Returns the text, "" for non-speech."""

    def transcribe(self, samples: np.ndarray, lang: str | None) -> str: ...


RecognizerFactory = Callable[[LocalSttModel, Path], Recognizer]


def has_sound(samples: np.ndarray, *, rise_db: float = 6.0, floor_db: float = -60.0, min_frames: int = 3) -> bool:
    """True when some stretch of at least 60 ms stands out of the background: louder than -60 dBFS and 6 dB
    above the quiet part of the buffer. False for silence, mic-floor hiss, and steady noise or hum at any
    level. Deliberately permissive (it never stopped one of 415 test utterances, down to -45 dBFS speech
    and 10 dB SNR); telling speech from other sounds is the VAD's job."""
    n = len(samples) // _FRAME
    if n < min_frames:
        return False
    frames = samples[: n * _FRAME].reshape(n, _FRAME).astype(np.float64)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    threshold = max(float(np.percentile(rms, 15)) * 10 ** (rise_db / 20), 10 ** (floor_db / 20))
    run = 0
    for loud in rms > threshold:
        run = run + 1 if loud else 0
        if run >= min_frames:
            return True
    return False


def leveled(samples: np.ndarray, *, peak: float = 0.9, max_gain: float = 100.0) -> np.ndarray:
    """The audio scaled so its loudest sample sits at `peak` (at most 40 dB up). The mic runs without gain
    control, so speech can arrive quiet, and the recognizer loses accuracy when it does. Measured with
    speech at -50 dBFS: Spanish exact match 80 % as is, 88 % levelled (one-word commands 60 % → 77 %); at
    ordinary levels levelling changes little either way."""
    top = float(np.max(np.abs(samples))) if samples.size else 0.0
    if top <= 0.0:
        return samples
    return (samples * min(peak / top, max_gain)).astype(np.float32)


# ---------------------------------------------------------------------------------- sherpa-onnx


class SherpaRecognizer:
    """A sherpa-onnx offline transducer plus its Silero VAD (the only code that imports sherpa_onnx)."""

    def __init__(self, entry: LocalSttModel, model_dir: Path) -> None:
        import sherpa_onnx

        # Greedy search, the sherpa-onnx default. Beam search with hotword biasing was measured and left out:
        # it recovered a third of the misheard short commands and made noise come back as "Yeah." more often.
        self._rec = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_dir / entry.encoder),
            decoder=str(model_dir / entry.decoder),
            joiner=str(model_dir / entry.joiner),
            tokens=str(model_dir / entry.tokens),
            num_threads=max(1, entry.num_threads),
            model_type=entry.family,
        )
        self._vad: Any | None = None
        if entry.vad:
            cfg = sherpa_onnx.VadModelConfig()
            cfg.silero_vad.model = str(model_dir / entry.vad)
            cfg.silero_vad.threshold = 0.5
            cfg.silero_vad.min_speech_duration = 0.1  # "no" and "sí" last about 0.2 s
            cfg.silero_vad.min_silence_duration = 0.25
            cfg.silero_vad.window_size = 512
            cfg.sample_rate = MIC_RATE
            cfg.num_threads = 1
            self._vad = sherpa_onnx.VadModel.create(cfg)

    def speech_span(self, samples: np.ndarray) -> tuple[int, int] | None:
        """Where the speech is: from the first window Silero VAD calls speech to the last, with a little air
        either side (sample offsets), or None when there is none. About 4 ms per second of audio."""
        vad = self._vad
        if vad is None:
            return (0, len(samples))
        vad.reset()
        size = vad.window_size()
        first: int | None = None
        last = 0
        for i in range(0, len(samples) - size + 1, size):
            if vad.is_speech(samples[i : i + size]):
                if first is None:
                    first = i
                last = i + size
        if first is None:
            return None
        return max(0, first - int(SPEECH_LEAD_S * MIC_RATE)), min(len(samples), last + int(SPEECH_TAIL_S * MIC_RATE))

    def has_speech(self, samples: np.ndarray) -> bool:
        return self.speech_span(samples) is not None

    def transcribe(self, samples: np.ndarray, lang: str | None) -> str:
        # Only the stretch with speech in it goes to the recognizer. The bar's microphone is often open for
        # seconds before anyone speaks, and a short word after a long silence is otherwise heard as nothing:
        # with ten seconds of silence first, one-word commands were right 14 of 52 times in English and 1 of
        # 48 in Spanish; cut to the speech, 34 and 25 (and better than uncut even after 0.4 s).
        span = self.speech_span(samples)
        if span is None:
            return ""
        stream = self._rec.create_stream()
        stream.accept_waveform(MIC_RATE, leveled(samples[span[0] : span[1]]))
        self._rec.decode_stream(stream)
        return str(stream.result.text).strip()


def _default_recognizer_factory(entry: LocalSttModel, model_dir: Path) -> Recognizer:
    return SherpaRecognizer(entry, model_dir)


# ---------------------------------------------------------------------------------- provider


class LocalStt:
    id = LOCAL_STT

    def __init__(
        self,
        catalog: ModelCatalog,
        paths: Paths,
        *,
        model: str | None = None,
        recognizer_factory: RecognizerFactory | None = None,
        partial_interval_s: float | None = 0.6,
        idle_unload_s: float = 600.0,
        bundled_models: Path | None = None,
    ) -> None:
        self._models: dict[str, LocalSttModel] = {m.model: m for m in catalog.local_stt_models()}
        default = catalog.default_local_stt_model()
        self._default = default.model if default is not None else ""
        self._pinned = model
        self._paths = paths
        self._bundled = bundled_models if bundled_models is not None else bundled_models_dir()
        self._factory = recognizer_factory
        self.partial_interval_s = partial_interval_s
        self._idle_unload_s = idle_unload_s
        self._lock = threading.Lock()  # one load or decode at a time
        self._recognizers: dict[str, Recognizer] = {}
        self._drop = False  # unload asked for while a decode was running
        self._streams = 0
        self._last_used = 0.0
        self._unload_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Future[Any]] = set()  # background work; the loop only holds weak references

    # ------------------------------------------------------------------ installation state

    @property
    def model(self) -> str:
        """Catalog id of the model in use: the one asked for, else the catalog default; when the default
        isn't on disk but another catalog model is, that one."""
        if self._pinned:
            return self._pinned
        if not self._complete(self._default):
            return next((m for m in self._models if self._complete(m)), self._default)
        return self._default

    def _complete_dir(self, model: str) -> Path | None:
        entry = self._models.get(model)
        if entry is None:
            return None
        dirs = [self._paths.models / LOCAL_STT_DIR / model]
        if self._bundled is not None:
            dirs.append(self._bundled / LOCAL_STT_DIR / model)
        files = entry.required_files()
        return next((d for d in dirs if all((d / f).exists() for f in files)), None)

    def _complete(self, model: str) -> bool:
        return self._complete_dir(model) is not None

    def model_dir(self, model: str | None = None) -> Path:
        """The app-data copy when it is complete, else the read-only bundled copy when that one is."""
        model = model or self.model
        return self._complete_dir(model) or self._paths.models / LOCAL_STT_DIR / model

    def installed(self, model: str | None = None) -> bool:
        return self._complete(model or self.model)

    def runtime_available(self) -> bool:
        return self._factory is not None or importlib.util.find_spec("sherpa_onnx") is not None

    def unavailable_reason(self, model: str | None = None) -> str | None:
        model = model or self.model
        entry = self._models.get(model)
        if entry is None:
            return "This speech recognition model isn't in the catalog."
        if not self.runtime_available():
            return "On-device speech recognition needs sherpa-onnx. Install open-speech[local-stt]."
        if not self.installed(model):
            size = int(round(entry.size_mb))
            about = f" (about {size} MB)" if size else ""
            return f"Download {entry.name or 'the speech recognition model'}{about} to use speech input on this Mac."
        return None

    def price_units(self, audio_sec: float) -> Units:
        return Units(audio_sec=round(audio_sec, 3))

    # ------------------------------------------------------------------ model state

    def _recognizer(self, model: str) -> Recognizer:
        """Call with `self._lock` held."""
        rec = self._recognizers.get(model)
        if rec is None:
            factory = self._factory or _default_recognizer_factory
            t0 = time.monotonic()
            try:
                rec = factory(self._models[model], self.model_dir(model))
            except EngineError:
                raise
            except Exception as exc:
                raise EngineError(
                    "provider_error",
                    "The speech recognition model couldn't be loaded. Remove it and download it again.",
                    provider=self.id,
                ) from exc
            log.info("loaded speech recognition model %s in %.2f s", model, time.monotonic() - t0)
            self._recognizers[model] = rec
        return rec

    def _load(self, model: str) -> None:
        with self._lock:
            try:
                self._recognizer(model)
                self._last_used = time.monotonic()
            finally:
                self._after_use()

    def _after_use(self) -> None:
        """Call with `self._lock` held, at the end of a load or decode."""
        if self._drop:
            self._recognizers.clear()
            self._drop = False

    def loaded(self) -> bool:
        return bool(self._recognizers)

    def unload(self) -> None:
        """Drop the loaded model. Never blocks: when a decode is running, the model goes when it finishes."""
        if self._lock.acquire(blocking=False):
            try:
                self._recognizers.clear()
                self._drop = False
            finally:
                self._lock.release()
        else:
            self._drop = True

    def _schedule_unload(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._unload_handle is not None:
            self._unload_handle.cancel()
        self._unload_handle = loop.call_later(self._idle_unload_s, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        self._unload_handle = None
        idle = time.monotonic() - self._last_used >= self._idle_unload_s * 0.99
        if idle and self._streams == 0 and not self._lock.locked():
            self.unload()
        elif self.loaded():
            self._schedule_unload()

    # ------------------------------------------------------------------ decoding (worker threads)

    def _decode(
        self, model: str, samples: np.ndarray, lang: str | None, skip: threading.Event | None = None
    ) -> tuple[str, float]:
        """Transcribe in the calling (worker) thread. Returns the text and the seconds the model took.
        `skip`: set by the stream once this decode is no longer wanted; checked when the lock is ours."""
        if not has_sound(samples):
            return "", 0.0
        with self._lock:
            if skip is not None and skip.is_set():
                return "", 0.0
            try:
                rec = self._recognizer(model)
                t0 = time.monotonic()
                text = rec.transcribe(samples, lang).strip()
                self._last_used = time.monotonic()
                return text, self._last_used - t0
            except EngineError:
                raise
            except Exception as exc:
                raise EngineError(
                    "provider_error", "Speech recognition failed. Try again, or type your command.", provider=self.id
                ) from exc
            finally:
                self._after_use()

    def _spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Future[T]:
        """A task this adapter keeps alive, whose failure (if nobody collects it) isn't logged as 'never
        retrieved'."""
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return task

    # ------------------------------------------------------------------ protocol

    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None = None) -> AsyncIterator[SttEvent]:
        """Partials while the user speaks (at most one decode in flight), then exactly one final."""
        model = self.model
        reason = self.unavailable_reason(model)
        if reason:
            raise EngineError("offline", reason, provider=self.id)
        step = int((self.partial_interval_s or 0) * BYTES_PER_SEC)
        buf = bytearray()
        stale = threading.Event()  # set once interim decodes of this utterance are no longer wanted
        pending: asyncio.Future[tuple[str, float]] | None = None
        decoded_at = 0  # buffer length when the last interim decode started
        wait = step  # bytes of new audio before the next interim decode
        last_text = ""
        self._streams += 1
        # Load while the user is still drawing breath: a cold model is ready before the first word ends.
        self._spawn(asyncio.to_thread(self._load, model))
        try:
            async for frame in pcm:
                buf.extend(frame)
                if pending is not None and pending.done():
                    text, took = _interim(pending)
                    pending = None
                    # Longer utterances take longer to re-decode: keep interim work under half the time.
                    wait = max(step, int(took * 2 * BYTES_PER_SEC))
                    if text and text != last_text:
                        last_text = text
                        yield SttEvent(kind="partial", text=text)
                if step and pending is None and len(buf) - decoded_at >= wait:
                    decoded_at = len(buf)
                    samples = int16_to_f32(bytes(buf))
                    pending = self._spawn(asyncio.to_thread(self._decode, model, samples, lang, stale))
            stale.set()
            if len(buf) < int(MIN_AUDIO_S * BYTES_PER_SEC):
                yield SttEvent(kind="final", text="")
                return
            text, _ = await asyncio.to_thread(self._decode, model, int16_to_f32(bytes(buf)), lang)
            yield SttEvent(kind="final", text=text)
        finally:
            stale.set()
            self._streams -= 1
            self._last_used = time.monotonic()
            self._schedule_unload()

    async def transcribe(self, audio: Path, words: bool = False) -> Transcript:
        """The file's text. `words` is accepted for the protocol; this recognizer gives no word timings."""
        model = self.model
        reason = self.unavailable_reason(model)
        if reason:
            raise EngineError("offline", reason, provider=self.id)
        self._streams += 1
        try:
            samples, sr = await asyncio.to_thread(read_audio_file, audio)
            if sr != MIC_RATE:
                samples = await asyncio.to_thread(resample, samples, sr, MIC_RATE)
            chunk = int(FILE_CHUNK_S * MIC_RATE)
            texts: list[str] = []
            for start in range(0, len(samples), chunk):
                text, _ = await asyncio.to_thread(self._decode, model, samples[start : start + chunk], None)
                if text:
                    texts.append(text)
            return Transcript(text=" ".join(texts), audio_sec=round(len(samples) / MIC_RATE, 3))
        finally:
            self._streams -= 1
            self._last_used = time.monotonic()
            self._schedule_unload()


def _interim(task: asyncio.Future[tuple[str, float]]) -> tuple[str, float]:
    """An interim decode that failed doesn't end the utterance; the final decode reports the problem."""
    if task.cancelled():
        return "", 0.0
    if task.exception() is not None:
        log.warning("interim speech recognition failed: %s", task.exception())
        return "", 0.0
    return task.result()
