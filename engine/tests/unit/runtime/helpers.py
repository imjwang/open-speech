"""Test helpers for the runtime suite (imported by conftest and tests)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2

from open_speech.config import EngineEnv, UserSettings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "providers"


def make_env(**kw: Any) -> EngineEnv:
    """EngineEnv that ignores the repo `.env` and the process environment for keys."""
    base: dict[str, Any] = {"anthropic_api_key": None, "openai_api_key": None, "gemini_api_key": None}
    base.update(kw)
    return EngineEnv(_env_file=None, **base)  # type: ignore[call-arg]



class SettingsBox:
    def __init__(self) -> None:
        self.value = UserSettings()

    def __call__(self) -> UserSettings:
        return self.value

    def set(self, key: str, value: Any) -> None:
        self.value = self.value.with_value(key, value)



class Recorder:
    """httpx2.MockTransport handler with a route table: (method, path-suffix) → response factory."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.routes: list[tuple[str, str, Callable[[httpx2.Request], httpx2.Response]]] = []

    def add(self, method: str, suffix: str, fn: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.routes.append((method.upper(), suffix, fn))

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        for method, suffix, fn in self.routes:
            if request.method == method and request.url.path.endswith(suffix):
                return fn(request)
        return httpx2.Response(404, json={"error": {"message": f"no mock for {request.method} {request.url}"}})

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def json_body(self, i: int = -1) -> Any:
        return json.loads(self.requests[i].content.decode())



def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()
