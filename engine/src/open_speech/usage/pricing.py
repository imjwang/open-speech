"""Pricing catalog (08 § Pricing catalog): bundled `data/pricing.toml` deep-merged with the user's
`pricing.toml`. All math is Decimal; results are estimates. User overrides apply on the next cost
computation (FR-COST-5): `cost()` re-reads the override file when its mtime changes.
"""

from __future__ import annotations

import logging
import math
import tomllib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from open_speech.config import Paths
from open_speech.local.catalog import DATA_DIR, deep_merge
from open_speech.providers.base import Units

log = logging.getLogger(__name__)

BUNDLED_PRICING = DATA_DIR / "pricing.toml"
DEFAULT_FREE_TZ = "America/Los_Angeles"  # Google's billing month (verify)
MTOK = Decimal(1_000_000)
QUANT = Decimal("0.000001")  # stored precision (micro-dollars)
_META = {"version", "currency", "chars_per_token"}


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


class Pricing:
    def __init__(
        self, data: dict[str, Any], *, override_path: Path | None = None, bundled: dict[str, Any] | None = None
    ) -> None:
        self._bundled = bundled if bundled is not None else data
        self._override_path = override_path
        self._override_mtime = self._mtime()
        self._apply(data)

    @classmethod
    def load(cls, paths: Paths | None = None, *, bundled: Path = BUNDLED_PRICING) -> Pricing:
        base = tomllib.loads(bundled.read_text("utf-8"))
        override = paths.pricing_toml if paths is not None else None
        data = cls._merge(base, override)
        return cls(data, override_path=override, bundled=base)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Pricing:
        return cls(data)

    @staticmethod
    def _merge(base: dict[str, Any], override: Path | None) -> dict[str, Any]:
        if override is None or not override.exists():
            return base
        over = tomllib.loads(override.read_text("utf-8"))
        merged = deep_merge(base, over)
        if "version" not in over:
            merged["version"] = f"{base.get('version', 'unknown')}+user"
        return merged

    def _apply(self, data: dict[str, Any]) -> None:
        self.data = data
        self.version: str = str(data.get("version", "unknown"))
        self.currency: str = str(data.get("currency", "USD"))
        self.chars_per_token = _d(data.get("chars_per_token", 4))

    def _mtime(self) -> float | None:
        if self._override_path is None:
            return None
        try:
            return self._override_path.stat().st_mtime
        except OSError:
            return None

    def refresh_if_changed(self) -> bool:
        m = self._mtime()
        if m == self._override_mtime:
            return False
        self._override_mtime = m
        self._apply(self._merge(self._bundled, self._override_path))
        return True

    # ------------------------------------------------------------------ lookups

    def entry(self, provider: str, model: str) -> dict[str, Any] | None:
        prov = self.data.get(provider)
        if not isinstance(prov, dict):
            return None
        e = prov.get(model)
        return e if isinstance(e, dict) else None

    def has(self, provider: str, model: str) -> bool:
        return self.entry(provider, model) is not None

    def free_allowance(self, provider: str, model: str) -> FreeAllowance | None:
        """The model's free monthly allowance (`free_chars_per_month`), or None."""
        self.refresh_if_changed()
        e = self.entry(provider, model)
        if e is None or not e.get("free_chars_per_month"):
            return None
        return FreeAllowance(
            provider=provider,
            model=model,
            chars=int(e["free_chars_per_month"]),
            tz=str(e.get("free_period_tz") or DEFAULT_FREE_TZ),
            per_mchars=_d(e.get("per_mchars", 0)),
        )

    def free_allowances(self) -> list[FreeAllowance]:
        out: list[FreeAllowance] = []
        for provider, models in self.data.items():
            if provider in _META or not isinstance(models, dict):
                continue
            for model in models:
                a = self.free_allowance(provider, model)
                if a is not None:
                    out.append(a)
        return out

    @staticmethod
    def rate_card(e: dict[str, Any], units: Units) -> dict[str, Any]:
        """The rates that apply to this call: the entry's `long_context` card replaces them when the prompt
        (input + cache-read + cache-write tokens) is over its `above_tokens` (Claude Haiku 5.5 over 100K)."""
        long = e.get("long_context")
        if not isinstance(long, dict) or "above_tokens" not in long:
            return e
        prompt = units.in_tok + units.cache_read_tok + units.cache_write_tok
        if prompt <= int(long["above_tokens"]):
            return e
        return {**{k: v for k, v in e.items() if k != "long_context"}, **long}

    def cost(self, provider: str, model: str, units: Units) -> Decimal:
        """Estimated USD for one call. Unknown models cost 0 (the ledger flags them `unpriced`)."""
        self.refresh_if_changed()
        e = self.entry(provider, model)
        if e is None:
            return Decimal(0)
        e = self.rate_card(e, units)
        total = Decimal(0)
        in_rate = e.get("input_per_mtok", e.get("text_input_per_mtok"))
        if in_rate is not None:
            in_tok = units.in_tok or (math.ceil(Decimal(units.chars) / self.chars_per_token) if units.chars else 0)
            total += _d(in_rate) * Decimal(in_tok) / MTOK
        if "output_per_mtok" in e:
            total += _d(e["output_per_mtok"]) * Decimal(units.out_tok) / MTOK
        if "audio_output_per_mtok" in e:
            audio_tok = Decimal(units.out_tok) if units.out_tok else (
                _d(units.audio_sec) * _d(e.get("audio_tokens_per_sec", 0))
            )
            total += _d(e["audio_output_per_mtok"]) * audio_tok / MTOK
        if units.cache_read_tok and "cache_read_per_mtok" in e:
            total += _d(e["cache_read_per_mtok"]) * Decimal(units.cache_read_tok) / MTOK
        if units.cache_write_tok:
            cw = e.get("cache_write_per_mtok")
            rate = _d(cw) if cw is not None else (_d(in_rate) * Decimal("1.25") if in_rate is not None else Decimal(0))
            total += rate * Decimal(units.cache_write_tok) / MTOK
        if "per_audio_minute" in e:
            total += _d(e["per_audio_minute"]) * _d(units.audio_sec) / Decimal(60)
        if "per_mchars" in e:
            total += _d(e["per_mchars"]) * Decimal(units.chars) / MTOK
        if "per_image" in e:
            total += _d(e["per_image"]) * Decimal(units.images)
        return total.quantize(QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FreeAllowance:
    """A monthly allowance that costs nothing (`free_chars_per_month`), counted per billing month in `tz`."""

    provider: str
    model: str
    chars: int
    tz: str = DEFAULT_FREE_TZ
    per_mchars: Decimal = Decimal(0)  # the price beyond it, per million characters

    @property
    def per_kchars(self) -> Decimal:
        return self.per_mchars / Decimal(1000)


@dataclass(frozen=True)
class BillingPeriod:
    key: str  # "YYYY-MM" in the allowance's time zone
    start: datetime  # first instant of the month (aware, in that zone)
    resets_on: date  # the first day of the next month, in that zone


def billing_zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("no time zone data for %s; using UTC-8 for its billing month", name)
        return timezone(timedelta(hours=-8))


def billing_period(now: datetime, tz_name: str) -> BillingPeriod:
    """The calendar month `now` falls in, in `tz_name` (Google bills by the Pacific-time month)."""
    local = (now if now.tzinfo is not None else now.replace(tzinfo=UTC)).astimezone(billing_zone(tz_name))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return BillingPeriod(key=start.strftime("%Y-%m"), start=start, resets_on=nxt)


def money(d: Decimal | str | int | float) -> str:
    """Decimal string for the API (never a float), trimmed of trailing zeros but keeping cents."""
    v = Decimal(str(d)).quantize(QUANT, rounding=ROUND_HALF_UP)
    if v == v.quantize(Decimal("0.01")):
        return str(v.quantize(Decimal("0.01")))
    return format(v.normalize(), "f")
