"""`qwen`: measure Qwen3-TTS variants on this Mac, the way the app runs them (providers/qwen3_worker.py in its own
process, streamed generation, a seed per sentence), to choose the default size and precision (docs/08).

    uv run python -m tools.audiolab qwen --model-dir <weights> --minutes 10 --label qwen-0.6b-8bit

- sustained: sentence after sentence for `--minutes` (a fanless MacBook Air throttles: the last minutes count):
  speed (seconds of audio per second of work, "x real time") per minute, first audio per sentence, peak memory.
- quality: 20 English + 10 Spanish sentences (Ryan; Spanish also in "auto" language mode), transcribed by the
  bundled Parakeet model (round-trip word error rate), plus how steady the voice stays across sentences (spread of
  median pitch, log-mel similarity between sentences). The WAVs stay in the scratch directory for the A/B set.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from open_speech.playback import metrics as m
from open_speech.providers import qwen3_worker as qw
from tools.audiolab.sources import EN_SENTENCES, ES_SENTENCES, SCRATCH, SR

# A long passage read sentence by sentence, as reading a document would (sustained run).
READING = EN_SENTENCES * 4


class WorkerClient:
    """A blocking client for providers/qwen3_worker.py, started exactly as the engine starts it."""

    def __init__(self, model_dir: str, *, voice: str | None = "Ryan", warm_lang: str = "english") -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1", TRANSFORMERS_OFFLINE="1",
                   TOKENIZERS_PARALLELISM="false", TQDM_DISABLE="1")
        self.proc = subprocess.Popen([sys.executable, "-I", "-m", "open_speech.providers.qwen3_worker"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.n = 0
        self.voice = voice
        warm: dict[str, Any] = {"lang": warm_lang}
        if voice:
            warm["voice"] = voice
        t0 = time.perf_counter()
        self.info, _ = self.call({"op": "load", "path": model_dir, "variant": Path(model_dir).name, "warm": warm})
        self.info["wall_s"] = round(time.perf_counter() - t0, 2)

    def call(self, h: dict[str, Any]) -> tuple[dict[str, Any], np.ndarray]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.n += 1
        self.proc.stdin.write(qw.encode_frame({**h, "id": self.n}))
        self.proc.stdin.flush()
        chunks: list[np.ndarray] = []
        while True:
            frame = qw.read_frame(self.proc.stdout)
            if frame is None:
                raise RuntimeError("the worker ended")
            r, payload = frame
            if r.get("op") == "chunk":
                chunks.append(np.frombuffer(payload, dtype="<f4").copy())
            elif r.get("op") in ("loaded", "done", "error"):
                if r.get("op") == "error":
                    raise RuntimeError(r.get("message"))
                return r, (np.concatenate(chunks) if chunks else np.zeros(0, np.float32))

    def say(self, text: str, *, lang: str = "english", seed: int | None = None, voice: str | None = None,
            interval: float = 0.5) -> tuple[dict[str, Any], np.ndarray]:
        return self.call({"op": "generate", "text": text, "voice": voice or self.voice, "lang": lang,
                          "seed": random.randrange(1 << 30) if seed is None else seed, "interval": interval})

    def job(self) -> None:
        self.say(random.choice(EN_SENTENCES))

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(timeout=10)


def sustained(c: WorkerClient, minutes: float) -> dict[str, Any]:
    t_end = time.perf_counter() + minutes * 60
    t_start = time.perf_counter()
    rows: list[dict[str, float]] = []
    i = 0
    while time.perf_counter() < t_end:
        text = READING[i % len(READING)]
        r, _ = c.say(text, seed=i)
        rows.append({"t": time.perf_counter() - t_start, "chars": len(text), "gen_s": r["gen_s"],
                     "audio_s": r["audio_s"], "first_s": r["first_s"], "peak_gb": r["peak_gb"],
                     "load1": os.getloadavg()[0]})
        i += 1
    per_min: list[float] = []
    load_min: list[float] = []  # the machine's 1-minute load average: other work on the Mac skews the speed
    for k in range(int(np.ceil(minutes))):
        sel = [x for x in rows if k * 60 <= x["t"] < (k + 1) * 60]
        if sel:
            per_min.append(round(sum(x["audio_s"] for x in sel) / sum(x["gen_s"] for x in sel), 3))
            load_min.append(round(float(np.mean([x["load1"] for x in sel])), 1))
    first = np.array([x["first_s"] for x in rows])
    total_audio = sum(x["audio_s"] for x in rows)
    total_gen = sum(x["gen_s"] for x in rows)
    last3 = [x for x in rows if x["t"] >= rows[-1]["t"] - 180]
    return {
        "sentences": len(rows),
        "x_rt_overall": round(total_audio / total_gen, 3),
        "x_rt_last3min": round(sum(x["audio_s"] for x in last3) / sum(x["gen_s"] for x in last3), 3),
        "x_rt_per_minute": per_min,
        "load_avg_per_minute": load_min,
        "first_audio_p50_s": round(float(np.median(first)), 3),
        "first_audio_p90_s": round(float(np.percentile(first, 90)), 3),
        "peak_gb": max(x["peak_gb"] for x in rows),
    }


def _mel_mean(x: np.ndarray) -> np.ndarray:
    """Average log-mel spectrum over the voiced frames (within 30 dB of the loudest): a timbre fingerprint."""
    lm = m.log_mel(x, SR)  # (frames, mels)
    level = lm.max(axis=1)
    voiced = lm[level > level.max() - 30.0]
    return np.asarray(voiced.mean(axis=0), dtype=np.float64)


def quality(c: WorkerClient, out_dir: Path, *, n_en: int = 20, n_es: int = 10) -> dict[str, Any]:
    from tools.audiolab.asr import Parakeet, wer

    out_dir.mkdir(parents=True, exist_ok=True)
    asr = Parakeet()
    res: dict[str, Any] = {}
    sets = [("en", "english", EN_SENTENCES[:n_en]), ("es", "spanish", ES_SENTENCES[:n_es]),
            ("es-auto", "auto", ES_SENTENCES[:n_es])]
    for tag, lang, texts in sets:
        edits = words = 0
        f0s: list[float] = []
        mels: list[np.ndarray] = []
        worst: list[tuple[float, str, str]] = []
        for k, text in enumerate(texts):
            _, x = c.say(text, lang=lang, seed=1000 + k)
            sf.write(out_dir / f"{tag}-{k:02d}.wav", x, SR, subtype="FLOAT")
            hyp = asr.transcribe(x, tag[:2])
            e, n = wer(text, hyp)
            edits, words = edits + e, words + n
            worst.append((e / max(1, n), text, hyp))
            f0 = m.median_f0(x, SR)
            if f0 > 0:
                f0s.append(f0)
            mels.append(_mel_mean(x))
        sims = []
        for a in range(len(mels)):
            for b in range(a + 1, len(mels)):
                u, v = mels[a] - mels[a].mean(), mels[b] - mels[b].mean()
                sims.append(float(u @ v / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-12)))
        worst.sort(reverse=True)
        res[tag] = {
            "wer": round(edits / max(1, words), 4),
            "words": words,
            "f0_median_hz": round(float(np.median(f0s)), 1) if f0s else None,
            "f0_spread_pct": round(100 * float(np.std(f0s) / np.mean(f0s)), 1) if f0s else None,
            "timbre_similarity_mean": round(float(np.mean(sims)), 3) if sims else None,
            "timbre_similarity_min": round(float(np.min(sims)), 3) if sims else None,
            "worst": [{"wer": round(w, 3), "ref": r, "hyp": h} for w, r, h in worst[:3]],
        }
        print(f"  {tag}: WER {res[tag]['wer']:.3f} over {words} words; F0 {res[tag]['f0_median_hz']} Hz "
              f"±{res[tag]['f0_spread_pct']}%; timbre similarity {res[tag]['timbre_similarity_mean']}", flush=True)
    return res


def run(args: argparse.Namespace) -> dict[str, Any]:
    c = WorkerClient(args.model_dir, voice=args.voice)
    print(f"loaded {Path(args.model_dir).name}: {c.info}", flush=True)
    out: dict[str, Any] = {"model": Path(args.model_dir).name, "load": c.info, "voice": args.voice}
    try:
        if not args.no_quality:
            out["quality"] = quality(c, SCRATCH.parent / "qwen" / "wav" / (args.label or Path(args.model_dir).name))
        if args.minutes > 0:
            out["sustained"] = sustained(c, args.minutes)
            s = out["sustained"]
            print(f"  sustained {args.minutes} min: {s['x_rt_overall']}x overall, {s['x_rt_last3min']}x last 3 min, "
                  f"per minute {s['x_rt_per_minute']} (load {s['load_avg_per_minute']}); "
                  f"first audio p50 {s['first_audio_p50_s']} s, p90 {s['first_audio_p90_s']} s; "
                  f"peak {s['peak_gb']} GB", flush=True)
    finally:
        c.close()
    return out


WAV_DIR = SCRATCH.parent / "qwen" / "wav"
PASSAGE_EN = (2, 7, 11, 15)  # sentence numbers joined into one ~20 s passage
PASSAGE_ES = (4, 6, 8)


def _passage(label: str, lang: str, idx: tuple[int, ...]) -> np.ndarray:
    gap = np.zeros(int(0.35 * SR), np.float32)
    parts: list[np.ndarray] = []
    for k in idx:
        x, _ = sf.read(WAV_DIR / label / f"{lang}-{k:02d}.wav", dtype="float32")
        parts += [np.asarray(x, dtype=np.float32), gap]
    return np.concatenate(parts[:-1])


def ab(args: argparse.Namespace) -> dict[str, Any]:
    """Blind, loudness-matched pairs → ~/Downloads/open-speech-listening/qwen/: Qwen sizes/precisions against each
    other (the same sentences, the same seeds), and a Qwen preset against Kokoro."""
    from tools.audiolab.ab import _match
    from tools.audiolab.sources import LISTENING, kokoro_audio, write_wav

    out = Path(args.out) if args.out else LISTENING / "qwen"
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261123)
    pairs: list[dict[str, Any]] = []

    def add(title: str, listen: str, a: np.ndarray, b: np.ndarray, la: str, lb: str) -> None:
        n = len(pairs) + 1
        x, y = _match(a, b)
        if rng.random() < 0.5:
            x, y, la, lb = y, x, lb, la
        write_wav(out / f"{n:02d}-A.wav", x)
        write_wav(out / f"{n:02d}-B.wav", y)
        pairs.append({"n": n, "title": title, "listen_for": listen, "A": la, "B": lb})

    labels = args.labels.split(",")
    names = dict(x.split("=", 1) for x in args.names.split(",")) if args.names else {}
    a = labels[0]  # the default candidate, against each of the others (a short set the owner can get through)
    for b in labels[1:]:
        na, nb = names.get(a, a), names.get(b, b)
        add(f"Qwen voice Ryan, English: {na} or {nb}?", "Clarity, natural rhythm, no slurred or skipped words, "
            "hiss or buzz in the pauses.", _passage(a, "en", PASSAGE_EN), _passage(b, "en", PASSAGE_EN), na, nb)
        add(f"Qwen voice Ryan, Spanish: {na} or {nb}?", "Pronunciation and accent; which would you listen to?",
            _passage(a, "es", PASSAGE_ES), _passage(b, "es", PASSAGE_ES), na, nb)
    base = labels[0]
    texts = [EN_SENTENCES[k] for k in PASSAGE_EN]
    gap = np.zeros(int(0.35 * SR), np.float32)
    for kv, kname in (("af_heart", "Kokoro Heart (female)"), ("am_michael", "Kokoro Michael (male)")):
        kok = np.concatenate([np.concatenate([kokoro_audio(t_, kv), gap]) for t_ in texts])[: -gap.size]
        add(f"Qwen Ryan ({names.get(base, base)}) or {kname}?", "Which voice would you rather read a long article "
            "with?", _passage(base, "en", PASSAGE_EN), kok, f"Qwen Ryan ({names.get(base, base)})", kname)
    readme = ["Open Speech — Qwen voices, blind listening set (headphones, normal volume)", "",
              "Each pair is the same text twice, A and B, matched in loudness. Note which you prefer (or 'same') "
              "before opening the key.", "Google Chirp isn't in this set: the build's key can't call Cloud "
              "Text-to-Speech yet.", ""]
    key = ["Key (open after listening)", ""]
    for pr in pairs:
        readme += [f"{pr['n']:02d}. {pr['title'].split(':')[0]}", f"    Listen for: {pr['listen_for']}", ""]
        key += [f"{pr['n']:02d}. {pr['title']}", f"    A = {pr['A']}", f"    B = {pr['B']}", ""]
    (out / "README.txt").write_text("\n".join(readme), "utf-8")
    (out / "KEY - open after listening.txt").write_text("\n".join(key), "utf-8")
    (out / ".key.json").write_text(json.dumps(pairs, indent=1), "utf-8")
    print(f"{len(pairs)} pairs in {out}")
    return {"pairs": pairs, "out": str(out)}


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("qwen-ab")
    p.add_argument("--labels", required=True, help="comma-separated result labels (wav folders); first = default")
    p.add_argument("--names", default=None, help="label=Display name,…")
    p.add_argument("--out", default=None)
    p = sub.add_parser("qwen")
    p.add_argument("--model-dir", required=True)
    p.add_argument("--voice", default="Ryan")
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--no-quality", action="store_true")
    p.add_argument("--label", default=None)
