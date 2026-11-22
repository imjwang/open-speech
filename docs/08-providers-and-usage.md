# 08 — Providers, models, keys and usage

Status: Draft v0.1 · 2026-11-21 · Model IDs verified against live APIs on 2026-11-21 unless marked *(verify)*.

## Provider protocols

```python
class TtsProvider(Protocol):
    id: str                                   # "openai" | "gemini" | "kokoro" | "qwen3" | "openai_compatible"
    def voices(self) -> list[Voice]: ...
    def limits(self) -> TtsLimits: ...        # max input chars, formats, style support, languages
    async def synthesize(self, req: TtsRequest) -> AsyncIterator[AudioChunk]: ...
    timestamps: bool                          # True → chunks carry word/token timings
    def price_units(self, req: TtsRequest, result: TtsResult) -> Units: ...

class SttProvider(Protocol):
    async def stream(self, pcm: AsyncIterator[bytes], lang: str | None) -> AsyncIterator[SttEvent]: ...   # partial/final
    async def transcribe(self, audio: Path, words: bool = False) -> Transcript: ...

class LlmProvider(Protocol):      # Anthropic only, for now
    async def annotate(self, blocks, rules) -> list[SpanOp]: ...
    async def transform(self, section, rules) -> list[Utterance]: ...
    def agent(self, tools) -> AgentSession: ...

class VisionProvider(Protocol):   # Anthropic only, for now
    async def describe_or_transcribe(self, image: bytes, rules, mode: Literal["auto", "transcribe", "describe"]) -> list[Block]: ...
```

The rule: nothing outside `providers/` imports a vendor SDK. Each adapter ships with **recorded fixtures**
so tests run offline.

## Model catalog (`models.toml`)

The bundled catalog lives at `engine/src/open_speech/data/models.toml`; user overrides in app data are
merged over it. Model IDs change often (Gemini 2.5 TTS was retired in Oct 2026, and Gemini's
`models.list` doesn't even show TTS models), so **nothing is hard-coded outside the catalog**.

```toml
[tts.openai."gpt-4o-mini-tts"]
snapshot = "gpt-4o-mini-tts-2025-12-15"
voices = ["alloy","ash","ballad","cedar","coral","echo","fable","marin","nova","onyx","sage","shimmer","verse"]  # (verify list)
formats = ["opus","mp3","aac","flac","wav","pcm"]
style = "instructions"
stream = true
max_input_chars = 4000          # (verify)
timestamps = false

[tts.gemini."gemini-3.8-flash-tts"]
api = "interactions"            # client.interactions.create, api_version v1beta, google-genai >= 2.25
voices = ["Zephyr","Puck","Charon","Kore","Fenrir","Leda","Orus","Aoede","Callirrhoe","Autonoe","Enceladus","Iapetus","Umbriel","Algieba","Despina","Erinome","Algenib","Rasalgethi","Laomedeia","Achernar","Alnilam","Schedar","Gacrux","Pulcherrima","Achird","Zubenelgenubi","Vindemiatrix","Sadachbia","Sadaltager","Sulafat"]
voices_endpoint = true          # client.voices.list() for the larger library
format = "l16"                  # 24 kHz mono PCM16 when streaming; wav when unary
style = "speech_metadata"
stream = true
timestamps = false

[tts.gemini."gemini-3.8-flash-lite-tts"]
inherit = "gemini-3.8-flash-tts"
languages = 101

[tts.kokoro."kokoro-82m-v1.0"]
local = true
weights = { url = "hf://onnx-community/Kokoro-82M-v1.0-ONNX-timestamped", quant = "q8", size_mb = 92, sha256 = "<pin>" }
ram_gb = 1.3
license = "Apache-2.0"
languages = ["en-US","en-GB"]   # more via optional phonemizer extras
timestamps = true

[tts.qwen3."qwen3-tts-12hz-0.6b"]            # P2, preset voices only (no cloning)
local = true
runtimes = ["mlx-audio"]         # Apple Silicon
size_gb = 1.3                    # GGUF Q8; ~2.5 GB bf16
ram_gb = 3
license = "Apache-2.0"
languages = ["zh","en","ja","ko","de","fr","ru","pt","es","it"]
requires = "apple-silicon"
timestamps = false

[stt.openai]
streaming = ["gpt-realtime-whisper", "gpt-live-transcribe"]   # (verify streaming semantics)
batch = ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gpt-transcribe"]
word_timestamps = ["whisper-1"]

[stt.gemini]
streaming = ["gemini-3.8-live"]       # (verify) Live API input transcription
batch = ["gemini-3.5-transcribe"]     # word timestamps

[llm.anthropic]
default = "claude-sonnet-5-5"
options = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-2"]
```

### Verified on 2026-11-21 (grounding spike)

| Check | Result |
|---|---|
| OpenAI `gpt-4o-mini-tts` streaming Opus, one sentence | First byte **1.38 s**, total 2.0 s |
| OpenAI audio models present | `gpt-4o-mini-tts(-2025-12-15)`, `tts-1(-hd)`, `gpt-4o(-mini)-transcribe`, `gpt-transcribe`, `gpt-live-transcribe`, `gpt-realtime-whisper`, `gpt-realtime-2.1(-mini)`, `whisper-1` |
| Gemini TTS via `generateContent` + `speechConfig` | **Rejected** (400: unknown field). Use the Interactions API |
| Gemini `gemini-3.8-flash-tts` / `-lite-tts` via Interactions, streaming | First audio **1.97 s**; unary 4.7–5.0 s; `Kore` voice OK |
| Claude models present | `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`, `claude-fable-5-2`, … |
| Claude annotate, numeric offsets, adaptive thinking low | Correct, 7.6 s, 925 output tokens |
| Claude annotate, numeric offsets, thinking off | **Wrong offsets** |
| Claude annotate, **quote-anchored**, Sonnet 5.5 thinking off | **Correct** (2/2), 2.1–2.5 s, 138 output tokens → the default |
| Claude annotate, quote-anchored, Haiku 5.5 low | Wrong judgments (dropped a cited author's name) → not recommended |

## Local models

**Tiering**

| Tier | Model | Runtime | Download / RAM | Speed | Word timing | Status |
|---|---|---|---|---|---|---|
| Default local | **Kokoro-82M v1.0** (Apache-2.0) | onnxruntime (CPU) | ~92 MB q8 / ≤ 1.3 GB | ~10x real time on CPU | Exact (timestamped export) | **P0**, included in packaged builds |
| Later | **Qwen3-TTS 0.6B** (Apache-2.0), preset voices only | mlx-audio (Apple Silicon) | ~1.3 GB Q8 / 2–3 GB | ~1.3–1.8x real time on M-series (MLX, third-party) | Via alignment | P2 |
| BYO server | Kokoro-FastAPI, mlx-audio server, vLLM-Omni, Speaches, qwentts.cpp | `openai_compatible` adapter | — | — | Via alignment (Kokoro-FastAPI `/dev/captioned_speech` optional) | P1 |
| Local STT | Moonshine v2 (streaming, EN), Parakeet TDT v3 (25 langs, CC-BY-4.0) | sherpa-onnx | 0.1–0.6 GB | real time+ | — | P1 |
| Local alignment | Qwen3-ForcedAligner-0.6B (Apache-2.0) or whisper.cpp DTW | Python / mlx-audio / whisper.cpp | ~0.6–1 GB | background | — | v1.1 |

**Voice cloning and custom voices are out of scope**: catalog entries expose preset voices only, and the larger Qwen3-TTS 1.7B (voice design, cloning) is not included.

Excluded from the bundled catalog for license or hardware reasons (users can still add them via BYO
server): F5-TTS (CC-BY-NC), Fish/OpenAudio (non-commercial), IndexTTS-2 (restricted), Voxtral TTS
(CC-BY-NC), VibeVoice (repos withdrawn), Piper (GPL engine), and 1.6B+ GPU-only models.

**Model manager** (`local/manager.py`):
- Hardware probe: Apple Silicon generation and unified memory. Marks each catalog entry `fits | slow | unsupported`.
- Installs show size, RAM and expected speed **before** confirming (by voice too).
- Downloads resume, verify sha256 and land in `models/`. Optional Python extras install into the engine environment with `uv pip install open-speech[local-stt]`.
- Idle models unload after 10 minutes (NFR-9).
- **License note:** some Kokoro phonemizer paths fall back to espeak-ng (GPL). The default install uses the English G2P without espeak. Espeak-backed languages are an optional, clearly-labelled extra.

## Keys

| Context | Source | Notes |
|---|---|---|
| Development | Repo-root `.env` read by pydantic-settings (`SecretStr`) | For local testing, copy `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GEMINI_API_KEY` from `../aden/.env`. `.env` is gitignored |
| Packaged app | OS keychain via `keyring` (service `open-speech`) | Pasted into the command bar; provider detected by prefix (`sk-ant-` → Anthropic, `sk-` → OpenAI, `AIza` → Gemini); a test call before saving |
| CLI | `open-speech keys set <provider>` (no echo) or env vars | Env overrides keychain |

The engine reports only `configured | missing | invalid`. With a key missing, dependent features
disappear from voice lists and tool results with a reason. With no keys at all, Open Speech still reads
with Kokoro and Classic rules.

## Usage ledger

Pattern adapted from `aden/aden/billing/ledger.py` and `cost.py`, simplified to local estimates.

```sql
CREATE TABLE ledger (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                 -- ISO 8601 UTC
  provider TEXT NOT NULL,           -- anthropic | openai | gemini | kokoro | qwen3 | openai_compatible
  model TEXT NOT NULL,
  op TEXT NOT NULL,                 -- tts | stt | annotate | transform | vision | chat | compile | preview
  units_json TEXT NOT NULL,         -- {chars, in_tok, out_tok, cache_read_tok, cache_write_tok, audio_sec, frames, images}
  usd_estimate TEXT NOT NULL,       -- decimal string; "0" for cache hits and local runs
  pricing_version TEXT NOT NULL,
  document_id TEXT,
  turn_id TEXT,
  cache_hit INTEGER NOT NULL DEFAULT 0,
  local INTEGER NOT NULL DEFAULT 0
);
```

- **Claude:** units come from `response.usage` (exact input, output, cache-read and cache-write tokens).
- **OpenAI TTS:** estimated from input chars → tokens and output audio seconds, priced per `pricing.toml`.
- **Gemini TTS:** `usage_metadata` tokens when present; otherwise estimated.
- **STT:** audio seconds.
- **Cache hits** write a `$0` row with `cache_hit = 1` and the *avoided* cost in `units_json.avoided_usd`, which powers "cache saved you $1.20".

## Pricing catalog (`pricing.toml`)

```toml
version = "2026-11-21"
currency = "USD"

[anthropic."claude-sonnet-5-5"]
input_per_mtok = "2.00"
output_per_mtok = "10.00"
cache_read_per_mtok = "0.10"

[anthropic."claude-opus-5-5"]
input_per_mtok = "4.00"
output_per_mtok = "20.00"
cache_read_per_mtok = "0.20"

[anthropic."claude-haiku-5-5"]
input_per_mtok = "0.10"
output_per_mtok = "0.50"

[openai."gpt-4o-mini-tts"]
# (verify) text input + audio output pricing; approx $0.015 per audio minute
text_input_per_mtok = "0.60"
audio_output_per_mtok = "12.00"

[gemini."gemini-3.8-flash-tts"]
# (verify)

[openai."whisper-1"]
per_audio_minute = "0.006"   # (verify)
```

Prices are dated and versioned, overridable in app data, and updated by PR. Every number the UI or voice
reports is labelled **estimated**.

## Budgets and estimates

- **Monthly budget** (`settings.toml` `[budget] monthly_usd`, `warn_at = 0.8`, `hard_stop = true`).
  - At 80%: a spoken notice and a bar row, once per threshold crossing.
  - At 100%: paid calls are refused with `budget_exceeded`. Reading continues on Kokoro and Classic rules. The user can override ("raise my budget to $20", confirmed).
- **Pre-flight estimate** when a single operation is estimated above `estimate_threshold_usd` (default `0.50`). Applies to large documents in AI Enhanced mode, whole-document exports, transforms and keep-offline pins.
  - **Estimator:** extracted chars → TTS chars after Classic filters, minus cached utterances; annotate tokens ≈ block tokens × 1.1 + output allowance; vision per image/frame. Target ±25% for text documents (FR-COST-3).
- **P2:** "prepare whole document" through the Claude Message Batches API (50% cheaper, minutes to hours) for books queued in advance.

### Reference costs (estimates, Sonnet 5.5 + OpenAI TTS)

| Item | Approx. |
|---|---|
| 300-page book, AI Enhanced annotate | ~$0.45 (Claude) |
| 300-page book, OpenAI TTS (~11 h audio) | ~$10 |
| Same book on Kokoro | $0 |
| One image described | ~$0.005 |
| One assistant turn | ~$0.002–0.01 |
