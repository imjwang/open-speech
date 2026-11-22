"""PCM helpers shared by adapters, the audio cache and the transport (no vendor SDKs here).

Internal audio format: mono float32 numpy arrays in [-1, 1]. `AudioChunk.pcm` carries the same samples
as little-endian float32 bytes.
"""

from __future__ import annotations

import io
import math
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

OPUS_RATES = (8000, 12000, 16000, 24000, 48000)


def f32_to_bytes(x: np.ndarray) -> bytes:
    return np.ascontiguousarray(x, dtype="<f4").tobytes()


def bytes_to_f32(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<f4").astype(np.float32, copy=True)


def int16_to_f32(b: bytes) -> np.ndarray:
    if len(b) % 2:
        b = b[:-1]
    return (np.frombuffer(b, dtype="<i2").astype(np.float32)) / 32768.0


def f32_to_int16(x: np.ndarray) -> bytes:
    y = np.clip(x, -1.0, 1.0)
    return (y * 32767.0).astype("<i2").tobytes()


def to_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 2:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float32)


def decode_audio(data: bytes) -> tuple[np.ndarray, int]:
    """Decode any container libsndfile reads (Ogg/Opus, Ogg/Vorbis, WAV, FLAC, MP3) to mono float32."""
    y, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
    return to_mono(y), int(sr)


def read_audio_file(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return to_mono(y), int(sr)


def audio_info_duration(data: bytes) -> float:
    info = sf.info(io.BytesIO(data))
    return float(info.frames) / float(info.samplerate) if info.samplerate else 0.0


def _sinc_lowpass(cutoff: float, taps: int = 63) -> np.ndarray:
    """Windowed-sinc FIR, cutoff as a fraction of the input Nyquist (0..1)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = cutoff * np.sinc(cutoff * n) * np.hanning(taps)
    return (h / h.sum()).astype(np.float32)


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Band-limited resampling good enough for speech: FIR low-pass when downsampling, then linear
    interpolation onto the new grid. Exact pass-through when the rates match."""
    if sr_in == sr_out or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    x = np.asarray(x, dtype=np.float32)
    if sr_out < sr_in:
        x = np.convolve(x, _sinc_lowpass(sr_out / sr_in), mode="same").astype(np.float32)
    n_out = int(round(x.size * sr_out / sr_in))
    if n_out <= 0:
        return np.zeros(0, dtype=np.float32)
    t_out = np.arange(n_out, dtype=np.float64) * (sr_in / sr_out)
    return np.interp(t_out, np.arange(x.size, dtype=np.float64), x).astype(np.float32)


def opus_rate(sr: int) -> int:
    """Nearest Opus-legal sample rate at or above `sr` (libsndfile's Opus wants 8/12/16/24/48 kHz)."""
    for r in OPUS_RATES:
        if r >= sr:
            return r
    return 48000


def encode_ogg_opus(x: np.ndarray, sr: int) -> tuple[bytes, int]:
    """Encode mono float32 to Ogg/Opus; resamples to an Opus rate first if needed."""
    target = opus_rate(sr)
    if target != sr:
        x = resample(x, sr, target)
        sr = target
    buf = io.BytesIO()
    sf.write(buf, np.clip(x, -1.0, 1.0), sr, format="OGG", subtype="OPUS")
    return buf.getvalue(), sr


def wav_bytes(x: np.ndarray, sr: int) -> bytes:
    """16-bit PCM WAV (what batch STT endpoints accept everywhere)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(f32_to_int16(x))
    return buf.getvalue()


def pcm16_wav_bytes(pcm16: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm16)
    return buf.getvalue()


def duration_s(x: np.ndarray, sr: int) -> float:
    return float(x.size) / float(sr) if sr else 0.0


def tone(freq: float, seconds: float, sr: int, amp: float = 0.25) -> np.ndarray:
    n = max(0, int(round(seconds * sr)))
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    t = np.arange(n, dtype=np.float64) / sr
    env = np.hanning(n) if n > 2 else np.ones(n)
    return (amp * env * np.sin(2 * math.pi * freq * t)).astype(np.float32)
