"""Voices, usage, local models and recordings use cases (04)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from open_speech.api import schemas as s
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import resolve_language, t
from open_speech.ids import new_id
from open_speech.providers.base import Voice

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

PREVIEW_TEXT = "Hi, I'm {name}. This is how I sound when I read to you."


class VoiceService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def list(self, *, provider: str | None = None, lang: str | None = None) -> s.VoiceList:
        voices = self.e.registry.voices()
        if provider:
            voices = [v for v in voices if v.provider == provider]
        if lang:
            lang_l = lang.lower()
            voices = [v for v in voices if not v.languages or any(x.lower().startswith(lang_l) for x in v.languages)]
        return s.VoiceList(items=voices)

    # ------------------------------------------------------------------ languages

    @staticmethod
    def speaks(voice: Voice, language: str) -> bool:
        """A voice with no declared languages is multilingual (cloud voices follow the text)."""
        return not voice.languages or any(x.lower().split("-")[0] == language for x in voice.languages)

    def for_language(self, preferred: str, language: str | None) -> str:
        """`preferred` when it can pronounce `language`; otherwise the voice the user chose for that language,
        or the first one that speaks it (local voices first). Unknown language: `preferred`."""
        if not language:
            return preferred
        voices = {v.id: v for v in self.e.registry.voices() if v.available}
        current = voices.get(preferred)
        if current is not None and self.speaks(current, language):
            return preferred
        chosen = self.e.settings.get().defaults.voices.get(language)
        if chosen in voices and self.speaks(voices[chosen], language):
            return str(chosen)
        native = [v for v in voices.values() if v.languages and self.speaks(v, language)]
        native.sort(key=lambda v: not v.local)
        return native[0].id if native else preferred

    def assistant_voice(self) -> str:
        """The voice replies are spoken in: the assistant voice, in the language the app is set to."""
        settings = self.e.settings.get()
        voices = {v.id: v for v in self.e.registry.voices() if v.available}
        voice = settings.defaults.assistant_voice
        if voice not in voices:
            voice = self.e.registry.default_local_voice()
        return self.for_language(voice, resolve_language(settings.language))

    async def set_default(self, voice_id: str) -> None:
        """Make a voice the default: for every language it speaks, and as the main voice when it speaks the
        app's language (so choosing a Spanish voice while reading Spanish doesn't break English reading)."""
        voice = self.e.registry.voice(voice_id)
        settings = self.e.settings.get()
        language = resolve_language(settings.language)
        per_language = dict(settings.defaults.voices)
        for code in {x.lower().split("-")[0] for x in voice.languages}:
            per_language[code] = voice_id
        patch: dict[str, Any] = {"voices": per_language}
        if self.speaks(voice, language):
            patch["voice"] = voice_id
        await self.e.settings.patch({"defaults": patch})

    def find_by_name(self, name: str) -> Voice | None:
        n = name.lower().replace(" ", "").replace("-", "").replace("_", "")
        candidates = [v for v in self.e.registry.voices() if v.available]
        for v in candidates:
            if v.name.lower().replace(" ", "") == n or v.id.split(":")[-1].lower().replace("_", "") == n:
                return v
        for v in candidates:
            if n in v.name.lower().replace(" ", "") or n in v.id.lower():
                return v
        return None

    async def preview(self, voice_id: str, *, style: str | None = None, play: bool = True) -> s.PreviewResponse:
        voice = next((v for v in self.e.registry.voices() if v.id == voice_id), None)
        if voice is None:
            raise NotFound(t("what.voice"))
        if not voice.available:
            raise EngineError("unavailable", voice.unavailable_reason or f"{voice.name} isn't available.")
        res = await self.e.synth.synth(
            PREVIEW_TEXT.format(name=voice.name.split(" (")[0]), voice_id, style=style, op="preview"
        )
        rec_id = self.e.recordings.register(res.audio.path, kind="preview", duration_s=res.audio.duration_s, copy=False)
        if play:
            await self.e.transport.play_clip(res.audio, duck=True)
        return s.PreviewResponse(recording_id=rec_id, cached=res.cache_hit, usd_estimate=str(res.usd))


class UsageService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def summary(
        self, *, period: Literal["today", "week", "month"], group_by: Literal["provider", "op", "document"]
    ) -> s.UsageSummary:
        return self.e.ledger.summary(period, group_by, budget_usd=Decimal(self.e.settings.get().budget.monthly_usd))

    def ledger(self, **kw: Any) -> s.LedgerPage:
        return self.e.ledger.rows(**kw)

    async def estimate(self, req: s.EstimateRequest) -> s.Estimate:
        from open_speech.usage.estimate import estimate_read

        settings = self.e.settings.get()
        if req.document_id:
            doc = self.e.documents.load(req.document_id)
            script = self.e.documents._scripts.get(doc.id)
            mode = req.mode or self.e.documents.mode_for(doc)
            voice_id = req.voice_id or self.e.documents.voice_for(doc.id)
        elif req.text:
            from open_speech.ingest import SourceInput, ingest

            res = await ingest(SourceInput(kind="text", text=req.text), extract_cache=self.e.extract_cache)
            doc, script = res.document, None
            mode = req.mode or settings.defaults.mode
            voice_id = req.voice_id or settings.defaults.voice
        else:
            raise EngineError("invalid_request", "Send a document_id or text to estimate.")
        voice = next((v for v in self.e.registry.voices() if v.id == voice_id), None)
        if voice is None:
            raise NotFound(f"the voice {voice_id}")
        est = estimate_read(
            doc,
            script,
            voice=voice,
            mode=mode,
            llm_model=settings.llm.model,
            pricing=self.e.pricing,
            audio_cache=self.e.audio_cache,
        )
        threshold = Decimal(settings.budget.estimate_threshold_usd)
        est.requires_confirmation = Decimal(est.usd_estimate) > threshold
        return est

    def budget(self) -> s.BudgetState:
        b = self.e.settings.get().budget
        spent = self.e.ledger.month_spent()
        monthly = Decimal(b.monthly_usd)
        pct = float(spent / monthly) if monthly > 0 else 0.0
        return s.BudgetState(
            monthly_usd=str(monthly),
            warn_at=b.warn_at,
            hard_stop=b.hard_stop,
            spent_usd=str(spent.quantize(Decimal("0.0001"))),
            pct=round(pct, 4),
        )

    async def set_budget(self, u: s.BudgetUpdate) -> s.BudgetState:
        patch = {k: v for k, v in u.model_dump(exclude_none=True).items()}
        if patch:
            await self.e.settings.patch({"budget": patch})
        return self.budget()


class ModelService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def list(self) -> s.LocalModelList:
        return s.LocalModelList(items=self.e.model_manager.list())

    async def install(self, model_id: str) -> s.JobAccepted:
        job_id = self.e.model_manager.start_install(model_id)
        return s.JobAccepted(job_id=job_id)

    async def remove(self, model_id: str) -> None:
        self.e.model_manager.remove(model_id)
        self.e.registry.refresh()


class RecordingService:
    """User utterances, assistant replies and previews as Ogg/Opus (07 § Recordings, FR-SETUP-8)."""

    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def register(
        self, path: Path, *, kind: str, duration_s: float = 0.0, turn_id: str | None = None, copy: bool = True
    ) -> str:
        rec_id = new_id("rec")
        target = path
        if copy:
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            d = self.e.paths.recordings / day
            d.mkdir(parents=True, exist_ok=True)
            target = d / f"{rec_id}.ogg"
            if path != target:
                shutil.copyfile(path, target)
        self.e.db.execute(
            "INSERT INTO recordings(id, kind, path, duration_s, turn_id, created_at) VALUES(?,?,?,?,?,?)",
            (rec_id, kind, str(target), duration_s, turn_id, datetime.now(UTC).isoformat()),
        )
        return rec_id

    def new_path(self) -> tuple[str, Path]:
        rec_id = new_id("rec")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        d = self.e.paths.recordings / day
        d.mkdir(parents=True, exist_ok=True)
        return rec_id, d / f"{rec_id}.ogg"

    def insert(self, rec_id: str, path: Path, *, kind: str, duration_s: float, turn_id: str | None = None) -> None:
        self.e.db.execute(
            "INSERT INTO recordings(id, kind, path, duration_s, turn_id, created_at) VALUES(?,?,?,?,?,?)",
            (rec_id, kind, str(path), duration_s, turn_id, datetime.now(UTC).isoformat()),
        )

    def path(self, recording_id: str) -> Path:
        row = self.e.db.one("SELECT path FROM recordings WHERE id=?", (recording_id,))
        if row is None or not Path(row["path"]).exists():
            raise NotFound(t("what.recording"))
        return Path(row["path"])

    async def play(self, recording_id: str) -> s.PlaybackState:
        await self.e.transport.play_clip(self.path(recording_id), duck=True)
        return self.e.playback.state()

    def keep_recordings(self) -> bool:
        return self.e.settings.get().voice_input.recording_retention_days > 0

    def prune(self) -> int:
        days = self.e.settings.get().voice_input.recording_retention_days
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat() if days > 0 else datetime.now(UTC).isoformat()
        rows = self.e.db.query(
            "SELECT id, path, kind FROM recordings WHERE created_at < ? AND kind IN ('user','reply')", (cutoff,)
        )
        for r in rows:
            with contextlib.suppress(OSError):
                Path(r["path"]).unlink(missing_ok=True)
            self.e.db.execute("DELETE FROM recordings WHERE id=?", (r["id"],))
        return len(rows)

    async def prune_loop(self) -> None:
        while True:
            self.prune()
            await asyncio.sleep(3600)
