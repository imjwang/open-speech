"""The local asset store: content-addressed files with an index (FR-CACHE-1/2/5).

Everything the engine makes or downloads that is worth keeping is stored once, under the SHA-256 of what
defines it, and found again by that hash:

- ``voice``       synthesized speech. Key = hash(transcript, provider, model, voice, style, format). The
                  transcript is stored beside the audio and hashed on its own (``text_hash``), so every
                  rendering of the same sentence can be found, whatever voice spoke it.
- ``transcript``  what a recording says. Key = hash(recorded audio, recognizer, language).
- ``page``        a fetched URL. Key = hash(normalized URL), with the validators needed to refresh it.

On disk: ``<cache>/<dir>/ab/cd/<key><ext>`` plus ``<key>.json``, the entry's own description (kind, text,
metadata). The sidecar makes every asset self-describing; the SQLite ``assets`` table is only an index of
them (size, last use, text hash) and is rebuilt entry by entry when a file is found without a row. That is
what lets the store survive a database reset and be copied wholesale, and it is the seam for keeping the same
assets in a hosted store later: only `_read`/`_write`/`_unlink` touch the file system.

The store is one cache with one budget: `evict()` removes least-recently-used entries across all kinds.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from open_speech.ids import sha256_hex
from open_speech.store.db import Database

AssetKind = Literal["voice", "transcript", "page"]
SIDECAR_VERSION = 2

# Kind → directory under the cache root. "audio" predates the store and is kept so existing caches stay valid.
_DIRS: dict[str, str] = {"voice": "audio", "transcript": "transcripts", "page": "pages"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def text_hash(text: str) -> str:
    """Hash of a transcript on its own (NFC, surrounding whitespace ignored): the same sentence, any voice."""
    return sha256_hex("text", unicodedata.normalize("NFC", text).strip())


@dataclass
class Asset:
    kind: str
    key: str
    path: Path
    size: int
    created_at: str
    meta: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    text_hash: str | None = None

    def read(self) -> bytes:
        return self.path.read_bytes()


class AssetStore:
    def __init__(self, cache_root: Path, db: Database) -> None:
        self.root = cache_root
        self.db = db

    # ------------------------------------------------------------------ paths

    def _dir(self, kind: str) -> Path:
        return self.root / _DIRS.get(kind, kind)

    def path_for(self, kind: str, key: str, ext: str) -> Path:
        return self._dir(kind) / key[:2] / key[2:4] / f"{key}{ext}"

    def _sidecar(self, kind: str, key: str) -> Path:
        return self._dir(kind) / key[:2] / key[2:4] / f"{key}.json"

    # ------------------------------------------------------------------ read

    def contains(self, kind: str, key: str) -> bool:
        """Presence check that doesn't count as a use (estimates rely on this)."""
        return self._load(kind, key) is not None

    def get(self, kind: str, key: str, *, touch: bool = True) -> Asset | None:
        asset = self._load(kind, key)
        if asset is None:
            self.db.execute("DELETE FROM assets WHERE kind = ? AND key = ?", (kind, key))
            return None
        row = self.db.one("SELECT key FROM assets WHERE kind = ? AND key = ?", (kind, key))
        if row is None:  # a file written before an index reset: adopt it
            self._index(asset, _now())
        elif touch:
            self.db.execute("UPDATE assets SET last_access = ? WHERE kind = ? AND key = ?", (_now(), kind, key))
        return asset

    def find_text(self, text: str, kind: str = "voice") -> list[Asset]:
        """Every stored asset of `kind` whose transcript is `text`."""
        rows = self.db.query(
            "SELECT key FROM assets WHERE kind = ? AND text_hash = ? ORDER BY last_access DESC",
            (kind, text_hash(text)),
        )
        return [a for a in (self._load(kind, r["key"]) for r in rows) if a is not None]

    def _load(self, kind: str, key: str) -> Asset | None:
        side = self._sidecar(kind, key)
        try:
            raw = json.loads(self._read(side))
            if raw.get("v") != SIDECAR_VERSION or raw.get("kind") != kind:
                raise ValueError("unknown sidecar")
            path = self.path_for(kind, key, str(raw["ext"]))
            size = path.stat().st_size
            return Asset(
                kind=kind,
                key=key,
                path=path,
                size=size + side.stat().st_size,
                created_at=str(raw.get("created_at") or ""),
                meta=dict(raw.get("meta") or {}),
                text=raw.get("text"),
                text_hash=raw.get("text_hash"),
            )
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, TypeError, OSError):
            self._unlink(kind, key)  # unreadable entry: drop it so the next write starts clean
            return None

    # ------------------------------------------------------------------ write

    def put(
        self,
        kind: str,
        key: str,
        data: bytes,
        *,
        ext: str,
        meta: dict[str, Any] | None = None,
        text: str | None = None,
    ) -> Asset:
        """Store `data` under `key`. Writes are atomic (temp file + rename): readers never see half a file."""
        path = self.path_for(kind, key, ext)
        side = self._sidecar(kind, key)
        now = _now()
        th = text_hash(text) if text is not None else None
        record = {
            "v": SIDECAR_VERSION,
            "kind": kind,
            "key": key,
            "ext": ext,
            "created_at": now,
            "text": text,
            "text_hash": th,
            "meta": meta or {},
        }
        self._write(path, data)
        self._write(side, json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        asset = Asset(
            kind=kind,
            key=key,
            path=path,
            size=len(data) + side.stat().st_size,
            created_at=now,
            meta=dict(meta or {}),
            text=text,
            text_hash=th,
        )
        self._index(asset, now)
        return asset

    def update_meta(self, kind: str, key: str, **changes: Any) -> Asset | None:
        """Merge `changes` into an entry's metadata (e.g. a page's `fetched_at` after a 304)."""
        asset = self._load(kind, key)
        if asset is None:
            return None
        side = self._sidecar(kind, key)
        record = json.loads(self._read(side))
        record["meta"] = {**(record.get("meta") or {}), **changes}
        self._write(side, json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        asset.meta = record["meta"]
        return asset

    def _index(self, asset: Asset, now: str) -> None:
        self.db.execute(
            "INSERT INTO assets(kind, key, ext, bytes, text_hash, created_at, last_access) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(kind, key) DO UPDATE SET ext=excluded.ext, bytes=excluded.bytes,"
            " text_hash=excluded.text_hash, last_access=excluded.last_access",
            (asset.kind, asset.key, asset.path.suffix, asset.size, asset.text_hash, asset.created_at or now, now),
        )

    # ------------------------------------------------------------------ size and eviction

    def size_bytes(self, kind: str | None = None) -> int:
        row = (
            self.db.one("SELECT COALESCE(SUM(bytes), 0) AS n FROM assets WHERE kind = ?", (kind,))
            if kind
            else self.db.one("SELECT COALESCE(SUM(bytes), 0) AS n FROM assets")
        )
        return int(row["n"]) if row else 0

    def count(self, kind: str | None = None) -> int:
        row = (
            self.db.one("SELECT COUNT(*) AS n FROM assets WHERE kind = ?", (kind,))
            if kind
            else self.db.one("SELECT COUNT(*) AS n FROM assets")
        )
        return int(row["n"]) if row else 0

    def stats(self) -> dict[str, dict[str, int]]:
        rows = self.db.query("SELECT kind, COUNT(*) AS n, COALESCE(SUM(bytes), 0) AS b FROM assets GROUP BY kind")
        return {r["kind"]: {"count": int(r["n"]), "bytes": int(r["b"])} for r in rows}

    def evict(self, max_bytes: int, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Delete least-recently-used entries (pinned keys exempt) until the store fits `max_bytes`."""
        total = self.size_bytes()
        if total <= max_bytes:
            return 0
        removed = 0
        for row in self.db.query("SELECT kind, key, bytes FROM assets ORDER BY last_access ASC, key ASC"):
            if total <= max_bytes:
                break
            if row["key"] in pinned:
                continue
            self.delete(row["kind"], row["key"])
            total -= int(row["bytes"])
            removed += 1
        return removed

    def clear(self, kind: str | None = None, pinned: set[str] | frozenset[str] = frozenset()) -> int:
        """Remove every entry (of one kind, or all) except pinned ones. Returns how many were removed."""
        kinds = [kind] if kind else list(_DIRS)
        removed = 0
        for k in kinds:
            for row in self.db.query("SELECT key FROM assets WHERE kind = ?", (k,)):
                if row["key"] in pinned:
                    continue
                self.delete(k, row["key"])
                removed += 1
            # files on disk without an index row (older layouts, interrupted writes)
            for side in self._dir(k).glob("*/*/*.json"):
                key = side.stem
                if key in pinned or self.db.one("SELECT key FROM assets WHERE kind = ? AND key = ?", (k, key)):
                    continue
                self._unlink(k, key)
                removed += 1
        return removed

    def delete(self, kind: str, key: str) -> None:
        self._unlink(kind, key)
        self.db.execute("DELETE FROM assets WHERE kind = ? AND key = ?", (kind, key))

    # ------------------------------------------------------------------ the only file-system access

    @staticmethod
    def _read(path: Path) -> bytes:
        return path.read_bytes()

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _unlink(self, kind: str, key: str) -> None:
        folder = self._sidecar(kind, key).parent
        if not folder.is_dir():
            return
        for p in folder.glob(f"{key}.*"):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
