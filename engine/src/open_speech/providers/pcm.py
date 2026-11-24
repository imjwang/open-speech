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
# libsndfile maps compression level 0..1 onto Opus bitrates ~256..~10 kbps. Its default gave ~36 kbps on speech,
# where Opus still codes in its band-limited speech modes: 2.0 dB log-spectral distance from the fresh audio.
# 0.8 (~57 kbps) is past the switch to full-band coding: 0.9 dB (audio lab, docs/05 § Caches). Files written
# before keep playing; only new ones are larger (~1.6x).
OPUS_COMPRESSION_LEVEL = 0.8


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


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """Band-limited resampling (soxr, very high quality) in both directions: no imaging when upsampling, no
    aliasing when downsampling. Exact pass-through when the rates match."""
    if sr_in == sr_out or x.size == 0:
        return np.asarray(x, dtype=np.float32)
    import soxr

    y = soxr.resample(np.ascontiguousarray(x, dtype=np.float32), sr_in, sr_out, quality="VHQ")
    return np.asarray(y, dtype=np.float32).reshape(-1)


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
    y = np.clip(x, -1.0, 1.0)
    try:
        sf.write(buf, y, sr, format="OGG", subtype="OPUS", compression_level=OPUS_COMPRESSION_LEVEL)
    except TypeError:  # soundfile < 0.12 can't set it: libsndfile's default (~36 kbps)
        buf = io.BytesIO()
        sf.write(buf, y, sr, format="OGG", subtype="OPUS")
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
