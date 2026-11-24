"""Settings migrations (service/settings_migrations.py): an old default stored in settings.toml moves to the new
one once per profile; a choice made afterwards sticks; a fresh profile starts at today's defaults."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from open_speech.config import EngineEnv, Paths, UserSettings, save_user_settings
from open_speech.service.settings import SettingsService
from open_speech.service.settings_migrations import (
    KV_KEY,
    MIGRATIONS,
    SettingsMigration,
    applied_ids,
    run_settings_migrations,
)
from open_speech.store.db import Database

from .helpers import make_env


def _old_profile(paths: Paths, **values: Any) -> None:
    """A settings.toml written by an earlier version: every key stored, with the defaults of the time."""
    s = UserSettings()
    for key, value in values.items():
        s = s.with_value(key, value)
    save_user_settings(paths.settings_toml, s)


def _start(paths: Paths, db: Database, env: EngineEnv | None = None) -> SettingsService:
    service = SettingsService(paths, env or make_env())
    run_settings_migrations(service, db)
    return service


def test_old_default_model_moves_to_haiku_once_and_a_later_choice_sticks(
    paths: Paths, db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    _old_profile(paths, **{"llm.model": "claude-sonnet-5-5"})
    with caplog.at_level(logging.INFO, logger="open_speech.settings"):
        service = _start(paths, db)
    assert service.get().llm.model == "claude-haiku-5-5"
    assert "claude-haiku-5-5" in paths.settings_toml.read_text()  # written, not only in memory
    assert "2026-11-llm-haiku" in applied_ids(db)
    assert any("2026-11-llm-haiku" in r.getMessage() for r in caplog.records)

    # The user deliberately goes back to Sonnet ("use Sonnet for the assistant"); restarts don't undo it.
    service.replace(service.get().with_value("llm.model", "claude-sonnet-5-5"))
    for _ in range(2):
        assert _start(paths, db).get().llm.model == "claude-sonnet-5-5"


def test_a_model_the_user_chose_is_left_alone(paths: Paths, db: Database) -> None:
    _old_profile(paths, **{"llm.model": "claude-opus-5-5"})
    assert _start(paths, db).get().llm.model == "claude-opus-5-5"
    assert "2026-11-llm-haiku" in applied_ids(db)  # done: it won't fire if the user picks Sonnet later


def test_a_fresh_profile_starts_at_the_current_defaults(paths: Paths, db: Database) -> None:
    service = _start(paths, db)
    assert service.created and service.get().llm.model == "claude-haiku-5-5"
    assert applied_ids(db) == {m.id for m in MIGRATIONS}


def test_a_model_seeded_from_the_environment_is_a_choice(paths: Paths, db: Database) -> None:
    service = _start(paths, db, make_env(open_speech_llm_model="claude-sonnet-5-5"))
    assert service.get().llm.model == "claude-sonnet-5-5"
    assert _start(paths, db).get().llm.model == "claude-sonnet-5-5"


def test_each_migration_runs_once_and_a_failing_one_is_retried(paths: Paths, db: Database) -> None:
    _old_profile(paths)
    runs: list[str] = []

    def font(s: UserSettings) -> UserSettings | None:
        runs.append("font")
        return s.with_value("reader.font_size", 20)

    def broken(s: UserSettings) -> UserSettings | None:
        runs.append("broken")
        raise RuntimeError("bug")

    migrations = [SettingsMigration("t-font", "bigger text", font), SettingsMigration("t-broken", "oops", broken)]
    service = SettingsService(paths, make_env())
    assert run_settings_migrations(service, db, migrations) == ["t-font"]
    assert service.get().reader.font_size == 20
    assert run_settings_migrations(service, db, migrations) == []
    assert runs == ["font", "broken", "broken"]  # the broken one is tried again; the applied one is not
    assert json.loads(db.get_kv(KV_KEY) or "[]") == ["t-font"]


def test_the_engine_migrates_before_anything_reads_settings(tmp_path: Path) -> None:
    from open_speech.service.engine import Engine

    home = tmp_path / "home"
    paths = Paths(home).ensure()
    _old_profile(paths, **{"llm.model": "claude-sonnet-5-5"})
    env = EngineEnv(  # type: ignore[call-arg]
        _env_file=None, open_speech_home=home, open_speech_fixture_providers=True, open_speech_audio_output="null",
        anthropic_api_key=None, openai_api_key=None, gemini_api_key=None,
    )
    engine = Engine(env)
    try:
        assert engine.settings.get().llm.model == "claude-haiku-5-5"
        assert engine.registry.llm_model() == "fixture-llm"  # no key: the fixture LLM, unaffected
    finally:
        engine.db.close()


# ------------------------------------------------------------------ 2026-11-voice-auto

HEART = "kokoro:kokoro-82m-v1.0:af_heart"
GEMINI_ZEPHYR = "gemini:gemini-3.8-flash-tts:Zephyr"


def test_old_default_voices_move_to_auto_once(paths: Paths, db: Database) -> None:
    _old_profile(paths, **{"defaults.voice": HEART, "defaults.assistant_voice": HEART})
    service = _start(paths, db)
    d = service.get().defaults
    assert (d.voice, d.assistant_voice, d.local_voice) == ("auto", "auto", HEART)
    assert 'voice = "auto"' in paths.settings_toml.read_text()
    assert "2026-11-voice-auto" in applied_ids(db)
    # Choosing Kokoro Heart on purpose afterwards sticks across restarts.
    service.replace(service.get().with_value("defaults.voice", HEART))
    for _ in range(2):
        assert _start(paths, db).get().defaults.voice == HEART


def test_the_owners_gemini_zephyr_profile_moves_to_auto(paths: Paths, db: Database) -> None:
    # The owner's real profile: reading with Gemini Zephyr, replies with the old default.
    _old_profile(paths, **{"defaults.voice": GEMINI_ZEPHYR, "defaults.assistant_voice": HEART})
    service = _start(paths, db)
    d = service.get().defaults
    assert (d.voice, d.assistant_voice, d.speed) == ("auto", "auto", 1.0)  # nothing else changes
    service.replace(service.get().with_value("defaults.voice", GEMINI_ZEPHYR))  # chosen again later: kept
    assert _start(paths, db).get().defaults.voice == GEMINI_ZEPHYR


@pytest.mark.parametrize(
    "voice",
    ["kokoro:kokoro-82m-v1.0:am_adam", "gemini:gemini-3.8-flash-tts:Kore", "gemini:gemini-3.8-flash-lite-tts:Zephyr",
     "openai:gpt-4o-mini-tts:coral", "google:chirp3-hd:en-GB-Chirp3-HD-Zephyr"],
)
def test_other_voices_people_chose_are_left_alone(paths: Paths, db: Database, voice: str) -> None:
    _old_profile(paths, **{"defaults.voice": voice, "defaults.assistant_voice": voice,
                           "defaults.voices": {"es": "kokoro:kokoro-82m-v1.0:ef_dora"}})
    d = _start(paths, db).get().defaults
    assert (d.voice, d.assistant_voice) == (voice, voice)
    assert d.voices == {"es": "kokoro:kokoro-82m-v1.0:ef_dora"}
    assert "2026-11-voice-auto" in applied_ids(db)


def test_a_fresh_profile_starts_on_auto(paths: Paths, db: Database) -> None:
    service = _start(paths, db)
    assert service.created and service.get().defaults.voice == "auto"
    assert "2026-11-voice-auto" in applied_ids(db)
