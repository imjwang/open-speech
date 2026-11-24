"""Where the harness gets speech: real Kokoro audio (the bundled weights, or a variant downloaded into the scratch
directory), cached as WAV so repeated runs don't re-synthesize. Nothing here touches the owner's app data."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

ENGINE = Path(__file__).resolve().parents[2]
REPO = ENGINE.parent
BUNDLED_MODELS = REPO / "app" / "engine-runtime" / "models"
SR = 24000

SCRATCH = Path(
    os.environ.get(
        "AUDIOLAB_DIR",
        "/private/tmp/claude-501/-Users-jeff-Documents-repos-open-speech/"
        "9872468b-b7b5-40ee-83ec-139955c3d458/scratchpad/audiolab",
    )
)
RESULTS = Path(__file__).resolve().parent / "results"
LISTENING = Path.home() / "Downloads" / "open-speech-listening"

# Passages for the stretch measurements: long enough (5–9 s each) for stable statistics, with pauses.
PASSAGES: list[tuple[str, str]] = [
    ("af_heart", "The committee met on Tuesday to review the proposal, and after a long discussion about costs, "
                 "it agreed to fund the first phase of the project."),
    ("am_michael", "Rivers carve their valleys slowly, over thousands of years, but a single flood can move more "
                   "stone in an afternoon than the river moves in a decade."),
    ("bm_george", "Before you begin, read every question carefully. Some of them have more than one correct "
                  "answer, and partial credit is given."),
    ("af_bella", "She opened the window, listened to the rain for a moment, and then went back to the letter "
                 "she had been trying to finish all week."),
    ("ef_dora", "La biblioteca abre a las nueve de la mañana y cierra a las ocho de la noche, excepto los "
                "domingos, cuando permanece cerrada."),
]

# Variant comparison set: 40 English + 20 Spanish sentences, short to long.
EN_SENTENCES = [
    "Turn left at the next light.",
    "The meeting has been moved to Thursday.",
    "Please save your work before closing the window.",
    "Water boils at one hundred degrees Celsius at sea level.",
    "The library will be closed for renovations until the end of March.",
    "If the battery is low, the screen dims automatically to save power.",
    "He finished the marathon in just under four hours, which surprised everyone.",
    "Photosynthesis converts light energy into chemical energy stored in sugars.",
    "The quarterly report shows a modest increase in revenue and a sharp drop in costs.",
    "Most of the paintings in this room were completed during the artist's final decade.",
    "Our flight was delayed by two hours because of a storm over the mountains.",
    "To reset your password, follow the link we sent to your email address.",
    "The bridge was designed to withstand winds of up to two hundred kilometres per hour.",
    "Children under twelve must be accompanied by an adult at all times.",
    "She studied the map carefully, then folded it and put it back in her pocket.",
    "The orchestra tuned their instruments while the audience found their seats.",
    "A good night's sleep improves memory, mood, and concentration the next day.",
    "The recipe calls for two cups of flour, one egg, and a pinch of salt.",
    "Scientists have discovered a new species of frog in the rainforest.",
    "The museum offers free admission on the first Sunday of every month.",
    "Remember to water the plants while I'm away.",
    "Several witnesses described a tall man in a grey coat leaving the building.",
    "The software update fixes a bug that caused the app to crash on startup.",
    "Interest rates are expected to remain unchanged for the rest of the year.",
    "Our neighbours adopted a small dog with enormous ears.",
    "The train to Boston departs from platform nine at a quarter past six.",
    "Exercise regularly, eat well, and drink plenty of water.",
    "The novel follows three generations of a family living on a remote island.",
    "Please speak clearly and slowly so that everyone can understand you.",
    "He apologised for the confusion and promised to send the correct invoice.",
    "Thunder rolled across the valley as the first drops of rain began to fall.",
    "The new policy takes effect on the first of January.",
    "Is this the right way to the station?",
    "Wow, that was much easier than I expected!",
    "The data suggest a strong correlation, but not necessarily a causal one.",
    "Doctor Patel will see you in about fifteen minutes.",
    "Fold the paper in half, then in half again, and cut along the dotted line.",
    "Unfortunately, the item you ordered is currently out of stock.",
    "In the summer of nineteen sixty nine, astronauts first walked on the moon.",
    "The garden was quiet except for the humming of bees among the lavender.",
]
ES_SENTENCES = [
    "Gire a la izquierda en el próximo semáforo.",
    "La reunión se ha cambiado al jueves.",
    "Por favor, guarde su trabajo antes de cerrar la ventana.",
    "El agua hierve a cien grados al nivel del mar.",
    "La biblioteca estará cerrada por obras hasta finales de marzo.",
    "Si la batería está baja, la pantalla se oscurece para ahorrar energía.",
    "Terminó el maratón en poco menos de cuatro horas, lo que sorprendió a todos.",
    "El informe trimestral muestra un aumento modesto de los ingresos.",
    "Nuestro vuelo se retrasó dos horas por una tormenta sobre las montañas.",
    "Los niños menores de doce años deben ir acompañados de un adulto.",
    "Estudió el mapa con cuidado, lo dobló y lo guardó en el bolsillo.",
    "La orquesta afinó sus instrumentos mientras el público buscaba sus asientos.",
    "Dormir bien mejora la memoria, el ánimo y la concentración.",
    "La receta lleva dos tazas de harina, un huevo y una pizca de sal.",
    "Los científicos han descubierto una nueva especie de rana en la selva.",
    "El museo ofrece entrada gratuita el primer domingo de cada mes.",
    "¿Es este el camino correcto a la estación?",
    "La nueva política entra en vigor el primero de enero.",
    "Doble el papel por la mitad y corte por la línea de puntos.",
    "El tren a Madrid sale del andén nueve a las seis y cuarto.",
]


@dataclass
class Variant:
    name: str  # "q8", "fp16", "q8f16", "fp32"
    model_file: str  # relative to the variant's directory
    path: Path  # the .onnx file


def bundled_variant() -> Variant:
    d = BUNDLED_MODELS / "kokoro" / "kokoro-82m-v1.0"
    return Variant("q8", "onnx/model_quantized.onnx", d / "onnx" / "model_quantized.onnx")


def make_kokoro(variant: Variant | None = None, *, session_factory: Callable[[Path], Any] | None = None) -> Any:
    """A real `KokoroTts` reading the bundled tokenizer and voices, with the variant's model file."""
    from open_speech.config import Paths
    from open_speech.local.catalog import ModelCatalog
    from open_speech.providers import kokoro as kmod

    if not (BUNDLED_MODELS / "kokoro" / "kokoro-82m-v1.0" / "tokenizer.json").exists():
        raise SystemExit(f"no Kokoro weights at {BUNDLED_MODELS} (build the app runtime first)")
    v = variant or bundled_variant()
    home = SCRATCH / "home"
    paths = Paths(home).ensure()
    catalog = ModelCatalog.load(paths)
    factory = session_factory or kmod._default_session_factory

    def load(_p: Path) -> Any:
        return factory(v.path)

    return kmod.KokoroTts(catalog, paths, bundled_models=BUNDLED_MODELS, session_factory=load)


def _cache_path(tag: str, voice: str, text: str) -> Path:
    h = hashlib.sha256(f"{voice}\x00{text}".encode()).hexdigest()[:16]
    return SCRATCH / "sources" / tag / f"{voice}-{h}.wav"


def kokoro_audio(text: str, voice: str, *, variant: Variant | None = None, tts: Any | None = None) -> np.ndarray:
    v = variant or bundled_variant()
    p = _cache_path(f"kokoro-{v.name}", voice, text)
    if p.exists():
        x, _ = sf.read(str(p), dtype="float32")
        return np.asarray(x, dtype=np.float32)
    tts = tts or make_kokoro(v)
    pcm, _words = tts.synthesize_sync(text, "kokoro-82m-v1.0", voice)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), pcm, SR, subtype="FLOAT")
    return np.asarray(pcm, dtype=np.float32)


def passages(variant: Variant | None = None) -> list[tuple[str, str, np.ndarray]]:
    tts = None
    out = []
    for voice, text in PASSAGES:
        p = _cache_path(f"kokoro-{(variant or bundled_variant()).name}", voice, text)
        if not p.exists() and tts is None:
            tts = make_kokoro(variant)
        out.append((voice, text, kokoro_audio(text, voice, variant=variant, tts=tts)))
    return out


def write_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(np.asarray(x, dtype=np.float32), -1, 1), sr, subtype="PCM_24")


class Timer:
    def __enter__(self) -> Timer:
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *_a: object) -> None:
        self.s = time.perf_counter() - self.t0
