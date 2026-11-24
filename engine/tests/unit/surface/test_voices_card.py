"""The voices card offers a short list, not 120 rows (service/commands.py `shortlist_voices`, 09 § Voices), and a
name on it can be said to hear that voice (service/assistant.py `_named_voice`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from open_speech.config import UserSettings
from open_speech.i18n import use_language
from open_speech.providers.base import Voice
from open_speech.service.assistant import _named_voice
from open_speech.service.commands import FEATURED_STARS, Commands, shortlist_voices
from open_speech.service.misc import VoiceService

STARS = ["Achernar", "Aoede", "Charon", "Fenrir", "Kore", "Leda", "Orus", "Puck", "Sulafat", "Zephyr"]
LOCALES = {"en-US": "US", "en-GB": "UK", "es-US": "Latin American Spanish", "es-ES": "Spain"}
KOKORO = ["af_heart", "af_bella", "af_sky", "am_michael", "am_adam", "bf_emma", "bm_george", "bm_lewis",
          "ef_dora", "em_alex", "em_santa"]
QWEN = ["Ryan", "Aiden", "Vivian", "Serena", "Dylan", "Eric"]


def chirp(locale: str, star: str, *, available: bool = True) -> Voice:
    return Voice(id=f"google:chirp3-hd:{locale}-Chirp3-HD-{star}", provider="google", model="chirp3-hd",
                 name=f"{star} (Chirp 3 HD, {LOCALES[locale]})", languages=[locale], available=available,
                 persona=f"chirp3-hd:{star}")


def kokoro(v: str) -> Voice:
    lang = {"a": "en-US", "b": "en-GB", "e": "es"}[v[0]]
    return Voice(id=f"kokoro:kokoro-82m-v1.0:{v}", provider="kokoro", model="kokoro-82m-v1.0",
                 name=f"{v.split('_')[1].title()} (Kokoro, {lang})", languages=[lang], local=True)


def catalog(*, google: bool = True) -> list[Voice]:
    voices = [chirp(loc, s, available=google) for loc in LOCALES for s in STARS]
    voices += [kokoro(v) for v in KOKORO]
    voices += [Voice(id=f"qwen3:qwen3-tts:{n}", provider="qwen3", model="qwen3-tts", name=f"{n} (Qwen3)",
                     languages=["en", "es", "zh"], local=True) for n in QWEN]
    voices.append(Voice(id="qwen3:clone:my-voice", provider="qwen3", model="clone", name="My voice (clone)",
                        languages=["en"], local=True))
    return voices


def engine(voices: list[Voice], *, language: str = "en", current: str | None = None) -> Any:
    settings = UserSettings().with_value("language", language)

    def auto_cloud_voice(lang: str) -> str | None:
        loc = "en-US" if lang == "en" else "es-US"
        vid = f"google:chirp3-hd:{loc}-Chirp3-HD-Zephyr"
        return vid if any(v.id == vid and v.available for v in voices) else None

    e = SimpleNamespace(
        registry=SimpleNamespace(voices=lambda: voices, auto_cloud_voice=auto_cloud_voice),
        settings=SimpleNamespace(get=lambda: settings),
        documents=SimpleNamespace(voice_for=lambda _doc: current),
        playback=SimpleNamespace(current_document_id=lambda: None),
        clones=SimpleNamespace(available=lambda: None, store=SimpleNamespace(get=lambda _slug: None)),
    )
    e.voices = VoiceService(e)  # type: ignore[arg-type]
    return e


def names(voices: list[Voice]) -> list[str]:
    return [v.id.rsplit(":", 1)[-1] for v in voices]


def test_the_short_list_in_english() -> None:
    sl = shortlist_voices(engine(catalog()), "en", "kokoro:kokoro-82m-v1.0:af_heart")
    assert names(sl.short) == [
        "af_heart",  # the current voice first
        *[f"en-US-Chirp3-HD-{s}" for s in FEATURED_STARS],  # six Google voices, in the first English locale
        "af_bella", "bf_emma", "am_michael", "bm_george",  # Kokoro's best for English
        "Ryan", "Aiden", "Vivian", "Serena",  # another local provider: its first few
        "my-voice",  # the user's clone
    ]
    assert len(sl.short) == 16 and len(sl.rest) < 60
    assert [v.provider for v in sl.rest[:15]] == ["google"] * 14 + ["kokoro"]  # the rest: Google, then local
    assert not any(v.languages and v.languages[0].startswith("es") for v in sl.rest)


def test_the_short_list_in_spanish() -> None:
    sl = shortlist_voices(engine(catalog(), language="es"), "es", None)
    assert names(sl.short)[:6] == [f"es-US-Chirp3-HD-{s}" for s in FEATURED_STARS]
    assert "ef_dora" in names(sl.short) and "em_alex" in names(sl.short) and "em_santa" not in names(sl.short)
    assert "af_heart" not in names(sl.short + sl.rest)


def test_without_google_the_list_is_local() -> None:
    sl = shortlist_voices(engine(catalog(google=False)), "en", None)
    assert all(v.local for v in sl.short) and not any(v.provider == "google" for v in sl.rest)


def test_the_card_says_little_and_offers_the_rest() -> None:
    use_language("en")
    e = engine(catalog(), current="google:chirp3-hd:en-US-Chirp3-HD-Zephyr")
    reply = Commands(e, SimpleNamespace()).voices_card("t1")  # type: ignore[arg-type]
    assert reply.text == "You're using Zephyr. Here are 15 more; say a name to hear one."
    assert reply.card is not None
    rows = reply.card.rows
    assert rows[0].selected and rows[0].title == "Zephyr"
    more, clone = rows[-2], rows[-1]
    assert more.id == "voices:more" and more.data["command"] == "more voices"
    # ＋ "Clone a voice" ends the card: a command row, the same as saying it.
    assert clone.id == "voices:clone" and clone.data == {"command": "clone my voice", "icon": "clone"}
    mine = next(r for r in rows if r.id == "qwen3:clone:my-voice")
    assert mine.data["clone"] and mine.data["slug"] == "my-voice"
    assert mine.data["delete_command"] == "delete the voice My voice"
    assert more.subtitle and more.subtitle.startswith("14 Google")
    full = Commands(e, SimpleNamespace()).voices_card("t1", more=True)  # type: ignore[arg-type]
    assert full.card is not None and len(full.card.rows) == int(more.value or 0)
    assert full.text.startswith("Here are ")
    google = Commands(e, SimpleNamespace()).voices_card("t1", more=True, provider="kokoro")  # type: ignore[arg-type]
    assert google.card is not None and {r.data["provider"] for r in google.card.rows} == {"kokoro"}


def test_saying_a_name_on_the_card_plays_it() -> None:
    shown = {"kokoro:kokoro-82m-v1.0:af_heart": "Heart", "google:chirp3-hd:es-US-Chirp3-HD-Aoede": "Aoede"}
    assert _named_voice(shown, "Heart") == "kokoro:kokoro-82m-v1.0:af_heart"
    assert _named_voice(shown, "play heart.") == "kokoro:kokoro-82m-v1.0:af_heart"
    assert _named_voice(shown, "¿Escuchar Aoede?") == "google:chirp3-hd:es-US-Chirp3-HD-Aoede"
    assert _named_voice(shown, "use Heart") is None  # switching is another command
    assert _named_voice(shown, "Bella") is None and _named_voice({}, "Heart") is None
