"""Regenerate the handwritten provider fixtures in this directory (deterministic).

    cd engine && uv run python tests/fixtures/providers/make_fixtures.py

Shapes follow each vendor's documented wire format (OpenAI audio.speech Ogg/Opus bytes and transcription
JSON; Gemini Interactions SSE and unary JSON; Anthropic Messages JSON and SSE; Google Cloud Text-to-Speech v1
REST JSON). No real API output is
stored here: there were no keys and the APIs were unreachable when these were written.
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import numpy as np
import soundfile as sf

HERE = Path(__file__).parent


def tone(freq: float, seconds: float, sr: int) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.hanning(t.size) * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def sse(events: list[dict], *, key: str) -> str:
    return "".join(f"event: {e[key]}\ndata: {json.dumps(e)}\n\n" for e in events)


CHIRP_STARS = [
    "Achernar", "Achird", "Algenib", "Algieba", "Alnilam", "Aoede", "Autonoe", "Callirrhoe", "Charon", "Despina",
    "Enceladus", "Erinome", "Fenrir", "Gacrux", "Iapetus", "Kore", "Laomedeia", "Leda", "Orus", "Puck",
    "Pulcherrima", "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Sulafat", "Umbriel", "Vindemiatrix",
    "Zephyr", "Zubenelgenubi",
]
CHIRP_MALE = {"Achird", "Algenib", "Algieba", "Alnilam", "Charon", "Enceladus", "Fenrir", "Iapetus", "Orus", "Puck",
              "Rasalgethi", "Sadachbia", "Sadaltager", "Schedar", "Umbriel", "Zubenelgenubi"}


def google_fixtures() -> None:
    """Google Cloud Text-to-Speech v1 (REST): text:synthesize, voices.list and the 403 a key gets when Cloud
    Text-to-Speech isn't among its allowed APIs (synthesis with this build's key, 2026-11-22: status
    PERMISSION_DENIED, ErrorInfo reason API_KEY_SERVICE_BLOCKED; project number zeroed). The same key's
    voices.list answered PERMISSION_DENIED with reason ACCESS_DENIED ("Permission denied on the service.");
    tests/unit/runtime/test_google_tts.py covers that shape inline."""
    # LINEAR16 at 24 kHz comes back as a WAV file (44-byte RIFF header). Two tones with 300 ms of silence between.
    sr = 24000
    pcm = np.concatenate([tone(220.0, 0.4, sr), np.zeros(int(0.3 * sr), np.float32), tone(330.0, 0.4, sr)])
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="WAV", subtype="PCM_16")
    (HERE / "google_tts_synthesize.json").write_text(
        json.dumps({"audioContent": base64.b64encode(buf.getvalue()).decode()}, indent=2) + "\n"
    )

    voices = []
    for locale in ("en-US", "en-GB", "es-US", "es-ES"):
        for name in (f"{locale}-Neural2-A", f"{locale}-Chirp-HD-D", f"{locale}-Standard-B"):
            voices.append(
                {"languageCodes": [locale], "name": name, "ssmlGender": "MALE", "naturalSampleRateHertz": 24000}
            )
        for star in CHIRP_STARS:
            if locale == "es-ES" and star == "Sadachbia":
                continue  # one catalog voice the API doesn't list: it must not be offered
            voices.append({
                "languageCodes": [locale],
                "name": f"{locale}-Chirp3-HD-{star}",
                "ssmlGender": "MALE" if star in CHIRP_MALE else "FEMALE",
                "naturalSampleRateHertz": 24000,
            })
    (HERE / "google_tts_voices.json").write_text(json.dumps({"voices": voices}, indent=2) + "\n")

    method = "google.cloud.texttospeech.v1.TextToSpeech.SynthesizeSpeech"
    message = f"Requests to this API texttospeech.googleapis.com method {method} are blocked."
    blocked = {
        "error": {
            "code": 403,
            "message": message,
            "status": "PERMISSION_DENIED",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "API_KEY_SERVICE_BLOCKED",
                    "domain": "googleapis.com",
                    "metadata": {
                        "consumer": "projects/000000000000",
                        "service": "texttospeech.googleapis.com",
                        "methodName": method,
                        "apiName": "texttospeech.googleapis.com",
                    },
                },
                {"@type": "type.googleapis.com/google.rpc.LocalizedMessage", "locale": "en-US", "message": message},
            ],
        }
    }
    (HERE / "google_tts_403_service_blocked.json").write_text(json.dumps(blocked, indent=2) + "\n")


def main() -> None:
    google_fixtures()
    # OpenAI audio.speech, response_format="opus": Ogg/Opus at 48 kHz.
    buf = io.BytesIO()
    sf.write(buf, tone(330.0, 0.8, 48000), 48000, format="OGG", subtype="OPUS")
    (HERE / "openai_speech.ogg").write_bytes(buf.getvalue())

    # Gemini Interactions streaming TTS: two L16 24 kHz deltas, then completion with usage.
    pcm = (tone(220.0, 0.6, 24000) * 32767).astype("<i2").tobytes()
    half = len(pcm) // 2
    events = [
        {"event_type": "interaction.created", "interaction": {"id": "int_tts_1", "status": "in_progress"}},
        {"event_type": "step.start", "index": 0, "step": {"type": "model_output", "content": []}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "audio", "mime_type": "audio/l16",
                                                            "sample_rate": 24000,
                                                            "data": base64.b64encode(pcm[:half]).decode()}},
        {"event_type": "step.delta", "index": 0, "delta": {"type": "audio", "mime_type": "audio/l16",
                                                            "sample_rate": 24000,
                                                            "data": base64.b64encode(pcm[half:]).decode()}},
        {"event_type": "step.stop", "index": 0},
        {"event_type": "interaction.completed",
         "interaction": {"id": "int_tts_1", "status": "completed",
                         "usage": {"total_input_tokens": 14, "total_output_tokens": 15}}},
    ]
    (HERE / "gemini_tts_stream.sse").write_text(sse(events, key="event_type"))
    err = [
        {"event_type": "interaction.created", "interaction": {"id": "int_tts_2", "status": "in_progress"}},
        {"event_type": "error", "error": {"code": "503", "message": "The model is overloaded."}},
    ]
    (HERE / "gemini_tts_error.sse").write_text(sse(err, key="event_type"))

    # Gemini unary transcription with word_info annotations.
    (HERE / "gemini_transcribe.json").write_text(json.dumps({
        "id": "int_stt_1", "status": "completed", "model": "gemini-3.5-transcribe",
        "steps": [{"type": "model_output", "content": [{
            "type": "text", "text": "skip the footnotes",
            "annotations": [
                {"type": "word_info", "text": "skip", "start_offset": "0.120s", "end_offset": "0.400s"},
                {"type": "word_info", "text": "the", "start_offset": "0.420s", "end_offset": "0.520s"},
                {"type": "word_info", "text": "footnotes", "start_offset": "0.540s", "end_offset": "1.100s"},
            ]}]}],
        "usage": {"total_input_tokens": 36, "total_output_tokens": 4},
    }, indent=1))

    # OpenAI transcriptions.
    (HERE / "openai_transcription.json").write_text(json.dumps({"text": "read my clipboard"}))
    (HERE / "openai_transcription_verbose.json").write_text(json.dumps({
        "task": "transcribe", "language": "english", "duration": 1.2, "text": "read my clipboard",
        "words": [{"word": "read", "start": 0.1, "end": 0.35}, {"word": "my", "start": 0.4, "end": 0.5},
                  {"word": "clipboard", "start": 0.55, "end": 1.1}],
    }, indent=1))

    # Anthropic Messages (structured outputs → a single JSON text block).
    def message(text: str, *, stop: str = "end_turn", usage: dict | None = None, mid: str = "msg_01") -> dict:
        return {
            "id": mid, "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": usage or {"input_tokens": 812, "output_tokens": 138,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1450},
        }

    ops = {"ops": [
        {"block_id": "blk_1", "op": "skip", "quote": " (Smith et al., 2020)", "occurrence": 1, "text": "",
         "rule_id": "rule_citations"},
        {"block_id": "blk_1", "op": "replace", "quote": "e.g.", "occurrence": 1, "text": "for example",
         "rule_id": ""},
        {"block_id": "blk_999", "op": "skip", "quote": "ghost", "occurrence": 1, "text": "", "rule_id": ""},
    ]}
    (HERE / "anthropic_annotate.json").write_text(json.dumps(message(json.dumps(ops)), indent=1))
    (HERE / "anthropic_refusal.json").write_text(json.dumps(message("", stop="refusal", mid="msg_r"), indent=1))
    (HERE / "anthropic_truncated.json").write_text(json.dumps(message('{"ops": [', stop="max_tokens"), indent=1))
    compiled = {
        "name": "Skip footnotes", "description": "Don't read footnotes or their markers.", "tier": "filter",
        "compiled": {"drop_blocks": ["footnote"], "drop_marks": ["footnote_ref"], "replace": [],
                     "keep_blocks_only": []},
        "scope_suggestion": {"sites": ["arxiv.org"], "types": [], "docs": []},
        "instruction": "stop reading the footnotes on this site",
    }
    (HERE / "anthropic_compile.json").write_text(json.dumps(message(json.dumps(compiled), mid="msg_c"), indent=1))
    vision = {"kind": "describe", "blocks": [
        {"type": "figure", "text": "A bar chart comparing three quarters of revenue."},
        {"type": "paragraph", "text": "   "}]}
    (HERE / "anthropic_vision.json").write_text(json.dumps(message(json.dumps(vision), mid="msg_v", usage={
        "input_tokens": 1250, "output_tokens": 40, "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0}), indent=1))

    transform = json.dumps({"utterances": ["The methods section compares two models.", "Both are small."],
                            "anchor_block_ids": ["blk_1", "blk_404"]})
    parts = [transform[i : i + 40] for i in range(0, len(transform), 40)]
    events = [
        {"type": "message_start", "message": {
            "id": "msg_t", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 1, "cache_creation_input_tokens": 300,
                      "cache_read_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "",
                                                                      "signature": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta",
                                                              "thinking": "Summarize briefly."}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "c2ln"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        *[{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": p}} for p in parts],
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 210}},
        {"type": "message_stop"},
    ]
    (HERE / "anthropic_transform.sse").write_text(sse(events, key="type"))


if __name__ == "__main__":
    main()
