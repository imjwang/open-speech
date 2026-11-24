"""Audio outputs. The transport hands an output a `pull(n) -> float32[n]` callable (and, where it helps, an
allocation-free `pull_into(buf)`) that only copies from the renderer's rings, so it is safe to call from a
real-time audio thread and never waits on the event loop.

- `SoundDeviceOutput`: PortAudio via `sounddevice` (lazy import; CoreAudio on macOS). Opens the device at its
  native rate (the renderer resamples with soxr in its own thread; 24 kHz if that fails), with the latency and
  mode chosen by the audio lab's deadline measurements (`DEFAULT_LATENCY_S`, `DEFAULT_MODE`, docs/05 § 9);
  counts PortAudio's status flags and reopens when the system output device or its rate changes.
- `NullOutput`: headless. A thread consumes frames on a real-time clock (optionally scaled for fast tests),
  so word events and highlights still happen at the right times without a sound card.
- `DeadlineOutput`: headless, for measuring how late an audio callback would be under load (audio lab, doctor).
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import numpy as np

log = logging.getLogger(__name__)
Pull = Callable[[int], np.ndarray]
PullInto = Callable[[np.ndarray], None]

# Chosen with `python -m tools.audiolab deadlines` on an M4 MacBook Air under mixed load (Kokoro x2, G2P, Opus,
# Parakeet): see docs/05 § 9 for the table. The default "low" latency (30 ms) missed deadlines under load.
DEFAULT_LATENCY_S = 0.10
DEFAULT_MODE = "callback"
FALLBACK_RATE = 24000
# The audio callback needs the GIL. With Python's default 5 ms switch interval a busy pure-Python thread (G2P,
# opening a long document) kept it waiting up to 31 ms (69 ms under mixed load); at 1 ms the worst was 6 ms with
# G2P alone. Set when a real device output starts (process-wide; tests with NullOutput are unaffected).
SWITCH_INTERVAL_S = 0.001


def use_audio_switch_interval() -> None:
    import sys

    if sys.getswitchinterval() > SWITCH_INTERVAL_S:
        sys.setswitchinterval(SWITCH_INTERVAL_S)


@runtime_checkable
class AudioOutput(Protocol):
    """`start(rate, pull)`; optional extras the transport uses when present: `preferred_rate()`, `rate`,
    `latency_frames`, `check()` (device changed → True) and `info()`."""

    name: str

    def start(self, sample_rate: int, pull: Pull) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    @property
    def running(self) -> bool: ...


class NullOutput:
    """Consumes audio on a wall clock. `time_scale=20` plays 20 s of audio per real second (tests).
    `capture=True` keeps everything pulled (for asserting on rendered audio)."""

    name = "null"

    def __init__(self, *, time_scale: float = 1.0, block_frames: int = 480, capture: bool = False,
                 rate: int = 24000) -> None:
        self.rate = rate
        self.latency_frames = 0
        self.time_scale = max(0.01, float(time_scale))
        self.block_frames = block_frames
        self.capture = capture
        self.captured: list[np.ndarray] = []
        self.frames_played = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pull: Pull | None = None
        self._sr = 24000

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def preferred_rate(self) -> int:
        return self.rate

    def info(self) -> dict[str, Any]:
        return {"device": "null", "rate": self.rate, "latency_ms": 0.0, "mode": "null"}

    def start(self, sample_rate: int, pull: Pull) -> None:
        if self.running:
            self._pull = pull
            return
        self._sr = sample_rate
        self.rate = sample_rate
        self._pull = pull
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="open-speech-null-output", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = self.block_frames / self._sr / self.time_scale
        next_t = time.perf_counter()
        while not self._stop.is_set():
            pull = self._pull
            if pull is not None:
                try:
                    block = pull(self.block_frames)
                    if self.capture:
                        self.captured.append(np.array(block, dtype=np.float32, copy=True))
                except Exception:
                    log.exception("null output pull failed")
            self.frames_played += self.block_frames
            next_t += period
            delay = next_t - time.perf_counter()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -0.25:
                next_t = time.perf_counter()  # fell far behind (debugger, CI hiccup): don't burst

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def close(self) -> None:
        self.stop()

    def captured_audio(self) -> np.ndarray:
        return np.concatenate(self.captured) if self.captured else np.zeros(0, dtype=np.float32)


def _raise_thread_priority() -> None:
    """Ask macOS for user-interactive QoS on the calling thread (what an audio thread would get; best effort)."""
    try:
        import ctypes
        import sys

        if sys.platform != "darwin":
            return
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        lib.pthread_set_qos_class_self_np(ctypes.c_uint(0x21), ctypes.c_int(0))  # QOS_CLASS_USER_INTERACTIVE
    except Exception:  # noqa: BLE001
        return


class DeadlineOutput:
    """Headless output that behaves like a device's audio callback, for measuring: a high-priority thread wakes
    every `period_s` (the device buffer), calls `pull(n)` and records how long after its wake-up the block was
    ready. A block ready later than one period after the wake-up is a miss (on a real device: an underflow).

    `mode="blocking"` models a blocking-write stream instead: a writer thread keeps a FIFO of `latency_s` topped
    up in `period_s` blocks while the "device" drains it at the sample rate, and a miss is the FIFO running dry.
    """

    name = "deadline"

    def __init__(self, *, period_s: float = 0.01, latency_s: float | None = None, mode: str = "callback",
                 capture: bool = False) -> None:
        self.period_s = period_s
        self.latency_s = latency_s if latency_s is not None else period_s
        self.mode = mode
        self.capture = capture
        self.captured: list[np.ndarray] = []
        self.lateness: list[float] = []  # seconds from wake-up (or from when the FIFO needed data) to ready
        self.misses = 0
        self.calls = 0
        self._sr = 24000
        self.rate = 24000
        self.latency_frames = 0
        self._pull: Pull | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def preferred_rate(self) -> int:
        return self.rate

    def start(self, sample_rate: int, pull: Pull) -> None:
        self._pull = pull
        if self.running:
            return
        self._sr = sample_rate
        self.rate = sample_rate
        self.latency_frames = int(round(self.latency_s * sample_rate))
        self._stop.clear()
        target = self._run_callback if self.mode == "callback" else self._run_blocking
        self._thread = threading.Thread(target=target, name="open-speech-deadline-output", daemon=True)
        self._thread.start()

    def _block(self) -> int:
        return max(1, int(round(self.period_s * self._sr)))

    def _call(self, n: int) -> None:
        p = self._pull
        if p is None:
            return
        block = p(n)
        if self.capture:
            self.captured.append(np.array(block, dtype=np.float32, copy=True))
        self.calls += 1

    def _run_callback(self) -> None:
        _raise_thread_priority()
        n = self._block()
        period = n / self._sr
        nxt = time.perf_counter() + period
        while not self._stop.is_set():
            delay = nxt - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            self._call(n)
            late = time.perf_counter() - nxt
            self.lateness.append(late)
            if late > period:
                self.misses += 1
            nxt += period
            if time.perf_counter() - nxt > 0.5:  # suspended (debugger, sleep): start over
                nxt = time.perf_counter() + period

    def _run_blocking(self) -> None:
        _raise_thread_priority()
        n = self._block()
        cap = max(n, int(round(self.latency_s * self._sr)))
        t0 = time.perf_counter()
        written = cap  # the FIFO starts full (PortAudio pre-fills with silence)
        while not self._stop.is_set():
            # Wait until there is room for one block (a blocking write sleeps without the GIL).
            room_at = (written + n - cap) / self._sr + t0
            delay = room_at - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            need = (written / self._sr) + t0  # when the FIFO would run dry without this block
            self._call(n)
            now = time.perf_counter()
            self.lateness.append(now - room_at)
            if now > need:
                self.misses += 1
                t0 = now - written / self._sr  # the device played silence meanwhile; resync
            written += n

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None

    def close(self) -> None:
        self.stop()

    def stats(self) -> dict[str, float]:
        lat = np.asarray(self.lateness) * 1000 if self.lateness else np.zeros(1)
        return {
            "calls": float(self.calls),
            "misses": float(self.misses),
            "p50_ms": float(np.percentile(lat, 50)),
            "p99_ms": float(np.percentile(lat, 99)),
            "p999_ms": float(np.percentile(lat, 99.9)),
            "max_ms": float(lat.max()),
        }


class _CoreAudio:
    """The system's default output device and its nominal rate, read through CoreAudio (macOS only, read-only:
    no stream is opened). Used to notice a device switch (AirPods connecting, headset mode changing the rate)."""

    def __init__(self) -> None:
        self._get: Any = None
        self._addr: Any = None
        try:
            import ctypes
            import sys

            if sys.platform != "darwin":
                return
            lib = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")

            class Addr(ctypes.Structure):
                _fields_ = [("sel", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("elem", ctypes.c_uint32)]

            fn = lib.AudioObjectGetPropertyData
            fn.argtypes = [ctypes.c_uint32, ctypes.POINTER(Addr), ctypes.c_uint32, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
            fn.restype = ctypes.c_int32
            self._ct = ctypes
            self._Addr = Addr
            self._get = fn
        except Exception:  # noqa: BLE001
            self._get = None

    @staticmethod
    def _cc(code: str) -> int:
        return int.from_bytes(code.encode(), "big")

    def default_output(self) -> tuple[int, float] | None:
        if self._get is None:
            return None
        ct = self._ct
        try:
            addr = self._Addr(self._cc("dOut"), self._cc("glob"), 0)
            dev = ct.c_uint32(0)
            size = ct.c_uint32(4)
            if self._get(1, ct.byref(addr), 0, None, ct.byref(size), ct.byref(dev)) != 0:
                return None
            raddr = self._Addr(self._cc("nsrt"), self._cc("glob"), 0)
            rate = ct.c_double(0.0)
            rsize = ct.c_uint32(8)
            if self._get(dev.value, ct.byref(raddr), 0, None, ct.byref(rsize), ct.byref(rate)) != 0:
                return int(dev.value), 0.0
            return int(dev.value), float(rate.value)
        except Exception:  # noqa: BLE001
            return None


class SoundDeviceOutput:
    """PortAudio output at the device's native rate.

    - `mode="callback"`: PortAudio calls `pull_into` on its audio thread every device buffer (`latency_s`).
    - `mode="blocking"`: a writer thread pulls blocks and writes them to a blocking stream; PortAudio's own C ring
      (`latency_s` deep) absorbs any wait for the GIL. Used if the deadline harness shows callback misses.
    """

    name = "sounddevice"

    def __init__(self, *, device: Any | None = None, latency: float | str | None = None, mode: str | None = None,
                 rate: int | None = None, diagnostics: Any | None = None) -> None:
        self._device = device
        self._latency = DEFAULT_LATENCY_S if latency is None else latency
        self.mode = mode or DEFAULT_MODE
        self._want_rate = rate
        self.rate = 0
        self.latency_frames = 0
        self.diagnostics = diagnostics
        self._stream: Any | None = None
        self._pull: Pull | None = None
        self._pull_into: PullInto | None = None
        self._channels = 1
        self._mono: np.ndarray = np.zeros(4096, dtype=np.float32)
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._ca = _CoreAudio()
        self._seen: tuple[int, float] | None = None
        self._device_name: str | None = None
        self._last_check = 0.0
        self._last_callback = 0.0
        self._started = False
        # Called with (rate, latency frames) once a stream is opened and *before* it starts, so whoever renders the
        # audio (the transport's renderer) is at the new rate before the first callback asks for it.
        self.prepare: Callable[[int, int], None] | None = None

    @staticmethod
    def available() -> bool:
        try:
            import sounddevice  # noqa: F401
        except (OSError, ImportError):
            return False
        return True

    @property
    def running(self) -> bool:
        return self._stream is not None and bool(getattr(self._stream, "active", False))

    # ------------------------------------------------------------------ device facts (no stream opened)

    def preferred_rate(self) -> int:
        """The output device's native rate (what CoreAudio would otherwise convert to), or 24 kHz."""
        if self._want_rate:
            return int(self._want_rate)
        try:
            import importlib.util

            if importlib.util.find_spec("soxr") is None:
                return FALLBACK_RATE
            import sounddevice as sd

            info = sd.query_devices(self._device, kind="output")
            return int(info["default_samplerate"]) or FALLBACK_RATE
        except Exception:  # noqa: BLE001
            return FALLBACK_RATE

    def info(self) -> dict[str, Any]:
        return {"device": self._device_name, "rate": self.rate, "mode": self.mode,
                "latency_ms": round(1000 * self.latency_frames / self.rate, 1) if self.rate else 0.0}

    # ------------------------------------------------------------------ lifecycle

    def start(self, sample_rate: int, pull: Pull, *, pull_into: PullInto | None = None) -> None:
        self._pull = pull
        if pull_into is None:
            owner = getattr(pull, "__self__", None)
            pull_into = getattr(owner, "pull_into", None)
        self._pull_into = pull_into
        use_audio_switch_interval()
        if self._stream is not None:
            # A stopped stream stays bound to the device it was opened on. If the system output changed while we
            # were idle (AirPods connected, headset mode), or restarting fails, open a fresh one instead.
            cur = self._ca.default_output()
            if cur is not None and self._seen is not None and cur != self._seen:
                self._reopen(f"output device changed while idle ({self._seen} → {cur})")
            else:
                try:
                    if not self._stream.active:
                        self._stream.start()
                except Exception as exc:  # noqa: BLE001
                    self._reopen(f"restarting the output failed ({exc})")
            self._started = True
            return
        self._open_with_fallback(sample_rate)
        self._started = True

    def _open_with_fallback(self, rate: int) -> None:
        try:
            self._open(rate)
        except Exception as exc:  # noqa: BLE001
            self._close_stream()
            if rate == FALLBACK_RATE:
                raise
            log.warning("could not open the output at %s Hz (%s); using %s Hz", rate, exc, FALLBACK_RATE)
            self._open(FALLBACK_RATE)

    def _reopen(self, why: str) -> None:
        """Close the stream, re-initialize PortAudio (it only sees new devices then) and open the current system
        output at its native rate."""
        log.info("reopening the audio output: %s", why)
        self._close_stream()
        try:
            import sounddevice as sd

            sd._terminate()
            sd._initialize()
        except Exception:  # noqa: BLE001
            log.exception("re-initializing PortAudio failed")
        self._open_with_fallback(self.preferred_rate())
        if self.diagnostics is not None:
            self.diagnostics.device_reopens += 1

    def _open(self, rate: int) -> None:
        import sounddevice as sd

        info = sd.query_devices(self._device, kind="output")
        self._device_name = str(info.get("name"))
        self._channels = 2 if int(info.get("max_output_channels", 1)) >= 2 else 1
        latency = self._latency
        kw: dict[str, Any] = dict(samplerate=rate, channels=self._channels, dtype="float32", latency=latency,
                                  device=self._device)
        if self.mode == "blocking":
            stream = sd.OutputStream(blocksize=0, **kw)
        else:
            stream = sd.OutputStream(blocksize=0, callback=self._callback, **kw)
        self._stream = stream
        self.rate = int(round(stream.samplerate))
        self.latency_frames = int(round(float(stream.latency) * self.rate))
        self._seen = self._ca.default_output()
        if self.diagnostics is not None:
            self.diagnostics.device = self._device_name
            self.diagnostics.latency_ms = 1000 * self.latency_frames / self.rate
            self.diagnostics.mode = self.mode
        if self.prepare is not None:
            try:
                self.prepare(self.rate, self.latency_frames)
            except Exception:  # noqa: BLE001
                log.exception("preparing the renderer for the output failed")
        stream.start()
        if self.mode == "blocking":
            self._writer_stop.clear()
            self._writer = threading.Thread(target=self._write_loop, name="open-speech-audio-writer", daemon=True)
            self._writer.start()

    def _fill(self, n: int) -> np.ndarray:
        if self._mono.size < n:
            self._mono = np.zeros(n, dtype=np.float32)
        mono = self._mono[:n]
        if self._pull_into is not None:
            self._pull_into(mono)
        elif self._pull is not None:
            mono[:] = self._pull(n)
        else:
            mono[:] = 0.0
        return mono

    def _count(self, status: Any) -> None:
        d = self.diagnostics
        if d is None or not status:
            return
        if getattr(status, "output_underflow", False):
            d.output_underflows += 1
        if getattr(status, "output_overflow", False):
            d.output_overflows += 1
        if getattr(status, "priming_output", False):
            d.priming_output += 1

    def _callback(self, outdata: np.ndarray, frames: int, _time: Any, status: Any) -> None:
        self._last_callback = time.monotonic()
        self._count(status)
        mono = self._fill(frames)
        if self._channels == 1:
            outdata[:, 0] = mono
        else:
            outdata[:, 0] = mono
            outdata[:, 1] = mono

    def _write_loop(self) -> None:
        stream = self._stream
        block = max(256, int(self.rate * 0.01))
        buf = np.zeros((block, self._channels), dtype=np.float32)
        while not self._writer_stop.is_set() and stream is not None:
            mono = self._fill(block)
            buf[:, 0] = mono
            if self._channels == 2:
                buf[:, 1] = mono
            try:
                underflowed = stream.write(buf)
            except Exception:  # noqa: BLE001 — stream closed under us
                return
            self._last_callback = time.monotonic()
            if underflowed and self.diagnostics is not None:
                self.diagnostics.output_underflows += 1

    def check(self) -> bool:
        """Called every second or so by the transport (never from the audio thread). Reopens the stream when the
        system output device or its rate changed, or the stream died; True when the rate changed. The renderer is
        moved to the new rate through `prepare` before the new stream starts."""
        if not self._started or self._stream is None:
            return False
        now = time.monotonic()
        if now - self._last_check < 1.0:
            return False
        self._last_check = now
        cur = self._ca.default_output()
        changed = cur is not None and self._seen is not None and cur != self._seen
        dead = not bool(getattr(self._stream, "active", False))
        if not (changed or dead):
            return False
        old_rate = self.rate
        try:
            self._reopen(f"output changed ({self._seen} → {cur}){', stream stopped' if dead else ''}")
        except Exception:  # noqa: BLE001
            log.exception("reopening the audio output failed")
            return False
        return self.rate != old_rate

    def _close_stream(self) -> None:
        self._writer_stop.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
        w = self._writer
        if w is not None and w is not threading.current_thread():
            w.join(timeout=1.0)
        self._writer = None

    def stop(self) -> None:
        self._started = False
        if self._stream is not None and self._stream.active:
            if self.mode == "blocking":
                self._close_stream()
            else:
                self._stream.stop()

    def close(self) -> None:
        self._started = False
        self._close_stream()


def default_output(*, prefer_device: bool = True) -> AudioOutput:
    """`SoundDeviceOutput` when PortAudio is present, otherwise `NullOutput` (headless)."""
    if prefer_device and SoundDeviceOutput.available():
        return SoundDeviceOutput()
    return NullOutput()
