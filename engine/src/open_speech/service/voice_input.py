"""Mic sessions over the WebSocket (07 § Voice loop; FR-BAR-2/4, FR-SETUP-8).

Binary frames (PCM16 LE, 16 kHz mono) between `mic.start` and `mic.stop` stream into the STT provider; partials
are published as `stt.partial`; on stop the final transcript is published with the stored recording id and, if
the session is submitted, becomes an assistant turn. Each session submits at most once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf

from open_speech.api import schemas as s
from open_speech.api.ws_schema import ErrorMsg, SttFinal, SttPartial
from open_speech.errors import MissingKey
from open_speech.providers.base import Units

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.voice_input")
SAMPLE_RATE = 16000


@dataclass
class MicSession:
    id: str
    mode: str
    context: s.TurnContext
    queue: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    pcm: bytearray = field(default_factory=bytearray)
    partial: str = ""
    final: str | None = None
    task: asyncio.Task[None] | None = None
    finished: bool = False
    provider_id: str = ""
    model: str = ""


class VoiceInputService:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self._active: MicSession | None = None
        self._lock = asyncio.Lock()

    async def start(self, session_id: str, mode: str, context: s.TurnContext) -> None:
        async with self._lock:
            if self._active is not None and not self._active.finished:
                await self._finish(self._active, submit=False)
            stt = self.e.registry.stt()
            if stt is None:
                err = MissingKey("openai", "Speech input")
                err.detail = (
                    "Speech input needs an OpenAI or Gemini key. You can type instead, or paste a key into the bar."
                )
                self.e.events.publish(ErrorMsg(problem=err.problem()))
                self._active = None
                return
            sess = MicSession(
                id=session_id, mode=mode, context=context, provider_id=stt.id, model=getattr(stt, "model", "")
            )
            self._active = sess
            sess.task = asyncio.create_task(self._run_stt(sess, stt))

    def feed(self, data: bytes) -> None:
        sess = self._active
        if sess is None or sess.finished:
            return
        sess.pcm.extend(data)
        sess.queue.put_nowait(data)

    async def stop(self, session_id: str, *, submit: bool = True) -> None:
        async with self._lock:
            sess = self._active
            if sess is None or sess.id != session_id or sess.finished:
                return
            await self._finish(sess, submit=submit)

    async def _pcm_iter(self, sess: MicSession) -> AsyncIterator[bytes]:
        while True:
            chunk = await sess.queue.get()
            if chunk is None:
                return
            yield chunk

    async def _run_stt(self, sess: MicSession, stt: object) -> None:
        try:
            async for ev in stt.stream(self._pcm_iter(sess)):  # type: ignore[attr-defined]
                if ev.kind == "partial":
                    sess.partial = ev.text
                    self.e.events.publish(SttPartial(session_id=sess.id, text=ev.text))
                else:
                    sess.final = ev.text
        except Exception as e:  # noqa: BLE001 — provider failures become a spoken error, never a crash
            log.warning("STT failed: %s", e)
            if sess.final is None:
                sess.final = sess.partial

    async def _finish(self, sess: MicSession, *, submit: bool) -> None:
        sess.finished = True
        sess.queue.put_nowait(None)
        if sess.task is not None:
            try:
                await asyncio.wait_for(sess.task, timeout=15)
            except TimeoutError:
                sess.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sess.task
        if self._active is sess:
            self._active = None
        if not submit:
            return
        text = (sess.final if sess.final is not None else sess.partial).strip()
        duration = len(sess.pcm) / 2 / SAMPLE_RATE
        rec_id = self._save_recording(sess, duration)
        if duration > 0:
            self.e.ledger.record(
                provider=sess.provider_id,
                model=sess.model or "stt",
                op="stt",
                units=Units(audio_sec=round(duration, 2)),
                local=sess.provider_id in ("fixture", "local"),
            )
        self.e.events.publish(
            SttFinal(session_id=sess.id, text=text, recording_id=rec_id, duration_s=round(duration, 2))
        )
        if text:
            await self.e.assistant.submit(
                s.TurnRequest(text=text, source="spoken", session_id=sess.id, recording_id=rec_id, context=sess.context)
            )

    def _save_recording(self, sess: MicSession, duration: float) -> str | None:
        if duration <= 0.05 or not self.e.recordings.keep_recordings():
            return None
        try:
            rec_id, path = self.e.recordings.new_path()
            samples = np.frombuffer(bytes(sess.pcm[: len(sess.pcm) // 2 * 2]), dtype="<i2")
            sf.write(str(path), samples.astype(np.float32) / 32768.0, SAMPLE_RATE, format="OGG", subtype="OPUS")
            self.e.recordings.insert(rec_id, path, kind="user", duration_s=round(duration, 2))
            return rec_id
        except Exception as e:  # noqa: BLE001
            log.warning("could not store recording: %s", e)
            return None
