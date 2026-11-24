"""Playback use cases over the single transport (03 § Playback transport; 04 § Playback)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from open_speech.api import schemas as s
from open_speech.config import AUTO_VOICE
from open_speech.errors import EngineError, EstimateRequired, NotFound
from open_speech.i18n import say, t
from open_speech.plan.script import SpeechScript

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.playback")

COST_ASK_AGAIN_S = 120.0  # asking to play again this soon after hearing the cost means "go ahead"


class PlaybackService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._doc_id: str | None = None
        self._lookahead: asyncio.Task[None] | None = None
        self._bar_open = False
        self._queue: list[s.QueueItem] = []  # read next, in order, when the current document ends
        self._advancing: asyncio.Task[None] | None = None
        self._cost_ok: set[str] = set()  # documents the user agreed to pay for
        self._cost_asked: dict[str, float] = {}  # document → when we last said what it would cost
        # Bumped whenever a command starts, resumes or changes reading (what plays, where, how fast, in which
        # voice): the assistant compares it across a turn to know whether reading audio is the turn's answer.
        self.touched = 0

    # ------------------------------------------------------------------ state

    def current_document_id(self) -> str | None:
        return self._doc_id

    def state(self) -> s.PlaybackState:
        return self.e.transport.snapshot()

    def reading_intended(self) -> bool:
        """Reading will be heard once the conversation lets go of it (it is playing, or held only by the bar or
        by speech). False when paused, stopped or with nothing open."""
        return bool(self.e.transport.reading_intended)

    # ------------------------------------------------------------------ play

    def _resolve_start(self, script: SpeechScript, frm: s.PlayFrom | None, document_id: str) -> tuple[int, int]:
        utts = script.utterances
        if not utts:
            raise EngineError("invalid_request", t("error.nothing_to_read"))
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
            raise EngineError("invalid_request", t("error.no_page", page=frm.page))
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
        """Play a document. Choosing a different one by hand ends whatever was queued behind the old one."""
        if req.document_id and req.document_id != self._doc_id:
            self._queue = []
        return await self._play(req)

    # ------------------------------------------------------------------ asking before a costly read (FR-COST-3)

    def confirm_cost(self, document_ids: list[str]) -> None:
        """The user said yes to what these documents are estimated to cost."""
        self._cost_ok.update(document_ids)

    async def cost_to_confirm(self, document_id: str) -> s.Estimate | None:
        """The estimate for reading `document_id`, when it is over the threshold and not yet agreed to. Free
        reading (a local voice, with no Claude cleanup in this build) is never estimated."""
        if document_id in self._cost_ok:
            return None
        doc = self.e.documents.load(document_id)
        try:
            free_voice = self.e.registry.is_local(self.e.documents.voice_for(document_id))
        except EngineError:
            free_voice = True  # an unavailable voice falls back to the local one
        if free_voice and self.e.documents.mode_for(doc) != "ai_enhanced":
            return None
        try:
            estimate = await self.e.usage.estimate(s.EstimateRequest(document_id=document_id))
        except EngineError:
            return None
        return estimate if estimate.requires_confirmation else None

    async def _check_cost(self, document_ids: list[str], *, confirmed: bool) -> None:
        """Every way of starting a read comes through here, so the question is asked once, in one place. The
        answer is `confirm_estimate`, a confirmation card, or simply asking again within two minutes. Several
        documents (a queue) are agreed to as a whole."""
        if confirmed:
            self._cost_ok.update(document_ids)
            return
        costly: dict[str, Decimal] = {}
        total = Decimal(0)
        for document_id in document_ids:
            estimate = await self.cost_to_confirm(document_id)
            if estimate is not None:
                costly[document_id] = Decimal(estimate.usd_estimate)
            if document_id not in self._cost_ok:
                total += Decimal(estimate.usd_estimate) if estimate is not None else Decimal(0)
        if not costly:
            return
        now = time.monotonic()
        if all(now - self._cost_asked.get(d, float("-inf")) <= COST_ASK_AGAIN_S for d in costly):
            self._cost_ok.update(costly)
            return
        for document_id in costly:
            self._cost_asked[document_id] = now
        raise EstimateRequired(str(total), "read")

    def cost_was_asked(self, document_id: str) -> None:
        """Whoever opened the document has shown its estimate: the next "play" is the go-ahead."""
        self._cost_asked[document_id] = time.monotonic()

    async def _play(self, req: s.PlayRequest) -> s.PlaybackState:
        document_id = req.document_id or self._doc_id
        if document_id is None:
            recent = self.e.db.one("SELECT id FROM documents ORDER BY last_opened_at DESC LIMIT 1")
            if recent is None:
                raise EngineError("invalid_request", t("error.nothing_to_play"))
            document_id = recent["id"]
        assert document_id is not None
        if req.document_id is None and req.from_ is None and self._doc_id == document_id:
            st = self.e.transport.snapshot().state
            if st == "paused":
                return await self.resume()
            if st == "playing":
                return self.state()
        await self._check_cost([document_id], confirmed=req.confirm_estimate)
        doc = self.e.documents.load(document_id)
        script = await self.e.documents.script_for(doc)
        start, offset = self._resolve_start(script, req.from_, document_id)
        settings = self.e.settings.get()
        speed = self.e.transport.snapshot().speed if self._doc_id else settings.defaults.speed
        await self.e.voices.settle_auto()  # right after startup: know whether Chirp works before "auto" picks
        voice_id, notice_reason = self.e.documents.resolve_voice(doc.id)
        await self.e.transport.load(
            document_id=doc.id,
            title=doc.title,
            page_count=doc.page_count,
            script=script,
            start_index=start,
            offset_ms=offset,
            voice_id=voice_id,
            style=settings.defaults.style,
            speed=speed,
        )
        if notice_reason:  # "auto" reads with the local voice this time: say why, once
            self.e.transport.show_notice(self.e.voices.notice_text(notice_reason), notice_reason)
        self._doc_id = doc.id
        self.e.documents.touch(doc.id)
        self.touched += 1
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
        self.touched += 1
        await self.e.transport.resume()
        return self.state()

    async def stop(self) -> s.PlaybackState:
        self._queue = []
        await self.e.transport.stop()
        return self.state()

    # ------------------------------------------------------------------ queue: several documents in a row

    def queue(self) -> list[s.QueueItem]:
        return list(self._queue)

    async def enqueue(self, req: s.QueueRequest) -> s.PlaybackState:
        """Read `document_ids` one after another: `now` starts the first at once, `next` slots them in after
        the current document, `last` appends. With nothing playing, any of them starts the first."""
        seen: set[str] = set()
        items: list[s.QueueItem] = []
        for did in req.document_ids:
            if did in seen:
                continue
            seen.add(did)
            items.append(s.QueueItem(document_id=did, title=self.e.documents.title(did)))
        # A queue is agreed to before it is queued, so reading never stops halfway to ask about money.
        await self._check_cost([q.document_id for q in items], confirmed=req.confirm_estimate)
        rest = [q for q in self._queue if q.document_id not in seen]
        busy = self._doc_id is not None and self.e.transport.snapshot().state in ("playing", "loading", "paused")
        if req.when == "now" or not busy:
            self._queue = items[1:] + (rest if req.when != "now" else [])
            return await self._play(s.PlayRequest(document_id=items[0].document_id))
        self._queue = [*items, *rest] if req.when == "next" else [*rest, *items]
        self.e.transport.publish_state()
        return self.state()

    async def next_document(self) -> s.PlaybackState:
        """Skip the rest of this document and start the next one in the queue."""
        if not self._queue:
            raise EngineError("invalid_request", t("queue.empty"))
        nxt = self._queue.pop(0)
        return await self._play(s.PlayRequest(document_id=nxt.document_id))

    async def clear_queue(self) -> s.PlaybackState:
        self._queue = []
        self.e.transport.publish_state()
        return self.state()

    def document_finished(self, document_id: str) -> None:
        """The transport played the last sentence of `document_id` (called from its tick, so only schedule)."""
        if document_id != self._doc_id or not self._queue:
            return

        async def advance() -> None:
            try:
                await self.next_document()
            except EngineError as e:
                log.warning("could not start the next queued document: %s", e.detail)
                if self._queue:  # skip the one that failed and keep going
                    self.document_finished(document_id)

        self._advancing = asyncio.create_task(advance())

    async def skip(self, req: s.SkipRequest) -> s.PlaybackState:
        if self._doc_id is None:
            raise EngineError("invalid_request", t("error.nothing_playing"))
        self.touched += 1
        await self.e.transport.skip(req.unit, req.count)
        return self.state()

    async def set_speed(self, speed: float) -> s.PlaybackState:
        speed = max(0.5, min(5.0, round(speed, 2)))
        self.touched += 1
        await self.e.transport.set_speed(speed)
        return self.state()

    async def change_speed(self, delta: float) -> s.PlaybackState:
        return await self.set_speed(self.e.transport.snapshot().speed + delta)

    async def go_to_page(self, page: int) -> s.PlaybackState:
        if self._doc_id is None:
            raise EngineError("invalid_request", t("error.nothing_open"))
        return await self.play(s.PlayRequest(document_id=self._doc_id, from_=s.PlayFrom(page=page)))

    async def set_voice(self, req: s.VoiceRequest) -> s.PlaybackState:
        voices = {v.id: v for v in self.e.registry.voices()}
        v = voices.get(req.voice_id)
        if req.voice_id == AUTO_VOICE:
            pass  # the automatic choice: Chirp Zephyr while it's free, else the local voice
        elif v is None:
            raise NotFound(f"the voice {req.voice_id}")
        elif not v.available:
            raise EngineError("unavailable", v.unavailable_reason or f"{v.name} isn't available right now.")
        if req.scope != "assistant":
            self.touched += 1
        if req.scope == "assistant":
            await self.e.settings.set_key("defaults.assistant_voice", req.voice_id)
        elif req.scope == "default" or self._doc_id is None:
            await self.e.voices.set_default(req.voice_id)
            if self._doc_id and not self.e.documents._settings(self._doc_id).get("voice_id"):
                await self.e.transport.set_voice(self.e.documents.voice_for(self._doc_id))
        else:
            await self.e.documents.update_settings(self._doc_id, s.DocumentSettingsUpdate(voice_id=req.voice_id))
        return self.state()

    # ------------------------------------------------------------------ ducking (07 § Ducking)

    async def bar_state(self, open_: bool) -> None:
        self._bar_open = open_
        if open_:
            self.e.warm_llm()  # the user is about to speak: have a warm connection ready for the reply
        await self.e.transport.duck(open_)

    # ------------------------------------------------------------------ where am I (FR-ASST-5)

    def where_am_i(self) -> tuple[str, dict[str, object]]:
        snap = self.state()
        if snap.document_id is None or snap.state in ("idle",):
            return t("where.nothing"), {}
        title = snap.document_title or t("where.this_document")
        parts: list[str] = [f"{snap.heading or title}."]
        if snap.page and snap.page_count:
            parts.append(t("where.page", page=snap.page, pages=snap.page_count))
        else:
            parts.append(t("where.percent", n=int(round(snap.progress * 100))))
        if snap.time_left_s is not None:
            key = "where.left" if snap.time_left_s >= 60 else "where.left_soon"  # not "about less than a minute"
            parts.append(t(key, time=say.duration(snap.time_left_s), speed=say.speed(snap.speed)))
        sentence = " ".join(parts).rstrip(",") if parts else title
        if not sentence.endswith("."):
            sentence += "."
        data: dict[str, object] = {
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
        for task in (self._lookahead, self._advancing):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
