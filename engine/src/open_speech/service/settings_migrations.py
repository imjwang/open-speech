"""One-time changes to an existing settings.toml when a default changes (03 § Storage).

`settings.toml` stores every key, so a new default never reaches a profile that already exists: the old default
is written there like any choice the user made. A migration moves the old default to the new one, once. The
IDs of applied migrations are kept in the database (`kv` table), so a deliberate change back afterwards
sticks. A fresh profile starts with the current defaults and records every migration as applied without
running it (an `OPEN_SPEECH_LLM_MODEL` that seeded the file is a choice, not an old default).

To add one: write `def _<name>(s: UserSettings) -> UserSettings | None` that returns the changed settings (or
None when this profile has nothing to change) and append it to `MIGRATIONS` with a dated id. Never reorder,
rename or remove an entry.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass

from open_speech.config import AUTO_VOICE, DEFAULT_LLM, UserSettings
from open_speech.service.settings import SettingsService
from open_speech.store.db import Database

log = logging.getLogger("open_speech.settings")

KV_KEY = "settings_migrations"  # JSON list of applied migration ids


@dataclass(frozen=True)
class SettingsMigration:
    id: str  # "YYYY-MM-<what>"; stored once applied
    summary: str  # for the log
    apply: Callable[[UserSettings], UserSettings | None]


def _llm_haiku(s: UserSettings) -> UserSettings | None:
    """Claude Sonnet 5.5 was the default model until round 3; Haiku 5.5 is faster and about 20x cheaper.
    A profile still on the old default moves to the new one."""
    if s.llm.model != "claude-sonnet-5-5":
        return None
    return s.with_value("llm.model", DEFAULT_LLM)


OLD_DEFAULT_VOICE = "kokoro:kokoro-82m-v1.0:af_heart"  # defaults.voice and assistant_voice until round 3
# The owner's own profile read with Gemini's paid Zephyr. The round-3 plan (agreed with the owner) moves it to
# "auto": the owner chose Chirp for reading and replies, and "auto" is Chirp's Zephyr — the same voice name — free
# for the first million characters a month. Only this exact value moves; any other voice someone chose stays.
OWNER_GEMINI_ZEPHYR = "gemini:gemini-3.8-flash-tts:Zephyr"
_TO_AUTO = {OLD_DEFAULT_VOICE, OWNER_GEMINI_ZEPHYR}


def _voice_auto(s: UserSettings) -> UserSettings | None:
    """Round 3 makes "auto" the default reading and reply voice (Chirp 3 HD Zephyr while it is free, the local
    voice otherwise). A profile still on the old default Kokoro Heart — or on Gemini Zephyr, see above — moves to
    "auto"; the local voice stays Kokoro Heart (`defaults.local_voice`)."""
    out = s
    for key in ("defaults.voice", "defaults.assistant_voice"):
        if out.get(key) in _TO_AUTO:
            out = out.with_value(key, AUTO_VOICE)
    return None if out is s else out


MIGRATIONS: list[SettingsMigration] = [
    SettingsMigration("2026-11-llm-haiku", "assistant and AI Enhanced model: Sonnet 5.5 → Haiku 5.5", _llm_haiku),
    SettingsMigration("2026-11-voice-auto", "reading and reply voice: old default, Gemini Zephyr → auto", _voice_auto),
]


def applied_ids(db: Database) -> set[str]:
    raw = db.get_kv(KV_KEY)
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("unreadable %s in the database; treating every migration as pending", KV_KEY)
        return set()
    return {str(x) for x in data} if isinstance(data, list) else set()


def run_settings_migrations(
    settings: SettingsService, db: Database, migrations: list[SettingsMigration] | None = None
) -> list[str]:
    """Apply every migration this profile hasn't had yet. Returns the ids that changed settings."""
    migrations = MIGRATIONS if migrations is None else migrations
    done = applied_ids(db)
    pending = [m for m in migrations if m.id not in done]
    if not pending:
        return []
    changed: list[str] = []
    if settings.created:
        log.info("new profile: settings start at the current defaults (%d migration(s) marked applied)", len(pending))
        done.update(m.id for m in pending)
    else:
        current = settings.get()
        for m in pending:
            try:
                new = m.apply(current)
            except Exception:  # noqa: BLE001 — a broken migration must not stop the engine; it is retried next start
                log.exception("settings migration %s failed", m.id)
                continue
            if new is not None and new != current:
                current = new
                changed.append(m.id)
                log.info("settings migration %s: %s", m.id, m.summary)
            else:
                log.info("settings migration %s: nothing to change", m.id)
            done.add(m.id)
        if changed:
            settings.replace(current)
    db.set_kv(KV_KEY, json.dumps(sorted(done)))
    return changed
