"""The one playback transport (03 § Playback transport, 05 § 9, FR-READ-6/8/12/14/17, 07 § Ducking).

Pipeline: utterance audio (cache or synthesis) → decode + resample to 24 kHz → `Renderer` (time-stretch at the
current speed, a bit-exact bypass at 1x; resampled to the device rate; ~0.3 s rings) → `AudioOutput`
(sounddevice at the device's native rate, or `NullOutput` headless).

Concurrency:
- Every public method runs on the asyncio loop. Synthesis is async (`Synthesizer`), at most
  `max_concurrency` per provider at once (Kokoro 1, cloud 3); PCM decoding runs in a worker thread.
- The audio thread only calls `Renderer.pull()`, which copies from the rings under a short lock and
  never waits on the loop.
- A loop task (the *ticker*, every ~20 ms) reads the *audible* position from the renderer (latency-corrected)
  and emits `playback.word` events, state/progress, persistence, feeding and prefetch, and the
  `assistant.speech` started/ended events for everything said through `open_speech()` (never from the audio
  thread; they also work under `NullOutput`).

Speech (replies, `/assistant/say`, previews, recordings): `open_speech(kind, turn_id=…)` returns a
`SpeechHandle` backed by a renderer `ClipStream` (append PCM pieces, `finish()`, `stop()`, `await wait()`).
`started` is emitted when its first sample is heard (first frame handed out + output latency), `ended` when its
last sample has been heard plus `voice_input.echo_tail_ms`. `play_clip` is the one-piece form.

States: idle → (load) paused → (play) loading ⇄ playing ⇄ paused → stopped; error on unrecoverable
synthesis failure. While a duck/clip hold is active the reported state is "paused" and reading resumes
exactly where it was when the hold is released (unless the user paused or stopped meanwhile).

Word anchors: provider word timings are in spoken-text offsets; they are mapped through
`Utterance.segments` to `{block_id, char_start, char_end}` by `default_anchor`. Pass `anchor_resolver`
to decorate anchors (PDF word bboxes, web `dom_anchor`s): it receives (utterance, spoken_start,
spoken_end) and returns an `Anchor` (or None to fall back to the default).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from open_speech.api.schemas import PlaybackState, PlaybackStatus, QueueItem
from open_speech.api.ws_schema import AssistantSpeech, ErrorMsg, PlaybackStateMsg, PlaybackWord
from open_speech.cache.audio import CachedAudio
from open_speech.config import UserSettings
from open_speech.document.model import Anchor
from open_speech.errors import EngineError
from open_speech.i18n import say
from open_speech.i18n import t as tr
from open_speech.plan.script import SpeechScript, Utterance
from open_speech.playback import fade
from open_speech.playback.diagnostics import AudioDiagnostics
from open_speech.playback.output import AudioOutput
from open_speech.playback.render import ClipStream, GrowingPcm, Renderer, RenderItem
from open_speech.playback.stretch import clamp_speed
from open_speech.playback.synth import Synthesizer
from open_speech.providers.base import WordTiming, max_concurrency
from open_speech.providers.pcm import read_audio_file, resample
from open_speech.service.events import EventBus

log = logging.getLogger(__name__)

AnchorResolver = Callable[[Utterance, int, int], Anchor | None]
PositionCallback = Callable[[str, int, int], None]
Intent = Literal["idle", "playing", "paused", "stopped", "error"]
SpeechKind = Literal["reply", "say", "preview", "recording"]
SpeechEnd = Literal["finished", "stopped", "superseded", "error"]
SAMPLE_RATE = 24000
BACK_RESTART_MS = 2000  # "back" within the first 2 s goes to the previous unit, later restarts this one
FEED_AHEAD = 2  # utterances handed to the renderer beyond the playhead


# ------------------------------------------------------------------------------------------ anchors


def default_anchor(utt: Utterance, spoken_start: int, spoken_end: int) -> Anchor | None:
    """Map a spoken-text range to a source anchor through the utterance's segments."""
    segs = [s for s in utt.segments if s.spoken_start < spoken_end and s.spoken_end > spoken_start]
    if not segs:
        return None
    first = segs[0]
    same = [s for s in segs if s.block_id == first.block_id]
    last = same[-1]

    def src(seg_pos: int, s: int, *, end: bool) -> int:
        seg = first if not end else last
        if (seg.spoken_end - seg.spoken_start) == (seg.src_end - seg.src_start):
            return seg.src_start + (seg_pos - seg.spoken_start)
        return seg.src_end if end else seg.src_start  # lexicon replacement: highlight the whole source term

    a = src(max(spoken_start, first.spoken_start), spoken_start, end=False)
    b = src(min(spoken_end, last.spoken_end), spoken_end, end=True)
    return Anchor(block_id=first.block_id, char_start=a, char_end=max(a, b))


def utterance_anchors(utt: Utterance) -> list[Anchor]:
    """The whole utterance (sentence tint): the planner's anchors, else one per block from segments."""
    if utt.anchors:
        return list(utt.anchors)
    spans: dict[str, tuple[int, int]] = {}
    for s in utt.segments:
        a, b = spans.get(s.block_id, (s.src_start, s.src_end))
        spans[s.block_id] = (min(a, s.src_start), max(b, s.src_end))
    if spans:
        return [Anchor(block_id=k, char_start=a, char_end=b) for k, (a, b) in spans.items()]
    return [Anchor(block_id=b) for b in utt.block_ids]


# ------------------------------------------------------------------------------------------ transport


@dataclass
class _Prepared:
    voice_id: str  # voice actually used
    pcm: np.ndarray
    words: list[WordTiming]
    duration_s: float
    # A streaming local voice (Qwen3) delivers an utterance in pieces: `growing` holds what has arrived and is what
    # the renderer plays; `done` once the whole utterance (and its word timings) is in.
    growing: GrowingPcm | None = None
    done: bool = True
    fed: bool = False  # handed to the renderer

    @property
    def source(self) -> np.ndarray | GrowingPcm:
        return self.growing if self.growing is not None else self.pcm

    @property
    def available_s(self) -> float:
        n = self.growing.size if self.growing is not None else self.pcm.size
        return n / SAMPLE_RATE


class SpeechHandle:
    """Something the engine says, played through one renderer `ClipStream`: append PCM in order (gapless),
    `finish()` when there is no more, or `stop()` to fade out. `await wait()` returns once `ended` has been
    emitted (the last sample heard plus the echo tail) with the reason. Loop-thread only."""

    def __init__(self, transport: Transport, stream: ClipStream, kind: SpeechKind, turn_id: str | None,
                 hold: str | None) -> None:
        self._t = transport
        self.stream = stream
        self.kind: SpeechKind = kind
        self.turn_id = turn_id
        self.recording_id: str | None = None
        self.hold = hold
        self.started = False  # `started` emitted
        self.ended = False  # `ended` emitted
        self.reason: SpeechEnd | None = None
        self._stop_reason: SpeechEnd | None = None
        self._heard_end_at: float | None = None
        self._future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def append(self, pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
        """Queue audio (mono float32) to play right after what came before."""
        x = np.asarray(pcm, dtype=np.float32).reshape(-1)
        if sample_rate != SAMPLE_RATE:
            x = resample(x, sample_rate, SAMPLE_RATE)
        self._t._ensure_output()
        self.stream.append(x)

    def finish(self, *, recording_id: str | None = None) -> None:
        if recording_id is not None:
            self.recording_id = recording_id
        self.stream.finish()

    def stop(self, *, reason: SpeechEnd = "stopped", fade_ms: float = fade.STOP_MS) -> None:
        if self.ended or self.stream.ended or self._stop_reason is not None:
            return  # already over (possibly only the echo tail is left): it finished, nothing to stop
        self._stop_reason = reason
        self.stream.stop(fade_ms)

    @property
    def played_ms(self) -> int:
        return self.stream.played_ms

    @property
    def done(self) -> bool:
        return self.ended

    async def wait(self) -> str:
        return await asyncio.shield(self._future)


class Transport:
    def __init__(
        self,
        synth: Synthesizer,
        output: AudioOutput,
        events: EventBus,
        *,
        settings: Callable[[], UserSettings],
        on_position: PositionCallback,
        anchor_resolver: AnchorResolver | None = None,
        on_finished: Callable[[str], None] | None = None,
        queue: Callable[[], list[QueueItem]] | None = None,
        sample_rate: int = SAMPLE_RATE,
        tick_s: float = 0.02,
        progress_interval_s: float = 1.0,
        position_interval_s: float = 5.0,
        chars_per_second: float = 15.0,
    ) -> None:
        self.synth = synth
        self.output = output
        self.events = events
        self._settings = settings
        self._on_position = on_position
        self._resolve_anchor = anchor_resolver
        self._on_finished = on_finished  # the last utterance of a document finished playing
        self._queue = queue  # what reads next (owned by the playback service; shown in every snapshot)
        self.sample_rate = sample_rate
        self._tick_s = tick_s
        self._progress_s = progress_interval_s
        self._persist_s = position_interval_s
        self._cps = chars_per_second
        self.diagnostics = AudioDiagnostics()
        out_diag = getattr(output, "diagnostics", "absent")
        if out_diag is None:
            output.diagnostics = self.diagnostics  # type: ignore[attr-defined]
        self.renderer = Renderer(sample_rate, diagnostics=self.diagnostics)
        if getattr(output, "prepare", "absent") is None:
            # A device output calls this once a stream is open and before it starts (also when it reopens after
            # a device change), so the renderer is already at the stream's rate for the first callback.
            output.prepare = self._prepare_output  # type: ignore[attr-defined]
        self._speech: list[SpeechHandle] = []
        self._caps: dict[str, int] = {}
        self._stop_output_pending = False
        self._quiet_since: float | None = None
        self._last_output_check = 0.0

        self._script: SpeechScript | None = None
        self._doc_id: str | None = None
        self._title: str | None = None
        self._page_count: int | None = None
        self._voice_id: str | None = None
        self._voice_override: str | None = None  # after a fallback, the voice actually in use
        self._style: str | None = None
        self._speed = 1.0
        self._intent: Intent = "idle"
        self._holds: set[str] = set()
        self._loading = False
        self._notice: str | None = None
        self._error: str | None = None
        self._index = 0
        self._offset_ms = 0
        self._feed_index = 0  # next utterance to hand to the renderer
        self._feed_start: tuple[int, int, int | None] | None = None  # (index, offset_ms, resume_char)
        self._eos_marked = False
        self._word_cursor: dict[int, int] = {}
        self._prepared: dict[tuple[str, str, str | None], _Prepared] = {}
        self._tasks: dict[tuple[str, str, str | None], asyncio.Task[None]] = {}
        self._failed: dict[tuple[str, str, str | None], str] = {}
        self._ticker: asyncio.Task[None] | None = None
        self._last_state_sig: tuple[object, ...] | None = None
        self._last_progress = 0.0
        self._last_persist = 0.0
        self._last_prune = 0.0
        self._closed = False
        self._output_on = False
        # Fallback notices ("This month's free Google voice is used up; reading with the local voice.") are
        # shown in the playback state and *spoken* once per session per reason through this hook. The engine
        # sets it (reply speech says the text); the default says nothing.
        self.speak_notice: Callable[[str], None] = lambda _text: None
        self._spoken_notices: set[str] = set()
        self._tinted: set[int] = set()  # utterances shown as a sentence tint while their words weren't known yet
        self._cps_voice: dict[str, float] = {}  # spoken characters per second, learned per voice (growing starts)

    # ================================================================== public API

    async def load(
        self,
        *,
        document_id: str,
        title: str,
        page_count: int | None,
        script: SpeechScript,
        start_index: int = 0,
        offset_ms: int = 0,
        voice_id: str,
        style: str | None,
        speed: float,
    ) -> None:
        """Load a document at a position (no synthesis until `play`, NFR-8)."""
        self._ensure_ticker()
        if self._doc_id is not None and self._doc_id != document_id:
            self._persist()
        self.renderer.pause_reading(True)
        self._script = script
        self._doc_id = document_id
        self._title = title
        self._page_count = page_count
        self._voice_id = voice_id
        self._voice_override = None
        self._style = style
        self._speed = clamp_speed(speed)
        self.renderer.set_speed(self._speed)
        self._notice = None
        self._error = None
        self._prepared.clear()
        self._failed.clear()
        self._intent = "paused" if script.utterances else "idle"
        n = len(script.utterances)
        self._reposition(min(max(0, start_index), max(0, n - 1)), max(0, offset_ms))
        self._publish_state(force=True)

    async def play(self) -> None:
        if self._script is None or not self._script.utterances:
            raise EngineError("invalid_request", "Nothing is loaded to play. Open something to read first.")
        await self.resume()

    async def resume(self) -> None:
        if self._script is None or not self._script.utterances:
            raise EngineError("invalid_request", "Nothing is loaded to play.")
        # An explicit play/resume (from the bar, a hotkey or a command) is the user changing playback: it
        # isn't held back by the open bar (07 § Ducking: "...unless the command changed playback").
        if "duck" in self._holds:
            self._holds.discard("duck")
            self._apply_hold()
        if self._intent == "playing" and self._error is None:
            return
        self._ensure_ticker()
        restart = self._intent in ("stopped", "error") or self._at_end()
        self._error = None
        if self._at_end():
            self._reposition(0, 0)
        elif restart:
            self._reposition(self._index, self._offset_ms)
        self._intent = "playing"
        self._loading = True
        self._ensure_output()
        self._apply_hold()
        self._schedule()
        self._feed()
        self._publish_state(force=True)

    async def pause(self) -> None:
        if self._intent != "playing":
            return
        self._intent = "paused"
        self.renderer.pause_reading(True)
        self._sync_position()
        self._persist()
        self._publish_state(force=True)

    async def stop(self) -> None:
        if self._intent in ("idle", "stopped"):
            return
        self._sync_position()
        self._intent = "stopped"
        self.renderer.pause_reading(True, fade_ms=fade.STOP_MS)
        self.renderer.flush(index=self._index, offset=self._ms_to_samples(self._offset_ms))
        self._feed_index = self._index
        self._feed_start = (self._index, self._offset_ms, None)
        self._eos_marked = False
        self._persist()
        self._stop_output()  # once the 15 ms fade has been heard
        self._publish_state(force=True)

    async def seek(self, utterance_index: int, offset_ms: int = 0) -> None:
        if self._script is None:
            raise EngineError("invalid_request", "Nothing is loaded.")
        n = len(self._script.utterances)
        self._reposition(min(max(0, utterance_index), max(0, n - 1)), max(0, offset_ms))
        self._after_reposition()

    async def skip(self, unit: Literal["sentence", "paragraph", "heading", "seconds"], count: int) -> None:
        if self._script is None or not self._script.utterances:
            raise EngineError("invalid_request", "Nothing is loaded.")
        if count == 0:
            return
        self._sync_position()
        utts = self._script.utterances
        idx, off = self._index, self._offset_ms
        if unit == "sentence":
            target = idx + count + (1 if count < 0 and off > BACK_RESTART_MS else 0)
            await self.seek(target, 0)
        elif unit in ("paragraph", "heading"):
            def pred(u: Utterance) -> bool:
                return u.is_heading if unit == "heading" else (u.paragraph_start or u.is_heading)

            await self.seek(self._unit_target(utts, idx, off, count, pred), 0)
        else:
            target_idx, target_ms = self._seconds_target(count * self._speed)
            await self.seek(target_idx, target_ms)

    async def set_speed(self, speed: float) -> None:
        self._speed = clamp_speed(speed)
        self.renderer.set_speed(self._speed)  # re-rendered from the playhead: heard within ~50 ms
        self._publish_state(force=True)
        if self._intent == "playing" and self._script is not None and self._script.utterances:
            utts = self._script.utterances
            self._check_keeps_up(self._voice_for(utts[min(self._index, len(utts) - 1)]))

    async def set_voice(self, voice_id: str) -> None:
        self.synth.registry.voice(voice_id)  # NotFound for unknown voices
        self._voice_id = voice_id
        self._voice_override = None
        self._notice = None
        if self._script is None:
            return
        self._sync_position()
        self._reposition(self._index, 0, resume_char=self._current_char())
        self._after_reposition()

    def replace_script(self, script: SpeechScript, keep_position: bool = True) -> None:
        """Swap the plan (rule change, re-plan from the playhead). Keeps position when possible."""
        old = self._script
        self._script = script
        if old is None or not script.utterances:
            self._reposition(0, 0)
            self._publish_state(force=True)
            return
        if not keep_position:
            self._reposition(0, 0)
            self._publish_state(force=True)
            return
        self._sync_position()
        old_utts = old.utterances
        new_index, resume_char = self._map_position(old_utts, script.utterances)
        unchanged = (
            new_index == self._index
            and len(script.utterances) >= self._feed_index
            and all(
                i < len(old_utts) and old_utts[i].text == script.utterances[i].text
                for i in range(self._index, max(self._index + 1, self._feed_index))
            )
        )
        if unchanged:
            return  # everything already queued is identical: keep playing without a glitch
        if new_index == self._index and old_utts[self._index].text == script.utterances[new_index].text:
            self._reposition(new_index, self._offset_ms)
        else:
            self._reposition(new_index, 0, resume_char=resume_char)
        self._after_reposition()

    async def play_clip(
        self,
        audio: CachedAudio | Path,
        *,
        duck: bool = True,
        kind: SpeechKind = "recording",
        turn_id: str | None = None,
        recording_id: str | None = None,
    ) -> str:
        """Play an assistant reply / preview / recording / spoken prompt in one piece and wait until it has been
        heard (`assistant.speech` ended). With `duck`, reading fades out and resumes exactly afterwards
        (07 § Ducking); without it the clip mixes over whatever is playing. Returns the end reason."""
        if isinstance(audio, CachedAudio):
            pcm = await asyncio.to_thread(self._load_cached, audio)
        else:
            pcm = await asyncio.to_thread(self._load_path, audio)
        handle = self.open_speech(kind, turn_id=turn_id, duck=duck)
        handle.append(pcm)
        handle.finish(recording_id=recording_id)
        return await handle.wait()

    def open_speech(self, kind: SpeechKind, *, turn_id: str | None = None, duck: bool = True) -> SpeechHandle:
        """Start saying something (see `SpeechHandle`). With `duck`, reading pauses (12 ms fade) until it ends."""
        self._ensure_ticker()
        self._ensure_output()
        stream = self.renderer.open_stream(kind)
        hold = None
        if duck:
            hold = f"speech:{id(stream)}"
            self._holds.add(hold)
            self._apply_hold()
            self._publish_state(force=True)
        handle = SpeechHandle(self, stream, kind, turn_id, hold)
        self._speech.append(handle)
        return handle

    def stop_speech(self, turn_id: str | None = None, *, reason: SpeechEnd = "stopped",
                    fade_ms: float = fade.STOP_MS) -> int:
        """Fade out (15 ms) what is being said — everything, or one turn's speech; `ended` follows with `reason`
        once the fade has been heard. Returns how many were stopped."""
        n = 0
        for h in list(self._speech):
            if h.ended or (turn_id is not None and h.turn_id != turn_id):
                continue
            h.stop(reason=reason, fade_ms=fade_ms)
            n += 1
        return n

    def show_notice(self, text: str, reason: str | None = None) -> None:
        """A fallback notice for what is being read: shown in the playback state (`fallback_notice`) and spoken
        once (see `announce`)."""
        self._notice = text
        self._publish_state(force=True)
        self.announce(text, reason)

    def announce(self, text: str, reason: str | None = None) -> bool:
        """Say a fallback notice through `speak_notice`, once per session per reason (a reason heard once isn't
        repeated for every document or reply; "no_style" is shown only). Returns whether it was handed over."""
        key = reason or text
        if reason == "no_style" or key in self._spoken_notices:
            return False
        self._spoken_notices.add(key)
        try:
            self.speak_notice(text)
        except Exception:  # noqa: BLE001 — a notice must never stop reading
            log.exception("speaking a notice failed")
        return True

    def speaking(self) -> list[SpeechHandle]:
        return [h for h in self._speech if not h.ended]

    @property
    def reading_intended(self) -> bool:
        """Reading is meant to play: it is playing, or it will resume as soon as speech and the bar let go."""
        return self._intent == "playing"

    async def duck(self, on: bool) -> None:
        """Bar opened (on) / closed (off). Reading pauses while ducked and resumes afterwards unless the
        user paused or stopped in the meantime."""
        if on:
            self._holds.add("duck")
        else:
            self._holds.discard("duck")
        self._apply_hold()
        self._publish_state(force=True)

    def snapshot(self) -> PlaybackState:
        utts = self._script.utterances if self._script else []
        utt = utts[self._index] if 0 <= self._index < len(utts) else None
        return PlaybackState(
            state=self._status(),
            document_id=self._doc_id,
            document_title=self._title,
            utterance_id=utt.id if utt else None,
            utterance_index=self._index,
            utterance_count=len(utts),
            offset_ms=int(self._offset_ms),
            speed=self._speed,
            voice_id=self._voice_override or self._voice_id,
            time_left_s=self._time_left() if utts else None,
            page=utt.page if utt else None,
            page_count=self._page_count,
            heading=(utt.heading_path[-1] if utt and utt.heading_path else None),
            progress=round(self._progress(), 4) if utts else 0.0,
            fallback_notice=self._notice,
            error=self._error,
            queue=self._queue() if self._queue else [],
        )

    def position(self) -> tuple[str | None, int, int]:
        return self._doc_id, self._index, int(self._offset_ms)

    @property
    def script(self) -> SpeechScript | None:
        return self._script

    async def close(self) -> None:
        if self._closed:
            return
        self._sync_position()
        self._persist()
        self._closed = True
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ticker
        for t in list(self._tasks.values()):
            t.cancel()
        self.renderer.close()
        for h in list(self._speech):
            self._end_speech(h, h._stop_reason or "stopped")
        with contextlib.suppress(Exception):
            self.output.close()

    # ================================================================== state helpers

    def _status(self) -> PlaybackStatus:
        if self._intent == "error":
            return "error"
        if self._intent == "playing":
            if self._holds:
                return "paused"
            return "loading" if self._loading else "playing"
        return self._intent

    def _active(self) -> bool:
        return self._intent == "playing" and not self._holds

    def _apply_hold(self) -> None:
        self.renderer.pause_reading(not self._active())

    def _ensure_output(self) -> None:
        """Start the device (at its native rate; the renderer resamples) and the render thread."""
        self._stop_output_pending = False
        self._quiet_since = None
        if not self._output_on or not self.output.running:
            pref = getattr(self.output, "preferred_rate", None)
            rate = int(pref()) if callable(pref) else self.sample_rate
            self.renderer.configure(rate)
            self.output.start(rate, self.renderer.pull)
            actual = int(getattr(self.output, "rate", rate) or rate)
            if actual != self.renderer.rate:
                self.renderer.configure(actual)
            self.renderer.set_output_latency(int(getattr(self.output, "latency_frames", 0) or 0))
            info = getattr(self.output, "info", None)
            if callable(info):
                details = info()
                self.diagnostics.device = details.get("device")
                self.diagnostics.mode = str(details.get("mode", ""))
                self.diagnostics.latency_ms = float(details.get("latency_ms", 0.0))
            self._output_on = True
        self.renderer.start()

    def _prepare_output(self, rate: int, latency_frames: int) -> None:
        self.renderer.configure(rate)
        self.renderer.set_output_latency(latency_frames)

    def _stop_output(self) -> None:
        """Stop the device once nothing is left to hear: the ticker waits until every fade has been handed out
        and the output latency has passed (a stop never cuts the last 15 ms)."""
        if self._output_on:
            self._stop_output_pending = True

    def _tick_output(self, now: float) -> None:
        if self._output_on:
            check = getattr(self.output, "check", None)
            if callable(check) and now - self._last_output_check >= 1.0:
                self._last_output_check = now
                try:
                    if check():
                        self.renderer.configure(int(getattr(self.output, "rate", self.renderer.rate)))
                    self.renderer.set_output_latency(int(getattr(self.output, "latency_frames", 0) or 0))
                except Exception:
                    log.exception("checking the audio output failed")
        if not self._stop_output_pending:
            return
        if self._active() or self.speaking() or not self.renderer.quiet():
            self._quiet_since = None
            if self._active() or self.speaking():
                self._stop_output_pending = False
            return
        if self._quiet_since is None:
            self._quiet_since = now
        _, lat = self.renderer.clock()
        if now - self._quiet_since >= lat / max(1, self.renderer.rate) + 0.03:
            self._stop_output_pending = False
            self._quiet_since = None
            with contextlib.suppress(Exception):
                self.output.stop()
            self._output_on = False

    def _ensure_ticker(self) -> None:
        if self._ticker is None or self._ticker.done():
            self.renderer.start()
            self._ticker = asyncio.get_running_loop().create_task(self._tick_loop(), name="transport-ticker")

    def _ms_to_samples(self, ms: float) -> int:
        return int(ms * self.sample_rate / 1000)

    def _voice_for(self, utt: Utterance) -> str:
        return utt.voice_ref or self._voice_override or self._voice_id or ""

    def _key(self, i: int) -> tuple[str, str, str | None]:
        assert self._script is not None
        u = self._script.utterances[i]
        return (u.text, self._voice_for(u), self._style)

    def _prepared_at(self, i: int) -> _Prepared | None:
        if self._script is None or not (0 <= i < len(self._script.utterances)):
            return None
        return self._prepared.get(self._key(i))

    def _duration_est(self, i: int) -> float:
        p = self._prepared_at(i)
        if p is not None and p.done:
            return p.duration_s
        assert self._script is not None
        utt = self._script.utterances[i]
        est = max(0.5, len(utt.text) / self._cps_voice.get(self._voice_for(utt), self._cps))
        return max(est, p.available_s) if p is not None else est

    def _at_end(self) -> bool:
        if self._script is None or not self._script.utterances:
            return False
        last = len(self._script.utterances) - 1
        if self._index < last:
            return False
        p = self._prepared_at(last)
        return p is not None and p.done and self._offset_ms >= p.duration_s * 1000 - 50

    # ================================================================== positioning

    def _reposition(self, index: int, offset_ms: int, *, resume_char: int | None = None) -> None:
        self._index = index
        self._offset_ms = offset_ms
        self._feed_index = index
        self._feed_start = (index, offset_ms, resume_char)
        self._eos_marked = False
        self._word_cursor = {}
        self._tinted.clear()
        for p in self._prepared.values():
            p.fed = False
        self.renderer.flush(index=index, offset=self._ms_to_samples(offset_ms))
        self._apply_hold()
        self._cancel_unneeded(index)

    def _cancel_unneeded(self, index: int) -> None:
        """After a jump, stop syntheses nobody will hear soon on voices that say so (`cancel_on_seek`: Qwen3 runs
        one generation at a time, seconds per sentence, so the new position would otherwise wait for it)."""
        if self._script is None:
            return
        n = len(self._script.utterances)
        keep = {self._key(j) for j in range(index, min(n, index + FEED_AHEAD + 1))}  # what `_feed` may hand over
        me = asyncio.current_task()  # called from a synthesis task's own callback (a fallback): never cancel it
        for key, task in list(self._tasks.items()):
            if key in keep or task.done() or task is me:
                continue
            prov = self._provider_obj(key[1])
            if getattr(prov, "cancel_on_seek", False):
                task.cancel()
                self._tasks.pop(key, None)
                stale = self._prepared.get(key)
                if stale is not None and not stale.done:
                    del self._prepared[key]  # a half-made utterance: made again (identically) when needed

    def _provider_obj(self, voice_id: str) -> object | None:
        try:
            return self.synth.registry.provider(self._provider_of(voice_id))
        except Exception:  # noqa: BLE001
            return None

    def _after_reposition(self) -> None:
        if self._intent == "playing":
            self._loading = True
        self._schedule()
        self._feed()
        self._publish_state(force=True)

    def _sync_position(self) -> None:
        if self._script is None:
            return
        pos = self.renderer.snapshot()
        for idx in pos.ended:
            self._emit_remaining(idx)
        if pos.index is not None and pos.index >= self._index and pos.index < len(self._script.utterances):
            if pos.index != self._index:
                self._emit_remaining(self._index)
                self._index = pos.index
            self._offset_ms = int(pos.offset * 1000 / self.sample_rate)

    def _current_char(self) -> int | None:
        """Spoken-text offset of the word at the playhead (to resume there with another voice)."""
        p = self._prepared_at(self._index)
        if p is None or not p.words:
            return None
        t = self._offset_ms / 1000
        for w in p.words:
            if w.t1 > t:
                return w.start
        return p.words[-1].start

    def _map_position(self, old: list[Utterance], new: list[Utterance]) -> tuple[int, int | None]:
        """Find where the current playhead lives in a new plan, by source anchor."""
        if not (0 <= self._index < len(old)):
            return 0, None
        cur = old[self._index]
        char = self._current_char()
        anchor = default_anchor(cur, char, char + 1) if char is not None else None
        if anchor is None and cur.segments:
            s = cur.segments[0]
            anchor = Anchor(block_id=s.block_id, char_start=s.src_start, char_end=s.src_end)
        if anchor is not None and anchor.block_id is not None and anchor.char_start is not None:
            for i, u in enumerate(new):
                for s in u.segments:
                    if s.block_id == anchor.block_id and s.src_start <= anchor.char_start < s.src_end:
                        spoken = s.spoken_start + (anchor.char_start - s.src_start)
                        return i, min(spoken, s.spoken_end)
            for i, u in enumerate(new):
                if anchor.block_id in u.block_ids:
                    return i, None
        for i, u in enumerate(new):
            if set(cur.block_ids) & set(u.block_ids):
                return i, None
        return min(self._index, len(new) - 1), None

    def _unit_target(
        self, utts: list[Utterance], idx: int, off: int, count: int, pred: Callable[[Utterance], bool]
    ) -> int:
        starts = [i for i, u in enumerate(utts) if pred(u)]
        if count > 0:
            after = [i for i in starts if i > idx]
            return after[min(count, len(after)) - 1] if after else idx  # no further unit: stay put
        cur_start = max((i for i in starts if i <= idx), default=None)
        if cur_start is None:
            return 0
        steps = -count
        if idx != cur_start or off > BACK_RESTART_MS:
            steps -= 1  # inside a unit, the first "back" returns to its start
            if steps == 0:
                return cur_start
        before = [i for i in starts if i < cur_start]
        if not before:
            return 0
        return before[max(0, len(before) - steps)]

    def _seconds_target(self, delta_s: float) -> tuple[int, int]:
        assert self._script is not None
        n = len(self._script.utterances)
        idx = self._index
        t = self._offset_ms / 1000 + delta_s
        while t < 0 and idx > 0:
            idx -= 1
            t += self._duration_est(idx)
        if t < 0:
            return 0, 0
        while idx < n - 1 and t >= self._duration_est(idx):
            t -= self._duration_est(idx)
            idx += 1
        return idx, int(min(t, self._duration_est(idx)) * 1000)

    # ================================================================== synthesis, feeding, prefetch

    def _schedule(self) -> None:
        """Prefetch ~prefetch_seconds of wall time ahead, never more than max_ahead_seconds of audio.
        Nothing new starts unless reading is playing (pause/stop let in-flight work finish only)."""
        if self._script is None or self._intent != "playing" or self._closed:
            return
        s = self._settings().playback
        target = min(s.prefetch_seconds * self._speed, float(s.max_ahead_seconds))
        n = len(self._script.utterances)
        ahead = 0.0
        # A local voice is synthesized on this machine's CPU, so a second synthesis running next to the one
        # the listener is waiting for only delays it. Until the playhead has audio it runs alone (NFR-1);
        # cloud voices wait on the network instead and keep prefetching in parallel.
        head = self._key(self._index) if self._index < n else None
        head_alone = head is not None and head not in self._prepared and self._local_voice(head[1])
        running_by: dict[str, int] = {}
        for k, t in self._tasks.items():
            if not t.done():
                p = self._provider_of(k[1])
                running_by[p] = running_by.get(p, 0) + 1
        for j in range(self._index, n):
            key = self._key(j)
            est = self._duration_est(j)
            remaining = est - (self._offset_ms / 1000 if j == self._index else 0.0)
            must = j <= self._index + 1
            if key in self._prepared or key in self._tasks or key in self._failed and j != self._index:
                ahead += max(0.0, remaining)
            else:
                if not must and (ahead >= target or ahead + est > s.max_ahead_seconds):
                    break
                if j != self._index and head_alone:
                    break
                # Each provider runs at most its `max_concurrency` syntheses at once (Kokoro shares this
                # machine's CPU with the audio path; cloud voices wait on the network). After a skip the playhead
                # may run next to prefetch that is still finishing, one over the cap.
                prov = self._provider_of(key[1])
                if running_by.get(prov, 0) >= self._cap(key[1]) + (1 if j == self._index else 0):
                    break
                self._start_task(j, key)
                running_by[prov] = running_by.get(prov, 0) + 1
                ahead += max(0.0, remaining)
            if ahead >= target and not must:
                break

    def _start_task(self, index: int, key: tuple[str, str, str | None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside the loop (replace_script from a thread): the ticker schedules it
        self._failed.pop(key, None)
        self._tasks[key] = loop.create_task(self._prepare(index, key), name=f"synth-{index}")

    def _prune(self) -> None:
        """Drop decoded audio far from the playhead (memory stays ~ the prefetch window)."""
        if self._script is None:
            self._prepared.clear()
            return
        n = len(self._script.utterances)
        lo, hi = max(0, self._index - 2), min(n, max(self._feed_index, self._index) + 40)
        keep = {self._key(i) for i in range(lo, hi)}
        for k in [k for k in self._prepared if k not in keep]:
            del self._prepared[k]

    async def _prepare(self, index: int, key: tuple[str, str, str | None]) -> None:
        text, voice, style = key

        def fresh(pcm: np.ndarray, sr: int, words: list[WordTiming], voice_id: str) -> None:
            # Newly synthesized audio, before it is encoded and cached: play it now rather than after the
            # Opus encode and a decode of the file just written (NFR-1, time to first audio).
            if sr == self.sample_rate:
                self._adopt(index, key, voice_id, np.asarray(pcm, dtype=np.float32), words)

        def piece(pcm: np.ndarray, sr: int, voice_id: str, offset: int) -> None:
            # A streaming voice's audio as it is generated: the utterance may start before it is finished.
            if sr == self.sample_rate:
                self._grow(index, key, voice_id, pcm, offset)

        try:
            res = await self.synth.synth(text, voice, style=style, document_id=self._doc_id, op="tts", on_audio=fresh,
                                         on_chunk=piece)
            if key not in self._prepared:  # a cache hit, or audio at another sample rate: read the stored file
                pcm = await asyncio.to_thread(self._load_cached, res.audio)
                self._adopt(index, key, res.voice_id, pcm, list(res.audio.words))
            if res.notice and res.notice != self._notice:
                self.show_notice(res.notice, res.notice_reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = exc if isinstance(exc, EngineError) else EngineError("provider_error", f"Speech failed: {exc}")
            self._failed[key] = err.detail
            log.warning("synthesis failed for utterance %s: %s", index, err.detail)
            stale = self._prepared.get(key)
            if stale is not None and not stale.done:
                # A streaming voice failed part-way: end what was handed to the renderer (it plays the part it has
                # and moves on instead of waiting forever) and forget it, so it is retried or reported at the head.
                if stale.growing is not None:
                    stale.growing.finish()
                del self._prepared[key]
            if self._script is not None and 0 <= self._index < len(self._script.utterances) and (
                self._key(self._index) == key
            ):
                self._intent = "error"
                self._error = err.detail
                self.renderer.pause_reading(True)
                self.events.publish(ErrorMsg(problem=err.problem()))
                self._publish_state(force=True)
        finally:
            if self._tasks.get(key) is asyncio.current_task():  # not a newer task started for the same key
                del self._tasks[key]
        self._feed()

    def _adopt(
        self, index: int, key: tuple[str, str, str | None], voice_id: str, pcm: np.ndarray, words: list[WordTiming]
    ) -> None:
        """Audio for `key` is ready: remember it, hand it to the renderer, and let prefetch continue."""
        text, voice, _style = key
        old = self._prepared.get(key)
        prepared = _Prepared(voice_id, pcm, words, pcm.size / self.sample_rate)
        if old is not None and old.growing is not None and not old.done:
            if old.voice_id == voice_id and old.growing.size <= pcm.size:
                g = old.growing
                if pcm.size > g.size:
                    g.append(pcm[g.size :])
                g.finish()  # the renderer plays to the end of what it has been playing
                prepared.growing, prepared.fed = g, old.fed
            elif old.fed:
                g = old.growing
                g.finish()
                self._replace_fed(index)
        if prepared.duration_s > 0.5 and pcm.size:
            v = self._cps_voice.get(voice_id)
            cps = len(text) / prepared.duration_s
            self._cps_voice[voice_id] = cps if v is None else 0.7 * v + 0.3 * cps
        self._prepared[key] = prepared
        self._check_keeps_up(voice_id)
        utts = self._script.utterances if self._script is not None else []
        if voice_id != voice and 0 <= index < len(utts) and not utts[index].voice_ref:
            # Fell back (budget, outage, missing key): keep using the fallback voice for the rest of the
            # session so later utterances don't each retry the failing provider. The audio just made is
            # also filed under the new key so the playhead finds it.
            self._voice_override = voice_id
            if utts[index].text == text:
                self._prepared.setdefault(self._key(index), self._prepared[key])
        self._feed()
        self._schedule()

    def _grow(self, index: int, key: tuple[str, str, str | None], voice_id: str, pcm: np.ndarray,
              offset: int) -> None:
        """A piece of a streaming voice's utterance arrived (`offset` = samples before it in this attempt)."""
        p = self._prepared.get(key)
        if p is not None and p.done:
            return
        if p is None or p.growing is None or p.voice_id != voice_id:
            if offset > 0 and (p is None or p.voice_id == voice_id):
                return  # its beginning is gone (pruned while generating): the whole audio comes with `_adopt`
            if p is not None and p.growing is not None and p.fed:
                p.growing.finish()
                self._replace_fed(index)
            p = _Prepared(voice_id, np.zeros(0, dtype=np.float32), [], 0.0,
                          growing=GrowingPcm(on_change=self.renderer.notify), done=False)
            self._prepared[key] = p
        g = p.growing
        assert g is not None
        have = g.size
        if offset + pcm.size <= have:
            return  # a retry re-delivering audio already here (the same seed gives the same audio)
        g.append(pcm[max(0, have - offset) :])
        p.duration_s = g.size / self.sample_rate
        self._feed()

    def _replace_fed(self, index: int) -> None:
        """Audio already handed to the renderer for `index` was replaced by another voice's (a crash and a
        fallback mid-utterance): play again from the current utterance, restarting it if it is the one replaced."""
        self._sync_position()
        offset = 0 if index == self._index else self._offset_ms
        self._reposition(self._index, offset)
        if self._intent == "playing":
            self._loading = True

    def _ready_to_start(self, i: int, p: _Prepared, start_s: float) -> bool:
        """Whether a growing utterance may be handed to the renderer: the audio already here (plus what will be
        made while earlier audio plays) covers the projected shortfall `max(0.8 s, remaining x (1 - rtf /
        (speed x 1.1)))`, so it plays through without a gap; whole audio always may."""
        if p.done:
            return True
        assert self._script is not None
        rtf = self._realtime_factor(p.voice_id)
        speed = max(0.1, self._speed)
        avail = p.available_s - start_s
        remaining = max(0.0, self._duration_est(i) - p.available_s)
        need = max(0.8, remaining * (1.0 - rtf / (speed * 1.1)))
        # Audio still queued before this utterance plays first, and generation goes on meanwhile.
        lead = 0.0
        for j in range(self._index, i):
            q = self._prepared_at(j)
            if q is None:
                return False
            lead += max(0.0, q.duration_s - (self._offset_ms / 1000 if j == self._index else 0.0)) / speed
        return avail + lead * rtf >= need

    def _realtime_factor(self, voice_id: str) -> float:
        prov = self._provider_obj(voice_id)
        fn = getattr(prov, "realtime_factor", None)
        if not callable(fn):
            return 10.0
        try:
            return float(fn(voice_id.split(":")[1] if voice_id.count(":") >= 2 else ""))
        except Exception:  # noqa: BLE001
            return 1.0

    def _check_keeps_up(self, voice_id: str) -> None:
        """Say once when a local voice measurably can't generate as fast as the listener listens (RTF below speed x
        1.1): "This voice can't keep up at 2 times speed on this Mac…" (Qwen3 on a small Mac)."""
        if not self._local_voice(voice_id):
            return
        prov = self._provider_obj(voice_id)
        measured = getattr(prov, "rtf_measured", None)
        if not callable(measured):
            return
        model = voice_id.split(":")[1] if voice_id.count(":") >= 2 else ""
        try:
            if not measured(model):
                return
            rtf = float(getattr(prov, "realtime_factor")(model))  # noqa: B009
            smallest = bool(getattr(prov, "smallest", lambda _m: True)(model))
        except Exception:  # noqa: BLE001
            return
        if rtf >= self._speed * 1.1:
            return
        key = "notice.slow_voice_small" if smallest else "notice.slow_voice"
        self.announce(tr(key, speed=say.decimal(self._speed)), "slow_voice")

    @staticmethod
    def _provider_of(voice_id: str) -> str:
        return voice_id.split(":", 1)[0]

    def _cap(self, voice_id: str) -> int:
        prov_id = self._provider_of(voice_id)
        cap = self._caps.get(prov_id)
        if cap is None:
            try:
                prov = self.synth.registry.provider(prov_id)
            except Exception:  # noqa: BLE001
                prov = None
            cap = max_concurrency(prov) if prov is not None else 3
            self._caps[prov_id] = cap
        return cap

    def _local_voice(self, voice_id: str) -> bool:
        try:
            return bool(self.synth.registry.is_local(voice_id))
        except Exception:  # noqa: BLE001 — unknown voice: synthesis reports it; treat it as remote here
            return False

    def _feed(self) -> None:
        """Hand prepared utterances to the renderer, in order, a few ahead of the playhead."""
        if self._script is None or self._intent not in ("playing", "paused"):
            return
        n = len(self._script.utterances)
        while self._feed_index < n and self._feed_index <= self._index + FEED_AHEAD:
            p = self._prepared_at(self._feed_index)
            if p is None:
                break
            pending_start = self._feed_start is not None and self._feed_start[0] == self._feed_index
            if not p.done:
                # A growing utterance: resuming at a word needs its timings (wait for the end), otherwise start
                # once enough of it is here to play through.
                if pending_start and self._feed_start is not None and self._feed_start[2] is not None:
                    break
                start_s = self._feed_start[1] / 1000 if pending_start and self._feed_start is not None else 0.0
                if not self._ready_to_start(self._feed_index, p, start_s):
                    break
            start = 0
            if self._feed_start is not None and self._feed_start[0] == self._feed_index:
                _, off_ms, resume_char = self._feed_start
                if resume_char is not None and p.words:
                    t0 = next((w.t0 for w in p.words if w.start >= resume_char), 0.0)
                    off_ms = int(t0 * 1000)
                    self._offset_ms = off_ms
                start = min(self._ms_to_samples(off_ms), p.source.size)
                self._feed_start = None
                self.renderer.flush(index=self._feed_index, offset=start)
                self._apply_hold()
                self._skip_words_before(self._feed_index, start / self.sample_rate)
            self.renderer.enqueue(RenderItem(self._feed_index, p.source, start))
            p.fed = True
            self._feed_index += 1
        if self._feed_index >= n and not self._eos_marked:
            self.renderer.mark_end_of_stream()
            self._eos_marked = True

    def _load_cached(self, audio: CachedAudio) -> np.ndarray:
        return resample(audio.load_pcm(), audio.sample_rate, self.sample_rate)

    def _load_path(self, path: Path) -> np.ndarray:
        x, sr = read_audio_file(path)
        return resample(x, sr, self.sample_rate)

    # ================================================================== ticker

    async def _tick_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closed:
            try:
                self._tick(loop.time())
            except Exception:
                log.exception("transport tick failed")
            await asyncio.sleep(self._tick_s)

    def _tick(self, now: float) -> None:
        self._tick_speech(now)
        self._tick_output(now)
        if self._script is None or not self._script.utterances:
            return
        utts = self._script.utterances
        pos = self.renderer.snapshot()
        changed = False
        for idx in pos.ended:
            self._emit_remaining(idx)
        if self._active():
            if pos.index is not None and self._index <= pos.index < len(utts):
                if pos.index != self._index:
                    self._emit_remaining(self._index)
                    self._index = pos.index
                    changed = True
                self._offset_ms = int(pos.offset * 1000 / self.sample_rate)
            self._emit_words(self._index, self._offset_ms / 1000)
            # Waiting for audio: nothing left to render, or a growing utterance outrun by the playhead.
            loading = pos.starved and not pos.finished and (
                not self.renderer.queued_indices() or self.renderer.waiting_for_synthesis())
            if loading != self._loading:
                self._loading = loading
                changed = True
            if pos.finished:
                self._finish()
                return
        self._feed()
        self._schedule()
        if now - self._last_prune >= 2.0:
            self._prune()
            self._last_prune = now
        if self._active() and now - self._last_persist >= self._persist_s:
            self._persist()
            self._last_persist = now
        if changed or self._active() and now - self._last_progress >= self._progress_s:
            self._publish_state(force=True)
        else:
            self._publish_state()

    def _finish(self) -> None:
        assert self._script is not None
        last = len(self._script.utterances) - 1
        self._emit_remaining(last)
        p = self._prepared_at(last)
        self._index = last
        self._offset_ms = int(p.duration_s * 1000) if p else self._offset_ms
        self._intent = "stopped"
        self._loading = False
        self.renderer.pause_reading(True)
        self._persist()
        self._stop_output()
        self._publish_state(force=True)
        if self._on_finished is not None and self._doc_id is not None:
            self._on_finished(self._doc_id)

    # ================================================================== speech events

    def _tick_speech(self, now: float) -> None:
        """`assistant.speech`: started when a stream's first sample is heard, ended when its last sample has been
        heard plus the echo tail (the bar keeps its microphone closed in between)."""
        if not self._speech:
            return
        dev, lat = self.renderer.clock()
        heard = dev - lat
        running = self._output_on and self.output.running
        tail = self._settings().voice_input.echo_tail_ms / 1000
        for h in list(self._speech):
            s = h.stream
            if not h.started and s.first_frame is not None and (heard >= s.first_frame or not running):
                h.started = True
                self.events.publish(AssistantSpeech(state="started", kind=h.kind, turn_id=h.turn_id))
            if not s.ended:
                continue
            if h._heard_end_at is None:
                last = s.last_frame if s.last_frame is not None else dev
                if heard >= last or not running:
                    h._heard_end_at = now
                else:
                    continue
            if now - h._heard_end_at >= tail or not h.started:
                # A stop that came too late to cut anything (the stream had already played out) doesn't count.
                reason: SpeechEnd = (h._stop_reason or "stopped") if s.reason == "stopped" else "finished"
                self._end_speech(h, reason)

    def _end_speech(self, h: SpeechHandle, reason: SpeechEnd) -> None:
        if h.ended:
            return
        h.ended = True
        h.reason = reason
        with contextlib.suppress(ValueError):
            self._speech.remove(h)
        self.events.publish(
            AssistantSpeech(state="ended", kind=h.kind, turn_id=h.turn_id, reason=reason,
                            recording_id=h.recording_id, played_ms=h.played_ms)
        )
        if not h._future.done():
            h._future.set_result(reason)
        if h.hold is not None:
            self._holds.discard(h.hold)
            self._apply_hold()
            self._publish_state(force=True)
        if self._intent != "playing" and not self.speaking():
            self._stop_output()

    def audio_diagnostics(self) -> dict[str, object]:
        """`GET /api/v1/diagnostics/audio`: what the audio path counted since it started."""
        snap = self.diagnostics.snapshot()
        snap["reading_buffered_ms"] = round(1000 * self.renderer.buffered_frames() / max(1, self.renderer.rate), 1)
        snap["speed"] = self._speed
        snap["output_running"] = bool(self._output_on and self.output.running)
        snap["speaking"] = len(self.speaking())
        return snap

    def publish_state(self) -> None:
        """Tell clients the snapshot changed for a reason the transport can't see (e.g. the queue)."""
        self._publish_state(force=True)

    # ================================================================== word events

    def _skip_words_before(self, index: int, t: float) -> None:
        p = self._prepared_at(index)
        if p is None:
            return
        k = 0
        while k < len(p.words) and p.words[k].t1 <= t:
            k += 1
        self._word_cursor[index] = k

    def _emit_remaining(self, index: int) -> None:
        if self._script is None or not (0 <= index < len(self._script.utterances)):
            return
        self._emit_words(index, float("inf"))

    def _emit_words(self, index: int, t: float) -> None:
        if self._script is None or self._doc_id is None or not (0 <= index < len(self._script.utterances)):
            return
        utt = self._script.utterances[index]
        p = self._prepared_at(index)
        if p is None:
            return
        cursor = self._word_cursor.get(index, 0)
        if index in self._tinted and p.done and p.words and utt.fidelity == "exact":
            # The utterance was tinted while it was still being generated; its words are known now: pick up the
            # word highlight at the playhead.
            self._tinted.discard(index)
            self._skip_words_before(index, t)
            cursor = self._word_cursor.get(index, 0)
        elif utt.fidelity != "exact" or not p.words:
            if cursor == 0 and t >= 0:
                anchors = utterance_anchors(utt)
                if anchors:
                    self.events.publish(
                        PlaybackWord(
                            document_id=self._doc_id,
                            utterance_id=utt.id,
                            utterance_index=index,
                            anchor=anchors[0],
                            sentence_anchors=anchors,
                            fidelity=utt.fidelity,
                            spoken_start=0,
                            spoken_end=len(utt.text),
                        )
                    )
                self._word_cursor[index] = 1
                if not p.done:
                    self._tinted.add(index)
            return
        sentence = None
        while cursor < len(p.words) and p.words[cursor].t0 <= t:
            w = p.words[cursor]
            cursor += 1
            anchor = None
            if self._resolve_anchor is not None:
                try:
                    anchor = self._resolve_anchor(utt, w.start, w.end)
                except Exception:
                    log.exception("anchor resolver failed")
            if anchor is None:
                anchor = default_anchor(utt, w.start, w.end)
            if sentence is None:
                sentence = utterance_anchors(utt)
            if anchor is None:
                if not sentence:
                    continue
                anchor = sentence[0]
            self.events.publish(
                PlaybackWord(
                    document_id=self._doc_id,
                    utterance_id=utt.id,
                    utterance_index=index,
                    anchor=anchor,
                    sentence_anchors=sentence,
                    fidelity=utt.fidelity,
                    spoken_start=w.start,
                    spoken_end=w.end,
                )
            )
        self._word_cursor[index] = cursor

    # ================================================================== reporting

    def _chars(self) -> tuple[int, int]:
        """(consumed, total) spoken characters at the playhead."""
        assert self._script is not None
        utts = self._script.utterances
        total = sum(len(u.text) for u in utts) or 1
        done = sum(len(u.text) for u in utts[: self._index])
        if 0 <= self._index < len(utts):
            dur = self._duration_est(self._index)
            frac = min(1.0, (self._offset_ms / 1000) / dur) if dur > 0 else 0.0
            done += int(len(utts[self._index].text) * frac)
        return done, total

    def _progress(self) -> float:
        done, total = self._chars()
        return min(1.0, done / total)

    def _time_left(self) -> float:
        done, total = self._chars()
        return round(max(0, total - done) / self._cps / max(self._speed, 0.1), 1)

    def _publish_state(self, *, force: bool = False) -> None:
        snap = self.snapshot()
        sig = (snap.state, snap.document_id, snap.utterance_index, snap.speed, snap.voice_id, snap.fallback_notice,
               snap.error, snap.utterance_count)
        if not force and sig == self._last_state_sig:
            return
        self._last_state_sig = sig
        self._last_progress = asyncio.get_running_loop().time() if _loop_running() else 0.0
        self.events.publish(PlaybackStateMsg(playback=snap))

    def _persist(self) -> None:
        if self._doc_id is None:
            return
        try:
            self._on_position(self._doc_id, self._index, int(self._offset_ms))
        except Exception:
            log.exception("position persistence failed")


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True
