"""Configuration.

Two layers (03 § Storage, 08 § Keys):
- `EngineEnv`: process environment + the repo-root `.env` (development keys, overrides). SecretStr keys.
- `UserSettings`: `settings.toml` in app data — shortcuts, defaults, budget, retention, modes. This is the
  schema the assistant must be able to read and write in full (FR-ASST-3) and that `GET/PATCH /settings`
  exposes.
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import tomli_w
from platformdirs import user_data_dir
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_VOICE = "kokoro:kokoro-82m-v1.0:af_heart"
DEFAULT_LLM = "claude-sonnet-5-5"
SHELL_ORIGIN = "app://open-speech"


class EngineEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None

    open_speech_home: Path | None = None
    open_speech_llm_model: str | None = None
    open_speech_default_voice: str | None = None
    open_speech_monthly_budget_usd: Decimal | None = None
    open_speech_live: bool = False
    # Deterministic, offline providers (fixture TTS/STT/LLM) for tests and the e2e suite. Never on in
    # packaged builds. They appear as provider "fixture" so they can't be mistaken for real voices.
    open_speech_fixture_providers: bool = False
    # Extra browser origins allowed to call the engine (dev/test only, e.g. the Vite dev server used
    # by the UI test suite). The shell's own origin is always allowed.
    open_speech_allowed_origins: list[str] = Field(default_factory=list)

    @field_validator("anthropic_api_key", "openai_api_key", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return None if v in ("", None) else v

    @field_validator("open_speech_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @cached_property
    def home(self) -> Path:
        base = self.open_speech_home or Path(user_data_dir("OpenSpeech", appauthor=False))
        return Path(base).expanduser()


class Paths:
    """Every on-disk location under app data (03 § Storage)."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.db = home / "db.sqlite"
        self.cache = home / "cache"
        self.audio_cache = self.cache / "audio"
        self.extract_cache = self.cache / "extract"
        self.recordings = home / "recordings"
        self.rules = home / "rules"
        self.speech_md = self.rules / "SPEECH.md"
        self.lexicon = self.rules / "lexicon.csv"
        self.skills = home / "skills"
        self.models = home / "models"
        self.models_toml = home / "models.toml"
        self.pricing_toml = home / "pricing.toml"
        self.settings_toml = home / "settings.toml"
        self.engine_json = home / "engine.json"
        self.logs = home / "logs"
        self.uploads = home / "uploads"

    def ensure(self) -> Paths:
        for d in (
            self.home,
            self.audio_cache,
            self.extract_cache,
            self.recordings,
            self.rules,
            self.skills,
            self.models,
            self.logs,
            self.uploads,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self


# ---------------------------------------------------------------- settings.toml


class Shortcuts(BaseModel):
    open_bar: str = "Alt+Space"
    play_pause: str = "Alt+Shift+Space"
    read_clipboard: str = "Alt+Shift+R"
    read_selection: str = "Alt+A"  # P1
    screenshot: str = "Alt+X"  # P1


class Defaults(BaseModel):
    voice: str = DEFAULT_VOICE
    assistant_voice: str = DEFAULT_VOICE  # 10 § Open questions: Kokoro by default (fast, $0)
    speed: float = Field(1.0, ge=0.5, le=5.0)
    mode: Literal["classic", "ai_enhanced"] = "classic"
    style: str | None = None


class Budget(BaseModel):
    monthly_usd: str = "10"  # decimal string (04: money is never a float)
    warn_at: float = Field(0.8, ge=0.0, le=1.0)
    hard_stop: bool = True
    estimate_threshold_usd: str = "0.50"

    @field_validator("monthly_usd", "estimate_threshold_usd")
    @classmethod
    def _decimal(cls, v: str) -> str:
        d = Decimal(str(v))
        if d < 0:
            raise ValueError("must be ≥ 0")
        return str(d)


class VoiceInput(BaseModel):
    end_of_speech_ms: int = Field(800, ge=200, le=5000)
    recording_retention_days: int = Field(30, ge=0, le=3650)  # 0 = never keep
    stt_provider: Literal["auto", "openai", "gemini", "local"] = "auto"


class CacheSettings(BaseModel):
    max_gb: float = Field(5.0, ge=0.1, le=500)


class LlmSettings(BaseModel):
    model: str = DEFAULT_LLM
    annotate_effort: Literal["low", "medium", "high"] = "low"
    assistant_effort: Literal["low", "medium", "auto"] = "low"


class ReaderSettings(BaseModel):
    font_size: int = Field(18, ge=10, le=48)
    line_spacing: float = Field(1.6, ge=1.0, le=3.0)
    font: Literal["system", "atkinson", "opendyslexic"] = "system"
    high_contrast: bool = False
    theme: Literal["system", "light", "dark"] = "system"


class PlaybackSettings(BaseModel):
    fallback: Literal["same_provider", "another_voice", "kokoro"] = "kokoro"
    prefetch_seconds: int = Field(30, ge=5, le=600)
    max_ahead_seconds: int = Field(120, ge=10, le=3600)


class UserSettings(BaseModel):
    shortcuts: Shortcuts = Field(default_factory=Shortcuts)
    defaults: Defaults = Field(default_factory=Defaults)
    budget: Budget = Field(default_factory=Budget)
    voice_input: VoiceInput = Field(default_factory=VoiceInput)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    reader: ReaderSettings = Field(default_factory=ReaderSettings)
    playback: PlaybackSettings = Field(default_factory=PlaybackSettings)
    onboarding_completed: bool = False

    # ------------------------------------------------------------ dotted-key access (assistant tools)

    @classmethod
    def keys(cls) -> list[str]:
        """Every leaf key, dotted (e.g. "budget.monthly_usd"). FR-ASST-3 parity test iterates this."""
        out: list[str] = []
        for name, f in cls.model_fields.items():
            ann = f.annotation
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                out.extend(f"{name}.{sub}" for sub in ann.model_fields)
            else:
                out.append(name)
        return out

    def get(self, key: str) -> Any:
        obj: Any = self
        for part in key.split("."):
            if not hasattr(obj, part):
                raise KeyError(key)
            obj = getattr(obj, part)
        return obj

    def with_value(self, key: str, value: Any) -> UserSettings:
        """Return a validated copy with one dotted key changed."""
        if key not in self.keys():
            raise KeyError(key)
        data = self.model_dump()
        parts = key.split(".")
        cur = data
        for p in parts[:-1]:
            cur = cur[p]
        cur[parts[-1]] = value
        return UserSettings.model_validate(data)


def load_user_settings(path: Path, env: EngineEnv | None = None) -> UserSettings:
    data: dict[str, Any] = {}
    if path.exists():
        data = tomllib.loads(path.read_text("utf-8"))
    s = UserSettings.model_validate(data)
    if env is not None and not path.exists():
        # Env overrides seed a fresh settings file only; after that the file is the source of truth.
        if env.open_speech_default_voice:
            s.defaults.voice = env.open_speech_default_voice
        if env.open_speech_llm_model:
            s.llm.model = env.open_speech_llm_model
        if env.open_speech_monthly_budget_usd is not None:
            s.budget.monthly_usd = str(env.open_speech_monthly_budget_usd)
    return s


def save_user_settings(path: Path, settings: UserSettings) -> None:
    data = settings.model_dump(mode="json", exclude_none=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(data), "utf-8")
    os.replace(tmp, path)
