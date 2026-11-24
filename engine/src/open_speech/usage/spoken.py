"""Usage sentences written for the ear (07 § Writing for the ear): short, numbers rounded sensibly,
always labelled as estimates. E.g. "About three dollars and twenty cents this month, estimated, mostly
OpenAI speech. You're at a third of your ten dollar budget."

The wording comes from the message catalog (`i18n`), the numbers from `i18n.say`, so the same sentence is
natural in every supported language.
"""

from __future__ import annotations

from decimal import Decimal

from open_speech.i18n import has, say, t
from open_speech.i18n.say import number_words

PROVIDER_NAMES = {
    "openai": "OpenAI",
    "gemini": "Gemini",
    "google": "Google",
    "anthropic": "Claude",
    "fixture": "test provider",
}

# Kept as the module's public helpers (callers and tests use these names).
money_words = say.money
fraction_words = say.fraction
budget_words = say.budget

__all__ = ["PROVIDER_NAMES", "budget_words", "fraction_words", "money_words", "number_words", "usage_sentence"]


def _top(provider: str, op: str) -> str:
    name = PROVIDER_NAMES.get(provider, provider)
    if provider == "anthropic":
        return name
    return t("usage.provider_op", provider=name, op=t(f"op.{op}") if has(f"op.{op}") else op)


def usage_sentence(
    total: Decimal,
    period: str,
    *,
    top: tuple[str, str] | None,
    top_share: float,
    budget: Decimal,
    month_pct: float | None,
    savings: Decimal,
) -> str:
    """One or two sentences. `top` is the dominant (provider, op) pair; `month_pct` is this month's
    share of the monthly budget (only mentioned for the month period)."""
    when = t(f"period.{period}") if has(f"period.{period}") else period
    if total <= 0:
        first = t("usage.nothing", when=when)
    else:
        if total < Decimal("0.01"):
            first = t("usage.under_a_cent", when=when)
        else:
            first = t("usage.total", amount=say.money(total), when=when)
        if top is not None and top_share >= 0.5:
            first += t("usage.mostly" if top_share < 0.95 else "usage.all", what=_top(*top))
        first += "."
    parts = [first]
    if month_pct is not None and budget > 0 and total > 0:
        if month_pct >= 1.0:
            parts.append(t("usage.budget_reached", budget=say.budget(budget)))
        elif month_pct < 0.01:
            # "about 0 percent of your budget" says nothing useful; say it plainly.
            parts.append(t("usage.budget_well_under", budget=say.budget(budget)))
        else:
            parts.append(t("usage.budget_at", fraction=say.fraction(month_pct), budget=say.budget(budget)))
    if savings >= Decimal("0.01"):
        parts.append(t("usage.cache_saved", amount=say.money(savings)))
    return " ".join(parts)
