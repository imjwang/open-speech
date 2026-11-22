"""Numbers, money, durations and sizes written for the ear, in the current language.

Replies are spoken by a text-to-speech voice, so they avoid symbols a voice may stumble on ("$3.20", "1.5x"
read as "one point five ex") while staying short. English spells small numbers out; Spanish keeps digits,
which every Spanish voice reads correctly, and uses a decimal comma.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from open_speech.i18n import current_language, t, tn

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]  # fmt: skip
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def number_words(n: int) -> str:
    """English cardinal ("forty-two"). Other languages say digits."""
    if n < 0:
        return "minus " + number_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return f"{_ONES[h]} hundred" + (f" {number_words(r)}" if r else "")
    if n < 1_000_000:
        th, r = divmod(n, 1000)
        return f"{number_words(th)} thousand" + (f" {number_words(r)}" if r else "")
    return f"{n:,}"


def number(n: int) -> str:
    return number_words(n) if current_language() == "en" else str(n)


def decimal(x: float, places: int = 2) -> str:
    """1.5 → "1.5" / "1,5"; trailing zeros dropped."""
    text = f"{x:.{places}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") if current_language() == "es" else text


def speed(x: float) -> str:
    """On screen and aloud: "1.5x" / "1,5x"."""
    return f"{decimal(x)}x"


def money(usd: Decimal) -> str:
    """Rounded for speech: cents under a dollar, dimes under ten dollars, dollars above."""
    usd = Decimal(usd)
    if usd <= 0:
        return t("money.nothing")
    if usd < Decimal("0.01"):
        return t("money.under_a_cent")
    if usd < 1:
        cents = int((usd * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return tn("money.cents", cents, words=number(cents))
    if usd < 10:
        tenths = (usd * 10).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        dollars, dimes = divmod(int(tenths), 10)
        if dimes:
            return tn("money.dollars_cents", dollars, dollars=number(dollars), cents=number(dimes * 10))
        return tn("money.dollars", dollars, words=number(dollars))
    dollars = int(usd.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return tn("money.dollars", dollars, words=number(dollars))


def money_figure(usd: Decimal) -> str:
    """For the screen: "$20", "$7.50"."""
    usd = Decimal(usd)
    return f"${usd:.2f}" if usd != usd.to_integral_value() else f"${int(usd)}"


def budget(usd: Decimal) -> str:
    """ "ten dollar" as in "your ten dollar budget" (English); Spanish says "de 10 dólares"."""
    usd = Decimal(usd)
    if current_language() == "en":
        # Before a noun the unit is singular: "your ten dollar budget", "your nine cent budget".
        if usd == usd.to_integral_value():
            return f"{number_words(int(usd))} dollar"
        if Decimal("0.01") <= usd < 1:
            cents = int((usd * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            return f"{number_words(cents)} cent"
    return money(usd)


def fraction(pct: float) -> str:
    """0.33 → "a third"; 0.5 → "half"; 0.12 → "about 12 percent"."""
    for value, key in (
        (0.25, "quarter"),
        (1 / 3, "third"),
        (0.5, "half"),
        (2 / 3, "two_thirds"),
        (0.75, "three_quarters"),
    ):
        if abs(pct - value) <= 0.03:
            return t(f"fraction.{key}")
    if pct >= 0.995:
        return t("fraction.all")
    return t("fraction.percent", n=round(pct * 100))


def duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return t("duration.hm", h=tn("duration.hours", hours), m=tn("duration.minutes", minutes))
    if hours:
        return tn("duration.hours", hours)
    if minutes:
        return tn("duration.minutes", minutes)
    return t("duration.under_a_minute")


def size(nbytes: int) -> str:
    gb = nbytes / 1e9
    if gb >= 0.1:
        return t("size.gb", n=decimal(gb, 1))
    return t("size.mb", n=max(1, round(nbytes / 1e6)))
