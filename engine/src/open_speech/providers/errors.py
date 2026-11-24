"""Vendor exception → `EngineError` mapping, shared by every adapter.

The SDKs (openai, anthropic, google-genai) all expose `status_code` on HTTP errors and name their
transport failures `APIConnectionError` / `APITimeoutError`, so the mapping is duck-typed and never imports
an SDK. Errors carry `retryable` (429 / 5xx / offline) so the synthesizer's backoff knows what to retry.
"""

from __future__ import annotations

import asyncio
from typing import Any

from open_speech.errors import EngineError, ErrorCode

PROVIDER_NAMES = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "google": "Google",
    "kokoro": "Kokoro",
    "qwen3": "Qwen",
    "fixture": "Fixture",
    "openai_compatible": "your speech server",
}


def provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider)


class ProviderError(EngineError):
    """A provider call failed. `retryable` marks transient failures (429, 5xx, network)."""

    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        *,
        provider: str,
        status: int | None = None,
        retryable: bool = False,
        key_invalid: bool = False,
    ) -> None:
        super().__init__(code, detail, provider=provider, upstream_status=status)
        self.provider = provider
        self.upstream_status = status
        self.retryable = retryable
        self.key_invalid = key_invalid


def _status_of(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int) and 100 <= v <= 599:
            return v
    resp: Any = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _is_network(exc: BaseException) -> bool:
    name = type(exc).__name__
    if "Connection" in name or "Timeout" in name or "Connect" in name:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError))


def map_provider_error(provider: str, exc: BaseException, *, feature: str = "speech") -> EngineError:
    """Translate any exception raised inside an adapter into an `EngineError` with a spoken-style detail
    (FR-ASST-7: what happened, then the one next step)."""
    if isinstance(exc, EngineError):
        return exc
    name = provider_name(provider)
    status = _status_of(exc)
    if status is None and _is_network(exc):
        return ProviderError(
            "offline",
            f"I couldn't reach {name}. I'll keep reading with the local voice and try again later.",
            provider=provider,
            retryable=True,
        )
    if status in (401, 403):
        return ProviderError(
            "provider_error",
            f"{name} turned the request down, so I'll keep reading with the local voice.",
            provider=provider,
            status=status,
            key_invalid=True,
        )
    if status == 429:
        return ProviderError(
            "rate_limited",
            f"{name} is rate limiting requests right now. I'll retry shortly.",
            provider=provider,
            status=status,
            retryable=True,
        )
    if status is not None and (status >= 500 or status in (408, 409)):
        return ProviderError(
            "provider_error",
            f"{name} had a server error ({status}). I'll retry, then fall back to the local voice.",
            provider=provider,
            status=status,
            retryable=True,
        )
    if status is not None:
        return ProviderError(
            "provider_error",
            f"{name} rejected the {feature} request ({status}).",
            provider=provider,
            status=status,
        )
    return ProviderError(
        "provider_error",
        f"{name} {feature} failed: {type(exc).__name__}.",
        provider=provider,
    )


def is_retryable(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False))
