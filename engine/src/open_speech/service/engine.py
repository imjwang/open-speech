"""Composition root: builds every component once and exposes the use-case services the surfaces call (03 § Shape:
all behavior lives in `service/`; `api/`, `cli/` and `mcp/` are thin adapters)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from open_speech import __version__
from open_speech.api import schemas as s
from open_speech.config import EngineEnv, Paths
from open_speech.providers.base import LlmCall
from open_speech.service.events import EventBus
from open_speech.store.db import Database

log = logging.getLogger("open_speech.engine")


class Engine:
    def __init__(
        self,
        env: EngineEnv | None = None,
        *,
        output: Any | None = None,
        use_keychain: bool = False,
        agent_runner_factory: Any | None = None,
    ) -> None:
        from open_speech.cache.audio import AudioCache
        from open_speech.cache.extract import ExtractCache
        from open_speech.cache.plans import PlanCache
        from open_speech.local.catalog import ModelCatalog
        from open_speech.local.manager import LocalModelManager
        from open_speech.plan.planner import Planner
        from open_speech.playback.synth import Synthesizer
        from open_speech.playback.transport import Transport
        from open_speech.providers.keys import KeyStore
        from open_speech.providers.registry import ProviderRegistry
        from open_speech.rules.files import RuleStore
        from open_speech.service.assistant import AssistantService
        from open_speech.service.clipboard import ClipboardReader
        from open_speech.service.documents import DocumentService
        from open_speech.service.misc import KeyService, ModelService, RecordingService, UsageService, VoiceService
        from open_speech.service.playback import PlaybackService
        from open_speech.service.rules import RuleService
        from open_speech.service.settings import SettingsService
        from open_speech.service.voice_input import VoiceInputService
        from open_speech.usage.budget import BudgetGuard
        from open_speech.usage.ledger import Ledger
        from open_speech.usage.pricing import Pricing

        self.started_at = time.monotonic()
        self.env = env or EngineEnv()
        self.paths = Paths(self.env.home).ensure()
        self.db = Database(self.paths.db)
        self.events = EventBus()
        self.settings = SettingsService(self.paths, self.env)
        self._agent_runner_factory = agent_runner_factory

        # runtime (workstream B)
        self.catalog = ModelCatalog.load(self.paths)
        self.keystore = KeyStore(self.env, use_keychain=use_keychain)
        self.registry = ProviderRegistry(self.env, self.paths, self.catalog, self.keystore)
        self.pricing = Pricing.load(self.paths)
        self.ledger = Ledger(self.db, self.pricing)
        self.budget = BudgetGuard(self.ledger, self.settings.get, self.events)
        self.audio_cache = AudioCache(self.paths, self.db)
        self.synth = Synthesizer(
            self.registry, self.audio_cache, self.ledger, self.budget, self.settings.get, self.events
        )
        self.model_manager = LocalModelManager(
            self.paths, self.catalog, self.events, on_change=lambda _model: self.registry.refresh()
        )
        if output is None:
            output = self._default_output()
        self.transport = Transport(
            self.synth,
            output,
            self.events,
            settings=self.settings.get,
            on_position=self._on_position,
            **self._transport_extras(),
        )

        # pipeline (workstream A)
        self.extract_cache = ExtractCache(self.paths)
        self.plan_cache = PlanCache(self.db)
        self.rule_store = RuleStore(self.paths)
        self.rule_store.ensure_defaults()
        self.planner = Planner(self.plan_cache)

        # use cases
        self.clipboard = ClipboardReader(self.paths.home if self.env.open_speech_fixture_providers else None)
        self.documents = DocumentService(self)
        self.playback = PlaybackService(self)
        self.voices = VoiceService(self)
        self.rules = RuleService(self)
        self.assistant = AssistantService(self)
        self.usage = UsageService(self)
        self.keys = KeyService(self)
        self.models = ModelService(self)
        self.recordings = RecordingService(self)
        self.voice_input = VoiceInputService(self)
        self._bg: list[asyncio.Task[Any]] = []
        self._ensure_default_voice()

    # ------------------------------------------------------------------ wiring helpers

    def _default_output(self) -> Any:
        from open_speech.playback.output import NullOutput

        if self.env.open_speech_audio_output == "null":
            return NullOutput()
        try:
            from open_speech.playback.output import SoundDeviceOutput

            return SoundDeviceOutput()
        except Exception as e:  # noqa: BLE001 — no PortAudio (Linux CI) → headless real-time clock output
            log.info("audio output unavailable (%s); using NullOutput", e)
            return NullOutput()

    def _transport_extras(self) -> dict[str, Any]:
        import inspect

        from open_speech.playback.transport import Transport

        params = inspect.signature(Transport.__init__).parameters
        extras: dict[str, Any] = {}
        if "anchor_resolver" in params:
            extras["anchor_resolver"] = self._resolve_anchor
        return extras

    def _resolve_anchor(self, utterance: Any, spoken_start: int, spoken_end: int) -> Any:
        """Map a spoken-text word range to a source anchor, adding PDF word bboxes and web DOM anchors."""
        from open_speech.document.model import Anchor

        seg = next(
            (g for g in utterance.segments if g.spoken_start <= spoken_start < max(g.spoken_end, g.spoken_start + 1)),
            None,
        )
        if seg is None:
            return utterance.anchors[0] if utterance.anchors else Anchor()
        if seg.spoken_end - seg.spoken_start == seg.src_end - seg.src_start:
            off = spoken_start - seg.spoken_start
            cs = seg.src_start + off
            ce = min(seg.src_end, seg.src_start + (spoken_end - seg.spoken_start))
        else:  # respelled span (lexicon/replace): highlight the whole source span
            cs, ce = seg.src_start, seg.src_end
        anchor = Anchor(block_id=seg.block_id, char_start=cs, char_end=ce)
        doc_id = self.playback.current_document_id() if hasattr(self, "playback") else None
        if doc_id:
            try:
                block = self.documents.load(doc_id).block(seg.block_id)
            except Exception:  # noqa: BLE001
                return anchor
            if block.dom_anchor is not None:
                anchor.dom_anchor = block.dom_anchor
            boxes = [w for w in block.word_boxes if w.start < ce and w.end > cs]
            if boxes:
                b0 = boxes[0].bbox
                anchor.bbox = b0.model_copy(
                    update={
                        "x0": min(w.bbox.x0 for w in boxes),
                        "y0": min(w.bbox.y0 for w in boxes),
                        "x1": max(w.bbox.x1 for w in boxes),
                        "y1": max(w.bbox.y1 for w in boxes),
                    }
                )
            elif block.bbox is not None:
                anchor.bbox = block.bbox
        return anchor

    def _ensure_default_voice(self) -> None:
        """If the configured default voice isn't usable (e.g. Kokoro weights not installed yet, or fixture mode),
        reading still works with the best local voice; the setting itself is left alone."""
        st = self.settings.get()
        voices = {v.id: v for v in self.registry.voices()}
        v = voices.get(st.defaults.voice)
        if self.env.open_speech_fixture_providers and (v is None or not v.available):
            fallback = self.registry.default_local_voice()
            st.defaults.voice = fallback
            st.defaults.assistant_voice = fallback

    def _on_position(self, document_id: str, utterance_index: int, offset_ms: int) -> None:
        with contextlib.suppress(Exception):
            self.documents.save_position(document_id, utterance_index, offset_ms)

    def record_llm(self, op: str, call: LlmCall, *, document_id: str | None = None, turn_id: str | None = None) -> None:
        provider = "fixture" if call.model.startswith("fixture") else "anthropic"
        self.ledger.record(
            provider=provider,
            model=call.model,
            op=op,
            units=call.units,
            document_id=document_id,
            turn_id=turn_id,
            local=provider == "fixture",
        )
        self.budget.after_record()

    def has_agent(self) -> bool:
        return self._agent_runner_factory is not None or bool(self.keystore.get("anthropic"))

    def agent_runner(self) -> Any | None:
        if self._agent_runner_factory is not None:
            return self._agent_runner_factory()
        key = self.keystore.get("anthropic")
        if not key:
            return None
        from open_speech.providers.anthropic_agent import AnthropicAgentRunner

        return AnthropicAgentRunner(key)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)

        def on_rules_changed() -> None:
            loop.create_task(self.rules.rules_changed("rule file edited"))

        self._bg.append(asyncio.create_task(self.rule_store.watch(on_rules_changed)))
        self._bg.append(asyncio.create_task(self.recordings.prune_loop()))
        self._bg.append(asyncio.create_task(self._cache_janitor()))

    async def _cache_janitor(self) -> None:
        while True:
            await asyncio.sleep(600)
            with contextlib.suppress(Exception):
                max_bytes = int(self.settings.get().cache.max_gb * 1e9)
                self.audio_cache.evict(max_bytes, pinned=self._pinned_audio_keys())

    def _pinned_audio_keys(self) -> set[str]:
        return set()

    async def shutdown(self) -> None:
        for t in self._bg:
            t.cancel()
        for t in self._bg:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.assistant.wait_idle(5)
        await self.playback.close()
        with contextlib.suppress(Exception):
            await self.transport.close()
        self.db.close()

    def health(self) -> s.Health:
        return s.Health(
            ok=True,
            version=__version__,
            uptime_s=round(time.monotonic() - self.started_at, 1),
            fixture_providers=self.env.open_speech_fixture_providers,
        )
