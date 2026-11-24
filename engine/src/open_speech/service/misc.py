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
from open_speech.config import AUTO_VOICE
from open_speech.errors import EngineError, NotFound
from open_speech.i18n import resolve_language, t
from open_speech.ids import new_id, split_voice_id
from open_speech.providers.base import Voice, cache_identity
from open_speech.providers.pcm import read_audio_file

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

PREVIEW_TEXT = "Hi, I'm {name}. This is how I sound when I read to you."
AUTO_WAIT_S = 1.5  # right after startup, how long "auto" may wait for the first Google check


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

    def app_language(self) -> str:
        return resolve_language(self.e.settings.get().language)

    # ------------------------------------------------------------------ "auto"

    def resolve(self, voice: str, language: str | None = None) -> tuple[str, str | None]:
        """A voice id that can be synthesized, for `voice` ("auto" or an id) in `language` (default: the app's), and
        why "auto" isn't the Google voice right now when the reason is worth telling the user.

        "auto" is Chirp Zephyr in the language's locale (en-US, es-US) while it can be used, else the local voice
        (`defaults.local_voice`, adjusted to the language):
        - structural reasons (no Google key in this build, Cloud Text-to-Speech not allowed for it, no Chirp voice
          for the language) → the local voice, silently: nothing changed and nothing can be done in the app;
        - runtime reasons → the local voice and a notice reason: "free_tier" (this month's free characters are
          used up and the user hasn't chosen to keep paying) or "budget" (they chose to pay, but the budget's hard
          stop was reached). Failures during synthesis (offline, a refusal) are the synthesizer's fallback.
        Any other voice id is returned as is."""
        if voice != AUTO_VOICE:
            return voice, None
        lang = language or self.app_language()
        cloud = self.e.registry.auto_cloud_voice(lang)
        local = self.e.registry.default_local_voice(lang)
        if cloud is None:
            return local, None
        guard = getattr(self.e, "free_tier", None)
        if guard is not None:
            provider, model, _ = split_voice_id(cloud)
            use = guard.use(provider, model)
            if use is not None and use.used >= use.stop_at:
                if use.policy == "local":
                    return local, "free_tier"
                if not self.e.budget.allow_paid():
                    return local, "budget"
        return cloud, None

    async def settle_auto(self) -> None:
        """Right after startup, wait (at most 1.5 s, usually ~0.3 s) for the first Google check before "auto"
        picks a voice. Until it answers, Chirp is assumed to work; with a key the API refuses, a read or reply
        started in that window would try Chirp, fail, and say "the Google voice isn't available". Waiting
        means the local voice from the first word, silently, and Chirp when it works: the voice never
        changes under the listener. Returns at once once the check has answered or when there is none."""
        check = self.e.registry.google_check()
        if check is not None and hasattr(check, "wait_first"):
            await check.wait_first(AUTO_WAIT_S)

    def effective(self, voice: str, language: str | None = None) -> str:
        """`resolve()` without the reason: the voice that will actually speak."""
        return self.resolve(voice, language)[0]

    @staticmethod
    def notice_text(reason: str) -> str:
        """What is said when "auto" reads with the local voice for a runtime reason (see `resolve`)."""
        target = t("notice.local_voice")
        return t("notice.free_tier" if reason == "free_tier" else "notice.budget", target=target)

    def for_language(self, preferred: str, language: str | None) -> str:
        """`preferred` when it can pronounce `language`; otherwise the voice the user chose for that language, the
        same speaker in another locale (a Chirp voice's `persona`: Zephyr in es-US for Spanish), or the first voice
        that speaks it (local voices first). Unknown language: `preferred`.

        "auto" is resolved for the language (Chirp Zephyr in that language's locale, or the local voice), except
        that a voice the user picked for a language other than the app's (`defaults.voices`) wins there."""
        if preferred == AUTO_VOICE:
            if language and language != self.app_language():
                chosen = self.e.settings.get().defaults.voices.get(language)
                if chosen and chosen != AUTO_VOICE:
                    voices = {v.id: v for v in self.e.registry.voices() if v.available}
                    if chosen in voices and self.speaks(voices[chosen], language):
                        return str(chosen)
            return self.effective(AUTO_VOICE, language)
        if not language:
            return preferred
        voices = {v.id: v for v in self.e.registry.voices() if v.available}
        current = voices.get(preferred)
        if current is not None and self.speaks(current, language):
            return preferred
        chosen = self.e.settings.get().defaults.voices.get(language)
        if chosen == AUTO_VOICE:
            return self.effective(AUTO_VOICE, language)
        if chosen in voices and self.speaks(voices[chosen], language):
            return str(chosen)
        if current is not None and current.persona:
            sibling = next(
                (v for v in voices.values() if v.persona == current.persona and self.speaks(v, language)), None
            )
            if sibling is not None:
                return sibling.id
        native = [v for v in voices.values() if v.languages and self.speaks(v, language)]
        native.sort(key=lambda v: not v.local)
        return native[0].id if native else preferred

    def assistant_voice(self) -> str:
        """The voice replies are spoken in: the assistant voice, in the language the app is set to."""
        return self.reply_voice(self.app_language())[0]

    def reply_voice(self, language: str | None = None) -> tuple[str, str | None]:
        """The voice a reply in `language` (default: the app's) is spoken in, and the notice reason when "auto"
        had to use the local voice for a runtime reason (see `resolve`)."""
        settings = self.e.settings.get()
        lang = language or self.app_language()
        voice = settings.defaults.assistant_voice
        if voice == AUTO_VOICE:
            resolved, reason = self.resolve(AUTO_VOICE, lang)
            return resolved, reason
        voices = {v.id: v for v in self.e.registry.voices() if v.available}
        if voice not in voices:
            voice = self.e.registry.default_local_voice(lang)
        return self.for_language(voice, lang), None

    def current_default(self) -> str:
        """The voice reading uses by default right now (the default voice, "auto" resolved)."""
        return self.effective(self.e.settings.get().defaults.voice)

    async def set_default(self, voice_id: str) -> None:
        """Make a voice the default: for every language it speaks, and as the main voice when it speaks the
        app's language (so choosing a Spanish voice while reading Spanish doesn't break English reading).
        "auto" goes back to the automatic choice for every language."""
        if voice_id == AUTO_VOICE:
            # Replaced, not merged: a per-language choice left behind (Dora for Spanish) would outrank "auto".
            await self.e.settings.set_key("defaults.voices", {})
            await self.e.settings.set_key("defaults.voice", AUTO_VOICE)
            return
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

    async def preview(
        self,
        voice_id: str,
        *,
        style: str | None = None,
        play: bool = True,
        turn_id: str | None = None,
        text: str | None = None,
    ) -> s.PreviewResponse:
        """A short sample of a voice. Played inside the reply of `turn_id` when that reply is being said (the
        assistant previewing while it talks: in order, never over its own words); otherwise it replaces whatever
        the engine is saying."""
        if voice_id == AUTO_VOICE:
            await self.settle_auto()
        voice_id = self.effective(voice_id)
        voice = next((v for v in self.e.registry.voices() if v.id == voice_id), None)
        if voice is None:
            raise NotFound(t("what.voice"))
        if not voice.available:
            raise EngineError("unavailable", voice.unavailable_reason or f"{voice.name} isn't available.")
        sample = (text or "").strip() or PREVIEW_TEXT.format(name=voice.name.split(" (")[0])
        res = await self.e.synth.synth(sample, voice_id, style=style, op="preview")
        rec_id = self.e.recordings.register(res.audio.path, kind="preview", duration_s=res.audio.duration_s, copy=False)
        if play:
            stream = self.e.reply_speech.active(turn_id) if turn_id is not None else None
            if stream is not None:
                x, sr = await asyncio.to_thread(read_audio_file, res.audio.path)
                stream.insert_audio(x, sr)
            else:
                await self.e.stop_speech(None, reason="superseded")
                await self.e.transport.play_clip(res.audio, duck=True, kind="preview")
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
            mode = self.e.documents.mode_for(doc)
            voice_id = req.voice_id or self.e.documents.voice_for(doc.id)
        elif req.text:
            from open_speech.ingest import SourceInput, ingest

            res = await ingest(SourceInput(kind="text", text=req.text), extract_cache=self.e.extract_cache)
            doc, script = res.document, None
            mode = self.e.documents.mode_for(doc)
            voice_id = self.e.voices.for_language(req.voice_id or settings.defaults.voice, doc.language)
        else:
            raise EngineError("invalid_request", "Send a document_id or text to estimate.")
        voice_id = self.e.voices.effective(voice_id, doc.language)
        voice = next((v for v in self.e.registry.voices() if v.id == voice_id), None)
        if voice is None:
            raise NotFound(f"the voice {voice_id}")
        try:
            prov, tmpl = self.e.registry.tts_for(voice.id)
            identity = cache_identity(prov, tmpl)
        except EngineError:
            identity = None
        free = self.e.free_tier.use(voice.provider, voice.model) if not voice.local else None
        est = estimate_read(
            doc,
            script,
            voice=voice,
            mode=mode,
            llm_model=settings.llm.model,
            pricing=self.e.pricing,
            audio_cache=self.e.audio_cache,
            model_identity=identity,
            free_remaining=free.remaining if free is not None else None,
            beyond_free=free.policy if free is not None else "local",
        )
        threshold = Decimal(settings.budget.estimate_threshold_usd)
        est.requires_confirmation = Decimal(est.usd_estimate) > threshold
        return est

    def budget(self) -> s.BudgetState:
        """Month-to-date spend against the budget, plus free allowances. Cheap (the month total is cached in
        the ledger), so the spend chip can paint from it and every `usage.changed` carries it."""
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
            free_tiers=self.e.free_tiers.states(),
        )

    async def set_budget(self, u: s.BudgetUpdate) -> s.BudgetState:
        patch = {k: v for k, v in u.model_dump(exclude_none=True).items()}
        if patch:
            await self.e.settings.patch({"budget": patch})
        return self.budget()


class ModelService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def list(self, *, all: bool = False) -> s.LocalModelList:  # noqa: A002
        return s.LocalModelList(items=self.e.model_manager.list(all=all, selected=self.selected_variants()))

    def selected_variants(self) -> set[str]:
        """The weight variants the settings choose (the Qwen voices' and the cloning model's), installed or not."""
        qwen = self.e.registry.qwen3()
        if qwen is None:
            return set()
        out = set()
        for family in ("customvoice", "base"):
            res = qwen.resolve(family)
            if res is not None:
                out.add(res.wanted.model)
        return out

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
        """Play a recording (the replay button on a turn): it replaces whatever the engine is saying."""
        path = self.path(recording_id)
        await self.e.stop_speech(None, reason="superseded")
        await self.e.transport.play_clip(path, duck=True, kind="recording", recording_id=recording_id)
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
