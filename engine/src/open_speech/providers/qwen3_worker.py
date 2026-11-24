"""Qwen3-TTS worker process: the only module that imports `mlx` / `mlx_audio` (08 § Local models, 03 § Engine).

The engine starts it as `sys.executable -I -m open_speech.providers.qwen3_worker` (providers/qwen3.py) with the
Hugging Face / transformers libraries told to stay offline. Running the model in its own process keeps its Python
generation loop (one interpreter step per 80 ms of audio, plus sampling) off the engine's GIL, so the audio and
render threads never wait for it; it gives every byte of Metal memory back when the engine stops it after 10
idle minutes; and a crash inside MLX ends this process, not the engine.

Protocol: frames on stdin/stdout. A frame is a 4-byte big-endian length, that many bytes of UTF-8 JSON (the
header), then `header["bytes"]` bytes of payload (raw little-endian float32 PCM for audio chunks).

Requests (engine → worker):
  {"op": "ping", "id"}                                  → {"op": "pong", "id"}
  {"op": "load", "id", "path", "variant", "warm"?}      → {"op": "loaded", "id", "load_s", "warm_s", "sample_rate",
                                                           "type", "size", "speakers", "languages", "active_gb"}
  {"op": "generate", "id", "text", "voice"?, "instruct"?, "ref_audio"?, "ref_text"?, "lang", "seed", "params",
   "interval"}                                          → {"op": "chunk", "id", "bytes", "sr"} + float32 PCM, …,
                                                           {"op": "done", "id", "gen_s", "audio_s", "rtf", "first_s",
                                                            "peak_gb", "cancelled"}
  {"op": "cancel", "id"}                                  the generation `id` stops at its next chunk (then "done")
  {"op": "exit"}
Errors: {"op": "error", "id", "kind", "message"}.

Generation always streams (`stream=True`): measured on an M4, the streaming decoder's output is the same audio as
decoding the whole utterance (max difference 3e-6), it starts audio after the first ~0.5 s of speech instead of
after all of it, it halves peak memory (0.6B: 3.0 GB instead of 6.4 GB for a 30 s paragraph), and it gives a
cancel point every chunk. `--fake` serves a deterministic tone model instead of MLX (tests; no weights needed).
"""

from __future__ import annotations

import io
import json
import math
import os
import queue
import struct
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import numpy as np

HEADER = struct.Struct(">I")
MAX_HEADER = 1 << 20
SAMPLE_RATE = 24000
TOKENS_PER_S = 12.5  # Qwen3-TTS-12Hz: one codec frame per 80 ms of audio
WARM_TEXT = "Hello."


# ---------------------------------------------------------------------------------------------- framing


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    if payload:
        header = {**header, "bytes": len(payload)}
    body = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return HEADER.pack(len(body)) + body + payload


def read_frame(fh: IO[bytes]) -> tuple[dict[str, Any], bytes] | None:
    """One frame from a blocking binary stream, or None at end of stream."""
    raw = _read_exact(fh, HEADER.size)
    if raw is None:
        return None
    (n,) = HEADER.unpack(raw)
    if n > MAX_HEADER:
        raise ValueError(f"frame header too large ({n} bytes)")
    body = _read_exact(fh, n)
    if body is None:
        return None
    header: dict[str, Any] = json.loads(body.decode("utf-8"))
    size = int(header.get("bytes", 0) or 0)
    payload = _read_exact(fh, size) if size else b""
    if payload is None:
        return None
    return header, payload


def _read_exact(fh: IO[bytes], n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = fh.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------------------------- generation


@dataclass
class GenerateRequest:
    text: str
    voice: str | None = None  # CustomVoice preset speaker
    instruct: str | None = None  # CustomVoice style (1.7B) / VoiceDesign description
    ref_audio: str | None = None  # Base (cloning): reference WAV path …
    ref_text: str | None = None  # … and what is said in it
    lang: str = "auto"  # mlx-audio language name ("english", "spanish", …) or "auto"
    seed: int = 0
    params: dict[str, Any] = field(default_factory=dict)  # temperature, top_k, top_p, repetition_penalty
    interval: float = 0.5  # seconds of audio per streamed chunk

    @classmethod
    def from_header(cls, h: dict[str, Any]) -> GenerateRequest:
        return cls(
            text=str(h.get("text") or ""),
            voice=h.get("voice"),
            instruct=h.get("instruct"),
            ref_audio=h.get("ref_audio"),
            ref_text=h.get("ref_text"),
            lang=str(h.get("lang") or "auto"),
            seed=int(h.get("seed") or 0),
            params=dict(h.get("params") or {}),
            interval=float(h.get("interval") or 0.5),
        )


def max_tokens_for(text: str) -> int:
    """A ceiling on codec frames for `text`, so a generation that never emits its end token stops instead of
    babbling for minutes. Generous: 0.2 s per Latin character (speech runs at ~0.07 s) and 0.6 s per CJK one,
    plus 3 s, at least 6 s."""
    cjk = sum(1 for c in text if "぀" <= c <= "鿿" or "가" <= c <= "힯")
    seconds = max(6.0, 3.0 + 0.2 * (len(text) - cjk) + 0.6 * cjk)
    return int(math.ceil(seconds * TOKENS_PER_S))


def _seed(model: Any, seed: int) -> None:
    fn = getattr(model, "seed", None)
    if callable(fn):
        fn(seed)
        return
    import mlx.core as mx

    mx.random.seed(seed)


def generate_chunks(model: Any, req: GenerateRequest, cancelled: Callable[[], bool]) -> Iterator[np.ndarray]:
    """Run one generation, yielding float32 PCM pieces as they are decoded; stops at the next piece once
    `cancelled()` is true. The same seed gives the same audio."""
    _seed(model, req.seed)
    kwargs: dict[str, Any] = {
        "text": req.text,
        "lang_code": req.lang,
        "stream": True,
        "streaming_interval": req.interval,
        "split_pattern": "",
        "max_tokens": int(req.params.get("max_tokens") or max_tokens_for(req.text)),
    }
    for k in ("temperature", "top_k", "top_p", "repetition_penalty"):
        if req.params.get(k) is not None:
            kwargs[k] = req.params[k]
    if req.voice:
        kwargs["voice"] = req.voice
    if req.instruct:
        kwargs["instruct"] = req.instruct
    if req.ref_audio:
        kwargs["ref_audio"] = req.ref_audio
        kwargs["ref_text"] = req.ref_text or ""
    gen = model.generate(**kwargs)
    try:
        for result in gen:
            pcm = np.asarray(result.audio, dtype=np.float32).reshape(-1)
            if pcm.size:
                yield pcm
            if cancelled():
                return
    finally:
        close = getattr(gen, "close", None)
        if callable(close):
            close()


def load_mlx_model(path: str) -> Any:
    from mlx_audio.tts.utils import load_model

    return load_model(Path(path))


def model_info(model: Any) -> dict[str, Any]:
    cfg = getattr(model, "config", None)
    speakers = list(getattr(model, "supported_speakers", None) or [])
    languages = [x for x in (getattr(model, "supported_languages", None) or []) if x != "auto"]
    return {
        "sample_rate": int(getattr(model, "sample_rate", SAMPLE_RATE)),
        "type": str(getattr(cfg, "tts_model_type", "") or ""),
        "size": str(getattr(cfg, "tts_model_size", "") or ""),
        "speakers": speakers,
        "languages": languages,
    }


def memory_gb(kind: str) -> float:
    try:
        import mlx.core as mx

        fn = {"active": mx.get_active_memory, "peak": mx.get_peak_memory}[kind]
        return round(fn() / 1e9, 3)
    except Exception:  # noqa: BLE001 — the fake model has no Metal memory
        return 0.0


def reset_peak() -> None:
    try:
        import mlx.core as mx

        mx.reset_peak_memory()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------- fake model


class _FakeResult:
    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio


class FakeModel:
    """Deterministic stand-in with the mlx-audio `generate` signature: a tone whose pitch follows the voice, 60 ms
    of audio per character, streamed in `streaming_interval` pieces. Texts that start with "__crash__" end the
    process, "__error__" raise, "__slow__" sleep 50 ms per piece (to test cancelling), "__chatty__" print a megabyte to
    stderr first."""

    def __init__(self, path: str | None = None) -> None:
        self.sample_rate = SAMPLE_RATE
        self._seed = 0
        cfg: dict[str, Any] = {}
        if path and (Path(path) / "config.json").exists():
            try:
                cfg = json.loads((Path(path) / "config.json").read_text("utf-8"))
            except ValueError:
                cfg = {}
        talker = cfg.get("talker_config") or {}
        self.supported_speakers = list((talker.get("spk_id") or {"ryan": 0, "aiden": 1}).keys())
        self.supported_languages = ["auto", *(talker.get("codec_language_id") or {"english": 0, "spanish": 1})]
        self.config = type("Cfg", (), {"tts_model_type": cfg.get("tts_model_type", "custom_voice"),
                                       "tts_model_size": cfg.get("tts_model_size", "0b6")})()
        self.calls: list[dict[str, Any]] = []

    def seed(self, seed: int) -> None:
        self._seed = seed

    def generate(self, text: str, voice: str | None = None, *, stream: bool = False,
                 streaming_interval: float = 2.0, **kw: Any) -> Iterator[_FakeResult]:
        self.calls.append({"text": text, "voice": voice, "stream": stream, "streaming_interval": streaming_interval,
                           **kw})
        if text.startswith("__crash__"):
            os._exit(3)
        if text.startswith("__error__"):
            raise ValueError("fake model failure")
        if text.startswith("__chatty__"):  # a library printing a megabyte in one line (more than a pipe holds)
            sys.stderr.write("y" * 1_000_000 + "\n")
            sys.stderr.flush()
        slow = text.startswith("__slow__")
        n = max(1, int(0.06 * SAMPLE_RATE * len(text)))
        pitch = 180.0 + 40.0 * (sum(map(ord, voice or kw.get("ref_text") or "x")) % 5) + (self._seed % 7)
        t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
        audio = (0.2 * np.sin(2 * np.pi * pitch * t)).astype(np.float32)
        step = max(1, int(streaming_interval * SAMPLE_RATE)) if stream else n
        for a in range(0, n, step):
            if slow:
                time.sleep(0.05)
            yield _FakeResult(audio[a : a + step])


# ---------------------------------------------------------------------------------------------- server


class Server:
    """The worker's request loop. A reader thread takes frames off stdin (so a cancel or ping is seen while a
    generation runs); the main thread loads and generates, one request at a time."""

    def __init__(self, inp: IO[bytes], out: IO[bytes], *, fake: bool = False) -> None:
        self._inp = inp
        self._out = out
        self._fake = fake
        self._write_lock = threading.Lock()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._cancelled: set[Any] = set()
        self._model: Any = None
        self._variant: str | None = None

    def send(self, header: dict[str, Any], payload: bytes = b"") -> None:
        frame = encode_frame(header, payload)
        with self._write_lock:
            self._out.write(frame)
            self._out.flush()

    def _reader(self) -> None:
        try:
            while True:
                frame = read_frame(self._inp)
                if frame is None:
                    break
                h, _ = frame
                op = h.get("op")
                if op == "cancel":
                    self._cancelled.add(h.get("id"))
                elif op == "ping":
                    self.send({"op": "pong", "id": h.get("id")})
                else:
                    self._queue.put(h)
                    if op == "exit":
                        return
        except Exception as exc:  # noqa: BLE001
            print(f"qwen3 worker: bad input ({exc})", file=sys.stderr)
        # The engine went away (stdin closed): never outlive it.
        os._exit(0)

    def serve(self) -> None:
        threading.Thread(target=self._reader, name="qwen3-reader", daemon=True).start()
        while True:
            h = self._queue.get()
            if h is None or h.get("op") == "exit":
                return
            rid = h.get("id")
            try:
                if h.get("op") == "load":
                    self._load(h)
                elif h.get("op") == "generate":
                    self._generate(h)
                else:
                    self.send({"op": "error", "id": rid, "kind": "bad_request", "message": f"unknown op {h.get('op')}"})
            except Exception as exc:  # noqa: BLE001 — report it; the engine decides what to do
                self.send({"op": "error", "id": rid, "kind": type(exc).__name__, "message": str(exc)[:500]})
            finally:
                self._cancelled.discard(rid)

    def _load(self, h: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        path = str(h.get("path") or "")
        if self._model is None or self._variant != h.get("variant"):
            self._model = None
            self._model = FakeModel(path) if self._fake else load_mlx_model(path)
            self._variant = h.get("variant")
        load_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        warm = h.get("warm")
        if isinstance(warm, dict):
            # The first streamed generation compiles the streaming decoder (measured: +2 s on 0.6B, +4.5 s on 1.7B
            # for the first sentence otherwise). Pay it here, while the document is still opening.
            req = GenerateRequest.from_header({"text": WARM_TEXT, "seed": 0, **warm})
            for _ in generate_chunks(self._model, req, lambda: False):
                pass
        self.send({"op": "loaded", "id": h.get("id"), "load_s": round(load_s, 3),
                   "warm_s": round(time.perf_counter() - t1, 3), "active_gb": memory_gb("active"),
                   **model_info(self._model)})

    def _generate(self, h: dict[str, Any]) -> None:
        rid = h.get("id")
        if self._model is None:
            self.send({"op": "error", "id": rid, "kind": "not_loaded", "message": "no model is loaded"})
            return
        req = GenerateRequest.from_header(h)
        sr = int(getattr(self._model, "sample_rate", SAMPLE_RATE))
        reset_peak()
        t0 = time.perf_counter()
        first: float | None = None
        samples = 0
        for pcm in generate_chunks(self._model, req, lambda: rid in self._cancelled):
            if first is None:
                first = time.perf_counter() - t0
            samples += pcm.size
            self.send({"op": "chunk", "id": rid, "sr": sr}, pcm.astype("<f4").tobytes())
        gen_s = time.perf_counter() - t0
        audio_s = samples / sr
        self.send({"op": "done", "id": rid, "gen_s": round(gen_s, 3), "audio_s": round(audio_s, 3),
                   "rtf": round(audio_s / gen_s, 3) if gen_s > 0 else 0.0, "first_s": round(first or gen_s, 3),
                   "peak_gb": memory_gb("peak"), "cancelled": rid in self._cancelled})


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Frames own the real stdout; anything a library prints goes to stderr (the engine logs it).
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = io.TextIOWrapper(os.fdopen(os.dup(2), "wb", buffering=0), write_through=True)
    Server(sys.stdin.buffer, out, fake="--fake" in argv).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
