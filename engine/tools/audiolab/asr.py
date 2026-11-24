"""Round-trip intelligibility: transcribe synthesized speech with the bundled Parakeet model and compare with the
text that was spoken (word error rate). A machine listener can't judge beauty, but it does notice dropped or
smeared phonemes."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

import numpy as np

from tools.audiolab.sources import BUNDLED_MODELS, SR

# Parakeet writes numbers as digits ("100", "1969") and some words in their US spelling; the sentences spell them
# out. Numbers are left out of the comparison on both sides and a few spellings unified, so the score counts
# misheard speech rather than formatting.
_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
    "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty",
    "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "first", "cero", "uno", "una", "dos",
    "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez", "once", "doce", "trece", "catorce",
    "quince", "cien", "ciento", "mil", "primero",
}
_SPELLING = {"neighbours": "neighbors", "apologised": "apologized", "kilometres": "km", "kilometers": "km",
             "grey": "gray", "dr": "doctor", "colour": "color"}


def normalize(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("'s", "s").replace("’s", "s").replace("-", " ")
    t = re.sub(r"[^\w\s]", " ", t)
    return [_SPELLING.get(w, w) for w in t.split() if not w.isdigit() and w not in _NUMBER_WORDS]


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """(edits, reference words)."""
    r, h = normalize(ref), normalize(hyp)
    d = np.arange(len(h) + 1)
    for i in range(1, len(r) + 1):
        prev = d.copy()
        d[0] = i
        for j in range(1, len(h) + 1):
            d[j] = min(prev[j] + 1, d[j - 1] + 1, prev[j - 1] + (r[i - 1] != h[j - 1]))
    return int(d[len(h)]), len(r)


class Parakeet:
    def __init__(self) -> None:
        from open_speech.local.catalog import ModelCatalog
        from open_speech.providers.local_stt import SherpaRecognizer

        entry = ModelCatalog.load().default_local_stt_model()
        if entry is None:
            raise RuntimeError("no local speech-to-text model in the catalog")
        d = BUNDLED_MODELS / "stt" / entry.model
        if not d.is_dir():
            raise RuntimeError(f"Parakeet weights not found at {d}")
        self.model = entry.model
        self._rec: Any = SherpaRecognizer(entry, d)

    def transcribe(self, x: np.ndarray, lang: str = "en", sr: int = SR) -> str:
        import soxr

        y = np.asarray(soxr.resample(np.asarray(x, dtype=np.float32), sr, 16000, quality="VHQ"), dtype=np.float32)
        return str(self._rec.transcribe(y, lang))


def available() -> bool:
    try:
        Parakeet()
    except Exception:  # noqa: BLE001
        return False
    return True
