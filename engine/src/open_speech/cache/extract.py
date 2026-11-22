"""Extract cache (05 § Caches): `Document`s as zstd JSON under `cache/extract/<sha256>.json.zst`, keyed by
`sha256(source_id, extractor_version)`. Invalidated only by extractor upgrades."""

from __future__ import annotations

import contextlib
import os

import zstandard

from open_speech.config import Paths
from open_speech.document.model import Document
from open_speech.ids import sha256_hex


class ExtractCache:
    def __init__(self, paths: Paths) -> None:
        self.dir = paths.extract_cache

    @staticmethod
    def key(source_id: str, extractor_version: str) -> str:
        return sha256_hex(source_id, extractor_version)

    def _path(self, key: str) -> os.PathLike[str]:
        return self.dir / f"{key}.json.zst"

    def get(self, source_id: str, extractor_version: str) -> Document | None:
        path = self.dir / f"{self.key(source_id, extractor_version)}.json.zst"
        if not path.exists():
            return None
        try:
            raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
            return Document.model_validate_json(raw)
        except Exception:  # corrupt entry: drop it and re-extract
            with contextlib.suppress(OSError):
                path.unlink()
            return None

    def put(self, document: Document) -> str:
        """Store and return the cache key (stored in `documents.extract_key`)."""
        key = self.key(document.source_id, document.extractor_version)
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{key}.json.zst"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(zstandard.ZstdCompressor(level=6).compress(document.model_dump_json().encode("utf-8")))
        os.replace(tmp, path)
        return key

    def delete(self, source_id: str, extractor_version: str) -> None:
        with contextlib.suppress(OSError):
            (self.dir / f"{self.key(source_id, extractor_version)}.json.zst").unlink()
