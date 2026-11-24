"""Word-timing estimators measured against Kokoro's exact word times (dev only, plays nothing).

    uv run python -m tools.timing_eval [--voices af_heart,bm_george,ef_dora] [--json out.json]

Kokoro reports exact word onsets. Each sentence is rendered with Kokoro, its timings are dropped, and the
word onsets are estimated again from the audio alone, as for a voice without timestamps (Chirp, Gemini,
OpenAI): with the old estimator (syllables spread over the voiced span, `estimate_word_timings`) and the
pause-anchored one (`estimate_word_timings_from_audio`). The error is |estimated onset − Kokoro onset| per word.

Kokoro weights come from the packaged runtime (`app/engine-runtime/models`, made by bundle-engine.sh) or from
OPEN_SPEECH_BUNDLED_RUNTIME; app data is never touched (a temporary home is used).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from open_speech.config import Paths
from open_speech.local.bundled import bundled_models_dir
from open_speech.local.catalog import ModelCatalog
from open_speech.playback.timing import estimate_word_timings, estimate_word_timings_from_audio, voiced_bounds
from open_speech.providers.base import WordTiming

REPO = Path(__file__).resolve().parents[2]
MODEL = "kokoro-82m-v1.0"

ENGLISH = [
    "The committee met on Tuesday to review the proposal, and it agreed that the work should continue.",
    "Before you begin, make sure the device is charged; otherwise, the update may fail halfway through.",
    "She paused at the door. Then, without a word, she turned around and walked back to the car.",
    "In 2019, the team published its first results, which were later confirmed by two independent groups.",
    "If the signal is weak, move closer to the router, restart it, and try again in a few minutes.",
    "The museum is open from nine to five, except on Mondays, when it is closed for maintenance.",
    "Most people underestimate how long a project will take: they plan for the best case, not the likely one.",
    "Water boils at a lower temperature at high altitude, because the air pressure is lower.",
    "He read the letter twice. It was short, polite, and completely unexpected.",
    "Our results suggest that sleep, more than diet or exercise, predicts how well students remember new material.",
    "The bridge, which opened in 1937, was the longest suspension bridge in the world at the time.",
    "Please send the signed form by Friday; we cannot process your application without it.",
    "When the rain finally stopped, the children ran outside to jump in the puddles.",
    "There are three things to remember: be on time, bring your notes, and ask questions.",
    "The algorithm sorts the list in place, so it needs almost no extra memory.",
    "I didn't expect the meeting to run so long, but the discussion turned out to be useful.",
    "According to the report, energy use fell by twelve percent last year, mostly in transport.",
    "The old lighthouse still stands on the cliff, although its lamp went dark decades ago.",
    "Turn left at the second light, then continue straight for about two miles.",
    "Scientists have long debated whether the dinosaurs died out suddenly or over millions of years.",
    "It was late, the streets were empty, and the only sound was the hum of the streetlights.",
    "The recipe calls for flour, sugar, two eggs, and a pinch of salt.",
    "Although the theory is elegant, it has never been tested against real data.",
    "We arrived early. The doors were still locked, so we waited in the café across the street.",
    "This chapter explains how memory works, why we forget, and what we can do about it.",
    "The price of the ticket includes the guided tour, but lunch is not included.",
    "After years of research, the company finally released a battery that charges in ten minutes.",
    "He spoke quietly, almost in a whisper, as if he were afraid someone might hear.",
    "The library will be closed next week while the new shelves are installed.",
    "Good writing is clear writing: short sentences, concrete words, and no unnecessary jargon.",
    "The river flooded twice that spring, destroying the crops on both banks.",
    "You can pause the reading at any time, and it will resume exactly where you left off.",
    "Mathematics, it has been said, is the language in which the universe is written.",
    "The first draft was too long, so the editor cut nearly half of it.",
    "On a clear night, far from the city, you can see thousands of stars with the naked eye.",
    "The study followed four hundred patients for five years; none of them reported serious side effects.",
    "Leave the keys on the table, close the windows, and lock the door behind you.",
    "Nobody knows exactly why the ancient city was abandoned, but drought is the leading theory.",
    "The new policy takes effect in January and applies to all full-time employees.",
    "Her answer was simple: if something matters to you, make time for it.",
]
SPANISH = [
    "El comité se reunió el martes para revisar la propuesta, y acordó que el trabajo debía continuar.",
    "Antes de empezar, asegúrate de que el dispositivo está cargado; si no, la actualización puede fallar.",
    "Se detuvo en la puerta. Luego, sin decir nada, dio media vuelta y volvió al coche.",
    "Si la señal es débil, acércate al router, reinícialo y vuelve a intentarlo en unos minutos.",
    "El museo abre de nueve a cinco, excepto los lunes, cuando cierra por mantenimiento.",
    "Leyó la carta dos veces. Era breve, educada y completamente inesperada.",
    "El puente, que se inauguró hace casi un siglo, era entonces el más largo del mundo.",
    "Por favor, envía el formulario firmado antes del viernes; sin él no podemos tramitar tu solicitud.",
    "Cuando por fin dejó de llover, los niños salieron corriendo a saltar en los charcos.",
    "Hay tres cosas que recordar: llegar a tiempo, traer tus apuntes y hacer preguntas.",
    "Según el informe, el consumo de energía bajó un doce por ciento el año pasado.",
    "El viejo faro sigue en pie sobre el acantilado, aunque su luz se apagó hace décadas.",
    "Gira a la izquierda en el segundo semáforo y sigue recto unos tres kilómetros.",
    "Era tarde, las calles estaban vacías y solo se oía el zumbido de las farolas.",
    "La receta lleva harina, azúcar, dos huevos y una pizca de sal.",
    "Aunque la teoría es elegante, nunca se ha comprobado con datos reales.",
    "Llegamos temprano. Las puertas seguían cerradas, así que esperamos en el café de enfrente.",
    "Este capítulo explica cómo funciona la memoria, por qué olvidamos y qué podemos hacer.",
    "Habló en voz baja, casi en un susurro, como si temiera que alguien lo oyera.",
    "Su respuesta fue sencilla: si algo te importa, búscale tiempo.",
]


def onset_errors(ref: list[WordTiming], est: list[WordTiming]) -> list[float]:
    """|estimated onset − reference onset| in seconds, for words both tokenizations start at the same offset."""
    by_start = {w.start: w for w in est}
    return [abs(by_start[w.start].t0 - w.t0) for w in ref if w.start in by_start]


def pct(xs: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(xs) * 1000.0, q)) if xs else float("nan")


def summary(xs: list[float]) -> dict[str, float]:
    return {"words": len(xs), "p50_ms": round(pct(xs, 50), 1), "p90_ms": round(pct(xs, 90), 1),
            "p99_ms": round(pct(xs, 99), 1), "mean_ms": round(float(np.mean(xs) * 1000.0), 1) if xs else float("nan")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voices", default="af_heart,am_michael,bf_emma,ef_dora,em_alex")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args(argv)

    from open_speech.providers.kokoro import KokoroTts

    bundled = bundled_models_dir() or (REPO / "app" / "engine-runtime" / "models")
    if not (bundled / "kokoro" / MODEL).is_dir():
        print(f"no Kokoro weights at {bundled}; run app/scripts/bundle-engine.sh first", file=sys.stderr)
        return 2
    home = Path(tempfile.mkdtemp(prefix="timing-eval-"))
    tts = KokoroTts(ModelCatalog.load(), Paths(home).ensure(), bundled_models=bundled)

    results: dict[str, Any] = {"per_voice": {}}
    totals: dict[str, dict[str, list[float]]] = {"en": {"old": [], "new": []}, "es": {"old": [], "new": []}}
    for voice in [v.strip() for v in args.voices.split(",") if v.strip()]:
        lang = "es" if voice.startswith("e") else "en"
        texts = SPANISH if lang == "es" else ENGLISH
        errs: dict[str, list[float]] = {"old": [], "new": []}
        for text in texts:
            pcm, ref = tts.synthesize_sync(text, MODEL, voice)
            sr = 24000
            start, end = voiced_bounds(pcm, sr)
            old = estimate_word_timings(text, pcm.size / sr, speech_start_s=start, speech_end_s=end)
            new = estimate_word_timings_from_audio(text, pcm, sr)
            errs["old"].extend(onset_errors(ref, old))
            errs["new"].extend(onset_errors(ref, new))
        totals[lang]["old"].extend(errs["old"])
        totals[lang]["new"].extend(errs["new"])
        results["per_voice"][voice] = {k: summary(v) for k, v in errs.items()}
        print(f"{voice:12s} old {summary(errs['old'])}  new {summary(errs['new'])}")
    for lang in ("en", "es"):
        if totals[lang]["old"]:
            results[lang] = {k: summary(v) for k, v in totals[lang].items()}
            print(f"{lang:12s} old {results[lang]['old']}  new {results[lang]['new']}")
    all_old = totals["en"]["old"] + totals["es"]["old"]
    all_new = totals["en"]["new"] + totals["es"]["new"]
    results["all"] = {"old": summary(all_old), "new": summary(all_new)}
    print(f"{'all':12s} old {results['all']['old']}  new {results['all']['new']}")
    if args.json:
        args.json.write_text(json.dumps(results, indent=2) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
