"""Provider allowances that cost nothing until they are used up (08 § Budgets), e.g. a voice provider's free
characters per month. Each provider that has one registers a `FreeTierSource`; `BudgetState.free_tiers` (the
spend chip, `GET /usage/budget`, `usage.changed`) lists what every source reports.

The registry starts empty: a source is added by the provider that owns the allowance. `FreeTierGuard` (below)
is the source for allowances declared in pricing.toml (`free_chars_per_month`: Google's free Chirp characters);
the engine registers it.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from open_speech.api.schemas import FreeTierState
from open_speech.api.ws_schema import UsageAlert
from open_speech.config import UserSettings
from open_speech.errors import EngineError
from open_speech.i18n import has, say, t
from open_speech.service.events import EventBus
from open_speech.usage.ledger import Ledger
from open_speech.usage.pricing import BillingPeriod, FreeAllowance, money

log = logging.getLogger(__name__)


@runtime_checkable
class FreeTierSource(Protocol):
    def free_tiers(self) -> list[FreeTierState]:
        """This source's allowances for the current period: used and limit, counted from this app's own use
        (so an estimate), and the date the allowance resets. Must be cheap: it runs on every budget read."""
        ...


class FreeTierRegistry:
    def __init__(self) -> None:
        self._sources: list[FreeTierSource] = []
        self._lock = threading.Lock()

    def register(self, source: FreeTierSource) -> None:
        with self._lock:
            if source not in self._sources:
                self._sources.append(source)

    def unregister(self, source: FreeTierSource) -> None:
        with self._lock:
            if source in self._sources:
                self._sources.remove(source)

    def states(self) -> list[FreeTierState]:
        """Every registered allowance, in registration order. A source that fails is left out (and logged):
        the budget read itself must never fail because of one provider."""
        with self._lock:
            sources = list(self._sources)
        out: list[FreeTierState] = []
        for src in sources:
            try:
                out.extend(src.free_tiers())
            except Exception:  # noqa: BLE001
                log.exception("free-tier source %r failed", src)
        return out


# ------------------------------------------------------------------ Google's free Chirp characters (and alike)

_ALERT_KV = "free_tier_alert_state"  # {"<provider>:<model>": {"period": "YYYY-MM", "level": "none|warn|stop"}}
_RANK = {"none": 0, "warn": 1, "stop": 2}
Policy = Literal["local", "pay"]
Coverage = Literal["none", "free", "paid"]


class FreeTierExhausted(EngineError):
    """A voice's free allowance for this month is used up and the user hasn't chosen to keep paying for it:
    the synthesizer reads on with the local voice (like the budget stop)."""

    def __init__(self, provider: str, used: int, limit: int) -> None:
        super().__init__("budget_exceeded", t("free_tier.exhausted"), provider=provider, used=used, limit=limit)


@dataclass(frozen=True)
class FreeTierUse:
    allowance: FreeAllowance
    period: BillingPeriod
    used: int
    policy: Policy

    @property
    def limit(self) -> int:
        return self.allowance.chars

    @property
    def stop_at(self) -> int:
        """Where reading switches to the local voice under the "local" policy: the allowance less a reserve for
        use of the same Google account that this app can't see."""
        return int(self.limit * (1 - RESERVE)) if self.policy == "local" else self.limit

    @property
    def remaining(self) -> int:
        """Free characters left before the switch (or, under "pay", before characters start to cost)."""
        return max(0, self.stop_at - self.used)


RESERVE = 0.02  # 2 % of the allowance kept back for use the app can't see
WARN_AT = 0.8


class FreeTierGuard:
    """Free monthly allowances (pricing.toml `free_chars_per_month`) for paid voices: Google's Chirp 3 HD.

    - `check()` before a provider call: "free" (the whole call fits: no cost, the budget doesn't apply), "paid"
      (beyond the allowance and the user chose to keep paying: the budget applies) or `FreeTierExhausted`
      (used up under the default "local" policy, which stops at the allowance less a 2 % reserve).
    - `playback.beyond_free_tier = "pay"` holds only for the billing month in `beyond_free_tier_month`; a new
      month is "local" again without anyone changing the setting back.
    - Alerts (`usage.alert`, level "warn": reading goes on either way) at 80 % and when the allowance is used
      up, once per allowance and billing month (kept in the `kv` table, like the budget alerts).
    - It is the `FreeTierSource` for every allowance whose provider this build can use (`available`).
    """

    def __init__(
        self,
        ledger: Ledger,
        settings: Callable[[], UserSettings],
        events: EventBus,
        *,
        available: Callable[[str], bool] | None = None,
    ) -> None:
        self.ledger = ledger
        self._settings = settings
        self._events = events
        self._available = available or (lambda _provider: True)

    # ------------------------------------------------------------ state

    def allowance(self, provider: str, model: str) -> FreeAllowance | None:
        return self.ledger.pricing.free_allowance(provider, model)

    def period(self, allowance: FreeAllowance) -> BillingPeriod:
        return self.ledger.free_period(allowance)

    def policy(self, allowance: FreeAllowance, period: BillingPeriod | None = None) -> Policy:
        pb = self._settings().playback
        period = period or self.period(allowance)
        return "pay" if pb.beyond_free_tier == "pay" and pb.beyond_free_tier_month == period.key else "local"

    def use(self, provider: str, model: str) -> FreeTierUse | None:
        allowance = self.allowance(provider, model)
        if allowance is None:
            return None
        period = self.period(allowance)
        return FreeTierUse(allowance, period, self.ledger.free_chars_used(allowance), self.policy(allowance, period))

    def exhausted(self, provider: str, model: str) -> bool:
        """The allowance is used up and the user hasn't chosen to keep paying: reading uses the local voice."""
        u = self.use(provider, model)
        return u is not None and u.policy == "local" and u.used >= u.stop_at

    def check(self, provider: str, model: str, chars: int) -> Coverage:
        u = self.use(provider, model)
        if u is None:
            return "none"
        if u.used + chars <= u.stop_at:
            return "free"
        if u.policy == "pay":
            return "paid"
        self.after_record()  # the "used up" alert, if it wasn't said yet
        raise FreeTierExhausted(provider, u.used, u.limit)

    # ------------------------------------------------------------ alerts

    def _states(self) -> dict[str, Any]:
        raw = self.ledger.db.get_kv(_ALERT_KV)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        return data if isinstance(data, dict) else {}

    def after_record(self, *_: Any) -> None:
        """After every ledger row (a listener): announce 80 % and "used up" once per allowance and month."""
        states = self._states()
        changed = False
        for allowance in self.ledger.pricing.free_allowances():
            if not self._available(allowance.provider):
                continue
            u = self.use(allowance.provider, allowance.model)
            if u is None:
                continue
            name = f"{allowance.provider}:{allowance.model}"
            prev = states.get(name) if isinstance(states.get(name), dict) else None
            last = prev.get("level", "none") if prev and prev.get("period") == u.period.key else "none"
            level = "stop" if u.used >= u.stop_at else ("warn" if u.used >= u.limit * WARN_AT else "none")
            if _RANK.get(level, 0) <= _RANK.get(last, 0):
                if prev is None or prev.get("period") != u.period.key:
                    states[name] = {"period": u.period.key, "level": level}
                    changed = True
                continue
            states[name] = {"period": u.period.key, "level": level}
            changed = True
            self._alert(level, u)
        if changed:
            self.ledger.db.set_kv(_ALERT_KV, json.dumps(states, sort_keys=True))

    def _alert(self, level: str, u: FreeTierUse) -> None:
        rate = say.money(u.allowance.per_kchars)
        if level == "warn":
            msg = t("free_tier.warn", pct=round(u.used / u.limit * 100))
        elif u.policy == "local":
            msg = t("free_tier.stop", rate=rate)
        else:
            msg = t("free_tier.passed", rate=rate)
        budget = Decimal(self._settings().budget.monthly_usd)
        self._events.publish(
            UsageAlert(level="warn", spent=money(self.ledger.month_spent()), budget=money(budget), message=msg)
        )

    # ------------------------------------------------------------ FreeTierSource

    def free_tiers(self) -> list[FreeTierState]:
        out: list[FreeTierState] = []
        for allowance in self.ledger.pricing.free_allowances():
            if not self._available(allowance.provider):
                continue
            u = self.use(allowance.provider, allowance.model)
            if u is None:
                continue
            out.append(
                FreeTierState(
                    provider=allowance.provider,
                    label=t(f"free_tier.label.{allowance.provider}")
                    if has(f"free_tier.label.{allowance.provider}")
                    else allowance.provider,
                    unit="chars",
                    used=u.used,
                    limit=u.limit,
                    resets_on=u.period.resets_on.isoformat(),
                )
            )
        return out
