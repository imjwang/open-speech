"""Where cloned and designed voices live: `voices/<slug>/` in app data (03 § Storage), never pruned.

    voices/<slug>/source.<ext>     what was recorded or given (kept: a later processing version can redo the rest)
    voices/<slug>/reference.wav    24 kHz mono, cleaned (voices/reference.py): what Qwen3-TTS Base listens to
    voices/<slug>/voice.json       {slug, name, language, source, design_prompt?, ref_text, ref_sha256,
                                    processing_version, duration_s, created_at, kept, consent, check, quality}

A voice is *kept* once the user has heard it and said yes (or made it through the API or the CLI, which keep at
once). Until then it can be synthesized (the "Keep it?" sample) but isn't listed among the voices; a pending voice
left over for a day is removed at startup.

`VoiceStore` is the `providers.qwen3.CloneStore` the registry hands the Qwen3 provider: `clones()` lists the kept
voices, `clone(slug)` finds any of them (kept or pending), as `CloneVoice`s.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from open_speech.ids import sha256_hex
from open_speech.providers.qwen3 import CloneVoice

Source = Literal["recording", "file", "design"]
PENDING_TTL = timedelta(hours=24)
VOICE_JSON = "voice.json"
REFERENCE = "reference.wav"


def slugify(name: str) -> str:
    """"Jeff's voice" → "jeffs-voice", "Mi voz" → "mi-voz" (ASCII, lowercase, hyphens; never empty)."""
    t = unicodedata.normalize("NFKD", name.lower())
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("'", "").replace("’", "")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:40].strip("-") or "voice"


def ref_hash(reference: bytes, ref_text: str, processing_version: str) -> str:
    """What identifies a reference: its audio, exactly what it says (NFC) and how it was processed. Part of the cache
    identity of everything the voice says, so a new reference or transcript makes new audio."""
    return sha256_hex(reference, unicodedata.normalize("NFC", ref_text.strip()), processing_version)


@dataclass
class VoiceRecord:
    slug: str
    name: str
    language: str
    source: Source
    ref_text: str
    ref_sha256: str
    processing_version: str
    duration_s: float
    created_at: str
    kept: bool = False
    design_prompt: str | None = None
    consent: dict[str, Any] = field(default_factory=dict)  # {text_version, accepted_at}
    check: dict[str, Any] = field(default_factory=dict)  # {wer, similarity, grade, recording_id}
    quality: dict[str, Any] = field(default_factory=dict)  # reference.Quality.as_dict() + warnings
    source_file: str | None = None  # the original's file name inside the folder

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "language": self.language,
            "source": self.source,
            **({"design_prompt": self.design_prompt} if self.design_prompt else {}),
            "ref_text": self.ref_text,
            "ref_sha256": self.ref_sha256,
            "processing_version": self.processing_version,
            "duration_s": round(self.duration_s, 3),
            "created_at": self.created_at,
            "kept": self.kept,
            "consent": self.consent,
            "check": self.check,
            "quality": self.quality,
            **({"source_file": self.source_file} if self.source_file else {}),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> VoiceRecord:
        return cls(
            slug=str(data["slug"]),
            name=str(data.get("name") or data["slug"]),
            language=str(data.get("language") or "en"),
            source=data.get("source") or "file",
            ref_text=str(data.get("ref_text") or ""),
            ref_sha256=str(data.get("ref_sha256") or ""),
            processing_version=str(data.get("processing_version") or ""),
            duration_s=float(data.get("duration_s") or 0.0),
            created_at=str(data.get("created_at") or ""),
            kept=bool(data.get("kept", True)),
            design_prompt=data.get("design_prompt"),
            consent=dict(data.get("consent") or {}),
            check=dict(data.get("check") or {}),
            quality=dict(data.get("quality") or {}),
            source_file=data.get("source_file"),
        )


class VoiceStore:
    """The voices folder (see the module docstring). Thread-safe: processing runs in worker threads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ paths

    def folder(self, slug: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
            raise ValueError(f"not a voice slug: {slug!r}")
        return self.root / slug

    def reference_path(self, slug: str) -> Path:
        return self.folder(slug) / REFERENCE

    # ------------------------------------------------------------------ reading

    def records(self, *, include_pending: bool = False) -> list[VoiceRecord]:
        out: list[VoiceRecord] = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            rec = self._load(d)
            if rec is not None and (rec.kept or include_pending):
                out.append(rec)
        out.sort(key=lambda r: r.created_at)
        return out

    def get(self, slug: str) -> VoiceRecord | None:
        try:
            d = self.folder(slug)
        except ValueError:
            return None
        return self._load(d) if d.is_dir() else None

    def find(self, name: str) -> VoiceRecord | None:
        """A kept voice by name or slug ("Jeff", "jeff", "my-voice"). "My voice" / "mi voz" with no voice of that
        name means the user's own: the latest one made from a recording."""
        want = slugify(name)
        recs = self.records()
        exact = next((r for r in recs if r.slug == want or slugify(r.name) == want), None)
        if exact is not None:
            return exact
        if want in ("my-voice", "my-own-voice", "my-clone", "my-cloned-voice", "mi-voz", "mi-clon", "mi-voz-clonada"):
            own = [r for r in recs if r.source == "recording"]
            if own:
                return own[-1]
        return next((r for r in recs if want and (want in r.slug or want in slugify(r.name))), None)

    def _load(self, d: Path) -> VoiceRecord | None:
        try:
            return VoiceRecord.from_json(json.loads((d / VOICE_JSON).read_text("utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # ------------------------------------------------------------------ CloneStore (providers/qwen3.py)

    def _clone(self, rec: VoiceRecord) -> CloneVoice:
        ref = self.reference_path(rec.slug)
        ok = ref.exists() and bool(rec.ref_text)
        return CloneVoice(slug=rec.slug, name=rec.name, language=rec.language, ref_audio=ref, ref_text=rec.ref_text,
                          ref_sha256=rec.ref_sha256, available=ok, unavailable_reason=None, source=rec.source)

    def clones(self) -> list[CloneVoice]:
        return [self._clone(r) for r in self.records()]

    def clone(self, slug: str) -> CloneVoice | None:
        rec = self.get(slug)
        return self._clone(rec) if rec is not None else None

    # ------------------------------------------------------------------ writing

    def new_slug(self, name: str) -> str:
        """A free slug for `name` ("my-voice", then "my-voice-2", …). Reserves it by creating the folder."""
        base = slugify(name)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            n = 1
            while True:
                slug = base if n == 1 else f"{base[:36]}-{n}"
                d = self.root / slug
                try:
                    d.mkdir()
                    return slug
                except FileExistsError:
                    n += 1

    def unique_name(self, name: str) -> str:
        """`name`, or "name 2", "name 3" … when a kept voice already has it."""
        taken = {r.name.strip().lower() for r in self.records(include_pending=True)}
        if name.strip().lower() not in taken:
            return name
        n = 2
        while f"{name} {n}".lower() in taken:
            n += 1
        return f"{name} {n}"

    def save(self, rec: VoiceRecord) -> None:
        with self._lock:
            d = self.folder(rec.slug)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / (VOICE_JSON + ".tmp")
            tmp.write_text(json.dumps(rec.to_json(), indent=2, ensure_ascii=False), "utf-8")
            tmp.replace(d / VOICE_JSON)

    def update(self, slug: str, **changes: Any) -> VoiceRecord | None:
        with self._lock:
            rec = self.get(slug)
            if rec is None:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            self.save(rec)
            return rec

    def delete(self, slug: str) -> bool:
        with self._lock:
            try:
                d = self.folder(slug)
            except ValueError:
                return False
            if not d.exists():
                return False
            shutil.rmtree(d, ignore_errors=True)
            return True

    def prune_pending(self, older_than: timedelta = PENDING_TTL) -> int:
        """Remove voices nobody kept (and folders that never got a voice.json) older than `older_than`."""
        if not self.root.is_dir():
            return 0
        cutoff = datetime.now(UTC) - older_than
        removed = 0
        for d in self.root.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            rec = self._load(d)
            if rec is not None and rec.kept:
                continue
            try:
                when = datetime.fromisoformat(rec.created_at) if rec is not None and rec.created_at else None
            except ValueError:
                when = None
            if when is None:
                with contextlib.suppress(OSError):
                    when = datetime.fromtimestamp(d.stat().st_mtime, UTC)
            if when is not None and when < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        return removed


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["REFERENCE", "Source", "VoiceRecord", "VoiceStore", "now_iso", "ref_hash", "slugify"]
