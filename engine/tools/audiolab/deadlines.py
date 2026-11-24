"""`deadlines`: how late would the audio callback be under the engine's real background load?

A `DeadlineOutput` probe (a high-priority thread, like CoreAudio's IO thread) pulls from the engine's real
`Renderer` (reading at 1.5x, so the stretcher works in the render thread) every 10 ms, while load threads
run what the engine runs during reading: Kokoro synthesis (one or two at once), English G2P, Opus encoding,
Parakeet speech-to-text. Each call's lateness (wake-up to block ready) is recorded. For a device period L
the expected misses per minute are P(lateness > L) x 60 / L; the chosen setting is then confirmed by a
direct run at that period.

`threads` measures Kokoro's speed (RTF) and the probe's lateness per onnxruntime intra-op thread count.
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from open_speech.playback.output import DeadlineOutput
from open_speech.playback.render import Renderer, RenderItem
from tools.audiolab.sources import BUNDLED_MODELS, EN_SENTENCES, SR, make_kokoro, passages

# "mixed" runs everything at once, flat out (two Kokoro syntheses, Parakeet, G2P, Opus): harsher than the app
# can be (Kokoro now synthesizes one sentence at a time, and reading is paused while the bar listens).
# "reading" is the app's worst case while reading: one Kokoro synthesis, G2P (opening a long document) and Opus.
# "qwen-inproc" / "qwen-worker": a local Qwen3-TTS voice synthesizing flat out (plus Opus encoding of its audio),
# generated inside this process (MLX's Python generation loop competes for the GIL) or in the engine's worker
# process (providers/qwen3_worker.py; this process only reads its frames). Needs --qwen-model <weights dir>.
SCENARIOS = ("idle", "kokoro1", "kokoro2", "g2p", "opus", "parakeet", "reading", "mixed", "qwen-inproc", "qwen-worker")


def legacy_session_factory(path: Any) -> Any:
    """onnxruntime's defaults, as the engine created sessions before round 3."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


def session_factory(threads: int | None, legacy: bool) -> Callable[[Any], Any] | None:
    if legacy:
        return legacy_session_factory
    if threads is None:
        return None  # the engine's own
    from open_speech.providers import kokoro as kmod

    def make(path: Any) -> Any:
        return kmod._default_session_factory(path, intra_op_threads=threads)

    return make


class Load:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.work = 0

    def add(self, fn: Callable[[], None], name: str) -> None:
        def loop() -> None:
            while not self.stop.is_set():
                fn()
                self.work += 1

        t = threading.Thread(target=loop, name=name, daemon=True)
        self.threads.append(t)
        t.start()

    def close(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=30)


def _kokoro_job(tts: Any) -> Callable[[], None]:
    def job() -> None:
        text = random.choice(EN_SENTENCES)
        tts.synthesize_sync(text, "kokoro-82m-v1.0", "af_heart")

    return job


def _g2p_job() -> Callable[[], None]:
    from open_speech.providers.g2p import phonemizer_for

    ph = phonemizer_for("en")
    words = " ".join(EN_SENTENCES).split()

    def job() -> None:
        random.shuffle(words)
        ph.phonemize(" ".join(words[:400]))

    return job


def _opus_job() -> Callable[[], None]:
    from open_speech.providers.pcm import encode_ogg_opus

    x = np.concatenate([p[2] for p in passages()[:2]])

    def job() -> None:
        encode_ogg_opus(x, SR)

    return job


def _parakeet_job() -> Callable[[], None] | None:
    try:
        from open_speech.local.catalog import ModelCatalog
        from open_speech.providers.local_stt import SherpaRecognizer
        from open_speech.providers.pcm import resample
    except Exception:  # noqa: BLE001
        return None
    entry = ModelCatalog.load().default_local_stt_model()
    if entry is None:
        return None
    d = BUNDLED_MODELS / "stt" / entry.model
    if not d.is_dir():
        return None
    rec = SherpaRecognizer(entry, d)
    audio = resample(passages()[0][2], SR, 16000)

    def job() -> None:
        rec.transcribe(audio, "en")

    return job


def _qwen_inproc_job(model_dir: str) -> Callable[[], None]:
    from open_speech.providers import qwen3_worker as qw

    model = qw.load_mlx_model(model_dir)
    voice = (qw.model_info(model)["speakers"] or [None])[0]

    def job() -> None:
        req = qw.GenerateRequest(text=random.choice(EN_SENTENCES), voice=voice, lang="english",
                                 seed=random.randrange(1 << 30))
        for _ in qw.generate_chunks(model, req, lambda: False):
            pass

    job()  # load + compile before the probe starts
    return job


def _feeder(r: Renderer, stop: threading.Event) -> threading.Thread:
    src = [p[2] for p in passages()]

    def loop() -> None:
        i = 0
        while not stop.is_set():
            if len(r.queued_indices()) < 2:
                r.enqueue(RenderItem(i, src[i % len(src)]))
                i += 1
            stop.wait(0.05)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


DEVICE_RATE = 48000  # the MacBook Air's native rate: the renderer resamples to it, as in the app


def make_probe_renderer(legacy: bool) -> Any:
    if legacy:
        from tools.audiolab.legacy_render import Renderer as LegacyRenderer

        return LegacyRenderer(SR)
    return Renderer(SR, rate=DEVICE_RATE)


def probe(load_setup: Callable[[Load], None], seconds: float, *, period_s: float = 0.01, mode: str = "callback",
          latency_s: float | None = None, legacy_renderer: bool = False) -> dict[str, Any]:
    r = make_probe_renderer(legacy_renderer)
    r.set_speed(1.5)
    r.start()
    r.pause_reading(False)
    stop = threading.Event()
    feeder = _feeder(r, stop)
    load = Load()
    load_setup(load)
    time.sleep(0.5)  # let the load threads start (session loads etc.)
    out = DeadlineOutput(period_s=period_s, latency_s=latency_s, mode=mode)
    out.start(SR if legacy_renderer else DEVICE_RATE, r.pull)
    time.sleep(seconds)
    out.stop()
    load.close()
    stop.set()
    feeder.join(timeout=2)
    starved = getattr(r, "diagnostics", None)
    r.close()
    lat = np.asarray(out.lateness)
    res: dict[str, Any] = {**out.stats(), "load_jobs": load.work, "seconds": seconds}
    res["lateness"] = lat
    if starved is not None:
        res["render"] = starved.snapshot() if hasattr(starved, "snapshot") else None
    return res


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.switch_interval:
        sys.setswitchinterval(args.switch_interval)
    secs = args.minutes * 60
    lats = [float(x) / 1000 for x in args.latencies.split(",")]
    wanted = args.scenarios.split(",") if args.scenarios else list(SCENARIOS)
    fac = session_factory(args.kokoro_threads, args.legacy_session)
    tts = make_kokoro(session_factory=fac)
    tts.warm("kokoro-82m-v1.0", "af_heart")
    parakeet = _parakeet_job() if "parakeet" in wanted or "mixed" in wanted else None
    qwen_model = getattr(args, "qwen_model", None)
    qwen_inproc = _qwen_inproc_job(qwen_model) if qwen_model and "qwen-inproc" in wanted else None
    qwen_worker = None
    if qwen_model and "qwen-worker" in wanted:
        from tools.audiolab.qwen import WorkerClient

        qwen_worker = WorkerClient(qwen_model)

    def setup(name: str) -> Callable[[Load], None]:
        def s(load: Load) -> None:
            if name in ("kokoro1", "reading", "mixed"):
                load.add(_kokoro_job(tts), "kokoro-a")
            if name in ("kokoro2", "mixed"):
                load.add(_kokoro_job(tts), "kokoro-a")
                load.add(_kokoro_job(tts), "kokoro-b")
            if name in ("g2p", "reading", "mixed"):
                load.add(_g2p_job(), "g2p")
            if name in ("opus", "reading", "mixed"):
                load.add(_opus_job(), "opus")
            if name in ("parakeet", "mixed") and parakeet is not None:
                load.add(parakeet, "parakeet")
            if name == "qwen-inproc" and qwen_inproc is not None:
                load.add(qwen_inproc, "qwen")
                load.add(_opus_job(), "opus")
            if name == "qwen-worker" and qwen_worker is not None:
                load.add(qwen_worker.job, "qwen")
                load.add(_opus_job(), "opus")

        return s

    out: dict[str, Any] = {
        "switch_interval_s": sys.getswitchinterval(),
        "kokoro_session": "legacy (onnxruntime defaults)" if args.legacy_session else (
            f"intra_op={args.kokoro_threads}" if args.kokoro_threads else "engine default"),
        "probe_period_ms": args.period_ms,
        "mode": args.mode,
        "renderer": "legacy (pre-round-3)" if args.legacy_renderer else f"engine at {DEVICE_RATE} Hz",
        "scenarios": {},
    }
    for name in wanted:
        if name == "parakeet" and parakeet is None:
            continue
        if name.startswith("qwen") and not qwen_model:
            continue
        res = probe(setup(name), secs, period_s=args.period_ms / 1000, mode=args.mode,
                    latency_s=args.fifo_ms / 1000 if args.fifo_ms else None, legacy_renderer=args.legacy_renderer)
        lat = res.pop("lateness")
        per_l = {}
        for L in lats:
            p = float(np.mean(lat > L)) if lat.size else 0.0
            per_l[f"{int(L * 1000)}ms"] = {"p_late": p, "misses_per_min": round(p * 60 / L, 3),
                                            "count_over": int(np.sum(lat > L))}
        res["by_latency"] = per_l
        out["scenarios"][name] = res
        row = " ".join(f"{k}:{v['count_over']}" for k, v in per_l.items())
        print(f"  {name:<11} misses={int(res['misses'])} calls={int(res['calls'])} p50={res['p50_ms']:.2f} "
              f"p99={res['p99_ms']:.2f} "
              f"p99.9={res['p999_ms']:.2f} max={res['max_ms']:.1f} ms  jobs={res['load_jobs']}  over[L]: {row}")
    if qwen_worker is not None:
        qwen_worker.close()
    return out


def threads(args: argparse.Namespace) -> dict[str, Any]:
    """Kokoro RTF and probe lateness per intra-op thread count (one synthesis at a time and two at once)."""
    out: dict[str, Any] = {"rows": []}
    texts = EN_SENTENCES[:12]
    for t in (1, 2, 3, 4, 6, 0):
        for conc in (1, 2):
            fac = legacy_session_factory if t == 0 else session_factory(t, False)
            tts = make_kokoro(session_factory=fac)
            tts.warm("kokoro-82m-v1.0", "af_heart")
            tts.synthesize_sync(texts[0], "kokoro-82m-v1.0", "af_heart")
            totals = {"audio_s": 0.0}
            lock = threading.Lock()
            first: list[float] = []

            def work(chunk: list[str], tts: Any = tts, lock: threading.Lock = lock, first: list[float] = first,
                     totals: dict[str, float] = totals) -> None:
                for text in chunk:
                    t0 = time.perf_counter()
                    pcm, _ = tts.synthesize_sync(text, "kokoro-82m-v1.0", "af_heart")
                    with lock:
                        totals["audio_s"] += pcm.size / SR
                        first.append(time.perf_counter() - t0)

            r = Renderer(SR)
            r.set_speed(1.5)
            r.start()
            r.pause_reading(False)
            stop = threading.Event()
            feeder = _feeder(r, stop)
            dout = DeadlineOutput(period_s=0.01)
            dout.start(SR, r.pull)
            t0 = time.perf_counter()
            ths = [threading.Thread(target=work, args=(texts[i::conc],)) for i in range(conc)]
            for th in ths:
                th.start()
            for th in ths:
                th.join()
            wall = time.perf_counter() - t0
            dout.stop()
            stop.set()
            feeder.join(timeout=2)
            r.close()
            st = dout.stats()
            row = {"intra_op": "default" if t == 0 else t, "concurrent": conc,
                   "rtf": round(wall / totals["audio_s"], 4),
                   "per_sentence_p50_ms": round(1000 * float(np.median(first)), 1),
                   "probe_p99_ms": round(st["p99_ms"], 2), "probe_max_ms": round(st["max_ms"], 1)}
            out["rows"].append(row)
            print("  ", row)
    return out
