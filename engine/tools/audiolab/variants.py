"""`kokoro-variants`: which Kokoro weights to ship, by measurement (q8 = shipped today, fp16, q8f16, fp32; all from
onnx-community/Kokoro-82M-v1.0-ONNX-timestamped @ dd4401a9, downloaded into the scratch directory only).

Each variant runs in its own process (clean memory numbers) with the engine's own session options: session load
time and memory, first audio for 22/64/89/200-character sentences (warm, median of 5), real-time factor over 60
sentences (40 English with af_heart, 20 Spanish with ef_dora), the 4–12 kHz noise floor in pauses, then
log-spectral distance to fp32 (DTW-aligned log-mel) and Parakeet round-trip WER.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from open_speech.playback import metrics as m
from tools.audiolab.sources import (
    EN_SENTENCES,
    ENGINE,
    ES_SENTENCES,
    SCRATCH,
    SR,
    Variant,
    bundled_variant,
    kokoro_audio,
    make_kokoro,
)

REVISION = "dd4401a9add81ac692d20e240d22ec9dda82cc29"
BASE = f"https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/{REVISION}"
PINS = {  # Hugging Face LFS sha256 at REVISION
    "fp32": ("onnx/model.onnx", "651ea8291843a92276a4a003581a215cb07d15e47dde6fcfb1b768f9a1682054"),
    "fp16": ("onnx/model_fp16.onnx", "220724d5c5e0cc01be30f38faa6cf0c895a7cde6e7773e91db2973c8c7e5123c"),
    "q8f16": ("onnx/model_q8f16.onnx", "db06e3f12daac36b13638ff6f6c4541241705405dac6d23126a7827dadf4c677"),
}
FIRST_AUDIO = {
    22: "Turn left at the light.",
    64: "The library will be closed for renovations until the end of March.",
    89: "Our flight was delayed by two hours because of a storm over the mountains, so we waited.",
    200: ("The quarterly report shows a modest increase in revenue and a sharp drop in costs, which the board "
          "attributes to the new suppliers, the smaller office, and a long winter that kept everyone indoors."),
}
DIR = SCRATCH.parent / "kokoro-variants"


def variant(name: str) -> Variant:
    if name == "q8":
        return bundled_variant()
    rel, sha = PINS[name]
    path = DIR / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  downloading {rel} …")
        subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", str(path) + ".part", f"{BASE}/{rel}"], check=True)
        Path(str(path) + ".part").rename(path)
    h = hashlib.sha256(path.read_bytes()).hexdigest()
    if h != sha:
        raise SystemExit(f"{rel}: sha256 {h}, expected {sha}")
    return Variant(name, rel, path)


def measure_one(name: str) -> dict[str, Any]:
    v = variant(name)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    tts = make_kokoro(v)
    tts.warm("kokoro-82m-v1.0", "af_heart")
    load_s = time.perf_counter() - t0
    tts.synthesize_sync("Warm up.", "kokoro-82m-v1.0", "af_heart")
    first: dict[str, float] = {}
    for chars, text in FIRST_AUDIO.items():
        runs = []
        for _ in range(5):
            t = time.perf_counter()
            tts.synthesize_sync(text, "kokoro-82m-v1.0", "af_heart")
            runs.append(time.perf_counter() - t)
        first[str(chars)] = round(1000 * float(np.median(runs)), 1)
    synth_s = audio_s = 0.0
    noise: list[float] = []
    for voice, texts in (("af_heart", EN_SENTENCES), ("ef_dora", ES_SENTENCES)):
        for text in texts:
            t = time.perf_counter()
            pcm, _ = tts.synthesize_sync(text, "kokoro-82m-v1.0", voice)
            synth_s += time.perf_counter() - t
            audio_s += pcm.size / SR
            kokoro_audio(text, voice, variant=v, tts=_Cached(pcm))  # write the WAV for comparison/listening
            n = m.pause_noise_db(pcm, SR)
            if np.isfinite(n):
                noise.append(n)
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "variant": name,
        "file": v.model_file,
        "size_mb": round(v.path.stat().st_size / 1e6, 1),
        "session_load_s": round(load_s, 3),
        "peak_rss_mb": round(rss1 / 1e6, 1),  # macOS reports bytes
        "rss_growth_mb": round((rss1 - rss0) / 1e6, 1),
        "first_audio_ms": first,
        "rtf": round(synth_s / audio_s, 4),
        "pause_noise_4_12k_db": round(float(np.median(noise)), 1) if noise else None,
    }


class _Cached:
    """Lets `kokoro_audio` store a waveform already synthesized (no second synthesis)."""

    def __init__(self, pcm: np.ndarray) -> None:
        self.pcm = pcm

    def synthesize_sync(self, *_a: Any) -> tuple[np.ndarray, list[Any]]:
        return self.pcm, []


def run(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "one", None):
        out = measure_one(args.one)
        Path(args.json).write_text(json.dumps(out), "utf-8")
        return out
    names = args.variants.split(",")
    rows: dict[str, dict[str, Any]] = {}
    for name in names:
        variant(name)  # download/verify before timing anything
        out_json = SCRATCH / f"variant-{name}.json"
        cmd = [sys.executable, "-m", "tools.audiolab", "kokoro-variants", "--one", name, "--json", str(out_json)]
        subprocess.run(cmd, cwd=ENGINE, check=True)
        rows[name] = json.loads(out_json.read_text("utf-8"))
        print("  ", rows[name])
    # Distances to fp32 and round-trip WER, per variant.
    ref_name = "fp32" if "fp32" in rows else names[0]
    sets = [("af_heart", EN_SENTENCES, "en"), ("ef_dora", ES_SENTENCES, "es")]
    asr = None
    if not args.no_wer:
        try:
            from tools.audiolab.asr import Parakeet

            asr = Parakeet()
        except Exception as exc:  # noqa: BLE001
            print(f"  (no Parakeet: {exc})")
    for name in names:
        v = variant(name)
        lsd: list[float] = []
        errs = {"en": [0, 0], "es": [0, 0]}
        for voice, texts, lang in sets:
            for text in texts:
                x = kokoro_audio(text, voice, variant=v)
                if name != ref_name:
                    lsd.append(m.log_spectral_distance(kokoro_audio(text, voice, variant=variant(ref_name)), x, SR))
                if asr is not None:
                    from tools.audiolab.asr import wer

                    e, n = wer(text, asr.transcribe(x, lang))
                    errs[lang][0] += e
                    errs[lang][1] += n
        rows[name]["lsd_vs_" + ref_name + "_db"] = round(float(np.mean(lsd)), 3) if lsd else 0.0
        if asr is not None:
            rows[name]["wer_en"] = round(errs["en"][0] / max(1, errs["en"][1]), 4)
            rows[name]["wer_es"] = round(errs["es"][0] / max(1, errs["es"][1]), 4)
    print()
    for name in names:
        print("  ", rows[name])
    return {"revision": REVISION, "variants": rows}
