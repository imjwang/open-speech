"""Google's free Chirp characters (usage/free_tier.py, usage/ledger.py, playback/synth.py): $0 inside the allowance,
only the part past it priced, the Pacific-time billing month, the 2 % reserve, alerts once per month, the fallback
to the local voice with a notice, "keep paying" for one month, estimates, and the spoken-notice hook."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from open_speech.api.schemas import Estimate
from open_speech.cache.audio import AudioCache
from open_speech.config import Paths
from open_speech.document.model import Block, BlockType, Document, SourceKind
from open_speech.i18n import t
from open_speech.local.catalog import ModelCatalog
from open_speech.playback.output import NullOutput
from open_speech.playback.synth import Synthesizer
from open_speech.playback.transport import Transport
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.pcm import f32_to_bytes, tone
from open_speech.providers.registry import ProviderFactories, ProviderRegistry
from open_speech.service.events import EventBus
from open_speech.store.assets import AssetStore
from open_speech.store.db import Database
from open_speech.usage.budget import BudgetGuard
from open_speech.usage.estimate import estimate_read
from open_speech.usage.free_tier import FreeTierExhausted, FreeTierGuard
from open_speech.usage.ledger import Ledger
from open_speech.usage.pricing import Pricing

from .helpers import SettingsBox, make_env

ZEPHYR = "google:chirp3-hd:en-US-Chirp3-HD-Zephyr"
LOCAL = "fixture:tone-v1:alto"
GOOGLE = ("google", "chirp3-hd")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class FakeGoogle:
    """Stands in for GoogleTts: counts calls, returns a tone."""

    id = "google"
    timestamps = False

    def __init__(self) -> None:
        self.calls = 0

    def voices(self) -> list[Voice]:
        return []

    def limits(self, model: str) -> TtsLimits:
        return TtsLimits(max_input_chars=5000, formats=["l16"])

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        self.calls += 1
        yield AudioChunk(pcm=f32_to_bytes(tone(220.0, 0.5, 24000)), sample_rate=24000, final=True)

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), audio_sec=round(audio_sec, 3))


@dataclass
class Rt:
    clock: Clock
    ledger: Ledger
    guard: FreeTierGuard
    budget: BudgetGuard
    synth: Synthesizer
    registry: ProviderRegistry
    google: FakeGoogle
    events: list[dict[str, Any]]


def runtime(paths: Paths, db: Database, settings: SettingsBox, *, at: datetime, fixture: bool = True) -> Rt:
    clock = Clock(at)
    events = EventBus()
    seen: list[dict[str, Any]] = []
    events.add_listener(seen.append)
    env = make_env(open_speech_fixture_providers=fixture, gemini_api_key="g-gem")
    keys = ProviderKeys(env)
    google = FakeGoogle()
    registry = ProviderRegistry(env, paths, ModelCatalog.load(paths), keys, settings=settings,
                                factories=ProviderFactories(google_tts=lambda c, k, check=None: google))
    ledger = Ledger(db, Pricing.load(paths), now=clock)
    budget = BudgetGuard(ledger, settings, events)
    guard = FreeTierGuard(ledger, settings, events, available=lambda p: p != "google" or registry.google_usable())
    ledger.add_listener(guard.after_record)
    synth = Synthesizer(registry, AudioCache(AssetStore(paths.cache, db)), ledger, budget, settings, events,
                        retry_base_s=0.001, retry_max_s=0.005, free_tier=guard)
    return Rt(clock, ledger, guard, budget, synth, registry, google, seen)


def use_chars(rt: Rt, n: int, **kw: Any) -> Decimal:
    return rt.ledger.record(provider="google", model="chirp3-hd", op="tts", units=Units(chars=n), **kw)


NOV = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


@pytest.fixture
def rt(paths: Paths, db: Database, settings: SettingsBox) -> Rt:
    return runtime(paths, db, settings, at=NOV)


# ------------------------------------------------------------------ the ledger


def test_free_inside_the_allowance_then_only_the_part_beyond_is_priced(rt: Rt) -> None:
    assert use_chars(rt, 999_000) == 0
    straddle = use_chars(rt, 2_000)  # 1,000 free, 1,000 at $30 per million
    assert straddle == Decimal("0.030000")
    assert use_chars(rt, 500) == Decimal("0.015000")
    rows = rt.ledger.rows().items
    assert rows[1].units["chars"] == 2_000 and rows[1].units["free_chars"] == 1_000
    assert rt.ledger.month_spent() == Decimal("0.045")
    allowance = rt.guard.allowance(*GOOGLE)
    assert allowance is not None and rt.ledger.free_chars_used(allowance) == 1_001_500


def test_cache_hits_and_local_runs_do_not_use_the_allowance(rt: Rt) -> None:
    use_chars(rt, 400_000)
    assert use_chars(rt, 5_000, cache_hit=True) == 0
    assert rt.ledger.rows().items[0].units["avoided_usd"] == "0.00"  # nothing avoided while inside the allowance
    use_chars(rt, 5_000, local=True)
    allowance = rt.guard.allowance(*GOOGLE)
    assert allowance is not None and rt.ledger.free_chars_used(allowance) == 400_000


def test_the_billing_month_follows_pacific_time(paths: Paths, db: Database, settings: SettingsBox) -> None:
    # 07:59 UTC on December 1st is still November 30th in California.
    rt = runtime(paths, db, settings, at=datetime(2026, 12, 1, 7, 59, tzinfo=UTC))
    assert use_chars(rt, 1_000_000) == 0
    assert use_chars(rt, 100) == Decimal("0.003000")  # November's allowance is used up
    rt.clock.at = datetime(2026, 12, 1, 8, 0, 1, tzinfo=UTC)  # midnight in California: a new allowance
    assert use_chars(rt, 100) == 0
    u = rt.guard.use(*GOOGLE)
    assert u is not None and u.period.key == "2026-12" and u.used == 100
    assert u.period.resets_on.isoformat() == "2027-01-01"


# ------------------------------------------------------------------ the guard


def test_reserve_and_policy(rt: Rt, settings: SettingsBox) -> None:
    use_chars(rt, 979_990)
    assert rt.guard.check(*GOOGLE, 10) == "free"  # exactly at allowance × 0.98
    with pytest.raises(FreeTierExhausted):
        rt.guard.check(*GOOGLE, 11)
    use_chars(rt, 10)
    assert rt.guard.exhausted(*GOOGLE)
    u = rt.guard.use(*GOOGLE)
    assert u is not None and u.stop_at == 980_000 and u.remaining == 0
    settings.set("playback.beyond_free_tier", "pay")
    settings.set("playback.beyond_free_tier_month", "2026-11")
    assert rt.guard.check(*GOOGLE, 10) == "free"  # under "pay" the reserve isn't needed: free to 1,000,000
    assert rt.guard.check(*GOOGLE, 20_001) == "paid"
    assert not rt.guard.exhausted(*GOOGLE)
    settings.set("playback.beyond_free_tier_month", "2026-10")  # "pay" was for another month
    assert rt.guard.policy(rt.guard.allowance(*GOOGLE)) == "local"  # type: ignore[arg-type]
    with pytest.raises(FreeTierExhausted):
        rt.guard.check(*GOOGLE, 1)
    assert rt.guard.check("openai", "gpt-4o-mini-tts", 1000) == "none"  # no allowance: the budget decides


def test_alerts_at_80_percent_and_when_used_up_once_per_month(rt: Rt) -> None:
    def alerts() -> list[str]:
        return [m["message"] for m in rt.events if m["type"] == "usage.alert"]

    use_chars(rt, 700_000)
    assert alerts() == []
    use_chars(rt, 110_000)
    assert alerts() == [t("free_tier.warn", pct=81)]
    use_chars(rt, 10_000)
    assert len(alerts()) == 1  # not again this month
    use_chars(rt, 170_000)  # 990,000: past the stop at 980,000
    assert alerts()[-1] == t("free_tier.stop", rate="three cents") and len(alerts()) == 2
    assert all(m["level"] == "warn" for m in rt.events if m["type"] == "usage.alert")  # reading goes on
    use_chars(rt, 5_000)
    assert len(alerts()) == 2
    rt.clock.at = datetime(2026, 12, 10, tzinfo=UTC)
    use_chars(rt, 850_000)
    assert len(alerts()) == 3 and alerts()[-1].startswith("Heads up")  # a new month re-arms


def test_free_tier_source_lists_the_google_allowance(rt: Rt) -> None:
    use_chars(rt, 12_345)
    (state,) = rt.guard.free_tiers()
    assert (state.provider, state.label, state.unit, state.used, state.limit, state.resets_on) == (
        "google", "Google voice", "chars", 12_345, 1_000_000, "2026-12-01"
    )


def test_no_free_tier_listed_without_usable_google_voices(paths: Paths, db: Database, settings: SettingsBox) -> None:
    rt = runtime(paths, db, settings, at=NOV)
    check = rt.registry.google_check()
    check.state, check.blocked = "blocked", "blocked"
    assert rt.guard.free_tiers() == []


# ------------------------------------------------------------------ the synthesizer


async def test_used_up_allowance_reads_with_the_local_voice_and_says_why(rt: Rt) -> None:
    use_chars(rt, 980_000)
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == LOCAL and rt.google.calls == 0
    assert res.notice == t("notice.free_tier", target=t("notice.local_voice"))
    assert res.notice_reason == "free_tier"


async def test_inside_the_allowance_the_budget_does_not_apply(rt: Rt, settings: SettingsBox) -> None:
    settings.set("budget.monthly_usd", "0")  # "no paid calls"
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == ZEPHYR and res.usd == 0 and rt.google.calls == 1


async def test_keep_paying_holds_under_the_budget_and_reverts_next_month(rt: Rt, settings: SettingsBox) -> None:
    settings.set("playback.beyond_free_tier", "pay")
    settings.set("playback.beyond_free_tier_month", "2026-11")
    use_chars(rt, 1_000_000)
    res = await rt.synth.synth("Hello there.", ZEPHYR)
    assert res.voice_id == ZEPHYR and res.usd == Decimal("0.000360")  # 12 characters at $30 per million
    settings.set("budget.monthly_usd", "0.0003")  # the budget's hard stop still applies
    res = await rt.synth.synth("Something new to say.", ZEPHYR)
    assert res.voice_id == LOCAL and res.notice_reason == "budget"
    # December: the "pay" choice was for November, so a used-up December allowance means the local voice again.
    settings.set("budget.monthly_usd", "10")
    rt.clock.at = datetime(2026, 12, 15, tzinfo=UTC)
    use_chars(rt, 980_000)
    res = await rt.synth.synth("And one more sentence.", ZEPHYR)
    assert res.voice_id == LOCAL and res.notice_reason == "free_tier"


# ------------------------------------------------------------------ estimates


def _doc(chars: int) -> Document:
    text = ("Word " * (chars // 5)).strip() + "."
    block = Block(id="b1", order=0, type=BlockType.paragraph, text=text)
    return Document(id="d1", source_id="s1", kind=SourceKind.text, title="t", blocks=[block])


def _estimate(paths: Paths, db: Database, remaining: int | None, policy: str = "local") -> Estimate:
    voice = Voice(id=ZEPHYR, provider="google", model="chirp3-hd", name="Zephyr (Chirp 3 HD, American English)")
    return estimate_read(_doc(10_000), None, voice=voice, mode="classic", llm_model="claude-haiku-5-5",
                         pricing=Pricing.load(paths), audio_cache=AudioCache(AssetStore(paths.cache, db)),
                         free_remaining=remaining, beyond_free=policy)


def test_estimate_prices_against_the_remaining_allowance(paths: Paths, db: Database) -> None:
    inside = _estimate(paths, db, 500_000)
    assert inside.usd_estimate == "0.00" and "within the free allowance" in inside.breakdown[0].item
    local = _estimate(paths, db, 4_000)
    assert local.usd_estimate == "0.00" and "read with the local voice" in local.breakdown[0].item
    paid = _estimate(paths, db, 4_000, "pay")
    assert paid.usd_estimate == "0.18"  # ~6,000 characters past the allowance at $30 per million
    assert "beyond the free allowance" in paid.breakdown[0].item
    full = _estimate(paths, db, None)  # a provider without an allowance: everything priced
    assert Decimal(full.usd_estimate) > 0


# ------------------------------------------------------------------ spoken notices


async def test_fallback_notices_are_spoken_once_per_session_per_reason(rt: Rt, settings: SettingsBox) -> None:
    transport = Transport(rt.synth, NullOutput(), EventBus(), settings=settings, on_position=lambda *_a: None)
    said: list[str] = []
    transport.speak_notice = said.append
    free = t("notice.free_tier", target=t("notice.local_voice"))
    transport.show_notice(free, "free_tier")
    transport.show_notice(free, "free_tier")
    transport.announce(free, "free_tier")
    assert said == [free] and transport.snapshot().fallback_notice == free
    transport.show_notice(t("notice.no_style"), "no_style")  # shown, never spoken
    assert said == [free]
    offline = t("notice.offline", name="Google", target=t("notice.local_voice"))
    transport.show_notice(offline, "offline")
    assert said == [free, offline]
    await transport.close()


async def test_default_hook_says_nothing(rt: Rt, settings: SettingsBox) -> None:
    transport = Transport(rt.synth, NullOutput(), EventBus(), settings=settings, on_position=lambda *_a: None)
    assert transport.announce("anything", "budget") is True  # handed to the no-op hook without error
    assert transport.announce("anything", "budget") is False
    await transport.close()


def test_turning_keep_paying_on_again_in_a_later_month_sticks() -> None:
    """A "pay" left over from October, switched off and on again in November, holds for November (it used to be
    reverted to "local" at once because the old month was still stored)."""
    from types import SimpleNamespace

    from open_speech.config import UserSettings
    from open_speech.service.engine import Engine

    replaced: list[UserSettings] = []
    fake = SimpleNamespace(
        pricing=SimpleNamespace(free_allowances=lambda: ["google"]),
        ledger=SimpleNamespace(free_period=lambda _a: SimpleNamespace(key="2026-11")),
        settings=SimpleNamespace(replace=replaced.append),
    )
    base = UserSettings()

    def with_policy(policy: str, month: str) -> UserSettings:
        pb = base.playback.model_copy(update={"beyond_free_tier": policy, "beyond_free_tier_month": month})
        return base.model_copy(update={"playback": pb})

    off = with_policy("local", "2026-10")
    on = with_policy("pay", "2026-10")
    Engine._free_tier_policy_month(fake, on, off)  # type: ignore[arg-type]
    assert replaced[-1].playback.beyond_free_tier == "pay"
    assert replaced[-1].playback.beyond_free_tier_month == "2026-11"
    # Already on since October (startup or another change): that month is over, back to the local voice.
    Engine._free_tier_policy_month(fake, on)  # type: ignore[arg-type]
    assert replaced[-1].playback.beyond_free_tier == "local"
