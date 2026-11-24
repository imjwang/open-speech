"""Every reply spoken, sentence by sentence (07 § Reply speech).

One stream at a time: `open(turn_id, voice, language)` returns a `ReplyStream`; opening another supersedes it
(it fades out and ends with reason "superseded"). A stream takes text as it is written (`feed`), cuts it into
units (`speakable.Splitter`: the first one after the first sentence, or sooner), and synthesizes ahead, up to
the voice provider's `max_concurrency` at once. Audio goes to one transport `SpeechHandle` strictly in order,
whichever unit finishes first: fresh PCM as soon as the provider returns it, or the cached audio decoded. A
unit that fails is skipped and logged; a reply none of whose units could be said ends with reason "error".

The transport handle is opened with the first audio, so a turn that ends up saying nothing (reading resumes
instead) makes no sound and no events. Once the turn has said it will speak (`will_speak()`, which is what
`assistant.done.speaks` reports), an `assistant.speech` ended event is always sent, also when no audio came.

The whole reply is saved as one recording (`kind="reply"`, when recordings are kept). Its id is set when the
last unit has been handed over, so the `ended` event carries it; `on_end` callbacks then record it on the
turn. `stop()` cancels pending synthesis and fades out what is playing.

`/assistant/say` (kind "say") and spoken notices use the same path. A notice (`Transport.speak_notice`:
"The Google voice isn't available; reading with the local voice.") joins the reply being said, as its own
unit, or is said on its own once the current speech has ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from open_speech.api.ws_schema import AssistantSpeech
from open_speech.errors import EngineError
from open_speech.i18n import current_language
from open_speech.providers.base import max_concurrency
from open_speech.providers.pcm import encode_ogg_opus, read_audio_file, resample
from open_speech.service.speakable import Splitter, speakable

if TYPE_CHECKING:
    from open_speech.playback.transport import SpeechEnd, SpeechHandle, SpeechKind
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.reply_speech")

RATE = 24000
EndCallback = Callable[["ReplyStream", str], None]


def _at_rate(pcm: np.ndarray, sr: int) -> np.ndarray:
    return resample(np.asarray(pcm, dtype=np.float32).reshape(-1), sr, RATE)


@dataclass(eq=False)
class _Unit:
    index: int
    text: str = ""
    pcm: np.ndarray | None = None  # 24 kHz mono, once it exists
    failed: bool = False
    task: asyncio.Task[None] | None = None
    recording_id: str | None = None  # audio that already is a recording (a reply said again)

    @property
    def settled(self) -> bool:
        return self.pcm is not None or self.failed


class ReplyStream:
    """One reply (or prompt) being said. Loop-thread only. See the module docstring."""

    def __init__(
        self,
        owner: ReplySpeech,
        *,
        turn_id: str | None,
        voice: str,
        language: str,
        kind: SpeechKind,
        on_end: EndCallback | None = None,
    ) -> None:
        self._o = owner
        self.e = owner.e
        self.turn_id = turn_id
        self.voice = voice
        self.language = language
        self.kind: SpeechKind = kind
        self._loop = asyncio.get_running_loop()
        self._splitter = Splitter(language)
        self._units: list[_Unit] = []
        self._next = 0  # next unit to hand to the transport
        self._handle: SpeechHandle | None = None
        self._handed_all = False  # the transport has been told there is no more
        self._finished = False  # finish() was called: no more text
        self.closed = False  # stopped or ended: nothing more is synthesized or played
        self.promised = False  # the client was told audio is coming (`will_speak`)
        self.reason: str | None = None
        self.recording_id: str | None = None
        self._parts: list[np.ndarray] = []  # what was handed over, for the recording
        self._sem = asyncio.Semaphore(owner.concurrency(voice))
        self._timer: asyncio.TimerHandle | None = None
        self._done: asyncio.Future[str] = self._loop.create_future()
        self._on_end: list[EndCallback] = [on_end] if on_end is not None else []
        self.opened_at = self._loop.time()
        self.first_audio_at: float | None = None  # when the first PCM was handed to the transport

    # ------------------------------------------------------------------ public

    @property
    def accepting(self) -> bool:
        """More can still be added before the end of this stream."""
        return not self.closed and not self._handed_all

    def feed(self, text: str) -> None:
        """More of the reply, as it is written (any size: a token, a sentence, all of it)."""
        if not text or self.closed or self._finished:
            return
        self._add(self._splitter.feed(text, self._loop.time()))
        self._arm()

    def insert(self, text: str) -> bool:
        """Say `text` as its own unit, after what has been cut so far (a notice). False when too late."""
        if not self.accepting:
            return False
        self._add(self._splitter.flush(self._loop.time()))
        said = speakable(text, self.language)
        if said:
            self._add([said])
        self._arm()
        return True

    def insert_audio(self, pcm: np.ndarray, sample_rate: int, *, recording_id: str | None = None) -> bool:
        """Play ready-made audio in order (a voice preview, a reply said again). False when too late."""
        if not self.accepting:
            return False
        self._add(self._splitter.flush(self._loop.time(), partial=True))  # what was written before it comes first
        self._arm()
        unit = _Unit(len(self._units), pcm=_at_rate(pcm, sample_rate), recording_id=recording_id)
        self._units.append(unit)
        self._pump()
        return True

    def finish(self) -> None:
        """No more text: say the rest, then end."""
        if self._finished or self.closed:
            return
        self._finished = True
        self._cancel_timer()
        self._add(self._splitter.finish())
        self._pump()

    def will_speak(self) -> bool:
        """Audio will be heard (or is being heard). From then on an `ended` event is guaranteed."""
        if self.closed:
            return False
        speaks = (
            self._handle is not None
            or any(not u.failed for u in self._units)
            or self._splitter.pending()
        )
        if speaks:
            self.promised = True
        return speaks

    def stop(self, reason: SpeechEnd = "stopped") -> None:
        """Fade out what is playing (~15 ms) and drop the rest, including synthesis not yet done."""
        if self.closed:
            return
        self.closed = True
        self._cancel_pending()
        if self._handle is not None and not self._handle.done:
            self._handle.stop(reason=reason)  # `ended` follows once the fade has been heard (`_watch`)
        else:
            self._end(reason)

    def on_end(self, callback: EndCallback) -> None:
        if self._done.done():
            callback(self, self.reason or "finished")
        else:
            self._on_end.append(callback)

    async def wait(self) -> str:
        """The end reason, once it has ended (heard to the last sample plus the echo tail, or stopped)."""
        return await asyncio.shield(self._done)

    @property
    def ended(self) -> bool:
        return self._done.done()

    # ------------------------------------------------------------------ units

    def _add(self, texts: list[str]) -> None:
        for text in texts:
            if not text.strip():
                continue
            unit = _Unit(len(self._units), text=text)
            self._units.append(unit)
            unit.task = self._loop.create_task(self._synth(unit))
        self._pump()

    async def _synth(self, unit: _Unit) -> None:
        try:
            async with self._sem:
                if self.closed:
                    return
                voice = self.voice

                def fresh(pcm: np.ndarray, sr: int, _words: Any, _voice_id: str) -> None:
                    if unit.pcm is None and not self.closed:
                        unit.pcm = _at_rate(pcm, sr)
                        self._pump()

                res = await self.e.synth.synth(unit.text, voice, turn_id=self.turn_id, op="tts", on_audio=fresh)
            if self.closed:
                return
            if res.voice_id != voice:  # a fallback (offline, refused, free characters used up): the rest follows
                self.voice = res.voice_id
            self._o.announce(res)
            if unit.pcm is None:  # cached, or shared with another caller: decode the stored audio
                x, sr = await asyncio.to_thread(read_audio_file, res.audio.path)
                unit.pcm = _at_rate(x, sr)
        except asyncio.CancelledError:
            raise
        except EngineError as err:
            log.info("a reply sentence was not spoken: %s", err.detail)
            unit.failed = True
        except Exception:
            log.exception("a reply sentence could not be synthesized")
            unit.failed = True
        self._pump()

    def _pump(self) -> None:
        """Hand every settled unit at the head of the line to the transport, in order."""
        if self.closed:
            return
        while self._next < len(self._units) and self._units[self._next].settled:
            unit = self._units[self._next]
            self._next += 1
            if unit.pcm is None or unit.pcm.size == 0:
                continue
            if self._handle is None:
                self._open_handle()
            assert self._handle is not None
            self._handle.append(unit.pcm, RATE)
            self._parts.append(unit.pcm)
            if self.first_audio_at is None:
                self.first_audio_at = self._loop.time()
        if self._finished and not self._handed_all and self._next == len(self._units):
            self._hand_over_end()

    def _open_handle(self) -> None:
        self._handle = self.e.transport.open_speech(self.kind, turn_id=self.turn_id, duck=True)
        self._o.track(self._loop.create_task(self._watch(self._handle)))

    async def _watch(self, handle: SpeechHandle) -> None:
        reason = await handle.wait()
        self._end(reason)

    def _hand_over_end(self) -> None:
        """Every unit has been handed over: the transport ends the speech after the last sample."""
        self._handed_all = True
        if self._handle is None:  # nothing was said
            failed = any(u.failed for u in self._units)
            self._end("error" if failed else "finished")
            return
        rec_id = self._recording()
        self.recording_id = rec_id
        self._handle.finish(recording_id=rec_id)

    def _recording(self) -> str | None:
        """The whole reply as one recording (replies only, when recordings are kept)."""
        if self.kind != "reply" or not self._parts or not self._o.keep_recordings():
            return None
        said = [u for u in self._units if u.pcm is not None and u.pcm.size]
        if len(said) == 1 and said[0].recording_id:  # "say that again": the same recording
            return said[0].recording_id
        rec_id, path = self.e.recordings.new_path()
        pcm = np.concatenate(self._parts)
        self._o.track(self._loop.create_task(self._save(rec_id, path, pcm)))
        return rec_id

    async def _save(self, rec_id: str, path: Path, pcm: np.ndarray) -> None:
        try:
            data, _ = await asyncio.to_thread(encode_ogg_opus, pcm, RATE)
            await asyncio.to_thread(path.write_bytes, data)
            self.e.recordings.insert(rec_id, path, kind="reply", duration_s=pcm.size / RATE, turn_id=self.turn_id)
        except Exception:
            log.exception("saving the reply recording failed")

    # ------------------------------------------------------------------ ending

    def _end(self, reason: str) -> None:
        if self._done.done():
            return
        self.closed = True
        self._cancel_pending()
        self.reason = reason
        if self._handle is None and self.promised:
            # Audio was promised but none came: the client still hears that it is over.
            self.e.events.publish(
                AssistantSpeech(state="ended", kind=self.kind, turn_id=self.turn_id, reason=reason, played_ms=0)  # type: ignore[arg-type]
            )
        self._done.set_result(reason)
        for cb in self._on_end:
            try:
                cb(self, reason)
            except Exception:
                log.exception("reply speech end callback failed")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _cancel_pending(self) -> None:
        """Drop synthesis not yet done. A unit whose audio already exists is left to finish being stored, so
        it is cached and on the ledger like any other audio that was made."""
        self._cancel_timer()
        for unit in self._units:
            if unit.task is not None and not unit.task.done() and unit.pcm is None:
                unit.task.cancel()

    def _arm(self) -> None:
        """Wake up when the splitter may hand out a unit without more text (a pause while a tool runs)."""
        self._cancel_timer()
        deadline = self._splitter.deadline()
        if deadline is not None and not self.closed and not self._finished:
            self._timer = self._loop.call_at(deadline, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if self.closed or self._finished:
            return
        self._add(self._splitter.ready(self._loop.time()))
        self._arm()


class ReplySpeech:
    """The engine's one voice for replies, prompts and notices (`engine.reply_speech`)."""

    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.current: ReplyStream | None = None
        self._notices: list[str] = []
        self._notice_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def open(
        self,
        turn_id: str | None,
        voice: str,
        language: str,
        *,
        kind: SpeechKind = "reply",
        on_end: EndCallback | None = None,
    ) -> ReplyStream:
        """Start saying something; whatever was being said is superseded."""
        old = self.current
        if old is not None and not old.closed:
            old.stop("superseded")
        self.current = ReplyStream(self, turn_id=turn_id, voice=voice, language=language, kind=kind, on_end=on_end)
        return self.current

    def active(self, turn_id: str | None = None) -> ReplyStream | None:
        """The stream still taking audio, for this turn (or any)."""
        s = self.current
        if s is None or not s.accepting or (turn_id is not None and s.turn_id != turn_id):
            return None
        return s

    async def stop(self, turn_id: str | None = None, *, reason: SpeechEnd = "stopped") -> int:
        """Stop the reply being said (this turn's, or any) and its pending synthesis."""
        s = self.current
        if s is None or s.closed or (turn_id is not None and s.turn_id != turn_id):
            return 0
        s.stop(reason)
        return 1

    async def say(self, text: str, *, voice: str, language: str, kind: SpeechKind = "say") -> str:
        """Say `text` and wait until it has been heard. Returns the end reason."""
        stream = self.open(None, voice, language, kind=kind)
        stream.feed(text)
        stream.finish()
        return await stream.wait()

    def notice(self, text: str) -> None:
        """`Transport.speak_notice`: said with the reply being spoken, or on its own after what is playing."""
        s = self.current
        if s is not None and s.insert(text):
            return
        self._notices.append(text)
        if self._notice_task is None or self._notice_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._notice_task = loop.create_task(self._say_notices())
            self.track(self._notice_task)

    async def _say_notices(self) -> None:
        while self._notices:
            s = self.current
            if s is not None and s.accepting:
                for text in self._notices:
                    s.insert(text)
                self._notices.clear()
                return
            if s is not None and not s.ended:
                with contextlib.suppress(Exception):
                    await s.wait()
                continue
            text = " ".join(self._notices)
            self._notices.clear()
            language = current_language()
            try:
                await self.say(text, voice=self.voice_for(language), language=language)
            except Exception:
                log.exception("a spoken notice failed")

    # ------------------------------------------------------------------ helpers

    def voice_for(self, language: str) -> str:
        """The assistant voice in `language` (without saying why "auto" chose it: the caller does)."""
        voice, _reason = self.e.voices.reply_voice(language)
        return self.e.voices.for_language(voice, language)

    def concurrency(self, voice: str) -> int:
        try:
            provider = self.e.registry.provider(voice.split(":", 1)[0])
        except Exception:  # noqa: BLE001
            provider = None
        return max_concurrency(provider) if provider is not None else 3

    def keep_recordings(self) -> bool:
        return bool(self.e.recordings.keep_recordings())

    def announce(self, res: Any) -> None:
        if getattr(res, "notice", None):
            self.e.transport.announce(res.notice, getattr(res, "notice_reason", None))

    def track(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        s = self.current
        if s is not None and not s.closed:
            s.stop("stopped")
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


__all__ = ["ReplySpeech", "ReplyStream"]
