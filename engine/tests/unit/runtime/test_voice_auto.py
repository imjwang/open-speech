"""The "auto" voice (service/misc.py `VoiceService`): Chirp Zephyr in the language's locale while Google's voices
can be used and the month's free characters last, else the local voice — silently for structural reasons, with a
notice reason for runtime ones; the same speaker in another locale for another language (persona); and reading
started with "auto" saying why once."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_speech.api import schemas as s
from open_speech.config import AUTO_VOICE, Paths, UserSettings
from open_speech.i18n import t
from open_speech.providers.base import Units
from open_speech.service.misc import VoiceService
from open_speech.store.db import Database

from .helpers import SettingsBox, make_env
from .test_free_tier import Rt, runtime

ZEPHYR_EN = "google:chirp3-hd:en-US-Chirp3-HD-Zephyr"
ZEPHYR_ES = "google:chirp3-hd:es-US-Chirp3-HD-Zephyr"
HEART = "kokoro:kokoro-82m-v1.0:af_heart"
DORA = "kokoro:kokoro-82m-v1.0:ef_dora"


def service(rt: Rt, settings: SettingsBox) -> VoiceService:
    engine = SimpleNamespace(registry=rt.registry, settings=SimpleNamespace(get=settings), free_tier=rt.guard,
                             budget=rt.budget)
    return VoiceService(engine)  # type: ignore[arg-type]


@pytest.fixture
def rt(paths: Paths, db: Database, settings: SettingsBox) -> Rt:
    return runtime(paths, db, settings, at=datetime(2026, 11, 22, tzinfo=UTC), fixture=False)


def test_defaults_are_auto_with_kokoro_heart_as_the_local_voice() -> None:
    d = UserSettings().defaults
    assert d.voice == AUTO_VOICE and d.assistant_voice == AUTO_VOICE and d.local_voice == HEART
    assert UserSettings().playback.beyond_free_tier == "local"


def test_auto_is_chirp_zephyr_in_the_languages_locale(rt: Rt, settings: SettingsBox) -> None:
    v = service(rt, settings)
    assert v.resolve(AUTO_VOICE, "en") == (ZEPHYR_EN, None)
    assert v.resolve(AUTO_VOICE, "es") == (ZEPHYR_ES, None)
    assert v.effective(AUTO_VOICE) == ZEPHYR_EN  # no language: the app's (English in tests)
    assert v.effective(HEART, "es") == HEART  # a chosen voice is returned as is
    assert v.assistant_voice() == ZEPHYR_EN
    assert v.reply_voice("es") == (ZEPHYR_ES, None)


def test_without_google_auto_is_the_local_voice(paths: Paths, db: Database, settings: SettingsBox) -> None:
    from open_speech.local.catalog import ModelCatalog
    from open_speech.providers.keys import ProviderKeys
    from open_speech.providers.registry import ProviderRegistry

    env = make_env()
    registry = ProviderRegistry(env, paths, ModelCatalog.load(paths), ProviderKeys(env), settings=settings)
    engine = SimpleNamespace(registry=registry, settings=SimpleNamespace(get=settings), free_tier=None, budget=None)
    v = VoiceService(engine)  # type: ignore[arg-type]
    assert v.resolve(AUTO_VOICE, "en") == (HEART, None)  # quietly: nothing changed for this build
    assert v.resolve(AUTO_VOICE, "es") == (DORA, None)  # the local voice, in Spanish
    settings.set("defaults.local_voice", "kokoro:kokoro-82m-v1.0:bm_george")
    assert v.effective(AUTO_VOICE, "en") == "kokoro:kokoro-82m-v1.0:bm_george"


def test_blocked_google_is_the_local_voice_without_a_notice(rt: Rt, settings: SettingsBox) -> None:
    check = rt.registry.google_check()
    check.state, check.blocked = "blocked", "blocked"
    assert service(rt, settings).resolve(AUTO_VOICE, "en") == (HEART, None)


def test_used_up_free_characters_are_the_local_voice_with_a_reason(rt: Rt, settings: SettingsBox) -> None:
    rt.ledger.record(provider="google", model="chirp3-hd", op="tts", units=Units(chars=980_000))
    v = service(rt, settings)
    assert v.resolve(AUTO_VOICE, "en") == (HEART, "free_tier")
    assert v.resolve(AUTO_VOICE, "es") == (DORA, "free_tier")
    assert v.notice_text("free_tier") == t("notice.free_tier", target=t("notice.local_voice"))
    settings.set("playback.beyond_free_tier", "pay")
    settings.set("playback.beyond_free_tier_month", "2026-11")
    assert v.resolve(AUTO_VOICE, "en") == (ZEPHYR_EN, None)  # "keep using the Google voice"
    settings.set("budget.monthly_usd", "0")  # ...but the budget's hard stop still applies past the allowance
    rt.ledger.record(provider="google", model="chirp3-hd", op="tts", units=Units(chars=30_000))
    assert v.resolve(AUTO_VOICE, "en") == (HEART, "budget")


def test_persona_keeps_the_speaker_in_another_language(rt: Rt, settings: SettingsBox) -> None:
    v = service(rt, settings)
    assert v.for_language("google:chirp3-hd:en-US-Chirp3-HD-Kore", "es") == "google:chirp3-hd:es-US-Chirp3-HD-Kore"
    assert v.for_language("google:chirp3-hd:es-ES-Chirp3-HD-Kore", "en") == "google:chirp3-hd:en-US-Chirp3-HD-Kore"
    assert v.for_language("google:chirp3-hd:en-GB-Chirp3-HD-Kore", "en") == "google:chirp3-hd:en-GB-Chirp3-HD-Kore"
    assert v.for_language(AUTO_VOICE, "es") == ZEPHYR_ES


def test_a_voice_picked_for_another_language_wins_over_auto(rt: Rt, settings: SettingsBox) -> None:
    settings.set("defaults.voices", {"es": "google:chirp3-hd:es-ES-Chirp3-HD-Kore", "en": HEART})
    v = service(rt, settings)
    assert v.for_language(AUTO_VOICE, "es") == "google:chirp3-hd:es-ES-Chirp3-HD-Kore"
    # For the app's own language the default voice decides, and the default is "auto".
    assert v.for_language(AUTO_VOICE, "en") == ZEPHYR_EN


async def test_reading_with_auto_after_the_free_characters_says_why_once(tmp_path: Path) -> None:
    from open_speech.service.engine import Engine

    engine = Engine(make_env(open_speech_home=tmp_path / "home", open_speech_fixture_providers=True,
                             gemini_api_key="g-gem", open_speech_audio_output="null"))
    said: list[str] = []
    engine.transport.speak_notice = said.append
    try:
        assert engine.settings.get().defaults.voice in (AUTO_VOICE, "fixture:tone-v1:alto")
        await engine.settings.set_key("defaults.voice", AUTO_VOICE)
        engine.ledger.record(provider="google", model="chirp3-hd", op="tts", units=Units(chars=990_000))
        notice = t("notice.free_tier", target=t("notice.local_voice"))
        ids: list[str] = []
        for text in ("A first thing to read. It has two sentences.", "Something else to read afterwards."):
            res = await engine.documents.create(s.CreateDocumentRequest(kind="text", text=text))
            ids.append(res.document.id)
        await engine.playback.play(s.PlayRequest(document_id=ids[0]))
        snap = engine.transport.snapshot()
        assert snap.voice_id == "fixture:tone-v1:alto" and snap.fallback_notice == notice
        await engine.playback.play(s.PlayRequest(document_id=ids[1]))
        assert said == [notice]  # once per session, not per document
        card = engine.assistant.commands.settings_card("t1")
        row = next(r for r in card.rows if r.id == "defaults.voice")
        assert row.data["voice_id"] == AUTO_VOICE and row.data["effective_voice_id"] == "fixture:tone-v1:alto"
        assert row.value.startswith("Automatic (")
    finally:
        await engine.shutdown()


async def test_choosing_auto_clears_voices_picked_for_other_languages(tmp_path: Path) -> None:
    from open_speech.service.engine import Engine

    engine = Engine(make_env(open_speech_home=tmp_path / "home", open_speech_fixture_providers=True,
                             open_speech_audio_output="null"))
    try:
        await engine.settings.set_key("defaults.voices", {"es": "kokoro:kokoro-82m-v1.0:ef_dora"})
        await engine.voices.set_default(AUTO_VOICE)
        d = engine.settings.get().defaults
        assert d.voice == AUTO_VOICE
        assert d.voices == {}  # Dora no longer outranks the automatic choice for Spanish
    finally:
        await engine.shutdown()
