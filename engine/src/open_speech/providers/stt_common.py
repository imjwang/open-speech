"""Shared STT plumbing: buffering mic PCM into batch transcriptions (P0) with optional interim partials.

P0 approach (documented gap): the cloud adapters do *batch* transcription of the buffered utterance when the
mic stops (push-to-talk release / end of speech), emitting a single `final`. With
`partial_interval_s` set, they also re-transcribe the growing buffer every N seconds of new audio and emit
`partial` events. That costs extra audio-seconds (O(n²) in utterance length, but cents per hour on the mini
models), so it is off by default. Real streaming partials via the OpenAI Realtime / Gemini Live
transcription sockets are the stretch goal.

Mic frames are 16-bit LE PCM, 16 kHz mono (04 § WebSocket protocol).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from open_speech.providers.base import SttEvent, Units
from open_speech.providers.pcm import pcm16_wav_bytes

MIC_RATE = 16000
BYTES_PER_SEC = MIC_RATE * 2
UsageSink = Callable[[str, str, str, Units], None]  # (provider, model, op, units)


async def buffered_stream(
    pcm: AsyncIterator[bytes],
    transcribe_wav: Callable[[bytes, bool], Awaitable[str]],
    *,
    partial_interval_s: float | None = None,
    min_audio_s: float = 0.15,
) -> AsyncIterator[SttEvent]:
    """`transcribe_wav(wav_bytes, interim)` → text. Yields partials (optional) then exactly one final."""
    buf = bytearray()
    last_partial_at = 0
    last_text = ""
    step = int((partial_interval_s or 0) * BYTES_PER_SEC)
    async for frame in pcm:
        buf.extend(frame)
        if step and len(buf) - last_partial_at >= step:
            last_partial_at = len(buf)
            text = (await transcribe_wav(pcm16_wav_bytes(bytes(buf), MIC_RATE), True)).strip()
            if text and text != last_text:
                last_text = text
                yield SttEvent(kind="partial", text=text)
    if len(buf) < int(min_audio_s * BYTES_PER_SEC):
        yield SttEvent(kind="final", text="")
        return
    text = (await transcribe_wav(pcm16_wav_bytes(bytes(buf), MIC_RATE), False)).strip()
    yield SttEvent(kind="final", text=text)


def mime_for(name: str) -> str:
    low = name.lower()
    if low.endswith(".ogg") or low.endswith(".opus"):
        return "audio/ogg"
    if low.endswith(".mp3"):
        return "audio/mp3"
    if low.endswith(".flac"):
        return "audio/flac"
    if low.endswith(".webm"):
        return "audio/webm"
    return "audio/wav"


def parse_offset(v: object) -> float:
    """Gemini durations look like "1.250s"."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().rstrip("s")
        try:
            return float(s)
        except ValueError:
            return 0.0
    return 0.0
