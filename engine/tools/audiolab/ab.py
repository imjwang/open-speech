"""`ab`: blind, loudness-matched A/B pairs for the owner's ears → ~/Downloads/open-speech-listening/.

Each pair is two WAVs (`NN-A.wav`, `NN-B.wav`) of the same speech, matched to the same speech level (gated RMS,
-23 dBFS) so the louder one doesn't win. Which side is "old"/"new" is decided at random per pair; `README.txt`
says what to listen for without giving it away, `KEY - open after listening.txt` says which is which.
"""

from __future__ import annotations

import argparse
import io
import json
import random
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from open_speech.playback import metrics as m
from open_speech.playback.stretch import stretch
from tools.audiolab.legacy_stretch import legacy_stretch
from tools.audiolab.render import Event, render
from tools.audiolab.sources import LISTENING, SR, kokoro_audio, make_kokoro, passages, write_wav

TARGET_DB = -23.0


def _match(*xs: np.ndarray) -> list[np.ndarray]:
    out = []
    for x in xs:
        g = 10 ** ((TARGET_DB - m.active_rms_db(x, SR)) / 20)
        out.append((x * g).astype(np.float32))
    peak = max(float(np.max(np.abs(y))) for y in out)
    if peak > 0.98:
        out = [(y * (0.98 / peak)).astype(np.float32) for y in out]
    return out


def _gap(s: float = 0.6) -> np.ndarray:
    return np.zeros(int(s * SR), dtype=np.float32)


def _opus(x: np.ndarray, level: float | None) -> np.ndarray:
    buf = io.BytesIO()
    kw = {} if level is None else {"compression_level": level}
    sf.write(buf, np.clip(x, -1, 1), SR, format="OGG", subtype="OPUS", **kw)
    y, _ = sf.read(io.BytesIO(buf.getvalue()), dtype="float32")
    return np.asarray(y, dtype=np.float32)[: x.size]


def run(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out) if args.out else LISTENING
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261122)
    ps = passages()
    female = ps[0][2]
    male = ps[1][2]
    both = np.concatenate([female, _gap(), male])
    pairs: list[dict[str, Any]] = []

    def add(title: str, listen_for: str, old: np.ndarray, new: np.ndarray, old_label: str, new_label: str) -> None:
        a, b = _match(old, new)
        swap = rng.random() < 0.5
        first, second = (b, a) if swap else (a, b)
        n = len(pairs) + 1
        write_wav(out_dir / f"{n:02d}-A.wav", first)
        write_wav(out_dir / f"{n:02d}-B.wav", second)
        pairs.append({"pair": n, "title": title, "listen_for": listen_for,
                      "A": new_label if swap else old_label, "B": old_label if swap else new_label})
        print(f"  {n:02d} {title}")

    legacy_1x = np.concatenate([render([female], 1.0, stretcher_factory="legacy").audio, _gap(),
                                render([male], 1.0, stretcher_factory="legacy").audio])
    add("Normal speed (1x)", "Roughness, a faint buzz or flutter in vowels; which sounds like clean speech?",
        legacy_1x, both, "old playback (audiotsm WSOLA even at 1x)", "new playback (1x passes audio through untouched)")
    for speed in (1.5, 2.0, 3.0):
        old = np.concatenate([legacy_stretch(female, speed), _gap(), legacy_stretch(male, speed)])
        new = np.concatenate([stretch(female, speed), _gap(), stretch(male, speed)])
        add(f"{speed:g}x speed", "Flutter, warble, doubled or swallowed syllables; which is easier to follow?",
            old, new, "old stretcher (audiotsm WSOLA)", "new stretcher (normalized-correlation WSOLA)")
    alt = np.concatenate([stretch(female, 3.0, frame_ms=40, tolerance_ms=15, overlap=2), _gap(),
                          stretch(male, 3.0, frame_ms=40, tolerance_ms=15, overlap=2)])
    new3 = np.concatenate([stretch(female, 3.0), _gap(), stretch(male, 3.0)])
    add("3x speed, two settings of the new stretcher",
        "Smoothness versus clarity: one is smoother, the other drops fewer short sounds. Which would you use?",
        alt, new3, "40 ms frames, 50 % overlap (smoother splices)", "30 ms frames, 75 % overlap (shipped above 2x)")

    # Kokoro precision: the same sentences from q8 (shipped) and the full-precision weights.
    texts = [("af_heart", "The museum offers free admission on the first Sunday of every month."),
             ("am_michael", "Thunder rolled across the valley as the first drops of rain began to fall."),
             ("ef_dora", "La orquesta afinó sus instrumentos mientras el público buscaba sus asientos.")]
    try:
        from tools.audiolab.variants import variant

        sets: dict[str, np.ndarray] = {}
        for name in ("q8", "fp32"):  # fp16 produced NaNs in 8 of 60 sentences on the CPU provider: not a candidate
            v = variant(name)
            tts = make_kokoro(v)
            parts = []
            for voice, text in texts:
                parts += [kokoro_audio(text, voice, variant=v, tts=tts), _gap(0.5)]
            sets[name] = np.concatenate(parts)
        add("Local voice weights: q8 vs full precision", "Hiss in the pauses, grit or buzz on vowels, clarity.",
            sets["q8"], sets["fp32"], "Kokoro q8 (shipped, 92 MB)", "Kokoro fp32 (326 MB)")
    except Exception as exc:  # noqa: BLE001
        print(f"  (skipping the Kokoro precision pairs: {exc})")

    add("Fresh vs cached audio (old cache quality)",
        "Replays and second readings come from the cache. Swishy or muffled consonants, a phasey sound?",
        both, _opus(both, None), "fresh audio (as synthesized)", "cached at the old Opus setting (~36 kbps)")
    add("Fresh vs cached audio (new cache quality)", "Same question at the new cache setting.",
        both, _opus(both, 0.8), "fresh audio (as synthesized)", "cached at the new Opus setting (~57 kbps)")

    events = [Event(1.53, "pause"), Event(2.2, "resume"), Event(3.6, "seek", index=0, offset=int(5.3 * SR)),
              Event(5.2, "seek", index=0, offset=int(1.2 * SR)), Event(6.9, "pause"), Event(7.4, "resume"),
              Event(8.3, "stop")]
    old_cuts = render([female], 1.0, stretcher_factory="legacy", events=events).audio
    new_cuts = render([female], 1.0, events=events).audio
    add("Pause, resume, skip and stop", "Clicks or thumps where playback pauses, resumes, jumps and stops.",
        old_cuts, new_cuts, "old (hard cuts)", "new (12–15 ms fades and crossfades)")

    readme = ["Open Speech — blind listening set (headphones, normal volume)", "",
              "Each pair is the same speech twice, A and B, matched in loudness. For each pair, note which one "
              "you prefer (or 'same') before opening the key file.", ""]
    for p in pairs:
        readme += [f"{p['pair']:02d}. {p['title']}", f"    Listen for: {p['listen_for']}", ""]
    (out_dir / "README.txt").write_text("\n".join(readme), "utf-8")
    key = ["Key (open after listening)", ""]
    for p in pairs:
        key += [f"{p['pair']:02d}. {p['title']}", f"    A = {p['A']}", f"    B = {p['B']}", ""]
    (out_dir / "KEY - open after listening.txt").write_text("\n".join(key), "utf-8")
    (out_dir / ".key.json").write_text(json.dumps(pairs, indent=2), "utf-8")
    print(f"\n  {len(pairs)} pairs in {out_dir}")
    return {"dir": str(out_dir), "pairs": pairs}
