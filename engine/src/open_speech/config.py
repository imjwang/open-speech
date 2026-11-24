"""Configuration.

Two layers (03 § Storage, 08 § Keys):
- `EngineEnv`: process environment, the repo-root `.env` in development and, in a packaged build, the
  `provider.env` written into the bundled runtime at build time. Provider keys are build configuration:
  users never enter one. SecretStr keys.
- `UserSettings`: `settings.toml` in app data — shortcuts, defaults, budget, retention. This is the
  schema the assistant must be able to read and write in full (FR-ASST-3) and that `GET/PATCH /settings`
  exposes.
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any, Literal

import tomli_w
from platformdirs import user_data_dir
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
# "auto": Google's Chirp 3 HD Zephyr while this build has Google's voices and the month's free characters last
# (or the user chose to keep paying for them), else the local voice (`defaults.local_voice`). Resolved by
# `service/misc.py` `VoiceService` wherever a voice is about to be used; never sent to a provider.
AUTO_VOICE = "auto"
DEFAULT_LOCAL_VOICE = "kokoro:kokoro-82m-v1.0:af_heart"
DEFAULT_VOICE = AUTO_VOICE
DEFAULT_LLM = "claude-haiku-5-5"  # must match `[llm.anthropic] default` in data/models.toml
SHELL_ORIGIN = "app://open-speech"
BUNDLED_RUNTIME_ENV = "OPEN_SPEECH_BUNDLED_RUNTIME"
PROVIDER_ENV_FILE = "provider.env"  # written by app/scripts/bundle-engine.sh from the build environment

Language = Literal["en", "es"]
LANGUAGES: tuple[Language, ...] = ("en", "es")


def env_files() -> tuple[Path, ...]:
    """Where keys and overrides are read from, lowest priority first (the process environment wins over all)."""
    files = [REPO_ROOT / ".env"]
    runtime = os.environ.get(BUNDLED_RUNTIME_ENV)
    if runtime:
        files.append(Path(runtime) / PROVIDER_ENV_FILE)
    return tuple(files)


class EngineEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=env_files(), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    # Google Cloud Text-to-Speech (Chirp 3 HD voices). When unset, the Gemini key is used: a key from the same
    # Google Cloud project works once Cloud Text-to-Speech is among its allowed APIs (providers/keys.py).
    google_tts_api_key: SecretStr | None = None

    open_speech_home: Path | None = None
    open_speech_llm_model: str | None = None
    open_speech_default_voice: str | None = None
    open_speech_monthly_budget_usd: Decimal | None = None
    open_speech_live: bool = False
    # Deterministic, offline providers (fixture TTS/STT/LLM) for tests and the e2e suite. Never on in
    # packaged builds. They appear as provider "fixture" so they can't be mistaken for real voices.
    open_speech_fixture_providers: bool = False
    # "null" plays nothing but keeps real-time playback clocks (tests, UI automation, headless CI).
    open_speech_audio_output: Literal["auto", "null"] = "auto"
    # Extra browser origins allowed to call the engine (dev/test only, e.g. the Vite dev server used
    # by the UI test suite). The shell's own origin is always allowed.
    open_speech_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Let URL fetches reach this machine and the local network (tests against a local server only).
    open_speech_allow_private_urls: bool = False

    @field_validator("anthropic_api_key", "openai_api_key", "gemini_api_key", "google_tts_api_key", mode="before")
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
        self.audio_cache = self.cache / "audio"  # voice assets (store/assets.py)
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
        # Cloned and designed voices (voices/<slug>/: the source, the processed reference, voice.json). Never pruned by
        # recording retention or the cache budget: only deleting the voice removes its folder.
        self.voices = home / "voices"

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
            self.voices,
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
    # "auto" (AUTO_VOICE) or a voice id. Reading and replies both default to "auto": Chirp Zephyr while it is
    # free, the local voice otherwise.
    voice: str = DEFAULT_VOICE
    assistant_voice: str = DEFAULT_VOICE
    # The free voice "auto" falls back to (and every fallback reads with), adjusted to the language being read.
    local_voice: str = DEFAULT_LOCAL_VOICE
    # Preferred voice per language for text the default voice can't speak (e.g. {"es": "kokoro:…:ef_dora"}).
    # Filled in when the user picks a voice while reading or speaking that language.
    voices: dict[str, str] = Field(default_factory=dict)
    speed: float = Field(1.0, ge=0.5, le=5.0)
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
    # "auto" prefers speech-to-text that runs on this Mac, then whatever cloud provider the build includes.
    stt_provider: Literal["auto", "local", "openai", "gemini"] = "auto"
    # After the engine stops speaking, the bar's microphone stays closed this much longer: CoreAudio playback
    # isn't in the bar's echo cancellation, and the room keeps ringing briefly (`assistant.speech` ended).
    echo_tail_ms: int = Field(150, ge=0, le=2000)


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


class Appearance(BaseModel):
    # "system" follows the Mac: lavender (pastel, light) by day, ember (dark, orange) in dark mode.
    theme: Literal["system", "lavender", "ember"] = "system"


class PlaybackSettings(BaseModel):
    fallback: Literal["same_provider", "another_voice", "kokoro"] = "kokoro"
    prefetch_seconds: int = Field(30, ge=5, le=600)
    max_ahead_seconds: int = Field(120, ge=10, le=3600)
    # When a voice's free monthly allowance (Google's Chirp characters) runs out: "local" reads on with the
    # local voice; "pay" keeps the voice at its price (the budget still applies) for the month in
    # `beyond_free_tier_month` only ("YYYY-MM" in the provider's billing time zone), then reverts to "local".
    beyond_free_tier: Literal["local", "pay"] = "local"
    beyond_free_tier_month: str | None = None


class Qwen3Settings(BaseModel):
    """Which Qwen3-TTS weights read the Qwen voices (providers/qwen3.py `resolve_variant`). The voices stay the same;
    "auto" is the size and precision measured best on this kind of Mac (the catalog's defaults; 0.6B with 8 GB)."""

    size: Literal["auto", "0.6b", "1.7b"] = "auto"
    precision: Literal["auto", "bf16", "8bit", "6bit", "5bit", "4bit"] = "auto"


class UserSettings(BaseModel):
    # The language the app speaks, listens for and shows ("auto" = the Mac's, when it is one we support).
    language: Literal["auto", "en", "es"] = "auto"
    appearance: Appearance = Field(default_factory=Appearance)
    shortcuts: Shortcuts = Field(default_factory=Shortcuts)
    defaults: Defaults = Field(default_factory=Defaults)
    budget: Budget = Field(default_factory=Budget)
    voice_input: VoiceInput = Field(default_factory=VoiceInput)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    reader: ReaderSettings = Field(default_factory=ReaderSettings)
    playback: PlaybackSettings = Field(default_factory=PlaybackSettings)
    qwen3: Qwen3Settings = Field(default_factory=Qwen3Settings)
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
