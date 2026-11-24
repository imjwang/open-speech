"""Cloned and designed voices (08 § Voice cloning, FR-VOICE-6, FR-VOICE-8): making them, checking them, keeping them,
and the conversation around it.

Making a clone is a job (service/jobs.py, `job.progress`):
  1. the reference: voices/reference.py picks and cleans 6-10 s of the recording or file;
  2. what it says: the on-device speech recognizer (Parakeet; never a cloud service) transcribes the window. For a
     passage the user read, the reference text is the passage's own words for that window when the recognizer agrees
     with it (word error rate ≤ 0.15), else the recording is turned down with a plain reason; for a file, the
     transcript is the reference text (editable later; typed text is needed only without the recognizer);
  3. the check: the new voice says a test sentence, the recognizer transcribes it, and the word error rate grades it
     good (≤ 10 %), fair (≤ 25 %) or poor. The user hears that sentence and decides: "Keep it?".

A designed voice starts from words: VoiceDesign renders a neutral passage three times with different seeds, each
candidate checked the same way; the one the user keeps becomes a clone of itself (so it sounds the same every time
afterwards, through the Base weights like any clone).

In the bar this is a conversation (07 § Fast path): "clone my voice" shows a card with a passage and the consent
line; "ready" (or the record button) and the bar records with its own capture path; the upload starts a turn that
says the check sentence in the new voice and asks "Keep it?"; "yes" keeps it and makes it the reading voice. "Clone
this voice" with an audio file attached asks for consent on a card, then does the same in that turn. When the
cloning weights aren't downloaded, the first card says how big they are; "yes" downloads them while the user reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from open_speech.api import schemas as s
from open_speech.api.ws_schema import AssistantCard, AssistantDelta, CardRow
from open_speech.config import AUTO_VOICE, DEFAULT_LOCAL_VOICE
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import resolve_language, say, t
from open_speech.ids import new_id, sha256_hex
from open_speech.providers.base import TtsRequest
from open_speech.providers.pcm import bytes_to_f32, encode_ogg_opus, resample
from open_speech.providers.qwen3 import CLONE_MODEL, FAMILY_CLONE, FAMILY_DESIGN, QWEN3, size_label
from open_speech.service.commands import Reply
from open_speech.service.jobs import Job, Progress
from open_speech.voices import passages as P
from open_speech.voices import reference as R
from open_speech.voices.store import VoiceRecord, VoiceStore, now_iso, ref_hash
from open_speech.voices.text import best_span, wer

if TYPE_CHECKING:
    from open_speech.service.commands import Turn
    from open_speech.service.engine import Engine

log = logging.getLogger(__name__)

PASSAGE_MAX_WER = 0.15  # the recording must say the passage (08 § Voice cloning)
GRADES = ((0.10, "good"), (0.25, "fair"))  # check sentence WER → grade; above the last: poor
DESIGN_CANDIDATES = 3
DESIGN_MAX_WER = 0.25  # a candidate that garbles the passage is rendered again with another seed
DESIGN_TRIES = 6
NAME_MAX = 64
DESIGN_LIMITS = R.Limits(min_speech_s=1.0, reject_snr_db=-200.0, warn_snr_db=-200.0)
INSTALL_WAIT_S = 3600.0

# Confirmation cards this service owns (assistant.action kinds).
CLONE_RECORD = "clone_record"  # the passage card: "ready" / the record button starts the bar's reference capture
CLONE_DOWNLOAD = "clone_download"  # the cloning weights are missing: download them (and go on to the passage)
CLONE_FILE = "clone_file"  # make a voice from an attached audio file (consent on the card)
KEEP_CLONE = "keep_clone"  # "This is how I sound with your voice. Keep it?"
DESIGN_VOICE = "design_voice"  # make three voices from a description (after a download, or proposed by the agent)
KEEP_DESIGN = "keep_design"  # "Which one should I keep?"
DELETE_VOICE = "delete_voice"
ACTION_KINDS = frozenset(
    {CLONE_RECORD, CLONE_DOWNLOAD, CLONE_FILE, KEEP_CLONE, DESIGN_VOICE, KEEP_DESIGN, DELETE_VOICE}
)
AUDIO_EXTS = {".wav", ".wave", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".aif", ".aiff", ".caf",
              ".mp4", ".webm"}  # fmt: skip

Transcriber = Callable[[np.ndarray], Awaitable[str | None]]  # 16 kHz mono float32 → text (None: no recognizer)


def is_audio_path(path: str | None) -> bool:
    return bool(path) and Path(str(path)).suffix.lower() in AUDIO_EXTS


def grade_for(error_rate: float | None) -> str | None:
    if error_rate is None:
        return None
    for limit, grade in GRADES:
        if error_rate <= limit:
            return grade
    return "poor"


def name_from_description(description: str) -> str:
    """A default name from what the voice was described as, up to the first "with …" / "who …" ("a calm old
    storyteller with a deep British accent" → "Calm old storyteller"); the user can rename it."""
    words = re.sub(r"[^\w\s'-]", " ", description).split()
    stop = {"a", "an", "the", "voice", "sounds", "sounding", "like", "una", "un", "el", "la", "voz", "suene", "como",
            "some", "very"}  # fmt: skip
    cut = {"with", "who", "that", "which", "and", "of", "con", "que", "y", "de", "del"}
    segments: list[list[str]] = [[]]
    for w in words:
        if w.lower() in cut:
            segments.append([])
        elif w.lower() not in stop:
            segments[-1].append(w)
    kept = next((seg for seg in segments if seg), [])[:4]
    name = " ".join(kept) or description.strip()[:30]
    return (name[:1].upper() + name[1:])[:40]


def name_from_file(path: Path) -> str:
    stem = re.sub(r"[_\-.]+", " ", path.stem).strip()
    return (stem[:1].upper() + stem[1:])[:40] if stem else t("clone.default_name")


@dataclass
class Candidate:
    id: str
    job_id: str
    index: int  # 1-based, as spoken
    seed: int
    path: Path  # 24 kHz WAV
    recording_id: str
    wer: float | None
    description: str
    language: str
    name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class CloneService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.store = VoiceStore(engine.paths.voices)
        self.transcriber: Transcriber | None = None  # tests swap in a fake; default: on-device Parakeet
        self.vad: R.SpeechDetector | None = None  # tests swap in one; default: Silero, else energy
        self.limits = R.LIMITS
        self._candidates: dict[str, Candidate] = {}
        self._clone_locks: dict[str, asyncio.Lock] = {}
        engine.registry.set_clone_store(self.store)
        with contextlib.suppress(OSError):
            self.store.prune_pending()
            self._prune_designs()

    # ------------------------------------------------------------------ the pieces it uses

    def qwen(self) -> Any | None:
        q = self.e.registry.qwen3()
        return q if q is not None and q.voices() is not None else None

    def available(self) -> str | None:
        """Why cloning can't run on this Mac at all (no Apple Silicon, no runtime), or None."""
        q = self.e.registry.qwen3()
        if q is None:
            return t("qwen.unavailable.chip")
        if not q.runtime_available():
            return t("qwen.unavailable.chip") if not q._platform_ok() else t("qwen.unavailable.runtime")  # noqa: SLF001
        return None

    def missing_weights(self, family: str = FAMILY_CLONE) -> Any | None:
        """The weights variant to download for `family` (None when it, or a sibling, is downloaded)."""
        q = self.e.registry.qwen3()
        if q is None:
            return None
        res = q.resolve(family)
        if res is None or res.installed:
            return None
        return res.wanted

    def _vad(self) -> R.SpeechDetector:
        if self.vad is not None:
            return self.vad
        try:
            path = self.e.registry.local_stt().vad_path()
            if path is not None:
                return R.SileroVad(path)
        except Exception:  # noqa: BLE001 — no sherpa-onnx, no model: the energy detector still finds speech
            log.info("Silero VAD unavailable for clone references; using the energy detector")
        return R.EnergyVad()

    async def transcribe(self, x16: np.ndarray) -> str | None:
        """What 16 kHz audio says, on this Mac only. None when there is no on-device recognizer."""
        if self.transcriber is not None:
            return await self.transcriber(x16)
        stt = self.e.registry.local_stt()
        if stt.unavailable_reason() is not None:
            return None
        text: str = await stt.transcribe_pcm(x16, R.VAD_SR)
        return text

    def _lock(self, slug: str) -> asyncio.Lock:
        return self._clone_locks.setdefault(slug, asyncio.Lock())

    # ------------------------------------------------------------------ queries

    def out(self, rec: VoiceRecord) -> s.CloneOut:
        check = rec.check or {}
        warnings = [str(w) for w in (rec.quality or {}).get("warnings", [])]
        return s.CloneOut(
            slug=rec.slug,
            voice_id=f"{QWEN3}:{CLONE_MODEL}:{rec.slug}",
            name=rec.name,
            language=rec.language,
            source=rec.source,
            design_prompt=rec.design_prompt,
            ref_text=rec.ref_text,
            ref_sha256=rec.ref_sha256,
            processing_version=rec.processing_version,
            duration_s=round(rec.duration_s, 2),
            created_at=rec.created_at,
            kept=rec.kept,
            consent=s.CloneConsent(**rec.consent) if rec.consent.get("accepted_at") else None,
            check=s.CloneCheck(
                wer=check.get("wer"), similarity=check.get("similarity"), grade=check.get("grade"),
                recording_id=check.get("recording_id"), note=check.get("note"),
            ) if check else None,
            quality={k: v for k, v in (rec.quality or {}).items() if k != "warnings"},
            warnings=warnings,
        )

    def listing(self) -> s.CloneList:
        return s.CloneList(items=[self.out(r) for r in self.store.records()])

    def get(self, slug: str) -> s.CloneOut:
        rec = self.store.get(slug)
        if rec is None:
            raise NotFound(t("what.clone"))
        return self.out(rec)

    def passages(self, language: str | None = None) -> s.PassageList:
        lang = P.base_language(language or resolve_language(self.e.settings.get().language))
        return s.PassageList(
            items=[s.PassageOut(id=p.id, language=p.language, text=p.text) for p in P.passages(lang)],
            consent=t("clone.consent"),
            consent_version=P.CONSENT_VERSION,
        )

    # ------------------------------------------------------------------ creating (API, CLI, the bar's upload)

    async def create(
        self,
        *,
        data: bytes | None = None,
        path: Path | None = None,
        filename: str = "recording.wav",
        name: str | None = None,
        consent: bool = False,
        language: str | None = None,
        ref_text: str | None = None,
        passage_id: str | None = None,
        source: str = "file",
        keep: bool = True,
        session_id: str | None = None,
        conversation_id: str | None = None,
        replace: str | None = None,
    ) -> s.CloneAccepted:
        """Start making a voice (a job). With `session_id` (the bar's upload) the result is a turn in that
        conversation: the check sentence in the new voice, then "Keep it?". `replace`: a kept voice whose reference
        this recording replaces (same slug, same name, a new reference hash)."""
        if not consent:
            err = EngineError("invalid_request", t("clone.consent_required"))
            err.status = 422
            raise err
        reason = self.available()
        if reason:
            raise EngineError("unavailable", reason, provider=QWEN3)
        passage = P.passage(passage_id) if passage_id else None
        if passage_id and passage is None:
            raise EngineError("invalid_request", t("clone.unknown_passage"))
        lang = P.base_language(language or (passage.language if passage else None)
                               or resolve_language(self.e.settings.get().language))
        if replace is not None:
            existing = self.store.get(replace)
            if existing is None:
                raise NotFound(t("what.clone"))
            slug, name = replace, existing.name
        else:
            name = self._clean_name(name) or (name_from_file(Path(filename)) if source == "file" and filename
                                              and not filename.startswith("recording") else t("clone.default_name"))
            name = self.store.unique_name(name)
            slug = self.store.new_slug(name)
        folder = self.store.folder(slug)
        staging = Path(tempfile.mkdtemp(prefix="source-", dir=self.store.root))
        ext = (Path(filename).suffix.lower() or ".wav") if not path else path.suffix.lower()
        src = staging / f"source{ext or '.wav'}"
        if data is not None:
            src.write_bytes(data)
        elif path is not None:
            p = Path(path).expanduser()
            if not p.is_file():
                shutil.rmtree(staging, ignore_errors=True)
                if replace is None:
                    self.store.delete(slug)
                raise NotFound(t("what.audio_file"))
            await asyncio.to_thread(shutil.copyfile, p, src)
        else:
            raise EngineError("invalid_request", t("clone.no_audio"))
        consent_info = {"text_version": P.CONSENT_VERSION, "accepted_at": now_iso()}
        plan = _ClonePlan(slug=slug, name=name, language=lang, source=source, passage=passage,
                          typed_text=(ref_text or "").strip() or None, keep=keep, consent=consent_info,
                          src=src, staging=staging, folder=folder, replace=replace is not None)
        job = self.e.jobs.start("clone", lambda progress: self._process(plan, progress), label=t("clone.job"))
        if session_id is not None:
            self.e.assistant.task_turn(
                session_id=session_id, conversation_id=conversation_id,
                work=lambda turn: self._turn_for_job(turn, job, plan),
            )
        return s.CloneAccepted(job_id=job.id, slug=slug, voice_id=f"{QWEN3}:{CLONE_MODEL}:{slug}")

    @staticmethod
    def _clean_name(name: str | None) -> str | None:
        n = re.sub(r"\s+", " ", (name or "").strip())[:NAME_MAX]
        return n or None

    async def _process(self, plan: _ClonePlan, progress: Progress) -> dict[str, Any]:
        """The job: reference → text → files → check. Fails with an EngineError whose detail says what to do."""
        try:
            async with self._lock(plan.slug):
                return await self._process_locked(plan, progress)
        except BaseException:
            if not plan.replace:
                self.store.delete(plan.slug)
            raise
        finally:
            shutil.rmtree(plan.staging, ignore_errors=True)

    async def _process_locked(self, plan: _ClonePlan, progress: Progress) -> dict[str, Any]:
        progress(5, t("clone.step.cleaning"))
        # A designed voice's audio comes from our own model: trimmed and levelled, never turned down.
        limits = DESIGN_LIMITS if plan.source == "design" else self.limits
        vad = R.LevelVad() if plan.source == "design" and self.vad is None else self._vad()
        try:
            prepared = await asyncio.to_thread(R.prepare_file, plan.src, vad=vad, limits=limits)
        except R.ReferenceRejected as exc:
            raise EngineError("invalid_request", rejection_text(exc.code, exc.params)) from exc
        progress(30, t("clone.step.listening"))
        heard = await self.transcribe(prepared.audio_16k())
        ref_text, warnings = self._reference_text(plan, heard)
        warnings = [rejection_text(code, params, warning=True) for code, params in prepared.warnings] + warnings
        progress(45, t("clone.step.saving"))
        folder = plan.folder
        folder.mkdir(parents=True, exist_ok=True)
        source_name = f"source{plan.src.suffix}"
        if plan.replace:
            for previous in folder.glob("source.*"):
                previous.unlink(missing_ok=True)
        await asyncio.to_thread(shutil.copyfile, plan.src, folder / source_name)
        ref_path = folder / "reference.wav"
        await asyncio.to_thread(R.write_wav, ref_path, prepared.audio)
        digest = ref_hash(ref_path.read_bytes(), ref_text, R.PROCESSING_VERSION)
        old = self.store.get(plan.slug)
        rec = VoiceRecord(
            slug=plan.slug, name=plan.name, language=plan.language, source=plan.source,  # type: ignore[arg-type]
            ref_text=ref_text, ref_sha256=digest, processing_version=R.PROCESSING_VERSION,
            duration_s=prepared.duration_s, created_at=old.created_at if old else now_iso(),
            kept=plan.keep or (old.kept if old else False), design_prompt=plan.design_prompt,
            consent=plan.consent, quality={**prepared.quality.as_dict(), "warnings": warnings,
                                           "window": [round(prepared.window.start_s, 2),
                                                      round(prepared.window.end_s, 2)]},
            source_file=source_name,
        )
        self.store.save(rec)
        if plan.replace:
            self.evict_audio(plan.slug)
        check = await self._check(rec, progress, start=55)
        rec = self.store.update(plan.slug, check=check) or rec
        progress(100, t("clone.step.done"))
        return {"slug": rec.slug, "voice_id": f"{QWEN3}:{CLONE_MODEL}:{rec.slug}", "name": rec.name,
                "ref_text": rec.ref_text, "duration_s": round(rec.duration_s, 2), "check": check,
                "warnings": warnings, "kept": rec.kept, "quality": rec.quality}

    def _reference_text(self, plan: _ClonePlan, heard: str | None) -> tuple[str, list[str]]:
        """What the reference window says, and any warning about it (see the module docstring)."""
        if plan.passage is not None:
            if heard is None:
                return plan.passage.text, [t("clone.warn.unverified")]
            span = best_span(plan.passage.text, heard)
            if span.words and span.wer <= PASSAGE_MAX_WER and span.coverage >= 0.3:
                return span.text, []
            raise EngineError("invalid_request", t("clone.reject.mismatch"))
        if plan.typed_text:
            return plan.typed_text, []
        if heard:
            return heard.strip(), []
        if heard is None:
            raise EngineError("invalid_request", t("clone.reject.need_text"))
        raise EngineError("invalid_request", t("clone.reject.no_words"))

    async def _check(self, rec: VoiceRecord, progress: Progress, *, start: float) -> dict[str, Any]:
        """The new voice says the test sentence; the recognizer grades it. Waits for the cloning weights when they
        are downloading; without them (and no download running) the check is left for later."""
        if not await self._wait_for_weights(FAMILY_CLONE, progress, start):
            missing = self.missing_weights(FAMILY_CLONE)
            note = t("clone.check.needs_download", size=size_label(missing.size_mb)) if missing else None
            return {"wer": None, "similarity": None, "grade": None, "note": note}
        progress(max(start, 70), t("clone.step.checking"))
        text = P.check_text(rec.language)
        pcm = await self.speak(rec.slug, text, language=rec.language)
        heard = await self.transcribe(resample(pcm, R.TARGET_SR, R.VAD_SR)) if pcm.size else None
        error_rate = wer(text, heard) if heard is not None else None
        recording_id = await self._save_recording(pcm, R.TARGET_SR) if pcm.size else None
        return {"wer": round(error_rate, 3) if error_rate is not None else None, "similarity": None,
                "grade": grade_for(error_rate) if pcm.size else "poor", "recording_id": recording_id,
                "text": text}

    async def speak(self, slug: str, text: str, *, language: str | None = None) -> np.ndarray:
        """`text` in a cloned voice, straight from the Qwen provider (never a fallback voice: a check must hear the
        clone or nothing)."""
        q = self.e.registry.qwen3()
        if q is None:
            raise EngineError("unavailable", t("qwen.unavailable.chip"), provider=QWEN3)
        parts: list[np.ndarray] = []
        async for chunk in q.synthesize(TtsRequest(text=text, model=CLONE_MODEL, voice=slug, language=language)):
            parts.append(bytes_to_f32(chunk.pcm) if chunk.sample_rate == R.TARGET_SR
                         else resample(bytes_to_f32(chunk.pcm), chunk.sample_rate, R.TARGET_SR))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    async def _save_recording(self, pcm: np.ndarray, sr: int) -> str:
        """A sample as a recording (kind "preview": never pruned with conversations), for replay."""
        rec_id, path = self.e.recordings.new_path()
        data, _ = await asyncio.to_thread(encode_ogg_opus, pcm, sr)
        await asyncio.to_thread(path.write_bytes, data)
        self.e.recordings.insert(rec_id, path, kind="preview", duration_s=round(pcm.size / sr, 3))
        return rec_id

    async def _wait_for_weights(self, family: str, progress: Progress | None, start: float) -> bool:
        """True once `family`'s weights are on disk (waiting for a download in progress, with its progress shown)."""
        missing = self.missing_weights(family)
        if missing is None:
            return True
        mm = self.e.model_manager
        running = mm._by_model.get(missing.model)  # noqa: SLF001 — the manager's running job for that variant
        if running is None or running.state != "running" or running.task is None:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + INSTALL_WAIT_S
        while not running.task.done() and loop.time() < deadline:
            if progress is not None:
                progress(start, t("clone.step.downloading", pct=f"{running.pct:.0f}"))
            await asyncio.wait({running.task}, timeout=1.0)
        return self.missing_weights(family) is None

    # ------------------------------------------------------------------ changing and deleting

    async def patch(self, slug: str, body: s.ClonePatch) -> s.CloneOut:
        rec = self.store.get(slug)
        if rec is None:
            raise NotFound(t("what.clone"))
        changes: dict[str, Any] = {}
        if body.name is not None:
            name = self._clean_name(body.name)
            if not name:
                raise EngineError("invalid_request", t("clone.reject.empty_name"))
            changes["name"] = name
        if body.language is not None:
            changes["language"] = P.base_language(body.language)
        if body.ref_text is not None and body.ref_text.strip() != rec.ref_text:
            text = body.ref_text.strip()
            if not text:
                raise EngineError("invalid_request", t("clone.reject.need_text"))
            ref = self.store.reference_path(slug)
            changes["ref_text"] = text
            changes["ref_sha256"] = ref_hash(ref.read_bytes() if ref.exists() else b"", text, rec.processing_version)
            changes["check"] = {}
        updated = self.store.update(slug, **changes) or rec
        if "ref_sha256" in changes:
            self.evict_audio(slug)
        self._voices_changed()
        return self.out(updated)

    async def delete(self, slug: str) -> s.CloneOut:
        rec = self.store.get(slug)
        if rec is None:
            raise NotFound(t("what.clone"))
        out = self.out(rec)
        async with self._lock(slug):
            evicted = self.evict_audio(slug)
            await self.reset_settings(f"{QWEN3}:{CLONE_MODEL}:{slug}")
            self.store.delete(slug)
        log.info("deleted cloned voice %s (%d cached sentences)", slug, evicted)
        self._voices_changed()
        return out

    def evict_audio(self, slug: str) -> int:
        """Remove every cached sentence this voice said (its reference changed or the voice is gone)."""
        return self.e.audio_cache.delete_voice(QWEN3, CLONE_MODEL, slug)

    async def reset_settings(self, voice_id: str) -> None:
        """Nothing keeps pointing at a voice that is about to go: defaults go back to "auto" (the local voice for
        `local_voice`), per-language choices and per-document voices are dropped."""
        d = self.e.settings.get().defaults
        if d.voice == voice_id:
            await self.e.settings.set_key("defaults.voice", AUTO_VOICE)
        if d.assistant_voice == voice_id:
            await self.e.settings.set_key("defaults.assistant_voice", AUTO_VOICE)
        if d.local_voice == voice_id:
            await self.e.settings.set_key("defaults.local_voice", DEFAULT_LOCAL_VOICE)
        if voice_id in d.voices.values():
            await self.e.settings.set_key("defaults.voices", {k: v for k, v in d.voices.items() if v != voice_id})
        rows = self.e.db.query("SELECT id, settings_json FROM documents WHERE settings_json LIKE ?",
                               (f"%{voice_id}%",))
        for row in rows:
            try:
                settings = json.loads(row["settings_json"] or "{}")
            except ValueError:
                continue
            if settings.get("voice_id") == voice_id:
                settings.pop("voice_id", None)
                self.e.db.execute("UPDATE documents SET settings_json=? WHERE id=?", (json.dumps(settings), row["id"]))
        doc = self.e.playback.current_document_id()
        if doc is not None and self.e.playback.state().voice_id == voice_id:
            with contextlib.suppress(EngineError):
                await self.e.transport.set_voice(self.e.documents.voice_for(doc))

    async def keep(self, slug: str, *, make_default: bool = True) -> VoiceRecord:
        rec = self.store.update(slug, kept=True)
        if rec is None:
            raise NotFound(t("what.clone"))
        self._voices_changed()
        if make_default:
            await self.e.playback.set_voice(s.VoiceRequest(voice_id=f"{QWEN3}:{CLONE_MODEL}:{slug}", scope="default"))
        return rec

    async def discard(self, slug: str) -> None:
        rec = self.store.get(slug)
        if rec is not None and not rec.kept:
            self.evict_audio(slug)
            self.store.delete(slug)

    def _voices_changed(self) -> None:
        from open_speech.api.ws_schema import CapabilitiesChanged

        self.e.events.publish(CapabilitiesChanged(capabilities=self.e.capabilities()))

    # ------------------------------------------------------------------ designed voices

    async def design(self, description: str, *, name: str | None = None, language: str | None = None,
                     session_id: str | None = None, conversation_id: str | None = None) -> s.DesignAccepted:
        description = re.sub(r"\s+", " ", description).strip()
        if len(description) < 3:
            raise EngineError("invalid_request", t("design.reject.describe"))
        reason = self.available()
        if reason:
            raise EngineError("unavailable", reason, provider=QWEN3)
        lang = P.base_language(language or resolve_language(self.e.settings.get().language))
        nm = self._clean_name(name) or name_from_description(description)
        job = self.e.jobs.start("design", lambda progress: self._design_job(progress, description, nm, lang),
                                label=t("design.job"))
        if session_id is not None:
            self.e.assistant.task_turn(session_id=session_id, conversation_id=conversation_id,
                                       work=lambda turn: self._turn_for_design(turn, job))
        return s.DesignAccepted(job_id=job.id)

    async def _design_job(self, progress: Progress, description: str, name: str, language: str) -> dict[str, Any]:
        if not await self._wait_for_weights(FAMILY_DESIGN, progress, 2):
            missing = self.missing_weights(FAMILY_DESIGN)
            raise EngineError("unavailable", t("design.reject.download", size=size_label(missing.size_mb))
                              if missing else t("unavailable.voice"), provider=QWEN3)
        q = self.e.registry.qwen3()
        assert q is not None
        text = P.design_text(language)
        job_id = progress.job.id
        folder = self.store.root / ".design" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        base_seed = int(sha256_hex("design", description, language)[:8], 16)
        found: list[Candidate] = []
        tries = 0
        while len(found) < DESIGN_CANDIDATES and tries < DESIGN_TRIES:
            seed = (base_seed + 7919 * tries) & 0x7FFFFFFF
            tries += 1
            progress(5 + 85 * len(found) / DESIGN_CANDIDATES, t("design.step", n=len(found) + 1,
                                                                 total=DESIGN_CANDIDATES))
            pcm = await q.design(text, description, language=language, seed=seed)
            if pcm.size < R.TARGET_SR:  # under a second: nothing usable
                continue
            heard = await self.transcribe(resample(pcm, R.TARGET_SR, R.VAD_SR))
            error_rate = wer(text, heard) if heard is not None else None
            if error_rate is not None and error_rate > DESIGN_MAX_WER and tries < DESIGN_TRIES:
                log.info("designed candidate seed %d garbled the passage (WER %.2f); another seed", seed, error_rate)
                continue
            path = folder / f"candidate-{len(found) + 1}.wav"
            await asyncio.to_thread(R.write_wav, path, pcm)
            rec_id = await self._save_recording(pcm, R.TARGET_SR)
            cand = Candidate(id=new_id("cand"), job_id=job_id, index=len(found) + 1, seed=seed, path=path,
                             recording_id=rec_id, wer=round(error_rate, 3) if error_rate is not None else None,
                             description=description, language=language, name=name)
            self._candidates[cand.id] = cand
            found.append(cand)
        if not found:
            raise EngineError("provider_error", t("design.reject.failed"))
        self._save_candidates(job_id)
        progress(100, t("design.done"))
        return {"candidates": [{"candidate_id": c.id, "index": c.index, "recording_id": c.recording_id,
                                "wer": c.wer, "seed": c.seed} for c in found],
                "description": description, "name": name, "language": language, "text": text}

    async def keep_candidate(self, candidate_id: str, *, name: str | None = None,
                             make_default: bool = False) -> s.CloneOut:
        """Keep a designed candidate: it becomes a voice whose reference is that candidate (`source = "design"`)."""
        cand = self._candidates.get(candidate_id) or self._load_candidate(candidate_id)
        if cand is None or not cand.path.exists():
            raise NotFound(t("what.candidate"))
        nm = self.store.unique_name(self._clean_name(name) or cand.name)
        slug = self.store.new_slug(nm)
        staging = Path(tempfile.mkdtemp(prefix="source-", dir=self.store.root))
        src = staging / "source.wav"
        shutil.copyfile(cand.path, src)
        plan = _ClonePlan(slug=slug, name=nm, language=cand.language, source="design", passage=None,
                          typed_text=P.design_text(cand.language), keep=True,
                          consent={"text_version": P.CONSENT_VERSION, "accepted_at": now_iso()},
                          src=src, staging=staging, folder=self.store.folder(slug), replace=False,
                          design_prompt=cand.description)
        job = self.e.jobs.start("clone", lambda progress: self._process(plan, progress), label=t("clone.job"))
        await self.e.jobs.wait(job.id)
        if job.state != "done":
            raise EngineError("invalid_request", (job.error or {}).get("detail") or t("design.reject.failed"))
        for other in [c for c in self._candidates.values() if c.job_id == cand.job_id]:
            self._candidates.pop(other.id, None)
        shutil.rmtree(cand.path.parent, ignore_errors=True)
        self._voices_changed()
        if make_default:
            await self.e.playback.set_voice(s.VoiceRequest(voice_id=f"{QWEN3}:{CLONE_MODEL}:{slug}", scope="default"))
        return self.get(slug)

    def _save_candidates(self, job_id: str) -> None:
        """Candidates outlive an engine restart for a day ("keep the second one" after the app was reopened)."""
        folder = self.store.root / ".design" / job_id
        data = [{"id": c.id, "index": c.index, "seed": c.seed, "path": c.path.name, "recording_id": c.recording_id,
                 "wer": c.wer, "description": c.description, "language": c.language, "name": c.name}
                for c in self.candidates_of(job_id)]
        with contextlib.suppress(OSError):
            (folder / "candidates.json").write_text(json.dumps(data), "utf-8")

    def _load_candidate(self, candidate_id: str) -> Candidate | None:
        root = self.store.root / ".design"
        if not root.is_dir():
            return None
        for meta in root.glob("*/candidates.json"):
            try:
                items = json.loads(meta.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            for item in items:
                cand = Candidate(id=item["id"], job_id=meta.parent.name, index=int(item["index"]),
                                 seed=int(item["seed"]), path=meta.parent / item["path"],
                                 recording_id=str(item["recording_id"]), wer=item.get("wer"),
                                 description=str(item["description"]), language=str(item["language"]),
                                 name=str(item["name"]))
                self._candidates.setdefault(cand.id, cand)
        return self._candidates.get(candidate_id)

    def _prune_designs(self, older_than_s: float = 86400.0) -> None:
        root = self.store.root / ".design"
        if not root.is_dir():
            return
        now = datetime.now(UTC).timestamp()
        for d in root.iterdir():
            with contextlib.suppress(OSError):
                if now - d.stat().st_mtime > older_than_s:
                    shutil.rmtree(d, ignore_errors=True)

    def candidates_of(self, job_id: str) -> list[Candidate]:
        return sorted((c for c in self._candidates.values() if c.job_id == job_id), key=lambda c: c.index)

    def discard_candidates(self, job_id: str) -> None:
        for c in self.candidates_of(job_id):
            self._candidates.pop(c.id, None)
        shutil.rmtree(self.store.root / ".design" / job_id, ignore_errors=True)

    # ------------------------------------------------------------------ the conversation (07 § Fast path)

    def start_clone(self, turn: Turn, *, name: str | None = None) -> Reply:
        """"Clone my voice": the passage card (or first, the download card when the cloning weights are missing)."""
        reason = self.available()
        if reason:
            return Reply(reason)
        missing = self.missing_weights(FAMILY_CLONE)
        if missing is not None and not self._installing(missing.model):
            size = say.size(int(missing.size_mb * 1024 * 1024))
            ask = t("ask.clone_download", size=size)
            self.e.assistant.create_action(
                kind=CLONE_DOWNLOAD, summary=t("action.clone_download", size=size_label(missing.size_mb)),
                spoken=ask, turn_id=turn.id,
                payload={"model_id": missing.model, "name": name, "size": size_label(missing.size_mb),
                         "via_turn": True},
            )
            return Reply(ask)
        return self.record_card(turn, name=name)

    def record_card(self, turn: Turn, *, name: str | None = None, intro: str | None = None,
                    after: str | None = None) -> Reply:
        lang = resolve_language(self.e.settings.get().language)
        passage = P.next_passage(lang, after)
        ask = (intro + " " if intro else "") + t("ask.clone_record")
        self.e.assistant.create_action(
            kind=CLONE_RECORD, summary=t("action.clone_record"), spoken=ask, turn_id=turn.id,
            payload={"passage_id": passage.id, "passage": passage.text, "language": passage.language,
                     "name": name or self.store.unique_name(t("clone.default_name")), "consent": t("clone.consent"),
                     "consent_version": P.CONSENT_VERSION},
        )
        return Reply(ask)

    def start_file_clone(self, turn: Turn, paths: list[str], *, name: str | None = None) -> Reply:
        """"Clone this voice" with an audio file attached: consent on a card first."""
        reason = self.available()
        if reason:
            return Reply(reason)
        audio = [p for p in paths if is_audio_path(p)]
        if not audio:
            return Reply(t("reply.clone_no_audio"))
        path = Path(audio[0]).expanduser()
        missing = self.missing_weights(FAMILY_CLONE)
        size = say.size(int(missing.size_mb * 1024 * 1024)) if missing is not None else None
        ask = t("ask.clone_file", name=path.name) + (" " + t("ask.clone_file_download", size=size) if size else "")
        self.e.assistant.create_action(
            kind=CLONE_FILE, summary=t("action.clone_file", name=path.name), spoken=ask, turn_id=turn.id,
            payload={"path": str(path), "name": self._clean_name(name) or name_from_file(path),
                     "consent": t("clone.consent"), "consent_version": P.CONSENT_VERSION,
                     "model_id": missing.model if missing is not None else None,
                     "size": size_label(missing.size_mb) if missing is not None else None, "via_turn": True},
        )
        return Reply(ask)

    def start_design(self, turn: Turn, description: str, *, name: str | None = None) -> Reply | None:
        """"Make a voice that sounds like …": renders at once when the weights are here (returns None: the caller
        runs `design_in_turn`); otherwise a card with the download size first."""
        reason = self.available()
        if reason:
            return Reply(reason)
        missing = [m for m in (self.missing_weights(FAMILY_DESIGN), self.missing_weights(FAMILY_CLONE)) if m]
        if not missing:
            return None
        total = sum(m.size_mb for m in missing)
        size = say.size(int(total * 1024 * 1024))
        ask = t("ask.design_download", size=size)
        self.e.assistant.create_action(
            kind=DESIGN_VOICE, summary=t("action.design_download", size=size_label(total)), spoken=ask,
            turn_id=turn.id,
            payload={"description": description, "name": name, "model_ids": [m.model for m in missing],
                     "size": size_label(total), "via_turn": True},
        )
        return Reply(ask)

    def propose_design(self, turn_id: str, description: str, *, name: str | None = None) -> str:
        """The agent's way in: a card that makes three voices from `description` when confirmed."""
        missing = [m for m in (self.missing_weights(FAMILY_DESIGN), self.missing_weights(FAMILY_CLONE)) if m]
        total = sum(m.size_mb for m in missing)
        summary = t("action.design", description=description[:80]) + (
            " · " + t("action.download_size", size=size_label(total)) if missing else "")
        return self.e.assistant.create_action(
            kind=DESIGN_VOICE, summary=summary, turn_id=turn_id,
            payload={"description": description, "name": name, "model_ids": [m.model for m in missing],
                     "size": size_label(total) if missing else None, "via_turn": True},
        )

    def propose_delete(self, turn_id: str, name: str) -> tuple[str | None, str]:
        rec = self.store.find(name)
        if rec is None:
            return None, t("reply.no_clone", name=name)
        ask = t("ask.delete_voice", name=rec.name)
        aid = self.e.assistant.create_action(
            kind=DELETE_VOICE, summary=t("action.delete_voice", name=rec.name), spoken=ask, turn_id=turn_id,
            payload={"slug": rec.slug, "name": rec.name, "via_turn": True},
        )
        return aid, ask

    async def rename(self, name: str, new_name: str) -> str:
        rec = self.store.find(name)
        if rec is None:
            return t("reply.no_clone", name=name)
        await self.patch(rec.slug, s.ClonePatch(name=new_name))
        return t("reply.clone_renamed", old=rec.name, new=self._clean_name(new_name) or new_name)

    def _installing(self, model_id: str) -> bool:
        job = self.e.model_manager._by_model.get(model_id)  # noqa: SLF001
        return job is not None and job.state == "running"

    def _install(self, model_id: str | None) -> None:
        if model_id and not self._installing(model_id):
            self.e.model_manager.start_install(model_id)

    # ---- answering the cards

    async def confirm(self, action: s.ActionOut, turn: Turn | None) -> Reply:
        """"Yes" (or the card's button) on one of this service's cards. Long ones (a file to clone, voices to
        design) run inside `turn`, so the result is that turn's reply."""
        payload = self._payload(action.action_id)
        kind = action.kind
        a = self.e.assistant
        if kind == CLONE_RECORD:
            missing = self.missing_weights(FAMILY_CLONE)
            self._install(missing.model if missing is not None else None)
            a.mark_action(action.action_id, "confirmed", t("done.clone_go"))
            return Reply(t("done.clone_go"))
        if kind == CLONE_DOWNLOAD:
            self._install(payload.get("model_id"))
            a.mark_action(action.action_id, "confirmed", t("done.installing"))
            if turn is None:
                return Reply(t("done.clone_downloading"))
            return self.record_card(turn, name=payload.get("name"), intro=t("done.clone_downloading"))
        if kind == CLONE_FILE:
            a.mark_action(action.action_id, "confirmed", t("done.generic"))
            self._install(payload.get("model_id"))
            path = Path(str(payload["path"]))
            accepted = await self.create(path=path, filename=path.name, name=payload.get("name"), consent=True,
                                         source="file", keep=False)
            job = self.e.jobs.get(accepted.job_id)
            assert job is not None
            if turn is None:
                await self.e.jobs.wait(job.id)
                return Reply((job.error or {}).get("detail") or t("done.clone_made", name=payload.get("name") or ""))
            return await self._turn_for_job(turn, job, None)
        if kind == KEEP_CLONE:
            slug = str(payload["slug"])
            try:
                await self.keep(slug, make_default=True)
            except EngineError as exc:
                a.mark_action(action.action_id, "failed", exc.detail)
                return Reply(exc.detail)
            msg = t("done.clone_kept")
            a.mark_action(action.action_id, "confirmed", msg)
            return Reply(msg)
        if kind == DESIGN_VOICE:
            for model_id in payload.get("model_ids") or []:
                self._install(model_id)
            a.mark_action(action.action_id, "confirmed", t("done.generic"))
            designing = await self.design(str(payload["description"]), name=payload.get("name"))
            job = self.e.jobs.get(designing.job_id)
            assert job is not None
            if turn is None:
                await self.e.jobs.wait(job.id)
                return Reply((job.error or {}).get("detail") or t("design.done"))
            return await self._turn_for_design(turn, job)
        if kind == KEEP_DESIGN:
            return Reply(t("ask.design_which", n=len(payload.get("candidates") or [])))
        if kind == DELETE_VOICE:
            try:
                out = await self.delete(str(payload["slug"]))
            except EngineError as exc:
                a.mark_action(action.action_id, "failed", exc.detail)
                return Reply(exc.detail)
            msg = t("done.voice_deleted", name=out.name)
            a.mark_action(action.action_id, "confirmed", msg)
            return Reply(msg)
        raise EngineError("invalid_request", f"Unknown action {kind}.")

    async def cancel(self, action: s.ActionOut) -> Reply:
        payload = self._payload(action.action_id)
        msg = t("done.cancelled")
        if action.kind == KEEP_CLONE:
            await self.discard(str(payload.get("slug")))
            msg = t("done.clone_discarded")
        elif action.kind == KEEP_DESIGN:
            self.discard_candidates(str(payload.get("job_id")))
            msg = t("done.design_discarded")
        self.e.assistant.mark_action(action.action_id, "cancelled", msg)
        return Reply(msg)

    async def choose(self, action: s.ActionOut, index: int) -> Reply:
        """"The second one": keep that designed candidate as a voice and read with it."""
        payload = self._payload(action.action_id)
        cands = payload.get("candidates") or []
        if not 1 <= index <= len(cands):
            return Reply(t("ask.design_which", n=len(cands)))
        cid = str(cands[index - 1]["candidate_id"])
        try:
            out = await self.keep_candidate(cid, make_default=True)
        except EngineError as exc:
            self.e.assistant.mark_action(action.action_id, "failed", exc.detail)
            return Reply(exc.detail)
        self.discard_candidates(str(payload.get("job_id")))
        msg = t("done.design_kept", name=out.name)
        self.e.assistant.mark_action(action.action_id, "confirmed", msg)
        return Reply(msg)

    def _payload(self, action_id: str) -> dict[str, Any]:
        row = self.e.db.one("SELECT payload_json FROM actions WHERE id=?", (action_id,))
        if row is None:
            raise NotFound(t("what.action"))
        data: dict[str, Any] = json.loads(row["payload_json"] or "{}")
        return data

    # ---- results as turns

    def _interim(self, turn: Turn, text: str) -> None:
        """Say something while the job runs ("One moment …"); the turn's final text replaces it on screen."""
        if turn.stream is not None:
            turn.stream.feed(text)
            turn.stream.insert("")  # cut it here, so it is said now
        self.e.events.publish(AssistantDelta(turn_id=turn.id, text=text + " "))

    async def _turn_for_job(self, turn: Turn, job: Job, plan: _ClonePlan | None) -> Reply:
        """The clone job's result as this turn's reply: the check sentence in the new voice, then "Keep it?" on a
        card; or why it didn't work, and the passage card again for another try."""
        self._interim(turn, t("clone.working"))
        await self.e.jobs.wait(job.id)
        if job.state != "done":
            detail = (job.error or {}).get("detail") or t("turn.failed")
            if plan is not None and plan.passage is not None:  # a recording: offer another try right away
                retry = self.record_card(turn, name=plan.name, after=plan.passage.id)
                return Reply(detail + " " + retry.text, error=None)
            return Reply(detail)
        res = job.result
        slug, name = str(res["slug"]), str(res["name"])
        check = res.get("check") or {}
        grade = check.get("grade")
        if res.get("kept"):
            return Reply(t("done.clone_made", name=name))
        if grade is None:  # no cloning weights yet: kept for later, nothing to hear
            self.store.update(slug, kept=True)
            self._voices_changed()
            return Reply(t("reply.clone_saved_unchecked", name=name, note=check.get("note") or ""))
        question = t({"good": "ask.keep_clone", "fair": "ask.keep_clone_fair"}.get(grade, "ask.keep_clone_poor"))
        warnings = [w for w in res.get("warnings") or [] if w]
        shown = f"{check.get('text') or ''} {question}".strip()
        self.e.assistant.create_action(
            kind=KEEP_CLONE, summary=t("action.keep_clone", name=name), spoken=question, turn_id=turn.id,
            payload={"slug": slug, "name": name, "grade": grade, "wer": check.get("wer"),
                     "recording_id": check.get("recording_id"), "warnings": warnings, "via_turn": True},
        )
        parts: list[Any] = []
        if check.get("recording_id"):
            with contextlib.suppress(EngineError, OSError):
                from open_speech.providers.pcm import read_audio_file

                pcm, sr = await asyncio.to_thread(read_audio_file, self.e.recordings.path(check["recording_id"]))
                parts.append((pcm, sr))
        parts.append(question)
        return Reply(shown, parts=parts)

    async def _turn_for_design(self, turn: Turn, job: Job) -> Reply:
        self._interim(turn, t("design.working"))
        await self.e.jobs.wait(job.id)
        if job.state != "done":
            return Reply((job.error or {}).get("detail") or t("design.reject.failed"))
        res = job.result
        cands = list(res.get("candidates") or [])
        rows = [
            CardRow(id=c["candidate_id"], title=t("design.candidate", n=c["index"]),
                    data={"recording_id": c["recording_id"], "candidate_id": c["candidate_id"], "index": c["index"],
                          "wer": c.get("wer"), "command": t("design.keep_command", n=c["index"])})
            for c in cands
        ]
        self.e.events.publish(AssistantCard(turn_id=turn.id, kind="design", title=t("card.design", name=res["name"]),
                                            rows=rows, data={"description": res["description"], "job_id": job.id}))
        question = t("ask.design_which", n=len(cands))
        self.e.assistant.create_action(
            kind=KEEP_DESIGN, summary=t("action.keep_design", name=res["name"]), spoken=question, turn_id=turn.id,
            payload={"job_id": job.id, "candidates": cands, "name": res["name"], "via_turn": True},
        )
        parts: list[Any] = []
        for c in cands:
            parts.append(t("design.say_candidate", n=c["index"]))
            cand = self._candidates.get(c["candidate_id"])
            if cand is not None:
                from open_speech.providers.pcm import read_audio_file

                with contextlib.suppress(OSError):
                    pcm, sr = await asyncio.to_thread(read_audio_file, cand.path)
                    parts.append((pcm, sr))
        parts.append(question)
        shown = t("design.shown", n=len(cands)) + " " + question
        return Reply(shown, parts=parts)

    async def design_in_turn(self, turn: Turn, description: str, *, name: str | None = None) -> Reply:
        accepted = await self.design(description, name=name)
        job = self.e.jobs.get(accepted.job_id)
        assert job is not None
        return await self._turn_for_design(turn, job)


@dataclass
class _ClonePlan:
    slug: str
    name: str
    language: str
    source: str
    passage: P.Passage | None
    typed_text: str | None
    keep: bool
    consent: dict[str, Any]
    src: Path
    staging: Path
    folder: Path
    replace: bool
    design_prompt: str | None = None


def rejection_text(code: str, params: dict[str, Any], *, warning: bool = False) -> str:
    """A reason code from voices/reference.py, in words the user can act on."""
    key = f"clone.{'warn' if warning else 'reject'}.{code}"
    try:
        return t(key, **{k: (f"{v:g}" if isinstance(v, float) else v) for k, v in params.items()})
    except KeyError:
        return t("clone.reject.unusable")


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


_READY = re.compile(
    r"^(?:ok(?:ay)?[, ]+)?(?:i'?m ready|ready|start|start recording|record|go|let'?s go|go ahead|begin|"
    r"listo|lista|estoy list[oa]|empieza|empezar|comienza|graba|adelante|vale|ya)[.!]?$"
)
_ORDINALS = {"first": 1, "second": 2, "third": 3, "primera": 1, "primero": 1, "segunda": 2, "segundo": 2,
             "tercera": 3, "tercero": 3}  # fmt: skip
_CARDINALS = {"one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "uno": 1, "una": 1, "dos": 2, "tres": 3}


def is_ready(words: str) -> bool:
    return bool(_READY.match(fold(words).strip()))


def chosen_index(words: str) -> int | None:
    """"the second one", "keep two", "number 3", "la segunda", "me quedo con la uno" → 2, 2, 3, 2, 1."""
    w = re.sub(r"[^\w\s]", " ", fold(words)).split()
    if not w or len(w) > 8:
        return None
    # "the second one": an ordinal wins over a number word ("one" is a pronoun there).
    hits = [_ORDINALS[x] for x in w if x in _ORDINALS] or [_CARDINALS[x] for x in w if x in _CARDINALS]
    return hits[-1] if len(set(hits)) == 1 else None


__all__ = [
    "ACTION_KINDS",
    "CloneService",
    "chosen_index",
    "grade_for",
    "is_audio_path",
    "is_ready",
    "rejection_text",
]
