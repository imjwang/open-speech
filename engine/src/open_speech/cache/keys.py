"""Content-addressed cache keys for audio and timings (05 § Caches and keys).

The audio key covers exactly what changes the waveform: utterance text, provider, model, voice, style,
container format and the engine's audio pipeline version. **Speed is never part of a key**: speed is
applied in the transport's stream, so one cached file serves every speed.
"""

from __future__ import annotations

import unicodedata

from open_speech.ids import sha256_hex

ENGINE_AUDIO_VERSION = "1"  # bump when the encode path changes audibly (forces re-synthesis)
DEFAULT_FORMAT = "ogg-opus"


def normalize_text(text: str) -> str:
    """NFC so visually identical text from different sources shares audio."""
    return unicodedata.normalize("NFC", text)


def audio_key(
    text: str,
    provider: str,
    model: str,
    voice: str,
    style: str | None = None,
    fmt: str = DEFAULT_FORMAT,
    *,
    engine_audio_version: str = ENGINE_AUDIO_VERSION,
) -> str:
    return sha256_hex(
        "audio",
        normalize_text(text),
        provider,
        model,
        voice,
        (style or "").strip(),
        fmt,
        engine_audio_version,
    )


def timings_key(audio: str, aligner_version: str) -> str:
    """Refined word timings for cached audio (v1.1 forced alignment)."""
    return sha256_hex("timings", audio, aligner_version)
