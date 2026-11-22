"""In-process event bus. The transport, assistant, jobs and budget publish `ws_schema` messages; each WS
client subscribes. Publishing never blocks: slow subscribers drop their oldest events (except state
snapshots, which are idempotent anyway).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

Listener = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self, max_queue: int = 512) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._listeners: list[Listener] = []
        self._max = max_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._max)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subs.discard(q)

    def add_listener(self, fn: Listener) -> None:
        """Synchronous in-process listener (tests, CLI)."""
        self._listeners.append(fn)

    def publish(self, msg: BaseModel | dict[str, Any]) -> None:
        """Thread-safe. Safe to call from audio/worker threads."""
        data = msg.model_dump(mode="json", exclude_none=True) if isinstance(msg, BaseModel) else msg
        for fn in list(self._listeners):
            with contextlib.suppress(Exception):
                fn(data)
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(data)
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self._fanout, data)

    def _fanout(self, data: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(data)
