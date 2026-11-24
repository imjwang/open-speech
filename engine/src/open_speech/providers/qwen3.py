"""Local Qwen3-TTS voices on Apple Silicon (08 § Local models, FR-VOICE-6).

- One voice list whatever weights read it: the nine preset speakers ("qwen3:qwen3-tts:Ryan", CustomVoice weights)
  and cloned voices ("qwen3:clone:<slug>", Base weights, references from a `CloneStore`). The settings
  `qwen3.size` / `qwen3.precision` choose the weights (`resolve_variant`): 0.6B or 1.7B, bf16 down to 4-bit, 25
  downloadable variants in all (`[local_weights.qwen3."…"]`, installed by local/manager.py). Changing them never
  changes a voice id; it changes the audio, so the cache identity names the variant.
- Generation runs in a worker process (providers/qwen3_worker.py, the only module that imports MLX), started on
  first use or when a document opens (`prewarm`), and stopped after 10 idle minutes so its memory (3-6 GB) goes back
  to the system. Measured on an M4: in-process generation raised the audio callback's worst lateness from 11 to
  30 ms; from the worker it stayed at idle levels.
- Streaming: audio arrives in ~0.5 s pieces (first piece after ~0.3-0.6 s), yielded as `AudioChunk`s so the
  transport can start an utterance before it is finished (playback/transport.py, growing utterances).
- One generation at a time (`max_concurrency = 1`); a cancelled synthesis stops the worker at its next piece.
- Every utterance gets a seed from its text, voice and variant, so the same request gives the same audio (a
  regenerated cache entry, or a retry after a worker crash, sounds identical).
- A crash restarts the worker once (audio already delivered is skipped on the retry, which is identical); a second
  crash, or any other failure, is a `provider_error` and the synthesizer reads on with Kokoro (`falls_back`).
- Word timings are estimated from the audio by the synthesizer (playback/timing.py, pause-anchored).
- The measured speed (seconds of audio per second of work) is kept per variant (an EWMA, persisted through `kv`):
  the transport uses it to decide when a growing utterance may start and to say when a voice can't keep up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import platform
import subprocess
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from open_speech.config import Paths, UserSettings
from open_speech.document.language import detect_language
from open_speech.errors import EngineError
from open_speech.i18n import t
from open_speech.local.bundled import bundled_models_dir
from open_speech.local.catalog import ModelCatalog, TtsModel, WeightsVariant
from open_speech.providers import qwen3_worker as qw
from open_speech.providers.base import AudioChunk, TtsLimits, TtsRequest, Units, Voice
from open_speech.providers.pcm import f32_to_bytes

log = logging.getLogger(__name__)

QWEN3 = "qwen3"
PRESET_MODEL = "qwen3-tts"
CLONE_MODEL = "clone"
DESIGN_MODEL = "design"  # not a voice: VoiceDesign renders a voice from a description (`Qwen3Tts.design`)
FAMILY_PRESET = "customvoice"
FAMILY_CLONE = "base"
FAMILY_DESIGN = "voicedesign"
SIZES = ("0.6b", "1.7b")
PRECISIONS = ("bf16", "8bit", "6bit", "5bit", "4bit")  # best first
SAMPLE_RATE = 24000
IDLE_UNLOAD_S = 600.0
RTF_PRIOR = 1.2  # x real time assumed before this variant has been measured on this Mac (conservative)
RTF_ALPHA = 0.3  # EWMA weight of the newest generation
CANCEL_TIMEOUT_S = 5.0  # a cancelled generation that hasn't stopped by then: the next one starts a fresh worker
MAX_PAYLOAD = 64 << 20  # bytes of PCM in one frame (a 0.5 s piece is 48 KB): anything larger is a broken stream
LOAD_TIMEOUT_S = 180.0
MAX_CRASHES = 2  # within CRASH_WINDOW_S: after that, failures fall back to Kokoro instead of restarting
CRASH_WINDOW_S = 600.0

# ISO 639-1 → the language names Qwen3-TTS takes. Anything else is "auto" (the model infers it from the text).
LANGUAGES = {
    "en": "english", "es": "spanish", "zh": "chinese", "ja": "japanese", "ko": "korean", "de": "german",
    "fr": "french", "ru": "russian", "pt": "portuguese", "it": "italian",
}


def qwen_language(tag: str | None) -> str:
    """"en-US" → "english", "es" → "spanish"; "auto" for anything Qwen3-TTS doesn't name."""
    return LANGUAGES.get((tag or "").lower().replace("_", "-").split("-")[0], "auto")


# ---------------------------------------------------------------------------------------------- clones


@dataclass(frozen=True)
class CloneVoice:
    """A cloned voice, as the clone store describes it (workstream 8 owns the store)."""

    slug: str
    name: str
    language: str = "en"  # ISO 639-1 of the reference recording; the voice can read every language
    ref_audio: Path | None = None  # 24 kHz mono WAV the model listens to (6-10 s of clean speech)
    ref_text: str = ""  # exactly what is said in `ref_audio`
    ref_sha256: str = ""  # identifies the reference (audio + text): part of the cache identity
    available: bool = True
    unavailable_reason: str | None = None
    source: str = "recording"  # "recording" | "file" | "design" (a designed voice reads as one, not as a clone)


class CloneStore(Protocol):
    def clones(self) -> list[CloneVoice]: ...
    def clone(self, slug: str) -> CloneVoice | None: ...


class NoClones:
    """The store until cloning is set up: no cloned voices."""

    def clones(self) -> list[CloneVoice]:
        return []

    def clone(self, slug: str) -> CloneVoice | None:
        return None


# ---------------------------------------------------------------------------------------------- variants


@dataclass(frozen=True)
class Resolution:
    variant: WeightsVariant  # what will read
    wanted: WeightsVariant  # what the settings ask for
    installed: bool
    note: str | None = None  # said when a downloaded sibling stands in for the wanted one


def variant_label(v: WeightsVariant) -> str:
    """"0.6B 8-bit", "1.7B bf16"."""
    size = v.size.upper()
    prec = v.precision if v.precision == "bf16" else f"{v.precision[:-3]}-bit"
    return t("qwen.variant", size=size, precision=prec)


def size_label(mb: float) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def resolve_variant(
    variants: list[WeightsVariant],
    family: str,
    size: str = "auto",
    precision: str = "auto",
    *,
    installed: Callable[[WeightsVariant], bool],
    memory_gb: float = 0.0,
    defaults: dict[str, Any] | None = None,
) -> Resolution | None:
    """Which weights read a voice of `family`. "auto" size is the catalog's measured default (`default_size`), or
    `small_memory_size` on Macs with `small_memory_gb` or less; "auto" precision is `default_precision`. VoiceDesign
    exists only at 1.7B. When the wanted variant isn't downloaded but another of the same family is, that one reads
    (the closest: same size first, then precision order) and the result carries a note saying so."""
    d = defaults or {}
    fam = [v for v in variants if v.family == family]
    if not fam:
        return None
    if size == "auto":
        small = float(d.get("small_memory_gb", 8))
        size = str(d.get("small_memory_size", "0.6b")) if memory_gb and memory_gb <= small else str(
            d.get("default_size", "0.6b"))
    if precision == "auto":
        precision = str(d.get("default_precision", "8bit"))
    sizes = sorted({v.size for v in fam})
    if size not in sizes:
        size = "1.7b" if "1.7b" in sizes else sizes[0]  # VoiceDesign: 1.7B only
    order = {p: i for i, p in enumerate(PRECISIONS)}
    wanted = next((v for v in fam if v.size == size and v.precision == precision), None)
    if wanted is None:
        wanted = min((v for v in fam if v.size == size), key=lambda v: order.get(v.precision, 99))
    if installed(wanted):
        return Resolution(wanted, wanted, True)
    p0 = order.get(wanted.precision, 1)

    def closeness(v: WeightsVariant) -> tuple[int, int, int]:
        return (v.size != wanted.size, abs(order.get(v.precision, 9) - p0), order.get(v.precision, 9))

    have = sorted((v for v in fam if installed(v)), key=closeness)
    if have:
        h = have[0]
        return Resolution(h, wanted, True, t("qwen.note.sibling", have=variant_label(h), want=variant_label(wanted)))
    return Resolution(wanted, wanted, False)


def utterance_seed(text: str, voice: str, variant: str) -> int:
    """A seed per utterance, so the same text, voice and weights always give the same audio."""
    h = hashlib.sha256(f"{text}|{voice}|{variant}".encode()).digest()
    return int.from_bytes(h[:4], "big") & 0x7FFFFFFF


def memory_gb_probe() -> float:
    if sys.platform != "darwin":
        return 0.0
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=False)
        return round(int(out.stdout.strip()) / 1024**3, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


# ---------------------------------------------------------------------------------------------- backends


class WorkerCrashed(Exception):
    """The worker process ended in the middle of a request."""


class Backend(Protocol):
    @property
    def alive(self) -> bool: ...
    async def load(self, variant: str, path: Path, warm: dict[str, Any] | None) -> dict[str, Any]: ...
    def generate(self, header: dict[str, Any]) -> AsyncIterator[np.ndarray]: ...
    async def wait_idle(self, timeout: float | None = None) -> bool: ...
    async def close(self) -> None: ...
    last_done: dict[str, Any]


def worker_env() -> dict[str, str]:
    """The worker's environment: offline Hugging Face/transformers, no telemetry, no progress bars, no PYTHON*
    variables (it runs isolated, `-I`)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1",
               TOKENIZERS_PARALLELISM="false", TQDM_DISABLE="1", TRANSFORMERS_NO_ADVISORY_WARNINGS="1")
    return env


class WorkerBackend:
    """providers/qwen3_worker.py in a child process, spoken to over its stdin/stdout (asyncio, no threads)."""

    def __init__(self, *, args: list[str] | None = None, python: str | None = None) -> None:
        self._args = list(args or [])
        self._python = python or sys.executable
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._queues: dict[int, asyncio.Queue[tuple[str, Any]]] = {}
        self._idle: asyncio.Future[None] | None = None
        self._n = 0
        self._tail: deque[str] = deque(maxlen=30)
        self._reader_done = False  # the stdout reader ended (EOF, a broken frame): no reply will ever come
        self.last_done: dict[str, Any] = {}

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._reader_done

    async def _start(self) -> None:
        self._reader_done = False
        self._proc = await asyncio.create_subprocess_exec(
            self._python, "-I", "-m", "open_speech.providers.qwen3_worker", *self._args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=worker_env(), limit=1 << 22,
        )
        self._reader = asyncio.get_running_loop().create_task(self._read(), name="qwen3-worker-reader")
        self._stderr = asyncio.get_running_loop().create_task(self._read_stderr(), name="qwen3-worker-stderr")
        log.info("started the Qwen3-TTS worker (pid %s)", self._proc.pid)

    async def _read(self) -> None:
        """Replies from the worker, routed to the waiting request. Ends at EOF or at the first frame that isn't one
        (a bad length, JSON or header): every waiting request then fails, the backend is no longer `alive`, and a
        worker still running is killed, so nothing waits for a reply that can't come."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        out = proc.stdout
        problem = "the worker closed its output"
        try:
            while True:
                raw = await out.readexactly(qw.HEADER.size)
                (n,) = qw.HEADER.unpack(raw)
                if n > qw.MAX_HEADER:
                    problem = f"a {n}-byte frame header"
                    break
                h = json.loads((await out.readexactly(n)).decode("utf-8"))
                if not isinstance(h, dict):
                    problem = "a frame header that isn't an object"
                    break
                size = int(h.get("bytes", 0) or 0)
                if not 0 <= size <= MAX_PAYLOAD:
                    problem = f"a {size}-byte frame"
                    break
                payload = await out.readexactly(size) if size else b""
                rid = int(h.get("id") or 0)
                q = self._queues.get(rid)
                op = h.get("op")
                if op == "chunk" and q is not None:
                    q.put_nowait(("chunk", np.frombuffer(payload, dtype="<f4").astype(np.float32)))
                elif op in ("done", "error", "loaded", "pong") and q is not None:
                    q.put_nowait((str(op), h))
                if op in ("done", "error", "loaded", "pong"):
                    self._queues.pop(rid, None)
                if op in ("done", "error", "loaded"):
                    self._set_idle()
        except asyncio.IncompleteReadError:
            pass
        except asyncio.CancelledError:
            problem = "the worker was stopped"
            raise
        except Exception as exc:  # noqa: BLE001 — a broken stream ends the worker, it must never hang a request
            problem = f"an unreadable frame ({type(exc).__name__})"
        finally:
            self._reader_done = True
            if proc.returncode is None:
                log.warning("the Qwen3-TTS worker sent %s; stopping it", problem)
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            tail = " | ".join(self._tail) or problem
            for q in self._queues.values():
                q.put_nowait(("crash", tail))
            self._queues.clear()
            self._set_idle()

    async def _read_stderr(self) -> None:
        """Log what the worker prints (its libraries print there; so does anything it writes to stdout). Reads in
        chunks and splits lines itself, so an endless line can't stop it: if this stopped while the worker runs,
        the pipe would fill and the worker would block writing."""
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        err = proc.stderr
        buf = b""
        while True:
            try:
                chunk = await err.read(1 << 14)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if proc.returncode is not None:
                    return
                await asyncio.sleep(0.05)
                continue
            if not chunk:
                self._log_line(buf)
                return
            buf += chunk
            *lines, buf = buf.split(b"\n")
            if len(buf) > 1 << 14:  # a very long line: log it in pieces
                lines.append(buf)
                buf = b""
            for line in lines:
                self._log_line(line)

    def _log_line(self, line: bytes) -> None:
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            self._tail.append(text[-300:])
            log.debug("qwen3 worker: %s", text[:2000])

    def _set_idle(self) -> None:
        if self._idle is not None and not self._idle.done():
            self._idle.set_result(None)
        self._idle = None

    async def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait for the generation in progress (a cancelled one finishing its piece). False when it is still running
        after `timeout` seconds."""
        idle = self._idle
        if idle is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(idle), timeout)
        except TimeoutError:
            return False
        return True

    def _send(self, header: dict[str, Any]) -> None:
        if not self.alive:
            raise WorkerCrashed("the worker isn't running")
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(qw.encode_frame(header))

    def _request(self, header: dict[str, Any], *, busy: bool = True) -> tuple[int, asyncio.Queue[tuple[str, Any]]]:
        if not self.alive:  # before anything is registered: a dead worker leaves no queue or idle future behind
            raise WorkerCrashed(" | ".join(self._tail) or "the worker isn't running")
        self._n += 1
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._queues[self._n] = q
        if busy:
            self._idle = asyncio.get_running_loop().create_future()
        try:
            self._send({**header, "id": self._n})
        except BaseException:
            self._queues.pop(self._n, None)
            if busy:
                self._set_idle()
            raise
        return self._n, q

    async def load(self, variant: str, path: Path, warm: dict[str, Any] | None) -> dict[str, Any]:
        if not self.alive:
            await self._start()
        _, q = self._request({"op": "load", "variant": variant, "path": str(path), "warm": warm})
        kind, h = await asyncio.wait_for(q.get(), LOAD_TIMEOUT_S)
        if kind == "crash":
            raise WorkerCrashed(str(h))
        if kind == "error":
            raise RuntimeError(str(h.get("message")))
        return dict(h)

    async def ping(self, timeout: float = 5.0) -> bool:
        rid = 0
        try:
            rid, q = self._request({"op": "ping"}, busy=False)
            kind, _ = await asyncio.wait_for(q.get(), timeout)
            return kind == "pong"
        except (WorkerCrashed, TimeoutError):
            return False
        finally:
            self._queues.pop(rid, None)

    async def generate(self, header: dict[str, Any]) -> AsyncIterator[np.ndarray]:
        rid, q = self._request({**header, "op": "generate"})
        finished = False
        try:
            while True:
                kind, item = await q.get()
                if kind == "chunk":
                    yield item
                elif kind == "done":
                    finished = True
                    self.last_done = dict(item)
                    return
                elif kind == "error":
                    finished = True
                    raise RuntimeError(str(item.get("message")))
                else:
                    finished = True
                    raise WorkerCrashed(str(item))
        finally:
            if not finished:
                # Stopped early (seek, skip, stop): the worker stops at its next piece. The next generation waits for
                # that (`wait_idle`); the provider replaces a worker that doesn't stop in time.
                with contextlib.suppress(WorkerCrashed, OSError):
                    self._send({"op": "cancel", "id": rid})

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(Exception):
                assert proc.stdin is not None
                proc.stdin.write(qw.encode_frame({"op": "exit"}))
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 5.0)
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._set_idle()
        log.info("stopped the Qwen3-TTS worker")


class InProcessBackend:
    """The same protocol with the model in this process, generating in a thread (tests use it with
    `qwen3_worker.FakeModel`; never used for real voices, whose generation loop would compete for the GIL)."""

    def __init__(self, model_factory: Callable[[str], Any] | None = None) -> None:
        self._factory = model_factory or qw.load_mlx_model
        self._model: Any = None
        self._variant: str | None = None
        self._idle: asyncio.Future[None] | None = None
        self.last_done: dict[str, Any] = {}
        self.loads = 0
        self.closed = False

    @property
    def alive(self) -> bool:
        return not self.closed

    async def load(self, variant: str, path: Path, warm: dict[str, Any] | None) -> dict[str, Any]:
        self.closed = False
        if self._model is None or self._variant != variant:
            self._model = await asyncio.to_thread(self._factory, str(path))
            self._variant = variant
            self.loads += 1
        return {"op": "loaded", **qw.model_info(self._model)}

    async def wait_idle(self, timeout: float | None = None) -> bool:
        idle = self._idle
        if idle is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(idle), timeout)
        except TimeoutError:
            return False
        return True

    async def generate(self, header: dict[str, Any]) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        stop = False
        req = qw.GenerateRequest.from_header(header)
        self._idle = idle = loop.create_future()
        model = self._model

        def work() -> None:
            t0 = time.perf_counter()
            n = 0
            try:
                for pcm in qw.generate_chunks(model, req, lambda: stop):
                    n += pcm.size
                    loop.call_soon_threadsafe(q.put_nowait, ("chunk", pcm))
                gen_s = time.perf_counter() - t0
                done = {"gen_s": gen_s, "audio_s": n / SAMPLE_RATE, "rtf": (n / SAMPLE_RATE) / max(gen_s, 1e-6),
                        "cancelled": stop}
                loop.call_soon_threadsafe(q.put_nowait, ("done", done))
            except Exception as exc:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, ("error", {"message": str(exc)}))
            finally:
                loop.call_soon_threadsafe(_resolve, idle)

        fut = loop.run_in_executor(None, work)
        try:
            while True:
                kind, item = await q.get()
                if kind == "chunk":
                    yield item
                elif kind == "done":
                    self.last_done = dict(item)
                    return
                else:
                    raise RuntimeError(str(item.get("message")))
        finally:
            stop = True
            fut.add_done_callback(lambda f: f.exception())

    async def close(self) -> None:
        self._model = None
        self._variant = None
        self.closed = True


def _load_error_key(exc: BaseException) -> str | None:
    """The message for a failed load, by cause (None: not a load failure; re-raise it, e.g. a cancel)."""
    if isinstance(exc, TimeoutError):  # before OSError: TimeoutError is one
        return "qwen.error.load_timeout"
    if isinstance(exc, WorkerCrashed):
        return "qwen.error.load_crashed"
    if isinstance(exc, RuntimeError):  # the worker answered with an error: the weights didn't load
        return "qwen.error.load"
    if isinstance(exc, OSError):
        return "qwen.error.load_start"
    return None


def _resolve(fut: asyncio.Future[None]) -> None:
    if not fut.done():
        fut.set_result(None)


BackendFactory = Callable[[], Backend]


# ---------------------------------------------------------------------------------------------- provider


class Qwen3Tts:
    id = QWEN3
    timestamps = False
    max_concurrency = 1  # one generation at a time: they would only share the same GPU
    falls_back = True  # a failure reads on with the local Kokoro voice (playback/synth.py)
    cancel_on_seek = True  # generations nobody will hear any more are stopped (playback/transport.py)
    streams = True  # yields audio while it generates (growing utterances)

    def __init__(
        self,
        catalog: ModelCatalog,
        paths: Paths,
        *,
        settings: Callable[[], UserSettings] | None = None,
        clones: CloneStore | None = None,
        backend_factory: BackendFactory | None = None,
        memory_gb: Callable[[], float] | None = None,
        platform_ok: Callable[[], bool] | None = None,
        kv: Any | None = None,
        idle_unload_s: float = IDLE_UNLOAD_S,
        bundled_models: Path | None = None,
    ) -> None:
        self._entry: TtsModel | None = catalog.tts_model(QWEN3, PRESET_MODEL)
        self._variants = catalog.weights_variants(QWEN3)
        self._paths = paths
        self._settings = settings
        self.clones: CloneStore = clones or NoClones()
        self._factory = backend_factory
        self._memory = memory_gb or memory_gb_probe
        self._memory_cache: float | None = None
        self._platform_ok = platform_ok or apple_silicon
        self._kv = kv
        self._idle_unload_s = idle_unload_s
        self._bundled = bundled_models if bundled_models is not None else bundled_models_dir()
        self._backend: Backend | None = None
        self._loaded: str | None = None  # variant id the backend has loaded
        self._lock = asyncio.Lock()  # one load-or-generate at a time
        self._last_used = 0.0
        self._unload_handle: asyncio.TimerHandle | None = None
        self._crashes: deque[float] = deque(maxlen=8)
        self._rtf: dict[str, float] = {}
        self._runtime: bool | None = None
        self._tasks: set[asyncio.Task[Any]] = set()  # background work (idle unload, closes); the loop holds weak refs
        self.last_note: str | None = None

    # ------------------------------------------------------------------ catalog and settings

    @property
    def extra(self) -> dict[str, Any]:
        return self._entry.extra if self._entry is not None else {}

    def set_clone_store(self, store: CloneStore) -> None:
        self.clones = store

    def memory_gb(self) -> float:
        if self._memory_cache is None:
            self._memory_cache = float(self._memory() or 0.0)
        return self._memory_cache

    def _choice(self) -> tuple[str, str]:
        st = self._settings() if self._settings is not None else None
        if st is None:
            return "auto", "auto"
        return st.qwen3.size, st.qwen3.precision

    def variant_dir(self, v: WeightsVariant) -> Path:
        return self._paths.models / QWEN3 / v.model

    def installed_dir(self, v: WeightsVariant) -> Path | None:
        files = v.required_files()
        dirs = [self.variant_dir(v)]
        if self._bundled is not None:
            dirs.append(self._bundled / QWEN3 / v.model)
        return next((d for d in dirs if files and all((d / f).exists() for f in files)), None)

    def installed(self, v: WeightsVariant) -> bool:
        return self.installed_dir(v) is not None

    def resolve(self, family: str = FAMILY_PRESET) -> Resolution | None:
        size, precision = self._choice()
        return resolve_variant(self._variants, family, size, precision, installed=self.installed,
                               memory_gb=self.memory_gb(), defaults=self.extra)

    @staticmethod
    def family_of(model: str) -> str:
        if model == CLONE_MODEL:
            return FAMILY_CLONE
        return FAMILY_DESIGN if model == DESIGN_MODEL else FAMILY_PRESET

    def speakers(self) -> list[str]:
        return list(self._entry.voices) if self._entry is not None else []

    def speaker_language(self, speaker: str) -> str:
        info = (self.extra.get("speakers") or {}).get(speaker) or {}
        return str(info.get("language") or "en")

    # ------------------------------------------------------------------ availability

    def runtime_available(self) -> bool:
        if self._factory is not None:
            return True
        if self._runtime is None:
            self._runtime = self._platform_ok() and importlib.util.find_spec("mlx_audio") is not None
        return self._runtime

    def unavailable_reason(self, model: str, voice: str | None = None) -> str | None:
        if self._entry is None:
            return t("unavailable.voice")
        if self._factory is None and not self._platform_ok():
            return t("qwen.unavailable.chip")
        if not self.runtime_available():
            return t("qwen.unavailable.runtime")
        family = self.family_of(model)
        if family == FAMILY_CLONE and voice is not None:
            clone = self.clones.clone(voice)
            if clone is None:
                return t("qwen.unavailable.unknown_clone", name=voice)
            if not clone.available:
                return clone.unavailable_reason or t("qwen.unavailable.clone_missing")
            if clone.ref_audio is None or not Path(clone.ref_audio).exists():
                return t("qwen.unavailable.clone_missing")
        res = self.resolve(family)
        if res is None:
            return t("unavailable.voice")
        if not res.installed:
            keys = {FAMILY_CLONE: "qwen.unavailable.download_clone", FAMILY_DESIGN: "qwen.unavailable.download_design"}
            key = keys.get(family, "qwen.unavailable.download")
            return t(key, variant=variant_label(res.wanted), size=size_label(res.wanted.size_mb))
        mem = self.memory_gb()
        if mem and res.variant.ram_gb > mem * 0.8:
            return t("qwen.unavailable.memory", ram=f"{res.variant.ram_gb:g}", mem=f"{mem:.0f}")
        return None

    def has_voice(self, model: str, voice: str) -> bool:
        if model == PRESET_MODEL:
            return voice in self.speakers()
        if model == CLONE_MODEL:
            return self.clones.clone(voice) is not None
        return False

    # ------------------------------------------------------------------ protocol

    def voices(self) -> list[Voice]:
        if self._entry is None or (self._factory is None and not self._platform_ok()):
            return []  # not offered at all on a Mac that can't run them
        out: list[Voice] = []
        langs = list(self._entry.languages)
        preset_reason = self.unavailable_reason(PRESET_MODEL)
        res = self.resolve(FAMILY_PRESET)
        styles = res is not None and res.variant.size == "1.7b"
        for speaker in self.speakers():
            native = self.speaker_language(speaker)
            out.append(Voice(
                id=f"{QWEN3}:{PRESET_MODEL}:{speaker}", provider=QWEN3, model=PRESET_MODEL,
                name=t("qwen.voice_name", speaker=speaker.replace("_", " "),
                       language=t(f"qwen.lang.{native}") if native in LANGUAGES else native),
                languages=[native, *[x for x in langs if x != native]], styles=styles, local=True,
                available=preset_reason is None, unavailable_reason=preset_reason, timestamps=False,
            ))
        for c in self.clones.clones():
            reason = self.unavailable_reason(CLONE_MODEL, c.slug)
            out.append(Voice(
                id=f"{QWEN3}:{CLONE_MODEL}:{c.slug}", provider=QWEN3, model=CLONE_MODEL,
                name=t("qwen.design_name" if c.source == "design" else "qwen.clone_name", name=c.name),
                languages=[c.language, *[x for x in langs if x != c.language]],
                styles=False, local=True, available=reason is None, unavailable_reason=reason, timestamps=False,
            ))
        return out

    def limits(self, model: str) -> TtsLimits:
        res = self.resolve(self.family_of(model))
        style = model == PRESET_MODEL and res is not None and res.variant.size == "1.7b"
        e = self._entry
        return TtsLimits(max_input_chars=e.max_input_chars if e else 2000, formats=["pcm"], style=style,
                         languages=list(e.languages) if e else [])

    def price_units(self, req: TtsRequest, audio_sec: float) -> Units:
        return Units(chars=len(req.text), audio_sec=round(audio_sec, 3))

    def language(self, req: TtsRequest) -> str:
        """The language name Qwen3-TTS is told: the request's, else the text's (when it can be told), else "auto".
        Measured (Parakeet round trip, 0.6B): Spanish told "spanish" 1.0 % word errors, "auto" 2.0 %."""
        if req.language:
            return qwen_language(req.language)
        return qwen_language(detect_language(req.text)) if len(req.text) >= 12 else "auto"

    def cache_identity(self, req: TtsRequest) -> str:
        """What the audio cache records as the model: `<model>/<variant>@<rev12>/p<params>/<language>`, plus the
        reference's hash for a cloned voice. A different size, precision, revision, sampling or reference makes
        new audio; nothing else does."""
        family = self.family_of(req.model)
        res = self.resolve(family)
        variant = res.variant if res is not None else None
        rev = (variant.weights.revision[:12] if variant is not None and variant.weights is not None else "none")
        ident = f"{req.model}/{variant.model if variant else 'none'}@{rev}/p{self.extra.get('params_version', 1)}"
        ident += f"/{self.language(req)}"
        if family == FAMILY_CLONE:
            clone = self.clones.clone(req.voice)
            ident += f"/ref-{(clone.ref_sha256 if clone else '')[:12] or 'none'}"
        return ident

    def params(self) -> dict[str, Any]:
        x = self.extra
        return {k: x[k] for k in ("temperature", "top_k", "top_p", "repetition_penalty") if k in x}

    def _job(self, req: TtsRequest, variant: WeightsVariant) -> dict[str, Any]:
        h: dict[str, Any] = {
            "text": req.text, "lang": self.language(req), "params": self.params(),
            "seed": utterance_seed(req.text, f"{req.model}:{req.voice}", variant.model),
            "interval": float(self.extra.get("stream_interval_s", 0.5)),
        }
        if self.family_of(req.model) == FAMILY_CLONE:
            clone = self.clones.clone(req.voice)
            if clone is None or clone.ref_audio is None:
                raise EngineError("invalid_request", t("qwen.unavailable.clone_missing"), provider=QWEN3)
            h.update(ref_audio=str(clone.ref_audio), ref_text=clone.ref_text)
        else:
            h["voice"] = req.voice
            if req.style and variant.size == "1.7b":
                h["instruct"] = req.style
        return h

    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]:
        reason = self.unavailable_reason(req.model, req.voice)
        if reason:
            raise EngineError("unavailable", reason, provider=QWEN3)
        res = self.resolve(self.family_of(req.model))
        assert res is not None
        self.last_note = res.note
        job = self._job(req, res.variant)
        delivered = 0  # samples already yielded: a retry after a crash (same seed, same audio) skips them
        attempt = 0
        reference: dict[str, Any] = {k: job[k] for k in ("ref_audio", "ref_text") if k in job}
        async with self._lock:
            self._last_used = time.monotonic()  # in use from now: an idle unload queued behind us must not fire
            while True:
                try:
                    backend = await self._idle_backend(res.variant, reference)
                    skip = delivered
                    async for pcm in backend.generate(job):
                        if skip:
                            k = min(skip, pcm.size)
                            pcm, skip = pcm[k:], skip - k
                        if pcm.size:
                            delivered += pcm.size
                            yield AudioChunk(pcm=f32_to_bytes(pcm), sample_rate=SAMPLE_RATE)
                    self._note_rtf(res.variant.model, backend.last_done)
                    break
                except WorkerCrashed as exc:
                    log.warning("the Qwen3-TTS worker crashed: %s", exc)
                    await self._drop_backend()
                    if not self._may_restart() or attempt >= 1:
                        raise EngineError("provider_error", t("qwen.error.crashed"), provider=QWEN3) from exc
                    attempt += 1
                except RuntimeError as exc:
                    raise EngineError("provider_error", t("qwen.error.failed", detail=str(exc)[:200]),
                                      provider=QWEN3) from exc
                finally:
                    self._last_used = time.monotonic()
                    self._schedule_unload()

    async def design(self, text: str, description: str, *, language: str | None = None, seed: int = 0) -> np.ndarray:
        """`text` said by a voice described in words ("a warm, low voice with a slight British accent"), with
        VoiceDesign weights (1.7B only). Each seed is another voice that fits the description; the same seed gives the
        same audio. Returns 24 kHz mono float32. Raises EngineError when the weights aren't downloaded or it fails."""
        reason = self.unavailable_reason(DESIGN_MODEL)
        if reason:
            raise EngineError("unavailable", reason, provider=QWEN3)
        res = self.resolve(FAMILY_DESIGN)
        assert res is not None
        job: dict[str, Any] = {
            "text": text, "instruct": description, "lang": qwen_language(language), "seed": int(seed) & 0x7FFFFFFF,
            "params": self.params(), "interval": float(self.extra.get("stream_interval_s", 0.5)),
        }
        parts: list[np.ndarray] = []
        async with self._lock:
            self._last_used = time.monotonic()
            try:
                backend = await self._idle_backend(res.variant, None)
                async for pcm in backend.generate(job):
                    parts.append(pcm)
                self._note_rtf(res.variant.model, backend.last_done)
            except WorkerCrashed as exc:
                await self._drop_backend()
                raise EngineError("provider_error", t("qwen.error.crashed"), provider=QWEN3) from exc
            except RuntimeError as exc:
                raise EngineError("provider_error", t("qwen.error.failed", detail=str(exc)[:200]),
                                  provider=QWEN3) from exc
            finally:
                self._last_used = time.monotonic()
                self._schedule_unload()
        return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, dtype=np.float32)

    def _may_restart(self) -> bool:
        now = time.monotonic()
        self._crashes.append(now)
        return sum(1 for c in self._crashes if now - c < CRASH_WINDOW_S) <= MAX_CRASHES

    # ------------------------------------------------------------------ worker lifecycle

    async def _idle_backend(self, variant: WeightsVariant, reference: dict[str, Any] | None) -> Backend:
        """`_ready`, then wait for a cancelled generation to stop; one that doesn't stop at its next piece gets a fresh
        worker. That is not a crash, so it doesn't use up the restarts a real crash gets. Call with the lock held."""
        backend = await self._ready(variant, warm=False, reference=reference)
        if not await backend.wait_idle(CANCEL_TIMEOUT_S):
            log.warning("the Qwen3-TTS worker didn't stop a cancelled generation; starting a fresh one")
            await self._drop_backend()
            backend = await self._ready(variant, warm=False, reference=reference)
        return backend

    def _new_backend(self) -> Backend:
        if self._factory is not None:
            return self._factory()
        return WorkerBackend()

    async def _ready(self, variant: WeightsVariant, *, warm: bool, reference: dict[str, Any] | None = None) -> Backend:
        """The backend with `variant` loaded (started, or restarted for another variant: one variant's memory at a
        time). Call with `self._lock` held. Base weights warm up on the clone about to speak (`reference`: its
        `ref_audio` and `ref_text`), which also encodes that reference ahead of the first sentence."""
        if self._backend is not None and self._loaded == variant.model and self._backend.alive:
            return self._backend
        await self._drop_backend()
        path = self.installed_dir(variant)
        if path is None:
            raise EngineError("unavailable", t("qwen.unavailable.download", variant=variant_label(variant),
                                               size=size_label(variant.size_mb)), provider=QWEN3)
        backend = self._new_backend()
        warm_req: dict[str, Any] | None = None
        if warm or self._factory is None:
            # The first streamed generation compiles the streaming decoder: do it while loading.
            warm_req = {"lang": "english"}
            if variant.family == FAMILY_PRESET:
                warm_req["voice"] = (self.speakers() or ["Ryan"])[0]
            elif variant.family == FAMILY_DESIGN:
                warm_req["instruct"] = "A clear, calm voice."
            elif variant.family == FAMILY_CLONE:
                if reference and reference.get("ref_audio"):
                    warm_req.update(reference)
                else:
                    warm_req = None  # nothing to clone yet: the first sentence pays for the warm-up instead
        t0 = time.monotonic()
        try:
            info = await backend.load(variant.model, path, warm_req)
        except BaseException as exc:
            # Whatever ended the load (a cancel included: a document closed while it loaded), the worker goes with
            # it; otherwise it would stay resident, unreferenced, next to the one the next sentence starts.
            await self._close_backend(backend)
            key = _load_error_key(exc)
            if key is None:
                raise
            log.warning("loading Qwen3-TTS %s failed", variant.model, exc_info=True)
            raise EngineError("provider_error", t(key), provider=QWEN3) from exc
        log.info("loaded Qwen3-TTS %s in %.1f s (%s)", variant.model, time.monotonic() - t0,
                 {k: info.get(k) for k in ("load_s", "warm_s", "active_gb")})
        self._backend, self._loaded = backend, variant.model
        return backend

    async def _drop_backend(self) -> None:
        backend, self._backend, self._loaded = self._backend, None, None
        if backend is not None:
            await self._close_backend(backend)

    async def _close_backend(self, backend: Backend) -> None:
        """Close a backend even if the caller is cancelled meanwhile (the close then finishes in the background)."""
        await asyncio.shield(self._spawn(backend.close()))

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        """A background task this provider keeps a reference to until it is done."""
        task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda t_: t_.cancelled() or t_.exception())
        return task

    def loaded(self) -> bool:
        return self._backend is not None and self._backend.alive

    async def prewarm(self, model: str | None = None, voice: str | None = None) -> None:
        """Start the worker and load the weights now (a document is opening), so the first sentence doesn't wait
        for them. Never raises: synthesis reports real problems when it runs."""
        model = model or PRESET_MODEL
        if self.unavailable_reason(model, voice):
            return
        res = self.resolve(self.family_of(model))
        if res is None:
            return
        reference: dict[str, Any] | None = None
        if model == CLONE_MODEL and voice is not None:
            clone = self.clones.clone(voice)
            if clone is not None and clone.ref_audio is not None:
                reference = {"ref_audio": str(clone.ref_audio), "ref_text": clone.ref_text}
        try:
            async with self._lock:
                await self._ready(res.variant, warm=True, reference=reference)
        except Exception:  # noqa: BLE001
            log.exception("warming the Qwen voice failed")
            return
        self._last_used = time.monotonic()
        self._schedule_unload()

    async def unload(self) -> None:
        async with self._lock:
            await self._drop_backend()

    def _schedule_unload(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._unload_handle is not None:
            self._unload_handle.cancel()
        self._unload_handle = loop.call_later(self._idle_unload_s, self._unload_if_idle)

    def _idle(self) -> bool:
        return time.monotonic() - self._last_used >= self._idle_unload_s * 0.99

    def _unload_if_idle(self) -> None:
        self._unload_handle = None
        if self._lock.locked() or not self._idle():
            if self.loaded():
                self._schedule_unload()
            return
        self._spawn(self._unload_idle())

    async def _unload_idle(self) -> None:
        """The idle unload. Checked again once the lock is ours: a sentence may have started (and taken the lock)
        between the timer and now, and its worker must not be stopped under it or right after it."""
        async with self._lock:
            if not self._idle():
                if self.loaded():
                    self._schedule_unload()
                return
            await self._drop_backend()

    async def aclose(self) -> None:
        if self._unload_handle is not None:
            self._unload_handle.cancel()
            self._unload_handle = None
        await self._drop_backend()

    # ------------------------------------------------------------------ measured speed

    def _note_rtf(self, variant: str, done: dict[str, Any]) -> None:
        audio, gen = float(done.get("audio_s") or 0), float(done.get("gen_s") or 0)
        if done.get("cancelled") or audio < 1.0 or gen <= 0:
            return
        x = audio / gen
        old = self._rtf.get(variant) or self._read_rtf(variant)
        new = x if old is None else (1 - RTF_ALPHA) * old + RTF_ALPHA * x
        self._rtf[variant] = round(new, 3)
        if self._kv is not None:
            with contextlib.suppress(Exception):
                self._kv.set_kv(f"qwen3.rtf.{variant}", f"{new:.3f}")

    def _read_rtf(self, variant: str) -> float | None:
        if self._kv is None:
            return None
        try:
            raw = self._kv.get_kv(f"qwen3.rtf.{variant}")
            return float(raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    def realtime_factor(self, model: str = PRESET_MODEL) -> float:
        """Seconds of audio per second of generation for the variant that reads `model` voices, as measured on this
        Mac (EWMA over recent utterances); a conservative prior before the first measurement."""
        res = self.resolve(self.family_of(model))
        if res is None:
            return RTF_PRIOR
        v = res.variant.model
        if v not in self._rtf:
            stored = self._read_rtf(v)
            if stored is not None:
                self._rtf[v] = stored
        return self._rtf.get(v, RTF_PRIOR)

    def rtf_measured(self, model: str = PRESET_MODEL) -> bool:
        """`realtime_factor` comes from this Mac, not the prior."""
        self.realtime_factor(model)
        res = self.resolve(self.family_of(model))
        return res is not None and res.variant.model in self._rtf

    def smallest(self, model: str = PRESET_MODEL) -> bool:
        """The variant reading `model` voices is already the smallest size."""
        res = self.resolve(self.family_of(model))
        return res is None or res.variant.size == SIZES[0]


__all__ = [
    "CLONE_MODEL",
    "DESIGN_MODEL",
    "PRESET_MODEL",
    "QWEN3",
    "CloneStore",
    "CloneVoice",
    "InProcessBackend",
    "NoClones",
    "Qwen3Tts",
    "Resolution",
    "WorkerBackend",
    "qwen_language",
    "resolve_variant",
    "utterance_seed",
]
