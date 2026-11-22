"""Usage ledger (08 § Usage ledger, FR-COST-1/2): one row per billable call, cache hit or local run.

- Paid calls: `usd_estimate` from `Pricing`.
- Cache hits: `$0` with `cache_hit = 1` and the *avoided* cost in `units_json.avoided_usd`.
- Local runs (Kokoro): `$0` with `local = 1`.
- Unknown models: `$0` with `units_json.unpriced = true` so the gap is visible.

Periods (today / week / month) are calendar periods in the machine's local time zone; timestamps are
stored as ISO 8601 UTC.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal

from open_speech.api.schemas import LedgerPage, LedgerRow, UsageLine, UsageSummary
from open_speech.providers.base import LlmCall, Units
from open_speech.store.db import Database
from open_speech.usage.pricing import Pricing, money
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
        if cache_hit:
            usd = Decimal(0)
            if avoided_usd is None:
                avoided_usd = Decimal(0) if local else self.pricing.cost(provider, model, units)
            units_json["avoided_usd"] = money(avoided_usd)
        elif local:
            usd = Decimal(0)
        else:
            usd = self.pricing.cost(provider, model, units)
            if not priced:
                units_json["unpriced"] = True
                log.warning("no price for %s/%s; recorded at $0", provider, model)
        self.db.execute(
            "INSERT INTO ledger(ts, provider, model, op, units_json, usd_estimate, pricing_version, document_id,"
            " turn_id, cache_hit, local) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                iso(self._now()),
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

    # ------------------------------------------------------------------ periods

    def _local_now(self) -> datetime:
        now = self._now()
        return now.astimezone(self._tz) if self._tz is not None else now.astimezone()

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
        return self.spent(self.period_start("month"))

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
