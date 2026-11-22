from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from open_speech.cache.audio import AudioCache
from open_speech.config import Paths
from open_speech.errors import EngineError, NotFound, Unavailable
from open_speech.local.catalog import ModelCatalog
from open_speech.playback.synth import Synthesizer
from open_speech.providers.base import TtsRequest
from open_speech.providers.fixture import FixtureLlm, FixtureStt, FixtureTts, FixtureVision
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.registry import ProviderFactories, ProviderRegistry
from open_speech.service.events import EventBus
from open_speech.store.assets import AssetStore
from open_speech.store.db import Database
from open_speech.usage.budget import BudgetGuard
from open_speech.usage.ledger import Ledger
from open_speech.usage.pricing import Pricing

from .helpers import SettingsBox, make_env
from .test_kokoro import install_fake_weights

PAID = "fixture:tone-paid-v1:soprano"
LOCAL = "fixture:tone-v1:alto"


@dataclass
class Runtime:
    registry: ProviderRegistry
    cache: AudioCache
    ledger: Ledger
    budget: BudgetGuard
    synth: Synthesizer
    keys: ProviderKeys


def build(paths: Paths, db: Database, settings: SettingsBox, events: EventBus, *, fixture: bool = True,
          factories: ProviderFactories | None = None, **env: Any) -> Runtime:
    e = make_env(open_speech_fixture_providers=fixture, **env)
    keys = ProviderKeys(e)
    catalog = ModelCatalog.load(paths)
    registry = ProviderRegistry(e, paths, catalog, keys, settings=settings, factories=factories)
    cache = AudioCache(AssetStore(paths.cache, db))
    ledger = Ledger(db, Pricing.load(paths))
    budget = BudgetGuard(ledger, settings, events)
    synth = Synthesizer(registry, cache, ledger, budget, settings, events, retry_base_s=0.001, retry_max_s=0.005)
    return Runtime(registry, cache, ledger, budget, synth, keys)


@pytest.fixture
def rt(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> Runtime:
    return build(paths, db, settings, events)


# ------------------------------------------------------------------ registry


def test_providers_the_build_has_no_key_for_are_absent(paths: Paths, db: Database, settings: SettingsBox,
                                                       events: EventBus) -> None:
    r = build(paths, db, settings, events, fixture=False).registry
    vs = r.voices()
    by = {v.id: v for v in vs}
    # No "add a key" placeholders: a provider the build doesn't include contributes nothing to choose from.
    assert not any(v.provider in ("openai", "gemini") for v in vs)
    assert not by["kokoro:kokoro-82m-v1.0:af_heart"].available  # weights absent here
    assert not any(v.provider == "fixture" for v in vs)  # test voices only in fixture mode
    assert not any(v.provider == "qwen3" for v in vs)  # P2: listed by the model manager only
    with pytest.raises(Unavailable) as missing:
        r.tts_for("openai:gpt-4o-mini-tts:coral")
    assert "key" not in missing.value.detail.lower()
    with pytest.raises(NotFound):
        r.tts_for("openai:gpt-4o-mini-tts:nobody")
    with pytest.raises(NotFound):
        r.tts_for(LOCAL)  # fixture voices don't exist outside fixture mode
    with pytest.raises(EngineError, match="Download"):
        r.tts_for("kokoro:kokoro-82m-v1.0:af_heart")
    assert r.default_local_voice() == "kokoro:kokoro-82m-v1.0:af_heart"
    assert r.llm() is None and r.vision() is None and r.stt() is None


def test_keys_enable_providers_and_refresh(paths: Paths, db: Database, settings: SettingsBox,
                                          events: EventBus) -> None:
    made: list[str] = []

    def fake_tts(catalog: ModelCatalog, key: str) -> Any:
        made.append(key)
        return FixtureTts(catalog)

    rt = build(paths, db, settings, events, fixture=False,
               factories=ProviderFactories(openai_tts=fake_tts, openai_stt=lambda c, k: FixtureStt(),
                                           gemini_stt=lambda c, k: FixtureStt()),
               openai_api_key="sk-one")
    r = rt.registry
    by = {v.id: v for v in r.voices()}
    assert by["openai:gpt-4o-mini-tts:coral"].available
    prov, tmpl = r.tts_for("openai:gpt-4o-mini-tts:coral")
    assert tmpl == TtsRequest(text="", model="gpt-4o-mini-tts", voice="coral")
    assert r.tts_for("openai:gpt-4o-mini-tts:ash")[0] is prov and made == ["sk-one"]  # cached adapter
    rt.keys.mark_invalid("openai")  # the provider rejected the build's key → refresh via listener
    assert "openai:gpt-4o-mini-tts:coral" not in {v.id for v in r.voices()}
    with pytest.raises(Unavailable):
        r.tts_for("openai:gpt-4o-mini-tts:coral")


def test_stt_routing_order(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> None:
    class Tagged(FixtureStt):
        def __init__(self, tag: str) -> None:
            super().__init__()
            self.tag = tag

    f = ProviderFactories(openai_stt=lambda c, k: Tagged("openai"), gemini_stt=lambda c, k: Tagged("gemini"))
    both = build(paths, db, settings, events, factories=f, openai_api_key="sk-x", gemini_api_key="AIza-x").registry
    assert both.stt().tag == "openai"  # type: ignore[union-attr]
    settings.set("voice_input.stt_provider", "gemini")
    assert both.stt().tag == "gemini"  # type: ignore[union-attr]
    settings.set("voice_input.stt_provider", "auto")
    only_g = build(paths, db, settings, events, factories=f, gemini_api_key="AIza-x").registry
    assert only_g.stt().tag == "gemini"  # type: ignore[union-attr]
    none = build(paths, db, settings, events, factories=f).registry
    assert isinstance(none.stt(), FixtureStt) and not hasattr(none.stt(), "tag")  # fixture last, in fixture mode
    sink: list[Any] = []
    none.set_usage_sink(lambda *a: sink.append(a))
    assert none.stt().usage_sink is not None  # type: ignore[union-attr]


def test_fixture_mode_registry(rt: Runtime) -> None:
    r = rt.registry
    ids = {v.id for v in r.voices() if v.provider == "fixture"}
    assert ids == {LOCAL, "fixture:tone-v1:bass", PAID}
    assert r.default_local_voice() == LOCAL
    assert isinstance(r.llm(), FixtureLlm) and isinstance(r.vision(), FixtureVision)
    assert r.llm_model() == "fixture-llm"
    assert r.is_local(LOCAL) and not r.is_local(PAID)


def test_kokoro_default_when_installed(paths: Paths, db: Database, settings: SettingsBox, events: EventBus) -> None:
    install_fake_weights(paths)
    r = build(paths, db, settings, events, fixture=False).registry
    assert r.tts_for("kokoro:kokoro-82m-v1.0:af_heart")[1].voice == "af_heart"
    assert r.default_local_voice() == "kokoro:kokoro-82m-v1.0:af_heart"


# ------------------------------------------------------------------ synthesizer


async def test_first_call_bills_then_replay_is_free_with_zero_provider_calls(rt: Runtime) -> None:
    text = "The cache makes replays free."
    first = await rt.synth.synth(text, PAID, document_id="doc_1")
    assert not first.cache_hit and first.usd > 0 and first.voice_id == PAID and first.notice is None
    assert FixtureTts.calls == 1
    assert [w.start for w in first.audio.words] == [0, 4, 10, 16, 24]  # exact fixture timings kept
    second = await rt.synth.synth(text, PAID, document_id="doc_1")
    assert second.cache_hit and FixtureTts.calls == 1  # M0 exit: replay makes 0 API calls
    rows = rt.ledger.rows().items
    assert [r.cache_hit for r in reversed(rows)] == [False, True]
    paid, hit = rows[1], rows[0]
    assert Decimal(paid.usd_estimate) == first.usd and paid.document_id == "doc_1"
    assert hit.usd_estimate == "0" and Decimal(hit.units["avoided_usd"]) == first.usd


async def test_local_voice_rows_are_free(rt: Runtime) -> None:
    res = await rt.synth.synth("Local voices cost nothing.", LOCAL)
    row = rt.ledger.rows().items[0]
    assert res.usd == 0 and row.local and row.usd_estimate == "0" and row.provider == "fixture"


async def test_budget_stop_falls_back_to_local_with_notice(rt: Runtime, settings: SettingsBox) -> None:
    settings.set("budget.monthly_usd", "0.000001")
    await rt.synth.synth("One paid sentence to cross the line.", PAID)
    res = await rt.synth.synth("This one must not be paid.", PAID)
    assert res.voice_id == LOCAL and res.notice == "Budget reached; reading with the local voice."
    assert rt.ledger.rows().items[0].local


async def test_retries_transient_errors_then_succeeds(rt: Runtime) -> None:
    FixtureTts.fail_next(503, 429)
    res = await rt.synth.synth("Retry me.", PAID)
    assert res.voice_id == PAID and FixtureTts.calls == 3


async def test_persistent_failure_falls_back_per_settings(rt: Runtime, settings: SettingsBox) -> None:
    FixtureTts.fail_next(503, 503, 503)
    res = await rt.synth.synth("Provider is down.", PAID)
    assert res.voice_id == LOCAL and "isn't responding" in (res.notice or "")
    settings.set("playback.fallback", "same_provider")
    FixtureTts.fail_next(500, 500, 500)
    with pytest.raises(EngineError):
        await rt.synth.synth("No fallback allowed.", PAID)


async def test_offline_and_rejected_key(rt: Runtime) -> None:
    FixtureTts.fail_next(0, 0, 0)
    res = await rt.synth.synth("Offline now.", PAID)
    assert res.voice_id == LOCAL and "couldn't reach" in (res.notice or "")
    FixtureTts.fail_next(401)
    res = await rt.synth.synth("Bad key.", PAID)
    # 3 offline attempts + 1 local fallback, then 1 rejected (not retried) + 1 local fallback
    assert res.voice_id == LOCAL and FixtureTts.calls == 6


async def test_unavailable_voice_falls_back(paths: Paths, db: Database, settings: SettingsBox,
                                            events: EventBus) -> None:
    rt = build(paths, db, settings, events)  # fixture mode, no OpenAI key
    res = await rt.synth.synth("Needs a key.", "openai:gpt-4o-mini-tts:coral")
    assert res.voice_id == LOCAL
    assert res.notice == "That voice isn't available right now; reading with the local voice."


async def test_style_is_part_of_the_key_and_ignored_with_notice_when_unsupported(
    paths: Paths, db: Database, settings: SettingsBox, events: EventBus
) -> None:
    install_fake_weights(paths)
    rt = build(paths, db, settings, events)
    a = await rt.synth.synth("Styled.", PAID, style="cheerful")
    b = await rt.synth.synth("Styled.", PAID, style="somber")
    assert a.audio.key != b.audio.key and a.style_applied
    kokoro_entry = rt.registry.catalog.tts_model("kokoro", "kokoro-82m-v1.0")
    assert kokoro_entry is not None and kokoro_entry.style is None


async def test_concurrent_requests_share_one_call(rt: Runtime) -> None:
    FixtureTts.latency_s = 0.05
    results = await asyncio.gather(*(rt.synth.synth("Same words.", PAID) for _ in range(4)))
    assert FixtureTts.calls == 1
    assert len({r.audio.key for r in results}) == 1
    assert sum(1 for r in rt.ledger.rows().items if not r.cache_hit) == 1


async def test_cached_lookup_has_no_side_effects(rt: Runtime) -> None:
    assert rt.synth.cached("Nothing yet.", PAID) is None
    await rt.synth.synth("Nothing yet.", PAID)
    before = rt.ledger.rows().total
    assert rt.synth.cached("Nothing yet.", PAID) is not None
    assert rt.ledger.rows().total == before
