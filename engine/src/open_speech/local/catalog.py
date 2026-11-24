"""Model catalog (08 § Model catalog): the bundled `data/models.toml` deep-merged with the user's
`models.toml` in app data. Every model ID the engine uses comes from here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from open_speech.config import Paths

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
BUNDLED_MODELS = DATA_DIR / "models.toml"


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Tables merge recursively; scalars and lists in `over` replace those in `base`."""
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class WeightFile(BaseModel):
    path: str
    sha256: str = ""
    size_mb: float = 0.0
    url: str | None = None  # full download URL, for a file that isn't under the weights' base URL


class Weights(BaseModel):
    url: str  # "hf://<repo>" or an https base URL
    revision: str = "main"
    quant: str | None = None
    size_mb: float = 0.0
    sha256: str = ""
    model_file: str = "model.onnx"
    tokenizer_file: str | None = None
    voice_file: str | None = None  # pattern with {voice}
    files: list[WeightFile] = Field(default_factory=list)
    voice_size_mb: float = 0.0
    voice_sha256: dict[str, str] = Field(default_factory=dict)  # voice name → sha256 of its voice file

    def voice_files(self, voices: list[str]) -> list[WeightFile]:
        """One pinned file per voice (when the weights have per-voice files)."""
        if not self.voice_file:
            return []
        return [WeightFile(path=self.voice_file.format(voice=v), sha256=self.voice_sha256.get(v, ""),
                           size_mb=self.voice_size_mb) for v in voices]

    def base_url(self, hf_base: str = "https://huggingface.co") -> str:
        if self.url.startswith("hf://"):
            return f"{hf_base.rstrip('/')}/{self.url[5:]}/resolve/{self.revision}"
        return self.url.rstrip("/")

    def all_files(self) -> list[WeightFile]:
        files = list(self.files)
        if not any(f.path == self.model_file for f in files):
            files.insert(0, WeightFile(path=self.model_file, sha256=self.sha256, size_mb=self.size_mb))
        if self.tokenizer_file and not any(f.path == self.tokenizer_file for f in files):
            files.append(WeightFile(path=self.tokenizer_file))
        return files


class TtsModel(BaseModel):
    provider: str
    model: str
    name: str = ""
    snapshot: str | None = None
    voices: list[str] = Field(default_factory=list)
    # A voice list written as locales × names (Google Chirp 3 HD: "{locale}-Chirp3-HD-{star}"): when
    # `voice_template` is set, `voices` is expanded from `locales` and `stars` (locale-major) and `languages`
    # defaults to `locales`. `voice_locale()` / `voice_star()` read a voice name back.
    voice_template: str | None = None
    locales: list[str] = Field(default_factory=list)
    stars: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    format: str | None = None
    style: str | None = None  # "instructions" | "speech_metadata" | None
    stream: bool = False
    max_input_chars: int = 4000
    timestamps: bool = False
    local: bool = False
    languages: list[str] = Field(default_factory=list)
    language_count: int | None = None
    sample_rate: int | None = None
    api: str | None = None
    api_version: str | None = None
    voices_endpoint: bool = False
    weights: Weights | None = None
    ram_gb: float = 0.0
    size_gb: float | None = None
    license: str | None = None
    runtimes: list[str] = Field(default_factory=list)
    requires: str | None = None
    status: str | None = None  # e.g. "later" (P2)
    speed: str | None = None
    max_tokens: int | None = None
    test_only: bool = False
    # openai_compatible (P1)
    base_url: str | None = None
    api_model: str | None = None
    response_format: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    def voice_id(self, voice: str) -> str:
        return f"{self.provider}:{self.model}:{voice}"

    def templated_voices(self) -> list[str]:
        if not self.voice_template:
            return []
        return [self.voice_template.format(locale=loc, star=star) for loc in self.locales for star in self.stars]

    def voice_parts(self, voice: str) -> tuple[str, str] | None:
        """(locale, star) of a templated voice name ("en-US-Chirp3-HD-Zephyr" → ("en-US", "Zephyr"))."""
        if not self.voice_template:
            return None
        for loc in self.locales:
            for star in self.stars:
                if self.voice_template.format(locale=loc, star=star) == voice:
                    return loc, star
        return None

    @property
    def size_mb(self) -> float:
        if self.weights is not None:
            return self.weights.size_mb + self.weights.voice_size_mb * len(self.voices)
        if self.size_gb is not None:
            return self.size_gb * 1024
        return 0.0


class SttCatalog(BaseModel):
    provider: str
    streaming: list[str] = Field(default_factory=list)
    batch: list[str] = Field(default_factory=list)
    word_timestamps: list[str] = Field(default_factory=list)
    api_version: str | None = None
    test_only: bool = False


LOCAL_STT = "local"  # provider id of on-device speech-to-text (`[stt.local."<model>"]`)
LOCAL_STT_DIR = "stt"  # weights live in `<models>/stt/<model>/` (app data and the packaged runtime alike)


class LocalSttModel(BaseModel):
    """An on-device speech-to-text model: a sherpa-onnx offline transducer (07 § STT routing, 08 § Local
    models). File names are relative to the model directory."""

    provider: str = LOCAL_STT
    model: str
    name: str = ""
    family: str = "nemo_transducer"  # sherpa-onnx transducer `model_type`
    encoder: str = "encoder.int8.onnx"
    decoder: str = "decoder.int8.onnx"
    joiner: str = "joiner.int8.onnx"
    tokens: str = "tokens.txt"
    vad: str | None = None  # Silero VAD model that keeps non-speech away from the recognizer
    languages: list[str] = Field(default_factory=list)
    num_threads: int = 2
    weights: Weights | None = None
    ram_gb: float = 0.0
    license: str | None = None
    speed: str | None = None
    status: str | None = None  # e.g. "later"
    requires: str | None = None
    test_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def size_mb(self) -> float:
        return self.weights.size_mb if self.weights is not None else 0.0

    def required_files(self) -> list[str]:
        """Every file the recognizer opens; the model is installed when all of them exist."""
        if self.weights is not None:
            return [f.path for f in self.weights.all_files()]
        return [f for f in (self.encoder, self.decoder, self.joiner, self.tokens, self.vad) if f]


class WeightsVariant(BaseModel):
    """One downloadable set of weights behind a user-facing model (`[local_weights.<provider>."<id>"]`): Qwen3-TTS
    comes in sizes × families × precisions (`qwen3-tts-0.6b-customvoice-8bit`), each its own repository, generated
    by `engine/tools/pin_hf_weights.py`. The voices themselves (`[tts.qwen3."qwen3-tts"]`) don't change with the
    variant; settings choose which variant reads them (providers/qwen3.py `resolve_variant`). Installed in
    `models/<provider>/<id>/`."""

    provider: str
    model: str  # the variant id (the model manager's id and directory name)
    family: str = ""  # "base" (cloning) | "customvoice" (preset speakers) | "voicedesign" (described voices)
    size: str = ""  # "0.6b" | "1.7b"
    precision: str = ""  # "bf16" | "8bit" | "6bit" | "5bit" | "4bit"
    name: str = ""
    speakers: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    weights: Weights | None = None
    ram_gb: float = 0.0
    license: str | None = None
    requires: str | None = None
    speed: str | None = None
    status: str | None = None
    test_only: bool = False

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def size_mb(self) -> float:
        return self.weights.size_mb if self.weights is not None else 0.0

    def required_files(self) -> list[str]:
        return [f.path for f in self.weights.all_files()] if self.weights is not None else []


class LlmTraits(BaseModel):
    """How requests to one model are shaped (`[llm.<provider>.models."<id>"]`). The defaults are what is safe
    for a model the catalog doesn't know: the thinking parameter left out (the model's own default, adaptive
    on every current Claude model) and no server-side refusal fallback."""

    thinking_off: Literal["disabled", "between_tools", "omit"] = "omit"
    server_fallback: bool = False
    annotate_thinking: Literal["off", "adaptive"] = "off"

    def thinking_off_param(self) -> dict[str, str] | None:
        """The `thinking` value that turns thinking off, or None when the parameter must be left out."""
        return None if self.thinking_off == "omit" else {"type": self.thinking_off}

    @property
    def can_disable_thinking(self) -> bool:
        return self.thinking_off != "omit"


class LlmCatalog(BaseModel):
    provider: str
    default: str
    options: list[str] = Field(default_factory=list)
    annotate_max_tokens: int = 4096
    transform_max_tokens: int = 16000
    compile_max_tokens: int = 2048
    vision_max_tokens: int = 4096
    thinking_reserve_tokens: int = 4096  # added to max_tokens when a call may think (thinking counts toward it)
    models: dict[str, LlmTraits] = Field(default_factory=dict)
    test_only: bool = False

    def traits(self, model: str) -> LlmTraits:
        """Request traits for `model`; safe defaults for one the catalog doesn't list."""
        return self.models.get(model) or LlmTraits()


_TTS_KNOWN = set(TtsModel.model_fields) - {"provider", "model", "extra", "language_count"}


class ModelCatalog:
    """Parsed, merged catalog. Construct with `ModelCatalog.load(paths)` or `ModelCatalog.from_dict()`."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.raw = data
        self.tts: dict[str, TtsModel] = {}  # "provider:model" → entry
        self.stt: dict[str, SttCatalog] = {}
        self.local_stt: dict[str, LocalSttModel] = {}  # model id → entry, from `[stt.local."<model>"]`
        self.local_stt_default: str | None = None  # `[stt.local] default`
        self.llm: dict[str, LlmCatalog] = {}
        self.variants: dict[str, WeightsVariant] = {}  # variant id → entry, from `[local_weights.<provider>."<id>"]`
        self._parse()

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, paths: Paths | None = None, *, bundled: Path = BUNDLED_MODELS) -> ModelCatalog:
        data = tomllib.loads(bundled.read_text("utf-8"))
        if paths is not None and paths.models_toml.exists():
            data = deep_merge(data, tomllib.loads(paths.models_toml.read_text("utf-8")))
        return cls(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelCatalog:
        return cls(data)

    def _parse(self) -> None:
        for provider, models in (self.raw.get("tts") or {}).items():
            if not isinstance(models, dict):
                continue
            for model_id, entry in models.items():
                if not isinstance(entry, dict):
                    continue
                resolved = dict(entry)
                seen = {model_id}
                while "inherit" in resolved:
                    parent_id = resolved.pop("inherit")
                    if parent_id in seen or parent_id not in models:
                        break
                    seen.add(parent_id)
                    resolved = deep_merge(models[parent_id], resolved)
                resolved.pop("inherit", None)
                self.tts[f"{provider}:{model_id}"] = self._tts_entry(provider, model_id, resolved)
        for provider, entry in (self.raw.get("stt") or {}).items():
            if not isinstance(entry, dict):
                continue
            if provider == LOCAL_STT:
                self._parse_local_stt(entry)
                continue
            self.stt[provider] = SttCatalog(provider=provider, **_known(entry, SttCatalog))
        for provider, entry in (self.raw.get("llm") or {}).items():
            if isinstance(entry, dict) and "default" in entry:
                self.llm[provider] = LlmCatalog(provider=provider, **_known(entry, LlmCatalog))
        for provider, table in (self.raw.get("local_weights") or {}).items():
            if not isinstance(table, dict):
                continue
            for vid, entry in table.items():
                if isinstance(entry, dict):
                    v = WeightsVariant(provider=provider, model=vid, **_known(entry, WeightsVariant, "model"))
                    if v.license is None or v.requires is None:
                        parent = next((m for m in self.tts.values() if m.provider == provider), None)
                        if parent is not None:
                            v.license = v.license or parent.license
                            v.requires = v.requires or parent.requires
                    self.variants[vid] = v

    def _parse_local_stt(self, table: dict[str, Any]) -> None:
        """`[stt.local]`: `default = "<model>"` plus one sub-table per model."""
        for model_id, entry in table.items():
            if isinstance(entry, dict):
                self.local_stt[model_id] = LocalSttModel(model=model_id, **_known(entry, LocalSttModel, "model"))
        default = table.get("default")
        self.local_stt_default = default if isinstance(default, str) else None

    @staticmethod
    def _tts_entry(provider: str, model_id: str, d: dict[str, Any]) -> TtsModel:
        fields = {k: v for k, v in d.items() if k in _TTS_KNOWN}
        extra = {k: v for k, v in d.items() if k not in _TTS_KNOWN}
        langs = fields.pop("languages", [])
        language_count: int | None = None
        if isinstance(langs, int):
            language_count, langs = langs, []
        m = TtsModel(
            provider=provider,
            model=model_id,
            languages=list(langs),
            language_count=language_count,
            extra=extra,
            **fields,
        )
        if m.voice_template and not m.voices:
            m.voices = m.templated_voices()
            if not m.languages:
                m.languages = list(m.locales)
        return m

    # ------------------------------------------------------------------ queries

    def tts_models(self, provider: str | None = None, *, include_test: bool = False) -> list[TtsModel]:
        return [
            m
            for m in self.tts.values()
            if (provider is None or m.provider == provider) and (include_test or not m.test_only)
        ]

    def tts_model(self, provider: str, model: str) -> TtsModel | None:
        return self.tts.get(f"{provider}:{model}")

    def local_models(self) -> list[TtsModel]:
        return [m for m in self.tts.values() if m.local and not m.test_only]

    def local_model(self, model_id: str) -> TtsModel | None:
        """Look up a local model by "model" or "provider:model"."""
        for m in self.local_models():
            if model_id in (m.model, m.key):
                return m
        return None

    def stt_for(self, provider: str) -> SttCatalog | None:
        return self.stt.get(provider)

    def local_stt_models(self) -> list[LocalSttModel]:
        """On-device speech-to-text models, the default first."""
        models = [m for m in self.local_stt.values() if not m.test_only]
        return sorted(models, key=lambda m: m.model != self.local_stt_default)

    def local_stt_model(self, model_id: str) -> LocalSttModel | None:
        """Look up a local speech-to-text model by "model" or "local:model"."""
        for m in self.local_stt_models():
            if model_id in (m.model, m.key):
                return m
        return None

    def default_local_stt_model(self) -> LocalSttModel | None:
        """`[stt.local] default`; when that names no entry, the first one."""
        return next(iter(self.local_stt_models()), None)

    def weights_variants(self, provider: str | None = None) -> list[WeightsVariant]:
        """Downloadable weight variants (Qwen3-TTS sizes × families × precisions), catalog order."""
        return [v for v in self.variants.values()
                if (provider is None or v.provider == provider) and not v.test_only]

    def weights_variant(self, variant_id: str) -> WeightsVariant | None:
        """Look up a variant by "id" or "provider:id"."""
        for v in self.weights_variants():
            if variant_id in (v.model, v.key):
                return v
        return None

    def llm_for(self, provider: str) -> LlmCatalog | None:
        return self.llm.get(provider)

    def llm_traits(self, model: str, provider: str = "anthropic") -> LlmTraits:
        entry = self.llm.get(provider)
        return entry.traits(model) if entry is not None else LlmTraits()


def _known(entry: dict[str, Any], model: type[BaseModel], *skip: str) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k in model.model_fields and k != "provider" and k not in skip}
