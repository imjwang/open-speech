from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from open_speech.config import Paths
from open_speech.errors import BudgetExceeded
from open_speech.providers.base import LlmCall, Units
from open_speech.service.events import EventBus
from open_speech.store.db import Database
from open_speech.usage.budget import BudgetGuard
from open_speech.usage.ledger import Ledger
from open_speech.usage.pricing import Pricing, money
from open_speech.usage.spoken import fraction_words, money_words

from .helpers import SettingsBox

# ------------------------------------------------------------------ pricing


def test_pricing_decimal_math(pricing: Pricing) -> None:
    assert pricing.version == "2026-11-22"
    # Sonnet: 1M in = $2, 1M out = $10, cache read $0.10/M, cache write $2.50/M
    c = pricing.cost(
        "anthropic",
        "claude-sonnet-5-5",
        Units(in_tok=1000, out_tok=500, cache_read_tok=2000, cache_write_tok=400),
    )
    assert isinstance(c, Decimal)
    assert c == Decimal("0.002") + Decimal("0.005") + Decimal("0.0002") + Decimal("0.001")
    # OpenAI TTS: 4000 chars → 1000 text tokens at $0.60/M + 60 s audio at 20.833 tok/s at $12/M
    tts = pricing.cost("openai", "gpt-4o-mini-tts", Units(chars=4000, audio_sec=60))
    assert tts == (Decimal("0.0006") + Decimal("60") * Decimal("20.833") * Decimal("12") / Decimal(1_000_000)).quantize(
        Decimal("0.000001")
    )
    assert abs(tts - Decimal("0.0156")) < Decimal("0.0001")  # ≈ $0.015/min + text
    # Gemini exact usage: audio tokens given directly
    g = pricing.cost("gemini", "gemini-3.8-flash-tts", Units(in_tok=14, out_tok=1500))
    assert g == (Decimal(14) * Decimal("0.50") + Decimal(1500) * Decimal("10.00")) / Decimal(1_000_000)
    # STT per minute
    assert pricing.cost("openai", "whisper-1", Units(audio_sec=30)) == Decimal("0.003")
    # unknown model is free but detectable
    assert pricing.cost("anthropic", "claude-fable-5-2", Units(in_tok=10**6)) == 0
    assert not pricing.has("anthropic", "claude-fable-5-2")


def test_cache_write_defaults_to_input_times_1_25() -> None:
    p = Pricing.from_dict({"version": "t", "x": {"m": {"input_per_mtok": "4"}}})
    assert p.cost("x", "m", Units(cache_write_tok=1_000_000)) == Decimal("5.000000")


def test_user_pricing_override_applies_immediately(paths: Paths) -> None:
    p = Pricing.load(paths)
    before = p.cost("openai", "whisper-1", Units(audio_sec=60))
    paths.pricing_toml.write_text('[openai."whisper-1"]\nper_audio_minute = "0.010"\n')
    after = p.cost("openai", "whisper-1", Units(audio_sec=60))
    assert before == Decimal("0.006") and after == Decimal("0.010")
    assert p.version == "2026-11-22+user"


def test_money_formatting() -> None:
    assert money(Decimal("3.2")) == "3.20"
    assert money(Decimal("0.0012345")) == "0.001235"
    assert money(0) == "0.00"


# ------------------------------------------------------------------ ledger


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_ledger(db: Database, pricing: Pricing, now: datetime) -> tuple[Ledger, Clock]:
    clock = Clock(now)
    return Ledger(db, pricing, now=clock, tz=UTC), clock


def test_ledger_rows_paid_cache_local_unpriced(db: Database, pricing: Pricing) -> None:
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, 12, tzinfo=UTC))
    usd = ledger.record(provider="openai", model="gpt-4o-mini-tts", op="tts",
                        units=Units(chars=400, audio_sec=6), document_id="doc_1")
    assert usd > 0
    zero = ledger.record(provider="openai", model="gpt-4o-mini-tts", op="tts",
                         units=Units(chars=400, audio_sec=6), document_id="doc_1", cache_hit=True)
    assert zero == 0
    ledger.record(provider="kokoro", model="kokoro-82m-v1.0", op="tts", units=Units(chars=100, audio_sec=2),
                  local=True)
    ledger.record(provider="anthropic", model="claude-fable-5-2", op="annotate", units=Units(in_tok=10))
    page = ledger.rows(limit=10)
    assert page.total == 4
    by_op = {(r.provider, r.cache_hit, r.local): r for r in page.items}
    hit = by_op[("openai", True, False)]
    assert hit.usd_estimate == "0" and Decimal(hit.units["avoided_usd"]) == usd
    assert by_op[("kokoro", False, True)].usd_estimate == "0"
    assert by_op[("anthropic", False, False)].units["unpriced"] is True
    assert all(r.pricing_version == "2026-11-22" for r in page.items)
    raw = db.query("SELECT units_json, usd_estimate FROM ledger ORDER BY id")
    assert json.loads(raw[0]["units_json"])["chars"] == 400
    assert Decimal(raw[0]["usd_estimate"]) == usd  # decimal string, never float
    assert ledger.rows(provider="kokoro").total == 1
    assert ledger.rows(cache_hit=True).total == 1


def test_record_call_from_llm_call(db: Database, pricing: Pricing) -> None:
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    call = LlmCall(output=[], units=Units(in_tok=1000, out_tok=100), model="claude-sonnet-5-5")
    assert ledger.record_call("annotate", call, document_id="doc_x") == Decimal("0.003")
    assert ledger.rows().items[0].provider == "anthropic"


def test_summary_periods_groups_and_spoken(db: Database, pricing: Pricing) -> None:
    now = datetime(2026, 11, 21, 15, tzinfo=UTC)  # a Saturday
    ledger, clock = make_ledger(db, pricing, now)
    # last month: must not count
    clock.now = datetime(2026, 10, 30, tzinfo=UTC)
    ledger.record(provider="openai", model="gpt-4o-mini-tts", op="tts", units=Units(audio_sec=6000))
    # earlier this month (not this week)
    clock.now = datetime(2026, 11, 3, tzinfo=UTC)
    ledger.record(provider="openai", model="gpt-4o-mini-tts", op="tts",
                  units=Units(chars=40000, audio_sec=10000), document_id="doc_a")
    # this week, today
    clock.now = now
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate",
                  units=Units(in_tok=100_000, out_tok=10_000), document_id="doc_b")
    ledger.record(provider="openai", model="gpt-4o-mini-tts", op="tts",
                  units=Units(chars=4000, audio_sec=60), document_id="doc_b", cache_hit=True)

    month = ledger.summary("month", "provider", budget_usd="10")
    assert month.estimated is True
    total = Decimal(month.total_usd)
    assert Decimal("2.5") < total < Decimal("3.0")
    assert [line.key for line in month.breakdown] == ["openai", "anthropic"]
    assert month.breakdown[0].calls == 1  # the cache hit isn't a call
    assert Decimal(month.cache_savings_usd) > 0
    assert 0.25 < month.budget_pct < 0.30
    assert month.spoken.startswith("About two dollars and")
    assert "estimated" in month.spoken and "mostly OpenAI speech" in month.spoken
    assert "about 28 percent of your ten dollar budget" in month.spoken
    assert "cache saved you" in month.spoken

    week = ledger.summary("week", "op", budget_usd="10")
    assert [line.key for line in week.breakdown] == ["annotate", "tts"]
    assert Decimal(week.total_usd) == Decimal("0.3")
    assert "this week" in week.spoken and "all Claude" in week.spoken

    docs = ledger.summary("month", "document")
    assert {line.key for line in docs.breakdown} == {"doc_a", "doc_b"}
    assert ledger.month_spent() == total
    assert ledger.spent(now - timedelta(hours=1)) == Decimal("0.3")


def test_spoken_rounding() -> None:
    assert money_words(Decimal("3.2")) == "three dollars and twenty cents"
    assert money_words(Decimal("0.45")) == "forty-five cents"
    assert money_words(Decimal("0.004")) == "less than a cent"
    assert money_words(Decimal("12.6")) == "thirteen dollars"
    assert money_words(Decimal("1.04")) == "one dollar"
    assert fraction_words(0.33) == "a third" and fraction_words(0.12) == "about 12 percent"


def test_empty_summary_is_spoken(db: Database, pricing: Pricing) -> None:
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    s = ledger.summary("today", "provider", budget_usd="10")
    assert s.total_usd == "0.00" and s.spoken == "Nothing spent today."


# ------------------------------------------------------------------ budget


def _alerts(captured: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in captured if m["type"] == "usage.alert"]


def test_budget_warns_once_then_stops_once(db: Database, pricing: Pricing, settings: SettingsBox,
                                           events: EventBus, captured: list[dict[str, Any]]) -> None:
    settings.set("budget.monthly_usd", "1")
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    guard = BudgetGuard(ledger, settings, events)
    cost = Units(in_tok=100_000)  # $0.20 on Sonnet
    for _ in range(3):
        ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=cost)
    assert _alerts(captured) == [] and guard.allow_paid()
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=cost)  # 0.80
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=Units(in_tok=10))
    alerts = _alerts(captured)
    assert [a["level"] for a in alerts] == ["warn"]
    assert alerts[0]["spent"] == "0.80" and alerts[0]["budget"] == "1.00"
    assert "80 percent" in alerts[0]["message"]
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=cost)  # 1.00
    assert [a["level"] for a in _alerts(captured)] == ["warn", "stop"]
    assert not guard.allow_paid()
    try:
        guard.check_paid()
    except BudgetExceeded as exc:
        assert exc.code == "budget_exceeded" and exc.extra["budget"] == "1.00"
    else:  # pragma: no cover
        raise AssertionError("expected BudgetExceeded")
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=cost)
    assert [a["level"] for a in _alerts(captured)] == ["warn", "stop"]  # still once per crossing

    # survives a restart: a new guard on the same db doesn't re-announce
    captured.clear()
    BudgetGuard(ledger, settings, events).after_record()
    assert _alerts(captured) == []

    # raising the budget re-arms
    settings.set("budget.monthly_usd", "20")
    assert guard.allow_paid()
    guard.after_record()
    assert _alerts(captured) == []


def test_budget_without_hard_stop_allows_paid(db: Database, pricing: Pricing, settings: SettingsBox,
                                              events: EventBus) -> None:
    settings.set("budget.monthly_usd", "0.01")
    settings.set("budget.hard_stop", False)
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    guard = BudgetGuard(ledger, settings, events)
    ledger.record(provider="anthropic", model="claude-sonnet-5-5", op="annotate", units=Units(in_tok=100_000))
    assert guard.allow_paid()
    guard.check_paid()


def test_zero_budget_means_no_paid_calls(db: Database, pricing: Pricing, settings: SettingsBox,
                                         events: EventBus) -> None:
    settings.set("budget.monthly_usd", "0")
    ledger, _ = make_ledger(db, pricing, datetime(2026, 11, 21, tzinfo=UTC))
    assert not BudgetGuard(ledger, settings, events).allow_paid()
