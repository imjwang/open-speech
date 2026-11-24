"""One shared Anthropic client (07 § Agent core): the planning adapter and every agent turn use the same
`AsyncAnthropic`, whose keep-alive pool lets a turn reuse the previous turn's connection, and opening the bar
after a quiet minute re-opens that connection in the background with a free Models API call."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from open_speech.config import EngineEnv, Paths
from open_speech.local.catalog import ModelCatalog
from open_speech.providers.keys import ProviderKeys
from open_speech.providers.registry import ProviderFactories, ProviderRegistry, _anthropic_client

from .helpers import Recorder, SettingsBox, make_env

MODEL_INFO = {"id": "claude-haiku-5-5", "type": "model", "display_name": "Claude Haiku 5.5",
              "created_at": "2026-10-01T00:00:00Z"}


def _mock_factory(recorder: Recorder, made: list[Any]) -> Callable[[str, Callable[[], None]], Any]:
    """Like the real factory (one client, an idle clock fed by a request hook) over a mock transport."""

    def factory(key: str, on_request: Callable[[], None]) -> Any:
        from anthropic import AsyncAnthropic

        async def touch(_request: Any) -> None:
            on_request()

        http = httpx2.AsyncClient(transport=httpx2.MockTransport(recorder), event_hooks={"request": [touch]})
        client = AsyncAnthropic(api_key=key, base_url="https://api.anthropic.com", http_client=http, max_retries=0)
        made.append(client)
        return client

    return factory


def _registry(paths: Paths, settings: SettingsBox, factories: ProviderFactories, **env: Any) -> ProviderRegistry:
    e = make_env(**env)
    return ProviderRegistry(e, paths, ModelCatalog.load(paths), ProviderKeys(e), settings=settings, factories=factories)


def test_planning_adapter_uses_the_shared_client(paths: Paths, settings: SettingsBox, recorder: Recorder) -> None:
    made: list[Any] = []
    registry = _registry(paths, settings, ProviderFactories(anthropic_client=_mock_factory(recorder, made)),
                         anthropic_api_key="sk-ant-test")
    llm = registry.llm()
    assert llm is not None and llm._client is registry.anthropic_client()  # type: ignore[attr-defined]
    registry.refresh()  # a local model was installed, a TTS key was rejected...: the pool stays
    assert registry.llm()._client is made[0] and len(made) == 1  # type: ignore[union-attr]


def test_no_key_no_client(paths: Paths, settings: SettingsBox, recorder: Recorder) -> None:
    made: list[Any] = []
    registry = _registry(paths, settings, ProviderFactories(anthropic_client=_mock_factory(recorder, made)))
    assert registry.anthropic_client() is None and made == []


async def test_warm_up_is_a_models_call_and_feeds_the_idle_clock(
    paths: Paths, settings: SettingsBox, recorder: Recorder
) -> None:
    recorder.add("GET", "/v1/models/claude-haiku-5-5", lambda r: httpx2.Response(200, json=MODEL_INFO))
    registry = _registry(paths, settings, ProviderFactories(anthropic_client=_mock_factory(recorder, [])),
                         anthropic_api_key="sk-ant-test")
    assert registry.anthropic_idle_s() == float("inf")
    assert await registry.warm_anthropic("claude-haiku-5-5")
    assert [r.method for r in recorder.requests] == ["GET"]  # the Models API: free, nothing billed
    assert registry.anthropic_idle_s() < 5
    recorder.routes.clear()  # now every request fails: a failed warm-up never raises
    assert not await registry.warm_anthropic("claude-haiku-5-5")


class _CountingServer:
    """A loopback HTTP/1.1 server that answers `GET /v1/models/<id>` with keep-alive and counts connections."""

    def __init__(self) -> None:
        self.connections = 0
        self.requests = 0
        self.server: asyncio.Server | None = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.decode().split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        length = int(line.split(":", 1)[1])
                if length:
                    await reader.readexactly(length)
                self.requests += 1
                body = json.dumps(MODEL_INFO).encode()
                writer.write(
                    b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: keep-alive\r\n"
                    + f"content-length: {len(body)}\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def __aenter__(self) -> str:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aexit__(self, *exc: Any) -> None:
        assert self.server is not None
        self.server.close()


@pytest.fixture
async def counting_server(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[_CountingServer, str]]:
    srv = _CountingServer()
    async with srv as url:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)  # the real factory, pointed at the loopback server
        yield srv, url


async def test_the_real_client_keeps_its_connection_between_turns(
    counting_server: tuple[_CountingServer, str],
) -> None:
    srv, _ = counting_server
    touched: list[float] = []
    client = _anthropic_client("sk-ant-test", lambda: touched.append(time.monotonic()))
    try:
        for _ in range(3):  # three turns, a moment apart
            await client.models.retrieve("claude-haiku-5-5")
            await asyncio.sleep(0.05)
    finally:
        await client.close()
    assert srv.requests == 3 and len(touched) == 3
    assert srv.connections == 1  # one TCP (and, against the API, TLS) handshake for all of them


# ------------------------------------------------------------------ the engine: shared runner client, warm on open


@pytest.fixture
def engine(tmp_path: Path, recorder: Recorder) -> Any:
    from open_speech.service.engine import Engine

    env = EngineEnv(  # type: ignore[call-arg]
        _env_file=None,
        open_speech_home=tmp_path / "home",
        open_speech_fixture_providers=True,
        open_speech_audio_output="null",
        anthropic_api_key="sk-ant-test",
        openai_api_key=None,
        gemini_api_key=None,
    )
    e = Engine(env)
    e.registry._f.anthropic_client = _mock_factory(recorder, [])
    yield e
    e.db.close()


def test_agent_turns_share_the_planning_client(engine: Any) -> None:
    first, second = engine.agent_runner(), engine.agent_runner()
    shared = engine.registry.anthropic_client()
    assert first.client is shared and second.client is shared
    assert engine.registry.llm()._client is shared
    assert first.catalog is engine.catalog  # the engine's catalog (with the user's overrides), not a fresh one


async def test_opening_the_bar_after_a_quiet_minute_warms_the_connection(engine: Any) -> None:
    warmed: list[str] = []

    async def warm(model: str) -> bool:
        warmed.append(model)
        return True

    engine.registry.warm_anthropic = warm
    await engine.playback.bar_state(True)  # nothing sent yet: warm
    assert engine._warming is not None
    await engine._warming
    assert warmed == [engine.settings.get().llm.model] == ["claude-haiku-5-5"]

    engine.registry._anthropic_used = time.monotonic() - 10  # a turn 10 s ago: the pool is still warm
    await engine.playback.bar_state(True)
    await engine._warming
    assert len(warmed) == 1

    engine.registry._anthropic_used = time.monotonic() - 61
    await engine.playback.bar_state(False)  # closing never warms
    assert len(warmed) == 1
    await engine.playback.bar_state(True)
    await engine._warming
    assert len(warmed) == 2
