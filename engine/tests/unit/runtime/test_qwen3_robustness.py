"""Regressions from the code review of providers/qwen3.py: a cancelled load never orphans a worker; a broken or closed
stdout ends the backend instead of hanging requests (with the provider's lock held); the idle unload re-checks
idleness under the lock; stderr is drained whatever the worker prints; a slow cancel gets a fresh worker without
using up the crash restarts; background tasks are referenced; ping leaves nothing behind; load failures say why."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from open_speech.config import Paths
from open_speech.errors import EngineError
from open_speech.local.catalog import ModelCatalog
from open_speech.providers import qwen3_worker as qw
from open_speech.providers.base import TtsRequest
from open_speech.providers.qwen3 import InProcessBackend, Qwen3Tts, WorkerBackend, WorkerCrashed

from .helpers import SettingsBox
from .test_qwen3 import SMALL, install

REQ = TtsRequest(text="A sentence to read aloud.", model="qwen3-tts", voice="Ryan")


def provider(paths: Paths, catalog: ModelCatalog, settings: SettingsBox, factory: Any, **kw: Any) -> Qwen3Tts:
    install(paths, catalog, SMALL)
    return Qwen3Tts(catalog, paths, settings=settings, backend_factory=factory, memory_gb=lambda: 16,
                    platform_ok=lambda: True, bundled_models=paths.home / "none", **kw)


async def collect(q: Qwen3Tts, req: TtsRequest = REQ) -> np.ndarray:
    parts = [np.frombuffer(c.pcm, dtype="<f4") async for c in q.synthesize(req)]
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)


class ScriptedBackend:
    """A backend whose load and generation do what a test says."""

    def __init__(self, *, load: Any = None, idle: bool = True) -> None:
        self._load = load
        self._idle_ok = idle
        self.closed = False
        self.loads = 0
        self.last_done: dict[str, Any] = {}
        self.load_started = asyncio.Event()

    @property
    def alive(self) -> bool:
        return not self.closed

    async def load(self, variant: str, path: Path, warm: dict[str, Any] | None) -> dict[str, Any]:
        self.loads += 1
        self.load_started.set()
        if self._load is not None:
            await self._load()
        return {"op": "loaded"}

    async def wait_idle(self, timeout: float | None = None) -> bool:
        return self._idle_ok

    async def generate(self, header: dict[str, Any]) -> AsyncIterator[np.ndarray]:
        yield np.full(24000, 0.1, np.float32)
        self.last_done = {"audio_s": 1.0, "gen_s": 0.5}

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


# ------------------------------------------------------------------ 1. a cancelled load closes its worker


@pytest.mark.parametrize("via", ["prewarm", "synthesize"])
async def test_a_cancelled_load_closes_the_worker(paths: Paths, catalog: ModelCatalog, settings: SettingsBox,
                                                  via: str) -> None:
    made: list[ScriptedBackend] = []

    def factory() -> ScriptedBackend:
        made.append(ScriptedBackend(load=(lambda: asyncio.sleep(30)) if not made else None))
        return made[-1]

    q = provider(paths, catalog, settings, factory)
    task = asyncio.create_task(q.prewarm("qwen3-tts", "Ryan") if via == "prewarm" else collect(q))
    await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(_started(made))), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert made[0].closed and q._backend is None and not q._lock.locked()  # nothing left resident
    audio = await collect(q)  # the next sentence starts exactly one new worker
    assert audio.size == 24000 and len(made) == 2 and not made[1].closed
    await q.aclose()


async def _started(made: list[ScriptedBackend]) -> None:
    for _ in range(1000):
        if made:
            break
        await asyncio.sleep(0.005)
    await made[0].load_started.wait()


# ------------------------------------------------------------------ 2 / 9. a broken stdout ends the backend


class _FakeProc:
    def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader | None = None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = None
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _frame_bytes(header: Any) -> bytes:
    body = json.dumps(header).encode()
    return qw.HEADER.pack(len(body)) + body


@pytest.mark.parametrize("garbage", [
    b"\x00\x00\x00\x05nope!",                      # not JSON
    b"\x00\x00\x00\x02\xff\xfe",                    # not UTF-8
    _frame_bytes([1, 2, 3]),                        # a header that isn't an object
    qw.HEADER.pack(qw.MAX_HEADER + 1) + b"x",       # an absurd header length
    _frame_bytes({"op": "chunk", "id": 1, "bytes": 1 << 40}),  # an absurd payload
    b"",                                            # EOF while the process still runs
])
async def test_a_broken_stream_fails_waiters_and_new_requests_at_once(garbage: bytes) -> None:
    out = asyncio.StreamReader()
    out.feed_data(garbage)
    out.feed_eof()
    w = WorkerBackend()
    proc = _FakeProc(out)
    w._proc = proc  # type: ignore[assignment]
    q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    w._queues[1] = q
    w._idle = asyncio.get_running_loop().create_future()
    await asyncio.wait_for(w._read(), 2)
    assert (await q.get())[0] == "crash"  # the waiting request fails
    assert not w.alive and proc.killed and not w._queues and w._idle is None
    with pytest.raises(WorkerCrashed):  # and a new one fails before registering anything
        w._request({"op": "generate"})
    assert not w._queues and w._idle is None
    assert await w.wait_idle(0.1)


async def test_a_dead_reader_never_hangs_the_provider(paths: Paths, catalog: ModelCatalog,
                                                      settings: SettingsBox) -> None:
    """A real worker whose output stops making sense: the sentence fails fast (and the synthesizer falls back);
    the next one starts a fresh worker."""
    made: list[WorkerBackend] = []

    def factory() -> WorkerBackend:
        made.append(WorkerBackend(args=["--fake"]))
        return made[-1]

    q = provider(paths, catalog, settings, factory)
    try:
        assert (await collect(q)).size > 0
        w = made[0]
        assert w._reader is not None
        w._reader.cancel()  # the reader ends while the process runs on
        await asyncio.sleep(0.05)
        assert not w.alive
        audio = await asyncio.wait_for(collect(q), 20)
        assert audio.size > 0 and len(made) == 2 and not q._crashes  # replaced, not counted as a crash
    finally:
        await q.aclose()


# ------------------------------------------------------------------ 3. idle unload re-checks under the lock


async def test_idle_unload_rechecks_once_it_holds_the_lock(paths: Paths, catalog: ModelCatalog,
                                                           settings: SettingsBox) -> None:
    backend = InProcessBackend(qw.FakeModel)
    q = provider(paths, catalog, settings, lambda: backend, idle_unload_s=60)
    await collect(q)
    q._last_used = time.monotonic() - 120  # the timer saw an idle worker…
    async with q._lock:  # …but a sentence took the lock first
        unload = asyncio.create_task(q._unload_idle())
        await asyncio.sleep(0.01)
        q._last_used = time.monotonic()  # and finished just now
    await unload
    assert q.loaded() and not backend.closed  # the worker stays for the next sentence
    q._last_used = time.monotonic() - 120
    await q._unload_idle()
    assert not q.loaded() and backend.closed  # genuinely idle: unloaded
    await q.aclose()


async def test_background_tasks_are_referenced(paths: Paths, catalog: ModelCatalog, settings: SettingsBox) -> None:
    backend = InProcessBackend(qw.FakeModel)
    q = provider(paths, catalog, settings, lambda: backend, idle_unload_s=0.05)
    await collect(q)
    q._last_used = time.monotonic() - 1
    q._unload_if_idle()
    assert len(q._tasks) == 1  # the unload task is held until it is done
    for _ in range(100):
        if not q._tasks:
            break
        await asyncio.sleep(0.01)
    assert not q._tasks and backend.closed
    await q.aclose()


# ------------------------------------------------------------------ 4. stderr is always drained


async def test_stderr_is_drained_through_endless_lines_and_read_errors() -> None:
    err = asyncio.StreamReader(limit=1024)
    err.feed_data(b"x" * 300_000 + b"\nloaded the model\n" + b"no newline at the end")
    err.feed_eof()
    w = WorkerBackend()
    w._proc = _FakeProc(asyncio.StreamReader(), err)  # type: ignore[assignment]
    await asyncio.wait_for(w._read_stderr(), 2)
    assert "loaded the model" in w._tail and w._tail[-1] == "no newline at the end"

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def read(self, n: int) -> bytes:
            self.calls += 1
            if self.calls == 1:
                raise ValueError("Separator is not found, and chunk exceed the limit")
            return b"still here\n" if self.calls == 2 else b""

    w2 = WorkerBackend()
    proc = _FakeProc(asyncio.StreamReader())
    proc.stderr = Flaky()  # type: ignore[assignment]
    w2._proc = proc  # type: ignore[assignment]
    await asyncio.wait_for(w2._read_stderr(), 2)
    assert "still here" in w2._tail  # kept reading after the error


async def test_a_worker_printing_megabytes_to_stderr_keeps_generating(paths: Paths) -> None:
    """The real worker with ~1 MB of output on stderr in one line (more than a pipe holds) still answers."""
    w = WorkerBackend(args=["--fake"])
    try:
        await w.load("v", paths.home, None)
        got = [p async for p in w.generate({"text": "__chatty__ then speak", "voice": "Ryan", "interval": 0.5})]
        assert sum(p.size for p in got) > 0 and w.alive
    finally:
        await w.close()


# ------------------------------------------------------------------ 5. a slow cancel isn't a crash


async def test_a_slow_cancel_gets_a_fresh_worker_without_counting_a_crash(paths: Paths, catalog: ModelCatalog,
                                                                          settings: SettingsBox) -> None:
    made: list[ScriptedBackend] = []

    def factory() -> ScriptedBackend:
        made.append(ScriptedBackend(idle=not made))  # the first worker's cancelled generation never stops…
        return made[-1]

    q = provider(paths, catalog, settings, factory)
    await collect(q)
    made[0]._idle_ok = False
    audio = await collect(q)
    assert audio.size == 24000 and made[0].closed and len(made) == 2
    assert not q._crashes  # …which doesn't use up the restarts a real crash gets
    await q.aclose()


async def test_ping_leaves_no_queue_behind(paths: Paths) -> None:
    w = WorkerBackend(args=["--fake"])
    try:
        await w.load("v", paths.home, None)
        for _ in range(3):
            assert await w.ping()
        assert not w._queues
    finally:
        await w.close()
    assert not await w.ping() and not w._queues  # a stopped worker: no answer, nothing registered


# ------------------------------------------------------------------ 8. load failures say why


@pytest.mark.parametrize(("error", "says"), [
    (TimeoutError(), "took too long to load"),
    (WorkerCrashed("killed"), "perhaps for lack of memory"),
    (RuntimeError("bad safetensors"), "Remove it and download it again"),
    (OSError("no python"), "couldn't start"),
])
async def test_load_failures_say_why_and_are_logged(paths: Paths, catalog: ModelCatalog, settings: SettingsBox,
                                                    error: BaseException, says: str,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    async def fail() -> None:
        raise error

    backend = ScriptedBackend(load=fail)
    q = provider(paths, catalog, settings, lambda: backend)
    with pytest.raises(EngineError) as err:
        await collect(q)
    assert says in err.value.detail and backend.closed and q._backend is None
    assert any("loading Qwen3-TTS" in r.message and r.exc_info for r in caplog.records)
