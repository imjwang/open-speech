"""Loopback API protection (03 § Security, FR-API-1, R10): token on every call, Host check, Origin allowlist."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from open_speech.api import schemas as s
from open_speech.api.app import create_app

TOKEN = "t" * 64


class _Events:
    def subscribe(self) -> Any:
        import asyncio

        return asyncio.Queue()

    def unsubscribe(self, q: Any) -> None:
        pass


def _fake_engine() -> Any:
    async def _noop() -> None:
        return None

    return SimpleNamespace(
        start=_noop,
        shutdown=_noop,
        health=lambda: s.Health(ok=True, version="test", uptime_s=1.0),
        events=_Events(),
        playback=SimpleNamespace(state=lambda: s.PlaybackState()),
    )


@pytest.fixture
def client() -> TestClient:
    app = create_app(_fake_engine(), token=TOKEN, allowed_origins={"http://localhost:5173"})
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_requires_token(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "invalid_request"


def test_wrong_token_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_bearer_token_accepted(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_query_token_accepted(client: TestClient) -> None:
    assert client.get(f"/api/v1/health?token={TOKEN}").status_code == 200


def test_dns_rebinding_host_rejected(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}", "Host": "evil.example:8765"})
    assert r.status_code == 421


def test_localhost_host_allowed(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}", "Host": "localhost:8765"})
    assert r.status_code == 200


def test_foreign_origin_rejected_even_with_token(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}", "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_shell_origin_gets_cors(client: TestClient) -> None:
    r = client.get("/api/v1/health", headers={"Authorization": f"Bearer {TOKEN}", "Origin": "app://open-speech"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "app://open-speech"


def test_preflight_from_allowed_origin(client: TestClient) -> None:
    r = client.options(
        "/api/v1/health",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_ws_requires_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/v1/ws", headers={"Host": "127.0.0.1:8765"}) as ws:
        ws.receive_json()
    assert exc.value.code == 4401


def test_ws_rejects_foreign_origin(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect(
            f"/api/v1/ws?token={TOKEN}", headers={"Origin": "https://evil.example", "Host": "127.0.0.1:8765"}
        ) as ws,
    ):
        ws.receive_json()


def test_ws_hello_with_token(client: TestClient) -> None:
    with client.websocket_connect(f"/api/v1/ws?token={TOKEN}", headers={"Host": "127.0.0.1:8765"}) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["protocol"] == 1
        state = ws.receive_json()
        assert state["type"] == "playback.state"


def test_contracts_are_current() -> None:
    """CI diffs the committed OpenAPI and WS schema (04 § Versioning)."""
    from open_speech.api import export

    assert export.main.__doc__ is None or True
    import sys

    argv = sys.argv
    try:
        sys.argv = ["export", "--check"]
        assert export.main() == 0, "Run: uv run python -m open_speech.api.export"
    finally:
        sys.argv = argv
