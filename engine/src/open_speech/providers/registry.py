"""Provider registry: one voice list across providers (FR-VOICE-1), adapter construction from keys and the
catalog, STT routing (07), and the local/fixture defaults.

Adapters are created lazily and cached; `refresh()` drops them (called when a key is rejected or a local
model is installed). A provider this build has no key for contributes no voices at all: users never see
something they'd need a key to use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from open_speech.config import DEFAULT_VOICE, EngineEnv, Paths, UserSettings
from open_speech.errors import EngineError, NotFound, Unavailable
from open_speech.i18n import t
from open_speech.ids import split_voice_id
from open_speech.local.catalog import ModelCatalog
from open_speech.providers import gemini_tts as gemini_tts_mod
from open_speech.providers import openai_tts as openai_tts_mod
from open_speech.providers.base import LlmProvider, SttProvider, TtsProvider, TtsRequest, VisionProvider, Voice
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.stt_common import UsageSink

log = logging.getLogger(__name__)

FIXTURE_VOICE = "fixture:tone-v1:alto"
KEYED_TTS = ("openai", "gemini")


def _openai_tts(catalog: ModelCatalog, key: str) -> TtsProvider:
    from open_speech.providers.openai_tts import OpenAITts

    return OpenAITts(catalog, key)


def _gemini_tts_f(catalog: ModelCatalog, key: str) -> TtsProvider:
    from open_speech.providers.gemini_tts import GeminiTts

    return GeminiTts(catalog, key)


def _kokoro(catalog: ModelCatalog, paths: Paths) -> Any:
    from open_speech.providers.kokoro import KokoroTts

    return KokoroTts(catalog, paths)


def _anthropic(catalog: ModelCatalog, key: str, settings: Callable[[], UserSettings] | None) -> Any:
    from open_speech.providers.anthropic_llm import AnthropicLlm

    def model() -> str | None:
        if settings is None:
            return None
        m = settings().llm.model
        return m if m.startswith("claude") else None

    def effort() -> str:
        return settings().llm.annotate_effort if settings is not None else "low"

    return AnthropicLlm(catalog, key, model=model, annotate_effort=effort)


INTERIM_PARTIALS_S = 0.7


def _openai_stt(catalog: ModelCatalog, key: str) -> SttProvider:
    from open_speech.providers.openai_stt import OpenAIStt

    # Interim partials every ~0.7 s so the transcript appears as the user speaks (FR-BAR-2).
    return OpenAIStt(catalog, key, partial_interval_s=INTERIM_PARTIALS_S)


def _gemini_stt(catalog: ModelCatalog, key: str) -> SttProvider:
    from open_speech.providers.gemini_stt import GeminiStt

    return GeminiStt(catalog, key, partial_interval_s=INTERIM_PARTIALS_S)


def _local_stt(catalog: ModelCatalog, paths: Paths) -> Any:
    from open_speech.providers.local_stt import LocalStt

    return LocalStt(catalog, paths)


def _compat(catalog: ModelCatalog) -> TtsProvider:
    from open_speech.providers.openai_compatible import OpenAICompatibleTts

    return OpenAICompatibleTts(catalog)


@dataclass
class ProviderFactories:
    """Constructors for each adapter; tests swap these for fakes."""

    openai_tts: Callable[[ModelCatalog, str], TtsProvider] = _openai_tts
    gemini_tts: Callable[[ModelCatalog, str], TtsProvider] = _gemini_tts_f
    kokoro: Callable[[ModelCatalog, Paths], Any] = _kokoro
    anthropic: Callable[[ModelCatalog, str, Callable[[], UserSettings] | None], Any] = _anthropic
    openai_stt: Callable[[ModelCatalog, str], SttProvider] = _openai_stt
    gemini_stt: Callable[[ModelCatalog, str], SttProvider] = _gemini_stt
    local_stt: Callable[[ModelCatalog, Paths], Any] = _local_stt
    openai_compatible: Callable[[ModelCatalog], TtsProvider] = _compat
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderRegistry:
    def __init__(
        self,
        env: EngineEnv,
        paths: Paths,
        catalog: ModelCatalog,
        keys: ProviderKeys,
        *,
        settings: Callable[[], UserSettings] | None = None,
        factories: ProviderFactories | None = None,
    ) -> None:
        self.env = env
        self.paths = paths
        self.catalog = catalog
        self.keys = keys
        self._settings = settings
        self._f = factories or ProviderFactories()
        self._cache: dict[str, Any] = {}
        self._usage_sink: UsageSink | None = None
        keys.add_listener(lambda _provider: self.refresh())

    # ------------------------------------------------------------------ lifecycle

    @property
    def fixture_mode(self) -> bool:
        return bool(self.env.open_speech_fixture_providers)

    def refresh(self) -> None:
        """Drop cached adapters (after a key change or catalog reload). Kokoro keeps its loaded session."""
        kokoro = self._cache.get("tts:kokoro")
        local_stt = self._cache.get("stt:local")
        self._cache.clear()
        if kokoro is not None:
            self._cache["tts:kokoro"] = kokoro
        if local_stt is not None:  # local speech-to-text keeps its loaded model too, unless its weights just went
            self._cache["stt:local"] = local_stt
            if not local_stt.installed():
                local_stt.unload()

    def reload_catalog(self, catalog: ModelCatalog) -> None:
        self.catalog = catalog
        self._cache.clear()

    def set_usage_sink(self, sink: UsageSink | None) -> None:
        """Billing callback for STT calls: sink(provider, model, "stt", Units)."""
        self._usage_sink = sink
        for k, v in self._cache.items():
            if k.startswith("stt:") and hasattr(v, "usage_sink"):
                v.usage_sink = sink

    def _get(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    # ------------------------------------------------------------------ TTS

    def kokoro(self) -> Any:
        return self._get("tts:kokoro", lambda: self._f.kokoro(self.catalog, self.paths))

    def fixture_tts(self) -> TtsProvider | None:
        if not self.fixture_mode:
            return None
        from open_speech.providers.fixture import FixtureTts

        return self._get("tts:fixture", lambda: FixtureTts(self.catalog))  # type: ignore[no-any-return]

    def provider(self, provider_id: str) -> TtsProvider | None:
        """The TTS adapter for a provider, or None when it can't be used (no key, fixture mode off)."""
        if provider_id in KEYED_TTS:
            if not self.keys.configured(provider_id):
                return None
            key = self.keys.get(provider_id)
            assert key is not None
            make = self._f.openai_tts if provider_id == "openai" else self._f.gemini_tts
            return self._get(f"tts:{provider_id}", lambda: make(self.catalog, key))  # type: ignore[no-any-return]
        if provider_id == "kokoro":
            return self.kokoro()  # type: ignore[no-any-return]
        if provider_id == "fixture":
            return self.fixture_tts()
        if provider_id == "openai_compatible":
            return self._get("tts:openai_compatible", lambda: self._f.openai_compatible(self.catalog))  # type: ignore[no-any-return]
        return None

    def voices(self) -> list[Voice]:
        out: list[Voice] = []
        for provider_id, catalog_fn in (
            ("openai", openai_tts_mod.catalog_voices),
            ("gemini", gemini_tts_mod.catalog_voices),
        ):
            models = self.catalog.tts_models(provider_id)
            if models and self.keys.configured(provider_id):
                out.extend(catalog_fn(models))
        if self.catalog.tts_models("kokoro"):
            out.extend(self.kokoro().voices())
        if self.catalog.tts_models("openai_compatible"):
            compat = self.provider("openai_compatible")
            if compat is not None:
                out.extend(compat.voices())
        fx = self.fixture_tts()
        if fx is not None:
            out.extend(fx.voices())
        return out

    def voice(self, voice_id: str) -> Voice:
        for v in self.voices():
            if v.id == voice_id:
                return v
        raise NotFound(f"a voice called {voice_id}")

    def tts_for(self, voice_id: str) -> tuple[TtsProvider, TtsRequest]:
        """Adapter + request template (text empty) for a voice. Raises NotFound / Unavailable /
        EngineError(invalid_request) when the voice can't be used right now."""
        try:
            provider_id, model, voice = split_voice_id(voice_id)
        except ValueError as exc:
            raise NotFound(f"a voice called {voice_id}") from exc
        entry = self.catalog.tts_model(provider_id, model)
        if entry is None or voice not in entry.voices or (entry.test_only and not self.fixture_mode):
            raise NotFound(f"a voice called {voice_id}")
        if entry.status == "later":
            raise EngineError("invalid_request", f"{entry.name or model} voices aren't available yet.")
        if provider_id in KEYED_TTS and not self.keys.configured(provider_id):
            raise Unavailable("voice", provider=provider_id)
        prov = self.provider(provider_id)
        if prov is None:
            raise NotFound(f"a voice called {voice_id}")
        if provider_id == "kokoro":
            reason = prov.unavailable_reason(model, voice)  # type: ignore[attr-defined]
            if reason:
                raise EngineError("invalid_request", reason, provider="kokoro")
        return prov, TtsRequest(text="", model=model, voice=voice)

    def is_local(self, voice_id: str) -> bool:
        try:
            provider_id, model, _ = split_voice_id(voice_id)
        except ValueError:
            return False
        entry = self.catalog.tts_model(provider_id, model)
        return bool(entry and entry.local)

    def default_local_voice(self) -> str:
        """The fallback/local voice: the fixture voice in fixture mode, else Kokoro (the user's default
        voice when it is a Kokoro voice)."""
        if self.fixture_mode:
            return FIXTURE_VOICE
        if self._settings is not None:
            v = self._settings().defaults.voice
            if v.startswith("kokoro:"):
                return v
        kokoro_models = self.catalog.tts_models("kokoro")
        if kokoro_models and kokoro_models[0].voices:
            m = kokoro_models[0]
            preferred = split_voice_id(DEFAULT_VOICE)[2]
            return m.voice_id(preferred if preferred in m.voices else m.voices[0])
        return DEFAULT_VOICE

    # ------------------------------------------------------------------ LLM / vision / STT

    def llm(self) -> LlmProvider | None:
        if self.keys.configured("anthropic"):
            key = self.keys.get("anthropic")
            assert key is not None
            return self._get("llm:anthropic", lambda: self._f.anthropic(self.catalog, key, self._settings))  # type: ignore[no-any-return]
        if self.fixture_mode:
            from open_speech.providers.fixture import FixtureLlm

            return self._get("llm:fixture", FixtureLlm)  # type: ignore[no-any-return]
        return None

    def vision(self) -> VisionProvider | None:
        if self.keys.configured("anthropic"):
            return self.llm()  # type: ignore[return-value]  # AnthropicLlm implements both protocols
        if self.fixture_mode:
            from open_speech.providers.fixture import FixtureVision

            return self._get("vision:fixture", FixtureVision)  # type: ignore[no-any-return]
        return None

    def llm_model(self) -> str:
        """Model id used for plan keys and estimates."""
        llm = self.llm()
        if llm is not None and hasattr(llm, "model"):
            return str(llm.model)
        if self._settings is not None:
            return self._settings().llm.model
        entry = self.catalog.llm_for("anthropic")
        return entry.default if entry else "claude-sonnet-5-5"

    def local_stt(self) -> Any:
        """The on-device speech-to-text adapter (`LocalStt`), whether or not its weights are installed."""
        return self._get("stt:local", lambda: self._f.local_stt(self.catalog, self.paths))

    def stt(self) -> SttProvider | None:
        """First available per 07 § STT routing: on-device (when sherpa-onnx and its weights are present),
        OpenAI, Gemini, then the fixture STT in fixture mode. `settings.voice_input.stt_provider` pins one.
        Fixture mode never routes to the on-device model: hermetic runs must not depend on installed weights."""
        pref = self._settings().voice_input.stt_provider if self._settings is not None else "auto"
        order = ["openai", "gemini", "fixture"] if pref == "auto" else [pref]
        if pref == "auto" and not self.fixture_mode:
            order = ["local", *order]
        if pref == "local" and self.fixture_mode:
            order = ["fixture"]
        for p in order:
            stt = self._stt(p)
            if stt is not None:
                return stt
        return None

    def stt_unavailable(self) -> tuple[str, str | None]:
        """Why the bar can't listen, and the local model whose download would fix it (if any)."""
        pref = self._settings().voice_input.stt_provider if self._settings is not None else "auto"
        if pref in ("auto", "local") and not self.fixture_mode and self.catalog.local_stt_models():
            local = self.local_stt()
            if local.runtime_available() and not local.installed():
                return t("unavailable.speech_model"), str(local.model)
        return t("unavailable.speech_input"), None

    def _stt(self, provider_id: str) -> SttProvider | None:
        if provider_id in ("openai", "gemini"):
            if not self.keys.configured(provider_id):
                return None
            key = self.keys.get(provider_id)
            assert key is not None
            make = self._f.openai_stt if provider_id == "openai" else self._f.gemini_stt
            stt = self._get(f"stt:{provider_id}", lambda: make(self.catalog, key))
        elif provider_id == "local":
            if not self.catalog.local_stt_models() or self.local_stt().unavailable_reason() is not None:
                return None
            stt = self.local_stt()
        elif provider_id == "fixture" and self.fixture_mode:
            from open_speech.providers.fixture import FixtureStt

            stt = self._get("stt:fixture", FixtureStt)
        else:
            return None
        if self._usage_sink is not None and hasattr(stt, "usage_sink"):
            stt.usage_sink = self._usage_sink
        return stt  # type: ignore[no-any-return]

