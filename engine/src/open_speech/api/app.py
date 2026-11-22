"""FastAPI app factory + WebSocket endpoint (04)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from open_speech import __version__
from open_speech.api import ws_schema as ws
from open_speech.api.auth import LoopbackGuard, ws_allowed
from open_speech.api.routes import router
from open_speech.config import SHELL_ORIGIN
from open_speech.errors import EngineError

if TYPE_CHECKING:
    from open_speech.service.engine import Engine

log = logging.getLogger("open_speech.api")


def _problem_response(err: EngineError) -> JSONResponse:
    return JSONResponse(err.problem(), status_code=err.status, media_type="application/problem+json")


def create_app(engine: Engine | None = None, *, token: str = "", allowed_origins: set[str] | None = None) -> FastAPI:
    origins = {SHELL_ORIGIN, *(allowed_origins or set())}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if engine is not None:
            await engine.start()
        try:
            yield
        finally:
            if engine is not None:
                await engine.shutdown()

    app = FastAPI(
        title="Open Speech Engine",
        version=__version__,
        summary="Local REST + WebSocket API. Every request needs `Authorization: Bearer <token>`.",
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.add_middleware(LoopbackGuard, token=token, allowed_origins=origins, open_paths=set())
    app.include_router(router)

    @app.exception_handler(EngineError)
    async def _engine_error(_: Request, exc: EngineError) -> JSONResponse:
        return _problem_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        return _problem_response(EngineError("invalid_request", detail or "Invalid request."))

    @app.websocket("/api/v1/ws")
    async def websocket(sock: WebSocket) -> None:
        headers = {k.decode().lower(): v.decode() for k, v in sock.scope.get("headers", [])}
        if not ws_allowed(token, origins, headers, sock.query_params.get("token")):
            await sock.close(code=4401)
            return
        await sock.accept()
        assert engine is not None
        queue = engine.events.subscribe()
        topics: set[str] = set()
        await sock.send_json(ws.Hello(version=__version__).model_dump(mode="json", exclude_none=True))
        await sock.send_json(
            ws.PlaybackStateMsg(playback=engine.playback.state()).model_dump(mode="json", exclude_none=True)
        )

        async def pump() -> None:
            while True:
                msg = await queue.get()
                if topics and msg.get("type", "").split(".")[0] not in topics and msg.get("type") not in topics:
                    continue
                await sock.send_json(msg)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                frame: dict[str, Any] = await sock.receive()
                if frame.get("type") == "websocket.disconnect":
                    break
                if frame.get("bytes") is not None:
                    engine.voice_input.feed(frame["bytes"])
                    continue
                text = frame.get("text")
                if text is None:
                    continue
                try:
                    msg = ws.client_message_adapter.validate_python(json.loads(text))
                except (ValidationError, json.JSONDecodeError) as e:
                    err = EngineError("invalid_request", f"Bad message: {e}")
                    await sock.send_json(ws.ErrorMsg(problem=err.problem()).model_dump(mode="json"))
                    continue
                try:
                    await _handle(engine, msg, topics)
                except EngineError as e:
                    await sock.send_json(ws.ErrorMsg(problem=e.problem()).model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task
            engine.events.unsubscribe(queue)

    return app


async def _handle(engine: Engine, msg: Any, topics: set[str]) -> None:
    from open_speech.api.schemas import TurnRequest

    match msg:
        case ws.MicStart():
            await engine.voice_input.start(msg.session_id, msg.mode, msg.context)
        case ws.MicStop():
            await engine.voice_input.stop(msg.session_id, submit=msg.submit)
        case ws.Command():
            await engine.assistant.submit(
                TurnRequest(text=msg.text, source=msg.source, session_id=msg.session_id, context=msg.context)
            )
        case ws.Subscribe():
            topics.clear()
            topics.update(msg.topics)
        case ws.BarState():
            await engine.playback.bar_state(msg.open)
