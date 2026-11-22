"""Documents use cases (04 § Documents; 05 §§ 1–4)."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from open_speech.api import schemas as s
from open_speech.api.ws_schema import DocumentUpdated
from open_speech.document.model import Document, SourceKind
from open_speech.errors import EngineError, EstimateRequired, MissingKey, NotFound
from open_speech.plan.script import SpeechScript

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

_EXT_KIND = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_URL = re.compile(r"^https?://", re.I)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DocumentService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._docs: OrderedDict[str, Document] = OrderedDict()
        self._scripts: dict[str, SpeechScript] = {}
        self._plan_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ create

    async def create(self, req: s.CreateDocumentRequest) -> s.CreateDocumentResponse:
        from open_speech.ingest import SourceInput

        src: SourceInput
        match req.kind:
            case "text":
                if not (req.text or "").strip():
                    raise EngineError("invalid_request", "There's no text to read.")
                src = SourceInput(kind="text", text=req.text, title=req.title)
            case "url":
                if not req.url:
                    raise EngineError("invalid_request", "A URL is required.")
                url = req.url if _URL.match(req.url) else f"https://{req.url}"
                src = SourceInput(kind="url", url=url, title=req.title)
            case "html_blocks":
                if req.html_blocks is None:
                    raise EngineError("invalid_request", "html_blocks payload is required.")
                src = SourceInput(
                    kind="html_blocks", html_blocks=req.html_blocks, title=req.title or req.html_blocks.title
                )
            case "image":
                if not req.image_base64:
                    raise EngineError("invalid_request", "image_base64 is required.")
                src = SourceInput(
                    kind="image",
                    data=base64.b64decode(req.image_base64),
                    media_type=req.media_type or "image/png",
                    image_mode=req.image_mode,
                    title=req.title,
                )
            case "clipboard":
                clip = await self.e.clipboard.read()
                if clip.image_png:
                    src = SourceInput(
                        kind="image",
                        data=clip.image_png,
                        media_type="image/png",
                        image_mode=req.image_mode,
                        title=req.title or "Clipboard image",
                    )
                elif clip.text and clip.text.strip():
                    src = SourceInput(kind="text", text=clip.text, title=req.title or "Clipboard")
                else:
                    raise EngineError("invalid_request", "Your clipboard is empty. Copy some text or an image first.")
            case "file_path":
                if not req.path:
                    raise EngineError("invalid_request", "A file path is required.")
                p = Path(req.path).expanduser()
                if not p.is_file():
                    raise NotFound(f"the file {p.name}")
                return await self.create_from_upload(
                    p.name,
                    p.read_bytes(),
                    play=req.play,
                    voice_id=req.voice_id,
                    mode=req.mode,
                    confirm_estimate=req.confirm_estimate,
                    path=str(p),
                )
        return await self._ingest_and_open(
            src, play=req.play, voice_id=req.voice_id, mode=req.mode, confirm_estimate=req.confirm_estimate
        )

    async def create_from_upload(
        self,
        filename: str,
        data: bytes,
        *,
        play: bool = False,
        voice_id: str | None = None,
        mode: s.Mode | None = None,
        confirm_estimate: bool = False,
        path: str | None = None,
    ) -> s.CreateDocumentResponse:
        from open_speech.ingest import SourceInput

        ext = Path(filename).suffix.lower()
        media_type = _EXT_KIND.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if media_type.startswith("image/"):
            src = SourceInput(
                kind="image", data=data, filename=filename, media_type=media_type, title=Path(filename).stem
            )
        elif ext in (".txt", ".md", ".markdown") or media_type.startswith("text/") or ext in (".pdf", ".docx"):
            src = SourceInput(kind="file", data=data, filename=filename, media_type=media_type)
        else:
            raise EngineError(
                "invalid_request",
                f"I can't read {ext or 'that kind of'} files yet. PDF, DOCX, text, Markdown and images work.",
            )
        return await self._ingest_and_open(
            src, play=play, voice_id=voice_id, mode=mode, confirm_estimate=confirm_estimate, path=path
        )

    async def _ingest_and_open(
        self,
        src: Any,
        *,
        play: bool,
        voice_id: str | None,
        mode: s.Mode | None,
        confirm_estimate: bool,
        path: str | None = None,
    ) -> s.CreateDocumentResponse:
        from open_speech.ingest import ingest

        vision = None
        if src.kind == "image":
            vision = self.e.registry.vision()
            if vision is not None:
                self.e.budget.check_paid()
        result = await ingest(
            src,
            extract_cache=self.e.extract_cache,
            vision=vision,
            vision_rules=self._vision_rules(),
        )
        doc = result.document
        if path:
            doc.path = path
        for op, call in result.llm_calls:
            self.e.record_llm(op, call, document_id=doc.id)
        self._store(doc, result.source_bytes, src)
        settings = self._settings(doc.id)
        changed = False
        if voice_id:
            settings["voice_id"] = voice_id
            changed = True
        if mode:
            settings["mode"] = mode
            changed = True
        if changed:
            self._save_settings(doc.id, settings)

        estimate: s.Estimate | None = None
        if self.mode_for(doc) == "ai_enhanced":
            estimate = await self.e.usage.estimate(s.EstimateRequest(document_id=doc.id))
            if estimate.requires_confirmation and not confirm_estimate:
                return s.CreateDocumentResponse(document=await self.get(doc.id), estimate=estimate, playing=False)

        await self.script_for(doc)
        playing = False
        if play:
            await self.e.playback.play(s.PlayRequest(document_id=doc.id))
            playing = True
        return s.CreateDocumentResponse(document=await self.get(doc.id), estimate=estimate, playing=playing)

    def _vision_rules(self) -> list[Any]:
        from open_speech.providers.base import RuleBrief

        return [
            RuleBrief(id=r.id, name=r.name, instruction=r.instruction)
            for r in self.e.rule_store.all()
            if r.enabled and (not r.scope.types or "image" in r.scope.types) and not r.preset
        ]

    # ------------------------------------------------------------------ persistence

    def _store(self, doc: Document, source_bytes: bytes | None, src: Any) -> None:
        now = _now()
        source_ext = None
        if source_bytes:
            source_ext = self._source_ext(doc, src)
            target = self.e.paths.uploads / f"{doc.source_id}{source_ext}"
            if not target.exists():
                target.write_bytes(source_bytes)
        existing = self.e.db.one("SELECT id, settings_json FROM documents WHERE id=?", (doc.id,))
        body = "\n".join(b.text for b in doc.blocks)[:400_000]
        with self.e.db.transaction() as c:
            if existing:
                c.execute(
                    "UPDATE documents SET title=?, url=?, site=?, path=COALESCE(?, path), page_count=?, extract_key=?, last_opened_at=? WHERE id=?",
                    (doc.title, doc.url, doc.site, doc.path, doc.page_count, doc.extractor_version, now, doc.id),
                )
                c.execute("DELETE FROM documents_fts WHERE doc_id=?", (doc.id,))
            else:
                c.execute(
                    "INSERT INTO documents(id, source_id, kind, title, url, site, path, page_count, extract_key, settings_json, created_at, last_opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        doc.id,
                        doc.source_id,
                        doc.kind.value,
                        doc.title,
                        doc.url,
                        doc.site,
                        doc.path,
                        doc.page_count,
                        doc.extractor_version,
                        json.dumps({"source_ext": source_ext} if source_ext else {}),
                        now,
                        now,
                    ),
                )
            c.execute("INSERT INTO documents_fts(doc_id, title, body) VALUES(?,?,?)", (doc.id, doc.title, body))
        if source_ext and existing:
            st = self._settings(doc.id)
            if st.get("source_ext") != source_ext:
                st["source_ext"] = source_ext
                self._save_settings(doc.id, st)
        self._remember(doc)

    @staticmethod
    def _source_ext(doc: Document, src: Any) -> str:
        if doc.kind == SourceKind.pdf:
            return ".pdf"
        if doc.kind == SourceKind.docx:
            return ".docx"
        if doc.kind == SourceKind.image:
            mt = getattr(src, "media_type", None) or "image/png"
            return mimetypes.guess_extension(mt) or ".png"
        fn = getattr(src, "filename", None)
        return Path(fn).suffix if fn else ".txt"

    def _remember(self, doc: Document) -> None:
        self._docs[doc.id] = doc
        self._docs.move_to_end(doc.id)
        while len(self._docs) > 16:
            old, _ = self._docs.popitem(last=False)
            self._scripts.pop(old, None)

    def _row(self, document_id: str) -> Any:
        row = self.e.db.one("SELECT * FROM documents WHERE id=?", (document_id,))
        if row is None:
            raise NotFound("that document")
        return row

    def _settings(self, document_id: str) -> dict[str, Any]:
        row = self.e.db.one("SELECT settings_json FROM documents WHERE id=?", (document_id,))
        return json.loads(row["settings_json"]) if row else {}

    def _save_settings(self, document_id: str, settings: dict[str, Any]) -> None:
        self.e.db.execute("UPDATE documents SET settings_json=? WHERE id=?", (json.dumps(settings), document_id))

    def load(self, document_id: str) -> Document:
        if document_id in self._docs:
            self._docs.move_to_end(document_id)
            return self._docs[document_id]
        row = self._row(document_id)
        doc = self.e.extract_cache.get(row["source_id"], row["extract_key"])
        if doc is None:
            raise EngineError(
                "not_found", f"The extracted text for “{row['title']}” is missing. Open the original again."
            )
        doc.path = doc.path or row["path"]
        self._remember(doc)
        return doc

    def touch(self, document_id: str) -> None:
        self.e.db.execute("UPDATE documents SET last_opened_at=? WHERE id=?", (_now(), document_id))

    # ------------------------------------------------------------------ queries

    def _out(self, row: Any) -> s.DocumentOut:
        st = json.loads(row["settings_json"] or "{}")
        pos = self.e.db.one("SELECT * FROM positions WHERE document_id=?", (row["id"],))
        try:
            doc = self.load(row["id"])
            block_count, flags = len(doc.blocks), list(doc.flags)
        except EngineError:
            block_count, flags = 0, ["extract_missing"]
        return s.DocumentOut(
            id=row["id"],
            kind=row["kind"],
            title=row["title"],
            url=row["url"],
            site=row["site"],
            path=row["path"],
            page_count=row["page_count"],
            block_count=block_count,
            created_at=row["created_at"],
            last_opened_at=row["last_opened_at"],
            progress=float(pos["progress"]) if pos else 0.0,
            position=s.Position(
                utterance_index=pos["utterance_index"] if pos else 0, offset_ms=pos["offset_ms"] if pos else 0
            ),
            settings=s.DocumentSettings(
                voice_id=st.get("voice_id"), mode=st.get("mode"), pinned=bool(st.get("pinned"))
            ),
            flags=flags,
            has_source_file=bool(st.get("source_ext")),
        )

    async def get(self, document_id: str) -> s.DocumentOut:
        return self._out(self._row(document_id))

    async def list(self, *, q: str | None = None, limit: int = 10) -> s.DocumentList:
        if q and q.strip():
            terms = " ".join(f'"{t}"*' for t in re.findall(r"\w+", q)[:8])
            if not terms:
                return s.DocumentList(items=[])
            rows = self.e.db.query(
                "SELECT d.* FROM documents_fts f JOIN documents d ON d.id = f.doc_id "
                "WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts, 0.0, 10.0, 1.0), d.last_opened_at DESC LIMIT ?",
                (terms, limit),
            )
        else:
            rows = self.e.db.query("SELECT * FROM documents ORDER BY last_opened_at DESC LIMIT ?", (limit,))
        return s.DocumentList(items=[self._out(r) for r in rows])

    async def blocks(self, document_id: str, *, offset: int, limit: int) -> s.BlocksPage:
        doc = self.load(document_id)
        return s.BlocksPage(
            items=doc.blocks[offset : offset + limit], total=len(doc.blocks), offset=offset, limit=limit
        )

    async def script(self, document_id: str) -> s.ScriptOut:
        script = await self.script_for(self.load(document_id))
        return s.ScriptOut(
            document_id=document_id,
            mode=script.mode,
            utterances=script.utterances,
            removed=script.removed,
            warnings=script.warnings,
        )

    async def source_file(self, document_id: str) -> tuple[Path, str]:
        row = self._row(document_id)
        ext = self._settings(document_id).get("source_ext")
        if not ext:
            raise NotFound("an original file for that document")
        p = self.e.paths.uploads / f"{row['source_id']}{ext}"
        if not p.exists():
            raise NotFound("the original file")
        return p, _EXT_KIND.get(ext, "application/octet-stream")

    # ------------------------------------------------------------------ planning

    def mode_for(self, doc: Document) -> Literal["classic", "ai_enhanced"]:
        st = self._settings(doc.id)
        if st.get("mode") in ("classic", "ai_enhanced"):
            return st["mode"]  # type: ignore[no-any-return]
        return self.e.rule_store.mode_for(doc, self.e.settings.get().defaults.mode)

    def voice_for(self, document_id: str | None) -> str:
        if document_id:
            v = self._settings(document_id).get("voice_id")
            if v:
                return str(v)
        return self.e.settings.get().defaults.voice

    def plan_context(self, doc: Document) -> Any:
        from open_speech.plan.planner import PlanContext

        rs = self.e.rule_store
        return PlanContext(
            document=doc,
            rules=rs.rules_for(doc),
            lexicon=rs.lexicon(),
            speech_md=rs.speech_md(),
            mode=self.mode_for(doc),
            llm_model=self.e.settings.get().llm.model,
        )

    def llm_for_plan(self, mode: str) -> Any:
        if mode != "ai_enhanced":
            return None
        llm = self.e.registry.llm()
        if llm is None or not self.e.budget.allow_paid():
            return None
        return llm

    async def script_for(self, doc: Document, *, force: bool = False) -> SpeechScript:
        lock = self._plan_locks.setdefault(doc.id, asyncio.Lock())
        async with lock:
            if not force and doc.id in self._scripts:
                cached = self._scripts[doc.id]
                ctx = self.plan_context(doc)
                if cached.mode == ctx.mode:
                    from open_speech.plan.planner import plan_key as _pk

                    try:
                        if cached.plan_key == _pk(ctx):
                            return cached
                    except Exception:  # noqa: BLE001 — planner without a public plan_key: fall through and re-plan
                        pass
            ctx = self.plan_context(doc)
            llm = self.llm_for_plan(ctx.mode)
            result = await self.e.planner.plan(ctx, llm, first_batch_only=True)
            for op, call in result.llm_calls:
                self.e.record_llm(op, call, document_id=doc.id)
            script = result.script
            if ctx.mode == "ai_enhanced" and llm is None:
                reason = (
                    "AI Enhanced needs an Anthropic key, so this used Classic cleanup."
                    if self.e.registry.llm() is None
                    else "Your budget is used up, so this used Classic cleanup."
                )
                if reason not in script.warnings:
                    script.warnings.append(reason)
            self._scripts[doc.id] = script
            return script

    async def extend_plan(self, document_id: str, upto_utterance: int) -> SpeechScript | None:
        """AI Enhanced lookahead: keep ≥ 2 annotate batches ahead of the playhead."""
        doc = self.load(document_id)
        script = self._scripts.get(document_id)
        if script is None or script.mode != "ai_enhanced":
            return None
        ctx = self.plan_context(doc)
        llm = self.llm_for_plan(ctx.mode)
        if llm is None:
            return None
        result = await self.e.planner.extend(ctx, llm, script, upto_utterance)
        for op, call in result.llm_calls:
            self.e.record_llm(op, call, document_id=doc.id)
        if result.script is not script:
            self._scripts[document_id] = result.script
        return result.script

    async def replan(self, document_id: str, reason: str) -> SpeechScript:
        doc = self.load(document_id)
        script = await self.script_for(doc, force=True)
        self.e.events.publish(DocumentUpdated(document_id=document_id, reason=reason))
        return script

    def invalidate_all(self) -> None:
        self._scripts.clear()

    async def regenerate(self, document_id: str, *, confirm_estimate: bool = False) -> s.ScriptOut:
        doc = self.load(document_id)
        if self.mode_for(doc) == "ai_enhanced":
            if self.e.registry.llm() is None:
                raise MissingKey("anthropic", "Regenerating with AI Enhanced")
            est = await self.e.usage.estimate(s.EstimateRequest(document_id=document_id, operation="regenerate"))
            if est.requires_confirmation and not confirm_estimate:
                raise EstimateRequired(est.usd_estimate, "regenerate")
        self.e.plan_cache.drop_document(document_id)
        self._scripts.pop(document_id, None)
        await self.replan(document_id, "regenerated")
        await self.e.playback.on_script_changed(document_id)
        return await self.script(document_id)

    # ------------------------------------------------------------------ settings, delete

    async def update_settings(self, document_id: str, upd: s.DocumentSettingsUpdate) -> s.DocumentOut:
        self._row(document_id)
        st = self._settings(document_id)
        replan = False
        if upd.voice_id is not None:
            st["voice_id"] = upd.voice_id
        if upd.mode is not None and upd.mode != st.get("mode"):
            st["mode"] = upd.mode
            replan = True
        if upd.pinned is not None:
            st["pinned"] = upd.pinned
        self._save_settings(document_id, st)
        if replan:
            await self.replan(document_id, "mode changed")
            await self.e.playback.on_script_changed(document_id)
        if upd.voice_id is not None and self.e.playback.current_document_id() == document_id:
            await self.e.transport.set_voice(upd.voice_id)
        return await self.get(document_id)

    def pinned_ids(self) -> set[str]:
        rows = self.e.db.query("SELECT id, settings_json FROM documents")
        return {r["id"] for r in rows if json.loads(r["settings_json"] or "{}").get("pinned")}

    async def delete(self, document_id: str) -> None:
        row = self._row(document_id)
        if self.e.playback.current_document_id() == document_id:
            await self.e.playback.stop()
        ext = self._settings(document_id).get("source_ext")
        with self.e.db.transaction() as c:
            c.execute("DELETE FROM documents_fts WHERE doc_id=?", (document_id,))
            c.execute("DELETE FROM positions WHERE document_id=?", (document_id,))
            c.execute("DELETE FROM plans WHERE document_id=?", (document_id,))
            c.execute("DELETE FROM documents WHERE id=?", (document_id,))
        if ext:
            (self.e.paths.uploads / f"{row['source_id']}{ext}").unlink(missing_ok=True)
        self._docs.pop(document_id, None)
        self._scripts.pop(document_id, None)

    # ------------------------------------------------------------------ positions

    def save_position(self, document_id: str, utterance_index: int, offset_ms: int) -> None:
        script = self._scripts.get(document_id)
        total = len(script.utterances) if script else 0
        progress = min(1.0, utterance_index / total) if total else 0.0
        self.e.db.execute(
            "INSERT INTO positions(document_id, utterance_index, offset_ms, progress, updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET utterance_index=excluded.utterance_index, "
            "offset_ms=excluded.offset_ms, progress=excluded.progress, updated_at=excluded.updated_at",
            (document_id, utterance_index, offset_ms, progress, _now()),
        )

    def position(self, document_id: str) -> tuple[int, int]:
        row = self.e.db.one("SELECT utterance_index, offset_ms FROM positions WHERE document_id=?", (document_id,))
        return (row["utterance_index"], row["offset_ms"]) if row else (0, 0)
