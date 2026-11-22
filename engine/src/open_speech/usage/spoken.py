"""Usage sentences written for the ear (07 § Writing for the ear): short, numbers rounded sensibly,
always labelled as estimates. E.g. "About three dollars and twenty cents this month, estimated, mostly
OpenAI speech. You're at a third of your ten dollar budget."
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]  # fmt: skip
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

PERIOD_WORDS = {"today": "today", "week": "this week", "month": "this month"}

OP_NOUNS = {
    "tts": "speech",
    "preview": "voice previews",
    "stt": "transcription",
    "annotate": "planning",
    "transform": "summaries",
    "compile": "rule writing",
    "vision": "image descriptions",
    "chat": "the assistant",
}
PROVIDER_NAMES = {"openai": "OpenAI", "gemini": "Gemini", "anthropic": "Claude", "fixture": "test provider"}


def number_words(n: int) -> str:
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (f"-{_ONES[o]}" if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return f"{_ONES[h]} hundred" + (f" {number_words(r)}" if r else "")
    if n < 1_000_000:
        th, r = divmod(n, 1000)
        return f"{number_words(th)} thousand" + (f" {number_words(r)}" if r else "")
    return f"{n:,}"


def money_words(usd: Decimal) -> str:
    """Rounded for speech: cents under a dollar, dimes under ten dollars, dollars above."""
    usd = Decimal(usd)
    if usd <= 0:
        return "nothing"
    if usd < Decimal("0.01"):
        return "less than a cent"
    if usd < 1:
        cents = int((usd * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return f"{number_words(cents)} cent{'s' if cents != 1 else ''}"
    if usd < 10:
        tenths = (usd * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        dollars, dimes = divmod(int(tenths), 10)
        if dollars == 10:
            return "ten dollars"
        d = f"{number_words(dollars)} dollar{'s' if dollars != 1 else ''}"
        return d + (f" and {number_words(dimes * 10)} cents" if dimes else "")
    dollars = int(usd.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{number_words(dollars)} dollars"


def fraction_words(pct: float) -> str:
    """0.33 → "a third"; 0.5 → "half"; 0.12 → "about 12 percent"."""
    named = [(0.25, "a quarter"), (1 / 3, "a third"), (0.5, "half"), (2 / 3, "two thirds"), (0.75, "three quarters")]
    for v, w in named:
        if abs(pct - v) <= 0.03:
            return w
    if pct >= 0.995:
        return "all"
    return f"about {round(pct * 100)} percent"


def budget_words(budget: Decimal) -> str:
    if budget == budget.to_integral_value():
        return f"{number_words(int(budget))} dollar"
    return money_words(budget)


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
    when = PERIOD_WORDS.get(period, period)
    if total <= 0:
        first = f"Nothing spent {when}."
    else:
        amount = money_words(total)
        lead = "About " + amount if amount not in ("less than a cent",) else amount.capitalize()
        first = f"{lead} {when}, estimated"
        if top is not None and top_share >= 0.5:
            provider, op = top
            pname = PROVIDER_NAMES.get(provider, provider)
            noun = "" if provider == "anthropic" else f" {OP_NOUNS.get(op, op)}"
            first += f", {'mostly' if top_share < 0.95 else 'all'} {pname}{noun}"
        first += "."
    parts = [first]
    if month_pct is not None and budget > 0 and total > 0:
        if month_pct >= 1.0:
            parts.append(f"You've reached your {budget_words(budget)} budget.")
        else:
            parts.append(f"You're at {fraction_words(month_pct)} of your {budget_words(budget)} budget.")
    if savings >= Decimal("0.01"):
        parts.append(f"The cache saved you about {money_words(savings)}.")
    return " ".join(parts)
