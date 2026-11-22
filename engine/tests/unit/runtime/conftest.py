"""Shared fixtures for the runtime workstream's tests.

Network guard: every runtime test runs with `socket.connect` patched to refuse non-loopback addresses, so
an adapter test can never reach a real provider even if a mock is missing (the vendor SDKs use httpx2,
which respx does not intercept; adapter tests use `httpx2.MockTransport` instead).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from open_speech.config import Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.fixture import reset_fixtures
from open_speech.service.events import EventBus
from open_speech.store.db import Database
from open_speech.usage.pricing import Pricing

from .helpers import Recorder, SettingsBox


def _loopback(addr: Any) -> bool:
    if isinstance(addr, (str, bytes)):  # AF_UNIX paths
        return True
    if not (isinstance(addr, tuple) and addr):
        return False
    host = addr[0]
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(self: socket.socket, addr: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(addr):
            raise ConnectionRefusedError(f"runtime tests may not reach the network ({addr!r})")
        return real_connect(self, addr)

    def guard_ex(self: socket.socket, addr: Any) -> Any:
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(addr):
            raise ConnectionRefusedError(f"runtime tests may not reach the network ({addr!r})")
        return real_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_ex)


@pytest.fixture(autouse=True)
def _reset_fixture_providers() -> Iterator[None]:
    reset_fixtures()
    yield
    reset_fixtures()


# ------------------------------------------------------------------ environment


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "home").ensure()


@pytest.fixture
def db(paths: Paths) -> Iterator[Database]:
    d = Database(paths.db)
    yield d
    d.close()


@pytest.fixture
def catalog(paths: Paths) -> ModelCatalog:
    return ModelCatalog.load(paths)


@pytest.fixture
def pricing(paths: Paths) -> Pricing:
    return Pricing.load(paths)


@pytest.fixture
def events() -> EventBus:
    return EventBus()


@pytest.fixture
def settings() -> SettingsBox:
    return SettingsBox()


@pytest.fixture
def captured(events: EventBus) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    events.add_listener(out.append)
    return out


# ------------------------------------------------------------------ HTTP mocking (httpx2)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


