"""Prefixed IDs (04 § Conventions): doc_, blk_, utt_, rule_, act_, rec_, job_, turn_, voice ids are "
"provider:model:voice."""

from __future__ import annotations

import hashlib
import secrets


def new_id(prefix: str, nbytes: int = 8) -> str:
    return f"{prefix}_{secrets.token_hex(nbytes)}"


def sha256_hex(*parts: bytes | str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8") if isinstance(p, str) else p)
        h.update(b"\x1f")
    return h.hexdigest()


def doc_id_for(source_id: str) -> str:
    return f"doc_{source_id[:16]}"


def split_voice_id(voice_id: str) -> tuple[str, str, str]:
    provider, model, voice = voice_id.split(":", 2)
    return provider, model, voice
