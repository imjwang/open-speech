"""`grid`: stretcher parameters by measurement. Frame length, tolerance and the compared region are varied on real
Kokoro passages at 1.5/2/3x; each configuration is scored on what the listener hears as roughness and flutter:
the share of output that no single stretch of the source explains (weak splices) and the share of voiced
frames whose pitch is off by more than 50 cents from the source they came from."""

from __future__ import annotations

import argparse
import itertools
from typing import Any

import numpy as np

from open_speech.playback import metrics as m
from open_speech.playback.stretch import Stretcher
from tools.audiolab.sources import SR, passages


def _stretch(x: np.ndarray, speed: float, **kw: Any) -> np.ndarray:
    s = Stretcher(SR, speed, **kw)
    return np.concatenate([s.process(x), s.flush()])


def evaluate(src: list[np.ndarray], speeds: list[float], **kw: Any) -> dict[str, Any]:
    rows = []
    for speed in speeds:
        for x in src:
            y = _stretch(x, speed, **kw)
            tr = m.splice_track(x, y, speed, SR)
            st = m.splice_stats(tr, SR, speed)
            dev, off = m.f0_deviation(x, y, tr, SR)
            hop = Stretcher(SR, speed, **kw).synthesis_hop
            rows.append({"speed": speed, "low_ncc": st.low_ncc_frac, "mean_ncc": st.mean_ncc, "f0_dev": dev,
                         "f0_off50": off, "mod_hop": m.frame_modulation(y, SR, SR / hop),
                         "clicks": m.clicks(y, SR).per_s})
    agg = {k: float(np.mean([r[k] for r in rows])) for k in rows[0] if k != "speed"}
    agg["score"] = agg["low_ncc"] + agg["f0_off50"]
    return agg


def intelligibility(configs: list[dict[str, Any]], speeds: list[float]) -> list[dict[str, Any]]:
    """Parakeet round-trip WER of the 40 English sentences stretched with each configuration (and the legacy
    stretcher, and unstretched), per speed."""
    from tools.audiolab.asr import Parakeet, wer
    from tools.audiolab.legacy_stretch import legacy_stretch
    from tools.audiolab.sources import EN_SENTENCES, kokoro_audio

    asr = Parakeet()
    audio = [kokoro_audio(t, "af_heart") for t in EN_SENTENCES]
    base_e = base_n = 0
    for t, x in zip(EN_SENTENCES, audio, strict=True):
        e, n = wer(t, asr.transcribe(x))
        base_e, base_n = base_e + e, base_n + n
    rows = [{"config": "unstretched", "speed": 1.0, "wer": round(base_e / base_n, 4)}]
    print("  ", rows[-1])
    cands: list[tuple[str, Any]] = [("legacy", lambda x, sp: legacy_stretch(x, sp))]
    for c in configs:
        label = f"{c['frame_ms']:g}ms/±{c['tolerance_ms']:g}ms/{c['match']}" + (
            f"/x{c['overlap']}" if "overlap" in c else "")
        kw = {k: c[k] for k in ("frame_ms", "tolerance_ms", "match", "overlap") if k in c}
        cands.append((label, lambda x, sp, kw=kw: _stretch(x, sp, **kw)))
    for label, fn in cands:
        for sp in speeds:
            e_tot = n_tot = 0
            for t, x in zip(EN_SENTENCES, audio, strict=True):
                e, n = wer(t, asr.transcribe(fn(x, sp)))
                e_tot, n_tot = e_tot + e, n_tot + n
            rows.append({"config": label, "speed": sp, "wer": round(e_tot / n_tot, 4)})
            print("  ", rows[-1])
    return rows


def combined(speeds_wer: list[float]) -> dict[str, Any]:
    """Every candidate on both kinds of evidence: splice quality on the passages and Parakeet WER."""
    src = [p[2] for p in passages()]
    configs = [{"frame_ms": f, "tolerance_ms": t, "overlap": ov, "match": "overlap"}
               for ov in (2, 4) for f in (20.0, 30.0, 40.0, 50.0) for t in (10.0, 15.0)]
    rows = []
    wer_rows = intelligibility(configs, speeds_wer)
    for c in configs:
        agg = evaluate(src, [1.5, 2.0, 3.0], **c)
        label = f"{c['frame_ms']:g}ms/±{c['tolerance_ms']:g}ms/{c['match']}/x{c['overlap']}"
        w = {r["speed"]: r["wer"] for r in wer_rows if r["config"] == label}
        row = {**c, **{k: round(v, 4) for k, v in agg.items()}, **{f"wer_{k:g}x": v for k, v in w.items()}}
        rows.append(row)
        print("  ", row)
    return {"wer": wer_rows, "results": rows}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "v2", False):
        return combined([2.0, 3.0])
    if getattr(args, "wer", False):
        configs = [{"frame_ms": f, "tolerance_ms": t, "match": "overlap"} for f in (30.0, 40.0, 50.0, 60.0)
                   for t in (15.0, 20.0)]
        return {"wer": intelligibility(configs, [2.0, 3.0])}
    src = [p[2] for p in passages()]
    speeds = [1.5, 2.0, 3.0]
    frames = [30.0, 40.0, 50.0] if args.quick else [25.0, 30.0, 40.0, 50.0, 60.0]
    tols = [8.5, 15.0] if args.quick else [8.5, 10.0, 12.5, 15.0, 20.0]
    matches = ["overlap", "frame"]
    results = []
    for frame_ms, tol_ms, match in itertools.product(frames, tols, matches):
        agg = evaluate(src, speeds, frame_ms=frame_ms, tolerance_ms=tol_ms, match=match)
        row = {"frame_ms": frame_ms, "tolerance_ms": tol_ms, "match": match, **{k: round(v, 4) for k, v in agg.items()}}
        results.append(row)
        print("  ", row)
    results.sort(key=lambda r: r["score"])
    print("\nbest:")
    for r in results[:5]:
        print("  ", r)
    return {"speeds": speeds, "results": results}
