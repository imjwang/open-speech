"""The harness commands (see `__main__`)."""

from __future__ import annotations

import argparse
from typing import Any

import numpy as np

from open_speech.playback import metrics as m
from tools.audiolab.analyze import mean_reports, stretch_report
from tools.audiolab.render import Event, render
from tools.audiolab.sources import SR, passages


def _factory(name: str) -> Any:
    if name == "legacy":
        return "legacy"  # the whole pre-round-3 renderer (tools/audiolab/legacy_render.py)
    if name == "legacy-stretch":
        from tools.audiolab.legacy_stretch import LegacyStretcher

        return LegacyStretcher  # the old stretcher inside today's renderer
    return None  # the engine's own stretcher


def _hop(name: str) -> int:
    if name.startswith("legacy"):
        return 204
    from open_speech.playback import stretch

    s = stretch.Stretcher(SR, 1.5)
    return int(getattr(s, "synthesis_hop", 0)) or 204


def _print_row(label: str, rep: dict[str, Any]) -> None:
    keys = ("splice_jumps_per_s", "median_jump_ms", "low_ncc_frac", "clicks_per_s", "mod_118hz_db", "mod_hop_db",
            "f0_dev_cents", "f0_off50_frac", "pitch_shift_cents", "length_error_pct", "residual_db", "bit_exact",
            "dropouts")
    print(f"  {label:<16}" + "  ".join(f"{k}={rep[k]}" for k in keys if k in rep))


# ---------------------------------------------------------------------------------------------- stretch


def stretch_cmd(speeds: list[float], stretcher: str, *, scenarios: bool) -> dict[str, Any]:
    fac = _factory(stretcher)
    hop = _hop(stretcher)
    src = passages()
    out: dict[str, Any] = {"stretcher": stretcher, "hop": hop, "source": "Kokoro q8 (bundled), 5 passages",
                           "speeds": {}}
    for speed in speeds:
        reps = []
        for voice, _text, x in src:
            y = render([x], speed, stretcher_factory=fac).audio
            rep = stretch_report(x, y, speed, hop=hop)
            rep["voice"] = voice
            reps.append(rep)
        agg = mean_reports(reps)
        out["speeds"][str(speed)] = {"mean": agg, "per_passage": reps}
        _print_row(f"{stretcher} {speed}x", agg)
    if scenarios:
        out["scenarios"] = scenario_reports(fac)
    return out


def scenario_reports(fac: Any) -> dict[str, Any]:
    """Cuts the listener hears: pause/resume, seek, stop, speed changes. Clicks per event and the worst spike."""
    src = passages()
    x = src[0][2]
    res: dict[str, Any] = {}
    cases = {
        "pause_resume": [Event(1.53, "pause"), Event(2.03, "resume"), Event(4.27, "pause"), Event(4.61, "resume")],
        "seek": [Event(2.11, "seek", index=0, offset=int(5.3 * SR)),
                 Event(3.37, "seek", index=0, offset=int(1.2 * SR))],
        "stop": [Event(2.71, "stop", index=0, offset=0)],
        "speed_changes": [Event(1.5, "speed", speed=1.5), Event(3.0, "speed", speed=2.0),
                          Event(4.0, "speed", speed=1.0), Event(5.5, "speed", speed=3.0)],
    }
    for name, events in cases.items():
        r = render([x], 1.0, stretcher_factory=fac, events=events)
        y = r.audio
        c = m.clicks(y, SR)
        cs = m.clicks(x, SR)
        res[name] = {
            "events": len(events),
            "clicks": c.events,
            "src_clicks": cs.events,
            "worst_ratio": round(c.worst_ratio, 1),
            "src_worst_ratio": round(cs.worst_ratio, 1),
            "hard_edges": m.hard_edges(y, SR),
            "src_hard_edges": m.hard_edges(x, SR),
            "dropouts": m.dropouts(y, SR),
        }
        print(f"  scenario {name:<14} clicks={c.events} (source {cs.events}) worst={c.worst_ratio:.1f} "
              f"(source {cs.worst_ratio:.1f}) hard edges={res[name]['hard_edges']} "
              f"(source {res[name]['src_hard_edges']}) "
              f"dropouts={res[name]['dropouts']}")
    return res


def deadlines_cmd(args: argparse.Namespace) -> dict[str, Any]:
    from tools.audiolab.deadlines import run

    return run(args)


def variants_cmd(args: argparse.Namespace) -> dict[str, Any]:
    from tools.audiolab.variants import run

    return run(args)


def grid_cmd(args: argparse.Namespace) -> dict[str, Any]:
    from tools.audiolab.grid import run

    return run(args)


def threads_cmd(args: argparse.Namespace) -> dict[str, Any]:
    from tools.audiolab.deadlines import threads

    return threads(args)


def ab_cmd(args: argparse.Namespace) -> dict[str, Any]:
    from tools.audiolab.ab import run

    return run(args)


__all__ = ["np", "stretch_cmd"]
