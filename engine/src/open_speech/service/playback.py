"""Playback use cases over the single transport (03 § Playback transport; 04 § Playback)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from open_speech.api import schemas as s
from open_speech.errors import EngineError, NotFound
from open_speech.plan.script import SpeechScript

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.playback")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h} hour{'s' if h != 1 else ''} {m} minute{'s' if m != 1 else ''}"
    if h:
        return f"{h} hour{'s' if h != 1 else ''}"
    if m:
        return f"{m} minute{'s' if m != 1 else ''}"
    return "less than a minute"


def _fmt_speed(speed: float) -> str:
    return f"{speed:g}x"


class PlaybackService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._doc_id: str | None = None
        self._lookahead: asyncio.Task[None] | None = None
        self._bar_open = False

    # ------------------------------------------------------------------ state

    def current_document_id(self) -> str | None:
        return self._doc_id

    def state(self) -> s.PlaybackState:
        return self.e.transport.snapshot()

    # ------------------------------------------------------------------ play

    def _resolve_start(self, script: SpeechScript, frm: s.PlayFrom | None, document_id: str) -> tuple[int, int]:
        utts = script.utterances
        if not utts:
            raise EngineError("invalid_request", "There's nothing to read in that document with the current rules.")
        if frm is None:
            idx, off = self.e.documents.position(document_id)
            if idx >= len(utts) - 1 and idx > 0:  # finished last time → start over
                return 0, 0
            return min(idx, len(utts) - 1), off
        if frm.utterance_index is not None:
            return max(0, min(frm.utterance_index, len(utts) - 1)), 0
        if frm.utterance_id is not None:
            for u in utts:
                if u.id == frm.utterance_id:
                    return u.index, 0
            raise NotFound("that sentence")
        if frm.page is not None:
            for u in utts:
                if u.page is not None and u.page >= frm.page:
                    return u.index, 0
            raise EngineError("invalid_request", f"There's no page {frm.page} in this document.")
        if frm.block_id is not None:
            doc = self.e.documents.load(document_id)
            order = {b.id: b.order for b in doc.blocks}
            target_order = order.get(frm.block_id)
            if target_order is None:
                raise NotFound("that paragraph")
            best: int | None = None
            for u in utts:
                if frm.block_id in u.block_ids:
                    if frm.char is None:
                        return u.index, 0
                    for seg in u.segments:
                        if seg.block_id == frm.block_id and seg.src_start <= frm.char < max(
                            seg.src_end, seg.src_start + 1
                        ):
                            return u.index, 0
                    if best is None:
                        best = u.index
                    # keep the last utterance in this block that starts before the char
                    starts = [seg.src_start for seg in u.segments if seg.block_id == frm.block_id]
                    if starts and min(starts) <= frm.char:
                        best = u.index
            if best is not None:
                return best, 0
            # block was skipped by rules: start at the next spoken block
            for u in utts:
                if any(order.get(b, -1) >= target_order for b in u.block_ids):
                    return u.index, 0
        return 0, 0

    async def play(self, req: s.PlayRequest) -> s.PlaybackState:
        document_id = req.document_id or self._doc_id
        if document_id is None:
            recent = self.e.db.one("SELECT id FROM documents ORDER BY last_opened_at DESC LIMIT 1")
            if recent is None:
                raise EngineError(
                    "invalid_request", "Nothing to play yet. Tell me what to read, or drop a file on the bar."
                )
            document_id = recent["id"]
        assert document_id is not None
        if req.document_id is None and req.from_ is None and self._doc_id == document_id:
            st = self.e.transport.snapshot().state
            if st == "paused":
                return await self.resume()
            if st == "playing":
                return self.state()
        doc = self.e.documents.load(document_id)
        script = await self.e.documents.script_for(doc)
        start, offset = self._resolve_start(script, req.from_, document_id)
        settings = self.e.settings.get()
        speed = self.e.transport.snapshot().speed if self._doc_id else settings.defaults.speed
        await self.e.transport.load(
            document_id=doc.id,
            title=doc.title,
            page_count=doc.page_count,
            script=script,
            start_index=start,
            offset_ms=offset,
            voice_id=self.e.documents.voice_for(doc.id),
            style=settings.defaults.style,
            speed=speed,
        )
        self._doc_id = doc.id
        self.e.documents.touch(doc.id)
        await self.e.transport.play()
        self._start_lookahead(doc.id, script)
        return self.state()

    def _start_lookahead(self, document_id: str, script: SpeechScript) -> None:
        if self._lookahead is not None:
            self._lookahead.cancel()
            self._lookahead = None
        if script.mode != "ai_enhanced":
            return
        self._lookahead = asyncio.create_task(self._lookahead_loop(document_id))

    async def _lookahead_loop(self, document_id: str) -> None:
        """Keep annotation ≥ 2 batches ahead of the playhead (05 § Tier 2 batching)."""
        try:
            while self._doc_id == document_id:
                snap = self.e.transport.snapshot()
                if snap.state in ("playing", "loading"):
                    try:
                        new = await self.e.documents.extend_plan(document_id, snap.utterance_index + 12)
                    except EngineError as e:
                        log.info("lookahead stopped: %s", e.detail)
                        return
                    if new is not None:
                        self.e.transport.replace_script(new, keep_position=True)
                        if new.annotated_upto_block is None:
                            return
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass

    async def on_script_changed(self, document_id: str) -> None:
        """A rule/lexicon/mode change re-planned this document: keep playing from the playhead."""
        if self._doc_id != document_id:
            return
        script = await self.e.documents.script_for(self.e.documents.load(document_id))
        self.e.transport.replace_script(script, keep_position=True)
        self._start_lookahead(document_id, script)

    # ------------------------------------------------------------------ transport passthrough

    async def pause(self) -> s.PlaybackState:
        await self.e.transport.pause()
        return self.state()

    async def resume(self) -> s.PlaybackState:
        snap = self.e.transport.snapshot()
        if snap.state in ("idle", "stopped") and self._doc_id is None:
            return await self.play(s.PlayRequest())
        if snap.state in ("idle", "stopped"):
            return await self.play(s.PlayRequest(document_id=self._doc_id))
        await self.e.transport.resume()
        return self.state()

    async def stop(self) -> s.PlaybackState:
        await self.e.transport.stop()
        return self.state()

    async def skip(self, req: s.SkipRequest) -> s.PlaybackState:
        if self._doc_id is None:
            raise EngineError("invalid_request", "Nothing is playing.")
        await self.e.transport.skip(req.unit, req.count)
        return self.state()

    async def set_speed(self, speed: float) -> s.PlaybackState:
        speed = max(0.5, min(5.0, round(speed, 2)))
        await self.e.transport.set_speed(speed)
        return self.state()

    async def change_speed(self, delta: float) -> s.PlaybackState:
        return await self.set_speed(self.e.transport.snapshot().speed + delta)

    async def go_to_page(self, page: int) -> s.PlaybackState:
        if self._doc_id is None:
            raise EngineError("invalid_request", "Nothing is open. Tell me what to read first.")
        return await self.play(s.PlayRequest(document_id=self._doc_id, from_=s.PlayFrom(page=page)))

    async def set_voice(self, req: s.VoiceRequest) -> s.PlaybackState:
        voices = {v.id: v for v in self.e.registry.voices()}
        v = voices.get(req.voice_id)
        if v is None:
            raise NotFound(f"the voice {req.voice_id}")
        if not v.available:
            raise EngineError("missing_key", v.unavailable_reason or f"{v.name} isn't available right now.")
        if req.scope == "default":
            await self.e.settings.set_key("defaults.voice", req.voice_id)
            if self._doc_id and not self.e.documents._settings(self._doc_id).get("voice_id"):
                await self.e.transport.set_voice(req.voice_id)
        elif req.scope == "assistant":
            await self.e.settings.set_key("defaults.assistant_voice", req.voice_id)
        else:
            if self._doc_id is None:
                await self.e.settings.set_key("defaults.voice", req.voice_id)
            else:
                await self.e.documents.update_settings(self._doc_id, s.DocumentSettingsUpdate(voice_id=req.voice_id))
        return self.state()

    # ------------------------------------------------------------------ ducking (07 § Ducking)

    async def bar_state(self, open_: bool) -> None:
        self._bar_open = open_
        await self.e.transport.duck(open_)

    # ------------------------------------------------------------------ where am I (FR-ASST-5)

    def where_am_i(self) -> tuple[str, dict[str, object]]:
        snap = self.state()
        if snap.document_id is None or snap.state in ("idle",):
            return "Nothing is playing right now.", {}
        parts: list[str] = []
        title = snap.document_title or "this document"
        if snap.heading:
            parts.append(f"{snap.heading}.")
        else:
            parts.append(f"{title}.")
        if snap.page and snap.page_count:
            parts.append(f"Page {snap.page} of {snap.page_count},")
        else:
            parts.append(f"{int(round(snap.progress * 100))} percent through,")
        if snap.time_left_s is not None:
            parts.append(f"about {_fmt_duration(snap.time_left_s)} left at {_fmt_speed(snap.speed)}.")
        sentence = " ".join(parts).rstrip(",") if parts else title
        if not sentence.endswith("."):
            sentence += "."
        data = {
            "document": title,
            "heading": snap.heading,
            "page": snap.page,
            "page_count": snap.page_count,
            "percent": round(snap.progress * 100),
            "time_left_s": snap.time_left_s,
            "speed": snap.speed,
        }
        return sentence, data

    async def close(self) -> None:
        if self._lookahead is not None:
            self._lookahead.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._lookahead
