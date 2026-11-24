"""Open Speech audio lab (dev only): `uv run python -m tools.audiolab <command>` from `engine/`.

Commands
  baseline         render real Kokoro speech through the engine's Renderer at 1x and measure it
  stretch          the same at 1.25/1.5/2/3x (and the speed-change and fade scenarios)
  deadlines        audio-callback lateness under load (DeadlineOutput), per output latency and mode
  kokoro-variants  q8 / fp16 / q8f16 / fp32: first audio, RTF, noise, LSD vs fp32, Parakeet round trip
  grid             stretcher parameter grid search
  ab               loudness-matched blind A/B pairs + key → ~/Downloads/open-speech-listening/
  qwen-ab          blind A/B pairs of Qwen variants and Qwen vs Kokoro → ~/Downloads/open-speech-listening/qwen/
  qwen             a Qwen3-TTS variant as the app runs it: sustained speed, first audio, memory, round-trip WER

`--stretcher legacy` renders with the pre-round-3 audiotsm WSOLA (tools/audiolab/legacy_stretch.py) through the
same renderer, so before/after can be re-run at any time. Results are written to tools/audiolab/results/.
Nothing here opens a sound device or the microphone.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tools.audiolab import commands
from tools.audiolab.sources import RESULTS


def _save(name: str, data: Any) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    p = RESULTS / f"{name}.json"
    p.write_text(json.dumps(data, indent=2, default=str) + "\n", "utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.audiolab", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("baseline", "stretch"):
        p = sub.add_parser(name)
        p.add_argument("--stretcher", choices=("engine", "legacy", "legacy-stretch"), default="engine")
        p.add_argument("--label", default=None, help="results file name (default: <cmd>-<stretcher>)")
        p.add_argument("--speeds", default=None, help="comma-separated speeds")
    p = sub.add_parser("deadlines")
    p.add_argument("--minutes", type=float, default=1.0, help="duration per scenario")
    p.add_argument("--latencies", default="10,20,30,40,60,80,100", help="ms")
    p.add_argument("--scenarios", default=None, help="comma-separated subset")
    p.add_argument("--label", default="deadlines")
    p.add_argument("--switch-interval", type=float, default=None, help="sys.setswitchinterval (s)")
    p.add_argument("--kokoro-threads", type=int, default=None, help="override intra-op threads")
    p.add_argument("--legacy-session", action="store_true", help="onnxruntime defaults (pre-round-3)")
    p.add_argument("--legacy-renderer", action="store_true", help="the pre-round-3 renderer")
    p.add_argument("--period-ms", type=float, default=10.0, help="probe period (device buffer)")
    p.add_argument("--mode", choices=("callback", "blocking"), default="callback")
    p.add_argument("--fifo-ms", type=float, default=0.0, help="blocking mode: PortAudio FIFO depth")
    p.add_argument("--qwen-model", default=None, help="Qwen3-TTS weights dir for the qwen-* scenarios")
    p = sub.add_parser("kokoro-variants")
    p.add_argument("--variants", default="q8,fp16,q8f16,fp32")
    p.add_argument("--limit", type=int, default=0, help="first N sentences per language (0 = all)")
    p.add_argument("--no-wer", action="store_true")
    p.add_argument("--label", default="kokoro-variants")
    p.add_argument("--one", default=None, help=argparse.SUPPRESS)
    p.add_argument("--json", default=None, help=argparse.SUPPRESS)
    p = sub.add_parser("grid")
    p.add_argument("--label", default="grid")
    p.add_argument("--quick", action="store_true")
    p.add_argument("--wer", action="store_true", help="Parakeet round-trip WER of candidate configurations")
    p.add_argument("--v2", action="store_true", help="overlap 2/4 x frame x tolerance, splice metrics + WER")
    p = sub.add_parser("ab")
    p.add_argument("--out", default=None)
    p = sub.add_parser("threads")
    p.add_argument("--label", default="kokoro-threads")
    from tools.audiolab import qwen as qwen_lab

    qwen_lab.add_parsers(sub)
    args = ap.parse_args(argv)

    if args.cmd in ("baseline", "stretch"):
        speeds = [float(s) for s in args.speeds.split(",")] if args.speeds else (
            [1.0] if args.cmd == "baseline" else [1.0, 1.25, 1.5, 2.0, 3.0])
        data = commands.stretch_cmd(speeds, args.stretcher, scenarios=args.cmd == "stretch")
        path = _save(args.label or f"{args.cmd}-{args.stretcher}", data)
    elif args.cmd == "deadlines":
        data = commands.deadlines_cmd(args)
        path = _save(args.label, data)
    elif args.cmd == "kokoro-variants":
        data = commands.variants_cmd(args)
        if args.one:
            return 0  # a worker process: the parent saves the results
        path = _save(args.label, data)
    elif args.cmd == "grid":
        data = commands.grid_cmd(args)
        path = _save(args.label, data)
    elif args.cmd == "threads":
        data = commands.threads_cmd(args)
        path = _save(args.label, data)
    elif args.cmd == "qwen":
        data = qwen_lab.run(args)
        path = _save(args.label or f"qwen-{Path(args.model_dir).name}", data)
    elif args.cmd == "qwen-ab":
        data = qwen_lab.ab(args)
        path = _save("qwen-ab", data)
    else:
        data = commands.ab_cmd(args)
        path = _save("ab", data)
    print(f"\nresults: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
