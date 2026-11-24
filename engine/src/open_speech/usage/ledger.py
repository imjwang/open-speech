"""Usage ledger (08 § Usage ledger, FR-COST-1/2): one row per billable call, cache hit or local run.

- Paid calls: `usd_estimate` from `Pricing`.
- Cache hits: `$0` with `cache_hit = 1` and the *avoided* cost in `units_json.avoided_usd`.
- Local runs (Kokoro): `$0` with `local = 1`.
- Unknown models: `$0` with `units_json.unpriced = true` so the gap is visible.
- Free monthly allowances (`free_chars_per_month` in pricing.toml, Google's Chirp voices): only the characters
  beyond the allowance are priced; the row keeps all its characters and `units_json.free_chars` says how many
  were free. Usage is counted per billing month in the allowance's time zone (Pacific for Google), from rows
  that called the provider (cache hits and local runs never count). Kept in memory like the month total.

Periods (today / week / month) are calendar periods in the machine's local time zone; timestamps are
stored as ISO 8601 UTC.

The month-to-date total is asked for on every paid call (budget check), by the spend chip and on every
`usage.changed`, so it is kept in memory: one SQL sum when it is first needed or the month changes, then each
`record()` adds its row under the same lock as the insert, so the cached total always equals the table's.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal

from open_speech.api.schemas import LedgerPage, LedgerRow, UsageLine, UsageSummary
from open_speech.providers.base import LlmCall, Units
from open_speech.store.db import Database
from open_speech.usage.pricing import BillingPeriod, FreeAllowance, Pricing, billing_period, money
from open_speech.usage.spoken import usage_sentence

log = logging.getLogger(__name__)

Period = Literal["today", "week", "month"]
GroupBy = Literal["provider", "op", "document"]
RecordListener = Callable[[Decimal], None]


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


class Ledger:
    def __init__(
        self,
        db: Database,
        pricing: Pricing,
        *,
        now: Callable[[], datetime] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self.db = db
        self.pricing = pricing
        self._now = now or (lambda: datetime.now(UTC))
        self._tz = tz
        self._listeners: list[RecordListener] = []
        # Month-to-date total: (month start as stored, total). Guarded by `_month_lock`, which is always taken
        # before the database's own lock, never the other way round.
        self._month_lock = threading.Lock()
        self._month: tuple[str, Decimal] | None = None
        # Characters used of each free allowance: (provider, model, billing month) → chars. Same lock.
        self._free_used: dict[tuple[str, str, str], int] = {}

    # ------------------------------------------------------------------ writing

    def add_listener(self, fn: RecordListener) -> None:
        """Called after every record with the row's usd (BudgetGuard subscribes itself)."""
        self._listeners.append(fn)

    def record(
        self,
        *,
        provider: str,
        model: str,
        op: str,
        units: Units,
        document_id: str | None = None,
        turn_id: str | None = None,
        cache_hit: bool = False,
        local: bool = False,
        avoided_usd: Decimal | None = None,
    ) -> Decimal:
        units_json: dict[str, Any] = units.as_dict()
        priced = self.pricing.has(provider, model)
        allowance = None if local else self.pricing.free_allowance(provider, model)
        now = self._now()
        with self._month_lock:
            free_key: tuple[str, str, str] | None = None
            if cache_hit:
                usd = Decimal(0)
                if avoided_usd is None:
                    if local:
                        avoided_usd = Decimal(0)
                    else:  # what the call would have cost now: nothing while inside a free allowance
                        billable, _ = self._beyond_free_locked(provider, model, units, allowance, now)
                        avoided_usd = self.pricing.cost(provider, model, billable)
                units_json["avoided_usd"] = money(avoided_usd)
            elif local:
                usd = Decimal(0)
            else:
                billable, free_key = self._beyond_free_locked(provider, model, units, allowance, now)
                if free_key is not None:
                    units_json["free_chars"] = units.chars - billable.chars
                usd = self.pricing.cost(provider, model, billable)
                if not priced:
                    units_json["unpriced"] = True
                    log.warning("no price for %s/%s; recorded at $0", provider, model)
            self.db.execute(
                "INSERT INTO ledger(ts, provider, model, op, units_json, usd_estimate, pricing_version, document_id,"
                " turn_id, cache_hit, local) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    iso(now),
                    provider,
                    model,
                    op,
                    json.dumps(units_json, sort_keys=True),
                    str(usd),
                    self.pricing.version,
                    document_id,
                    turn_id,
                    int(cache_hit),
                    int(local),
                ),
            )
            if free_key is not None:
                self._free_used[free_key] = self._free_used.get(free_key, 0) + units.chars
            month = self.month_key(now)
            if self._month is not None:
                # Same month: add the row. A new month: drop the old total; the next read sums the new month.
                self._month = (month, self._month[1] + usd) if self._month[0] == month else None
        for fn in list(self._listeners):
            try:
                fn(usd)
            except Exception:
                log.exception("ledger listener failed")
        return usd

    def record_call(
        self,
        op: str,
        call: LlmCall,
        *,
        provider: str | None = None,
        document_id: str | None = None,
        turn_id: str | None = None,
    ) -> Decimal:
        """Convenience for the pipeline's `(op, LlmCall)` tuples."""
        if provider is None:
            provider = "fixture" if call.model.startswith("fixture") else "anthropic"
        return self.record(provider=provider, model=call.model, op=op, units=call.units,
                           document_id=document_id, turn_id=turn_id)

    # ------------------------------------------------------------------ free allowances

    def free_period(self, allowance: FreeAllowance, at: datetime | None = None) -> BillingPeriod:
        return billing_period(at or self._now(), allowance.tz)

    def _free_used_locked(self, allowance: FreeAllowance, period: BillingPeriod) -> int:
        key = (allowance.provider, allowance.model, period.key)
        used = self._free_used.get(key)
        if used is None:
            rows = self.db.query(
                "SELECT units_json FROM ledger WHERE provider = ? AND model = ? AND cache_hit = 0 AND local = 0"
                " AND ts >= ?",
                (allowance.provider, allowance.model, iso(period.start)),
            )
            used = 0
            for r in rows:
                try:
                    used += int(json.loads(r["units_json"]).get("chars", 0) or 0)
                except (ValueError, TypeError):
                    continue
            # Drop counters of earlier months for this allowance (one entry per allowance stays).
            for k in [k for k in self._free_used if k[:2] == key[:2]]:
                del self._free_used[k]
            self._free_used[key] = used
        return used

    def _beyond_free_locked(
        self, provider: str, model: str, units: Units, allowance: FreeAllowance | None, now: datetime
    ) -> tuple[Units, tuple[str, str, str] | None]:
        """The units to price (characters past the free allowance only) and the allowance counter they count
        toward (None when the model has no allowance)."""
        if allowance is None:
            return units, None
        period = self.free_period(allowance, now)
        used = self._free_used_locked(allowance, period)
        billable = min(units.chars, max(0, used + units.chars - allowance.chars))
        return dataclasses.replace(units, chars=billable), (provider, model, period.key)

    def free_chars_used(self, allowance: FreeAllowance) -> int:
        """Characters of `allowance` used this billing month by this app (an estimate of the account's use)."""
        with self._month_lock:
            return self._free_used_locked(allowance, self.free_period(allowance))

    # ------------------------------------------------------------------ periods

    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self._tz) if self._tz is not None else dt.astimezone()

    def _local_now(self) -> datetime:
        return self._local(self._now())

    def month_key(self, at: datetime | None = None) -> str:
        """The calendar month (local time) `at` falls in, as "YYYY-MM"; now by default."""
        return self._local(at or self._now()).strftime("%Y-%m")

    def period_start(self, period: Period) -> datetime:
        now = self._local_now()
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "today":
            return day
        if period == "week":
            return day - timedelta(days=day.weekday())
        return day.replace(day=1)

    # ------------------------------------------------------------------ reading

    def _sum(self, column_sql: str, since: datetime, extra: str = "") -> Decimal:
        rows = self.db.query(f"SELECT {column_sql} AS v FROM ledger WHERE ts >= ? {extra}", (iso(since),))
        total = Decimal(0)
        for r in rows:
            if r["v"] is not None:
                total += Decimal(str(r["v"]))
        return total

    def spent(self, since: datetime) -> Decimal:
        return self._sum("usd_estimate", since)

    def month_spent(self) -> Decimal:
        """Month-to-date total, from memory (one SQL sum when first asked and when the month changes)."""
        with self._month_lock:
            now = self._local_now()
            month = now.strftime("%Y-%m")
            if self._month is None or self._month[0] != month:
                start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                self._month = (month, self.spent(start))
            return self._month[1]

    def cache_savings(self, since: datetime) -> Decimal:
        rows = self.db.query("SELECT units_json FROM ledger WHERE ts >= ? AND cache_hit = 1", (iso(since),))
        total = Decimal(0)
        for r in rows:
            try:
                total += Decimal(str(json.loads(r["units_json"]).get("avoided_usd", "0")))
            except (ValueError, ArithmeticError):
                continue
        return total

    def summary(
        self,
        period: Period = "month",
        group_by: GroupBy = "provider",
        *,
        budget_usd: Decimal | str | None = None,
    ) -> UsageSummary:
        since = self.period_start(period)
        col = {"provider": "provider", "op": "op", "document": "document_id"}[group_by]
        rows = self.db.query(
            f"SELECT {col} AS k, usd_estimate AS usd, cache_hit FROM ledger WHERE ts >= ?", (iso(since),)
        )
        lines: dict[str, tuple[Decimal, int]] = {}
        total = Decimal(0)
        for r in rows:
            key = r["k"] if r["k"] is not None else "none"
            usd = Decimal(str(r["usd"]))
            prev_usd, calls = lines.get(key, (Decimal(0), 0))
            lines[key] = (prev_usd + usd, calls + (0 if r["cache_hit"] else 1))
            total += usd
        breakdown = [
            UsageLine(key=k, usd_estimate=money(v), calls=c)
            for k, (v, c) in sorted(lines.items(), key=lambda kv: (-kv[1][0], kv[0]))
        ]
        savings = self.cache_savings(since)
        budget = Decimal(str(budget_usd)) if budget_usd is not None else Decimal(0)
        month_total = total if period == "month" else self.month_spent()
        pct = float(month_total / budget) if budget > 0 else 0.0
        top, share = self._top_pair(since, total)
        spoken = usage_sentence(
            total,
            period,
            top=top,
            top_share=share,
            budget=budget,
            month_pct=pct if period == "month" else None,
            savings=savings,
        )
        return UsageSummary(
            period=period,
            group_by=group_by,
            total_usd=money(total),
            cache_savings_usd=money(savings),
            budget_usd=money(budget),
            budget_pct=round(pct, 4),
            breakdown=breakdown,
            estimated=True,
            spoken=spoken,
        )

    def _top_pair(self, since: datetime, total: Decimal) -> tuple[tuple[str, str] | None, float]:
        """Dominant provider, and that provider's dominant op ("mostly OpenAI speech")."""
        if total <= 0:
            return None, 0.0
        rows = self.db.query("SELECT provider, op, usd_estimate FROM ledger WHERE ts >= ?", (iso(since),))
        by_provider: dict[str, Decimal] = {}
        by_pair: dict[tuple[str, str], Decimal] = {}
        for r in rows:
            usd = Decimal(str(r["usd_estimate"]))
            by_provider[r["provider"]] = by_provider.get(r["provider"], Decimal(0)) + usd
            pair = (r["provider"], r["op"])
            by_pair[pair] = by_pair.get(pair, Decimal(0)) + usd
        provider, pusd = max(by_provider.items(), key=lambda kv: kv[1])
        op = max(((o, v) for (p, o), v in by_pair.items() if p == provider), key=lambda kv: kv[1])[0]
        return (provider, op), float(pusd / total)

    def rows(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: str | None = None,
        op: str | None = None,
        document_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        cache_hit: bool | None = None,
    ) -> LedgerPage:
        where: list[str] = []
        params: list[Any] = []
        for col, val in (("provider", provider), ("op", op), ("document_id", document_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        if since is not None:
            where.append("ts >= ?")
            params.append(iso(since))
        if until is not None:
            where.append("ts < ?")
            params.append(iso(until))
        if cache_hit is not None:
            where.append("cache_hit = ?")
            params.append(int(cache_hit))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total_row = self.db.one(f"SELECT COUNT(*) AS n FROM ledger {clause}", params)
        rows = self.db.query(
            f"SELECT * FROM ledger {clause} ORDER BY id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
        )
        items = [
            LedgerRow(
                id=r["id"],
                ts=r["ts"],
                provider=r["provider"],
                model=r["model"],
                op=r["op"],
                units=json.loads(r["units_json"]),
                usd_estimate=r["usd_estimate"],
                pricing_version=r["pricing_version"],
                document_id=r["document_id"],
                turn_id=r["turn_id"],
                cache_hit=bool(r["cache_hit"]),
                local=bool(r["local"]),
            )
            for r in rows
        ]
        return LedgerPage(items=items, total=int(total_row["n"]) if total_row else 0)
