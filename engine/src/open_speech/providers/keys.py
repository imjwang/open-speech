"""Provider keys (08 § Keys).

Keys are **build configuration, not user data**. They come from the process environment, the repo-root `.env`
in development, or the `provider.env` that `app/scripts/bundle-engine.sh` writes into a packaged build
(`EngineEnv` reads all three). Nothing in the app asks for, stores, shows or changes a key: a build either
includes a provider or it doesn't, and features that depend on a missing provider are simply absent.

Keys are held as `SecretStr` and never logged, printed or returned by any API (status only).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Literal

from pydantic import SecretStr

from open_speech.config import EngineEnv

log = logging.getLogger(__name__)

PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "gemini")
KeyStatus = Literal["configured", "missing", "invalid"]


class ProviderKeys:
    def __init__(self, env: EngineEnv) -> None:
        self._env = env
        self._invalid: set[str] = set()
        self._listeners: list[Callable[[str], None]] = []

    def __repr__(self) -> str:  # never show keys
        return f"ProviderKeys({', '.join(f'{p}={self.status(p)}' for p in PROVIDERS)})"

    def get(self, provider: str) -> str | None:
        secret: SecretStr | None = getattr(self._env, f"{provider}_api_key", None)
        value = secret.get_secret_value().strip() if secret is not None else ""
        return value or None

    def status(self, provider: str) -> KeyStatus:
        if self.get(provider) is None:
            return "missing"
        return "invalid" if provider in self._invalid else "configured"

    def configured(self, provider: str) -> bool:
        return self.status(provider) == "configured"

    def mark_invalid(self, provider: str) -> None:
        """A provider rejected the build's key (401/403): stop offering it until the engine restarts."""
        if provider in PROVIDERS and provider not in self._invalid:
            self._invalid.add(provider)
            log.warning("the %s key in this build was rejected; its features are off for this run", provider)
            for fn in list(self._listeners):
                try:
                    fn(provider)
                except Exception:
                    log.exception("key listener failed")

    def add_listener(self, fn: Callable[[str], None]) -> None:
        """Called with the provider id when a key is marked invalid (the registry drops its adapters)."""
        self._listeners.append(fn)
