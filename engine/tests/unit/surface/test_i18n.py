"""What the engine says, in English and Spanish (open_speech.i18n)."""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from pathlib import Path

import pytest

import open_speech
from open_speech import i18n
from open_speech.i18n import say, t, tn

SRC = Path(open_speech.__file__).parent
PLACEHOLDER = re.compile(r"\{(\w+)[^}]*\}")


def _placeholders(text: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(text))


def test_spanish_has_every_message_english_has() -> None:
    en, es = i18n.CATALOGS["en"], i18n.CATALOGS["es"]
    assert sorted(es) == sorted(en)
    for key in en:
        assert _placeholders(es[key]) == _placeholders(en[key]), key
        assert en[key].strip() and es[key].strip(), key


def test_every_message_the_code_asks_for_exists() -> None:
    """A typo in a key would only show up when that sentence is finally spoken; catch it here instead."""
    en = i18n.CATALOGS["en"]
    singles = re.compile(r"""(?<![\w.])t\(\s*"([a-z0-9_.]+)\"""")
    plurals = re.compile(r"""(?<![\w.])tn\(\s*"([a-z0-9_.]+)\"""")
    missing: list[str] = []
    used = 0
    for path in SRC.rglob("*.py"):
        if path.parent.name == "i18n":
            continue
        source = path.read_text("utf-8")
        for key in singles.findall(source):
            used += 1
            if key not in en:
                missing.append(f"{path.name}: {key}")
        for key in plurals.findall(source):
            used += 1
            missing += [f"{path.name}: {key}.{form}" for form in ("one", "other") if f"{key}.{form}" not in en]
    assert used > 150, "the scan should find the engine's messages"
    assert missing == []


def test_nothing_the_engine_says_asks_for_a_key_or_an_account() -> None:
    for lang, catalog in i18n.CATALOGS.items():
        for key, text in catalog.items():
            assert not re.search(
                r"\b(api key|your key|a key|paste|clave|sign in|log in|account|cuenta)\b", text, re.I
            ), (
                lang,
                key,
            )


def test_plurals_and_fallback_to_english() -> None:
    assert tn("reply.reading_many", 1) == "Reading 1 item."
    assert tn("reply.reading_many", 3) == "Reading 3 items, one after another."
    i18n.set_default_language("es")
    assert tn("reply.reading_many", 1) == "Leyendo 1 elemento."
    assert t("turn.empty") == "Di o escribe lo que quieres que haga."
    # An unknown language means English, never a crash.
    assert i18n.set_default_language("fr") == "en"
    assert t("turn.empty").startswith("Say or type")


def test_spoken_numbers_money_and_time() -> None:
    cases = {
        "en": [
            "1.5x",
            "2x",
            "one dollar and twenty cents",
            "less than a cent",
            "nothing",
            "half",
            "a third",
            "1 hour 1 minute",
        ],
        "es": [
            "1,5x",
            "2x",
            "1 dólar con 20 centavos",
            "menos de un centavo",
            "nada",
            "la mitad",
            "un tercio",
            "1 hora y 1 minuto",
        ],
    }
    for lang, expected in cases.items():
        i18n.set_default_language(lang)
        assert [
            say.speed(1.5),
            say.speed(2.0),
            say.money(Decimal("1.2")),
            say.money(Decimal("0.004")),
            say.money(Decimal("0")),
            say.fraction(0.5),
            say.fraction(0.33),
            say.duration(3700),
        ] == expected
    # Before "budget" English uses the singular; Spanish says "presupuesto de …".
    i18n.set_default_language("en")
    assert [say.budget(Decimal("20")), say.budget(Decimal("0.09")), say.budget(Decimal("0.01"))] == [
        "twenty dollar",
        "nine cent",
        "one cent",
    ]
    i18n.set_default_language("es")
    assert say.budget(Decimal("20")) == "20 dólares"
    # Figures on screen keep one format in every language.
    assert say.money_figure(Decimal("1.2")) == "$1.20"


def test_language_setting() -> None:
    assert i18n.normalize("es-MX") == "es"
    assert i18n.normalize("EN_us") == "en"
    assert i18n.normalize("fr") is None
    assert i18n.resolve_language("es") == "es"
    assert i18n.resolve_language("auto") == "en"  # the test Mac "speaks" English (see conftest)
    assert i18n.resolve_language(None) == "en"


def test_auto_follows_the_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "system_language", lambda: "es")
    assert i18n.resolve_language("auto") == "es"
    assert i18n.resolve_language("en") == "en"
    monkeypatch.setattr(i18n, "system_language", lambda: "de")
    assert i18n.resolve_language("auto") == "en"


async def test_one_turn_can_answer_in_another_language_without_changing_the_rest() -> None:
    """A Spanish sentence typed into an English interface is answered in Spanish, for that turn only."""

    async def turn(lang: str) -> str:
        i18n.use_language(lang)
        await asyncio.sleep(0)
        return t("turn.empty")

    spanish, english = await asyncio.gather(asyncio.create_task(turn("es")), asyncio.create_task(turn("en")))
    assert spanish.startswith("Di o escribe")
    assert english.startswith("Say or type")
    assert i18n.current_language() == "en"
