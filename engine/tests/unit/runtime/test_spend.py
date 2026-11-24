"""Month-to-date spend (08 § Budgets): Haiku's long-prompt rate card, the cached month total, the single budget
trigger, free allowances, and `usage.changed` (usage/watch.py)."""

from __future__ import annotations

import asyncio
import random
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from open_speech.api.schemas import BudgetState, FreeTierState
from open_speech.config import EngineEnv
from open_speech.providers.base import LlmCall, Units
from open_speech.service.events import EventBus
from open_speech.store.db import Database
from open_speech.usage.free_tier import FreeTierRegistry
from open_speech.usage.pricing import Pricing
from open_speech.usage.watch import UsageWatcher

from .test_usage import make_ledger

# ------------------------------------------------------------------ Haiku 5.5's two rate cards


def test_haiku_prompts_over_100k_tokens_use_the_long_rate_card(pricing: Pricing) -> None:
    def haiku(**u: int) -> Decimal:
        return pricing.cost("anthropic", "claude-haiku-5-5", Units(**u))

    # Up to 100K prompt tokens: $0.10 in, $0.50 out, cache reads $0.01, 5-minute cache writes $0.125 per MTok.
    assert haiku(in_tok=100_000, out_tok=1_000) == Decimal("0.0100") + Decimal("0.0005")
    assert haiku(in_tok=40_000, cache_read_tok=50_000, cache_write_tok=10_000, out_tok=2_000) == (
        Decimal("0.004") + Decimal("0.0005") + Decimal("0.00125") + Decimal("0.001")
    )
    # One token more and the whole call is billed at $0.50 / $2.50 (cache reads $0.05, writes $0.625).
    assert haiku(in_tok=100_001, out_tok=1_000) == Decimal("0.052501")  # 0.0500005 + 0.0025, to the micro-dollar
    # The prompt counts cached tokens too: 1K new tokens on top of a 100K cached prefix is a long prompt.
    assert haiku(in_tok=1_000, cache_read_tok=100_000, out_tok=500) == (
        Decimal("0.0005") + Decimal("0.005") + Decimal("0.00125")
    )
    # Models with one rate card are unaffected.
    assert pricing.cost("anthropic", "claude-sonnet-5-5", Units(in_tok=200_000)) == Decimal("0.4")


def test_a_user_override_can_change_the_long_rate_card(pricing: Pricing, paths: Any) -> None:
    paths.pricing_toml.write_text('[anthropic."claude-haiku-5-5".long_context]\noutput_per_mtok = "3.00"\n')
    p = Pricing.load(paths)
    assert p.cost("anthropic", "claude-haiku-5-5", Units(in_tok=200_000, out_tok=1_000_000)) == Decimal("3.1")


# ------------------------------------------------------------------ the cached month total


def _sql_month(db: Database, since: datetime) -> Decimal:
    rows = db.query("SELECT usd_estimate FROM ledger WHERE ts >= ?", (since.astimezone(UTC).isoformat(),))
    return sum((Decimal(r["usd_estimate"]) for r in rows), Decimal(0))


def _random_units(rng: random.Random) -> dict[str, Any]:
    kind = rng.choice(["haiku", "sonnet", "tts", "local", "hit"])
    if kind == "haiku":
        return {"provider": "anthropic", "model": "claude-haiku-5-5", "op": "chat",
                "units": Units(in_tok=rng.randint(0, 150_000), out_tok=rng.randint(0, 3000),
                               cache_read_tok=rng.randint(0, 20_000))}
    if kind == "sonnet":
        return {"provider": "anthropic", "model": "claude-sonnet-5-5", "op": "annotate",
                "units": Units(in_tok=rng.randint(0, 9000), out_tok=rng.randint(0, 500))}
    if kind == "tts":
        return {"provider": "openai", "model": "gpt-4o-mini-tts", "op": "tts",
                "units": Units(chars=rng.randint(1, 4000), audio_sec=rng.randint(1, 240))}
    if kind == "local":
        return {"provider": "kokoro", "model": "kokoro-82m-v1.0", "op": "tts", "units": Units(chars=200),
                "local": True}
    return {"provider": "openai", "model": "gpt-4o-mini-tts", "op": "tts", "units": Units(chars=900),
            "cache_hit": True}


def test_cached_month_total_equals_the_sql_sum_across_a_month_change(db: Database, pricing: Pricing) -> None:
    rng = random.Random(7)
    ledger, clock = make_ledger(db, pricing, datetime(2026, 11, 2, 9, tzinfo=UTC))
    clock.now = datetime(2026, 10, 28, tzinfo=UTC)
    for _ in range(20):  # last month: never part of November's total
        ledger.record(**_random_units(rng))
    clock.now = datetime(2026, 11, 2, 9, tzinfo=UTC)
    november = datetime(2026, 11, 1, tzinfo=UTC)
    assert ledger.month_spent() == _sql_month(db, november)  # the one SQL sum
    for i in range(300):
        clock.now += timedelta(minutes=rng.randint(1, 90))
        if clock.now.month == 12:
            break
        ledger.record(**_random_units(rng))
        if i % 17 == 0:
            assert ledger.month_spent() == _sql_month(db, november)
    assert ledger.month_spent() == _sql_month(db, november) > 0

    # A new month: the first read (or the first record) starts from that month's rows only.
    clock.now = datetime(2026, 12, 1, 0, 5, tzinfo=UTC)
    assert ledger.month_spent() == 0
    usd = ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="chat", units=Units(in_tok=10_000))
    assert ledger.month_spent() == usd == Decimal("0.02")
    clock.now = datetime(2027, 1, 1, tzinfo=UTC)
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="chat", units=Units(in_tok=5_000))
    assert ledger.month_spent() == Decimal("0.01") == _sql_month(db, datetime(2027, 1, 1, tzinfo=UTC))


def test_cached_month_total_survives_concurrent_writers(db: Database, pricing: Pricing) -> None:
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    ledger.month_spent()  # cached before the writers start

    def write(seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(60):
            ledger.record(**_random_units(rng))
            ledger.month_spent()

    threads = [threading.Thread(target=write, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ledger.month_spent() == _sql_month(db, datetime(2026, 11, 1, tzinfo=UTC))


def test_reading_the_month_total_does_not_query_the_database(db: Database, pricing: Pricing,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    ledger.month_spent()
    queries: list[str] = []
    real = db.query
    monkeypatch.setattr(db, "query", lambda sql, params=(): queries.append(sql) or real(sql, params))
    for _ in range(5):
        ledger.record(provider="anthropic", model="claude-haiku-5-5", op="chat", units=Units(in_tok=1000))
        ledger.month_spent()
    assert queries == []


# ------------------------------------------------------------------ one budget trigger


def _engine(tmp_path: Path) -> Any:
    from open_speech.service.engine import Engine

    env = EngineEnv(  # type: ignore[call-arg]
        _env_file=None, open_speech_home=tmp_path / "home", open_speech_fixture_providers=True,
        open_speech_audio_output="null", anthropic_api_key=None, openai_api_key=None, gemini_api_key=None,
    )
    return Engine(env)


def test_the_ledger_is_the_only_budget_trigger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine(tmp_path)
    try:
        calls: list[int] = []
        real = engine.budget.after_record
        monkeypatch.setattr(engine.budget, "after_record", lambda: calls.append(1) or real())
        engine.record_llm("annotate", LlmCall(output=[], units=Units(in_tok=10), model="claude-haiku-5-5"))
        assert calls == [1]
    finally:
        engine.db.close()


# ------------------------------------------------------------------ free allowances


class _Allowance:
    def __init__(self, used: int) -> None:
        self.used = used

    def free_tiers(self) -> list[FreeTierState]:
        return [FreeTierState(provider="google", label="Google voice", used=self.used, limit=1_000_000,
                              resets_on="2026-12-01")]


class _Broken:
    def free_tiers(self) -> list[FreeTierState]:
        raise RuntimeError("down")


def test_budget_state_lists_registered_free_allowances(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        assert engine.usage.budget().free_tiers == []  # nothing registers one yet
        engine.free_tiers.register(_Broken())
        engine.free_tiers.register(_Allowance(120_000))
        state = engine.usage.budget()
        assert [(f.provider, f.used, f.limit) for f in state.free_tiers] == [("google", 120_000, 1_000_000)]
    finally:
        engine.db.close()


def test_free_tier_registry_registers_once() -> None:
    reg = FreeTierRegistry()
    src = _Allowance(1)
    reg.register(src)
    reg.register(src)
    assert len(reg.states()) == 1
    reg.unregister(src)
    assert reg.states() == []


# ------------------------------------------------------------------ usage.changed


class _Spend:
    """A budget state the test controls: spend, budget and one free allowance."""

    def __init__(self) -> None:
        self.spent = Decimal("0")
        self.monthly = "10"
        self.used: int | None = None
        self.month = "2026-11"
        self.reads = 0

    def state(self) -> BudgetState:
        self.reads += 1
        tiers = [] if self.used is None else _Allowance(self.used).free_tiers()
        return BudgetState(monthly_usd=self.monthly, warn_at=0.8, hard_stop=True,
                           spent_usd=str(self.spent.quantize(Decimal("0.0001"))), pct=0.0, free_tiers=tiers)


async def _watch(spend: _Spend) -> tuple[UsageWatcher, list[dict[str, Any]]]:
    events = EventBus()
    out: list[dict[str, Any]] = []
    events.add_listener(lambda m: out.append(m) if m["type"] == "usage.changed" else None)
    watcher = UsageWatcher(spend.state, lambda: spend.month, events, delay_s=0.05, max_wait_s=0.2)
    watcher.start(asyncio.get_running_loop())
    return watcher, out


async def test_a_burst_of_paid_rows_is_one_message_after_a_quiet_moment() -> None:
    spend = _Spend()
    watcher, out = await _watch(spend)
    for _ in range(5):
        spend.spent += Decimal("0.0021")
        watcher.poke(Decimal("0.0021"))
        await asyncio.sleep(0.01)
    assert out == []  # still inside the debounce window
    await asyncio.sleep(0.1)
    assert len(out) == 1 and out[0]["budget"]["spent_usd"] == "0.0105"
    watcher.stop()


async def test_a_steady_stream_still_updates_within_the_bound() -> None:
    spend = _Spend()
    watcher, out = await _watch(spend)
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < 0.35:  # a poke every 20 ms: a plain trailing debounce would never fire
        spend.spent += Decimal("0.001")
        watcher.poke()
        await asyncio.sleep(0.02)
    assert 1 <= len(out) <= 2
    watcher.stop()


async def test_nothing_is_sent_when_nothing_visible_changed() -> None:
    spend = _Spend()
    watcher, out = await _watch(spend)
    watcher.poke(Decimal(0))  # Kokoro reading, cache hits: $0 rows
    spend.spent += Decimal("0.00004")  # below what a client shows ($0.0001)
    watcher.poke()
    await asyncio.sleep(0.12)
    assert out == []
    spend.monthly = "20"  # the budget changed
    watcher.poke()
    await asyncio.sleep(0.12)
    assert [m["budget"]["monthly_usd"] for m in out] == ["20"]
    spend.used = 4_000  # a free allowance moved, spend didn't
    watcher.poke()
    await asyncio.sleep(0.12)
    assert len(out) == 2 and out[1]["budget"]["free_tiers"][0]["used"] == 4_000
    watcher.stop()


async def test_the_first_row_of_a_new_month_is_announced_even_at_zero() -> None:
    spend = _Spend()
    watcher, out = await _watch(spend)
    spend.month = "2026-12"  # $0 last month and $0 so far this month: same numbers, new month
    watcher.poke(Decimal(0))
    await asyncio.sleep(0.12)
    assert len(out) == 1
    watcher.stop()


async def test_pokes_from_other_threads_are_safe() -> None:
    spend = _Spend()
    watcher, out = await _watch(spend)
    spend.spent = Decimal("0.5")
    threads = [threading.Thread(target=watcher.poke) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    await asyncio.sleep(0.15)
    assert len(out) == 1
    watcher.stop()
    watcher.poke()  # after stop: ignored
    await asyncio.sleep(0.08)
    assert len(out) == 1
