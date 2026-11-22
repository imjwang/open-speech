"""Monthly budget (08 § Budgets, FR-COST-4).

- At `warn_at` (default 80%): one `usage.alert` (level "warn") per threshold crossing.
- At 100% with `hard_stop`: paid calls are refused with `BudgetExceeded`; one "stop" alert per crossing.
  Reading continues on the local voice (the synthesizer falls back).

"Once per crossing" survives restarts: the last alert level for the current month and budget is kept in
the `kv` table. Raising the budget or a new month re-arms the alerts. A budget of 0 with hard stop means
"no paid calls".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from open_speech.api.ws_schema import UsageAlert
from open_speech.config import UserSettings
from open_speech.errors import BudgetExceeded
from open_speech.service.events import EventBus
from open_speech.usage.ledger import Ledger
from open_speech.usage.pricing import money
from open_speech.usage.spoken import budget_words, money_words

log = logging.getLogger(__name__)
_KV = "budget_alert_state"
Level = Literal["none", "warn", "stop"]
_RANK = {"none": 0, "warn": 1, "stop": 2}


class BudgetGuard:
    def __init__(self, ledger: Ledger, settings: Callable[[], UserSettings], events: EventBus) -> None:
        self.ledger = ledger
        self._settings = settings
        self._events = events
        ledger.add_listener(lambda _usd: self.after_record())

    # ------------------------------------------------------------------ state

    def budget(self) -> Decimal:
        return Decimal(self._settings().budget.monthly_usd)

    def spent(self) -> Decimal:
        return self.ledger.month_spent()

    def pct(self) -> float:
        b = self.budget()
        return float(self.spent() / b) if b > 0 else 0.0

    def allow_paid(self) -> bool:
        s = self._settings().budget
        if not s.hard_stop:
            return True
        b = Decimal(s.monthly_usd)
        return self.spent() < b if b > 0 else False

    def check_paid(self) -> None:
        """Raise `BudgetExceeded` when a paid call would break the hard stop."""
        if self.allow_paid():
            return
        spent, budget = self.spent(), self.budget()
        self._alert("stop", spent, budget)
        raise BudgetExceeded(money(spent), money(budget))

    # ------------------------------------------------------------------ alerts

    def _period_key(self) -> str:
        s = self._settings().budget
        month = self.ledger.period_start("month").strftime("%Y-%m")
        return f"{month}|{Decimal(s.monthly_usd)}|{s.warn_at}"

    def _last_level(self) -> Level:
        raw = self.ledger.db.get_kv(_KV)
        if not raw:
            return "none"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "none"
        if data.get("key") != self._period_key():
            return "none"
        lvl = data.get("level", "none")
        return lvl if lvl in _RANK else "none"  # type: ignore[no-any-return]

    def _set_level(self, level: Level) -> None:
        self.ledger.db.set_kv(_KV, json.dumps({"key": self._period_key(), "level": level}))

    def after_record(self) -> None:
        """Emit `usage.alert` once per threshold crossing (and re-arm when spend drops below)."""
        s = self._settings().budget
        budget = Decimal(s.monthly_usd)
        if budget <= 0:
            return
        spent = self.spent()
        pct = float(spent / budget)
        level: Level = "stop" if pct >= 1.0 else ("warn" if pct >= s.warn_at else "none")
        last = self._last_level()
        if _RANK[level] < _RANK[last]:
            self._set_level(level)  # spend fell (budget raised mid-month etc.): re-arm
            return
        if _RANK[level] > _RANK[last]:
            self._alert(level, spent, budget)

    def _alert(self, level: Level, spent: Decimal, budget: Decimal) -> None:
        if level == "none" or _RANK[self._last_level()] >= _RANK[level]:
            return  # nothing to say, or this crossing was already announced
        hard = self._settings().budget.hard_stop
        if level == "warn":
            msg = (
                f"Heads up: you've used about {round(float(spent / budget) * 100)} percent of your "
                f"{budget_words(budget)} budget this month (about {money_words(spent)}, estimated)."
            )
        elif hard:
            msg = (
                f"You've reached your {budget_words(budget)} monthly budget. I'll keep reading with the local "
                "voice; say 'raise my budget' to change it."
            )
        else:
            msg = f"You've passed your {budget_words(budget)} monthly budget (about {money_words(spent)}, estimated)."
        self._set_level(level)
        self._events.publish(UsageAlert(level=level, spent=money(spent), budget=money(budget), message=msg))
