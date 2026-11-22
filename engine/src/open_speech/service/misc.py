"""Voices, usage, keys, local models and recordings use cases (04)."""

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
from open_speech.ids import new_id

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

    def find_by_name(self, name: str) -> s.Voice | None:  # type: ignore[name-defined]
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
            raise NotFound(f"the voice {voice_id}")
        if not voice.available:
            raise EngineError("missing_key", voice.unavailable_reason or f"{voice.name} isn't available.")
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


class KeyService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def state(self) -> s.KeysState:
        ks = self.e.keystore
        return s.KeysState(anthropic=ks.status("anthropic"), openai=ks.status("openai"), gemini=ks.status("gemini"))

    async def put(self, provider: str, key: str) -> s.KeyTestResult:
        key = key.strip()
        if provider == "auto":
            detected = self.e.keystore.detect(key)
            if detected is None:
                return s.KeyTestResult(
                    ok=False,
                    provider="unknown",
                    detail="I don't recognise that key. Anthropic keys start with sk-ant-, OpenAI with sk-, Gemini with AIza.",
                )
            provider = detected
        ok, detail = await self.e.keystore.set(provider, key)
        if ok:
            self.e.registry.refresh()
        return s.KeyTestResult(ok=ok, provider=provider, detail=detail)


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
            raise NotFound("that recording")
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
