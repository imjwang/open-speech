from __future__ import annotations

from typing import Any

import pytest

from open_speech.config import Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.keys import KEYCHAIN_SERVICE, KeyStore

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
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/main"
    )
    assert "af_heart" in k.voices
    q = catalog.tts_model("qwen3", "qwen3-tts-12hz-0.6b")
    assert q is not None and q.status == "later" and q.requires == "apple-silicon"
    assert catalog.stt_for("openai") is not None
    assert catalog.stt_for("openai").batch[0] == "gpt-4o-transcribe"  # type: ignore[union-attr]
    llm = catalog.llm_for("anthropic")
    assert llm is not None and llm.default == "claude-sonnet-5-5"


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


# ------------------------------------------------------------------ keys


class FakeKeyring:
    def __init__(self, data: dict[str, str] | None = None, broken: bool = False) -> None:
        self.data = dict(data or {})
        self.broken = broken

    def get_password(self, service: str, user: str) -> str | None:
        if self.broken:
            raise RuntimeError("no backend")
        assert service == KEYCHAIN_SERVICE
        return self.data.get(user)

    def set_password(self, service: str, user: str, value: str) -> None:
        if self.broken:
            raise RuntimeError("no backend")
        self.data[user] = value

    def delete_password(self, service: str, user: str) -> None:
        self.data.pop(user, None)


def test_detect_prefixes() -> None:
    assert KeyStore.detect("sk-ant-api03-xyz") == "anthropic"
    assert KeyStore.detect("sk-proj-abc") == "openai"
    assert KeyStore.detect("  AIzaSyD  ") == "gemini"
    assert KeyStore.detect("hello") is None


def test_env_overrides_keychain() -> None:
    kr = FakeKeyring({"openai": "sk-from-keychain", "gemini": "AIza-keychain"})
    ks = KeyStore(make_env(openai_api_key="sk-from-env"), use_keychain=True, keyring_backend=kr)
    assert ks.get("openai") == "sk-from-env"
    assert ks.source("openai") == "env"
    assert ks.get("gemini") == "AIza-keychain"
    assert ks.source("gemini") == "keychain"
    assert ks.status("anthropic") == "missing"
    # keychain ignored when disabled
    ks2 = KeyStore(make_env(), use_keychain=False, keyring_backend=kr)
    assert ks2.get("gemini") is None


def test_broken_keychain_is_missing_not_crash() -> None:
    ks = KeyStore(make_env(), use_keychain=True, keyring_backend=FakeKeyring(broken=True))
    assert ks.status("openai") == "missing"


async def test_set_runs_test_call_and_stores_only_on_success() -> None:
    calls: list[tuple[str, str]] = []

    async def tester(provider: str, key: str) -> tuple[bool, str]:
        calls.append((provider, key))
        return (key.endswith("good"), "ok" if key.endswith("good") else "That key was rejected.")

    kr = FakeKeyring()
    ks = KeyStore(make_env(), use_keychain=True, tester=tester, keyring_backend=kr)
    changed: list[str] = []
    ks.add_listener(changed.append)

    ok, detail = await ks.set("openai", "sk-bad")
    assert not ok and "rejected" in detail
    assert ks.status("openai") == "missing" and "openai" not in kr.data

    ok, detail = await ks.set("openai", "sk-good")
    assert ok and "keychain" in detail
    assert ks.get("openai") == "sk-good" and kr.data["openai"] == "sk-good"
    assert changed == ["openai"]
    assert calls == [("openai", "sk-bad"), ("openai", "sk-good")]


async def test_set_rejects_prefix_mismatch_without_test_call() -> None:
    async def tester(provider: str, key: str) -> tuple[bool, str]:  # pragma: no cover - must not run
        raise AssertionError("no test call for a mismatched key")

    ks = KeyStore(make_env(), use_keychain=False, tester=tester)
    ok, detail = await ks.set("openai", "sk-ant-abc")
    assert not ok and "Anthropic" in detail


async def test_tester_exception_is_mapped_and_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    class Boom(Exception):
        status_code = 401

    async def tester(provider: str, key: str) -> tuple[bool, str]:
        raise Boom(f"bad key {key}")

    ks = KeyStore(make_env(), use_keychain=False, tester=tester)
    ok, detail = await ks.set("anthropic", "sk-ant-SECRET123")
    assert not ok
    assert "SECRET123" not in detail
    assert "SECRET123" not in caplog.text
    assert "SECRET123" not in repr(ks)


def test_mark_invalid_and_repr() -> None:
    ks = KeyStore(make_env(gemini_api_key="AIzaSECRET"), use_keychain=False)
    assert ks.status("gemini") == "configured"
    ks.mark_invalid("gemini")
    assert ks.status("gemini") == "invalid"
    assert "AIzaSECRET" not in repr(ks)
    assert ks.all_status() == {"anthropic": "missing", "openai": "missing", "gemini": "invalid"}


async def test_default_tester_maps_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default tester uses the SDK; with the network guard it must come back as 'couldn't reach'."""
    from open_speech.providers import keys as keys_mod

    ok, detail = await keys_mod.default_key_tester("openai", "sk-test")
    assert not ok
    assert "OpenAI" in detail


def _unused(_: Any) -> None:  # keep imports honest for type checkers
    return None
