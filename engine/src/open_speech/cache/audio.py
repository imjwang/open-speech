"""Content-addressed audio cache (05 § Caches and keys, FR-CACHE-1/2).

Layout: `cache/audio/ab/cd/<sha256>.ogg` (Ogg/Opus) plus `<sha256>.json` (word timings, duration, rate).
Writes are atomic (temp file + rename). An index table (`audio_cache`, created here with
`CREATE TABLE IF NOT EXISTS`) tracks size and last access for LRU eviction; files found on disk without an
index row (e.g. after a database reset) are adopted on first `get`, so the cache survives restarts.

Opus in libsndfile accepts 8/12/16/24/48 kHz only; other rates are resampled up to the next legal rate on
write. libsndfile preserves the exact sample count on read-back, so timings stay aligned.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from open_speech.cache.keys import DEFAULT_FORMAT, audio_key
from open_speech.config import Paths
from open_speech.providers.base import WordTiming
from open_speech.providers.pcm import audio_info_duration, encode_ogg_opus, read_audio_file
from open_speech.store.db import Database

SIDECAR_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audio_cache (
  key TEXT PRIMARY KEY,
  bytes INTEGER NOT NULL,
  duration_s REAL NOT NULL,
  sample_rate INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  last_access TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audio_cache_lru ON audio_cache(last_access);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass
class CachedAudio:
    key: str
    path: Path
    duration_s: float
    sample_rate: int
    words: list[WordTiming] = field(default_factory=list)

    def load_pcm(self) -> np.ndarray:
        """Decoded mono float32 samples at `sample_rate`."""
        x, _ = read_audio_file(self.path)
        return x


class AudioCache:
    def __init__(self, paths: Paths, db: Database) -> None:
        self.root = paths.audio_cache
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db
        for stmt in filter(None, (s.strip() for s in _SCHEMA.split(";"))):
            db.execute(stmt)

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
        return self.root / key[:2] / key[2:4] / f"{key}.ogg"

    def _sidecar(self, key: str) -> Path:
        return self.path_for(key).with_suffix(".json")

    # ------------------------------------------------------------------ read

    def contains(self, key: str) -> bool:
        """Presence check that doesn't count as an access (estimates use this)."""
        return self.path_for(key).exists() and self._sidecar(key).exists()

    def get(self, key: str, *, touch: bool = True) -> CachedAudio | None:
        path = self.path_for(key)
        side = self._sidecar(key)
        if not (path.exists() and side.exists()):
            self.db.execute("DELETE FROM audio_cache WHERE key = ?", (key,))
            return None
        try:
            meta = json.loads(side.read_text("utf-8"))
            words = [WordTiming(int(w[0]), int(w[1]), float(w[2]), float(w[3])) for w in meta.get("words", [])]
            audio = CachedAudio(
                key=key,
                path=path,
                duration_s=float(meta["duration_s"]),
                sample_rate=int(meta["sample_rate"]),
                words=words,
            )
        except (ValueError, KeyError, TypeError, IndexError):
            self._remove_files(key)
            self.db.execute("DELETE FROM audio_cache WHERE key = ?", (key,))
            return None
        row = self.db.one("SELECT key FROM audio_cache WHERE key = ?", (key,))
        now = _now()
        if row is None:  # adopt a file written before an index reset
            self._index(key, path.stat().st_size + side.stat().st_size, audio.duration_s, audio.sample_rate, now)
        elif touch:
            self.db.execute("UPDATE audio_cache SET last_access = ? WHERE key = ?", (now, key))
        return audio

    # ------------------------------------------------------------------ write

    def put(self, key: str, pcm: np.ndarray, sr: int, words: list[WordTiming]) -> CachedAudio:
        """Encode mono float32 PCM to Ogg/Opus and store it with its word timings."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        data, enc_sr = encode_ogg_opus(x, sr)
        duration = x.size / sr if sr else 0.0
        return self._store(key, data, duration, enc_sr, words)

    def put_encoded(self, key: str, data: bytes, words: list[WordTiming]) -> CachedAudio:
        """Store vendor-encoded Ogg/Opus verbatim (OpenAI "opus" responses), no re-encode."""
        import io

        import soundfile as sf

        info = sf.info(io.BytesIO(data))
        return self._store(key, data, audio_info_duration(data), int(info.samplerate), words)

    def _store(self, key: str, data: bytes, duration: float, sr: int, words: list[WordTiming]) -> CachedAudio:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "version": SIDECAR_VERSION,
            "duration_s": round(duration, 6),
            "sample_rate": sr,
            "words": [[w.start, w.end, round(w.t0, 4), round(w.t1, 4)] for w in words],
        }
        _atomic_write(path, data)
        side = self._sidecar(key)
        _atomic_write(side, json.dumps(meta, separators=(",", ":")).encode("utf-8"))
        now = _now()
        self._index(key, len(data) + side.stat().st_size, duration, sr, now)
        return CachedAudio(key=key, path=path, duration_s=duration, sample_rate=sr, words=list(words))

    def _index(self, key: str, nbytes: int, duration: float, sr: int, now: str) -> None:
        self.db.execute(
            "INSERT INTO audio_cache(key, bytes, duration_s, sample_rate, created_at, last_access)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET bytes=excluded.bytes,"
            " duration_s=excluded.duration_s, sample_rate=excluded.sample_rate, last_access=excluded.last_access",
            (key, nbytes, duration, sr, now, now),
        )

    # ------------------------------------------------------------------ eviction

    def size_bytes(self) -> int:
        row = self.db.one("SELECT COALESCE(SUM(bytes), 0) AS n FROM audio_cache")
        return int(row["n"]) if row else 0

    def count(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM audio_cache")
        return int(row["n"]) if row else 0

    def evict(self, max_bytes: int, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Delete least-recently-used entries (pinned keys exempt) until the cache fits `max_bytes`.
        Returns the number of entries removed."""
        total = self.size_bytes()
        if total <= max_bytes:
            return 0
        removed = 0
        for row in self.db.query("SELECT key, bytes FROM audio_cache ORDER BY last_access ASC, key ASC"):
            if total <= max_bytes:
                break
            if row["key"] in pinned:
                continue
            self._remove_files(row["key"])
            self.db.execute("DELETE FROM audio_cache WHERE key = ?", (row["key"],))
            total -= int(row["bytes"])
            removed += 1
        return removed

    def evict_gb(self, max_gb: float, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        return self.evict(int(max_gb * 1024**3), pinned)

    def clear(self, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Remove every entry except pinned ones ("clear cache"). Returns the number removed."""
        removed = 0
        for row in self.db.query("SELECT key FROM audio_cache"):
            if row["key"] in pinned:
                continue
            self._remove_files(row["key"])
            self.db.execute("DELETE FROM audio_cache WHERE key = ?", (row["key"],))
            removed += 1
        # orphans on disk without an index row
        for p in self.root.glob("*/*/*.ogg"):
            if p.stem not in pinned and self.db.one("SELECT key FROM audio_cache WHERE key = ?", (p.stem,)) is None:
                self._remove_files(p.stem)
                removed += 1
        return removed

    def delete(self, key: str) -> None:
        self._remove_files(key)
        self.db.execute("DELETE FROM audio_cache WHERE key = ?", (key,))

    def _remove_files(self, key: str) -> None:
        for p in (self.path_for(key), self._sidecar(key)):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
