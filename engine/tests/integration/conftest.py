"""Integration harness: a real Engine with fixture providers behind the real FastAPI app.

Every test drives the engine only through its public REST + WebSocket API, the same way the shell does.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

TOKEN = "e" * 64
HOST = {"Host": "127.0.0.1:8765"}
AUTH = {"Authorization": f"Bearer {TOKEN}", **HOST}


class WsReader:
    """Reads WS messages on a background thread so tests can wait with timeouts and never hang."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.q: queue.Queue[dict[str, Any]] = queue.Queue()
        self.seen: list[dict[str, Any]] = []
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        try:
            while True:
                self.q.put(self.ws.receive_json())
        except Exception:  # noqa: BLE001 — socket closed
            return

    def until(self, pred: Callable[[dict[str, Any]], bool], timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                types = [m.get("type") for m in self.seen[-25:]]
                raise AssertionError(f"timed out waiting for message; last types: {types}")
            try:
                msg = self.q.get(timeout=remaining)
            except queue.Empty:
                continue
            self.seen.append(msg)
            if pred(msg):
                return msg

    def of_type(self, t: str, timeout: float = 15.0, **match: Any) -> dict[str, Any]:
        return self.until(lambda m: m.get("type") == t and all(m.get(k) == v for k, v in match.items()), timeout)

    def drain(self) -> list[dict[str, Any]]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                self.seen.extend(out)
                return out


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("OPEN_SPEECH_HOME", str(h))
    monkeypatch.setenv("OPEN_SPEECH_FIXTURE_PROVIDERS", "1")
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(k, "")
    return h


@pytest.fixture
def engine_factory(home: Path) -> Iterator[Callable[..., tuple[Any, TestClient]]]:
    from open_speech.api.app import create_app
    from open_speech.config import EngineEnv
    from open_speech.providers.fixture import reset_fixtures
    from open_speech.service.engine import Engine

    clients: list[TestClient] = []

    def make(**kw: Any) -> tuple[Any, TestClient]:
        reset_fixtures()
        env = EngineEnv(_env_file=None)  # type: ignore[call-arg]
        engine = Engine(env, **kw)
        app = create_app(engine, token=TOKEN)
        client = TestClient(app, base_url="http://127.0.0.1:8765", headers=AUTH)
        client.__enter__()
        clients.append(client)
        return engine, client

    yield make
    for c in clients:
        c.__exit__(None, None, None)
    os.environ.pop("OPEN_SPEECH_FIXTURE_PROVIDERS", None)


@pytest.fixture
def api(engine_factory: Callable[..., tuple[Any, TestClient]]) -> tuple[Any, TestClient]:
    return engine_factory()


def ws_connect(client: TestClient) -> Any:
    return client.websocket_connect(f"/api/v1/ws?token={TOKEN}", headers=HOST)


def wait_state(client: TestClient, states: set[str], timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.get("/api/v1/playback").json()
        if last["state"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(f"playback never reached {states}; last={last}")


def turn(
    client: TestClient, reader: WsReader, text: str, *, source: str = "typed", timeout: float = 15.0
) -> dict[str, Any]:
    """Submit a command and wait for its `assistant.done`. Returns {done, cards, actions, deltas}."""
    turn_id = client.post("/api/v1/assistant/turns", json={"text": text, "source": source}).json()["turn_id"]
    done = reader.of_type("assistant.done", timeout=timeout, turn_id=turn_id)
    related = [m for m in reader.seen if m.get("turn_id") == turn_id]
    return {
        "done": done,
        "cards": [m for m in related if m["type"] == "assistant.card"],
        "actions": [m for m in related if m["type"] == "assistant.action"],
        "deltas": [m for m in related if m["type"] == "assistant.delta"],
    }
