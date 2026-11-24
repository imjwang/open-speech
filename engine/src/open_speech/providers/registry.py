"""Provider registry: one voice list across providers (FR-VOICE-1), adapter construction from keys and the
catalog, STT routing (07), and the local/fixture defaults.

Adapters are created lazily and cached; `refresh()` drops them (called when a key is rejected or a local
model is installed), except local ones whose loaded model is worth keeping (`TtsEntry.persistent`: Kokoro, Qwen3,
on-device speech-to-text). A provider this build has no key for contributes no voices at all: users never see
something they'd need a key to use.

Text-to-speech providers are one table (`TTS_PROVIDERS`): whether they need a key, how the adapter is made, how their
voices are listed, and whether voice ids beyond the catalog's list exist (Qwen3's cloned voices,
"qwen3:clone:<slug>", which `tts_for` checks with `provider.has_voice()`).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from open_speech.config import DEFAULT_LLM, DEFAULT_LOCAL_VOICE, EngineEnv, Paths, UserSettings
from open_speech.errors import EngineError, NotFound, Unavailable
from open_speech.i18n import t
from open_speech.ids import split_voice_id
from open_speech.local.catalog import ModelCatalog
from open_speech.providers import gemini_tts as gemini_tts_mod
from open_speech.providers import google_tts as google_tts_mod
from open_speech.providers import openai_tts as openai_tts_mod
from open_speech.providers.base import LlmProvider, SttProvider, TtsProvider, TtsRequest, VisionProvider, Voice
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.stt_common import UsageSink

log = logging.getLogger(__name__)

FIXTURE_VOICE = "fixture:tone-v1:alto"


def _openai_tts(catalog: ModelCatalog, key: str) -> TtsProvider:
    from open_speech.providers.openai_tts import OpenAITts

    return OpenAITts(catalog, key)


def _gemini_tts_f(catalog: ModelCatalog, key: str) -> TtsProvider:
    from open_speech.providers.gemini_tts import GeminiTts

    return GeminiTts(catalog, key)


def _google_tts(catalog: ModelCatalog, key: str, check: Any = None) -> TtsProvider:
    from open_speech.providers.google_tts import GoogleTts

    return GoogleTts(catalog, key, check=check)


def _google_check(catalog: ModelCatalog, key: Callable[[], str | None], **kw: Any) -> Any:
    from open_speech.providers.google_tts import GoogleVoiceCheck

    return GoogleVoiceCheck(catalog, key, **kw)


def _kokoro(catalog: ModelCatalog, paths: Paths) -> Any:
    from open_speech.providers.kokoro import KokoroTts

    return KokoroTts(catalog, paths)


def _qwen3(catalog: ModelCatalog, paths: Paths, *, settings: Callable[[], UserSettings] | None = None,
           kv: Any | None = None, clones: Any | None = None) -> Any:
    from open_speech.providers.qwen3 import Qwen3Tts

    return Qwen3Tts(catalog, paths, settings=settings, kv=kv, clones=clones)


ANTHROPIC_TIMEOUT_S = 120.0  # per read: streamed replies and transforms keep the connection busy, not idle
ANTHROPIC_KEEPALIVE_S = 60.0  # how long an idle pooled connection is kept for the next turn
ANTHROPIC_POOL = 8  # keep-alive connections (annotate batches and an agent turn can overlap)


def _anthropic_client(key: str, on_request: Callable[[], None]) -> Any:
    """One `AsyncAnthropic` for every Claude call: its connection pool keeps TLS sessions alive between turns.
    `on_request` is called as each request leaves (the registry's idle clock)."""
    import anthropic
    import httpx2

    async def touch(_request: Any) -> None:
        on_request()

    http = anthropic.DefaultAsyncHttpxClient(
        timeout=anthropic.Timeout(ANTHROPIC_TIMEOUT_S, connect=5.0),
        limits=httpx2.Limits(
            max_connections=4 * ANTHROPIC_POOL,
            max_keepalive_connections=ANTHROPIC_POOL,
            keepalive_expiry=ANTHROPIC_KEEPALIVE_S,
        ),
        event_hooks={"request": [touch]},
    )
    return anthropic.AsyncAnthropic(api_key=key, max_retries=2, http_client=http)


def _anthropic(
    catalog: ModelCatalog, key: str, settings: Callable[[], UserSettings] | None, client: Any | None = None
) -> Any:
    from open_speech.providers.anthropic_llm import AnthropicLlm

    def model() -> str | None:
        if settings is None:
            return None
        m = settings().llm.model
        return m if m.startswith("claude") else None

    def effort() -> str:
        return settings().llm.annotate_effort if settings is not None else "low"

    return AnthropicLlm(catalog, key, model=model, annotate_effort=effort, client=client)


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
    google_tts: Callable[..., TtsProvider] = _google_tts  # (catalog, key, check)
    google_check: Callable[..., Any] = _google_check  # (catalog, key getter, on_change=, on_invalid=)
    kokoro: Callable[[ModelCatalog, Paths], Any] = _kokoro
    qwen3: Callable[..., Any] = _qwen3  # (catalog, paths, settings=, kv=, clones=)
    anthropic: Callable[..., Any] = _anthropic  # (catalog, key, settings, client=shared AsyncAnthropic)
    anthropic_client: Callable[[str, Callable[[], None]], Any] = _anthropic_client
    openai_stt: Callable[[ModelCatalog, str], SttProvider] = _openai_stt
    gemini_stt: Callable[[ModelCatalog, str], SttProvider] = _gemini_stt
    local_stt: Callable[[ModelCatalog, Paths], Any] = _local_stt
    openai_compatible: Callable[[ModelCatalog], TtsProvider] = _compat
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TtsEntry:
    """One text-to-speech provider in the registry's table."""

    id: str
    make: Callable[[ProviderRegistry], Any]  # the adapter (called once; cached until refresh)
    list_voices: Callable[[ProviderRegistry], list[Voice]]  # what it offers (only called when `offered`)
    offered: Callable[[ProviderRegistry], bool]  # the catalog has it (and a key, fixture mode, …)
    keyed: bool = False  # needs a provider key this build may or may not ship
    persistent: bool = False  # kept across refresh(): a local model whose loaded weights are worth keeping
    dynamic: bool = False  # voice ids beyond the catalog's list (cloned voices): checked with `has_voice()`


def _key(r: ProviderRegistry, provider: str) -> str:
    key = r.keys.get(provider)
    assert key is not None
    return key


def _keyed_offered(provider: str) -> Callable[[ProviderRegistry], bool]:
    return lambda r: bool(r.catalog.tts_models(provider)) and r.keys.configured(provider)


def _adapter_voices(r: ProviderRegistry, provider: str) -> list[Voice]:
    adapter = r.provider(provider)
    return list(adapter.voices()) if adapter is not None else []


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
        kv: Any | None = None,
    ) -> None:
        self.env = env
        self.kv = kv  # the engine's key-value store (Qwen3 keeps its measured speed per variant there)
        self._clones: Any | None = None  # Qwen3's cloned voices (a `providers.qwen3.CloneStore`); see `set_clone_store`
        self.paths = paths
        self.catalog = catalog
        self.keys = keys
        self._settings = settings
        self._f = factories or ProviderFactories()
        self._cache: dict[str, Any] = {}
        self._usage_sink: UsageSink | None = None
        self._anthropic: tuple[str, Any] | None = None  # (key, shared AsyncAnthropic); survives refresh()
        self._anthropic_used: float | None = None  # monotonic time of the last request sent to Anthropic
        self._google_check: Any | None = None  # GoogleVoiceCheck; survives refresh()
        self.on_voices_changed: Callable[[], None] | None = None  # the engine publishes capabilities.changed
        keys.add_listener(lambda _provider: self.refresh())

    # ------------------------------------------------------------------ lifecycle

    @property
    def fixture_mode(self) -> bool:
        return bool(self.env.open_speech_fixture_providers)

    def refresh(self) -> None:
        """Drop cached adapters (after a key change or catalog reload). Local voices keep their loaded models
        (`TtsEntry.persistent`: Kokoro's session, Qwen3's worker), and so does on-device speech-to-text."""
        kept = {f"tts:{e.id}": self._cache[f"tts:{e.id}"] for e in TTS_PROVIDERS
                if e.persistent and f"tts:{e.id}" in self._cache}
        local_stt = self._cache.get("stt:local")
        self._cache.clear()
        self._cache.update(kept)
        if local_stt is not None:  # local speech-to-text keeps its loaded model too, unless its weights just went
            self._cache["stt:local"] = local_stt
            if not local_stt.installed():
                local_stt.unload()

    def reload_catalog(self, catalog: ModelCatalog) -> None:
        self.catalog = catalog
        self._close_later(self._cache.get("tts:qwen3"))  # its worker would otherwise outlive the adapter
        self._cache.clear()
        if self._google_check is not None:
            self._google_check.set_catalog(catalog)

    @staticmethod
    def _close_later(adapter: Any) -> None:
        close = getattr(adapter, "aclose", None)
        if close is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(close())
        except RuntimeError:
            return
        task.add_done_callback(lambda t_: t_.cancelled() or t_.exception())

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
        return self._get("tts:kokoro", lambda: TTS_BY_ID["kokoro"].make(self))

    def qwen3(self) -> Any | None:
        """The Qwen3-TTS adapter (`Qwen3Tts`), or None when the catalog has no Qwen voices."""
        return self.provider("qwen3")

    def set_clone_store(self, store: Any) -> None:
        """Where cloned voices ("qwen3:clone:<slug>") come from (a `providers.qwen3.CloneStore`). Kept by the registry,
        so an adapter made again (catalog reload) still has it."""
        self._clones = store
        cached = self._cache.get("tts:qwen3")
        if cached is not None:
            cached.set_clone_store(store)

    def fixture_tts(self) -> TtsProvider | None:
        return self.provider("fixture")

    # ------------------------------------------------------------------ Google (Chirp 3 HD)

    def google_check(self) -> Any | None:
        """The `GoogleVoiceCheck` for this build's Google key, or None without one (or without Chirp in the
        catalog). Created once; the engine runs its schedule (`run()`) in the background."""
        if not self.catalog.tts_models("google") or not self.keys.configured("google"):
            return None
        if self._google_check is None:

            def changed() -> None:
                self._cache.pop("tts:google", None)
                if self.on_voices_changed is not None:
                    self.on_voices_changed()

            self._google_check = self._f.google_check(
                self.catalog,
                lambda: self.keys.get("google") if self.keys.configured("google") else None,
                on_change=changed,
                on_invalid=lambda: self.keys.mark_invalid("google"),
            )
        return self._google_check

    def google_usable(self) -> bool:
        """Chirp voices can be offered: a Google key, and the API isn't known to refuse it."""
        check = self.google_check()
        return check is not None and check.usable()

    def auto_cloud_voice(self, language: str) -> str | None:
        """The voice "auto" means in `language` while Chirp can be used: the catalog's `default_star` (Zephyr) in the
        first catalog locale of that language (en → en-US, es → es-US). None when the build has no usable Google
        voices or none in that language — structural reasons, so "auto" quietly means the local voice."""
        if not self.google_usable():
            return None
        check = self.google_check()
        base = language.lower().split("-")[0]
        for m in self.catalog.tts_models("google"):
            star = str(m.extra.get("default_star") or (m.stars[0] if m.stars else ""))
            for loc in m.locales:
                if loc.lower().split("-")[0] != base or not m.voice_template:
                    continue
                name = m.voice_template.format(locale=loc, star=star)
                if name in m.voices and (check is None or check.listed(name)):
                    return m.voice_id(name)
        return None

    def provider(self, provider_id: str) -> TtsProvider | None:
        """The TTS adapter for a provider, or None when it can't be used (no key, fixture mode off, not in the
        catalog)."""
        entry = TTS_BY_ID.get(provider_id)
        if entry is None or not entry.offered(self):
            return None
        return self._get(f"tts:{provider_id}", lambda: entry.make(self))  # type: ignore[no-any-return]

    def voices(self) -> list[Voice]:
        """Every voice this build can offer, in table order: catalog voices plus a provider's own (Qwen3's cloned
        voices)."""
        out: list[Voice] = []
        for entry in TTS_PROVIDERS:
            if entry.offered(self):
                out.extend(entry.list_voices(self))
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
        table = TTS_BY_ID.get(provider_id)
        entry = self.catalog.tts_model(provider_id, model)
        in_catalog = entry is not None and voice in entry.voices and (not entry.test_only or self.fixture_mode)
        if not in_catalog:
            dyn = self.provider(provider_id) if table is not None and table.dynamic else None
            if dyn is None or not dyn.has_voice(model, voice):  # type: ignore[attr-defined]
                raise NotFound(f"a voice called {voice_id}")
        if entry is not None and entry.status == "later":
            raise EngineError("invalid_request", f"{entry.name or model} voices aren't available yet.")
        if table is not None and table.keyed and not self.keys.configured(provider_id):
            raise Unavailable("voice", provider=provider_id)
        if provider_id == "google":
            check = self.google_check()
            if check is not None and not check.listed(voice):
                raise NotFound(f"a voice called {voice_id}")
            if check is not None and not check.usable():
                raise Unavailable("voice", detail=check.reason(), provider="google")
        prov = self.provider(provider_id)
        if prov is None:
            raise NotFound(f"a voice called {voice_id}")
        check_local = getattr(prov, "unavailable_reason", None)
        if callable(check_local) and provider_id in ("kokoro", "qwen3"):
            reason = check_local(model, voice)
            if reason:
                raise EngineError("invalid_request", reason, provider=provider_id)
        return prov, TtsRequest(text="", model=model, voice=voice)

    def is_local(self, voice_id: str) -> bool:
        try:
            provider_id, model, _ = split_voice_id(voice_id)
        except ValueError:
            return False
        entry = self.catalog.tts_model(provider_id, model)
        if entry is not None:
            return bool(entry.local)
        table = TTS_BY_ID.get(provider_id)
        if table is not None and table.dynamic:  # a provider's own voices (Qwen3 clones) are as local as it is
            return any(m.local for m in self.catalog.tts_models(provider_id))
        return False

    def default_local_voice(self, language: str | None = None) -> str:
        """The free voice that "auto" and every fallback read with: the fixture voice in fixture mode, else
        `defaults.local_voice` (Kokoro Heart unless changed) when it speaks `language`, else the user's own local
        voice for that language (`defaults.voices`), else the first local voice that speaks it. Without a language,
        `defaults.local_voice` (or the user's Kokoro default voice, for profiles from before `local_voice`)."""
        if self.fixture_mode:
            return FIXTURE_VOICE
        st = self._settings() if self._settings is not None else None
        preferred = st.defaults.local_voice if st is not None else DEFAULT_LOCAL_VOICE
        if st is not None and preferred == DEFAULT_LOCAL_VOICE and st.defaults.voice.startswith("kokoro:"):
            preferred = st.defaults.voice  # an explicit Kokoro choice is the local voice this user knows
        if not self.is_local(preferred):
            preferred = DEFAULT_LOCAL_VOICE
        if not language:
            return self._kokoro_or(preferred)
        base = language.lower().split("-")[0]
        local = [v for v in self._local_voices() if v.languages]

        def speaks(vid: str) -> bool:
            v = next((x for x in local if x.id == vid), None)
            return v is not None and any(x.lower().split("-")[0] == base for x in v.languages)

        if speaks(preferred):
            return preferred
        chosen = st.defaults.voices.get(base) if st is not None else None
        if chosen and self.is_local(chosen) and speaks(chosen):
            return chosen
        native = [v for v in local if any(x.lower().split("-")[0] == base for x in v.languages)]
        native.sort(key=lambda v: not v.available)  # catalog order; an installed voice first
        return native[0].id if native else self._kokoro_or(preferred)

    def _local_voices(self) -> list[Voice]:
        out: list[Voice] = []
        if self.catalog.tts_models("kokoro"):
            out.extend(self.kokoro().voices())
        return out

    def _kokoro_or(self, preferred: str) -> str:
        """`preferred` when the catalog has it, else the catalog's first Kokoro voice."""
        try:
            provider_id, model, voice = split_voice_id(preferred)
        except ValueError:
            provider_id, model, voice = "", "", ""
        entry = self.catalog.tts_model(provider_id, model)
        if entry is not None and voice in entry.voices:
            return preferred
        kokoro_models = self.catalog.tts_models("kokoro")
        if kokoro_models and kokoro_models[0].voices:
            m = kokoro_models[0]
            name = split_voice_id(DEFAULT_LOCAL_VOICE)[2]
            return m.voice_id(name if name in m.voices else m.voices[0])
        return DEFAULT_LOCAL_VOICE

    # ------------------------------------------------------------------ Anthropic client (07 § Agent core)

    def anthropic_client(self) -> Any | None:
        """The one `AsyncAnthropic` the planning adapter and the agent share, or None without a usable key.
        Its keep-alive pool lets a turn reuse the TLS connection of the previous one instead of opening its
        own; it is rebuilt only if the key itself changes."""
        if not self.keys.configured("anthropic"):
            return None
        key = self.keys.get("anthropic")
        assert key is not None
        if self._anthropic is None or self._anthropic[0] != key:
            self._anthropic = (key, self._f.anthropic_client(key, self._touch_anthropic))
        return self._anthropic[1]

    def _touch_anthropic(self) -> None:
        self._anthropic_used = time.monotonic()

    def anthropic_idle_s(self) -> float:
        """Seconds since the last request to Anthropic (infinite before the first one)."""
        return math.inf if self._anthropic_used is None else time.monotonic() - self._anthropic_used

    async def warm_anthropic(self, model: str) -> bool:
        """Open (or re-open) a pooled connection with a free Models API call, so the next turn starts on a
        warm TLS session. Never raises: a failed warm-up costs nothing and the turn reports real errors."""
        client = self.anthropic_client()
        if client is None:
            return False
        try:
            await client.models.retrieve(model)
        except Exception as e:  # noqa: BLE001 — offline, rate limited, bad key: the real call will say so
            log.info("Anthropic warm-up failed (%s)", type(e).__name__)
            return False
        return True

    async def aclose(self) -> None:
        qwen = self._cache.get("tts:qwen3")
        close_qwen = getattr(qwen, "aclose", None)
        if close_qwen is not None:
            try:
                await close_qwen()
            except Exception:  # noqa: BLE001
                log.debug("stopping the Qwen3-TTS worker failed", exc_info=True)
        google = self._cache.get("tts:google")
        for closable in (google, self._google_check):
            close = getattr(closable, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.debug("closing a Google client failed", exc_info=True)
        if self._anthropic is not None:
            client, self._anthropic = self._anthropic[1], None
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    log.debug("closing the Anthropic client failed", exc_info=True)

    # ------------------------------------------------------------------ LLM / vision / STT

    def llm(self) -> LlmProvider | None:
        if self.keys.configured("anthropic"):
            key = self.keys.get("anthropic")
            assert key is not None
            client = self.anthropic_client()
            return self._get(  # type: ignore[no-any-return]
                "llm:anthropic", lambda: self._f.anthropic(self.catalog, key, self._settings, client=client)
            )
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
        return entry.default if entry else DEFAULT_LLM

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


# Order is the voice list's order.
TTS_PROVIDERS: tuple[TtsEntry, ...] = (
    TtsEntry("openai", keyed=True, offered=_keyed_offered("openai"),
             make=lambda r: r._f.openai_tts(r.catalog, _key(r, "openai")),
             list_voices=lambda r: openai_tts_mod.catalog_voices(r.catalog.tts_models("openai"))),
    TtsEntry("gemini", keyed=True, offered=_keyed_offered("gemini"),
             make=lambda r: r._f.gemini_tts(r.catalog, _key(r, "gemini")),
             list_voices=lambda r: gemini_tts_mod.catalog_voices(r.catalog.tts_models("gemini"))),
    TtsEntry("google", keyed=True, offered=_keyed_offered("google"),
             make=lambda r: r._f.google_tts(r.catalog, _key(r, "google"), r.google_check()),
             list_voices=lambda r: google_tts_mod.catalog_voices(r.catalog.tts_models("google"), r.google_check())),
    TtsEntry("kokoro", persistent=True, offered=lambda r: bool(r.catalog.tts_models("kokoro")),
             make=lambda r: r._f.kokoro(r.catalog, r.paths), list_voices=lambda r: _adapter_voices(r, "kokoro")),
    TtsEntry("qwen3", persistent=True, dynamic=True, offered=lambda r: bool(r.catalog.tts_models("qwen3")),
             make=lambda r: r._f.qwen3(r.catalog, r.paths, settings=r._settings, kv=r.kv, clones=r._clones),
             list_voices=lambda r: _adapter_voices(r, "qwen3")),
    TtsEntry("openai_compatible", offered=lambda r: bool(r.catalog.tts_models("openai_compatible")),
             make=lambda r: r._f.openai_compatible(r.catalog),
             list_voices=lambda r: _adapter_voices(r, "openai_compatible")),
    TtsEntry("fixture", offered=lambda r: r.fixture_mode, make=lambda r: _fixture_tts(r.catalog),
             list_voices=lambda r: _adapter_voices(r, "fixture")),
)
TTS_BY_ID = {e.id: e for e in TTS_PROVIDERS}
KEYED_TTS = tuple(e.id for e in TTS_PROVIDERS if e.keyed)


def _fixture_tts(catalog: ModelCatalog) -> TtsProvider:
    from open_speech.providers.fixture import FixtureTts

    return FixtureTts(catalog)
