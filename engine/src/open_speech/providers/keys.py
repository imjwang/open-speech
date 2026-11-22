"""Provider keys (08 § Keys).

Resolution order for `get()`: a key saved through `set()` in this process → environment / repo-root `.env`
(`EngineEnv`) → OS keychain (`keyring`, service "open-speech", only when `use_keychain=True`). The env
overrides the keychain, as the spec requires; a key the user just pasted wins for the running process.

Keys are held as `SecretStr` and never logged, printed or returned by any API (status only).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import SecretStr

from open_speech.config import EngineEnv
from open_speech.providers.errors import map_provider_error, provider_name

log = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini")
KEYCHAIN_SERVICE = "open-speech"
KeyStatus = Literal["configured", "missing", "invalid"]
KeyTester = Callable[[str, str], Awaitable[tuple[bool, str]]]


class KeyStore:
    def __init__(
        self,
        env: EngineEnv,
        *,
        use_keychain: bool,
        tester: KeyTester | None = None,
        keyring_backend: Any | None = None,
    ) -> None:
        self._env = env
        self._use_keychain = use_keychain
        self._tester: KeyTester = tester or default_key_tester
        self._keyring = keyring_backend
        self._session: dict[str, SecretStr] = {}
        self._keychain_cache: dict[str, SecretStr | None] = {}
        self._invalid: set[str] = set()
        self._listeners: list[Callable[[str], None]] = []

    def __repr__(self) -> str:  # never show keys
        return f"KeyStore({', '.join(f'{p}={self.status(p)}' for p in PROVIDERS)})"

    # ------------------------------------------------------------------ reading

    def secret(self, provider: str) -> SecretStr | None:
        if provider in self._session:
            return self._session[provider]
        env_val: SecretStr | None = getattr(self._env, f"{provider}_api_key", None)
        if env_val is not None and env_val.get_secret_value().strip():
            return env_val
        if self._use_keychain and provider in PROVIDERS:
            return self._keychain_get(provider)
        return None

    def get(self, provider: str) -> str | None:
        s = self.secret(provider)
        return s.get_secret_value().strip() if s is not None else None

    def source(self, provider: str) -> Literal["session", "env", "keychain"] | None:
        if provider in self._session:
            return "session"
        env_val: SecretStr | None = getattr(self._env, f"{provider}_api_key", None)
        if env_val is not None and env_val.get_secret_value().strip():
            return "env"
        if self._use_keychain and self._keychain_get(provider) is not None:
            return "keychain"
        return None

    def status(self, provider: str) -> KeyStatus:
        if self.get(provider) is None:
            return "missing"
        if provider in self._invalid:
            return "invalid"
        return "configured"

    def all_status(self) -> dict[str, KeyStatus]:
        return {p: self.status(p) for p in PROVIDERS}

    def configured(self, provider: str) -> bool:
        return self.status(provider) == "configured"

    # ------------------------------------------------------------------ detection

    @staticmethod
    def detect(key: str) -> str | None:
        k = key.strip()
        if k.startswith("sk-ant-"):
            return "anthropic"
        if k.startswith("sk-"):
            return "openai"
        if k.startswith("AIza"):
            return "gemini"
        return None

    # ------------------------------------------------------------------ writing

    async def set(self, provider: str, key: str) -> tuple[bool, str]:
        """Run a cheap test call with the key; store it only if the call succeeds."""
        provider = provider.strip().lower()
        name = provider_name(provider)
        if provider not in PROVIDERS:
            return False, f"I don't know a provider called {provider}."
        key = key.strip()
        if not key:
            return False, f"That {name} key is empty."
        detected = self.detect(key)
        if detected is not None and detected != provider:
            return False, (
                f"That looks like a {provider_name(detected)} key, not a {name} key. "
                f"Paste it again and I'll save it for {provider_name(detected)}."
            )
        try:
            ok, detail = await self._tester(provider, key)
        except Exception as exc:  # the tester must never leak the key into logs
            ok, detail = False, map_provider_error(provider, exc, feature="key check").detail
        if not ok:
            return False, detail
        self._session[provider] = SecretStr(key)
        self._invalid.discard(provider)
        stored = "for this session"
        if self._use_keychain:
            if self._keychain_set(provider, key):
                stored = "in your keychain"
            else:
                stored = "for this session (the keychain wasn't available)"
        self._notify(provider)
        return True, f"Your {name} key works. I saved it {stored}."

    def delete(self, provider: str) -> None:
        self._session.pop(provider, None)
        self._invalid.discard(provider)
        if self._use_keychain:
            kr = self._kr()
            if kr is not None:
                try:
                    kr.delete_password(KEYCHAIN_SERVICE, provider)
                except Exception:
                    log.debug("keychain delete failed for %s", provider)
            self._keychain_cache[provider] = None
        self._notify(provider)

    def mark_invalid(self, provider: str) -> None:
        """An adapter got 401/403 with this key: report `invalid` until a new key is set."""
        if provider in PROVIDERS and provider not in self._invalid:
            self._invalid.add(provider)
            self._notify(provider)

    def add_listener(self, fn: Callable[[str], None]) -> None:
        """Called with the provider id whenever a key changes or is marked invalid (registry.refresh)."""
        self._listeners.append(fn)

    def _notify(self, provider: str) -> None:
        for fn in list(self._listeners):
            try:
                fn(provider)
            except Exception:
                log.exception("key listener failed")

    # ------------------------------------------------------------------ keychain

    def _kr(self) -> Any | None:
        if self._keyring is not None:
            return self._keyring
        try:
            import keyring

            return keyring
        except Exception:
            return None

    def _keychain_get(self, provider: str) -> SecretStr | None:
        if provider in self._keychain_cache:
            return self._keychain_cache[provider]
        val: SecretStr | None = None
        kr = self._kr()
        if kr is not None:
            try:
                raw = kr.get_password(KEYCHAIN_SERVICE, provider)
                val = SecretStr(raw) if raw else None
            except Exception:
                log.debug("keychain unavailable reading %s", provider)
        self._keychain_cache[provider] = val
        return val

    def _keychain_set(self, provider: str, key: str) -> bool:
        kr = self._kr()
        if kr is None:
            return False
        try:
            kr.set_password(KEYCHAIN_SERVICE, provider, key)
        except Exception:
            log.debug("keychain unavailable writing %s", provider)
            return False
        self._keychain_cache[provider] = SecretStr(key)
        return True


# ---------------------------------------------------------------------- default test calls


async def default_key_tester(provider: str, key: str) -> tuple[bool, str]:
    """One cheap, free call per provider (list models). Vendor SDKs are imported lazily here."""
    name = provider_name(provider)
    try:
        if provider == "anthropic":
            import anthropic

            async with anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=15.0) as client:
                await client.models.list(limit=1)
        elif provider == "openai":
            import openai

            async with openai.AsyncOpenAI(api_key=key, max_retries=0, timeout=15.0) as oc:
                await oc.models.list()
        elif provider == "gemini":
            from google import genai
            from google.genai import types

            gc = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15000))
            pager = await gc.aio.models.list(config=types.ListModelsConfig(page_size=1))
            async for _ in pager:
                break
        else:
            return False, f"I don't know a provider called {provider}."
    except Exception as exc:
        err = map_provider_error(provider, exc, feature="key check")
        if getattr(err, "key_invalid", False):
            return False, f"That {name} key was rejected. Check it and paste it again."
        if err.code == "offline":
            return False, f"I couldn't reach {name} to check the key, so I didn't save it. Try again in a moment."
        return False, f"{name} couldn't confirm the key ({err.detail}) so I didn't save it."
    return True, f"Your {name} key works."
