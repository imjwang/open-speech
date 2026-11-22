"""The one playback transport (03 § Playback transport, 05 § 9, FR-READ-6/8/12/14/17, 07 § Ducking).

Pipeline: utterance audio (cache or synthesis) → decode + resample to 24 kHz → `Renderer` (WSOLA at the
current speed, ~120 ms ring buffer) → `AudioOutput` (sounddevice, or `NullOutput` headless).

Concurrency:
- Every public method runs on the asyncio loop. Synthesis is async (`Synthesizer`), PCM decoding runs in
  a worker thread.
- The audio thread only calls `Renderer.pull()`, which copies from the ring buffer under a short lock and
  never waits on the loop.
- A loop task (the *ticker*, every ~20 ms) reads the played position from the renderer and emits
  `playback.word` events, state/progress, persistence, feeding and prefetch.

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

from open_speech.api.schemas import PlaybackState, PlaybackStatus
from open_speech.api.ws_schema import ErrorMsg, PlaybackStateMsg, PlaybackWord
from open_speech.cache.audio import CachedAudio
from open_speech.config import UserSettings
from open_speech.document.model import Anchor
from open_speech.errors import EngineError
from open_speech.plan.script import SpeechScript, Utterance
from open_speech.playback.output import AudioOutput
from open_speech.playback.render import Renderer, RenderItem
from open_speech.playback.stretch import clamp_speed
from open_speech.playback.synth import Synthesizer
from open_speech.providers.base import WordTiming
from open_speech.providers.pcm import read_audio_file, resample
from open_speech.service.events import EventBus

log = logging.getLogger(__name__)

AnchorResolver = Callable[[Utterance, int, int], Anchor | None]
PositionCallback = Callable[[str, int, int], None]
Intent = Literal["idle", "playing", "paused", "stopped", "error"]
SAMPLE_RATE = 24000
BACK_RESTART_MS = 2000  # "back" within the first 2 s goes to the previous unit, later restarts this one


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
        sample_rate: int = SAMPLE_RATE,
        tick_s: float = 0.02,
        progress_interval_s: float = 1.0,
        position_interval_s: float = 5.0,
        max_parallel_synth: int = 2,
        chars_per_second: float = 15.0,
    ) -> None:
        self.synth = synth
        self.output = output
        self.events = events
        self._settings = settings
        self._on_position = on_position
        self._resolve_anchor = anchor_resolver
        self.sample_rate = sample_rate
        self._tick_s = tick_s
        self._progress_s = progress_interval_s
        self._persist_s = position_interval_s
        self._max_parallel = max(1, max_parallel_synth)
        self._cps = chars_per_second
        self.renderer = Renderer(sample_rate)

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
        self.renderer.pause_reading(True)
        self.renderer.flush(index=self._index, offset=self._ms_to_samples(self._offset_ms))
        self._feed_index = self._index
        self._feed_start = (self._index, self._offset_ms, None)
        self._eos_marked = False
        self._persist()
        if not self.renderer.has_clips():
            self._stop_output()
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
        self.renderer.set_speed(self._speed)  # applied by the render thread within one ~43 ms block
        self._publish_state(force=True)

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

    async def play_clip(self, audio: CachedAudio | Path, *, duck: bool = True) -> None:
        """Play an assistant reply / preview / recording. With `duck`, reading pauses and resumes exactly
        afterwards (07 § Ducking); without it the clip mixes over whatever is playing."""
        self._ensure_ticker()
        if isinstance(audio, CachedAudio):
            pcm = await asyncio.to_thread(self._load_cached, audio)
        else:
            pcm = await asyncio.to_thread(self._load_path, audio)
        hold = f"clip:{id(pcm)}"
        if duck:
            self._holds.add(hold)
            self._apply_hold()
            self._publish_state(force=True)
        self._ensure_output()
        done = self.renderer.play_clip(pcm)
        try:
            await asyncio.to_thread(done.wait)  # set by the render thread at the clip's end, or on close
        finally:
            if duck:
                self._holds.discard(hold)
                self._apply_hold()
                self._publish_state(force=True)
            if self._intent != "playing" and not self.renderer.has_clips():
                self._stop_output()

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
        if not self._output_on or not self.output.running:
            self.output.start(self.sample_rate, self.renderer.pull)
            self._output_on = True
        self.renderer.start()

    def _stop_output(self) -> None:
        if self._output_on:
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
        if p is not None:
            return p.duration_s
        assert self._script is not None
        return max(0.5, len(self._script.utterances[i].text) / self._cps)

    def _at_end(self) -> bool:
        if self._script is None or not self._script.utterances:
            return False
        last = len(self._script.utterances) - 1
        if self._index < last:
            return False
        p = self._prepared_at(last)
        return p is not None and self._offset_ms >= p.duration_s * 1000 - 50

    # ================================================================== positioning

    def _reposition(self, index: int, offset_ms: int, *, resume_char: int | None = None) -> None:
        self._index = index
        self._offset_ms = offset_ms
        self._feed_index = index
        self._feed_start = (index, offset_ms, resume_char)
        self._eos_marked = False
        self._word_cursor = {}
        self.renderer.flush(index=index, offset=self._ms_to_samples(offset_ms))
        self._apply_hold()

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
        running = sum(1 for t in self._tasks.values() if not t.done())
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
                if running >= self._max_parallel:
                    break
                self._start_task(j, key)
                running += 1
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
        try:
            res = await self.synth.synth(text, voice, style=style, document_id=self._doc_id, op="tts")
            pcm = await asyncio.to_thread(self._load_cached, res.audio)
            words = list(res.audio.words)
            self._prepared[key] = _Prepared(res.voice_id, pcm, words, pcm.size / self.sample_rate)
            if res.notice and res.notice != self._notice:
                self._notice = res.notice
                self._publish_state(force=True)
            utts = self._script.utterances if self._script is not None else []
            if res.voice_id != voice and 0 <= index < len(utts) and not utts[index].voice_ref:
                # Fell back (budget, outage, missing key): keep using the fallback voice for the rest of the
                # session so later utterances don't each retry the failing provider. The audio just made is
                # also filed under the new key so the playhead finds it.
                self._voice_override = res.voice_id
                if utts[index].text == text:
                    self._prepared.setdefault(self._key(index), self._prepared[key])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = exc if isinstance(exc, EngineError) else EngineError("provider_error", f"Speech failed: {exc}")
            self._failed[key] = err.detail
            log.warning("synthesis failed for utterance %s: %s", index, err.detail)
            if self._script is not None and 0 <= self._index < len(self._script.utterances) and (
                self._key(self._index) == key
            ):
                self._intent = "error"
                self._error = err.detail
                self.renderer.pause_reading(True)
                self.events.publish(ErrorMsg(problem=err.problem()))
                self._publish_state(force=True)
        finally:
            self._tasks.pop(key, None)
        self._feed()

    def _feed(self) -> None:
        """Hand prepared utterances to the renderer, in order, a few ahead of the playhead."""
        if self._script is None or self._intent not in ("playing", "paused"):
            return
        n = len(self._script.utterances)
        while self._feed_index < n and self._feed_index <= self._index + 2:
            p = self._prepared_at(self._feed_index)
            if p is None:
                break
            start = 0
            if self._feed_start is not None and self._feed_start[0] == self._feed_index:
                _, off_ms, resume_char = self._feed_start
                if resume_char is not None and p.words:
                    t0 = next((w.t0 for w in p.words if w.start >= resume_char), 0.0)
                    off_ms = int(t0 * 1000)
                    self._offset_ms = off_ms
                start = min(self._ms_to_samples(off_ms), p.pcm.size)
                self._feed_start = None
                self.renderer.flush(index=self._feed_index, offset=start)
                self._apply_hold()
                self._skip_words_before(self._feed_index, start / self.sample_rate)
            self.renderer.enqueue(RenderItem(self._feed_index, p.pcm, start))
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
            loading = pos.starved and not self.renderer.queued_indices() and not pos.finished
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
        if not self.renderer.has_clips():
            self._stop_output()
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
        if utt.fidelity != "exact" or not p.words:
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
