"""Turn a rendered output (plus its source) into the numbers the before/after table reports."""

from __future__ import annotations

from typing import Any

import numpy as np

from open_speech.playback import metrics as m

SR = 24000
LEGACY_HOP = 204  # audiotsm frame 1024 // 5 at 24 kHz → 117.6 splices/s


def stretch_report(src: np.ndarray, out: np.ndarray, speed: float, *, hop: int | None = None, sr: int = SR
                   ) -> dict[str, Any]:
    track = m.splice_track(src, out, speed, sr)
    st = m.splice_stats(track, sr, speed)
    rep: dict[str, Any] = {
        "speed": speed,
        "length_error_pct": round(100 * (out.size * speed / max(1, src.size) - 1), 3),
        "splice_jumps_per_s": round(st.jumps_per_s, 2),
        "median_jump_ms": round(st.median_jump_ms, 2),
        "max_jump_ms": round(st.max_jump_ms, 2),
        "low_ncc_frac": round(st.low_ncc_frac, 4),
        "mean_ncc": round(st.mean_ncc, 4),
        "clicks_per_s": round(m.clicks(out, sr).per_s, 3),
        "src_clicks_per_s": round(m.clicks(src, sr).per_s, 3),
        "mod_118hz_db": round(m.frame_modulation(out, sr, sr / LEGACY_HOP), 2),
        "src_mod_118hz_db": round(m.frame_modulation(src, sr, sr / LEGACY_HOP), 2),
        "dropouts": m.dropouts(out, sr),
    }
    if hop:
        rep["hop"] = hop
        rep["mod_hop_db"] = round(m.frame_modulation(out, sr, sr / hop), 2)
        rep["src_mod_hop_db"] = round(m.frame_modulation(src, sr, sr / hop), 2)
    if abs(speed - 1.0) < 1e-9:
        res, lag = m.residual_db(src, out)
        rep["residual_db"] = round(res, 1)
        rep["lag"] = lag
        n = min(src.size, out.size)
        rep["bit_exact"] = bool(lag == 0 and n == src.size and np.array_equal(src[:n], out[:n]))
        rep["altered_frac"] = round(st.altered_frac, 4)
    dev, off = m.f0_deviation(src, out, track, sr)
    rep["f0_dev_cents"] = round(dev, 2)
    rep["f0_off50_frac"] = round(off, 4)
    vo = m.median_f0(out, sr)
    vs = m.median_f0(src, sr)
    rep["pitch_shift_cents"] = round(1200 * float(np.log2(vo / vs)), 1) if vo > 0 and vs > 0 else None
    return rep


def mean_reports(reps: list[dict[str, Any]]) -> dict[str, Any]:
    keys = [k for k in reps[0] if isinstance(reps[0][k], (int, float)) and not isinstance(reps[0][k], bool)]
    out: dict[str, Any] = {}
    for k in keys:
        vals = [r[k] for r in reps if isinstance(r.get(k), (int, float))]
        out[k] = round(float(np.mean(vals)), 4) if vals else None
    for k in ("bit_exact",):
        if k in reps[0]:
            out[k] = all(bool(r.get(k)) for r in reps)
    out["n"] = len(reps)
    return out
