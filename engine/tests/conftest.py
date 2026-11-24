"""Hermetic tests: no test may reach a real provider unless OPEN_SPEECH_LIVE=1 (10 § Testing: live use is opt-in).

Vendor SDKs honour base-URL environment variables, which would route requests around respx mocks, so they're
cleared for every test, along with any keys from the developer's shell or `.env`.
"""

from __future__ import annotations

import os
import socket

import pytest

_STRIP = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_TTS_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # No test plays sound: an Engine built in a test would otherwise open the Mac's output device.
    monkeypatch.setenv("OPEN_SPEECH_AUDIO_OUTPUT", os.environ.get("OPEN_SPEECH_AUDIO_OUTPUT") or "null")
    # Tests read English unless they choose a language: not whatever this Mac is set to, and not whatever the
    # previous test left behind.
    from open_speech import i18n

    monkeypatch.setattr(i18n, "system_language", lambda: "en")
    i18n.set_default_language("en")
    i18n._override.set(None)
    if os.environ.get("OPEN_SPEECH_LIVE") == "1":
        return
    for name in _STRIP:
        monkeypatch.delenv(name, raising=False)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(addr: object) -> None:
        if isinstance(addr, tuple) and addr and isinstance(addr[0], str) and addr[0] not in _LOOPBACK:
            raise NetworkBlocked(f"tests may not reach the network (tried {addr[0]}); mock the provider instead")

    def guarded_connect(self: socket.socket, addr: object) -> None:
        _check(addr)
        real_connect(self, addr)  # type: ignore[arg-type]

    def guarded_connect_ex(self: socket.socket, addr: object) -> int:
        _check(addr)
        return real_connect_ex(self, addr)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
