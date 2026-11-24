from __future__ import annotations

from pathlib import Path

import pytest

from open_speech.config import DEFAULT_LLM, Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.keys import ProviderKeys

from .helpers import make_env

# ------------------------------------------------------------------ catalog


def test_bundled_catalog_matches_spec(catalog: ModelCatalog) -> None:
    oa = catalog.tts_model("openai", "gpt-4o-mini-tts")
    assert oa is not None
    assert oa.snapshot == "gpt-4o-mini-tts-2025-12-15"
    assert oa.style == "instructions" and "coral" in oa.voices and oa.max_input_chars == 4000
    g = catalog.tts_model("gemini", "gemini-3.8-flash-tts")
    assert g is not None and g.api == "interactions" and "Kore" in g.voices and len(g.voices) == 30
    lite = catalog.tts_model("gemini", "gemini-3.8-flash-lite-tts")
    assert lite is not None
    assert lite.voices == g.voices  # inherit
    assert lite.language_count == 101 and lite.style == "speech_metadata"
    k = catalog.tts_model("kokoro", "kokoro-82m-v1.0")
    assert k is not None and k.local and k.timestamps and k.weights is not None
    assert k.weights.base_url() == (
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/dd4401a9add81ac692d20e240d22ec9dda82cc29"
    )
    assert "af_heart" in k.voices
    q = catalog.tts_model("qwen3", "qwen3-tts")
    assert q is not None and q.status is None and q.local and q.requires == "apple-silicon"
    assert q.voices[:2] == ["Ryan", "Aiden"] and {"en", "es"} <= set(q.languages)
    # Every size x family x precision Hugging Face publishes (no 0.6B VoiceDesign), pinned per file.
    variants = catalog.weights_variants("qwen3")
    assert len(variants) == 25
    assert {v.family for v in variants} == {"base", "customvoice", "voicedesign"}
    assert all(v.size == "1.7b" for v in variants if v.family == "voicedesign")
    for v in variants:
        assert v.weights is not None and len(v.weights.revision) == 40 and v.license == "Apache-2.0"
        assert all(len(f.sha256) == 64 for f in v.weights.files), v.model
        assert "model.safetensors" in v.required_files() and "speech_tokenizer/model.safetensors" in v.required_files()
    cv = catalog.weights_variant("qwen3-tts-0.6b-customvoice-8bit")
    assert cv is not None and {s.lower() for s in q.voices} == set(cv.speakers)
    assert catalog.stt_for("openai") is not None
    assert catalog.stt_for("openai").batch[0] == "gpt-4o-transcribe"  # type: ignore[union-attr]
    llm = catalog.llm_for("anthropic")
    assert llm is not None and llm.default == DEFAULT_LLM == "claude-haiku-5-5"
    assert llm.options[0] == "claude-haiku-5-5"


def test_llm_traits_per_model(catalog: ModelCatalog) -> None:
    """How each model turns thinking off and whether it has a server-side refusal fallback (05 § Tier 2)."""
    haiku = catalog.llm_traits("claude-haiku-5-5")
    assert haiku.thinking_off_param() == {"type": "disabled"} and not haiku.server_fallback
    assert haiku.annotate_thinking == "adaptive"  # the accuracy gate's choice (tests/live/test_annotate_eval.py)
    sonnet = catalog.llm_traits("claude-sonnet-5-5")
    assert sonnet.thinking_off_param() == {"type": "between_tools"} and sonnet.server_fallback
    opus = catalog.llm_traits("claude-opus-5-5")
    assert opus.thinking_off_param() is None and opus.server_fallback  # `disabled` is a 400 on Opus 5.5
    # Every model the catalog offers has traits; an unknown one gets the safe defaults.
    llm = catalog.llm_for("anthropic")
    assert llm is not None and set(llm.options) <= set(llm.models)
    unknown = catalog.llm_traits("claude-someday-9")
    assert unknown.thinking_off_param() is None and not unknown.server_fallback


def test_fixture_entries_are_test_only(catalog: ModelCatalog) -> None:
    assert all(m.provider != "fixture" for m in catalog.tts_models())
    fx = catalog.tts_models("fixture", include_test=True)
    assert {m.model for m in fx} == {"tone-v1", "tone-paid-v1"}
    assert all(m.test_only for m in fx)
    assert catalog.local_model("tone-v1") is None  # never offered by the model manager


def test_user_override_deep_merges(paths: Paths) -> None:
    paths.models_toml.write_text(
        '[tts.openai."gpt-4o-mini-tts"]\nvoices = ["coral"]\n\n'
        '[tts.openai_compatible."kokoro-fastapi"]\nbase_url = "http://127.0.0.1:8880/v1"\nvoices = ["af_heart"]\n'
    )
    cat = ModelCatalog.load(paths)
    oa = cat.tts_model("openai", "gpt-4o-mini-tts")
    assert oa is not None and oa.voices == ["coral"]
    assert oa.snapshot == "gpt-4o-mini-tts-2025-12-15"  # untouched keys survive the merge
    compat = cat.tts_model("openai_compatible", "kokoro-fastapi")
    assert compat is not None and compat.base_url == "http://127.0.0.1:8880/v1"


# ------------------------------------------------------------------ keys (build configuration)


def test_keys_come_from_the_build_environment_only() -> None:
    keys = ProviderKeys(make_env(openai_api_key="sk-from-env", gemini_api_key="   "))
    assert keys.get("openai") == "sk-from-env" and keys.configured("openai")
    assert keys.get("gemini") is None and keys.status("gemini") == "missing"  # blank is not a key
    assert keys.status("anthropic") == "missing"
    # Nothing in the app can add, change or remove a key.
    assert not any(hasattr(keys, name) for name in ("set", "delete", "detect"))


def test_packaged_builds_read_the_bundled_provider_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`bundle-engine.sh` writes provider.env into the runtime; the process environment still wins."""
    from open_speech import config

    (tmp_path / config.PROVIDER_ENV_FILE).write_text("ANTHROPIC_API_KEY=sk-ant-built-in\nOPENAI_API_KEY=sk-built-in\n")
    monkeypatch.setenv(config.BUNDLED_RUNTIME_ENV, str(tmp_path))
    files = config.env_files()
    assert files[-1] == tmp_path / config.PROVIDER_ENV_FILE
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-process")
    env = config.EngineEnv(_env_file=files[-1:])  # type: ignore[call-arg]
    keys = ProviderKeys(env)
    assert keys.get("anthropic") == "sk-ant-built-in"
    assert keys.get("openai") == "sk-from-process"


def test_mark_invalid_and_repr() -> None:
    keys = ProviderKeys(make_env(gemini_api_key="AIzaSECRET"))
    changed: list[str] = []
    keys.add_listener(changed.append)
    assert keys.status("gemini") == "configured"
    keys.mark_invalid("gemini")
    keys.mark_invalid("gemini")  # reported once
    assert keys.status("gemini") == "invalid" and not keys.configured("gemini")
    assert changed == ["gemini"]
    assert "AIzaSECRET" not in repr(keys)
