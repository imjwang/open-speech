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
from open_speech.i18n import resolve_language, set_default_language
from open_speech.providers.base import LlmCall
from open_speech.service.events import EventBus
from open_speech.store.db import Database

log = logging.getLogger("open_speech.engine")

WARM_AFTER_IDLE_S = 60.0  # re-open the Anthropic connection when the bar opens after this long without a call


class Engine:
    def __init__(
        self,
        env: EngineEnv | None = None,
        *,
        output: Any | None = None,
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
        from open_speech.providers.keys import ProviderKeys
        from open_speech.providers.registry import ProviderRegistry
        from open_speech.rules.files import RuleStore
        from open_speech.service.assistant import AssistantService
        from open_speech.service.clipboard import ClipboardReader
        from open_speech.service.clones import CloneService
        from open_speech.service.documents import DocumentService
        from open_speech.service.jobs import Jobs
        from open_speech.service.misc import ModelService, RecordingService, UsageService, VoiceService
        from open_speech.service.playback import PlaybackService
        from open_speech.service.reply_speech import ReplySpeech
        from open_speech.service.rules import RuleService
        from open_speech.service.settings import SettingsService
        from open_speech.service.settings_migrations import run_settings_migrations
        from open_speech.service.voice_input import VoiceInputService
        from open_speech.store.assets import AssetStore
        from open_speech.store.pages import PageStore
        from open_speech.usage.budget import BudgetGuard
        from open_speech.usage.free_tier import FreeTierGuard, FreeTierRegistry
        from open_speech.usage.ledger import Ledger
        from open_speech.usage.pricing import Pricing
        from open_speech.usage.watch import UsageWatcher

        self.started_at = time.monotonic()
        self.env = env or EngineEnv()
        self.paths = Paths(self.env.home).ensure()
        self.db = Database(self.paths.db)
        self.events = EventBus()
        self.settings = SettingsService(self.paths, self.env)
        run_settings_migrations(self.settings, self.db)  # before anything reads a setting
        self._agent_runner_factory = agent_runner_factory

        # runtime (workstream B)
        self.catalog = ModelCatalog.load(self.paths)
        self.keys = ProviderKeys(self.env)
        self.registry = ProviderRegistry(self.env, self.paths, self.catalog, self.keys, settings=self.settings.get,
                                         kv=self.db)
        self.pricing = Pricing.load(self.paths)
        self.ledger = Ledger(self.db, self.pricing)
        self.budget = BudgetGuard(self.ledger, self.settings.get, self.events)  # alerts after every ledger row
        self.free_tiers = FreeTierRegistry()  # providers with a free monthly allowance register here
        # Google's free Chirp characters (pricing.toml `free_chars_per_month`): priced by the ledger, enforced here.
        self.free_tier = FreeTierGuard(self.ledger, self.settings.get, self.events, available=self._provider_usable)
        self.free_tiers.register(self.free_tier)
        self.ledger.add_listener(self.free_tier.after_record)
        self.registry.on_voices_changed = self._voices_changed  # e.g. Google's voices start (or stop) working
        self.assets = AssetStore(self.paths.cache, self.db)
        self.audio_cache = AudioCache(self.assets)
        self.synth = Synthesizer(
            self.registry, self.audio_cache, self.ledger, self.budget, self.settings.get, self.events,
            free_tier=self.free_tier,
        )
        self.model_manager = LocalModelManager(
            self.paths, self.catalog, self.events, on_change=self._models_changed
        )
        if output is None:
            output = self._default_output()
        self.transport = Transport(
            self.synth,
            output,
            self.events,
            settings=self.settings.get,
            on_position=self._on_position,
            anchor_resolver=self._resolve_anchor,
            on_finished=lambda document_id: self.playback.document_finished(document_id),
            queue=lambda: self.playback.queue(),
        )
        self.pages = PageStore(self.assets, allow_private=self.env.open_speech_allow_private_urls)

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
        # Every reply, prompt and spoken notice goes through one sentence-by-sentence voice (07 § Reply speech).
        self.reply_speech = ReplySpeech(self)
        self.transport.speak_notice = self.reply_speech.notice
        self.usage = UsageService(self)
        self.models = ModelService(self)
        self.recordings = RecordingService(self)
        self.jobs = Jobs(self.events)  # clone and design jobs (model downloads keep their own: model_manager)
        self.clones = CloneService(self)  # cloned and designed voices; registers itself as Qwen3's clone store
        self.voice_input = VoiceInputService(self)
        self.usage_watcher = UsageWatcher(self.usage.budget, self.ledger.month_key, self.events)
        self.ledger.add_listener(self.usage_watcher.poke)
        self.settings.on_change(lambda old, new: self.usage_watcher.poke() if old.budget != new.budget else None)
        self._bg: list[asyncio.Task[Any]] = []
        self._warming: asyncio.Task[Any] | None = None
        self._ensure_default_voice()
        set_default_language(resolve_language(self.settings.get().language))
        self.settings.on_change(self._settings_changed)

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
                # The word's own range inside the block's element, not the whole block.
                base = block.dom_anchor.start_offset
                anchor.dom_anchor = block.dom_anchor.model_copy(
                    update={"start_offset": base + cs, "end_offset": base + max(ce, cs)}
                )
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

    def _models_changed(self, _model: str) -> None:
        """A local model was installed or removed: voices and listening may have just become possible."""
        from open_speech.api.ws_schema import CapabilitiesChanged

        self.registry.refresh()
        self.events.publish(CapabilitiesChanged(capabilities=self.capabilities()))

    def _provider_usable(self, provider: str) -> bool:
        """The build can use this provider's voices now (Google: a key the API doesn't refuse)."""
        if provider == "google":
            return self.registry.google_usable()
        return self.keys.configured(provider)

    def _voices_changed(self) -> None:
        """Which cloud voices can be used changed (the Google check): tell clients, refresh the spend chip."""
        from open_speech.api.ws_schema import CapabilitiesChanged

        self.events.publish(CapabilitiesChanged(capabilities=self.capabilities()))
        self.usage_watcher.poke()

    def _free_tier_policy_month(self, new: Any, old: Any | None = None) -> None:
        """"Keep paying for the Google voice" holds for one billing month. Turning it on (a card, a hand edit, the
        assistant) stamps the current month, whatever month an earlier "pay" left behind; a "pay" that was already
        on from an earlier month goes back to "local". `old` is None at startup (nothing was just turned on)."""
        pb = new.playback
        allowance = next(iter(self.pricing.free_allowances()), None)
        if allowance is None or pb.beyond_free_tier != "pay":
            return
        month = self.ledger.free_period(allowance).key
        if pb.beyond_free_tier_month == month:
            return
        just_turned_on = old is not None and old.playback.beyond_free_tier != "pay"
        patch: dict[str, Any] = (
            {"beyond_free_tier_month": month}
            if just_turned_on or pb.beyond_free_tier_month is None
            else {"beyond_free_tier": "local", "beyond_free_tier_month": None}
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.settings.replace(new.model_copy(update={"playback": pb.model_copy(update=patch)}))
            return
        loop.create_task(self.settings.patch({"playback": patch}))

    def _settings_changed(self, old: Any, new: Any) -> None:
        """settings.toml changed (voice command, API, hand edit): speak the new language and tell clients."""
        from open_speech.api.ws_schema import CapabilitiesChanged, SettingsChanged

        language = set_default_language(resolve_language(new.language))
        self.events.publish(SettingsChanged(settings=new, language=language))
        if old.playback.beyond_free_tier != new.playback.beyond_free_tier or (
            old.playback.beyond_free_tier_month != new.playback.beyond_free_tier_month
        ):
            self._free_tier_policy_month(new, old)
            self.usage_watcher.poke()
        if old.voice_input.stt_provider != new.voice_input.stt_provider or old.language != new.language:
            self.registry.refresh()
            self.events.publish(CapabilitiesChanged(capabilities=self.capabilities()))

    def effective_theme(self) -> str:
        """The theme on screen: the chosen one, or what "system" means on this Mac right now."""
        theme = self.settings.get().appearance.theme
        if theme != "system":
            return theme
        import subprocess
        import sys

        if sys.platform == "darwin":
            try:
                out = subprocess.run(
                    ["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=2
                )
                return "ember" if "dark" in out.stdout.lower() else "lavender"
            except (OSError, subprocess.SubprocessError):
                pass
        return "lavender"

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
        self.ledger.record(  # the budget guard and the usage watcher hear about it from the ledger
            provider=provider,
            model=call.model,
            op=op,
            units=call.units,
            document_id=document_id,
            turn_id=turn_id,
            local=provider == "fixture",
        )

    def has_agent(self) -> bool:
        return self._agent_runner_factory is not None or self.keys.configured("anthropic")

    def agent_runner(self) -> Any | None:
        if self._agent_runner_factory is not None:
            return self._agent_runner_factory()
        client = self.registry.anthropic_client()
        if client is None:
            return None
        from open_speech.providers.anthropic_agent import AnthropicAgentRunner

        return AnthropicAgentRunner(client=client, catalog=self.catalog)

    def warm_llm(self) -> None:
        """The bar opened. If nothing has been sent to Anthropic for over a minute, re-open a pooled
        connection in the background with a free Models API call, so the turn the user is about to speak
        doesn't wait for TCP and TLS."""
        if self._agent_runner_factory is not None or self.registry.anthropic_idle_s() <= WARM_AFTER_IDLE_S:
            return
        if self._warming is not None and not self._warming.done():
            return
        if self.registry.anthropic_client() is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._warming = loop.create_task(self.registry.warm_anthropic(self.settings.get().llm.model))

    async def stop_speech(self, turn_id: str | None = None, *, reason: Any = "stopped") -> None:
        """Stop whatever the engine is saying (barge-in by key): everything, or one turn's speech. It fades out
        over ~15 ms and `assistant.speech` ended(reason="stopped") follows once the fade has been heard. Reply
        speech (service/reply_speech.py) also cancels its pending synthesis here."""
        stopper = getattr(self, "reply_speech", None)
        if stopper is not None:
            await stopper.stop(turn_id, reason=reason)
        self.transport.stop_speech(turn_id, reason=reason)

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.events.bind_loop(loop)

        def on_rules_changed() -> None:
            loop.create_task(self.rules.rules_changed("rule file edited"))

        self.usage_watcher.start(loop)
        self._free_tier_policy_month(self.settings.get())  # a "keep paying" from an earlier month ends
        google = self.registry.google_check()
        if google is not None:  # is Cloud Text-to-Speech allowed for this build's key? (free; re-checked later)
            google.scheduled = True  # "auto" waits briefly for its first answer (VoiceService.settle_auto)
            self._bg.append(asyncio.create_task(google.run()))
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
        # The CLI writes engine.json; remove it here (uvicorn re-raises SIGTERM after shutdown, so code after
        # server.run() never runs) so clients don't find a stale port/token after quit.
        with contextlib.suppress(Exception):
            import json
            import os

            if json.loads(self.paths.engine_json.read_text()).get("pid") == os.getpid():
                self.paths.engine_json.unlink()
        self.usage_watcher.stop()
        tasks = [*self._bg, *([self._warming] if self._warming is not None else [])]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        await self.assistant.wait_idle(5)
        await self.jobs.close()
        await self.reply_speech.close()
        await self.playback.close()
        with contextlib.suppress(Exception):
            await self.transport.close()
        await self.registry.aclose()
        self.db.close()

    def capabilities(self) -> s.Capabilities:
        from open_speech.config import LANGUAGES
        from open_speech.providers.registry import KEYED_TTS

        stt = self.registry.stt()
        if stt is not None:
            speech = s.SpeechInput(
                available=True,
                provider=stt.id,
                local=stt.id in ("local", "fixture"),
                model=getattr(stt, "model", None) or None,
            )
        else:
            reason, install_model = self.registry.stt_unavailable()
            speech = s.SpeechInput(available=False, reason=reason, install_model=install_model)
        return s.Capabilities(
            speech_input=speech,
            assistant=self.has_agent(),
            ai_cleanup=self.registry.llm() is not None,
            vision=self.registry.vision() is not None,
            cloud_voices=[p for p in KEYED_TTS if self._provider_usable(p)],
            languages=list(LANGUAGES),
            language=resolve_language(self.settings.get().language),
        )

    def health(self) -> s.Health:
        return s.Health(
            ok=True,
            version=__version__,
            uptime_s=round(time.monotonic() - self.started_at, 1),
            fixture_providers=self.env.open_speech_fixture_providers,
        )
