"""Synthesized speech in the asset store (05 § Caches and keys, FR-CACHE-1/2).

Each entry is one utterance spoken by one voice: Ogg/Opus audio under `cache/audio/ab/cd/<sha256>.ogg`, with its
transcript, word timings and voice recorded beside it (`store/assets.py`). The key covers exactly what changes
the waveform (`cache/keys.audio_key`), so replays are free and any speed uses the same file.

Opus in libsndfile accepts 8/12/16/24/48 kHz only; other rates are resampled up to the next legal rate on
write. libsndfile preserves the exact sample count on read-back, so timings stay aligned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from open_speech.cache.keys import DEFAULT_FORMAT, audio_key
from open_speech.providers.base import WordTiming
from open_speech.providers.pcm import audio_info_duration, encode_ogg_opus, read_audio_file
from open_speech.store.assets import Asset, AssetStore

KIND = "voice"


@dataclass
class CachedAudio:
    key: str
    path: Path
    duration_s: float
    sample_rate: int
    words: list[WordTiming] = field(default_factory=list)
    text: str | None = None  # the transcript this audio speaks

    def load_pcm(self) -> np.ndarray:
        """Decoded mono float32 samples at `sample_rate`."""
        x, _ = read_audio_file(self.path)
        return x


@dataclass(frozen=True)
class VoiceRef:
    """Which voice spoke an asset (recorded with it, so the store can be browsed without the keys)."""

    provider: str
    model: str
    voice: str
    style: str | None = None


class AudioCache:
    def __init__(self, assets: AssetStore) -> None:
        self.assets = assets

    # ------------------------------------------------------------------ keys and paths

    @staticmethod
    def key(
        text: str,
        provider: str,
        model: str,
        voice: str,
        style: str | None = None,
        fmt: str = DEFAULT_FORMAT,
    ) -> str:
        return audio_key(text, provider, model, voice, style, fmt)

    def path_for(self, key: str) -> Path:
        return self.assets.path_for(KIND, key, ".ogg")

    # ------------------------------------------------------------------ read

    def contains(self, key: str) -> bool:
        return self.assets.contains(KIND, key)

    def get(self, key: str, *, touch: bool = True) -> CachedAudio | None:
        asset = self.assets.get(KIND, key, touch=touch)
        if asset is None:
            return None
        try:
            return self._audio(asset)
        except (ValueError, KeyError, TypeError, IndexError):
            self.assets.delete(KIND, key)
            return None

    @staticmethod
    def _audio(asset: Asset) -> CachedAudio:
        m = asset.meta
        words = [WordTiming(int(w[0]), int(w[1]), float(w[2]), float(w[3])) for w in m.get("words", [])]
        return CachedAudio(
            key=asset.key,
            path=asset.path,
            duration_s=float(m["duration_s"]),
            sample_rate=int(m["sample_rate"]),
            words=words,
            text=asset.text,
        )

    # ------------------------------------------------------------------ write

    def put(
        self,
        key: str,
        pcm: np.ndarray,
        sr: int,
        words: list[WordTiming],
        *,
        text: str | None = None,
        voice: VoiceRef | None = None,
    ) -> CachedAudio:
        """Encode mono float32 PCM to Ogg/Opus and store it with its transcript and word timings."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        data, enc_sr = encode_ogg_opus(x, sr)
        duration = x.size / sr if sr else 0.0
        return self._store(key, data, duration, enc_sr, words, text, voice)

    def put_encoded(
        self,
        key: str,
        data: bytes,
        words: list[WordTiming],
        *,
        text: str | None = None,
        voice: VoiceRef | None = None,
    ) -> CachedAudio:
        """Store vendor-encoded Ogg/Opus verbatim (OpenAI "opus" responses), no re-encode."""
        import io

        import soundfile as sf

        info = sf.info(io.BytesIO(data))
        return self._store(key, data, audio_info_duration(data), int(info.samplerate), words, text, voice)

    def _store(
        self,
        key: str,
        data: bytes,
        duration: float,
        sr: int,
        words: list[WordTiming],
        text: str | None,
        voice: VoiceRef | None,
    ) -> CachedAudio:
        meta: dict[str, Any] = {
            "duration_s": round(duration, 6),
            "sample_rate": sr,
            "words": [[w.start, w.end, round(w.t0, 4), round(w.t1, 4)] for w in words],
        }
        if voice is not None:
            meta.update(provider=voice.provider, model=voice.model, voice=voice.voice, style=voice.style or "")
        asset = self.assets.put(KIND, key, data, ext=".ogg", meta=meta, text=text)
        return CachedAudio(
            key=key, path=asset.path, duration_s=duration, sample_rate=sr, words=list(words), text=text
        )

    # ------------------------------------------------------------------ transcripts

    def renderings(self, text: str) -> list[CachedAudio]:
        """Every stored rendering of `text`, whatever voice spoke it (found by the transcript's own hash)."""
        return [self._audio(a) for a in self.assets.find_text(text, KIND)]

    # ------------------------------------------------------------------ size and eviction

    def size_bytes(self) -> int:
        return self.assets.size_bytes(KIND)

    def count(self) -> int:
        return self.assets.count(KIND)

    def evict(self, max_bytes: int, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Delete least-recently-used entries until the whole asset store fits `max_bytes`."""
        return self.assets.evict(max_bytes, pinned)

    def evict_gb(self, max_gb: float, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        return self.evict(int(max_gb * 1024**3), pinned)

    def clear(self, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Remove every voice asset except pinned ones ("clear cache"). Returns the number removed."""
        return self.assets.clear(KIND, pinned)

    def delete(self, key: str) -> None:
        self.assets.delete(KIND, key)

    def delete_voice(self, provider: str, model: str, voice: str) -> int:
        """Remove everything one voice said (a cloned voice deleted, or its reference replaced). Entries record
        which voice spoke them (`VoiceRef`); the keys alone can't say. Returns the number removed."""
        removed = 0
        for row in self.assets.db.query("SELECT key FROM assets WHERE kind = ?", (KIND,)):
            asset = self.assets.get(KIND, row["key"], touch=False)
            if asset is None:
                continue
            m = asset.meta
            if m.get("provider") == provider and m.get("model") == model and m.get("voice") == voice:
                self.assets.delete(KIND, asset.key)
                removed += 1
        return removed
